# -*- coding: utf-8 -*-
"""File import for the play log: parse, stage, compare, then commit.

Nothing an uploaded file contains touches the database until the user has
seen what is in it. The flow the web UI drives:

1. `stage(filename, raw)` - the file is parsed into normalised sessions and
   held in memory under a token. The result describes what was found: the
   format, how many sessions, the date range, and - the part that matters -
   how it compares with what is already logged: exact duplicates, days where
   the two overlap, days only the file knows about.
2. The user picks the options on the staging screen: merge (skip
   duplicates) or replace the overlapping period, and what "replace" is
   allowed to delete.
3. `commit(token, ...)` writes it, as one batch with an id, so a bad import
   is one click to remove again. `discard(token)` forgets it.

Four formats are recognised, sniffed from the content rather than the file
extension, since exports get renamed:

- Jellyfin **Playback Reporting** backup (JSON) or TSV export - the richest
  source: every play on every device with clock times.
- Another box's **JellyStat** history (`history.db`, or its JSON export) -
  for merging a second Kodi machine into one picture.
- **Trakt** history CSV (`watched_at` column, full timestamps) and
  **Letterboxd** diary CSV (dates only; those land at noon, marked so the
  time-of-day chart can leave them out).
- **Generic** CSV/JSON with documented columns, for everything else.
"""

import csv
import io
import json
import os
import secrets
import sqlite3
import tempfile
import threading
import time
from datetime import datetime

import xbmc

import history
import playlog


def _min_seconds():
    """The same "too short to be a viewing" threshold the live logger uses.

    Applies only to rows that carry a duration: a Trakt or Letterboxd row
    has no watched time at all (0 = unknown), and an unknown length is not
    evidence the sitting was short.
    """
    import xbmcaddon
    addon = xbmcaddon.Addon(history.ADDON_ID)
    try:
        return int(addon.getSetting("playlog_min_minutes")) * 60
    except (ValueError, TypeError):
        return playlog.DEFAULT_MIN_SECONDS


def _too_short(session, min_seconds):
    watched = session.get("watched_seconds") or 0
    return 0 < watched < min_seconds

MAX_BYTES = 50 * 1024 * 1024
STAGE_TTL_S = 1800  # a forgotten staging screen should not hold 50MB forever

# A Letterboxd diary has no clock times; sessions synthesised from it are
# stamped noon and flagged, so hour/weekday charts can skip them.
NOON = 12

_lock = threading.Lock()
_staged = {}  # token -> {"at": epoch, "sessions": [...], "meta": {...}}

BATCHES_SCHEMA = """
CREATE TABLE IF NOT EXISTS import_batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    imported_at TEXT NOT NULL,
    filename    TEXT NOT NULL,
    format      TEXT NOT NULL,
    mode        TEXT NOT NULL,
    sessions    INTEGER NOT NULL,
    replaced    INTEGER NOT NULL DEFAULT 0
);
"""


def log(message, level=xbmc.LOGINFO):
    xbmc.log("[JellyStat] %s" % message, level)


class ImportError_(Exception):
    """Anything wrong with the file or the request; message is user-facing."""


# ---------------------------------------------------------------------------
# Parsing - every format lands in the same session dict shape
# ---------------------------------------------------------------------------

def _parse_stamp(value):
    """Accept the timestamp spellings the known exports use."""
    value = (value or "").strip().rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M",
                "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value[:19 if "S" in fmt else 16], fmt)
        except ValueError:
            continue
    return None


def _session(started, media, title, **extra):
    ended = extra.pop("ended", None)
    watched = int(extra.pop("watched_seconds", 0) or 0)
    if ended is None:
        ended = started
    session = {
        "started_at": started,
        "ended_at": ended,
        "media": media if media in ("movie", "episode") else "other",
        "title": (title or "?").strip() or "?",
        "show": (extra.pop("show", None) or None),
        "season": extra.pop("season", None),
        "episode": extra.pop("episode", None),
        "year": extra.pop("year", None),
        "runtime_seconds": extra.pop("runtime_seconds", None),
        "watched_seconds": watched,
        "device": extra.pop("device", None),
        "dateonly": extra.pop("dateonly", False),
    }
    return session


def _int_or_none(value):
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return None


# ---- Jellyfin Playback Reporting ------------------------------------------

def _parse_playback_reporting_json(data):
    """The plugin's backup: a JSON list of raw PlaybackActivity rows."""
    sessions = []
    for row in data:
        started = _parse_stamp(row.get("DateCreated"))
        if not started:
            continue
        item_type = (row.get("ItemType") or "").lower()
        media = {"movie": "movie", "episode": "episode"}.get(item_type)
        if not media:
            continue
        name = row.get("ItemName") or "?"
        show = None
        title = name
        # Episodes arrive as "Show - s01e02 - Title"; split what splits.
        if media == "episode" and " - " in name:
            parts = name.split(" - ")
            show = parts[0].strip()
            title = parts[-1].strip()
        watched = _int_or_none(row.get("PlayDuration")) or 0
        sessions.append(_session(
            started, media, title, show=show,
            watched_seconds=watched,
            device=row.get("DeviceName") or row.get("ClientName")))
    return sessions


def _parse_playback_reporting_tsv(text):
    """The plugin's user-activity TSV export."""
    sessions = []
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) < 4:
            continue
        started = _parse_stamp(fields[1] if len(fields) > 1 else "")
        if not started:
            continue
        item_type = fields[3].strip().lower() if len(fields) > 3 else ""
        media = {"movie": "movie", "episode": "episode"}.get(item_type)
        if not media:
            continue
        name = fields[2].strip() if len(fields) > 2 else "?"
        show, title = None, name
        if media == "episode" and " - " in name:
            parts = name.split(" - ")
            show, title = parts[0].strip(), parts[-1].strip()
        watched = 0
        if len(fields) > 6:
            watched = _int_or_none(fields[6]) or 0
        sessions.append(_session(started, media, title, show=show,
                                 watched_seconds=watched,
                                 device=fields[5].strip()
                                 if len(fields) > 5 else None))
    return sessions


# ---- JellyStat -------------------------------------------------------------

def _parse_jellystat_db(raw):
    """Another box's history.db. Written to a temp file to open: sqlite has
    no in-memory way to read a database out of a bytes object on this
    Python."""
    handle, path = tempfile.mkstemp(suffix=".db")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(raw)
        connection = sqlite3.connect(path)
        try:
            rows = connection.execute(
                "SELECT started_at, ended_at, media, title, show, season, "
                "episode, year, runtime_seconds, watched_seconds, device "
                "FROM plays ORDER BY started_at").fetchall()
        finally:
            connection.close()
    except sqlite3.DatabaseError as err:
        raise ImportError_("That file is not a JellyStat history database "
                           "(%s)." % err)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    sessions = []
    for row in rows:
        started = _parse_stamp(row[0])
        if not started:
            continue
        sessions.append(_session(
            started, row[2], row[3], ended=_parse_stamp(row[1]) or started,
            show=row[4], season=row[5], episode=row[6], year=row[7],
            runtime_seconds=row[8], watched_seconds=row[9] or 0,
            device=row[10]))
    return sessions


def _parse_jellystat_json(data):
    return _parse_generic_rows(data.get("plays") or [])


# ---- Trakt / Letterboxd ----------------------------------------------------

def _parse_trakt_csv(rows):
    """Trakt history export: full timestamps in `watched_at`."""
    sessions = []
    for row in rows:
        started = _parse_stamp(row.get("watched_at"))
        if not started:
            continue
        kind = (row.get("type") or "").strip().lower()
        if kind in ("episode", "show"):
            sessions.append(_session(
                started, "episode",
                row.get("episode_title") or row.get("title") or "?",
                show=row.get("show_title") or row.get("title"),
                season=_int_or_none(row.get("season_number")),
                episode=_int_or_none(row.get("episode_number")),
                year=_int_or_none(row.get("year"))))
        else:
            sessions.append(_session(
                started, "movie", row.get("title") or "?",
                year=_int_or_none(row.get("year"))))
    return sessions


def _parse_letterboxd_csv(rows):
    """Letterboxd diary: films only, dates only - marked `dateonly` so the
    clock charts can leave them out instead of inventing an hour."""
    sessions = []
    for row in rows:
        date = _parse_stamp(row.get("Watched Date") or row.get("Date"))
        if not date:
            continue
        sessions.append(_session(
            date.replace(hour=NOON), "movie", row.get("Name") or "?",
            year=_int_or_none(row.get("Year")), dateonly=True))
    return sessions


# ---- Generic ---------------------------------------------------------------

GENERIC_COLUMNS = ("started_at", "media", "title", "show", "season",
                   "episode", "year", "watched_seconds", "device")


def _parse_generic_rows(rows):
    sessions = []
    for row in rows:
        started = _parse_stamp(str(row.get("started_at") or ""))
        if not started:
            continue
        stamp = str(row.get("started_at"))
        sessions.append(_session(
            started, (row.get("media") or "").strip().lower(),
            row.get("title"), show=row.get("show"),
            season=_int_or_none(row.get("season")),
            episode=_int_or_none(row.get("episode")),
            year=_int_or_none(row.get("year")),
            watched_seconds=_int_or_none(row.get("watched_seconds")) or 0,
            device=row.get("device"),
            dateonly=len(stamp.strip()) <= 10))
    return sessions


# ---- Sniffing --------------------------------------------------------------

def parse(filename, raw):
    """(format_name, sessions, warnings) from the uploaded bytes."""
    if len(raw) > MAX_BYTES:
        raise ImportError_("The file is over %d MB." % (MAX_BYTES // 2**20))
    if raw[:16] == b"SQLite format 3\x00":
        return "jellystat-db", _parse_jellystat_db(raw), []

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ImportError_("The file is neither text nor a JellyStat "
                           "database; is it the right export?")

    stripped = text.lstrip()
    if stripped[:1] in ("[", "{"):
        try:
            data = json.loads(stripped)
        except ValueError as err:
            raise ImportError_("The file looks like JSON but does not parse: "
                               "%s" % err)
        if isinstance(data, dict) and "plays" in data:
            return "jellystat-json", _parse_jellystat_json(data), []
        if isinstance(data, list) and data \
                and isinstance(data[0], dict) and "DateCreated" in data[0]:
            return ("playback-reporting",
                    _parse_playback_reporting_json(data), [])
        if isinstance(data, list):
            return "generic-json", _parse_generic_rows(
                [row for row in data if isinstance(row, dict)]), []
        raise ImportError_("Unrecognised JSON layout. A generic import is a "
                           "list of objects with at least started_at, media "
                           "and title.")

    first_line = stripped.splitlines()[0] if stripped else ""
    if "\t" in first_line:
        return ("playback-reporting-tsv",
                _parse_playback_reporting_tsv(stripped), [])

    rows = list(csv.DictReader(io.StringIO(stripped)))
    if not rows:
        raise ImportError_("No rows found in the file.")
    header = {name.strip() for name in (rows[0].keys() or [])}
    if "watched_at" in header:
        return "trakt-csv", _parse_trakt_csv(rows), []
    if {"Name", "Date"} <= header or {"Name", "Watched Date"} <= header:
        return ("letterboxd-csv", _parse_letterboxd_csv(rows),
                ["Letterboxd diaries carry no clock times; these plays are "
                 "excluded from the time-of-day chart."])
    if "started_at" in header:
        return "generic-csv", _parse_generic_rows(rows), []
    raise ImportError_(
        "Unrecognised CSV columns: %s. A generic import needs at least "
        "started_at, media and title (also understood: %s)."
        % (", ".join(sorted(header)[:8]), ", ".join(GENERIC_COLUMNS)))


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------

def _prune():
    cutoff = time.time() - STAGE_TTL_S
    for token in [t for t, s in _staged.items() if s["at"] < cutoff]:
        del _staged[token]


def _existing_index():
    """What is already in the log, for the comparison: exact-duplicate keys
    and a per-day session count."""
    keys = set()
    days = {}
    for event in playlog.events():
        keys.add((playlog.dedup_key(event["show"], event["title"]),
                  event["day"]))
        days[event["day"]] = days.get(event["day"], 0) + 1
    return keys, days


def duplicate_of(session, existing_keys):
    key = (playlog.dedup_key(session["show"], session["title"]),
           session["started_at"].strftime("%Y-%m-%d"))
    return key in existing_keys


def stage(filename, raw):
    """Parse and hold the file; describe it and how it compares."""
    fmt, sessions, warnings = parse(filename, raw)
    sessions = [s for s in sessions if s["media"] in ("movie", "episode")]
    if not sessions:
        raise ImportError_("The file parsed as %s but contained no movie or "
                           "episode plays." % fmt)
    sessions.sort(key=lambda s: s["started_at"])

    existing_keys, existing_days = _existing_index()
    duplicates = sum(1 for s in sessions
                     if duplicate_of(s, existing_keys))
    file_days = {s["started_at"].strftime("%Y-%m-%d") for s in sessions}
    overlap_days = sorted(day for day in file_days if day in existing_days)

    below_threshold = sum(1 for s in sessions
                          if _too_short(s, _min_seconds()))

    token = secrets.token_hex(16)
    with _lock:
        _prune()
        _staged[token] = {"at": time.time(), "sessions": sessions,
                          "meta": {"filename": filename, "format": fmt}}

    devices = {}
    for session in sessions:
        name = session.get("device") or "unknown"
        devices[name] = devices.get(name, 0) + 1
    return {
        "token": token,
        "format": fmt,
        "filename": filename,
        "warnings": warnings,
        "sessions": len(sessions),
        "movies": sum(1 for s in sessions if s["media"] == "movie"),
        "episodes": sum(1 for s in sessions if s["media"] == "episode"),
        "date_from": sessions[0]["started_at"].strftime("%Y-%m-%d"),
        "date_to": sessions[-1]["started_at"].strftime("%Y-%m-%d"),
        "dateonly": sum(1 for s in sessions if s.get("dateonly")),
        "below_threshold": below_threshold,
        "devices": sorted(devices.items(), key=lambda kv: -kv[1])[:8],
        "compare": {
            "existing_sessions": sum(existing_days.values()),
            "duplicates": duplicates,
            "new_sessions": len(sessions) - duplicates,
            "overlap_days": len(overlap_days),
            "overlap_from": overlap_days[0] if overlap_days else None,
            "overlap_to": overlap_days[-1] if overlap_days else None,
        },
        "sample": [_preview(s) for s in sessions[:8]],
    }


def _preview(session):
    return {
        "started_at": session["started_at"].strftime("%Y-%m-%d %H:%M"),
        "media": session["media"],
        "title": session["title"],
        "show": session["show"],
        "device": session.get("device"),
    }


def discard(token):
    with _lock:
        return _staged.pop(token, None) is not None


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------

def _batches_connect():
    connection = playlog.connect()
    connection.executescript(BATCHES_SCHEMA)
    return connection


def commit(token, mode="merge", replace_sources=None):
    """Write a staged batch.

    mode:
    - "merge": add everything that is not already logged (same title on the
      same day); existing rows are never touched.
    - "replace": first delete existing rows inside the file's date range -
      but only rows whose source is in `replace_sources` (default: imported
      rows only, so the Kodi box's own log survives unless explicitly
      included) - then insert the whole file.
    Returns a summary including the batch id.
    """
    with _lock:
        staged = _staged.pop(token, None)
    if staged is None:
        raise ImportError_("That staged import has expired or was already "
                           "handled; upload the file again.")
    sessions = staged["sessions"]
    meta = staged["meta"]
    source = "import:%s" % meta["format"]

    replaced = 0
    connection = _batches_connect()
    try:
        with connection:
            if mode == "replace":
                sources = replace_sources or ["import"]
                clauses = []
                for name in sources:
                    if name == "import":
                        clauses.append("source LIKE 'import:%'")
                    elif name == "kodi":
                        clauses.append("source = 'kodi'")
                if not clauses:
                    raise ImportError_("Replace mode needs at least one "
                                       "source to replace.")
                cursor = connection.execute(
                    "DELETE FROM plays WHERE day >= ? AND day <= ? AND (%s)"
                    % " OR ".join(clauses),
                    (sessions[0]["started_at"].strftime("%Y-%m-%d"),
                     sessions[-1]["started_at"].strftime("%Y-%m-%d")))
                replaced = cursor.rowcount

            existing_keys, _ = _existing_index()
            cursor = connection.execute(
                "INSERT INTO import_batches (imported_at, filename, format, "
                "mode, sessions) VALUES (?, ?, ?, ?, 0)",
                (datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                 meta["filename"], meta["format"], mode))
            batch_id = cursor.lastrowid

            inserted = 0
            skipped = 0
            dropped = 0
            min_seconds = _min_seconds()
            for session in sessions:
                if _too_short(session, min_seconds):
                    # Same rule as playlog.record(): a ten-second start is
                    # not a viewing, whichever file it arrived in.
                    dropped += 1
                    continue
                if mode == "merge" and duplicate_of(session, existing_keys):
                    skipped += 1
                    continue
                started = session["started_at"]
                connection.execute(
                    "INSERT INTO plays (started_at, ended_at, day, hour, "
                    "weekday, media, title, show, season, episode, year, "
                    "runtime_seconds, watched_seconds, completed, source, "
                    "device, batch_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "?, ?)",
                    (started.strftime("%Y-%m-%dT%H:%M:%S"),
                     session["ended_at"].strftime("%Y-%m-%dT%H:%M:%S"),
                     started.strftime("%Y-%m-%d"),
                     # Date-only rows get an out-of-range hour, so the clock
                     # charts can recognise and skip them.
                     -1 if session.get("dateonly") else started.hour,
                     started.weekday(),
                     session["media"], session["title"], session["show"],
                     session["season"], session["episode"], session["year"],
                     session["runtime_seconds"],
                     session["watched_seconds"],
                     # completed means what it means in playlog.record():
                     # >= 90% of a known runtime. An unknown runtime cannot
                     # support a completion claim, so it stores 0.
                     1 if (session["runtime_seconds"]
                           and session["watched_seconds"]
                           >= session["runtime_seconds"]
                           * playlog.COMPLETE_FRACTION) else 0,
                     source, session.get("device"), batch_id))
                inserted += 1
            connection.execute(
                "UPDATE import_batches SET sessions = ?, replaced = ? "
                "WHERE id = ?", (inserted, replaced, batch_id))
    finally:
        connection.close()
    log("Imported %d sessions from %s (%s, mode=%s, %d replaced, %d "
        "duplicates skipped, %d below the minimum length)"
        % (inserted, meta["filename"], meta["format"], mode, replaced,
           skipped, dropped))
    return {"batch_id": batch_id, "inserted": inserted, "skipped": skipped,
            "replaced": replaced, "dropped": dropped,
            "format": meta["format"]}


def batches():
    connection = _batches_connect()
    try:
        rows = connection.execute(
            "SELECT b.id, b.imported_at, b.filename, b.format, b.mode, "
            "b.replaced, COUNT(p.id), MIN(p.day), MAX(p.day) "
            "FROM import_batches b LEFT JOIN plays p ON p.batch_id = b.id "
            "GROUP BY b.id ORDER BY b.id DESC").fetchall()
    finally:
        connection.close()
    return [{"id": row[0], "imported_at": row[1], "filename": row[2],
             "format": row[3], "mode": row[4], "replaced": row[5],
             "sessions": row[6], "from": row[7], "to": row[8]}
            for row in rows]


def delete_batch(batch_id):
    """Remove an import and every session it added. Cannot restore what a
    replace-mode import deleted; the staging screen says so before commit."""
    connection = _batches_connect()
    try:
        with connection:
            cursor = connection.execute(
                "DELETE FROM plays WHERE batch_id = ?", (batch_id,))
            removed = cursor.rowcount
            connection.execute("DELETE FROM import_batches WHERE id = ?",
                               (batch_id,))
    finally:
        connection.close()
    log("Deleted import batch %d (%d sessions removed)" % (batch_id, removed))
    return removed
