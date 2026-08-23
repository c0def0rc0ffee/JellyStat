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
from datetime import datetime, timedelta, timezone

import xbmc

import history

# How a timestamp is spelled in this table, everywhere, by every writer.
# Readers compare these as strings - library._known_session does its
# duplicate check with a BETWEEN over them - so a second spelling is not a
# cosmetic difference, it is a row that silently escapes the comparison.
STAMP = "%Y-%m-%dT%H:%M:%S"

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
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
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


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------

def to_local(utc_naive):
    """A naive UTC datetime as the naive local time this table stores.

    Every column here is local wall time, because every question asked of
    it - which hour of the evening, which day of the week, was this the
    same sitting as that - is about where the viewer's own clock stood.
    Services that hand out UTC (Trakt does) have to be converted at the
    edge where their data arrives, not compensated for by each reader.

    Converted one timestamp at a time through the platform's timezone
    database rather than by adding today's offset to everything: a film
    watched last July was watched under July's offset, and importing it in
    January must not move it an hour. That is also why this cannot be a
    single stored number.
    """
    return datetime.fromtimestamp(
        utc_naive.replace(tzinfo=timezone.utc).timestamp())


def parse_stamp(value):
    """A stored timestamp in either spelling, or None if it is neither.

    Both are accepted because rows written before the repair below carry a
    space where everything else carries a T.
    """
    raw = (value or "").strip()[:19]
    for fmt in (STAMP, "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# One-time data repairs
# ---------------------------------------------------------------------------

# Unlike the ALTERs above, these rewrite rows rather than the schema, so
# each is recorded once it has run and never runs a second time. Re-running
# one would shift already-corrected timestamps again, which is worse than
# the fault being repaired.
TRAKT_TIME_REPAIR = "trakt-utc-and-separator-v1"

# The sources whose rows came from a Trakt export, and so were written from
# a UTC timestamp: 'trakt' from the JSON importer, 'import:trakt-csv' from
# the file importer. No other importer's timestamps are UTC - Letterboxd
# diary entries and JellyStat's own exports are local already.
_TRAKT_SOURCES = ("trakt", "import:trakt-csv")


def _repair_trakt_times(connection):
    """Move Trakt rows to local time and the standard stamp spelling.

    Before this, both Trakt paths stored Trakt's UTC timestamp as though it
    were already local, and the JSON importer wrote "date time" where every
    other writer writes "dateTtime". The first files an evening's viewing
    under the wrong hour and, anywhere west of UTC, the wrong day; the
    second slips past library._known_session's string BETWEEN, so the next
    library sync logs the same sitting all over again.

    The rows are rewritten rather than compensated for at read time because
    nothing on a row says which convention it was written under, and every
    consumer - the clock and weekday charts, screen time, both duplicate
    checks - reads the stored value at face value.

    Date-only rows are left alone: hour -1 means nobody recorded a time, so
    there is nothing to convert and shifting one would only drag it across
    midnight into the wrong day.
    """
    marks = ",".join("?" * len(_TRAKT_SOURCES))
    rows = connection.execute(
        "SELECT id, started_at, ended_at FROM plays "
        "WHERE source IN (%s) AND hour >= 0" % marks,
        _TRAKT_SOURCES).fetchall()
    repaired = 0
    for row_id, started, ended in rows:
        start_utc = parse_stamp(started)
        if start_utc is None:
            continue
        start_local = to_local(start_utc)
        end_utc = parse_stamp(ended)
        # The stored end is shifted by the same rule rather than recomputed
        # from watched_seconds, so a row keeps whatever duration it was
        # given - including the zero-length ones the CSV path writes.
        end_local = to_local(end_utc) if end_utc else start_local
        connection.execute(
            "UPDATE plays SET started_at = ?, ended_at = ?, day = ?, "
            "hour = ?, weekday = ? WHERE id = ?",
            (start_local.strftime(STAMP), end_local.strftime(STAMP),
             start_local.strftime("%Y-%m-%d"), start_local.hour,
             start_local.weekday(), row_id))
        repaired += 1
    return repaired


def _data_repairs(connection):
    """Run any one-time repair this database has not had yet.

    The repair and its record are written in one transaction, so a failure
    part-way leaves the log exactly as it was rather than half-converted -
    a half-converted log cannot be told apart from an unconverted one, and
    the next attempt would shift the repaired rows a second time.
    """
    done = {row[0] for row in
            connection.execute("SELECT key FROM schema_meta")}
    if TRAKT_TIME_REPAIR in done:
        return
    marks = ",".join("?" * len(_TRAKT_SOURCES))
    pending = connection.execute(
        "SELECT COUNT(*) FROM plays WHERE source IN (%s) AND hour >= 0"
        % marks, _TRAKT_SOURCES).fetchone()[0]
    if pending:
        # A copy first: this rewrites rows that cannot be reconstructed
        # from anywhere else, which is exactly what backup.py is for.
        # Imported here so a missing backup module cannot stop the log
        # opening at all.
        try:
            import backup
            backup.create(when="before", reason="timestamp-repair")
        except Exception as err:
            log("Could not snapshot before repairing timestamps: %s" % err,
                xbmc.LOGWARNING)
    with connection:
        repaired = _repair_trakt_times(connection)
        connection.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
            (TRAKT_TIME_REPAIR, datetime.now().strftime(STAMP)))
    if repaired:
        log("Repaired %d Trakt play row(s): UTC timestamps moved to local "
            "time and stamps rewritten in the standard form." % repaired)


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
        # After the columns exist, since a repair reads them. A repair that
        # fails must not stop the log opening - the addon is still usable
        # with mis-stamped Trakt rows, and it is not usable at all if every
        # connect() raises. It is left unrecorded, so the next start retries.
        try:
            _data_repairs(connection)
        except Exception as err:
            log("Could not repair stored timestamps: %s" % err,
                xbmc.LOGWARNING)
        _schema_done = True
    return connection


def dedup_key(show, title):
    """How a session is matched against a Jellyfin last-played date.

    Case-folded show + title, because the two sources spell the same episode
    identically but not always in the same case.
    """
    return ((show or "").strip().lower(), (title or "").strip().lower())


def _completed(session, runtime, watched):
    """Whether this sitting carried the item to the end, as Jellyfin judges.

    Measured by how far playback reached, not by how long the sitting ran.
    The two agree only for a film watched from the start in one go: an
    evening that picks a two hour film up at 1:40 and plays it out covers
    twenty minutes, which is the whole film finished and nowhere near
    ninety percent of anything. Jellyfin marks that played, because it
    looks at the position too, and this column exists to mean what its
    "played" means.

    Without a sampled position there is nothing to read but the clock, so
    the old rule stands for sittings that ended before the first sample.
    """
    if not runtime:
        return False
    if session.get("samples"):
        return int(session.get("position") or 0) >= runtime * COMPLETE_FRACTION
    return watched >= runtime * COMPLETE_FRACTION


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
        completed = _completed(session, runtime, watched)
        connection = connect()
        with connection:
            connection.execute(
                "INSERT INTO plays (started_at, ended_at, day, hour, weekday, "
                "media, title, show, season, episode, year, runtime_seconds, "
                "watched_seconds, completed, source, device, batch_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (started.strftime(STAMP),
                 session["ended_at"].strftime(STAMP),
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
