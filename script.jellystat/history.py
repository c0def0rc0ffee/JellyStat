# -*- coding: utf-8 -*-
"""
<summary>
Daily snapshot history in a local SQLite file.
</summary>
<remarks>
The dashboard's live numbers come straight from Jellyfin, but Jellyfin keeps
only each item's *most recent* play date, so anything older than the last
watch of an item is lost to it. Storing one snapshot per day gives the
dashboard something Jellyfin cannot answer on its own: how the totals and the
genre mix have moved over weeks and months.

One row per local calendar date, replaced if the day is recorded again. A
row is the whole snapshot, so it costs roughly 10 KB a day on a mid-sized
library; RETAIN_DAYS caps the file at a few years of that rather than letting
it grow for the life of the box.
</remarks>
"""

import json
import os
import sqlite3
from datetime import datetime

import xbmc
import xbmcvfs

ADDON_ID = "script.jellystat"
DATA_DIR = "special://profile/addon_data/%s/" % ADDON_ID
DB_NAME = "history.db"

# Three years is far more than any chart draws, and about 10 MB.
RETAIN_DAYS = 1095

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    day             TEXT PRIMARY KEY,
    taken_at        TEXT NOT NULL,
    movies_total    INTEGER NOT NULL,
    movies_plays    INTEGER NOT NULL,
    episodes_total  INTEGER NOT NULL,
    episodes_plays  INTEGER NOT NULL,
    payload         TEXT NOT NULL
);
"""


def log(message, level=xbmc.LOGINFO):
    """
    <summary>
    Write one line to Kodi's log under the JellyStat tag.
    </summary>
    <param name="message">Text to log.</param>
    <param name="level">Kodi log level, info by default.</param>
    """
    xbmc.log("[JellyStat] %s" % message, level)


def db_path():
    """
    <summary>
    Filesystem path to the history database, creating the folder.
    </summary>
    """
    folder = xbmcvfs.translatePath(DATA_DIR)
    if not os.path.isdir(folder):
        try:
            os.makedirs(folder)
        except OSError:
            pass
    return os.path.join(folder, DB_NAME)


def connect():
    """
    <summary>
    Open the history database and make sure its schema exists.
    </summary>
    <returns>An sqlite3 connection.</returns>
    """
    connection = sqlite3.connect(db_path(), timeout=10)
    connection.execute(SCHEMA)
    return connection


def record(payload, when=None):
    """
    <summary>
    Store today's snapshot, replacing an earlier one for the same date.
    </summary>
    <remarks>
    `payload` is the stats_sender snapshot dict. Errors are logged, never
    raised: losing a day of history must not break a page load or a send.
    </remarks>
    """
    when = when or datetime.now()
    try:
        movies = payload.get("movies") or {}
        tv = payload.get("tv") or {}
        all_movies = movies.get("all_time") or {}
        all_tv = tv.get("all_time") or {}
        connection = connect()
        with connection:
            connection.execute(
                "REPLACE INTO snapshots (day, taken_at, movies_total, "
                "movies_plays, episodes_total, episodes_plays, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (when.strftime("%Y-%m-%d"),
                 payload.get("generated_at") or when.strftime(
                     "%Y-%m-%dT%H:%M:%SZ"),
                 int(all_movies.get("total") or 0),
                 int(all_movies.get("plays") or 0),
                 int(all_tv.get("total") or 0),
                 int(all_tv.get("plays") or 0),
                 json.dumps(payload)))
            connection.execute(
                "DELETE FROM snapshots WHERE day NOT IN "
                "(SELECT day FROM snapshots ORDER BY day DESC LIMIT ?)",
                (RETAIN_DAYS,))
        connection.close()
        return True
    except Exception as err:  # sqlite locked, read-only profile, bad payload
        log("Could not record history: %s" % err, xbmc.LOGWARNING)
        return False


def recorded_today(when=None):
    """
    <summary>
    Whether a snapshot for the given day, today by default, is already stored.
    </summary>
    <param name="when">The day to check; now when omitted.</param>
    <returns>True when a row exists; False otherwise, including on any database error.</returns>
    """
    when = when or datetime.now()
    try:
        connection = connect()
        row = connection.execute(
            "SELECT 1 FROM snapshots WHERE day = ?",
            (when.strftime("%Y-%m-%d"),)).fetchone()
        connection.close()
        return row is not None
    except Exception:
        return False


def totals_series(limit=365):
    """
    <summary>
    Recent snapshots oldest-first: the raw material for the trend chart.
    </summary>
    <remarks>
    Also carries per-day deltas (how many more items were watched than the
    day before), which is the number a reader actually wants to see. The
    first row has no previous day, so its deltas are null rather than a
    misleading zero.
    </remarks>
    """
    try:
        connection = connect()
        rows = connection.execute(
            "SELECT day, movies_total, movies_plays, episodes_total, "
            "episodes_plays FROM snapshots ORDER BY day DESC LIMIT ?",
            (limit,)).fetchall()
        connection.close()
    except Exception as err:
        log("Could not read history: %s" % err, xbmc.LOGWARNING)
        return []
    rows.reverse()
    series = []
    previous = None
    for day, movies, movie_plays, episodes, episode_plays in rows:
        entry = {
            "day": day,
            "movies_total": movies,
            "movies_plays": movie_plays,
            "episodes_total": episodes,
            "episodes_plays": episode_plays,
            "movies_delta": None,
            "episodes_delta": None,
        }
        if previous is not None:
            entry["movies_delta"] = max(movies - previous[0], 0)
            entry["episodes_delta"] = max(episodes - previous[1], 0)
        previous = (movies, episodes)
        series.append(entry)
    return series


def genre_history(media="movies", window="all_time", limit=180, top=8):
    """
    <summary>
    Genre share over time -> {days: [...], genres: [{genre, percent[]}]}.
    </summary>
    <remarks>
    Only the genres that are largest in the most recent snapshot are kept
    (`top`), because a chart of forty thin lines says nothing. A genre that
    is missing from an older snapshot reads as 0% there, which is what it
    was.
    </remarks>
    """
    try:
        connection = connect()
        rows = connection.execute(
            "SELECT day, payload FROM snapshots ORDER BY day DESC LIMIT ?",
            (limit,)).fetchall()
        connection.close()
    except Exception as err:
        log("Could not read genre history: %s" % err, xbmc.LOGWARNING)
        return {"days": [], "genres": []}
    rows.reverse()
    days = []
    per_day = []
    for day, raw in rows:
        try:
            block = ((json.loads(raw).get(media) or {}).get(window)
                     or {}).get("genres") or []
        except ValueError:
            continue
        days.append(day)
        per_day.append({entry["genre"]: entry.get("percent") or 0.0
                        for entry in block})
    if not per_day:
        return {"days": [], "genres": []}
    latest = per_day[-1]
    names = sorted(latest, key=lambda name: -latest[name])[:top]
    return {
        "days": days,
        "genres": [{"genre": name,
                    "percent": [day.get(name, 0.0) for day in per_day]}
                   for name in names],
    }
