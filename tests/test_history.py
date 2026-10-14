#!/usr/bin/env python3
"""
<summary>
Tests for the database layer: schema migrations on an old file, the one time
Trakt timestamp repair, the backup name check, and a restored file being
noticed while the process runs.
</summary>
<remarks>
No Kodi is needed; kodi_stubs puts the profile in a temporary folder. Every
check records a failure rather than stopping, and the process exits non zero
if any failed.

Usage:
    python3 tests/test_history.py
</remarks>
"""

import os
import sqlite3
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kodi_stubs  # noqa: E402

kodi_stubs.install()

import backup  # noqa: E402
import history  # noqa: E402
import library  # noqa: E402
import playlog  # noqa: E402

FAILURES = []

# The plays table as it first shipped: none of the columns MIGRATIONS adds.
OLD_PLAYS = """
CREATE TABLE plays (
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
    completed       INTEGER NOT NULL DEFAULT 0
);
"""


def check(condition, message):
    """<summary>Record a failure without stopping.</summary>"""
    if not condition:
        FAILURES.append(message)
        print("FAIL: %s" % message)


def equal(actual, expected, what):
    """<summary>check() for equality, printing both sides on failure.</summary>"""
    check(actual == expected, "%s: expected %r, got %r" % (what, expected, actual))


def columns(table):
    """<summary>The column names of a table in the history database.</summary>"""
    connection = sqlite3.connect(history.db_path())
    names = [row[1] for row in connection.execute("PRAGMA table_info(%s)" % table)]
    connection.close()
    return names


def forget_schema():
    """
    <summary>
    Make both modules believe this process has not migrated the file yet.
    </summary>
    """
    playlog._schema_for = None
    library._schema_for = None


def remove_db():
    """<summary>Delete the history database so the next connect starts afresh.</summary>"""
    path = history.db_path()
    if os.path.exists(path):
        os.remove(path)
    forget_schema()


def write_old_file(path, trakt_rows=()):
    """
    <summary>
    Write a database in the original schema at a path, with optional Trakt rows.
    </summary>
    <param name="path">Where to write it.</param>
    <param name="trakt_rows">(started_at, ended_at) UTC stamps to insert as Trakt plays.</param>
    <remarks>
    The source column is added by hand when rows are given, since the original
    schema had none and a Trakt row needs one to be recognised.
    </remarks>
    """
    if os.path.exists(path):
        os.remove(path)
    connection = sqlite3.connect(path)
    connection.executescript(OLD_PLAYS)
    if trakt_rows:
        connection.execute("ALTER TABLE plays ADD COLUMN source TEXT NOT NULL DEFAULT 'kodi'")
        for started, ended in trakt_rows:
            connection.execute(
                "INSERT INTO plays (started_at, ended_at, day, hour, weekday, media, "
                "title, watched_seconds, source) VALUES (?, ?, ?, ?, ?, 'movie', 'T', "
                "6000, 'trakt')",
                (started, ended, started[:10], int(started[11:13]), 0))
    connection.commit()
    connection.close()


# ---- tests ----------------------------------------------------------------

def test_migrations_on_old_schema():
    """<summary>A file in the original schema gains every column MIGRATIONS adds.</summary>"""
    print("running test_migrations_on_old_schema")
    remove_db()
    write_old_file(history.db_path())
    before = columns("plays")
    check("assumed" not in before and "item_id" not in before, "old file has no new columns")
    playlog.connect().close()
    after = columns("plays")
    for name in ("source", "device", "batch_id", "assumed", "item_id"):
        check(name in after, "column %s added by migration" % name)
    check("schema_meta" in [row[0] for row in sqlite3.connect(history.db_path()).execute(
        "SELECT name FROM sqlite_master WHERE type='table'")], "schema_meta created")
    library.connect().close()
    items = columns("items")
    for name in ("user_rating", "jf_rating", "runtime_minutes", "imdb_id"):
        check(name in items, "mirror column %s present" % name)


def test_repair_runs_once():
    """<summary>The Trakt timestamp repair shifts a row exactly once, however often it runs.</summary>"""
    print("running test_repair_runs_once")
    remove_db()
    utc = "2026-07-01T20:00:00"
    write_old_file(history.db_path(), [(utc, utc)])
    expected = playlog.to_local(playlog.parse_stamp(utc)).strftime(playlog.STAMP)
    playlog.connect().close()
    rows = sqlite3.connect(history.db_path()).execute(
        "SELECT started_at FROM plays").fetchall()
    equal(rows, [(expected,)], "row converted to local time on first connect")
    # A second process, or this one after a restart, must leave it alone.
    forget_schema()
    playlog.connect().close()
    connection = playlog.connect()
    playlog._data_repairs(connection)
    playlog._data_repairs(connection)
    connection.close()
    rows = sqlite3.connect(history.db_path()).execute(
        "SELECT started_at FROM plays").fetchall()
    equal(rows, [(expected,)], "repair never shifts a row a second time")


def test_concurrent_first_connect():
    """<summary>Several threads opening the log at once still repair each row once.</summary>"""
    print("running test_concurrent_first_connect")
    remove_db()
    utc = "2026-03-15T22:30:00"
    write_old_file(history.db_path(), [(utc, utc)] * 5)
    expected = playlog.to_local(playlog.parse_stamp(utc)).strftime(playlog.STAMP)
    errors = []

    def opener():
        """<summary>One connect, errors kept rather than lost in the thread.</summary>"""
        try:
            playlog.connect().close()
        except Exception as err:  # reported below
            errors.append(err)

    threads = [threading.Thread(target=opener) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    equal(errors, [], "no thread failed")
    rows = sqlite3.connect(history.db_path()).execute(
        "SELECT DISTINCT started_at FROM plays").fetchall()
    equal(rows, [(expected,)], "every row shifted exactly once under concurrency")


def test_restored_file_is_migrated():
    """<summary>A file swapped under the running process gets its migrations too.</summary>"""
    print("running test_restored_file_is_migrated")
    remove_db()
    playlog.connect().close()
    library.connect().close()
    check("assumed" in columns("plays"), "fresh file migrated")
    # Restore an older backup by hand: a different inode under the same name.
    older = history.db_path() + ".restore"
    write_old_file(older)
    os.replace(older, history.db_path())
    check("assumed" not in columns("plays"), "restored file lacks the column")
    playlog.connect().close()
    library.connect().close()
    check("assumed" in columns("plays"), "restored file migrated on the next connect")
    check("jf_rating" in columns("items"), "mirror schema restored too")


def test_history_round_trip():
    """<summary>A recorded snapshot is read back as a series and a genre history.</summary>"""
    print("running test_history_round_trip")
    remove_db()
    payload = {"generated_at": "2026-10-14T10:00:00Z",
               "movies": {"all_time": {"total": 3, "plays": 5,
                                       "genres": [{"genre": "Drama", "percent": 66.7}]}},
               "tv": {"all_time": {"total": 10, "plays": 12, "genres": []}}}
    check(history.record(payload), "record returns True")
    check(history.recorded_today(), "recorded_today after record")
    series = history.totals_series()
    equal(len(series), 1, "one snapshot in the series")
    equal((series[0]["movies_total"], series[0]["episodes_plays"]), (3, 12), "totals read back")
    genres = history.genre_history("movies")
    equal([g["genre"] for g in genres["genres"]], ["Drama"], "genre history read back")


def test_backup_names():
    """<summary>path_of answers only its own file pattern, so no path from a request escapes the folder.</summary>"""
    print("running test_backup_names")
    remove_db()
    playlog.connect().close()
    made = backup.create("manual", "test")
    check(made is not None and os.path.isfile(os.path.join(backup.folder(), made["name"])),
          "a backup was written")
    check(backup.path_of(made["name"]) is not None, "its own name is found")
    for bad in ("../history.db", "history.db", made["name"] + "/../history.db",
                "jellystat-20261399-256199-before-x.db", "", None,
                "jellystat-20260101-000000-before-x.db.partial"):
        check(backup.path_of(bad) is None, "refused %r" % (bad,))
    check(not any(name.endswith(".partial") for name in os.listdir(backup.folder())),
          "no partial file left behind")


def main():
    """
    <summary>
    Run every test and return the exit code: 0 when all passed, 1 otherwise.
    </summary>
    """
    try:
        test_migrations_on_old_schema()
        test_repair_runs_once()
        test_concurrent_first_connect()
        test_restored_file_is_migrated()
        test_history_round_trip()
        test_backup_names()
    finally:
        kodi_stubs.cleanup()
    if FAILURES:
        print("test_history: %d failure(s)" % len(FAILURES))
        return 1
    print("test_history: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
