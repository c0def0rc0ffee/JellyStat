#!/usr/bin/env python3
"""
<summary>
Tests for the stats build: webdata.build() with the Jellyfin calls stubbed,
the mirror fallback when the server is unreachable, and the strptime fallback
Kodi's embedded Python needs.
</summary>
<remarks>
No Kodi and no Jellyfin. main.get_watched and friends are replaced with
functions answering from a small invented library, and the play log is given
one real session so the calendar's two eras both run. The strptime check
replaces the datetime class timeparse uses with one whose strptime raises the
exact TypeError a torn down interpreter leaves behind, and the build must
still succeed through the time.strptime path.

Usage:
    python3 tests/test_webdata.py
</remarks>
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kodi_stubs  # noqa: E402

kodi_stubs.install()

import main as core  # noqa: E402
import playlog  # noqa: E402
import timeparse  # noqa: E402
import webdata  # noqa: E402

FAILURES = []


def check(condition, message):
    """<summary>Record a failure without stopping.</summary>"""
    if not condition:
        FAILURES.append(message)
        print("FAIL: %s" % message)


def equal(actual, expected, what):
    """<summary>check() for equality, printing both sides on failure.</summary>"""
    check(actual == expected, "%s: expected %r, got %r" % (what, expected, actual))


# ---- an invented library --------------------------------------------------

def stamp(days_ago, hour=12):
    """
    <summary>
    A Jellyfin LastPlayedDate so many days ago, in UTC.
    </summary>
    <param name="days_ago">Days back from today.</param>
    <param name="hour">UTC hour; midday by default so the local date is the
    same whatever zone the test box is in.</param>
    <returns>The stamp text.</returns>
    """
    when = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days_ago)
    return when.replace(hour=hour, minute=0, second=0).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


MOVIES = [
    {"Id": "a" * 32, "Name": "First Film", "ProductionYear": 2001, "Genres": ["Drama"],
     "CommunityRating": 7.5, "UserData": {"PlayCount": 2, "LastPlayedDate": stamp(1),
                                          "IsFavorite": True}},
    {"Id": "b" * 32, "Name": "Second Film", "ProductionYear": 2010, "Genres": ["Sci-Fi"],
     "CommunityRating": 6.0, "UserData": {"PlayCount": 1, "LastPlayedDate": stamp(40)}},
]
SERIES = [{"Id": "c" * 32, "Name": "A Show", "Genres": ["Comedy"], "UserData": {}}]
EPISODES = [
    {"Id": "d" * 32, "Name": "Pilot", "SeriesId": "c" * 32, "SeriesName": "A Show",
     "ParentIndexNumber": 1, "IndexNumber": 1, "CommunityRating": 8.0,
     "UserData": {"PlayCount": 1, "LastPlayedDate": stamp(3)}},
]


def stub_jellyfin(reachable=True):
    """
    <summary>
    Point the stats build at the invented library, or at a server that is down.
    </summary>
    <param name="reachable">False makes every call raise JellyStatError.</param>
    """
    def down(*args, **kwargs):
        """<summary>What an unreachable server looks like to the build.</summary>"""
        raise core.JellyStatError("Could not reach the Jellyfin server at test")

    if not reachable:
        core.get_credentials = down
        return
    core.get_credentials = lambda: {"base": "http://test", "token": "t", "user_id": "u"}
    core.get_watched = lambda creds, kind: list(MOVIES if kind == "Movie" else EPISODES)
    core.get_series = lambda creds: list(SERIES)
    core.get_series_genres = lambda creds, series=None: {s["Id"]: s["Genres"] for s in SERIES}


def log_one_session():
    """
    <summary>
    One real sitting in the play log, so coverage_start is set.
    </summary>
    <remarks>
    The same episode, on the same local day as Jellyfin's last played date
    for it, so the calendar's dedup has something to drop: that day must
    count the sitting once, not the sitting and the server's date both.
    </remarks>
    """
    played = core.parse_played_date(EPISODES[0]["UserData"]["LastPlayedDate"])
    started = played + webdata._utc_offset() + timedelta(hours=1)
    playlog.record({"started_at": started, "ended_at": started + timedelta(minutes=50),
                    "media": "episode", "title": "Pilot", "show": "A Show",
                    "runtime_seconds": 3000, "watched_seconds": 3000,
                    "position": 3000, "samples": 10})


# ---- tests ----------------------------------------------------------------

def test_build():
    """<summary>The full payload is built from the invented library and the log.</summary>"""
    print("running test_build")
    stub_jellyfin()
    log_one_session()
    data = webdata.build(30)
    equal(data["source"], "jellyfin", "built from the server")
    equal(data["totals"]["movies"], 2, "films counted")
    equal(data["totals"]["episodes"], 1, "episodes counted")
    equal(data["totals"]["plays"], 4, "plays summed")
    equal(len(data["calendar"]), webdata.CALENDAR_DAYS, "a slot for every day")
    check(data["playlog"]["since"] is not None, "coverage start set by the logged session")
    watched_days = [day for day in data["calendar"] if day["movies"] or day["episodes"]]
    equal(len(watched_days), 3, "three days with something watched")
    equal(max(day["episodes"] for day in data["calendar"]), 1,
          "the logged sitting and the server's date for it count once, not twice")
    equal(data["streaks"]["longest"] >= 1, True, "a streak of at least one day")
    equal(data["top_shows"][0]["show"], "A Show", "top show named")
    equal(len(data["favourites"]), 1, "one favourite film")
    equal(data["mirror"]["items"], 3, "mirror synced with every watched item")


def test_mirror_fallback():
    """<summary>With the server down the build comes from the mirror rather than failing.</summary>"""
    print("running test_mirror_fallback")
    stub_jellyfin(reachable=False)
    data = webdata.build(30)
    equal(data["source"], "mirror", "served from the mirror")
    equal(data["totals"]["movies"], 2, "mirror holds the films")
    stub_jellyfin()


def test_strptime_fallback():
    """<summary>The build survives datetime.strptime being torn down under it.</summary>"""
    print("running test_strptime_fallback")

    class TornDown(datetime):
        """<summary>What datetime looks like after Kodi has cleared _strptime.</summary>"""

        @classmethod
        def strptime(cls, value, fmt):
            """<summary>The exact failure seen on a real box.</summary>"""
            raise TypeError("'NoneType' object is not callable")

    saved = timeparse.datetime
    timeparse.datetime = TornDown
    try:
        parsed = timeparse.strptime("2026-10-14T20:31:12", "%Y-%m-%dT%H:%M:%S")
        equal((parsed.year, parsed.month, parsed.day, parsed.hour, parsed.minute, parsed.second),
              (2026, 10, 14, 20, 31, 12), "helper falls back to time.strptime")
        try:
            timeparse.strptime("not a date", "%Y-%m-%d")
            check(False, "a bad value still raises ValueError on the fallback path")
        except ValueError:
            pass
        stub_jellyfin()
        data = webdata.build(30)
        equal(data["totals"]["movies"], 2, "the stats still build with strptime torn down")
        check(data["playlog"]["since"] is not None, "the calendar's log era still ran")
    finally:
        timeparse.datetime = saved


def main():
    """
    <summary>
    Run every test and return the exit code: 0 when all passed, 1 otherwise.
    </summary>
    """
    try:
        test_build()
        test_mirror_fallback()
        test_strptime_fallback()
    finally:
        kodi_stubs.cleanup()
    if FAILURES:
        print("test_webdata: %d failure(s)" % len(FAILURES))
        return 1
    print("test_webdata: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
