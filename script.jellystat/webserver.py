# -*- coding: utf-8 -*-
"""The dashboard's HTTP server, run in a thread by the background service.

Small on purpose: it serves one page, two JSON endpoints and an optional
password gate, all from the standard library, because a Kodi addon cannot
assume anything else is installed on the box.

It is meant for a home network. Binding to every interface (the default) puts
the page on http://<kodi-box>:8099/ for any device on the LAN; the password
setting is there for shared households, not as protection against the open
internet, and the addon never asks you to port-forward it.
"""

import hashlib
import hmac
import json
import os
import socket
import threading
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
import trakt_api
import trakt_import
import recommend
import webdata

ADDON_ID = "script.jellystat"
PAGE = "special://home/addons/%s/resources/web/dashboard.html" % ADDON_ID
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


def _setting_int(addon, name, default):
    try:
        return int(addon.getSetting(name))
    except (ValueError, TypeError):
        return default


def _token_for(password):
    """The cookie value a correct password produces."""
    return hashlib.sha256(("jellystat:" + password).encode("utf-8")).hexdigest()


def _read_page():
    path = xbmcvfs.translatePath(PAGE)
    with open(path, encoding="utf-8") as handle:
        return handle.read()


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

        Accepts the cookie the login form sets, or the same value as an
        `X-Auth-Token` header so a script can poll the JSON without a
        browser session.
        """
        if not password:
            return True
        # Compared as bytes: compare_digest raises TypeError on non-ASCII
        # str, and both the header and the cookie arrive attacker-chosen.
        expected = _token_for(password).encode("utf-8")
        header = (self.headers.get("X-Auth-Token") or "").strip()
        if header and hmac.compare_digest(header.encode("utf-8"), expected):
            return True
        for chunk in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = chunk.strip().partition("=")
            if name == COOKIE and hmac.compare_digest(
                    value.encode("utf-8"), expected):
                return True
        return False

    # ---- routes -----------------------------------------------------------

    def do_GET(self):
        route = urlparse(self.path)
        password = self._password()
        if route.path in ("/", "/index.html"):
            if password and not self._authorised(password):
                self._send(200, LOGIN_PAGE, "text/html; charset=utf-8")
                return
            try:
                self._send(200, _read_page(), "text/html; charset=utf-8")
            except OSError as err:
                self._send(500, "Dashboard page missing: %s" % err,
                           "text/plain; charset=utf-8")
            return

        if route.path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return

        if not route.path.startswith("/api/"):
            self._send(404, "Not found", "text/plain; charset=utf-8")
            return

        # Trakt sends the browser back here from its own site, and the login
        # cookie is SameSite=Strict, so it will not be sent on that hop. The
        # gate would turn every redirect sign-in into a 401. It is safe to
        # let this one through: the callback carries nothing but a one-shot
        # unguessable value this box issued to an already-authorised session
        # a moment earlier, and trakt_api rejects anything else.
        if route.path == trakt_api.CALLBACK_PATH:
            self._serve_trakt_callback(parse_qs(route.query))
            return

        if password and not self._authorised(password):
            self._send_json(401, {"error": "Password required."})
            return

        if route.path == "/api/data":
            self._serve_data(parse_qs(route.query))
        elif route.path == "/api/history":
            self._serve_history(parse_qs(route.query))
        elif route.path == "/api/import/batches":
            self._send_json(200, {"batches": importer.batches()})
        elif route.path == "/api/search":
            term = (parse_qs(route.query).get("q") or [""])[0]
            self._send_json(200, library.search(term))
        elif route.path == "/api/movie":
            item_id = (parse_qs(route.query).get("id") or [""])[0]
            self._serve_detail("movie", item_id=item_id)
        elif route.path == "/api/show":
            name = (parse_qs(route.query).get("name") or [""])[0]
            self._serve_detail("tv", name=name)
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
        elif route.path == "/api/trakt/authorize":
            self._serve_trakt_authorize()
        elif route.path == "/api/trakt/status":
            try:
                self._send_json(200, trakt_api.status())
            except Exception as err:
                self._send_json(500, {"error": str(err)})
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
        """
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return False
        # An "Origin: null" (sandboxed frame, file://) parses to an empty
        # netloc and is rejected below, which is the right answer for it.
        host = (self.headers.get("Host") or "").strip()
        return urlparse(origin).netloc != host

    def do_POST(self):
        if self._cross_origin():
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
        if path == "/api/trakt/connect":
            try:
                self._send_json(200, trakt_api.begin())
            except trakt_api.TraktApiError as err:
                self._send_json(400, {"error": str(err)})
            return
        if path == "/api/trakt/settings":
            body = self._json_body()
            try:
                self._send_json(200, trakt_api.set_client(
                    body.get("client_id"), body.get("client_secret")))
            except trakt_api.TraktApiError as err:
                self._send_json(400, {"error": str(err)})
            return
        if path == "/api/trakt/disconnect":
            trakt_api.forget()
            self._send_json(200, trakt_api.status())
            return
        if path == "/api/trakt/fetch":
            self._serve_trakt_fetch()
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
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace")
        supplied = (parse_qs(body).get("password") or [""])[0]
        password = self._password()
        if not password or not hmac.compare_digest(
                supplied.encode("utf-8"), password.encode("utf-8")):
            self._send(200, LOGIN_PAGE.replace(
                "<!--ERROR-->",
                '<p class="err">That password was not accepted.</p>'),
                "text/html; charset=utf-8")
            return
        # Session cookie only: it lasts until the browser is closed, which is
        # the right lifetime for a page on the living-room network.
        cookie = "%s=%s; Path=/; HttpOnly; SameSite=Strict" % (
            COOKIE, _token_for(password))
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
                payload = recommend.recommendations(media, include_seen,
                                                    limit, offset, genres)
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

    def _dashboard_origin(self):
        """The address this browser is reaching the dashboard on.

        Taken from the request rather than from the settings because it is
        the one thing known to work: whatever the reader typed got here, so
        Trakt sending them back to it will get here too. It is also the
        string that has to be registered on the Trakt application, which is
        why the page shows it rather than guessing on the reader's behalf.
        """
        host = (self.headers.get("Host") or "").strip()
        if not host:
            address, port = self.server.server_address[:2]
            host = "%s:%s" % (address, port)
        return "http://" + host

    def _redirect(self, location):
        self._send(302, b"", "text/plain; charset=utf-8",
                   [("Location", location)])

    def _serve_trakt_authorize(self):
        """Send the browser to Trakt to approve the sign-in."""
        try:
            url = trakt_api.authorize_url(
                self._dashboard_origin() + trakt_api.CALLBACK_PATH)
        except trakt_api.TraktApiError as err:
            self._send_json(400, {"error": str(err)})
            return
        self._redirect(url)

    def _serve_trakt_callback(self, query):
        """Where Trakt sends the browser back, approved or not.

        Every ending is a redirect to the dashboard: the outcome is already
        in /api/trakt/status, which the page reads on load, so it can say
        what happened in its own words instead of on a bare white page.
        """
        error = (query.get("error") or [""])[0]
        if error:
            trakt_api.denied((query.get("error_description") or [""])[0]
                             or error)
        else:
            try:
                trakt_api.complete((query.get("code") or [""])[0],
                                   (query.get("state") or [""])[0])
            except trakt_api.TraktApiError as err:
                log("Trakt sign-in did not complete: %s" % err,
                    xbmc.LOGWARNING)
        self._redirect("/")

    def _serve_trakt_fetch(self):
        """Pull everything from Trakt and stage it, exactly as a file would.

        The API returns the same JSON the export files contain, so the
        fetched data goes through the identical parse, match and staging
        path; only its arrival is different.
        """
        try:
            envelope = trakt_api.fetch_all()
            self._send_json(200, trakt_import.stage(envelope))
        except trakt_api.TraktApiError as err:
            self._send_json(400, {"error": str(err)})
        except trakt_import.TraktImportError as err:
            self._send_json(400, {"error": str(err)})
        except core.JellyStatError as err:
            self._send_json(503, {"error": str(err)})
        except Exception as err:
            log("Trakt fetch failed: %s" % err, xbmc.LOGERROR)
            self._send_json(500, {"error": "Could not fetch from Trakt: %s"
                                           % err})

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
                body.get("skip_duplicates", True)))
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
        try:
            source = sqlite3.connect(history.db_path(), timeout=10)
            with tempfile.NamedTemporaryFile(suffix=".db",
                                             delete=False) as handle:
                snap_path = handle.name
            snapshot = sqlite3.connect(snap_path)
            with snapshot:
                source.backup(snapshot)
            snapshot.close()
            source.close()
            with open(snap_path, "rb") as handle:
                blob = handle.read()
            os.unlink(snap_path)
        except Exception as err:
            self._send_json(500, {"error": "Could not export: %s" % err})
            return
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
input { width:100%; box-sizing:border-box; padding:10px 12px; font-size:15px;
  border-radius:8px; border:1px solid #333846; background:#0e1017;
  color:inherit; }
button { margin-top:14px; width:100%; padding:10px; font-size:15px;
  border:0; border-radius:8px; background:#7c5cff; color:#fff;
  cursor:pointer; }
</style></head>
<body><form method="post" action="/login">
<h1>JellyStat</h1>
<p>This dashboard is password protected.</p>
<!--ERROR-->
<input type="password" name="password" placeholder="Password" autofocus>
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
