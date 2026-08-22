# -*- coding: utf-8 -*-
"""Headline tiles and the screen-time panel.

Two different questions, answered from two different places, and the split
matters because getting it wrong would invent numbers:

- **Tiles** ("this month" and "all time") count titles and estimate hours
  from each item's runtime times how often it was played. That reaches back
  over the whole library, including everything watched before this addon
  existed, but it is an estimate: it assumes a play means the whole thing.

- **Screen time** comes from the play log's real sittings, so it only covers
  the period the log reaches back to. Every figure carries the window it was
  measured over and a comparison against the window before it, because "6h
  3m a day" only means something next to what it used to be.

  Not all of it is measured, though, and the panel says which is which. A
  sitting recorded by this box has a real duration; one imported from Trakt
  or a similar service has only the item's runtime, because those services
  record *that* something was played and never for how long. Both are
  reported, separately, rather than adding an assumption to a measurement
  and presenting the sum as fact.
"""

from datetime import datetime, timedelta

import xbmc

import library
import playlog

# Where the day is cut into named parts, matching how people describe
# viewing rather than any clock convention.
DAYPARTS = (("Morning", 5, 12), ("Afternoon", 12, 17),
            ("Evening", 17, 22), ("Night", 22, 5))

# A day is not 24 usable hours. The share of *waking* time is the number
# worth showing, and this is the assumption behind it.
WAKING_HOURS = 16.0


def log(message, level=xbmc.LOGINFO):
    xbmc.log("[JellyStat] %s" % message, level)


def _connect():
    return library.connect()


# ---------------------------------------------------------------------------
# Tiles
# ---------------------------------------------------------------------------

def _tile_row(connection, since=None):
    """Counts and estimated minutes for everything, or since a date."""
    # Series rows exist to hold show-level ratings; they are not watched
    # items and must not be counted as titles or minutes here.
    where = ("WHERE media IN ('movie', 'episode')"
             + (" AND last_played >= ?" if since else ""))
    params = (since,) if since else ()
    row = connection.execute(
        "SELECT "
        "SUM(CASE WHEN media='episode' THEN 1 ELSE 0 END), "
        "COUNT(DISTINCT CASE WHEN media='episode' THEN series_name END), "
        "SUM(CASE WHEN media='movie' THEN 1 ELSE 0 END), "
        "SUM(play_count), "
        # Runtime times plays: what the item is worth, however often watched.
        "SUM(COALESCE(runtime_minutes, 0) * MAX(play_count, 1)), "
        "SUM(CASE WHEN user_rating IS NOT NULL THEN 1 ELSE 0 END) "
        "FROM items %s" % where, params).fetchone()
    return {
        "episodes": row[0] or 0,
        "shows": row[1] or 0,
        "movies": row[2] or 0,
        "plays": row[3] or 0,
        "minutes": row[4] or 0,
        "ratings": row[5] or 0,
    }


def tiles(now=None):
    """The two headline blocks: this month, and all time."""
    now = now or datetime.now()
    month_start = now.replace(day=1, hour=0, minute=0,
                              second=0).strftime("%Y-%m-%d")
    connection = _connect()
    try:
        this_month = _tile_row(connection, month_start)
        all_time = _tile_row(connection)
    finally:
        connection.close()
    return {
        "month_label": now.strftime("%B"),
        "this_month": this_month,
        "all_time": all_time,
    }


# ---------------------------------------------------------------------------
# Screen time
# ---------------------------------------------------------------------------

def _daypart(hour):
    for name, start, end in DAYPARTS:
        if start < end:
            if start <= hour < end:
                return name
        elif hour >= start or hour < end:      # the wrap-around one
            return name
    return "Night"


def _window(connection, start_day, end_day):
    """Measured minutes in a date range, split the ways the panel shows."""
    rows = connection.execute(
        "SELECT day, hour, media, watched_seconds, "
        "COALESCE(assumed, 0) FROM plays "
        "WHERE day >= ? AND day <= ?", (start_day, end_day)).fetchall()
    per_day = {}
    per_day_assumed = {}
    dayparts = {name: 0 for name, _, _ in DAYPARTS}
    movies = shows = measured = assumed = 0
    for day, hour, media, seconds, is_assumed in rows:
        minutes = (seconds or 0) / 60.0
        per_day[day] = per_day.get(day, 0.0) + minutes
        if is_assumed:
            assumed += minutes
            per_day_assumed[day] = per_day_assumed.get(day, 0.0) + minutes
        else:
            measured += minutes
        if hour is not None and hour >= 0:
            dayparts[_daypart(hour)] += 1
        if media == "movie":
            movies += minutes
        elif media == "episode":
            shows += minutes
    total = sum(per_day.values())
    return {"per_day": per_day, "per_day_assumed": per_day_assumed,
            "dayparts": dayparts, "movies": movies, "shows": shows,
            "total": total, "measured": measured, "assumed": assumed,
            "sessions": len(rows)}


def _days(start, count):
    return [(start + timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(count)]


def screen_time(days=7, now=None):
    """The panel: daily bars, average, dayparts, totals, all with deltas.

    Compared against the immediately preceding window of the same length,
    which is what makes a number like "-31m" meaningful.
    """
    now = now or datetime.now()
    today = now.date()
    start = today - timedelta(days=days - 1)
    previous_start = start - timedelta(days=days)
    previous_end = start - timedelta(days=1)

    connection = _connect()
    try:
        current = _window(connection, start.strftime("%Y-%m-%d"),
                          today.strftime("%Y-%m-%d"))
        earlier = _window(connection, previous_start.strftime("%Y-%m-%d"),
                          previous_end.strftime("%Y-%m-%d"))
        covered = playlog.since()
    finally:
        connection.close()

    labels = _days(start, days)
    breakdown = [{"date": day,
                  "weekday": datetime.strptime(day, "%Y-%m-%d").weekday(),
                  "minutes": round(current["per_day"].get(day, 0.0)),
                  "assumed_minutes": round(
                      current["per_day_assumed"].get(day, 0.0)),
                  "today": day == today.strftime("%Y-%m-%d")}
                 for day in labels]

    # The average deliberately divides by elapsed days, not by the window:
    # a week that is three days old should not report a third of its rate.
    elapsed = min(days, (today - start).days + 1)
    average = current["total"] / elapsed if elapsed else 0.0
    previous_average = earlier["total"] / days if days else 0.0

    return {
        "days": days,
        "from": start.strftime("%Y-%m-%d"),
        "to": today.strftime("%Y-%m-%d"),
        "covered_from": covered,
        "measured": bool(current["sessions"] or earlier["sessions"]),
        "breakdown": breakdown,
        "daily_average": {"minutes": round(average),
                          "delta": round(average - previous_average)},
        "total": {"minutes": round(current["total"]),
                  "delta": round(current["total"] - earlier["total"])},
        # Reported alongside every figure above, never folded into them.
        "measured_minutes": round(current["measured"]),
        "assumed_minutes": round(current["assumed"]),
        "waking": {"percent": round(100.0 * (average / 60.0) / WAKING_HOURS,
                                    1),
                   "delta": round(100.0 * ((average - previous_average) / 60.0)
                                  / WAKING_HOURS, 1)},
        "movies": {"minutes": round(current["movies"]),
                   "delta": round(current["movies"] - earlier["movies"])},
        "shows": {"minutes": round(current["shows"]),
                  "delta": round(current["shows"] - earlier["shows"])},
        "dayparts": [{"name": name, "sessions": current["dayparts"][name]}
                     for name, _, _ in DAYPARTS],
    }
