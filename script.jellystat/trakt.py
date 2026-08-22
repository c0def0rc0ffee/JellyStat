# -*- coding: utf-8 -*-
"""Read a Trakt export and line it up with this library.

Trakt's export is a folder of JSON files, not one document: watch history in
`watched-history-N.json`, ratings split across `ratings-movies-N.json`,
`ratings-episodes-N.json` and `ratings-shows.json`, and much else besides.
The dashboard's upload gathers the ones it understands and sends them as a
single envelope, which is what parse() takes.

Two things make this different from the CSV importers:

- **Identity.** Every Trakt record carries IMDb, TMDb and TVDb ids, and so
  does every item Jellyfin holds. Matching on those instead of on titles is
  the difference between eight episodes in ten finding their row and
  practically all of them - "24" alone spells its episode titles four
  different ways across the two services.

- **Duration.** Trakt records *that* something was played, never for how
  long. So an imported sitting is credited with the item's runtime and
  flagged as assumed, and anything reading the play log can tell the
  difference between a measured hour and an assumed one.

Nothing here writes: parsing and matching are separate from committing so
the staging screen can show exactly what would happen first.
"""

import re

import xbmc

import library
import main as core

# Trakt's own words for how a play was recorded. A scrobble came from a
# player reporting progress in real time; a "watch" is usually a backfill,
# someone ticking a box long afterwards. Worth keeping: it is the only clue
# an imported row carries about how trustworthy its timestamp is.
ACTIONS = ("scrobble", "watch", "checkin")

MOVIE_FALLBACK_MINUTES = 100
EPISODE_FALLBACK_MINUTES = 45


def log(message, level=xbmc.LOGINFO):
    xbmc.log("[JellyStat] %s" % message, level)


def norm(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _stamp(value):
    """Trakt sends UTC ISO8601 with a Z; the play log stores local time."""
    return (value or "")[:19].replace("T", " ") or None


# ---------------------------------------------------------------------------
# Recognising the files
# ---------------------------------------------------------------------------

def classify(records):
    """What a single Trakt export file holds, or None if not ours."""
    if not isinstance(records, list) or not records:
        return None
    row = records[0]
    if not isinstance(row, dict):
        return None
    if "watched_at" in row and "action" in row:
        return "history"
    if "rated_at" in row and "rating" in row:
        kind = row.get("type")
        if kind in ("movie", "episode", "show", "season"):
            return "ratings-" + kind
    if "paused_at" in row and "progress" in row:
        return "playback"
    return None


def ids_of(node):
    """The provider ids Trakt knows a title by, as strings."""
    raw = (node or {}).get("ids") or {}
    out = {}
    for key in ("imdb", "tmdb", "tvdb"):
        value = raw.get(key)
        if value:
            out[key] = str(value)
    return out


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_history(records):
    """Watch events -> the session shape the play log stores.

    `watched_seconds` is left at zero here. It is filled in by resolve(),
    which is the only place that knows the item's runtime.
    """
    sessions = []
    for row in records:
        kind = row.get("type")
        stamp = _stamp(row.get("watched_at"))
        if not stamp or kind not in ("movie", "episode"):
            continue
        node = row.get(kind) or {}
        show = row.get("show") or {}
        session = {
            "started_at": stamp,
            "media": "movie" if kind == "movie" else "episode",
            "title": node.get("title") or "?",
            "show": (show.get("title") if kind == "episode" else None),
            "season": node.get("season") if kind == "episode" else None,
            "episode": node.get("number") if kind == "episode" else None,
            "year": (node.get("year") if kind == "movie"
                     else show.get("year")),
            "watched_seconds": 0,
            "runtime_seconds": None,
            "assumed": 1,
            "source": "trakt",
            "device": row.get("action") if row.get("action") in ACTIONS
                      else None,
            "ids": ids_of(node),
            "show_ids": ids_of(show) if kind == "episode" else {},
            "trakt_id": row.get("id"),
        }
        sessions.append(session)
    sessions.sort(key=lambda s: s["started_at"])
    return sessions


def parse_ratings(records):
    """Rating rows -> {kind, ids, title, rating, rated_at, ...}."""
    out = []
    for row in records:
        kind = row.get("type")
        if kind not in ("movie", "episode", "show"):
            continue
        node = row.get(kind) or {}
        show = row.get("show") or {}
        try:
            score = float(row.get("rating"))
        except (TypeError, ValueError):
            continue
        out.append({
            "kind": kind,
            "title": node.get("title") or "?",
            "show": show.get("title") if kind == "episode" else None,
            "season": node.get("season") if kind == "episode" else None,
            "episode": node.get("number") if kind == "episode" else None,
            "year": node.get("year") or show.get("year"),
            "rating": score,
            "rated_at": _stamp(row.get("rated_at")),
            "ids": ids_of(node),
            "show_ids": ids_of(show) if kind == "episode" else {},
        })
    return out


def parse(envelope):
    """A whole gathered export -> {history: [...], ratings: [...]}."""
    history, ratings = [], []
    for kind, records in (envelope or {}).items():
        if kind == "history":
            history.extend(parse_history(records))
        elif kind.startswith("ratings-"):
            ratings.extend(parse_ratings(records))
    history.sort(key=lambda s: s["started_at"])
    return {"history": history, "ratings": ratings}


# ---------------------------------------------------------------------------
# Matching against the library
# ---------------------------------------------------------------------------

class Index(object):
    """Every way this library can be looked up, built once per import."""

    def __init__(self):
        connection = library.connect()
        self.by_imdb = {}
        self.by_tmdb = {}
        self.by_tvdb = {}
        self.movies_by_title = {}
        self.series_by_title = {}
        self.episodes_by_number = {}
        self.runtime = {}
        rows = connection.execute(
            "SELECT id, media, name, year, imdb_id, tmdb_id, tvdb_id, "
            "series_name, season, episode, runtime_minutes FROM items"
        ).fetchall()
        connection.close()
        for (item_id, media, name, year, imdb, tmdb, tvdb, series_name,
             season, episode, runtime) in rows:
            record = (item_id, media)
            if runtime:
                self.runtime[item_id] = runtime
            if imdb:
                self.by_imdb.setdefault(imdb, record)
            if tmdb:
                self.by_tmdb.setdefault((media, tmdb), record)
            if tvdb:
                self.by_tvdb.setdefault((media, tvdb), record)
            if media == "movie":
                self.movies_by_title.setdefault(norm(name), record)
                if year:
                    self.movies_by_title.setdefault(
                        "%s|%s" % (norm(name), year), record)
            elif media == "series":
                self.series_by_title.setdefault(norm(name), record)
            elif media == "episode":
                self.episodes_by_number.setdefault(
                    (norm(series_name), season, episode), record)

    def find(self, media, ids, title, show=None, season=None, episode=None,
             year=None):
        """(item_id, how) for a Trakt record, or (None, None).

        Ids first and titles last, deliberately: a title match is a guess
        that happens to be usually right, and `how` records which it was so
        the staging screen can say so.
        """
        if ids.get("imdb") and ids["imdb"] in self.by_imdb:
            return self.by_imdb[ids["imdb"]][0], "imdb"
        for key, table in (("tmdb", self.by_tmdb), ("tvdb", self.by_tvdb)):
            if ids.get(key) and (media, ids[key]) in table:
                return table[(media, ids[key])][0], key
        if media == "movie":
            if year:
                hit = self.movies_by_title.get("%s|%s" % (norm(title), year))
                if hit:
                    return hit[0], "title+year"
            hit = self.movies_by_title.get(norm(title))
            if hit:
                return hit[0], "title"
        elif media == "episode":
            hit = self.episodes_by_number.get((norm(show), season, episode))
            if hit:
                return hit[0], "show+number"
        elif media == "series":
            hit = self.series_by_title.get(norm(title))
            if hit:
                return hit[0], "title"
        return None, None


def enrich_from_server(sessions, index, progress=None):
    """Resolve what the mirror could not, by asking Jellyfin directly.

    The mirror only holds items that have been *played*, so history for a
    title sitting unwatched on the server finds no row: 507 episodes of one
    show, in the export this was built against. Those are on the server and
    do have runtimes, they simply were never marked played there.

    Two cheap queries cover it. The whole film catalogue comes back in one
    request, and each show that still has unmatched episodes is fetched
    once. Nothing is written; this only fills in item ids and runtimes.
    """
    outstanding = [s for s in sessions if not s.get("item_id")]
    if not outstanding:
        return {"movies_found": 0, "episodes_found": 0}
    creds = core.get_credentials()
    found_movies = found_episodes = 0

    if any(s["media"] == "movie" for s in outstanding):
        if progress:
            progress("Looking up films on the server")
        catalogue = core.api_get(
            creds["base"], creds["token"],
            "/Users/%s/Items" % creds["user_id"],
            {"IncludeItemTypes": "Movie", "Recursive": "true",
             "Fields": "ProviderIds,ProductionYear"}).get("Items") or []
        by_imdb, by_tmdb, by_title = {}, {}, {}
        for item in catalogue:
            ids = item.get("ProviderIds") or {}
            if ids.get("Imdb"):
                by_imdb.setdefault(ids["Imdb"], item)
            if ids.get("Tmdb"):
                by_tmdb.setdefault(str(ids["Tmdb"]), item)
            by_title.setdefault(norm(item.get("Name")), item)
        for session in outstanding:
            if session["media"] != "movie":
                continue
            ids = session["ids"]
            item = (by_imdb.get(ids.get("imdb"))
                    or by_tmdb.get(ids.get("tmdb"))
                    or by_title.get(norm(session["title"])))
            if item:
                session["item_id"] = item["Id"]
                session["matched_by"] = "server"
                _apply_runtime(session, item)
                found_movies += 1

    # Group what is left by show, so each series costs one request however
    # many of its episodes are missing.
    shows = {}
    for session in sessions:
        if session.get("item_id") or session["media"] != "episode":
            continue
        shows.setdefault(norm(session.get("show")), []).append(session)
    for key, group in shows.items():
        series_id, _ = index.find("series", group[0].get("show_ids") or {},
                                  group[0].get("show"))
        if not series_id:
            continue
        if progress:
            progress("Looking up %s" % (group[0].get("show") or "a show"))
        try:
            episodes = core.api_get(
                creds["base"], creds["token"],
                "/Shows/%s/Episodes" % series_id,
                {"userId": creds["user_id"],
                 "Fields": "ProviderIds"}).get("Items") or []
        except Exception as err:
            log("Could not read episodes for %s: %s" % (key, err),
                xbmc.LOGWARNING)
            continue
        by_imdb, by_number = {}, {}
        for item in episodes:
            ids = item.get("ProviderIds") or {}
            if ids.get("Imdb"):
                by_imdb.setdefault(ids["Imdb"], item)
            by_number.setdefault((item.get("ParentIndexNumber"),
                                  item.get("IndexNumber")), item)
        for session in group:
            item = (by_imdb.get(session["ids"].get("imdb"))
                    or by_number.get((session.get("season"),
                                      session.get("episode"))))
            if item:
                session["item_id"] = item["Id"]
                session["matched_by"] = "server"
                _apply_runtime(session, item)
                found_episodes += 1
    return {"movies_found": found_movies, "episodes_found": found_episodes}


def _apply_runtime(session, item):
    ticks = item.get("RunTimeTicks")
    if ticks:
        minutes = int(ticks // 600000000)
        session["runtime_seconds"] = minutes * 60
        session["watched_seconds"] = minutes * 60
        session["runtime_known"] = True


def resolve(sessions, index=None, runtimes=None):
    """Attach the library item and a duration to each parsed session.

    A session that matches nothing is still kept. The play happened; this
    library simply has no row for it, usually because the title is not on
    the server or was never marked played there. It gets a fallback runtime
    and is flagged, rather than being silently dropped - losing real
    history to a bookkeeping gap would be the worse error.
    """
    index = index or Index()
    runtimes = runtimes or {}
    summary = {"imdb": 0, "tmdb": 0, "tvdb": 0, "title": 0, "title+year": 0,
               "show+number": 0, "unmatched": 0}
    for session in sessions:
        item_id, how = index.find(
            session["media"], session["ids"], session["title"],
            show=session.get("show"), season=session.get("season"),
            episode=session.get("episode"), year=session.get("year"))
        session["item_id"] = item_id
        session["matched_by"] = how
        summary[how or "unmatched"] = summary.get(how or "unmatched", 0) + 1
        minutes = None
        if item_id:
            minutes = index.runtime.get(item_id) or runtimes.get(item_id)
        if not minutes:
            minutes = (MOVIE_FALLBACK_MINUTES if session["media"] == "movie"
                       else EPISODE_FALLBACK_MINUTES)
            session["runtime_known"] = False
        else:
            session["runtime_known"] = True
        session["runtime_seconds"] = int(minutes) * 60
        session["watched_seconds"] = int(minutes) * 60
    return summary
