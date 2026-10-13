# -*- coding: utf-8 -*-
"""
<summary>
The dashboard's side of the shared add-on API: finding the other add-ons on this
Kodi that offer one, and passing the dashboard's requests through to them.
</summary>
<remarks>
The contract is ADDON-API-SPEC.md, kept beside the Kodi projects; this file is
section 9 of it. Each add-on (Engage first) runs a small server on 127.0.0.1 and
announces it in the Home window property AddonApi.<addon id> as JSON holding its
port and a token made fresh each start. The browser never sees either: it talks to
this dashboard at /api/addons/..., behind the dashboard's own Host pin, cross origin
check and password, and webserver.py hands the request here.

What makes this safe to put in front of the LAN is that nothing in the request can
choose where it goes. The host is always 127.0.0.1, the port always comes from the
announcement of an add-on Kodi says is installed and enabled, and the path is held
to lowercase letters, digits, underscores and slashes. Only Content-Type and
If-Match go through with the token; the browser's cookie and X-Auth-Token never do.
Only Content-Type, ETag and Content-Disposition come back.

Errors this file raises itself use the spec's shape, {"error": {"code", "message"}},
like the add-ons' own, so the Add-ons page reads one shape whoever answered.
</remarks>
"""

import http.client
import json
import re
from urllib.parse import parse_qs, urlencode

import xbmc
import xbmcgui

# The spec major version this dashboard speaks. An add-on announcing another is
# listed as needing a newer JellyStat and is offered nothing else.
API_VERSION = 1

HOME_WINDOW_ID = 10000
PROPERTY_PREFIX = "AddonApi."
TOKEN_HEADER = "X-Addon-Api-Token"

# Spec section 9: bodies over 1 MB are refused, and an add-on that does not answer
# inside ten seconds is reported as unavailable.
MAX_BODY = 2 ** 20
TIMEOUT_SECONDS = 10

# Add-on ids as Kodi writes them, dot separated with no empty part, so '..' can
# never pass for one; and the part of the path after the id.
_ADDON_ID = re.compile(r"^[a-z0-9][a-z0-9_\-]*(\.[a-z0-9_\-]+)*$")
_PATH = re.compile(r"^[a-z0-9_]+(/[a-z0-9_]+)*$")

# Headers passed through each way.
_REQUEST_HEADERS = ("Content-Type", "If-Match")
_RESPONSE_HEADERS = ("Content-Type", "ETag", "Content-Disposition")


class ProxyError(Exception):
    """
    <summary>
    A request this file refuses or cannot complete, in the spec's error shape.
    </summary>
    """

    def __init__(self, status, code, message):
        """
        <summary>
        Keep the status and the spec error fields.
        </summary>
        <param name="status">The HTTP status to answer with.</param>
        <param name="code">The spec's error code, such as 'unavailable'.</param>
        <param name="message">Plain UK English for the user.</param>
        """
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def payload(self):
        """
        <summary>
        The JSON body for this error.
        </summary>
        <returns>{"error": {"code": ..., "message": ...}}.</returns>
        """
        return {"error": {"code": self.code, "message": self.message}}


def log(message, level=xbmc.LOGINFO):
    """
    <summary>
    Write one line to Kodi's log under the JellyStat tag.
    </summary>
    <param name="message">Text to log. Never a token.</param>
    <param name="level">Kodi log level, info by default.</param>
    """
    xbmc.log("[JellyStat] add-ons: %s" % message, level)


# ---- discovery ------------------------------------------------------------

def _enabled_addons():
    """
    <summary>
    Every enabled add-on Kodi knows, as (id, name) pairs.
    </summary>
    <returns>A list, empty when Kodi will not say.</returns>
    <remarks>
    Window properties cannot be listed, so the candidates come from Kodi and each is
    then looked up by name. Asked in process, so no network is involved.
    </remarks>
    """
    try:
        reply = json.loads(xbmc.executeJSONRPC(json.dumps({
            "jsonrpc": "2.0", "method": "Addons.GetAddons", "id": 1,
            "params": {"enabled": True, "properties": ["name"]}})))
    except (ValueError, TypeError) as err:
        log("could not list add-ons: %s" % err, xbmc.LOGWARNING)
        return []
    addons = (reply.get("result") or {}).get("addons") or []
    return [(a.get("addonid") or "", a.get("name") or a.get("addonid") or "")
            for a in addons if a.get("addonid")]


def _announcement(addon_id):
    """
    <summary>
    An add-on's parsed AddonApi property, or None when it has none or it is unusable.
    </summary>
    <param name="addon_id">The add-on.</param>
    <returns>{"api": int, "port": int, "token": str, "version": str} or None.</returns>
    <remarks>
    A property that does not parse, or names a port outside 1 to 65535, is treated
    as no API at all rather than trusted in part.
    </remarks>
    """
    raw = xbmcgui.Window(HOME_WINDOW_ID).getProperty(PROPERTY_PREFIX + addon_id)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    api, port, token = value.get("api"), value.get("port"), value.get("token")
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (api, port)):
        return None
    if not 1 <= port <= 65535 or not isinstance(token, str) or not token:
        return None
    return {"api": api, "port": port, "token": token,
            "version": str(value.get("version") or "")}


def discover():
    """
    <summary>
    The add-ons on this Kodi that announce an API.
    </summary>
    <returns>A dict of add-on id to {"name", "api", "port", "token", "version"}.</returns>
    """
    found = {}
    for addon_id, name in _enabled_addons():
        if not _ADDON_ID.match(addon_id):
            continue
        announced = _announcement(addon_id)
        if announced:
            announced["name"] = name
            found[addon_id] = announced
    return found


def list_addons():
    """
    <summary>
    The body of GET /api/addons: every announcing add-on, each with its /info.
    </summary>
    <returns>{"addons": [...]}. Each entry has "id", "name" and "version", then
    either "info" (the add-on's own /info) or "problem" (plain English, why it cannot
    be managed from here).</returns>
    <remarks>
    One add-on not answering never hides the others: it is listed with its problem.
    </remarks>
    """
    addons = []
    for addon_id, found in sorted(discover().items(), key=lambda kv: kv[1]["name"].lower()):
        entry = {"id": addon_id, "name": found["name"], "version": found["version"]}
        if found["api"] != API_VERSION:
            entry["problem"] = ("%s speaks version %d of the add-on API and this "
                                "JellyStat speaks %d. Update JellyStat to manage it here."
                                % (found["name"], found["api"], API_VERSION))
        else:
            try:
                status, body, _ = _call(found, "GET", "/info", None, {})
                info = json.loads(body.decode("utf-8")) if status == 200 else None
                if not isinstance(info, dict):
                    raise ValueError("status %d" % status)
                entry["info"] = info
            except ProxyError as err:
                entry["problem"] = err.message
            except (ValueError, UnicodeDecodeError) as err:
                log("%s gave an unreadable /info: %s" % (addon_id, err), xbmc.LOGWARNING)
                entry["problem"] = "%s did not describe itself properly." % found["name"]
        addons.append(entry)
    return {"addons": addons}


# ---- forwarding -----------------------------------------------------------

def _call(found, method, path, body, headers):
    """
    <summary>
    One request to an add-on's loopback server.
    </summary>
    <param name="found">The add-on's entry from discover().</param>
    <param name="method">The HTTP method.</param>
    <param name="path">The path and query, starting with a slash.</param>
    <param name="body">The request body as bytes, or None.</param>
    <param name="headers">Headers to send besides the token.</param>
    <returns>(status, body bytes, [(name, value)] for the passed through headers).</returns>
    <exception cref="ProxyError">503 when the add-on refuses the connection or does
    not answer in time.</exception>
    """
    conn = http.client.HTTPConnection("127.0.0.1", found["port"], timeout=TIMEOUT_SECONDS)
    sent = dict(headers)
    sent[TOKEN_HEADER] = found["token"]
    try:
        conn.request(method, path, body=body, headers=sent)
        response = conn.getresponse()
        data = response.read()
        passed = [(name, response.getheader(name)) for name in _RESPONSE_HEADERS
                  if response.getheader(name)]
        return response.status, data, passed
    except (OSError, http.client.HTTPException) as err:
        log("%s did not answer on its port: %s" % (found["name"], err), xbmc.LOGWARNING)
        raise ProxyError(503, "unavailable",
                         "%s is not answering. It may be starting up or switched off "
                         "in its settings." % found["name"])
    finally:
        conn.close()


def check_path(rest):
    """
    <summary>
    Split and check the part of the URL after /api/addons/.
    </summary>
    <param name="rest">For example 'service.engage/slots/3'.</param>
    <returns>(add-on id, path below it with a leading slash).</returns>
    <exception cref="ProxyError">404 when either part breaks the rules: an id Kodi
    could not have written, or a path with anything but lowercase letters, digits
    and underscores between single slashes. That rules out dots, so '..' can never
    reach the add-on.</exception>
    """
    addon_id, _, path = rest.partition("/")
    if not _ADDON_ID.match(addon_id) or not _PATH.match(path):
        raise ProxyError(404, "not_found", "There is nothing at that address.")
    return addon_id, "/" + path


def forward(rest, query, method, body, headers):
    """
    <summary>
    Pass one dashboard request through to the add-on it names.
    </summary>
    <param name="rest">The URL path after /api/addons/.</param>
    <param name="query">The raw query string; '' for none. It is decoded and encoded
    again before it goes on, so whatever the browser sent arrives as a well formed
    query and cannot upset the HTTP client.</param>
    <param name="method">GET, POST, PUT or DELETE.</param>
    <param name="body">The request body as bytes, or None.</param>
    <param name="headers">The browser's request headers; only Content-Type and
    If-Match are taken from them.</param>
    <returns>(status, body bytes, [(name, value)] response headers).</returns>
    <exception cref="ProxyError">404 for a bad path or an add-on that does not
    announce an API, 413 for a body over MAX_BODY, 503 when the add-on does not
    answer, 409 when it speaks another API version.</exception>
    """
    addon_id, path = check_path(rest)
    found = discover().get(addon_id)
    if found is None:
        raise ProxyError(404, "not_found",
                         "That add-on is not installed here, or is not offering an API.")
    if found["api"] != API_VERSION:
        raise ProxyError(409, "unsupported",
                         "%s needs a newer JellyStat to be managed here." % found["name"])
    if body is not None and len(body) > MAX_BODY:
        raise ProxyError(413, "too_large", "That is larger than %d bytes." % MAX_BODY)
    sent = {}
    for name in _REQUEST_HEADERS:
        value = headers.get(name)
        if value:
            sent[name] = value
    query = urlencode(parse_qs(query), doseq=True)
    if query:
        path += "?" + query
    return _call(found, method, path, body, sent)

