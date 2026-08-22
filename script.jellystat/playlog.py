# -*- coding: utf-8 -*-
"""A record of individual viewing sessions, with the date and time.

Jellyfin cannot supply this. Its API gives a running `PlayCount` with no
dates attached and a single `LastPlayedDate` that every rewatch overwrites,
so a film watched in January, March and last night reports as "3 plays, last
night" and the first two evenings are unrecoverable.

So the addon keeps its own log. `player.py` watches Kodi playback and calls
`record()` here when something finishes, which means these rows carry what
Jellyfin never had: the actual clock time a session started and ended, how
much of the item was really watched, and one row per sitting rather than one
per title.

The trade is coverage: only what plays on this Kodi box lands here. The
dashboard fills the gap by falling back to Jellyfin's last-played dates for
anything it has no session for (see webdata's _calendar and
_clock_and_week), so a film watched on a phone still appears - just without
the clock time.

Rows live in the same file as the daily snapshots, since they are two views
of one history and a single file is one thing to back up.
"""

import sqlite3
from datetime import datetime, timedelta

import xbmc

import history

# A session shorter than this was a mis-click or a look at the first minute,
# not an evening's viewing. Overridable in the settings.
DEFAULT_MIN_SECONDS = 120

# Jellyfin marks an item played at roughly 90% watched; matching it keeps
# "completed" here meaning the same thing as "played" there.
COMPLETE_FRACTION = 0.9

SCHEMA = """
CREATE TABLE IF NOT EXISTS plays (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT    NOT NULL,
    ended_at        TEXT    NOT NULL,
    day             TEXT    NOT NULL,
    hour            INTEGER NOT NULL,
    weekday         INTEGER NOT NULL,
    media           TEXT    NOT NULL,
    title           TEXT    NOT NULL,
    show            TEXT,
    season          INTEGER,
    episode         INTEGER,
    year            INTEGER,
    runtime_seconds INTEGER,
    watched_seconds INTEGER NOT NULL,
    completed       INTEGER NOT NULL DEFAULT 0,
    source          TEXT    NOT NULL DEFAULT 'kodi',
    device          TEXT,
    batch_id        INTEGER
);
CREATE INDEX IF NOT EXISTS plays_day ON plays (day);
CREATE INDEX IF NOT EXISTS plays_batch ON plays (batch_id);
"""

# Columns added after the table first shipped. SQLite has no "ADD COLUMN IF
# NOT EXISTS", so each is attempted and its duplicate-column error ignored -
# cheaper and clearer than reading back the schema.
MIGRATIONS = [
    "ALTER TABLE plays ADD COLUMN source TEXT NOT NULL DEFAULT 'kodi'",
    "ALTER TABLE plays ADD COLUMN device TEXT",
    "ALTER TABLE plays ADD COLUMN batch_id INTEGER",
    # Whether watched_seconds was measured or inferred from the runtime.
    # Trakt and similar services record that a play happened and never how
    # long it lasted, so their rows carry a whole runtime that nobody
    # actually timed. Screen time says so rather than presenting 400 days
    # of assumption as measurement.
    "ALTER TABLE plays ADD COLUMN assumed INTEGER NOT NULL DEFAULT 0",
    # The mirror row this play belongs to, where one could be found.
    "ALTER TABLE plays ADD COLUMN item_id TEXT",
]


def log(message, level=xbmc.LOGINFO):
    xbmc.log("[JellyStat] %s" % message, level)


_schema_done = False


def connect():
    # Same file as the snapshot history; db_path() also makes the folder.
    global _schema_done
    connection = sqlite3.connect(history.db_path(), timeout=10)
    if not _schema_done:
        # Once per process is enough: webdata.build() alone opens five
        # connections per rebuild, and re-running DDL on each would be felt
        # on a Pi with a large log.
        connection.executescript(SCHEMA)
        for statement in MIGRATIONS:
            try:
                connection.execute(statement)
            except sqlite3.OperationalError:
                pass  # already present
        _schema_done = True
    return connection


def dedup_key(show, title):
    """How a session is matched against a Jellyfin last-played date.

    Case-folded show + title, because the two sources spell the same episode
    identically but not always in the same case.
    """
    return ((show or "").strip().lower(), (title or "").strip().lower())


def record(session, min_seconds=DEFAULT_MIN_SECONDS):
    """Store one finished session. Returns True if it was kept.

    Sessions below the threshold are dropped rather than stored and filtered
    later, so the log stays a record of viewing rather than of every time a
    file was opened. Never raises: a lost row must not disturb playback.
    """
    watched = int(session.get("watched_seconds") or 0)
    if watched < min_seconds:
        return False
    try:
        started = session["started_at"]
        runtime = int(session.get("runtime_seconds") or 0)
        completed = bool(runtime and watched >= runtime * COMPLETE_FRACTION)
        connection = connect()
        with connection:
            connection.execute(
                "INSERT INTO plays (started_at, ended_at, day, hour, weekday, "
                "media, title, show, season, episode, year, runtime_seconds, "
                "watched_seconds, completed, source, device, batch_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (started.strftime("%Y-%m-%dT%H:%M:%S"),
                 session["ended_at"].strftime("%Y-%m-%dT%H:%M:%S"),
                 started.strftime("%Y-%m-%d"),
                 started.hour,
                 started.weekday(),
                 session.get("media") or "other",
                 session.get("title") or "?",
                 session.get("show"),
                 session.get("season"),
                 session.get("episode"),
                 session.get("year"),
                 runtime or None,
                 watched,
                 1 if completed else 0,
                 session.get("source") or "kodi",
                 session.get("device"),
                 session.get("batch_id")))
        connection.close()
        log("Logged %s: %s (%d min%s)"
            % (session.get("media") or "item", session.get("title"),
               round(watched / 60.0), ", completed" if completed else ""))
        return True
    except Exception as err:
        log("Could not log the session: %s" % err, xbmc.LOGWARNING)
        return False


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _rows(query, params=()):
    try:
        connection = connect()
        rows = connection.execute(query, params).fetchall()
        connection.close()
        return rows
    except Exception as err:
        log("Could not read the play log: %s" % err, xbmc.LOGWARNING)
        return []


def since():
    """The first day the log covers, or None if it is empty.

    Everything the dashboard does with sessions hinges on this date: before
    it there were no sessions to miss, so Jellyfin's last-played dates are
    the only account of those days and are used unchallenged.
    """
    rows = _rows("SELECT MIN(day) FROM plays")
    return rows[0][0] if rows and rows[0][0] else None


def _kind(media):
    if media == "movie":
        return "movies"
    if media == "episode":
        return "episodes"
    return None


def events(from_day=None, days=None):
    """Sessions as plain dicts, oldest first, for the habit charts."""
    query = ("SELECT day, hour, weekday, media, title, show, season, episode, "
             "year, started_at, ended_at, watched_seconds, completed, source "
             "FROM plays")
    params = []
    if from_day is None and days:
        from_day = (datetime.now()
                    - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    if from_day:
        query += " WHERE day >= ?"
        params.append(from_day)
    query += " ORDER BY started_at"
    return [{
        "day": row[0], "hour": row[1], "weekday": row[2], "media": row[3],
        "kind": _kind(row[3]), "title": row[4], "show": row[5],
        "season": row[6], "episode": row[7], "year": row[8],
        "started_at": row[9], "ended_at": row[10],
        "watched_seconds": row[11], "completed": bool(row[12]),
        "source": row[13],
    } for row in _rows(query, tuple(params))]


def summary():
    """Headline numbers about the log itself, for the dashboard's cards."""
    rows = _rows("SELECT COUNT(*), SUM(watched_seconds), "
                 "SUM(CASE WHEN media = 'movie' THEN 1 ELSE 0 END), "
                 "SUM(CASE WHEN media = 'episode' THEN 1 ELSE 0 END), "
                 "SUM(CASE WHEN assumed = 1 THEN 1 ELSE 0 END), "
                 "SUM(CASE WHEN assumed = 1 THEN watched_seconds ELSE 0 END) "
                 "FROM plays")
    if not rows or not rows[0][0]:
        return {"sessions": 0, "hours_watched": 0.0, "movies": 0,
                "episodes": 0, "since": None, "assumed": 0,
                "assumed_hours": 0.0}
    count, seconds, movies, episodes, assumed, assumed_seconds = rows[0]
    return {
        "sessions": count,
        "hours_watched": round((seconds or 0) / 3600.0, 1),
        "movies": movies or 0,
        "episodes": episodes or 0,
        "assumed": assumed or 0,
        "assumed_hours": round((assumed_seconds or 0) / 3600.0, 1),
        "since": since(),
    }


def rewatches(limit=10):
    """Titles with more than one logged session - real rewatch evidence.

    This is the question Jellyfin's PlayCount can only half answer: it knows
    a title was played three times, this knows which evenings they were.
    """
    rows = _rows(
        "SELECT title, show, media, COUNT(*) AS sessions, MIN(day), MAX(day) "
        "FROM plays GROUP BY LOWER(COALESCE(show, '')), LOWER(title), media "
        "HAVING sessions > 1 ORDER BY sessions DESC, MAX(day) DESC LIMIT ?",
        (limit,))
    return [{"title": row[0], "show": row[1], "media": row[2],
             "sessions": row[3], "first": row[4], "last": row[5]}
            for row in rows]


def recent(limit=25):
    """The latest sessions, newest first, with their real clock times."""
    rows = _rows(
        "SELECT started_at, ended_at, media, title, show, season, episode, "
        "watched_seconds, completed, source FROM plays "
        "ORDER BY started_at DESC LIMIT ?", (limit,))
    return [{"started_at": row[0], "ended_at": row[1], "media": row[2],
             "title": row[3], "show": row[4], "season": row[5],
             "episode": row[6], "watched_seconds": row[7],
             "completed": bool(row[8]), "source": row[9]} for row in rows]
