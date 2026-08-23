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

What happens where the two sides already disagree is **chosen, not assumed**.
`MODES` below names each choice, says in plain words what it does to data
that is already here, and maps to an actual code path rather than a label:
there is no mode that only sounds different from another. stage() costs out
every one of them against the real numbers, so the choice is made while
looking at what it would do rather than after.
"""

import csv
import io
import json
import secrets
import threading
import time
from datetime import datetime, timedelta

import xbmc

import backup
import importer
import library
import playlog
import trakt

STAGE_TTL_S = 1800

# Imported ratings stay in JellyStat and are not pushed to Jellyfin.
PUSH_RATINGS = False

# A Trakt play and an existing logged sitting for the same title within this
# many hours are taken to be the same event, not two viewings.
DEDUP_HOURS = 12

CATEGORIES = ("history", "ratings-movie", "ratings-episode", "ratings-show")

# How an import treats what is already here. Two independent decisions -
# what to do about a play that looks already logged, and what to do about a
# rating that disagrees - offered as named combinations rather than as two
# more questions to answer.
#
#   plays_skip_duplicates  a Trakt play within DEDUP_HOURS of a sitting
#                          already logged is the same event seen twice
#   ratings                only-new  leave every rating already here alone
#                          newer     take Trakt's only when it is the later
#                                    of the two
#                          overwrite take Trakt's wherever they disagree
MODES = {
    "missing": {
        "label": "Add what is missing",
        "badge": "SAFE",
        "blurb": "Brings over only what JellyStat does not already hold. "
                 "Nothing here is changed or removed: a play that looks "
                 "like one already logged is left out, and a title you have "
                 "already rated keeps your score even where Trakt disagrees.",
        "plays_skip_duplicates": True,
        "ratings": "only-new",
    },
    "newer": {
        "label": "Take Trakt's only where it is newer",
        "badge": "SMART",
        "blurb": "Adds everything missing, and where a rating exists in both "
                 "places takes Trakt's only if you rated it there more "
                 "recently than here. A score you changed in JellyStat since "
                 "stays.",
        "plays_skip_duplicates": True,
        "ratings": "newer",
    },
    "trakt-wins": {
        "label": "Let Trakt win",
        "badge": "RECOMMENDED",
        "blurb": "Adds everything missing and settles every disagreement in "
                 "Trakt's favour, so the two hold the same scores "
                 "afterwards. Repeat plays are still skipped.",
        "plays_skip_duplicates": True,
        "ratings": "overwrite",
    },
    "everything": {
        "label": "Everything, including repeat plays",
        "badge": "ADVANCED",
        "blurb": "As above, but keeps plays that look like sittings already "
                 "logged rather than dropping them. For when Trakt holds "
                 "rewatches this box recorded once \u2014 it will double up "
                 "anything that really was the same viewing.",
        "plays_skip_duplicates": False,
        "ratings": "overwrite",
    },
}
DEFAULT_MODE = "trakt-wins"

_staged = {}
# The dashboard is served by a threading HTTP server, so two requests can
# be inside this module at once - a double-clicked Import button is the
# ordinary way it happens. Every read or write of _staged goes through this.
_lock = threading.Lock()


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

def _existing_play_times(connection):
    """Title key -> the times that title has already been logged at.

    Times, not calendar days. The day was the easy key and the wrong one:
    a sitting this box logged at 23:55 and the Trakt scrobble stamped
    00:05 fall on two dates and were imported as two viewings, while a
    genuine morning-and-evening rewatch shares one date and was silently
    dropped as a duplicate. DEDUP_HOURS was documented as the rule the
    whole time; this is it applied.
    """
    times = {}
    for show, title, started in connection.execute(
            "SELECT show, title, started_at FROM plays"):
        when = playlog.parse_stamp(started)
        if when is None:
            continue
        times.setdefault(playlog.dedup_key(show, title), []).append(when)
    return times


def _when(value):
    """A comparable timestamp.

    Everything written now carries a T between date and time, but ratings
    stored by earlier versions from a Trakt export carry a space, and
    "2020-01-01T09:00" sorts before "2020-01-01 09:00" for no reason
    anybody meant. Comparing these as strings without flattening that is
    the kind of bug that silently keeps the wrong score, so the flattening
    stays for as long as rows written under the old spelling can still be
    sitting in the table.
    """
    return (value or "").replace("T", " ")


def _rating_report(connection, ratings):
    """What these ratings would do: new, unchanged, or a disagreement.

    Disagreements are split by which side is the more recent, because that
    is the whole difference between the "newer" mode and the "overwrite"
    one and it cannot be worked out later - the local timestamp is gone the
    moment the first write lands.
    """
    current = {}
    for item_id, score, at in connection.execute(
            "SELECT id, user_rating, user_rating_at FROM items "
            "WHERE user_rating IS NOT NULL"):
        current[item_id] = (score, at)
    fresh = same = changed = trakt_newer = unmatched = 0
    examples = []
    for row in ratings:
        item_id = row.get("item_id")
        if not item_id:
            unmatched += 1
            continue
        held = current.get(item_id)
        if held is None:
            fresh += 1
            continue
        existing, held_at = held
        if abs(float(existing) - row["rating"]) < 0.001:
            same += 1
            continue
        changed += 1
        newer = _when(row.get("rated_at")) > _when(held_at)
        if newer:
            trakt_newer += 1
        if len(examples) < 8:
            examples.append({"title": row["title"], "from": existing,
                             "to": row["rating"], "trakt_newer": newer})
    return {"new": fresh, "unchanged": same, "overwritten": changed,
            "trakt_newer": trakt_newer, "unmatched": unmatched,
            "examples": examples}


def stage(envelope, progress=None):
    """Parse and match an export; write nothing. Returns the report."""
    with _lock:
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
        existing = _existing_play_times(connection)
        window = timedelta(hours=DEDUP_HOURS)
        duplicates = 0
        for session in history:
            when = playlog.parse_stamp(session["started_at"])
            already = existing.get(
                playlog.dedup_key(session.get("show"), session["title"]), ())
            session["duplicate"] = bool(
                when is not None
                and any(abs(when - other) <= window for other in already))
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
    with _lock:
        _staged[token] = {"at": time.time(), "history": history,
                          "ratings": ratings}
    categories = {
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
    }
    return {
        "token": token,
        "policy": {"push_to_jellyfin": PUSH_RATINGS,
                   "durations": "assumed from runtime"},
        # Summed over the categories, which already include history's own
        # unmatched count - adding it again on top counted every unmatched
        # play twice and told the user an import was failing half as well
        # as it really was.
        "unmatched": sum(c.get("unmatched", 0)
                         for c in categories.values()),
        "modes": _cost_modes(categories),
        "default_mode": DEFAULT_MODE,
        "categories": categories,
    }


def _cost_modes(categories):
    """What each mode would do to this particular export.

    A named choice with no numbers beside it is still a guess. Every card
    the page draws carries the count it would actually write, worked out
    here from the same report the categories are drawn from rather than
    estimated in the browser.
    """
    history = categories.get("history") or {}
    rated = [categories[k] for k in
             ("ratings-movie", "ratings-episode", "ratings-show")
             if categories.get(k)]
    out = []
    for key, mode in MODES.items():
        plays = (history.get("would_add", 0)
                 if mode["plays_skip_duplicates"]
                 else history.get("records", 0))
        if mode["ratings"] == "only-new":
            written = sum(c.get("new", 0) for c in rated)
            changed = 0
        elif mode["ratings"] == "newer":
            written = sum(c.get("new", 0) + c.get("trakt_newer", 0)
                          for c in rated)
            changed = sum(c.get("trakt_newer", 0) for c in rated)
        else:
            written = sum(c.get("new", 0) + c.get("overwritten", 0)
                          for c in rated)
            changed = sum(c.get("overwritten", 0) for c in rated)
        out.append({
            "key": key,
            "label": mode["label"],
            "badge": mode["badge"],
            "blurb": mode["blurb"],
            "plays": plays,
            "ratings": written,
            # The number worth seeing before pressing anything: how much of
            # what is already here would stop being what it is.
            "overwrites": changed,
            "kept": sum(c.get("overwritten", 0) for c in rated) - changed,
        })
    return out


# ---------------------------------------------------------------------------
# What could not be matched
# ---------------------------------------------------------------------------

def _unmatched_rows(staged, chosen):
    """Every row that found no title in this library, as plain fields.

    A count of what did not match tells you the import was imperfect and
    nothing else. The rows tell you it was three anime specials Trakt
    numbers differently, which is something a person can act on - so they
    are handed over rather than summarised away.
    """
    rows = []
    if "history" in chosen:
        for session in staged["history"]:
            if session.get("item_id"):
                continue
            rows.append({"kind": "watch", "title": session["title"],
                         "show": session.get("show") or "",
                         "season": session.get("season") or "",
                         "episode": session.get("episode") or "",
                         "year": session.get("year") or "",
                         "when": session.get("started_at") or "",
                         "detail": ""})
    for kind in ("movie", "episode", "show"):
        if "ratings-" + kind not in chosen:
            continue
        for row in staged["ratings"]:
            if row["kind"] != kind or row.get("item_id"):
                continue
            rows.append({"kind": kind + " rating", "title": row["title"],
                         "show": row.get("show") or "",
                         "season": row.get("season") or "",
                         "episode": row.get("episode") or "",
                         "year": row.get("year") or "",
                         "when": row.get("rated_at") or "",
                         "detail": "rated %s" % row["rating"]})
    return rows


def unmatched_csv(token):
    """The unmatched rows of a staged or just-finished import, as CSV."""
    with _lock:
        _prune()
        staged = _staged.get(token)
    if not staged:
        raise TraktImportError("That import is no longer held in memory.")
    rows = _unmatched_rows(staged, list(CATEGORIES))
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Type", "Title", "Show", "Season", "Episode", "Year",
                     "When", "Detail"])
    for row in rows:
        writer.writerow([row["kind"], row["title"], row["show"],
                         row["season"], row["episode"], row["year"],
                         row["when"], row["detail"]])
    return out.getvalue()


# ---------------------------------------------------------------------------
# Committing
# ---------------------------------------------------------------------------

def _insert_history(connection, sessions, batch_id, skip_duplicates=True):
    added = skipped = 0
    for session in sessions:
        if skip_duplicates and session.get("duplicate"):
            skipped += 1
            continue
        start = datetime.strptime(session["started_at"], playlog.STAMP)
        end = start + timedelta(seconds=session["watched_seconds"])
        connection.execute(
            "INSERT INTO plays (started_at, ended_at, day, hour, weekday, "
            "media, title, show, season, episode, year, runtime_seconds, "
            "watched_seconds, completed, source, device, batch_id, assumed, "
            "item_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, "
            "'trakt', ?, ?, 1, ?)",
            (session["started_at"], end.strftime(playlog.STAMP),
             session["started_at"][:10], start.hour, start.weekday(),
             session["media"], session["title"], session.get("show"),
             session.get("season"), session.get("episode"),
             session.get("year"), session.get("runtime_seconds"),
             session["watched_seconds"], session.get("device"), batch_id,
             session.get("item_id")))
        added += 1
    return added, skipped


def _apply_ratings(connection, rows, stamp, policy):
    """Write chosen ratings under the mode's rule for disagreements.

    Returns (written, skipped, kept) - kept being the scores this box was
    already holding that the chosen mode deliberately left alone, which is
    the number the result screen reports back.
    """
    held = {}
    for item_id, score, at in connection.execute(
            "SELECT id, user_rating, user_rating_at FROM items "
            "WHERE user_rating IS NOT NULL"):
        held[item_id] = (score, at)
    written = skipped = kept = 0
    for row in rows:
        item_id = row.get("item_id")
        if not item_id:
            skipped += 1
            continue
        current = held.get(item_id)
        if current is not None:
            score, at = current
            # Checked before the policy, not inside it: a score that already
            # agrees is nothing to write under any mode, and counting it as
            # written would make the result disagree with the number the
            # card promised.
            if abs(float(score) - row["rating"]) < 0.001:
                kept += 1
                continue
            if policy == "only-new" or (
                    policy == "newer"
                    and not _when(row.get("rated_at")) > _when(at)):
                kept += 1
                continue
        connection.execute(
            "UPDATE items SET user_rating = ?, user_rating_at = ?, "
            "rating_sync = 'from trakt' WHERE id = ?",
            (row["rating"], row.get("rated_at") or stamp, item_id))
        written += 1
    return written, skipped, kept


def commit(token, categories, mode=DEFAULT_MODE):
    """Import the chosen categories under a mode, snapshot either side.

    The staged import is claimed before any writing starts. Checking a
    "done" flag that is only set at the end leaves the entire insert
    window open: a second request - a double-clicked button, a browser
    retrying - reads the flag while the first is still working, finds it
    unset, and replays the whole export. The duplicate marks were worked
    out at staging time, so nothing downstream would have skipped a single
    row of it.
    """
    chosen = [c for c in (categories or []) if c in CATEGORIES]
    if not chosen:
        raise TraktImportError("Nothing was chosen, so there was nothing "
                               "to import.")
    rule = MODES.get(mode)
    if rule is None:
        raise TraktImportError("Unknown import mode %r." % mode)
    skip_duplicates = rule["plays_skip_duplicates"]

    with _lock:
        _prune()
        staged = _staged.get(token)
        if not staged:
            raise TraktImportError(
                "That import expired before it was confirmed. Load the "
                "files again; nothing was written.")
        if staged.get("done"):
            raise TraktImportError(
                "That import has already been run. Fetch from Trakt again "
                "to import anything further; nothing was written twice.")
        if staged.get("running"):
            raise TraktImportError(
                "That import is already running. Wait for it to finish; "
                "nothing was written twice.")
        # Claimed, not completed. A run that fails releases this again so
        # the user can retry the import they already staged, while a run
        # that succeeds marks it done for good.
        staged["running"] = True

    try:
        return _run_commit(token, staged, chosen, mode, rule, skip_duplicates)
    except Exception:
        # Nothing was committed, so the claim is given back and the same
        # staged import can be confirmed again.
        with _lock:
            staged["running"] = False
        raise


def _run_commit(token, staged, chosen, mode, rule, skip_duplicates):
    """The writing half of commit(), once the import has been claimed."""
    result = {"categories": chosen, "mode": mode, "mode_label": rule["label"],
              "history": None, "ratings": {}}
    with backup.around("trakt-import") as snapshots:
        connection = library.connect()
        playlog.connect().close()          # ensure the plays schema exists
        # And the batch table, which belongs to the file importer: this
        # writes a row there too, and on a fresh install nothing had made
        # it. Going straight from a first Trakt import - without ever
        # opening the file importer or the batches list - met "no such
        # table: import_batches" and lost the whole confirmed import.
        # Its one definition is reused rather than repeated, so the two
        # writers cannot drift apart.
        connection.executescript(importer.BATCHES_SCHEMA)
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
                    written, skipped, kept = _apply_ratings(
                        connection, rows, stamp, rule["ratings"])
                    result["ratings"][key] = {"written": written,
                                              "skipped": skipped,
                                              "kept": kept}
        finally:
            connection.close()
    result["backups"] = snapshots
    result["unmatched"] = _unmatched_rows(staged, chosen)
    # Kept rather than dropped: the result screen offers the rows that found
    # no home as a file, and it can only do that while they still exist.
    # _prune clears them on the same timer as any other staged import.
    with _lock:
        staged["running"] = False
        staged["done"] = True
    # A summary, not the whole result: that carries one dict per unmatched
    # row, and an export with a few thousand of them wrote a multi-megabyte
    # single line into kodi.log on every import.
    log("Trakt import finished: %s, %d unmatched, mode %s"
        % (json.dumps({k: v for k, v in result.items()
                       if k not in ("unmatched", "backups")}),
           len(result.get("unmatched") or []), mode))
    return result
