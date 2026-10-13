#!/usr/bin/env python3
"""
<summary>
Unit test for addon_api.py, the dashboard's side of the shared add-on API: which
add-ons are found, which paths are refused, and exactly what the proxy passes
through each way.
</summary>
<remarks>
No Kodi is needed. The xbmc modules are stubbed in sys.modules before addon_api is
imported: Addons.GetAddons answers from a list here, and the Home window is a
dict. A fake add-on runs on a real loopback port and records every request it
gets, so the test sees what actually crossed the wire, not what the code meant
to send. Exits non zero when a check fails.

The build does not run it yet, so run it by hand before every release.

Usage:
    python3 tests/test_addon_api.py
</remarks>
"""

import json
import os
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADDON_DIR = os.path.join(ROOT, "script.jellystat")

# ---- Kodi stubs -----------------------------------------------------------

ADDONS = []
PROPS = {}


def fake_jsonrpc(request):
    """
    <summary>
    Answer Addons.GetAddons from ADDONS; anything else is a test failure.
    </summary>
    """
    req = json.loads(request)
    assert req["method"] == "Addons.GetAddons", req["method"]
    assert req["params"]["enabled"] is True, "only enabled add-ons are asked for"
    return json.dumps({"result": {"addons": [{"addonid": a, "name": n} for a, n in ADDONS]}})


class _Window:
    """<summary>Stand-in for xbmcgui.Window over the PROPS dict.</summary>"""

    def __init__(self, window_id):
        assert window_id == 10000, "the Home window"

    def getProperty(self, key):
        return PROPS.get(key, "")


xbmc = types.ModuleType("xbmc")
xbmc.LOGDEBUG, xbmc.LOGINFO, xbmc.LOGWARNING, xbmc.LOGERROR = 0, 1, 2, 3
xbmc.log = lambda msg, level=1: None
xbmc.executeJSONRPC = fake_jsonrpc
xbmcgui = types.ModuleType("xbmcgui")
xbmcgui.Window = _Window
sys.modules["xbmc"] = xbmc
sys.modules["xbmcgui"] = xbmcgui
sys.path.insert(0, ADDON_DIR)

import addon_api  # noqa: E402

# ---- a fake add-on --------------------------------------------------------

SEEN = []
TOKEN = "t" * 43


class _FakeAddon(BaseHTTPRequestHandler):
    """
    <summary>
    Records each request, then answers /info, echoes anything else, and sends a
    header the proxy must not pass back.
    </summary>
    """

    def log_message(self, fmt, *args):
        pass

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        SEEN.append({"method": self.command, "path": self.path,
                     "headers": dict(self.headers.items()), "body": body})
        if self.headers.get("X-Addon-Api-Token") != TOKEN:
            payload, status = {"error": {"code": "unauthorised", "message": "no"}}, 401
        elif self.path == "/info":
            payload, status = {"api": 1, "id": "service.fake", "resources": []}, 200
        else:
            payload, status = {"echo": self.path}, 200
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("ETag", '"abc"')
        self.send_header("Content-Disposition", 'attachment; filename="b.json"')
        self.send_header("Set-Cookie", "stolen=1")
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_DELETE = _answer


def start_fake():
    """<summary>Start the fake add-on on a free loopback port; returns the server.</summary>"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeAddon)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def free_port():
    """<summary>A loopback port nobody is listening on, for the not answering case.</summary>"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeAddon)
    port = server.server_address[1]
    server.server_close()
    return port


# ---- helpers --------------------------------------------------------------

FAILURES = []


def check(condition, message):
    """<summary>Record a failure without stopping.</summary>"""
    if not condition:
        FAILURES.append(message)
        print("FAIL: %s" % message)


def equal(actual, expected, what):
    """<summary>check() for equality, printing both sides on failure.</summary>"""
    check(actual == expected, "%s: expected %r, got %r" % (what, expected, actual))


def refused(call, status, code, what):
    """<summary>Check a call raises ProxyError with this status and code.</summary>"""
    try:
        call()
    except addon_api.ProxyError as err:
        equal((err.status, err.code), (status, code), what)
        check(bool(err.message), "%s: has a message" % what)
        return
    check(False, "%s: was not refused" % what)


def announce(addon_id, **value):
    """<summary>Set an add-on's AddonApi property.</summary>"""
    PROPS["AddonApi." + addon_id] = json.dumps(value)


# ---- tests ----------------------------------------------------------------

def test_discover(port):
    """<summary>Only well formed announcements from enabled add-ons count.</summary>"""
    print("running test_discover")
    ADDONS[:] = [("service.fake", "Fake"), ("service.none", "None"), ("service.junk", "Junk"),
                 ("service.badport", "Bad port"), ("service.boolapi", "Bool api"),
                 ("Service.Upper", "Upper")]
    PROPS.clear()
    announce("service.fake", api=1, port=port, token=TOKEN, version="1.0")
    PROPS["AddonApi.service.junk"] = "{not json"
    announce("service.badport", api=1, port=70000, token=TOKEN)
    announce("service.boolapi", api=True, port=port, token=TOKEN)
    announce("Service.Upper", api=1, port=port, token=TOKEN)
    announce("service.notenabled", api=1, port=port, token=TOKEN)
    found = addon_api.discover()
    equal(sorted(found), ["service.fake"], "only the well formed, enabled, lowercase id")
    equal(found["service.fake"]["name"], "Fake", "name from Kodi")


def test_list(port):
    """<summary>Each add-on listed with its /info, or with why it cannot be managed.</summary>"""
    print("running test_list")
    ADDONS[:] = [("service.fake", "Fake"), ("service.future", "Future"), ("service.down", "Down")]
    PROPS.clear()
    announce("service.fake", api=1, port=port, token=TOKEN, version="1.0")
    announce("service.future", api=2, port=port, token=TOKEN)
    announce("service.down", api=1, port=free_port(), token=TOKEN)
    entries = {a["id"]: a for a in addon_api.list_addons()["addons"]}
    equal(entries["service.fake"].get("info", {}).get("id"), "service.fake", "info fetched")
    check("problem" not in entries["service.fake"], "a working add-on has no problem")
    check("version 2" in entries["service.future"].get("problem", ""), "newer API named")
    check("not answering" in entries["service.down"].get("problem", ""), "down add-on named")
    check(all("token" not in a and "port" not in a for a in entries.values()),
          "no token or port reaches the browser")


def test_paths():
    """<summary>Only plain lowercase paths below a plausible add-on id get through.</summary>"""
    print("running test_paths")
    equal(addon_api.check_path("service.engage/slots/3"), ("service.engage", "/slots/3"), "good path")
    equal(addon_api.check_path("service.engage/options/sequence_items"),
          ("service.engage", "/options/sequence_items"), "underscores allowed")
    for bad in ("service.engage", "service.engage/", "service.engage/../x", "service.engage/a//b",
                "service.engage/Slots", "service.engage/slots/", "service.engage/a.b",
                "../etc/slots", "service engage/slots", "/slots", "service.engage/%2e%2e"):
        refused(lambda: addon_api.check_path(bad), 404, "not_found", "path %r refused" % bad)


def test_forward(port):
    """<summary>What crosses the wire each way, and every refusal.</summary>"""
    print("running test_forward")
    ADDONS[:] = [("service.fake", "Fake"), ("service.future", "Future"), ("service.down", "Down")]
    PROPS.clear()
    announce("service.fake", api=1, port=port, token=TOKEN)
    announce("service.future", api=2, port=port, token=TOKEN)
    announce("service.down", api=1, port=free_port(), token=TOKEN)
    SEEN.clear()
    browser = {"Content-Type": "application/json", "If-Match": '"e1"',
               "Cookie": "jellystat_auth=secret", "X-Auth-Token": "dashpass",
               "X-Forwarded-For": "1.2.3.4", "Host": "kodi.local:8099"}
    status, body, headers = addon_api.forward("service.fake/slots/1", "q=a b&x=1", "PUT",
                                              b'{"a":1}', browser)
    equal(status, 200, "status passed back")
    sent = SEEN[-1]
    equal((sent["method"], sent["path"]), ("PUT", "/slots/1?q=a+b&x=1"), "method and normalised query")
    equal(sent["body"], b'{"a":1}', "body passed on")
    sent_names = {k.lower() for k in sent["headers"]}
    check("cookie" not in sent_names, "browser cookie not passed on")
    check("x-auth-token" not in sent_names, "dashboard token not passed on")
    check("x-forwarded-for" not in sent_names, "other browser headers not passed on")
    equal(sent["headers"].get("If-Match"), '"e1"', "If-Match passed on")
    equal(sent["headers"].get("X-Addon-Api-Token"), TOKEN, "add-on token added")
    equal(sent["headers"].get("Host"), "127.0.0.1:%d" % port, "Host is the add-on's own")
    names = [name for name, _ in headers]
    equal(sorted(names), ["Content-Disposition", "Content-Type", "ETag"], "only three headers back")

    status, _, _ = addon_api.forward("service.fake/info", "", "GET", None, {})
    equal(status, 200, "GET with no body or query")
    equal(SEEN[-1]["path"], "/info", "no stray question mark")

    refused(lambda: addon_api.forward("service.missing/slots", "", "GET", None, {}),
            404, "not_found", "add-on with no API")
    refused(lambda: addon_api.forward("service.future/slots", "", "GET", None, {}),
            409, "unsupported", "add-on speaking another API version")
    refused(lambda: addon_api.forward("service.down/slots", "", "GET", None, {}),
            503, "unavailable", "add-on not answering")
    refused(lambda: addon_api.forward("service.fake/slots", "", "POST",
                                      b"x" * (addon_api.MAX_BODY + 1), {}),
            413, "too_large", "oversized body")
    count = len(SEEN)
    refused(lambda: addon_api.forward("service.fake/../info", "", "GET", None, {}),
            404, "not_found", "traversal")
    equal(len(SEEN), count, "a refused path never reaches the add-on")


def main():
    """
    <summary>
    Run every test and return the exit code: 0 when all passed, 1 otherwise.
    </summary>
    """
    server = start_fake()
    port = server.server_address[1]
    try:
        test_discover(port)
        test_list(port)
        test_paths()
        test_forward(port)
    finally:
        server.shutdown()
        server.server_close()
    if FAILURES:
        print("test_addon_api: %d failure(s)" % len(FAILURES))
        return 1
    print("test_addon_api: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
