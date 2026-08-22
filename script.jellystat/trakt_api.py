# -*- coding: utf-8 -*-
"""Talk to Trakt directly, so an import needs no export file.

Trakt's sync endpoints return exactly the JSON its export files contain, so
everything fetched here drops straight into the same envelope the file
import already understands: parse, match, show, tick, import. Only the way
the data arrives is new.

**Signing in.** Two routes, because the dashboard is read on a real computer
but the addon runs on a television.

- **Redirect** is the ordinary web sign-in and the one worth using: the
  dashboard sends the browser to Trakt, you approve there, and Trakt sends
  the browser back to the dashboard's own address carrying a code it
  swaps for a token. Nothing is typed. It costs one thing - the exact
  address the browser uses to reach the dashboard has to be registered on
  the Trakt application as a redirect URI, because Trakt refuses to send a
  code anywhere it was not told about in advance.

- **Device** is the fallback for when that address cannot be pinned down
  (it moves with DHCP, or is reached through a tunnel): Trakt issues a
  short code, you type it into trakt.tv/activate on any device, and the
  addon polls until Trakt says you approved it.

Either way no password is typed into the dashboard, and the dashboard never
sees one.

**Why you have to register an application.** Trakt issues credentials per
application, not per user, and it will not talk to an unregistered one.
Embedding a secret in an addon published on GitHub would put it in
everybody's hands, so JellyStat asks for your own instead: create an
application at trakt.tv/oauth/applications, give it the redirect URI the
dashboard shows you (and `urn:ietf:wg:oauth:2.0:oob` as well if you want the
device fallback), then paste its Client ID and Secret in. They stay on this
box - the dashboard can take them so they need not be typed on a remote
control, but it writes them to this addon's settings and nowhere else.

Tokens live in the addon's data folder rather than in the settings file,
because settings are meant to be readable and a refresh token is not.
"""

import hmac
import json
import os
import secrets
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
ACTIVATE_URL = "https://trakt.tv/activate"
AUTHORIZE_URL = "https://trakt.tv/oauth/authorize"
# Where Trakt sends the browser back to. The host half is whatever address
# the dashboard was opened on, so this is only the tail of it.
CALLBACK_PATH = "/api/trakt/callback"
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
# The half-finished redirect sign-in: the anti-forgery value Trakt must hand
# back, and the redirect URI the token exchange has to repeat verbatim.
_pending = {"state": None, "redirect_uri": None}


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
    """(client_id, client_secret) from the addon settings."""
    addon = _addon()
    cid = (addon.getSetting("trakt_client_id") or "").strip()
    secret = (addon.getSetting("trakt_client_secret") or "").strip()
    return cid, secret


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
        _pending.update(state=None, redirect_uri=None)


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
    with _lock:
        state = dict(_device)
    return {
        "configured": bool(cid and secret),
        # The Client ID is half of a public pair and the page prefills the
        # field with it. The secret only ever goes the other way: the page
        # is told one exists, never what it is.
        "client_id": cid,
        "has_secret": bool(secret),
        "connected": bool(token and token.get("access_token")),
        "username": (token or {}).get("username"),
        "connected_at": (token or {}).get("saved_at"),
        "device": {k: state[k]
                   for k in ("state", "code", "url", "message", "flow")},
        "expires_in": max(0, int(state["expires_at"] - time.time()))
                      if state["expires_at"] else 0,
    }


def authorize_url(redirect_uri):
    """Start a redirect sign-in and return the Trakt page to send them to.

    `redirect_uri` is the dashboard's own address as this browser sees it.
    It is remembered because the token exchange must repeat it character for
    character - Trakt compares the two and refuses a mismatch.
    """
    cid, secret = client()
    if not (cid and secret):
        raise TraktApiError(
            "Set a Trakt Client ID and Secret first - there are fields for "
            "them just above.")
    # A value only this box knows, handed to Trakt and required back. It is
    # what stops another page on the network from walking someone through a
    # sign-in to an account that is not theirs.
    state = secrets.token_urlsafe(24)
    with _lock:
        _pending.update(state=state, redirect_uri=redirect_uri)
        _device.update(state="waiting", code=None, url=redirect_uri,
                       flow="redirect", message=None,
                       expires_at=time.time() + POLL_CEILING_S)
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": cid,
        "redirect_uri": redirect_uri,
        "state": state,
    })


def complete(code, state):
    """Finish a redirect sign-in with what Trakt sent back to the callback."""
    with _lock:
        expected = _pending.get("state")
        redirect_uri = _pending.get("redirect_uri")
        # Spent on first use, so a callback cannot be replayed.
        _pending.update(state=None)
    if not (expected and state) or not hmac.compare_digest(
            str(state), str(expected)):
        _fail("That sign-in did not start from this dashboard, so it was "
              "not completed. Try again from here.")
        raise TraktApiError("Unrecognised sign-in.")
    if not code:
        _fail("Trakt sent no authorisation code back.")
        raise TraktApiError("No code.")
    cid, secret = client()
    http, payload, _ = _request("/oauth/token", body={
        "code": code, "client_id": cid, "client_secret": secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code"}, method="POST")
    if http != 200 or not payload or not payload.get("access_token"):
        _fail("Trakt would not exchange the sign-in (HTTP %s). Check that "
              "%s is listed as a redirect URI on the Trakt application."
              % (http, redirect_uri))
        raise TraktApiError("Trakt refused the exchange (HTTP %s)." % http)
    payload["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    payload["username"] = _whoami(payload.get("access_token"))
    save_token(payload)
    with _lock:
        _device.update(state="connected", code=None, message=None,
                       flow="redirect")
    log("Connected to Trakt as %s" % payload.get("username"))
    return status()


def denied(reason=None):
    """Trakt sent the browser back with a refusal rather than a code."""
    with _lock:
        _pending.update(state=None)
    _fail("Sign-in was not granted on Trakt%s."
          % (": %s" % reason if reason else ""))


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
