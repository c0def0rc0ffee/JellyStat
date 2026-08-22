# -*- coding: utf-8 -*-
"""Stage a Trakt export, show what is in it, import the parts chosen.

The shape is deliberate: **nothing is written until you have seen the
numbers and ticked the categories**. A Trakt export carries five years of
somebody's viewing, and an import that silently guessed wrong about ten
thousand rows would be discovered weeks later, if at all.

So it runs in three steps:

1. `stage()` parses, matches against the library, and reports per category
   what it found, how it matched, and what it would overwrite.
2. You choose categories.
3. `commit()` writes only those, with a database snapshot taken
   immediately before and immediately after (see backup.py).

Conflicts go to Trakt. That is the user's standing decision and it is not
inferred here: `RATING_POLICY` names it, so changing it is one edit rather
than an archaeology exercise.
"""

import json
import secrets
import time
from datetime import datetime, timedelta

import xbmc

import backup
import library
import playlog
import trakt

STAGE_TTL_S = 1800

# Where a rating exists in both places and they disagree.
RATING_POLICY = "trakt-wins"

# Imported ratings stay in JellyStat and are not pushed to Jellyfin.
PUSH_RATINGS = False

# A Trakt play and an existing logged sitting for the same title within this
# many hours are taken to be the same event, not two viewings.
DEDUP_HOURS = 12

CATEGORIES = ("history", "ratings-movie", "ratings-episode", "ratings-show")

_staged = {}


def log(message, level=xbmc.LOGINFO):
    xbmc.log("[JellyStat] %s" % message, level)


class TraktImportError(Exception):
    """Anything that stops a Trakt import."""


def _prune():
    cutoff = time.time() - STAGE_TTL_S
    for token in [t for t, s in _staged.items() if s["at"] < cutoff]:
        del _staged[token]


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------

def _existing_play_keys(connection):
    """(title key, day) for every sitting already logged."""
    keys = set()
    for show, title, day in connection.execute(
            "SELECT show, title, day FROM plays"):
        keys.add((playlog.dedup_key(show, title), day))
    return keys


def _rating_report(connection, ratings):
    """What the chosen ratings would do: new, unchanged, or overwritten."""
    current = {}
    for item_id, score in connection.execute(
            "SELECT id, user_rating FROM items WHERE user_rating IS NOT NULL"):
        current[item_id] = score
    fresh = same = changed = unmatched = 0
    examples = []
    for row in ratings:
        item_id = row.get("item_id")
        if not item_id:
            unmatched += 1
            continue
        existing = current.get(item_id)
        if existing is None:
            fresh += 1
        elif abs(float(existing) - row["rating"]) < 0.001:
            same += 1
        else:
            changed += 1
            if len(examples) < 8:
                examples.append({"title": row["title"],
                                 "from": existing, "to": row["rating"]})
    return {"new": fresh, "unchanged": same, "overwritten": changed,
            "unmatched": unmatched, "examples": examples}


def stage(envelope, progress=None):
    """Parse and match an export; write nothing. Returns the report."""
    _prune()
    parsed = trakt.parse(envelope)
    history = parsed["history"]
    ratings = parsed["ratings"]
    if not history and not ratings:
        raise TraktImportError(
            "No Trakt watch history or ratings found in those files. A Trakt "
            "export contains watched-history-*.json and ratings-*.json; the "
            "other files in it are not read by this import.")

    index = trakt.Index()
    trakt.resolve(history, index)
    if progress:
        progress("Matching against your library")
    server = trakt.enrich_from_server(history, index, progress)
    # Counted after the server lookup, not before it: reporting the first
    # pass would tell the user 1,244 things went unmatched when the real
    # figure, once the server was asked, is a third of that.
    match = {}
    for session in history:
        how = session.get("matched_by") or "unmatched"
        match[how] = match.get(how, 0) + 1

    # Ratings match the same way, through the same index.
    for row in ratings:
        media = {"movie": "movie", "episode": "episode",
                 "show": "series"}[row["kind"]]
        item_id, how = index.find(media, row["ids"], row["title"],
                                  show=row.get("show"),
                                  season=row.get("season"),
                                  episode=row.get("episode"),
                                  year=row.get("year"))
        row["item_id"] = item_id
        row["matched_by"] = how
        row["media"] = media

    connection = library.connect()
    try:
        existing = _existing_play_keys(connection)
        duplicates = 0
        for session in history:
            key = (playlog.dedup_key(session.get("show"), session["title"]),
                   session["started_at"][:10])
            session["duplicate"] = key in existing
            if session["duplicate"]:
                duplicates += 1
        by_kind = {"movie": [], "episode": [], "show": []}
        for row in ratings:
            by_kind[row["kind"]].append(row)
        reports = {kind: _rating_report(connection, rows)
                   for kind, rows in by_kind.items()}
    finally:
        connection.close()

    matched = sum(1 for s in history if s.get("item_id"))
    assumed = sum(1 for s in history if not s.get("runtime_known"))
    days = sorted(s["started_at"][:10] for s in history) or [None]
    minutes = sum(s["watched_seconds"] for s in history) / 60.0

    token = secrets.token_hex(16)
    _staged[token] = {"at": time.time(), "history": history,
                      "ratings": ratings}
    return {
        "token": token,
        "policy": {"conflicts": RATING_POLICY, "push_to_jellyfin":
                   PUSH_RATINGS, "durations": "assumed from runtime"},
        "categories": {
            "history": {
                "label": "Watch history",
                "records": len(history),
                "matched": matched,
                "unmatched": len(history) - matched,
                "duplicates": duplicates,
                "would_add": len(history) - duplicates,
                "assumed_runtime": assumed,
                "from": days[0], "to": days[-1],
                "hours": round(minutes / 60.0, 1),
                "matched_by": match,
                "found_on_server": server,
            },
            "ratings-movie": dict(reports["movie"], label="Film ratings",
                                  records=len(by_kind["movie"])),
            "ratings-episode": dict(reports["episode"],
                                    label="Episode ratings",
                                    records=len(by_kind["episode"])),
            "ratings-show": dict(reports["show"], label="Show ratings",
                                 records=len(by_kind["show"])),
        },
    }


# ---------------------------------------------------------------------------
# Committing
# ---------------------------------------------------------------------------

def _insert_history(connection, sessions, batch_id, skip_duplicates=True):
    added = skipped = 0
    for session in sessions:
        if skip_duplicates and session.get("duplicate"):
            skipped += 1
            continue
        start = datetime.strptime(session["started_at"], "%Y-%m-%d %H:%M:%S")
        end = start + timedelta(seconds=session["watched_seconds"])
        connection.execute(
            "INSERT INTO plays (started_at, ended_at, day, hour, weekday, "
            "media, title, show, season, episode, year, runtime_seconds, "
            "watched_seconds, completed, source, device, batch_id, assumed, "
            "item_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, "
            "'trakt', ?, ?, 1, ?)",
            (session["started_at"], end.strftime("%Y-%m-%d %H:%M:%S"),
             session["started_at"][:10], start.hour, start.weekday(),
             session["media"], session["title"], session.get("show"),
             session.get("season"), session.get("episode"),
             session.get("year"), session.get("runtime_seconds"),
             session["watched_seconds"], session.get("device"), batch_id,
             session.get("item_id")))
        added += 1
    return added, skipped


def _apply_ratings(connection, rows, stamp):
    """Write chosen ratings. Trakt wins where the two disagree."""
    written = skipped = 0
    for row in rows:
        if not row.get("item_id"):
            skipped += 1
            continue
        connection.execute(
            "UPDATE items SET user_rating = ?, user_rating_at = ?, "
            "rating_sync = 'from trakt' WHERE id = ?",
            (row["rating"], row.get("rated_at") or stamp, row["item_id"]))
        written += 1
    return written, skipped


def commit(token, categories, skip_duplicates=True):
    """Import the chosen categories, with a snapshot either side."""
    _prune()
    staged = _staged.get(token)
    if not staged:
        raise TraktImportError(
            "That import expired before it was confirmed. Load the files "
            "again; nothing was written.")
    chosen = [c for c in (categories or []) if c in CATEGORIES]
    if not chosen:
        raise TraktImportError("No categories were ticked, so there was "
                               "nothing to import.")

    result = {"categories": chosen, "history": None, "ratings": {}}
    with backup.around("trakt-import") as snapshots:
        connection = library.connect()
        playlog.connect().close()          # ensure the plays schema exists
        stamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        try:
            with connection:
                batch_id = None
                if "history" in chosen:
                    cursor = connection.execute(
                        "INSERT INTO import_batches (imported_at, filename, "
                        "format, mode, sessions) VALUES (?, ?, ?, ?, 0)",
                        (stamp, "Trakt export", "trakt", "merge"))
                    batch_id = cursor.lastrowid
                    added, skipped = _insert_history(
                        connection, staged["history"], batch_id,
                        skip_duplicates)
                    connection.execute(
                        "UPDATE import_batches SET sessions = ? WHERE id = ?",
                        (added, batch_id))
                    result["history"] = {"added": added, "skipped": skipped,
                                         "batch_id": batch_id}
                for kind in ("movie", "episode", "show"):
                    key = "ratings-" + kind
                    if key not in chosen:
                        continue
                    rows = [r for r in staged["ratings"]
                            if r["kind"] == kind]
                    written, skipped = _apply_ratings(connection, rows, stamp)
                    result["ratings"][key] = {"written": written,
                                              "skipped": skipped}
        finally:
            connection.close()
    result["backups"] = snapshots
    del _staged[token]
    log("Trakt import finished: %s" % json.dumps(result))
    return result
