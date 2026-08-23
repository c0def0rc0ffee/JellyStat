# -*- coding: utf-8 -*-
"""The dashboard's HTTP server, run in a thread by the background service.

Small on purpose: it serves one page, two JSON endpoints and an optional
password gate, all from the standard library, because a Kodi addon cannot
assume anything else is installed on the box.

It is meant for a home network. Binding to every interface puts the page on
http://<kodi-box>:8099/ for any device on the LAN, and that needs a password
set: without one the export endpoint hands the whole database to anything
that asks, so it serves this machine only instead and says why. The password
is there for shared households, not as protection against the open internet,
and the addon never asks you to port-forward it.
"""

import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import xbmc
import xbmcaddon
import xbmcvfs

import backup
import history
import importer
import library
import media as artwork
import main as core
import playback
import ratings
import reconcile
import screentime
import trakt_import
import recommend
import webdata

ADDON_ID = "script.jellystat"
PAGE_RELATIVE = "resources/web/dashboard.html"
COOKIE = "jellystat_auth"

DEFAULT_PORT = 8099
DEFAULT_CACHE_MINUTES = 10

_server = None
_thread = None


def log(message, level=xbmc.LOGINFO):
    xbmc.log("[JellyStat] %s" % message, level)


def _addon():
    # Explicit id: the service and the script resolve context differently.
    return xbmcaddon.Addon(ADDON_ID)


def _number(query, name, cast=int):
    """One numeric query parameter, or None when absent or nonsense.

    None rather than a default: these are filters, and "no lower bound" has
    to stay distinguishable from "a lower bound of zero".
    """
    raw = (query.get(name) or [""])[0]
    if not str(raw).strip():
        return None
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return None


def _setting_int(addon, name, default):
    try:
        return int(addon.getSetting(name))
    except (ValueError, TypeError):
        return default


# The X-Auth-Token a script sends is derived from the password, because a
# script has no way to fill in a login form. That value travels over plain
# HTTP on the home network, so it has to survive being seen: a bare SHA-256
# of a short password falls to a wordlist in seconds, which made one
# captured request enough to recover the password itself. The salt is fixed
# rather than per-install because both sides must arrive at the same value
# with nothing shared between them but the password - it is the work factor
# that costs an attacker anything, and that is what this buys.
API_TOKEN_SALT = b"jellystat-web-auth-v1"
API_TOKEN_ROUNDS = 200000

# Deriving it takes a noticeable fraction of a second on the sort of box
# this runs on, and a polling script sends it on every request, so the
# answer is worked out once per password and kept.
_token_cache = {}

# How long a signed-in browser stays signed in. Long enough not to nag
# somebody part-way through an evening, short enough that a cookie lifted
# off the wire stops working. Sessions live in memory only, so restarting
# Kodi signs every browser out - which is the right way round for a box
# that is often left on.
SESSION_HOURS = 12

_sessions = {}          # token -> (expiry epoch, the password it was issued for)
_sessions_lock = threading.Lock()


def _token_for(password):
    """The X-Auth-Token value a correct password produces, for scripts."""
    cached = _token_cache.get(password)
    if cached is None:
        cached = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                     API_TOKEN_SALT, API_TOKEN_ROUNDS).hex()
        _token_cache[password] = cached
    return cached


def _new_session(password):
    """Issue a fresh session token and remember it until it expires."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _sessions_lock:
        for stale in [t for t, (expiry, _) in _sessions.items()
                      if expiry <= now]:
            del _sessions[stale]
        _sessions[token] = (now + SESSION_HOURS * 3600, _token_for(password))
    return token


def _session_valid(token, password):
    """True for a token this process issued, still live, still for this
    password.

    Tying it to the password means changing the password signs out every
    browser, which is what somebody changing it is usually trying to do.
    A plain lookup rather than a constant-time compare: the token is 32
    random bytes, so there is no secret here to guess a character at a time.
    """
    if not token:
        return False
    with _sessions_lock:
        held = _sessions.get(token)
        if held is None:
            return False
        expiry, issued_for = held
        if expiry <= time.time() or issued_for != _token_for(password):
            del _sessions[token]
            return False
        return True


def _read_page():
    """The dashboard HTML, from wherever this addon is actually installed.

    Asked of the addon rather than assumed to be under special://home:
    a system-wide or portable install puts it somewhere else entirely, and
    hard-coding the usual path served those users a 404 for the one file
    the whole dashboard is.
    """
    base = _addon().getAddonInfo("path")
    path = xbmcvfs.translatePath(os.path.join(base, PAGE_RELATIVE))
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _own_names():
    """The names this machine legitimately answers to."""
    names = {"localhost"}
    try:
        host = socket.gethostname().strip().lower()
    except OSError:
        return names
    if host:
        short = host.partition(".")[0]
        # gethostname() returns the short or the qualified form depending
        # on the box; accept both, plus the mDNS spelling, so every address
        # local_urls() prints keeps working.
        names.update((host, short, short + ".local"))
    return names


def _host_name(header):
    """The name part of a Host header, lowercased and without its port."""
    value = (header or "").strip().lower()
    if value.startswith("["):        # [::1]:8099 - bracketed IPv6 literal
        return value.partition("]")[0][1:]
    if value.count(":") == 1:        # name:port; a bare IPv6 has more
        return value.rpartition(":")[0]
    return value


def _host_allowed(header):
    """True when Host names this server rather than someone else's domain.

    The origin check further down can only compare Origin against Host, so
    it is defeated by anyone who controls both. That is DNS rebinding: a
    page on some domain whose name has been re-pointed at this box sends an
    Origin and a Host that agree, the check calls it same-origin, and the
    browser - which also thinks it is same-origin - lets the page read the
    reply. Pinning Host to addresses this server can actually be reached at
    is what closes it, and it runs on every request rather than only the
    ones that change something: the database export is a GET, and it is the
    thing worth stealing.

    An IP literal is always accepted. A page can only be same-origin with
    one if it was served from that address, which means it was served by
    this dashboard. Names are the whole risk, so only the ones the addon
    itself hands out are allowed: localhost, this machine's own hostname,
    and mDNS ".local" names, which the local network answers rather than
    public DNS.
    """
    name = _host_name(header)
    if not name:
        # HTTP/1.1 requires a Host; a request without one is not a browser
        # following the addon's own address.
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    return name in _own_names() or name.endswith(".local")


class Handler(BaseHTTPRequestHandler):
    server_version = "JellyStat"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    # A client that announces a Content-Length and stalls must not hold a
    # worker thread forever; the default timeout is None.
    timeout = 30

    # ---- plumbing ---------------------------------------------------------

    def log_message(self, fmt, *args):
        # Kodi's log is shared with everything else on the box; a page load
        # is debug-level noise there, not information.
        xbmc.log("[JellyStat] web: " + (fmt % args), xbmc.LOGDEBUG)

    def _send(self, code, body, content_type, extra_headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The page is generated per request and the data changes constantly;
        # a cached copy would show yesterday's numbers after a refresh.
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra_headers or []):
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # browser navigated away mid-response

    def _send_json(self, code, payload, extra_headers=None):
        self._send(code, json.dumps(payload), "application/json; charset=utf-8",
                   extra_headers)

    # ---- auth -------------------------------------------------------------

    def _password(self):
        return (_addon().getSetting("web_password") or "").strip()

    def _authorised(self, password):
        """True when this request may see data.

        Two ways in, deliberately different in kind. A browser signs in
        once and carries a random session token this process issued, which
        expires and can be revoked by changing the password. A script sends
        `X-Auth-Token`, derived from the password, because it cannot fill
        in a login form - that one is a standing credential and is treated
        as such.

        Both used to be the same value: one unsalted hash of the password,
        identical on every device and for every login, with no expiry at
        all. Seeing a single request was enough to hold the dashboard for
        good.
        """
        if not password:
            return True
        for chunk in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = chunk.strip().partition("=")
            if name == COOKIE and _session_valid(value, password):
                return True
        # Compared as bytes: compare_digest raises TypeError on non-ASCII
        # str, and the header arrives attacker-chosen.
        header = (self.headers.get("X-Auth-Token") or "").strip()
        if header:
            return hmac.compare_digest(header.encode("utf-8"),
                                       _token_for(password).encode("utf-8"))
        return False

    # ---- routes -----------------------------------------------------------

    def _reject_host(self):
        """Refuse a request addressed to a name this server does not own."""
        self._send(403,
                   "This dashboard only answers to its own address. Open it "
                   "at this machine's IP or name - the addon's settings "
                   "screen prints the ones that work.\n",
                   "text/plain; charset=utf-8",
                   [("Connection", "close")])

    def do_GET(self):
        # Before routing, and before the password check: a request naming
        # somebody else's domain is not this dashboard's traffic whether or
        # not it carries a valid token.
        if not _host_allowed(self.headers.get("Host")):
            self._reject_host()
            return
        route = urlparse(self.path)
        password = self._password()
        if route.path in ("/", "/index.html"):
            if password and not self._authorised(password):
                self._send(200, LOGIN_PAGE, "text/html; charset=utf-8")
                return
            try:
                self._send(200, _read_page(), "text/html; charset=utf-8")
            except Exception as err:
                # Any failure, not just a missing file: resolving the
                # addon's own path can fail in its own ways, and an
                # unhandled one here drops the connection so the browser
                # shows nothing at all rather than the reason.
                self._send(500, "Could not read the dashboard page: %s" % err,
                           "text/plain; charset=utf-8")
            return

        if route.path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return

        if not route.path.startswith("/api/"):
            self._send(404, "Not found", "text/plain; charset=utf-8")
            return

        if password and not self._authorised(password):
            self._send_json(401, {"error": "Password required."})
            return

        try:
            self._dispatch_get(route)
        except Exception as err:
            log("GET %s failed: %s" % (route.path, err),
                xbmc.LOGERROR)
            self._send_json(500, {"error": str(err)})

    def _dispatch_get(self, route):
        """Route one authorised GET.

        Split out so a single guard covers every endpoint. Half of these
        called straight into a data module with nothing around them, so an
        unexpected failure escaped the handler, reset the socket, and left
        the page showing a connection error rather than the reason.
        """
        if route.path == "/api/data":
            self._serve_data(parse_qs(route.query))
        elif route.path == "/api/history":
            self._serve_history(parse_qs(route.query))
        elif route.path == "/api/import/batches":
            self._send_json(200, {"batches": importer.batches()})
        elif route.path == "/api/search":
            term = (parse_qs(route.query).get("q") or [""])[0]
            self._send_json(200, recommend.search(term))
        elif route.path == "/api/movie":
            item_id = (parse_qs(route.query).get("id") or [""])[0]
            self._serve_detail("movie", item_id=item_id)
        elif route.path == "/api/show":
            name = (parse_qs(route.query).get("name") or [""])[0]
            self._serve_detail("tv", name=name)
        elif route.path == "/api/sittings":
            query = parse_qs(route.query)
            title = (query.get("title") or [None])[0]
            show = (query.get("show") or [None])[0]
            self._send_json(200, {"sittings": library.sittings_for(
                title=title, show=show)})
        elif route.path == "/api/library":
            self._serve_library(parse_qs(route.query))
        elif route.path == "/api/stats":
            media = (parse_qs(route.query).get("media") or ["movies"])[0]
            self._send_json(200, library.movie_stats()
                            if media == "movies" else library.tv_stats())
        elif route.path in ("/api/recommend", "/api/similar"):
            self._serve_suggestions(route.path, parse_qs(route.query))
        elif route.path == "/api/unrated":
            query = parse_qs(route.query)
            media = (query.get("media") or ["movies"])[0]
            scope = (query.get("scope") or [ratings.DEFAULT_SCOPE])[0]
            payload = ratings.unrated(
                media if media in ("movies", "tv") else "movies", scope)
            payload["summary"] = ratings.summary()
            payload["capabilities"] = ratings.capabilities()
            self._send_json(200, payload)
        elif route.path == "/api/playback":
            try:
                self._send_json(200, playback.status())
            except playback.PlaybackError as err:
                self._send_json(503, {"error": str(err)})
        elif route.path == "/api/image":
            self._serve_image(parse_qs(route.query))
        elif route.path == "/api/person":
            self._serve_simple(
                lambda q: artwork.person((q.get("id") or [""])[0]),
                parse_qs(route.query))
        elif route.path == "/api/episodes":
            self._serve_simple(
                lambda q: artwork.episodes(
                    (q.get("series") or [""])[0],
                    (q.get("season") or [None])[0]),
                parse_qs(route.query))
        elif route.path == "/api/trakt/unmatched":
            self._serve_trakt_unmatched(parse_qs(route.query))
        elif route.path == "/api/reconcile":
            try:
                self._send_json(200, reconcile.survey())
            except Exception as err:
                self._send_json(500, {"error": str(err)})
        elif route.path == "/api/backups":
            self._send_json(200, {"backups": backup.listing(),
                                  "keep": backup.KEEP})
        elif route.path == "/api/backup/download":
            self._serve_backup(parse_qs(route.query))
        elif route.path == "/api/tiles":
            self._send_json(200, screentime.tiles())
        elif route.path == "/api/screentime":
            try:
                days = int((parse_qs(route.query).get("days") or ["7"])[0])
            except ValueError:
                days = 7
            self._send_json(200, screentime.screen_time(max(2, min(days, 90))))
        elif route.path == "/api/export":
            self._serve_export()
        else:
            self._send_json(404, {"error": "Unknown endpoint."})

    def _cross_origin(self):
        """True when this POST came from some other site's page.

        With no password set there is no authentication on the POST
        endpoints, and a cross-origin text/plain POST needs no preflight -
        so any web page opened on the LAN could otherwise delete import
        batches. Browsers always send Origin on cross-origin POSTs; the
        dashboard's own requests either omit it or carry this host.

        This compares Origin against Host and so is only as trustworthy as
        Host itself, which is why _host_allowed() pins that first. The two
        run together: the pin establishes that Host names this server, and
        this then establishes that the page asking shares that name.
        """
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return False
        # An "Origin: null" (sandboxed frame, file://) parses to an empty
        # netloc and is rejected below, which is the right answer for it.
        host = (self.headers.get("Host") or "").strip()
        return urlparse(origin).netloc != host

    def _drain(self):
        """Read and discard an announced request body.

        A refused POST still has its body sitting in the socket, and this
        is a keep-alive server: leaving it there means the next request on
        that connection starts part-way through the last one's payload and
        is parsed as nonsense.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True
            return
        if length > 0:
            try:
                self.rfile.read(min(length, importer.MAX_BYTES))
            except Exception:
                self.close_connection = True

    def do_POST(self):
        if not _host_allowed(self.headers.get("Host")):
            self._reject_host()
            return
        if self._cross_origin():
            self._drain()
            self._send_json(403, {"error": "Cross-origin requests are not "
                                           "accepted."})
            return
        path = urlparse(self.path).path
        if path == "/login":
            self._login()
            return
        # Every other POST changes something - a rating, an import, now a
        # set of Trakt credentials - so the gate goes here, above the
        # routes, rather than part-way down the list.
        password = self._password()
        if password and not self._authorised(password):
            self._drain()
            self._send_json(401, {"error": "Password required."})
            return
        if path == "/api/rate":
            self._serve_rate()
            return
        if path == "/api/play":
            self._serve_play()
            return
        if path == "/api/trakt/stage":
            self._serve_trakt_stage()
            return
        if path == "/api/trakt/commit":
            self._serve_trakt_commit()
            return
        if path == "/api/reconcile/start":
            body = self._json_body()
            try:
                self._send_json(200, reconcile.start(
                    body.get("action") or "push",
                    body.get("scope") or "all"))
            except ValueError as err:
                self._send_json(400, {"error": str(err)})
            except Exception as err:
                log("Reconcile failed to start: %s" % err, xbmc.LOGERROR)
                self._send_json(500, {"error": str(err)})
            return
        if path == "/api/backup/create":
            self._send_json(200, {"backup": backup.create("manual",
                                                          "requested")})
            return
        if path == "/api/import/stage":
            self._import_stage()
        elif path == "/api/import/commit":
            self._import_commit()
        elif path == "/api/import/discard":
            self._import_discard()
        elif path == "/api/import/delete":
            self._import_delete()
        else:
            self._send(404, "Not found", "text/plain; charset=utf-8")

    def _read_body(self, limit=importer.MAX_BYTES):
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            # Drain what was announced before answering. Refusing without
            # reading leaves the client writing into a closed socket, and
            # it sees a broken pipe rather than the explanation.
            self._drain(length)
            raise importer.ImportError_(
                "The upload is over %d MB." % (limit // 2**20))
        return self.rfile.read(length)

    def _drain(self, length):
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)

    def _json_body(self, limit=2**20):
        """Parse a JSON request body.

        The default cap suits the small command bodies most endpoints take.
        An import envelope is a file upload wearing a JSON hat and passes
        its own, much larger, limit.
        """
        try:
            return json.loads(self._read_body(limit).decode("utf-8"))
        except importer.ImportError_:
            raise
        except (ValueError, UnicodeDecodeError):
            return {}

    # ---- import ----------------------------------------------------------

    def _import_stage(self):
        """The file arrives as the raw request body (fetch(file) from the
        page), with its name in a header - no multipart parsing needed."""
        filename = (self.headers.get("X-Filename") or "upload").strip()
        try:
            result = importer.stage(filename, self._read_body())
        except importer.ImportError_ as err:
            self._send_json(400, {"error": str(err)})
            return
        except Exception as err:
            log("Import staging failed: %s" % err, xbmc.LOGERROR)
            self._send_json(500, {"error": "Could not read the file: %s"
                                           % err})
            return
        self._send_json(200, result)

    def _import_commit(self):
        body = self._json_body()
        try:
            result = importer.commit(
                body.get("token") or "",
                mode=body.get("mode") or "merge",
                replace_sources=body.get("replace_sources"))
        except importer.ImportError_ as err:
            self._send_json(400, {"error": str(err)})
            return
        except Exception as err:
            log("Import commit failed: %s" % err, xbmc.LOGERROR)
            self._send_json(500, {"error": "The import failed part-way: %s"
                                           % err})
            return
        webdata.invalidate()
        self._send_json(200, result)

    def _import_discard(self):
        importer.discard(self._json_body().get("token") or "")
        self._send_json(200, {"ok": True})

    def _import_delete(self):
        batch_id = self._json_body().get("batch_id")
        if not isinstance(batch_id, int) or isinstance(batch_id, bool):
            self._send_json(400, {"error": "batch_id must be a number."})
            return
        removed = importer.delete_batch(batch_id)
        webdata.invalidate()
        self._send_json(200, {"ok": True, "removed": removed})

    # ---- login -----------------------------------------------------------

    def _login(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            # A malformed length used to raise here and drop the
            # connection instead of answering.
            self._send(400, "Bad request.", "text/plain; charset=utf-8",
                       [("Connection", "close")])
            return
        body = self.rfile.read(max(0, length)).decode("utf-8", "replace")
        supplied = (parse_qs(body).get("password") or [""])[0]
        password = self._password()
        if not password or not hmac.compare_digest(
                supplied.encode("utf-8"), password.encode("utf-8")):
            self._send(200, LOGIN_PAGE.replace(
                "<!--ERROR-->",
                '<p class="err">That password was not accepted.</p>'),
                "text/html; charset=utf-8")
            return
        # A fresh random token per sign-in, good for SESSION_HOURS and no
        # longer. Still a session cookie as far as the browser is concerned
        # - closing it forgets the cookie - but the server now decides when
        # it stops working rather than trusting the browser to.
        cookie = ("%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict"
                  % (COOKIE, _new_session(password), SESSION_HOURS * 3600))
        self._send(303, b"", "text/plain",
                   [("Location", "/"), ("Set-Cookie", cookie)])

    # ---- data -------------------------------------------------------------

    def _serve_data(self, query):
        addon = _addon()
        recent_days = _setting_int(addon, "recent_days", 30)
        max_age = 60 * _setting_int(addon, "web_cache_minutes",
                                    DEFAULT_CACHE_MINUTES)
        force = query.get("refresh") == ["1"]
        try:
            data, rebuilt = webdata.get(recent_days, max_age, force)
        except core.JellyStatError as err:
            self._send_json(503, {"error": str(err)})
            return
        except Exception as err:  # a broken page beats a dead server
            log("Dashboard data failed: %s" % err, xbmc.LOGERROR)
            self._send_json(500, {"error": "Could not build the stats: %s"
                                           % err})
            return
        if rebuilt and addon.getSetting("history_enabled") != "false" \
                and not history.recorded_today():
            history.record(data["snapshot"])
        data["cache_age_seconds"] = webdata.cached_age()
        self._send_json(200, data)

    def _serve_detail(self, media, item_id=None, name=None):
        """One title's page: from the mirror, else from the catalog.

        A recommendation the user has never watched has no mirror row, but
        it is exactly the thing they clicked on - so fall back to Jellyfin's
        catalog and mark it unwatched rather than answering "no such film".
        """
        detail = (library.movie_detail(item_id) if media == "movie"
                  else library.show_detail(name))
        if detail is not None:
            detail["watched"] = True
            self._attach_artwork(detail, media)
            self._send_json(200, detail)
            return
        try:
            detail = recommend.catalog_detail(media, item_id=item_id,
                                              name=name)
        except core.JellyStatError as err:
            self._send_json(503, {"error": str(err)})
            return
        if detail is None:
            self._send_json(404, {"error": "That title is not on the server."})
            return
        self._attach_artwork(detail, media)
        self._send_json(200, detail)

    def _attach_artwork(self, detail, media):
        """Fold live Jellyfin detail into a page payload, never failing it.

        The mirror is the source of truth for what was watched; this adds
        only what it deliberately does not store - artwork, cast, overview
        and resolution. A server that is down costs the page its pictures,
        not its numbers.
        """
        item_id = detail.get("id") or detail.get("series_id")
        detail["media_detail"] = None
        if not item_id:
            return
        try:
            detail["media_detail"] = artwork.detail(item_id)
        except Exception as err:
            log("No live detail for %s: %s" % (item_id, err), xbmc.LOGDEBUG)
        self._attach_files(detail)

    def _attach_files(self, detail):
        """Where each copy of a duplicated film actually lives.

        Only for duplicates: the path, size and quality are what make one
        copy the keeper and the other the spare, and asking Jellyfin for
        them on every film to answer a question almost no film raises would
        be a request per page for nothing.
        """
        copies = detail.get("duplicates")
        if not copies:
            return
        for item in [detail] + list(copies):
            try:
                item["files"] = artwork.files(item.get("id"))
            except Exception as err:
                item["files"] = None
                log("No file detail for %s: %s" % (item.get("id"), err),
                    xbmc.LOGDEBUG)

    def _serve_rate(self):
        """Save one score and report honestly what reached Jellyfin."""
        password = self._password()
        if password and not self._authorised(password):
            self._send_json(401, {"error": "Password required."})
            return
        body = self._json_body()
        item_id = body.get("id")
        if not isinstance(item_id, str) or not item_id:
            self._send_json(400, {"error": "An item id is required."})
            return
        score = body.get("score")
        if score is not None and not isinstance(score, (int, float)):
            self._send_json(400, {"error": "Score must be a number or null."})
            return
        if isinstance(score, bool):
            self._send_json(400, {"error": "Score must be a number or null."})
            return
        try:
            result = ratings.rate(item_id, score,
                                  favourite=body.get("favourite"),
                                  push=body.get("push", True) is not False)
        except core.JellyStatError as err:
            self._send_json(404, {"error": str(err)})
            return
        except Exception as err:
            log("Rating failed: %s" % err, xbmc.LOGERROR)
            self._send_json(500, {"error": "Could not save the rating: %s"
                                           % err})
            return
        result["summary"] = ratings.summary()
        self._send_json(200, result)

    def _serve_play(self):
        """Play or queue on this Kodi box.

        Behind the password gate like everything else: this one actually
        changes what is on the television, so it must not be the endpoint
        that forgot to check.
        """
        password = self._password()
        if password and not self._authorised(password):
            self._send_json(401, {"error": "Password required."})
            return
        body = self._json_body()
        action = body.get("action")
        if action not in ("play", "queue"):
            self._send_json(400, {"error": "Action must be play or queue."})
            return
        item_id = body.get("id")
        show = body.get("show")
        try:
            if show:
                result = playback.play_show(show, action)
            elif isinstance(item_id, str) and item_id:
                result = (playback.play(item_id) if action == "play"
                          else playback.queue(item_id))
            else:
                self._send_json(400, {"error": "An item id or show name is "
                                               "required."})
                return
        except playback.PlaybackError as err:
            self._send_json(409, {"error": str(err)})
            return
        except Exception as err:
            log("Playback failed: %s" % err, xbmc.LOGERROR)
            self._send_json(500, {"error": "Could not control playback: %s"
                                           % err})
            return
        try:
            result["status"] = playback.status()
        except playback.PlaybackError:
            pass
        self._send_json(200, result)

    def _serve_library(self, query):
        """The library browser: everything owned, filtered and ordered."""
        media = (query.get("media") or ["movies"])[0]
        if media not in ("movies", "tv"):
            media = "movies"
        try:
            limit = min(max(int((query.get("limit") or ["48"])[0]), 1), 200)
        except ValueError:
            limit = 48
        try:
            offset = int((query.get("offset") or ["0"])[0])
        except ValueError:
            offset = 0
        descending = (query.get("desc") or [None])[0]
        try:
            payload = recommend.browse(
                media, limit, offset,
                genres=[g for g in (query.get("genre") or []) if g.strip()],
                exclude=[g for g in (query.get("not") or []) if g.strip()],
                sort=(query.get("sort") or ["name"])[0],
                descending=None if descending is None else descending == "1",
                year_from=_number(query, "from"),
                year_to=_number(query, "to"),
                rating_min=_number(query, "minrating", float),
                show=(query.get("show") or ["all"])[0])
        except core.JellyStatError as err:
            self._send_json(503, {"error": str(err)})
            return
        self._send_json(200, payload)

    def _serve_suggestions(self, path, query):
        """Recommendations and similar titles share their failure modes.

        Both need the Jellyfin catalog for anything unseen, so both answer
        503 with the connection error when it is unreachable and the caller
        asked for new titles only - the page turns that into "can't suggest
        new titles while the server is down" rather than an empty list,
        which would read as "nothing to watch".
        """
        media = (query.get("media") or ["movies"])[0]
        include_seen = (query.get("seen") or ["0"])[0] == "1"
        try:
            if path == "/api/recommend":
                if media not in ("movies", "tv"):
                    media = "movies"
                try:
                    offset = int((query.get("offset") or ["0"])[0])
                except ValueError:
                    offset = 0
                try:
                    limit = min(max(int((query.get("limit") or ["20"])[0]),
                                    1), 100)
                except ValueError:
                    limit = 20
                genres = [g for g in (query.get("genre") or []) if g.strip()]
                exclude = [g for g in (query.get("not") or []) if g.strip()]
                payload = recommend.recommendations(
                    media, include_seen, limit, offset, genres, exclude,
                    sort=(query.get("sort") or ["match"])[0],
                    year_from=_number(query, "from"),
                    year_to=_number(query, "to"),
                    rating_min=_number(query, "minrating", float))
            else:
                payload = recommend.similar(
                    "movie" if media == "movies" else "tv",
                    item_id=(query.get("id") or [None])[0],
                    name=(query.get("name") or [None])[0],
                    include_seen=include_seen)
        except core.JellyStatError as err:
            self._send_json(503, {"error": str(err)})
            return
        except Exception as err:
            log("Suggestions failed: %s" % err, xbmc.LOGERROR)
            self._send_json(500, {"error": "Could not build suggestions: %s"
                                           % err})
            return
        self._send_json(200, payload)

    def _serve_simple(self, builder, query):
        """Run one read-only lookup and answer with it, or with its error."""
        try:
            self._send_json(200, builder(query))
        except artwork.MediaError as err:
            self._send_json(404, {"error": str(err)})
        except core.JellyStatError as err:
            self._send_json(503, {"error": str(err)})
        except Exception as err:
            log("Lookup failed: %s" % err, xbmc.LOGERROR)
            self._send_json(500, {"error": str(err)})

    def _serve_image(self, query):
        """Proxy one Jellyfin image, so the browser never sees the token.

        Cached hard by the browser as well as in memory: artwork for a given
        id and size does not change, and a poster grid should not re-fetch
        on every page view.
        """
        item_id = (query.get("id") or [""])[0]
        kind = (query.get("type") or ["Primary"])[0]
        try:
            width = int((query.get("w") or ["400"])[0])
        except ValueError:
            width = 400
        try:
            content_type, blob = artwork.fetch_image(item_id, kind, width)
        except artwork.MediaError as err:
            self._send(404, str(err), "text/plain; charset=utf-8")
            return
        except core.JellyStatError as err:
            self._send(503, str(err), "text/plain; charset=utf-8")
            return
        except Exception as err:
            log("Image fetch failed: %s" % err, xbmc.LOGWARNING)
            self._send(500, "Could not fetch the image.",
                       "text/plain; charset=utf-8")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        try:
            self.wfile.write(blob)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_backup(self, query):
        """Download one automatic snapshot."""
        name = (query.get("name") or [""])[0]
        path = backup.path_of(name)
        if not path:
            self._send(404, "No such backup.", "text/plain; charset=utf-8")
            return
        try:
            with open(path, "rb") as handle:
                blob = handle.read()
        except OSError as err:
            self._send(500, "Could not read it: %s" % err,
                       "text/plain; charset=utf-8")
            return
        self._send(200, blob, "application/octet-stream",
                   [("Content-Disposition",
                     'attachment; filename="%s"' % name)])

    def _serve_trakt_unmatched(self, query):
        """The rows an import could not place, as a file to work through."""
        try:
            body = trakt_import.unmatched_csv((query.get("token") or [""])[0])
        except trakt_import.TraktImportError as err:
            self._send_json(404, {"error": str(err)})
            return
        self._send(200, body, "text/csv; charset=utf-8",
                   [("Content-Disposition",
                     'attachment; filename="trakt-unmatched.csv"')])

    def _serve_trakt_stage(self):
        """Parse an uploaded Trakt export and report what it holds."""
        try:
            # A whole Trakt export runs to several megabytes of JSON.
            body = self._json_body(importer.MAX_BYTES)
        except importer.ImportError_ as err:
            self._send_json(413, {"error": str(err)})
            return
        envelope = body.get("files")
        if not isinstance(envelope, dict) or not envelope:
            self._send_json(400, {"error": "No Trakt files were sent."})
            return
        try:
            self._send_json(200, trakt_import.stage(envelope))
        except trakt_import.TraktImportError as err:
            self._send_json(400, {"error": str(err)})
        except core.JellyStatError as err:
            self._send_json(503, {"error": str(err)})
        except Exception as err:
            log("Trakt staging failed: %s" % err, xbmc.LOGERROR)
            self._send_json(500, {"error": "Could not read that export: %s"
                                           % err})

    def _serve_trakt_commit(self):
        body = self._json_body()
        try:
            self._send_json(200, trakt_import.commit(
                body.get("token"), body.get("categories"),
                body.get("mode") or trakt_import.DEFAULT_MODE))
        except trakt_import.TraktImportError as err:
            self._send_json(400, {"error": str(err)})
        except Exception as err:
            log("Trakt import failed: %s" % err, xbmc.LOGERROR)
            self._send_json(500, {"error": "The import failed: %s" % err})

    def _serve_export(self):
        """Download the whole database - mirror, snapshots, play log.

        Served from a point-in-time copy made with SQLite's own backup API,
        so a write landing mid-download cannot hand out a torn file. The
        copy is small (a few MB) and lives only for the request.
        """
        import sqlite3
        import tempfile
        stamp = datetime.now().strftime("%Y-%m-%d")
        source = snapshot = None
        snap_path = None
        try:
            source = sqlite3.connect(history.db_path(), timeout=10)
            with tempfile.NamedTemporaryFile(suffix=".db",
                                             delete=False) as handle:
                snap_path = handle.name
            snapshot = sqlite3.connect(snap_path)
            with snapshot:
                source.backup(snapshot)
            snapshot.close()
            snapshot = None
            with open(snap_path, "rb") as handle:
                blob = handle.read()
        except Exception as err:
            self._send_json(500, {"error": "Could not export: %s" % err})
            return
        finally:
            # The copy is created the moment the connection opens, so an
            # export that fails part-way used to leave a multi-megabyte
            # file in the temp directory and two open handles behind it -
            # every time it failed.
            for handle in (snapshot, source):
                try:
                    if handle is not None:
                        handle.close()
                except Exception:
                    pass
            if snap_path:
                try:
                    os.unlink(snap_path)
                except OSError:
                    pass
        self._send(200, blob, "application/octet-stream",
                   [("Content-Disposition",
                     'attachment; filename="jellystat-backup-%s.db"'
                     % stamp)])

    def _serve_history(self, query):
        media = (query.get("media") or ["movies"])[0]
        if media not in ("movies", "tv"):
            media = "movies"
        self._send_json(200, {
            "totals": history.totals_series(),
            "genres": history.genre_history(media),
            "media": media,
        })


LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JellyStat</title>
<style>
:root { color-scheme: dark; }
body { margin:0; min-height:100vh; display:flex; align-items:center;
  justify-content:center; background:#11131a; color:#e7e9f0;
  font:16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
form { background:#181b24; border:1px solid #272b38; border-radius:14px;
  padding:32px; width:min(340px, 90vw); }
h1 { margin:0 0 4px; font-size:20px; }
p { margin:0 0 20px; color:#9aa1b4; font-size:14px; }
p.err { color:#ff8b8b; }
/* Sized for a remote as well as a thumb: this page is opened on a
   television as often as on a phone, and a d-pad can only reach what it
   can see. Both controls are full width, both are tall enough to hit
   from across a room, and both say plainly when they hold focus - a TV
   browser moves focus with arrow keys and draws nothing itself, so a
   page with no focus style is a page you navigate blind. */
input { width:100%; box-sizing:border-box; padding:14px 12px; font-size:17px;
  border-radius:8px; border:1px solid #333846; background:#0e1017;
  color:inherit; }
button { margin-top:14px; width:100%; padding:14px; font-size:17px;
  border:0; border-radius:8px; background:#7c5cff; color:#fff;
  cursor:pointer; }
input:focus, button:focus {
  outline:3px solid #b9a6ff; outline-offset:3px; }
button:focus, button:hover { background:#8f74ff; }
@media (prefers-reduced-motion: no-preference) {
  input, button { transition:outline-color .12s ease, background .12s ease; }
}
</style></head>
<body><form method="post" action="/login">
<h1>JellyStat</h1>
<p>This dashboard is password protected.</p>
<!--ERROR-->
<label for="pw" style="display:block;margin-bottom:6px;font-size:14px;
  color:#9aa1b4">Password</label>
<input id="pw" type="password" name="password" autofocus
  autocomplete="current-password" enterkeyhint="go">
<button type="submit">Open dashboard</button>
</form></body></html>
"""


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def is_running():
    return _server is not None


def current_port():
    return _server.server_address[1] if _server else None


def start():
    """Start the server if the settings ask for it. Returns True if serving.

    Safe to call repeatedly: an already-running server is left alone. A port
    that is taken is reported once and treated as "not serving" rather than
    retried forever.
    """
    global _server, _thread
    if _server is not None:
        return True
    addon = _addon()
    if addon.getSetting("web_enabled") != "true":
        return False
    port = _setting_int(addon, "web_port", DEFAULT_PORT)
    host = "0.0.0.0" if addon.getSetting("web_bind_all") != "false" \
        else "127.0.0.1"
    refused = lan_refused(addon)
    _notify_lan_refused(refused)
    if refused:
        # Asked to listen on every interface with no password set. That
        # combination hands anything on the network the whole database
        # over /api/export - every title, every viewing, every file path -
        # and the run of the box besides: /api/play, /api/rate,
        # /api/import, /api/reconcile. The origin check cannot help here,
        # because a program that is not a browser simply omits the header
        # it inspects.
        #
        # So it serves the machine it is running on and says why, rather
        # than doing the exposing thing quietly. Setting a password in the
        # addon's settings restores it, which is one field and takes
        # effect within seconds.
        host = "127.0.0.1"
        log("The dashboard is set to be reachable from other machines but "
            "has no password, so it is being served to this machine only. "
            "Set a password in the addon's Web dashboard settings to open "
            "it to the network.", xbmc.LOGWARNING)
    try:
        server = ThreadingHTTPServer((host, port), Handler)
    except OSError as err:
        log("Could not start the dashboard on %s:%d - %s" % (host, port, err),
            xbmc.LOGERROR)
        return False
    server.daemon_threads = True
    # Do not join request threads on close: a browser holding an idle
    # keep-alive connection would stall Kodi's shutdown until the request
    # timeout, and Kodi kills the service after five seconds of that.
    server.block_on_close = False
    _server = server
    _thread = threading.Thread(target=server.serve_forever,
                               kwargs={"poll_interval": 0.5},
                               name="JellyStatWeb")
    _thread.daemon = True
    _thread.start()
    log("Dashboard listening on http://%s:%d/" % (host, port))
    return True


def stop():
    global _server, _thread
    if _server is None:
        return
    try:
        _server.shutdown()
        _server.server_close()
    except Exception as err:
        log("Dashboard did not shut down cleanly: %s" % err, xbmc.LOGWARNING)
    _server = None
    _thread = None
    log("Dashboard stopped")


_lan_warned = False


def _notify_lan_refused(refused):
    """Say on screen that the dashboard is being kept to this machine.

    The log alone was not enough. This takes away access somebody was
    already using - the phone that opened the dashboard yesterday simply
    stops connecting - and nobody reads kodi.log to find out why. The
    reason and the remedy belong where they will be seen.

    Once per transition into the state, not once per start: a box left
    this way would otherwise nag at every reboot, and the moment worth
    interrupting is the one where it changes, which is usually right after
    somebody turns "reachable from other machines" on.
    """
    global _lan_warned
    if not refused:
        _lan_warned = False        # a later lapse is worth saying again
        return
    if _lan_warned:
        return
    _lan_warned = True
    try:
        import xbmcgui
        xbmcgui.Dialog().notification(
            "JellyStat",
            "Dashboard is on this machine only - set a password to open "
            "it to other devices",
            xbmcgui.NOTIFICATION_WARNING, 7000)
    except Exception as err:
        # A notification that will not draw must never stop the server.
        log("Could not show the dashboard notice: %s" % err, xbmc.LOGDEBUG)


def lan_refused(addon=None):
    """True when network access is asked for but withheld for want of a
    password.

    The one combination this refuses: listening on every interface with no
    password at all. Everything else - loopback with or without a password,
    the network with one - is served exactly as configured.
    """
    addon = addon or _addon()
    return (addon.getSetting("web_bind_all") != "false"
            and not (addon.getSetting("web_password") or "").strip())


def local_urls(port):
    """Addresses to show the user, best guess first.

    The LAN address is what they actually need (the point is reaching it from
    another machine), so it is worked out by asking the OS which interface
    would carry traffic out - no extra dependency, no guessing at subnets.
    """
    urls = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.settimeout(0.5)
        # No packet is sent by connect() on UDP; this just picks the route.
        # A public address, so a box with a 10/8 side route still answers
        # with the interface its default route uses.
        probe.connect(("8.8.8.8", 1))
        urls.append("http://%s:%d/" % (probe.getsockname()[0], port))
        probe.close()
    except OSError:
        pass
    urls.append("http://%s:%d/" % (socket.gethostname(), port))
    urls.append("http://127.0.0.1:%d/" % port)
    return urls
