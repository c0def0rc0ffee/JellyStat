# -*- coding: utf-8 -*-
"""Talk to Trakt directly, so an import needs no export file.

Trakt's sync endpoints return exactly the JSON its export files contain, so
everything fetched here drops straight into the same envelope the file
import already understands: parse, match, show, tick, import. Only the way
the data arrives is new.

**Signing in.** Through Trakt's device flow, which is built for exactly this
situation - a box with no keyboard and a browser somewhere else. The addon
asks Trakt for a short code, you enter it at trakt.tv/activate on any device,
and the addon polls until Trakt says you approved it. No password is typed
into the dashboard and the dashboard never sees one.

There is no redirect sign-in here, and that is not an oversight. Trakt
rejects any redirect URI that is not HTTPS or a loopback host, and a Kodi box
on a home network is neither: it is plain http on a LAN address, reached from
a browser on some other machine. An earlier version of this file offered one;
Trakt answered "Redirect URI must use HTTPS (HTTP allowed only for loopback
hosts)" and it was removed rather than left as a button that cannot work.

**Why you have to register an application.** Trakt issues credentials per
application, not per user, and it will not talk to an unregistered one.
Embedding a secret in an addon published on GitHub would put it in
everybody's hands, so JellyStat asks for your own instead: create an
application at trakt.tv/oauth/applications with the redirect URI
`urn:ietf:wg:oauth:2.0:oob` - the device flow never uses it, but the form
insists on a value - then paste its Client ID and Secret in. They stay on
this box: the dashboard can take them so they need not be typed on a remote
control, but it writes them to this addon's settings and nowhere else.

Tokens live in the addon's data folder rather than in the settings file,
because settings are meant to be readable and a refresh token is not.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import xbmc
import xbmcaddon

import history

ADDON_ID = "script.jellystat"
API = "https://api.trakt.tv"
# ---------------------------------------------------------------------------
# The JellyStat application itself.
#
# Trakt will not talk to an unregistered application, and it has no flow that
# lets one prove itself without a secret. So either every user registers
# their own - which is a form to fill in before the addon does anything at
# all - or the addon carries one, the way SIMKL, Plex and the official Kodi
# Trakt addon all do. Filled in, these turn the whole setup step off: the
# page shows one button and nothing else.
#
# This is a published secret and it is meant to be. It identifies the
# application, not the person: it grants nothing on its own, every user still
# approves the sign-in on Trakt under their own account, and the tokens it
# produces stay on their box. The realistic cost of it being public is that
# somebody could burn this application's rate limit, which is why a user can
# still put their own pair in the settings and take priority over it.
BUILTIN_CLIENT_ID = ""
BUILTIN_CLIENT_SECRET = ""

ACTIVATE_URL = "https://trakt.tv/activate"
OOB_REDIRECT = "urn:ietf:wg:oauth:2.0:oob"
TOKEN_FILE = "trakt-token.json"

# Trakt's published ceilings: 1000 GETs per five minutes, and pages of up to
# 250. A full history is ~42 pages, so a complete sync costs a rounding
# error of the allowance.
PAGE_SIZE = 250
MAX_PAGES = 400
POLL_CEILING_S = 900

_lock = threading.Lock()
# "flow" is only so the page can word the wait correctly - "enter this code"
# against "approve it in the tab that just opened".
_device = {"state": "idle", "code": None, "url": ACTIVATE_URL,
           "expires_at": 0, "message": None, "flow": None}
_poller = None


def log(message, level=xbmc.LOGINFO):
    xbmc.log("[JellyStat] %s" % message, level)


class TraktApiError(Exception):
    """Any failure talking to Trakt."""


# ---------------------------------------------------------------------------
# Credentials and tokens
# ---------------------------------------------------------------------------

def _addon():
    return xbmcaddon.Addon(ADDON_ID)


def client():
    """(client_id, client_secret) to sign in with.

    A pair entered in the settings wins, so anyone who would rather not
    share the built-in application's rate limit can use their own. Where
    none is set, the built-in one is used and there is nothing to set up.
    """
    addon = _addon()
    cid = (addon.getSetting("trakt_client_id") or "").strip()
    secret = (addon.getSetting("trakt_client_secret") or "").strip()
    if cid and secret:
        return cid, secret
    return BUILTIN_CLIENT_ID, BUILTIN_CLIENT_SECRET


def using_builtin():
    """True when the sign-in runs on the addon's own registration."""
    addon = _addon()
    return not ((addon.getSetting("trakt_client_id") or "").strip()
                and (addon.getSetting("trakt_client_secret") or "").strip())


def set_client(client_id, client_secret=None):
    """Store the Trakt application credentials.

    They live in the addon's settings either way; this exists so they can be
    pasted with a keyboard instead of spelled out on a remote control.

    An empty secret means "leave the stored one alone", because the page
    never sends the secret back to the browser and so cannot resubmit it.
    Passing a non-empty one replaces it.
    """
    addon = _addon()
    client_id = (client_id or "").strip()
    if not client_id:
        raise TraktApiError("A Client ID is needed.")
    addon.setSetting("trakt_client_id", client_id)
    secret = (client_secret or "").strip()
    if secret:
        addon.setSetting("trakt_client_secret", secret)
    elif not (addon.getSetting("trakt_client_secret") or "").strip():
        # Refused rather than half-stored: a Client ID with no Secret beside
        # it satisfies neither pair, and would leave the addon unable to
        # sign in at all where it had been about to work by itself.
        addon.setSetting("trakt_client_id", "")
        raise TraktApiError(
            "A Client Secret is needed too - it is on the same Trakt "
            "application page as the Client ID.")
    return status()


def token_path():
    return os.path.join(os.path.dirname(history.db_path()), TOKEN_FILE)


def load_token():
    try:
        with open(token_path(), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def save_token(token):
    try:
        with open(token_path(), "w", encoding="utf-8") as handle:
            json.dump(token, handle)
        # The refresh token is a long-lived credential; keep it off other
        # accounts on the box.
        os.chmod(token_path(), 0o600)
    except OSError as err:
        log("Could not store the Trakt token: %s" % err, xbmc.LOGWARNING)


def forget():
    try:
        os.unlink(token_path())
    except OSError:
        pass
    with _lock:
        _device.update(state="idle", code=None, message=None, expires_at=0,
                       flow=None)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _request(path, params=None, body=None, token=None, method=None):
    cid, _ = client()
    if not cid:
        raise TraktApiError(
            "No Trakt Client ID is set. Create an application at "
            "trakt.tv/oauth/applications and paste its Client ID and Secret "
            "into the addon's settings.")
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Content-Type": "application/json",
               "trakt-api-version": "2",
               "trakt-api-key": cid}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            payload = json.loads(raw) if raw.strip() else None
            return response.status, payload, dict(response.headers)
    except urllib.error.HTTPError as err:
        raw = err.read()
        try:
            payload = json.loads(raw) if raw.strip() else None
        except ValueError:
            payload = None
        return err.code, payload, dict(err.headers or {})
    except (urllib.error.URLError, OSError) as err:
        raise TraktApiError("Could not reach Trakt: %s" % err)


# ---------------------------------------------------------------------------
# Device sign-in
# ---------------------------------------------------------------------------

def status():
    """What the connection is doing, for the page to poll."""
    token = load_token()
    cid, secret = client()
    builtin = using_builtin()
    with _lock:
        state = dict(_device)
    return {
        "configured": bool(cid and secret),
        # True when nothing needs setting up, so the page can leave the
        # whole subject out rather than explaining a step nobody has to take.
        "builtin": builtin,
        # The Client ID is half of a public pair and the page prefills the
        # field with it - but only ever the user's own, never the built-in
        # one, whose field should read as empty and waiting. The secret only
        # goes the other way: the page is told one exists, never what it is.
        "client_id": "" if builtin else cid,
        "has_secret": bool(secret) and not builtin,
        "connected": bool(token and token.get("access_token")),
        "username": (token or {}).get("username"),
        "connected_at": (token or {}).get("saved_at"),
        "device": {k: state[k]
                   for k in ("state", "code", "url", "message", "flow")},
        "expires_in": max(0, int(state["expires_at"] - time.time()))
                      if state["expires_at"] else 0,
    }


def begin():
    """Ask Trakt for a device code and start polling for approval."""
    global _poller
    cid, secret = client()
    if not (cid and secret):
        raise TraktApiError(
            "Set a Trakt Client ID and Secret in the addon's settings first. "
            "Create an application at trakt.tv/oauth/applications with the "
            "redirect URI urn:ietf:wg:oauth:2.0:oob.")
    code, payload, _ = _request("/oauth/device/code", body={"client_id": cid},
                                method="POST")
    if code != 200 or not payload:
        raise TraktApiError(
            "Trakt would not issue a device code (HTTP %s). Check the Client "
            "ID." % code)
    with _lock:
        _device.update(state="waiting",
                       flow="device",
                       code=payload.get("user_code"),
                       url=payload.get("verification_url") or ACTIVATE_URL,
                       expires_at=time.time() + (payload.get("expires_in")
                                                 or 600),
                       message=None)
    interval = max(int(payload.get("interval") or 5), 5)
    device_code = payload.get("device_code")
    if _poller and _poller.is_alive():
        pass          # an old poller times itself out; it cannot win a race
    _poller = threading.Thread(target=_poll, args=(device_code, interval),
                               name="JellyStatTraktAuth")
    _poller.daemon = True
    _poller.start()
    return status()


def _poll(device_code, interval):
    """Ask Trakt whether the code has been approved yet, until it has."""
    cid, secret = client()
    deadline = time.time() + POLL_CEILING_S
    while time.time() < deadline:
        time.sleep(interval)
        with _lock:
            if _device["state"] != "waiting":
                return
        try:
            code, payload, _ = _request("/oauth/device/token", body={
                "code": device_code, "client_id": cid,
                "client_secret": secret}, method="POST")
        except TraktApiError as err:
            _fail(str(err))
            return
        if code == 200 and payload:
            payload["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            payload["username"] = _whoami(payload.get("access_token"))
            save_token(payload)
            with _lock:
                _device.update(state="connected", code=None, message=None)
            log("Connected to Trakt as %s" % payload.get("username"))
            return
        if code == 400:
            continue                      # not approved yet: keep waiting
        if code == 404:
            _fail("Trakt did not recognise that code. Start again.")
            return
        if code == 409:
            with _lock:
                _device.update(state="connected", message=None)
            return
        if code == 410:
            _fail("The code expired before it was approved. Start again.")
            return
        if code == 418:
            _fail("Sign-in was denied on Trakt.")
            return
        if code == 429:
            interval += 5                 # told to slow down
    _fail("Gave up waiting for approval.")


def _fail(message):
    with _lock:
        _device.update(state="failed", code=None, message=message)
    log("Trakt sign-in failed: %s" % message, xbmc.LOGWARNING)


def _whoami(token):
    try:
        code, payload, _ = _request("/users/me", token=token)
        if code == 200 and payload:
            return payload.get("username") or payload.get("name")
    except TraktApiError:
        pass
    return None


def _access_token():
    """A usable access token, refreshed if it has expired."""
    token = load_token()
    if not token or not token.get("access_token"):
        raise TraktApiError("Not connected to Trakt yet.")
    created = token.get("created_at") or 0
    expires = token.get("expires_in") or 0
    if created and expires and time.time() > (created + expires - 3600):
        cid, secret = client()
        code, payload, _ = _request("/oauth/token", body={
            "refresh_token": token.get("refresh_token"),
            "client_id": cid, "client_secret": secret,
            "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
            "grant_type": "refresh_token"}, method="POST")
        if code == 200 and payload:
            payload["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            payload["username"] = token.get("username")
            save_token(payload)
            return payload["access_token"]
        raise TraktApiError(
            "The Trakt sign-in expired and could not be renewed. Connect "
            "again.")
    return token["access_token"]


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def _paged(path, token, params=None, progress=None, label=""):
    """Every page of a paginated endpoint, stopping on an empty one.

    Trakt's own advice: read until a page comes back empty rather than
    trusting a total, because the total can move while you are reading it.
    """
    out = []
    for page in range(1, MAX_PAGES + 1):
        query = dict(params or {})
        query.update({"page": page, "limit": PAGE_SIZE})
        code, payload, headers = _request(path, params=query, token=token)
        if code == 429:
            time.sleep(int(headers.get("Retry-After") or 2))
            continue
        if code != 200:
            raise TraktApiError("Trakt returned HTTP %s for %s"
                                % (code, path))
        if not payload:
            break
        out.extend(payload)
        if progress:
            progress("%s: %d so far" % (label or path, len(out)))
        if len(payload) < PAGE_SIZE:
            break
    return out


def fetch_all(progress=None):
    """Everything this import understands, in the export's own envelope."""
    token = _access_token()
    envelope = {}
    history_rows = _paged("/sync/history", token, progress=progress,
                          label="Watch history")
    if history_rows:
        envelope["history"] = history_rows
    for kind, path in (("movie", "/sync/ratings/movies"),
                       ("episode", "/sync/ratings/episodes"),
                       ("show", "/sync/ratings/shows")):
        rows = _paged(path, token, progress=progress,
                      label="%s ratings" % kind.title())
        if rows:
            envelope["ratings-" + kind] = rows
    return envelope
