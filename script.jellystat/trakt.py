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

import io
import json
import os
import re
import zipfile
from datetime import datetime

import xbmc

import library
import main as core
import playlog

# Trakt's own words for how a play was recorded. A scrobble came from a
# player reporting progress in real time; a "watch" is usually a backfill,
# someone ticking a box long afterwards. Worth keeping: it is the only clue
# an imported row carries about how trustworthy its timestamp is.
ACTIONS = ("scrobble", "watch", "checkin")

MOVIE_FALLBACK_MINUTES = 100
EPISODE_FALLBACK_MINUTES = 45

# A Trakt export arrives as a .zip. Two separate caps, because one number
# cannot do both jobs: JSON compresses about ten to one, so a cap loose
# enough to admit a real export as a zip would admit a 500MB unpacking as
# well. MAX_ZIP_BYTES bounds what is read off the socket; MAX_UNPACKED_BYTES
# bounds what the archive claims it will become, checked against the central
# directory before a single entry is opened.
MAX_ZIP_BYTES = 64 * 1024 * 1024
MAX_UNPACKED_BYTES = 512 * 1024 * 1024


def log(message, level=xbmc.LOGINFO):
    xbmc.log("[JellyStat] %s" % message, level)


def norm(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _stamp(value):
    """Trakt's UTC ISO8601 as the local timestamp the play log stores.

    Trakt sends UTC with a trailing Z. The play log is local wall time
    throughout, so the conversion belongs here, where Trakt's data enters
    the addon, rather than in each of the things that later read it.

    Anything that is not a full date and time is refused. A bare date used
    to be passed along and then brought the whole commit down when it was
    parsed strictly hours later; a row nobody can place in time is one row
    to skip, not an import to lose.
    """
    raw = (value or "").strip()[:19]
    try:
        utc = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return playlog.to_local(utc).strftime(playlog.STAMP)


# ---------------------------------------------------------------------------
# Recognising the files
# ---------------------------------------------------------------------------

def classify(records):
    """What a single Trakt export file holds, or None if not ours.

    Content, not filename. Trakt has renamed these files more than once and
    a third-party exporter names them whatever it likes, so every file is
    opened and asked what it is. The one exception is a custom list, whose
    *name* lives only in the filename - see list_name_from().
    """
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
    # Lists. All three are "a set of titles" and become one lists table row
    # each; they differ only in where the name comes from.
    if "collected_at" in row:
        return "collection"
    if "listed_at" in row:
        return "list-items"
    # The lists index: names and descriptions, no titles. Matched last
    # because it is the loosest test here - a row with a name and a privacy
    # setting and none of the stamps above.
    if "name" in row and ("privacy" in row or "item_count" in row):
        return "list-index"
    return None


def list_slug_from(path):
    """The list's own part of its filename, with Trakt's scaffolding off.

    Deliberately light-handed. An earlier version also stripped trailing
    digits, on the theory that they were list ids, and turned "Halloween
    2019" into "Halloween" - losing the year *and*, worse, breaking the
    match against the lists index that would have supplied the real name.
    A number at the end of a list name is far more often a year than an id.
    """
    stem = os.path.splitext(os.path.basename(path or ""))[0]
    stem = re.sub(r"^(?:user-)?lists?[-_]", "", stem, flags=re.I)
    stem = re.sub(r"[-_]items?$", "", stem, flags=re.I)
    return stem.strip()


def list_name_from(path):
    """A readable list name from the zip entry that held it.

    The only thing in a Trakt export that content-sniffing cannot recover.
    Every item row inside a custom list is identical in shape to every item
    row inside every other custom list; what separates "Halloween 2019"
    from "Films Dad Likes" is the filename and nothing else. Used only when
    the export carries no lists index to give the name properly.
    """
    slug = list_slug_from(path).replace("-", " ").replace("_", " ").strip()
    if not slug:
        return "Trakt list"
    # Title-cased only here, in the fallback. Where the export carries a
    # lists index the user's own capitalisation is used untouched; this is
    # for the case where all that survives is a slug, and "films dad likes"
    # reads as a mistake where "Films Dad Likes" reads as a name.
    return slug if slug != slug.lower() else slug.title()


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
# Reading the archive
# ---------------------------------------------------------------------------

class TraktZipError(Exception):
    """A .zip that could not be read as a Trakt export."""


def _unpacked_size(archive):
    """What the archive says it will become, from the central directory.

    Read from the header rather than measured while extracting, so an
    archive that would exhaust this box's memory is refused before any of
    it is decompressed rather than after.
    """
    return sum(info.file_size for info in archive.infolist()
               if not info.is_dir())


def read_zip(raw, name="export.zip"):
    """A Trakt export .zip -> the envelope stage() already takes.

    The whole point of this function is that it produces exactly what the
    browser's file picker produces, so everything downstream - matching,
    the staging report, the modes, the commit - is reached by one path and
    cannot drift into two behaviours.

    Entries are opened one at a time and the decoded text dropped as soon
    as it is parsed. Kodi runs on boxes with a few hundred megabytes to
    spare, and holding the archive, every extracted file and every parsed
    object at once is the difference between working and being killed.
    """
    if len(raw) > MAX_ZIP_BYTES:
        raise TraktZipError(
            "That archive is over %d MB." % (MAX_ZIP_BYTES // 2 ** 20))
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise TraktZipError(
            "That file is not a readable .zip. If you unzipped it already, "
            "select the .json files inside it instead.")
    unpacked = _unpacked_size(archive)
    if unpacked > MAX_UNPACKED_BYTES:
        raise TraktZipError(
            "That archive unpacks to %d MB, which is more than this addon "
            "will read." % (unpacked // 2 ** 20))

    envelope = {}
    lists = []
    index = {}
    skipped = read = 0
    for info in archive.infolist():
        if info.is_dir():
            continue
        # Only JSON is looked at. A Trakt export also carries images and
        # the odd README, and decoding a JPEG as UTF-8 to discover it is
        # not a ratings file is wasted work on a slow box.
        if not info.filename.lower().endswith(".json"):
            skipped += 1
            continue
        try:
            with archive.open(info) as handle:
                records = json.loads(handle.read().decode("utf-8"))
        except Exception as err:
            log("Skipping %s in the export: %s" % (info.filename, err),
                xbmc.LOGWARNING)
            skipped += 1
            continue
        kind = classify(records)
        if kind is None:
            skipped += 1
            continue
        read += 1
        if kind == "list-index":
            for row in records:
                if isinstance(row, dict) and row.get("name"):
                    index[norm(row["name"])] = row
        elif kind in ("list-items", "collection"):
            lists.append({"path": info.filename, "kind": kind,
                          "records": records})
        else:
            envelope[kind] = (envelope.get(kind) or []) + records

    if lists:
        envelope["lists"] = _name_lists(lists, index)
    return {"envelope": envelope, "skipped": skipped, "read": read}


def _is_collection(entry):
    """Whether this file is part of the Trakt collection rather than a list.

    Checked by content where read_zip has already classified it, and by
    filename otherwise. The filename test exists because the browser's file
    picker cannot classify a collection any more precisely than "a set of
    titles" - and without it the same export named its collection two
    different things depending on whether you handed over the .zip or the
    files inside it.
    """
    if entry.get("kind") == "collection":
        return True
    return bool(re.match(r"^collections?\b",
                         list_slug_from(entry.get("path")), re.I))


def _name_lists(found, index):
    """Attach a name and description to each set of list items.

    The lists index, when the export carries one, holds what the user
    actually called each list and what they wrote under it. The filename
    is the fallback, and a poor one - it has been through a slug.

    Entries that come out with the same name are merged rather than
    returned twice. Every real Trakt export contains both
    collection-movies.json and collection-shows.json, which are one list by
    any reading, and returning them separately meant the commit tried to
    create "Trakt collection" twice and took the whole import down with it.
    """
    out, seen = [], {}
    for entry in found:
        if _is_collection(entry):
            # Collection files split by media type and are one concept, not
            # two lists: collection-movies.json and collection-shows.json
            # both mean "things I own".
            name, description = "Trakt collection", "Imported from Trakt"
            kind = "collection"
        else:
            # Looked up on the slug rather than the tidied-up name, because
            # norm() drops the separators anyway and the slug is what the
            # name was turned into: "Halloween 2019" and "halloween-2019"
            # both normalise to halloween2019 and meet here.
            meta = index.get(norm(list_slug_from(entry.get("path")))) or {}
            name = meta.get("name") or list_name_from(entry.get("path"))
            description = meta.get("description") or ""
            kind = entry.get("kind") or "list-items"
        records = entry.get("records") or []
        held = seen.get(norm(name))
        if held is not None:
            held["records"] = held["records"] + records
            continue
        made = {"name": name, "description": description, "kind": kind,
                "records": records}
        seen[norm(name)] = made
        out.append(made)
    return out


def parse_list_items(records):
    """List rows -> the shape the lists table stores.

    Rank is kept where Trakt gave one. A list somebody ordered by hand is
    not the same object as the same titles in alphabetical order, and
    dropping the order would quietly turn one into the other.
    """
    out = []
    for row in records:
        if not isinstance(row, dict):
            continue
        kind = row.get("type")
        if kind not in ("movie", "episode", "show", "season"):
            # Collection files often omit `type` and simply carry the node.
            kind = ("movie" if "movie" in row
                    else "show" if "show" in row else None)
        if kind not in ("movie", "episode", "show"):
            continue
        node = row.get(kind) or {}
        show = row.get("show") or {}
        out.append({
            "kind": kind,
            "media": {"movie": "movie", "episode": "episode",
                      "show": "series"}[kind],
            "title": node.get("title") or "?",
            "show": show.get("title") if kind == "episode" else None,
            "season": node.get("season") if kind == "episode" else None,
            "episode": node.get("number") if kind == "episode" else None,
            "year": node.get("year") or show.get("year"),
            "rank": row.get("rank"),
            "added_at": _stamp(row.get("listed_at")
                               or row.get("collected_at")),
            "ids": ids_of(node),
            "show_ids": ids_of(show) if kind == "episode" else {},
        })
    out.sort(key=lambda r: (r["rank"] is None, r["rank"] or 0))
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
    """A whole gathered export -> {history, ratings, lists}.

    `lists` is a list of lists, not a flat set of rows like the other two:
    which list a title was on is the whole of the information, so it
    cannot be flattened here and reconstructed later.
    """
    history, ratings, collections = [], [], []
    # Two callers put lists in here. read_zip has already named them from
    # the archive; the file picker cannot, because a browser File carries a
    # name and nothing else, so it sends {path, records} and the naming
    # happens below - in one place, under one set of rules, rather than
    # reimplemented in JavaScript where it would quietly drift.
    named, index = [], {}
    for kind, records in (envelope or {}).items():
        if kind == "history":
            history.extend(parse_history(records))
        elif kind.startswith("ratings-"):
            ratings.extend(parse_ratings(records))
        elif kind == "lists":
            named.extend(records or [])
        elif kind == "list-index":
            for row in records or []:
                if isinstance(row, dict) and row.get("name"):
                    index[norm(row["name"])] = row
    for entry in _name_lists(
            [e for e in named if not e.get("name")], index) + [
            e for e in named if e.get("name")]:
        items = parse_list_items(entry.get("records") or [])
        if not items:
            continue
        collections.append({"name": entry.get("name") or "Trakt list",
                            "description": entry.get("description") or "",
                            "kind": entry.get("kind") or "list-items",
                            "items": items})
    history.sort(key=lambda s: s["started_at"])
    return {"history": history, "ratings": ratings, "lists": collections}


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

    Best-effort throughout. Matching against the mirror has already run and
    its results stand; everything here only fills gaps. So a server that is
    down, or was never configured, costs the import the extra matches and
    nothing else - it does not fail an import that needed no server to
    begin with. The episode lookups were already written that way; the
    credentials and the film catalogue were not, and one unreachable
    server threw the whole staging away after the local work had succeeded.
    """
    outstanding = [s for s in sessions if not s.get("item_id")]
    if not outstanding:
        return {"movies_found": 0, "episodes_found": 0}
    try:
        creds = core.get_credentials()
    except Exception as err:
        log("Skipping the server lookups: %s" % err, xbmc.LOGWARNING)
        return {"movies_found": 0, "episodes_found": 0}
    found_movies = found_episodes = 0

    if any(s["media"] == "movie" for s in outstanding):
        if progress:
            progress("Looking up films on the server")
        try:
            catalogue = core.api_get(
                creds["base"], creds["token"],
                "/Users/%s/Items" % creds["user_id"],
                {"IncludeItemTypes": "Movie", "Recursive": "true",
                 "Fields": "ProviderIds,ProductionYear"}).get("Items") or []
        except Exception as err:
            log("Could not read the film catalogue: %s" % err,
                xbmc.LOGWARNING)
            catalogue = []
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
