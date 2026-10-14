#!/usr/bin/env python3
"""
<summary>
Tests for the dashboard's HTTP server: the Host pin, the login and session,
the cross origin gate with its body drained, the add-on proxy as reached
through the handler, bad Content-Length headers, the failure logging, and the
service restarting the server when the password changes.
</summary>
<remarks>
The real webserver.Handler is served on a loopback port with the Kodi
modules stubbed by kodi_stubs, and every request is made with http.client so
the Host, Origin, Cookie and Content-Length headers are exactly what the test
says. A fake add-on on a second loopback port records what the proxy sends
it. Every check records a failure rather than stopping, and the process exits
non zero if any failed.

Usage:
    python3 tests/test_webserver.py
</remarks>
"""

import http.client
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kodi_stubs  # noqa: E402

kodi_stubs.install()

import service  # noqa: E402
import webdata  # noqa: E402
import webserver  # noqa: E402

FAILURES = []
PORT = None
TOKEN = "t" * 43
SEEN = []


def check(condition, message):
    """<summary>Record a failure without stopping.</summary>"""
    if not condition:
        FAILURES.append(message)
        print("FAIL: %s" % message)


def equal(actual, expected, what):
    """<summary>check() for equality, printing both sides on failure.</summary>"""
    check(actual == expected, "%s: expected %r, got %r" % (what, expected, actual))


# ---- the server under test and a fake add-on -------------------------------

def start_server():
    """<summary>Serve webserver.Handler on a free loopback port; returns the server.</summary>"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), webserver.Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class _FakeAddon(BaseHTTPRequestHandler):
    """
    <summary>
    Records each request, answers /info, echoes anything else, and sends a
    header the proxy must not pass back.
    </summary>
    """

    def log_message(self, fmt, *args):
        """<summary>Quiet.</summary>"""
        pass

    def _answer(self):
        """<summary>Record and answer one request.</summary>"""
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        SEEN.append({"method": self.command, "path": self.path,
                     "headers": dict(self.headers.items()), "body": body})
        if self.headers.get("X-Addon-Api-Token") != TOKEN:
            payload, status = {"error": {"code": "unauthorised", "message": "no"}}, 401
        elif self.path == "/info":
            payload, status = {"api": 1, "id": "service.fake", "name": "Fake",
                               "resources": []}, 200
        else:
            payload, status = {"echo": self.path}, 200
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("ETag", '"abc"')
        self.send_header("Set-Cookie", "stolen=1")
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_DELETE = _answer


def start_fake():
    """<summary>Start the fake add-on on a free loopback port; returns the server.</summary>"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeAddon)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ---- requests ---------------------------------------------------------------

def call(method, path, headers=None, body=None, host=None, conn=None):
    """
    <summary>
    One request against the server under test, with the headers given and no
    others besides Host.
    </summary>
    <param name="method">HTTP method.</param>
    <param name="path">Path and query.</param>
    <param name="headers">Extra headers, or None.</param>
    <param name="body">Bytes to send, or None.</param>
    <param name="host">The Host header; the server's own address when None,
    and no Host header at all when False.</param>
    <param name="conn">An open HTTPConnection to reuse, or None for a new one
    closed afterwards.</param>
    <returns>(status, response, body bytes).</returns>
    """
    headers = dict(headers or {})
    own = conn is None
    conn = conn or http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    if host is not False:
        conn.putheader("Host", host or "127.0.0.1:%d" % PORT)
    if body is not None and "Content-Length" not in headers:
        headers["Content-Length"] = str(len(body))
    for name, value in headers.items():
        conn.putheader(name, value)
    conn.endheaders(body)
    response = conn.getresponse()
    data = response.read()
    if own:
        conn.close()
    return response.status, response, data


def announce_fake(port):
    """<summary>Make the fake add-on discoverable through the stubbed Kodi.</summary>"""
    kodi_stubs.ADDONS[:] = [("service.fake", "Fake")]
    kodi_stubs.PROPS["AddonApi.service.fake"] = json.dumps(
        {"api": 1, "port": port, "token": TOKEN, "version": "1.0"})


# ---- tests ------------------------------------------------------------------

def test_host_pin():
    """<summary>Only the names this server owns get an answer.</summary>"""
    print("running test_host_pin")
    kodi_stubs.SETTINGS["web_password"] = ""
    status, _, data = call("GET", "/", host="evil.example:%d" % PORT)
    equal(status, 403, "a foreign name is refused")
    status, _, _ = call("GET", "/api/backups", host="evil.example:%d" % PORT)
    equal(status, 403, "the API refuses the foreign name before anything else")
    status, _, _ = call("GET", "/", host=False)
    equal(status, 403, "no Host at all is refused")
    for own in ("127.0.0.1:%d" % PORT, "localhost:%d" % PORT, "kodi.local:%d" % PORT,
                "[::1]:%d" % PORT):
        status, _, data = call("GET", "/", host=own)
        equal(status, 200, "Host %s is answered" % own)
        check(b"<title>" in data, "the page is served for %s" % own)


def test_login_and_session():
    """<summary>The login page, the session cookie, the script token, and a password change signing everyone out.</summary>"""
    print("running test_login_and_session")
    kodi_stubs.SETTINGS["web_password"] = ""
    status, _, _ = call("GET", "/api/backups")
    equal(status, 200, "no password means no gate")
    kodi_stubs.SETTINGS["web_password"] = "pw"
    status, _, data = call("GET", "/")
    equal(status, 200, "the login page answers 200")
    check(b"password protected" in data, "the login page is served instead of the dashboard")
    status, _, _ = call("GET", "/api/backups")
    equal(status, 401, "the API wants the password")
    status, response, data = call("POST", "/login", {"Content-Type": "application/x-www-form-urlencoded"},
                                  b"password=wrong")
    equal(status, 200, "a wrong password gets the login page again")
    check(b"not accepted" in data and response.getheader("Set-Cookie") is None,
          "a wrong password sets no cookie")
    status, response, _ = call("POST", "/login", {"Content-Type": "application/x-www-form-urlencoded"},
                               b"password=pw")
    equal(status, 303, "the right password redirects")
    cookie = (response.getheader("Set-Cookie") or "").split(";")[0]
    check(cookie.startswith(webserver.COOKIE + "="), "a session cookie is issued")
    status, _, _ = call("GET", "/api/backups", {"Cookie": cookie})
    equal(status, 200, "the cookie opens the API")
    status, _, data = call("GET", "/", {"Cookie": cookie})
    check(status == 200 and b"<title>" in data, "the cookie opens the dashboard")
    status, _, _ = call("GET", "/api/backups", {"X-Auth-Token": webserver._token_for("pw")})
    equal(status, 200, "the derived token opens the API for scripts")
    status, _, _ = call("GET", "/api/backups", {"X-Auth-Token": "nope"})
    equal(status, 401, "a wrong token is refused")
    status, _, _ = call("GET", "/api/backups", {"Cookie": webserver.COOKIE + "=forged"})
    equal(status, 401, "a cookie this server never issued is refused")
    kodi_stubs.SETTINGS["web_password"] = "pw2"
    status, _, _ = call("GET", "/api/backups", {"Cookie": cookie})
    equal(status, 401, "changing the password signs the browser out")
    status, _, _ = call("GET", "/api/backups", {"X-Auth-Token": webserver._token_for("pw")})
    equal(status, 401, "changing the password retires the old token")
    kodi_stubs.SETTINGS["web_password"] = ""


def test_cross_origin_drained():
    """<summary>A cross origin write is refused, its body is drained, and the connection stays usable.</summary>"""
    print("running test_cross_origin_drained")
    kodi_stubs.SETTINGS["web_password"] = ""
    evil = {"Origin": "http://evil.example:%d" % PORT, "Content-Type": "application/json"}
    body = json.dumps({"token": "x" * 2000}).encode("utf-8")
    for method, path in (("POST", "/api/import/discard"), ("PUT", "/api/addons/service.fake/slots/1"),
                         ("DELETE", "/api/addons/service.fake/slots/1")):
        conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
        status, _, data = call(method, path, evil, body, conn=conn)
        equal(status, 403, "%s from another origin is refused" % method)
        check(b"Cross-origin" in data, "%s refusal says why" % method)
        status, _, data = call("GET", "/api/backups", conn=conn)
        equal(status, 200, "the next request on the same connection after %s still parses" % method)
        conn.close()
    same = {"Origin": "http://127.0.0.1:%d" % PORT, "Content-Type": "application/json"}
    status, _, data = call("POST", "/api/import/discard", same, b'{"token": "x"}')
    equal(status, 200, "the page's own origin is accepted")
    kodi_stubs.SETTINGS["web_password"] = "pw"
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    status, _, _ = call("POST", "/api/import/discard", {"Content-Type": "application/json"},
                        body, conn=conn)
    equal(status, 401, "an unauthenticated write is refused")
    status, _, _ = call("GET", "/", conn=conn)
    equal(status, 200, "its body was drained too")
    conn.close()
    kodi_stubs.SETTINGS["web_password"] = ""


def test_proxy_through_handler(fake_port):
    """<summary>The add-on proxy as the browser reaches it: discovery, pass through, and the refusals.</summary>"""
    print("running test_proxy_through_handler")
    kodi_stubs.SETTINGS["web_password"] = ""
    announce_fake(fake_port)
    status, _, data = call("GET", "/api/addons")
    equal(status, 200, "add-ons listed")
    listed = json.loads(data.decode("utf-8"))
    equal([a["id"] for a in listed["addons"]], ["service.fake"], "the fake is found")
    check(TOKEN not in data.decode("utf-8") and str(fake_port) not in json.dumps(listed["addons"][0]),
          "neither the token nor the port reaches the browser")
    SEEN[:] = []
    status, response, data = call("GET", "/api/addons/service.fake/info", {"Cookie": "a=b"})
    equal(status, 200, "GET passed through")
    check(response.getheader("X-Addon-Api-Token") is None, "the token is not echoed back")
    check(response.getheader("Set-Cookie") is None, "the add-on's Set-Cookie is dropped")
    equal(response.getheader("ETag"), '"abc"', "ETag passed back")
    check("Cookie" not in SEEN[-1]["headers"], "the browser's cookie never reaches the add-on")
    equal(SEEN[-1]["headers"].get("X-Addon-Api-Token"), TOKEN, "the token travels on the proxied hop only")
    SEEN[:] = []
    status, _, data = call("PUT", "/api/addons/service.fake/slots/1?q=a",
                           {"Content-Type": "application/json", "If-Match": '"e1"'}, b'{"a": 1}')
    equal(status, 200, "PUT passed through")
    equal((SEEN[-1]["method"], SEEN[-1]["path"], SEEN[-1]["body"]),
          ("PUT", "/slots/1?q=a", b'{"a": 1}'), "method, path, query and body as sent")
    equal(SEEN[-1]["headers"].get("If-Match"), '"e1"', "If-Match passed through unchanged")
    SEEN[:] = []
    for bad in ("/api/addons/service.fake/%2e%2e/info", "/api/addons/service.fake/../info",
                "/api/addons/service.fake/Slots", "/api/addons/../etc/passwd",
                "/api/addons/service.fake/a//b"):
        status, _, data = call("GET", bad)
        equal(status, 404, "%s refused" % bad)
        equal(json.loads(data.decode("utf-8"))["error"]["code"], "not_found", "%s in the spec's shape" % bad)
    equal(SEEN, [], "a refused path never reaches the add-on")
    status, _, data = call("POST", "/api/addons/service.fake/slots",
                           {"Content-Type": "application/json"}, b"x" * (2 ** 20 + 1))
    equal(status, 413, "a body over 1 MB is refused")
    equal(json.loads(data.decode("utf-8"))["error"]["code"], "too_large", "413 in the spec's shape")
    status, _, data = call("GET", "/api/addons/service.none/info")
    equal(status, 404, "an add-on that announces nothing is not found")
    kodi_stubs.SETTINGS["web_password"] = "pw"
    SEEN[:] = []
    status, _, _ = call("DELETE", "/api/addons/service.fake/slots/1", {"If-Match": '"e1"'})
    equal(status, 401, "DELETE behind the password")
    equal(SEEN, [], "a refused DELETE never reaches the add-on")
    kodi_stubs.SETTINGS["web_password"] = ""


def test_bad_lengths(fake_port):
    """<summary>A Content-Length that is negative or not a number answers 400 at once rather than holding the thread.</summary>"""
    print("running test_bad_lengths")
    kodi_stubs.SETTINGS["web_password"] = ""
    announce_fake(fake_port)
    for path in ("/api/import/discard", "/api/addons/service.fake/slots", "/api/trakt/stage-zip",
                 "/api/import/stage"):
        for value in ("-5", "abc"):
            started = time.time()
            status, response, data = call("POST", path, {"Content-Length": value,
                                                         "Content-Type": "application/json"})
            equal(status, 400, "%s with Content-Length %s answers 400" % (path, value))
            check(time.time() - started < 5, "%s with Content-Length %s answered at once" % (path, value))
            check(b"length" in data, "the 400 names the length")
    status, _, data = call("POST", "/login", {"Content-Length": "abc"})
    equal(status, 400, "the login form with a bad length answers 400")


def test_failure_logging():
    """<summary>An unexpected failure building the stats answers 500 with the reason and logs a traceback.</summary>"""
    print("running test_failure_logging")
    kodi_stubs.SETTINGS["web_password"] = ""
    saved = webdata.get

    def boom(*args, **kwargs):
        """<summary>The sort of failure the real box produced.</summary>"""
        raise TypeError("'NoneType' object is not callable")

    webdata.get = boom
    kodi_stubs.LOGGED[:] = []
    try:
        status, _, data = call("GET", "/api/data")
    finally:
        webdata.get = saved
    equal(status, 500, "the page gets a status rather than a dropped connection")
    check(b"Could not build the stats: 'NoneType' object is not callable" in data,
          "the message names the error")
    check(kodi_stubs.logged("Traceback (most recent call last)"), "a traceback is logged")
    check(kodi_stubs.logged("in boom"), "the traceback names the failing function")


def test_password_change_restarts():
    """<summary>The service restarts the dashboard when the password changes, not only on port or binding changes.</summary>"""
    print("running test_password_change_restarts")
    calls = []
    saved = (webserver.start, webserver.stop)
    webserver.start = lambda: calls.append("start") or True
    webserver.stop = lambda: calls.append("stop")
    try:
        addon = kodi_stubs._Addon()
        kodi_stubs.SETTINGS.update({"web_enabled": "true", "web_port": "8099",
                                    "web_bind_all": "true", "web_password": ""})
        applied = service.sync_web(addon, None)
        equal(calls, ["stop", "start"], "first pass starts the server")
        calls[:] = []
        applied = service.sync_web(addon, applied)
        equal(calls, [], "unchanged settings leave it alone")
        kodi_stubs.SETTINGS["web_password"] = "pw"
        applied = service.sync_web(addon, applied)
        equal(calls, ["stop", "start"], "setting a password restarts it, so the network opens")
        calls[:] = []
        kodi_stubs.SETTINGS["web_password"] = ""
        applied = service.sync_web(addon, applied)
        equal(calls, ["stop", "start"], "clearing the password restarts it, so the network closes")
        calls[:] = []
        kodi_stubs.SETTINGS["web_port"] = "8100"
        service.sync_web(addon, applied)
        equal(calls, ["stop", "start"], "a port change still restarts it")
    finally:
        webserver.start, webserver.stop = saved


def main():
    """
    <summary>
    Run every test and return the exit code: 0 when all passed, 1 otherwise.
    </summary>
    """
    global PORT
    server = start_server()
    PORT = server.server_address[1]
    fake = start_fake()
    try:
        test_host_pin()
        test_login_and_session()
        test_cross_origin_drained()
        test_proxy_through_handler(fake.server_address[1])
        test_bad_lengths(fake.server_address[1])
        test_failure_logging()
        test_password_change_restarts()
    finally:
        server.shutdown()
        server.server_close()
        fake.shutdown()
        fake.server_close()
        kodi_stubs.cleanup()
    if FAILURES:
        print("test_webserver: %d failure(s)" % len(FAILURES))
        return 1
    print("test_webserver: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
