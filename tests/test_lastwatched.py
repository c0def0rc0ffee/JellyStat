#!/usr/bin/env python3
"""
<summary>
Tests for the last watched rule and for bulk re-mark days being set aside.
</summary>
<remarks>
The rule: a title's last watched date is the newest completed, live play log
sitting for it, and Jellyfin's LastPlayedDate (made local) only when the log
has none. Checked through library.LastWatched directly and through every
reader that shows a date: search, the library rows, the item pages, the TV
stats, the dashboard's recent list, the calendar's fallback and the month
tile the Home window properties are built from.

The bulk half seeds a day of change detected rows far larger than the
others, checks it is reported, sets it aside, and checks the rows are still
in the file but out of every reader, then restores them.

No Kodi and no Jellyfin: kodi_stubs puts the profile in a temporary folder.

Usage:
    python3 tests/test_lastwatched.py
</remarks>
"""

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kodi_stubs  # noqa: E402

kodi_stubs.install()

import library  # noqa: E402
import main as core  # noqa: E402
import playlog  # noqa: E402
import screentime  # noqa: E402
import webdata  # noqa: E402

FAILURES = []

BULK_DAY = "2025-12-15"
BULK_STAMP = BULK_DAY + "T20:00:00"


def check(condition, message):
    """<summary>Record a failure without stopping.</summary>"""
    if not condition:
        FAILURES.append(message)
        print("FAIL: %s" % message)


def equal(actual, expected, what):
    """<summary>check() for equality, printing both sides on failure.</summary>"""
    check(actual == expected, "%s: expected %r, got %r" % (what, expected, actual))


def jf_item(item_id, name, year, played_utc, series=None, season=None,
            episode=None, favourite=False):
    """
    <summary>
    A Jellyfin item as the mirror expects it.
    </summary>
    <param name="played_utc">A UTC YYYY-MM-DDTHH:MM:SS stamp or None.</param>
    """
    item = {"Id": item_id, "Name": name, "ProductionYear": year,
            "RunTimeTicks": 90 * 60 * 10000000,
            "UserData": {"PlayCount": 1, "IsFavorite": favourite,
                         "LastPlayedDate": (played_utc + ".0000000Z"
                                            if played_utc else None)}}
    if series:
        item["SeriesId"] = "s-" + series.lower()
        item["SeriesName"] = series
        item["ParentIndexNumber"] = season
        item["IndexNumber"] = episode
    else:
        item["Genres"] = ["Drama"]
    return item


MOVIES = [
    jf_item("m1", "Alpha", 2010, BULK_STAMP, favourite=True),
    jf_item("m2", "Beta", 2012, "2026-10-01T19:30:00"),
    jf_item("m3", "Gamma", 2015, BULK_STAMP),
]
SERIES = [{"Id": "s-show", "Name": "Show", "Genres": ["Comedy"], "UserData": {}}]
EPISODES = [
    jf_item("e1", "One", 2020, "2026-09-20T21:00:00", "Show", 1, 1),
    jf_item("e2", "Two", 2020, BULK_STAMP, "Show", 1, 2),
]


def add_play(title, day, show=None, year=None, source="trakt", completed=1,
             hour=20):
    """
    <summary>
    One play log row written straight into the table.
    </summary>
    <remarks>
    Bypasses playlog.record so a row can carry any source and a completed
    flag of 0, which record() decides for itself.
    </remarks>
    """
    stamp = "%sT%02d:00:00" % (day, hour)
    when = datetime.strptime(stamp, playlog.STAMP)
    connection = playlog.connect()
    with connection:
        connection.execute(
            "INSERT INTO plays (started_at, ended_at, day, hour, weekday, "
            "media, title, show, season, episode, year, runtime_seconds, "
            "watched_seconds, completed, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, 5400, 5400, ?, ?)",
            (stamp, stamp, day, hour, when.weekday(),
             "episode" if show else "movie", title, show, year, completed,
             source))
    connection.close()
    return stamp


def local(utc_stamp):
    """<summary>What the rule's fallback should produce for a UTC stamp.</summary>"""
    return library.local_stamp(utc_stamp)


def seed_mirror():
    """<summary>Sync the invented library into the mirror once.</summary>"""
    offset = webdata._utc_offset()
    result = library.sync(MOVIES, EPISODES, {"s-show": ["Comedy"]}, offset,
                          SERIES)
    equal(result["added"], 6, "mirror seeded")


# ---- tests ----------------------------------------------------------------

def test_rule_and_fallback():
    """<summary>The log wins when it has a completed sitting; Jellyfin's date otherwise.</summary>"""
    print("running test_rule_and_fallback")
    seed_mirror()
    logged = add_play("Alpha", "2026-09-10", year=2010)
    add_play("Gamma", "2026-09-11", year=2015, completed=0)   # not finished
    add_play("Beta", "2026-09-12", year=1999)                 # a remake
    dates = library.LastWatched()

    equal(dates.title(BULK_STAMP, None, "Alpha", 2010), logged,
          "a completed sitting beats Jellyfin's date")
    equal(dates.title(BULK_STAMP, None, "Gamma", 2015), local(BULK_STAMP),
          "an unfinished sitting does not count, so Jellyfin's date stands")
    equal(dates.title("2026-10-01T19:30:00", None, "Beta", 2012),
          local("2026-10-01T19:30:00"),
          "a sitting of a different year's film is not this film's")
    equal(dates.title(None, None, "Nobody", 2000), None,
          "no sitting and no Jellyfin date gives None")
    equal(dates.title(BULK_STAMP, None, "ALPHA", 2010), logged,
          "titles match regardless of case")

    hits = library.search("alpha")
    equal(hits["movies"][0]["last_played"], logged, "search shows the log's date")
    rows = {row["name"]: row for row in library.browse_rows("movies")}
    equal(rows["Alpha"]["last_played"], logged, "library rows show the log's date")
    equal(rows["Gamma"]["last_played"], local(BULK_STAMP),
          "library rows fall back to Jellyfin's date, made local")
    detail = library.movie_detail("m1")
    equal(detail["last_played"], logged, "the film page shows the log's date")
    equal(detail["jellyfin_last_played"], local(BULK_STAMP),
          "the film page keeps Jellyfin's own date alongside")
    check(detail["sittings"] and not detail["sittings"][0]["set_aside"],
          "sittings carry the set aside flag, off by default")


def test_shows():
    """<summary>A show takes its newest logged episode, else its newest Jellyfin date.</summary>"""
    print("running test_shows")
    dates = library.LastWatched()
    equal(dates.show(BULK_STAMP, "Show"), local(BULK_STAMP),
          "no episode sittings yet, so the show falls back")
    logged = add_play("Two", "2026-08-01", show="Show", year=2020)
    dates = library.LastWatched()
    equal(dates.show(BULK_STAMP, "Show"), logged, "the show takes the logged episode")
    equal(dates.title(BULK_STAMP, "Show", "Two"), logged,
          "the episode itself takes its sitting")
    equal(dates.title("2026-09-20T21:00:00", "Show", "One"),
          local("2026-09-20T21:00:00"),
          "a sibling episode with no sitting keeps Jellyfin's date")
    detail = library.show_detail("Show")
    equal(detail["last_played"], logged, "the show page follows the rule")
    equal(detail["jellyfin_last_played"], local("2026-09-20T21:00:00"),
          "the show page keeps Jellyfin's newest episode date alongside")
    by_name = {e["name"]: e["last_played"] for e in detail["episodes"]}
    equal(by_name["Two"], logged, "the episode list follows the rule")
    stats = library.tv_stats()
    equal(stats["top_shows"][0]["last_played"], logged, "TV stats follow the rule")
    tv_rows = {row["name"]: row for row in library.browse_rows("tv")}
    equal(tv_rows["Show"]["last_played"], logged, "the TV library rows follow the rule")
    hits = library.search("show")
    equal(hits["shows"][0]["last_played"], logged, "show search follows the rule")


def test_bulk_detection():
    """<summary>A day of Jellyfin detected rows far above the others is reported; set aside and restore change nothing but the flag.</summary>"""
    print("running test_bulk_detection")
    # Two ordinary days of change detection, then a bulk one.
    add_play("Beta", "2026-10-01", year=2012, source="jellyfin-sync")
    add_play("One", "2026-09-20", show="Show", year=2020, source="jellyfin-sync")
    titles = ["Bulk %d" % n for n in range(playlog.BULK_MIN_ROWS + 5)]
    for title in titles:
        add_play(title, BULK_DAY, year=2001, source="jellyfin-sync")
    add_play("Alpha", BULK_DAY, year=2010, source="trakt")   # real, same day

    days = playlog.bulk_days()
    equal([d["day"] for d in days], [BULK_DAY], "only the bulk day is reported")
    equal(days[0]["rows"], len(titles), "Trakt rows on that day are not counted")
    equal(days[0]["set_aside"], 0, "nothing set aside yet")
    before = playlog.summary()["sessions"]
    total_rows = playlog._rows("SELECT COUNT(*) FROM plays")[0][0]

    changed = playlog.set_aside(BULK_DAY)
    equal(changed, len(titles), "every Jellyfin detected row on the day is set aside")
    equal(playlog._rows("SELECT COUNT(*) FROM plays")[0][0], total_rows,
          "nothing was deleted")
    equal(playlog.summary()["sessions"], before - len(titles),
          "the summary leaves set aside rows out")
    check(not any(e["day"] == BULK_DAY and e["source"] == "jellyfin-sync"
                  for e in playlog.events()),
          "events() leaves set aside rows out")
    check(any(e["day"] == BULK_DAY and e["source"] == "trakt"
              for e in playlog.events()),
          "the Trakt row on the same day is untouched")
    days = playlog.bulk_days()
    equal(days[0]["set_aside"], len(titles), "the day reports its rows as set aside")
    keys = playlog.set_aside_keys()
    check((playlog.dedup_key(None, titles[0]), BULK_DAY) in keys,
          "set aside keys name the title and day")
    dates = library.LastWatched()
    equal(dates.title(BULK_STAMP, None, titles[0], 2001), local(BULK_STAMP),
          "a set aside row no longer counts as a sitting")
    equal(playlog.set_aside(BULK_DAY), 0, "setting aside twice changes nothing")

    equal(playlog.set_aside(BULK_DAY, restore=True), len(titles), "restored")
    equal(playlog.summary()["sessions"], before, "the summary has them back")
    equal(playlog.bulk_days()[0]["set_aside"], 0, "the day reports nothing set aside")
    try:
        playlog.set_aside("yesterday")
        check(False, "a bad day is refused")
    except ValueError:
        pass
    # Put the bulk day back aside for the calendar and tile checks below.
    playlog.set_aside(BULK_DAY)


def test_bulk_thresholds():
    """<summary>The size and factor tests both have to pass.</summary>"""
    print("running test_bulk_thresholds")
    equal(playlog._median([]), 0, "median of nothing")
    equal(playlog._median([3, 1, 2]), 2, "odd median")
    equal(playlog._median([4, 1, 2, 3]), 2.5, "even median")
    # A busy but ordinary day: well under BULK_FACTOR times the others.
    for n in range(playlog.BULK_MIN_ROWS):
        add_play("Busy %d" % n, "2026-10-10", year=2002, source="jellyfin-sync")
    for day in ("2026-10-11", "2026-10-12", "2026-10-13"):
        for n in range(playlog.BULK_MIN_ROWS - 2):
            add_play("Usual %d" % n, day, year=2003, source="jellyfin-sync")
    reported = [d["day"] for d in playlog.bulk_days()]
    check("2026-10-10" not in reported, "a busy day among busy days is not a bulk re-mark")


def test_calendar_and_recent():
    """<summary>The dashboard's fallback skips set aside dates, and recent follows the rule.</summary>"""
    print("running test_calendar_and_recent")
    # Whole seconds: the live offset carries microseconds, and a stamp built
    # from it would land a second short of the minute being checked.
    offset = timedelta(seconds=round(webdata._utc_offset().total_seconds()))
    today = datetime.now()
    bulk_local = (today - timedelta(days=3)).replace(hour=20, minute=0, second=0,
                                                    microsecond=0)
    bulk_utc = (bulk_local - offset).strftime("%Y-%m-%dT%H:%M:%S")
    day = bulk_local.strftime("%Y-%m-%d")
    items = [jf_item("x%d" % n, "Recent %d" % n, 2004, bulk_utc) for n in range(3)]
    for item in items:
        add_play(item["Name"], day, year=2004, source="jellyfin-sync")
    playlog.set_aside(day)
    sessions = playlog.events()
    aside = playlog.set_aside_keys()
    calendar = webdata._calendar(items, [], offset, sessions=sessions,
                                 coverage_start=playlog.since(), set_aside=aside)
    slot = [d for d in calendar if d["date"] == day][0]
    equal(slot["movies"], 0, "a set aside day is not counted from Jellyfin's dates either")
    calendar = webdata._calendar(items, [], offset, sessions=sessions,
                                 coverage_start=playlog.since())
    slot = [d for d in calendar if d["date"] == day][0]
    equal(slot["movies"], 3, "without the set aside keys the fallback would count them")
    hours, weekdays = webdata._clock_and_week(items, [], offset, sessions=sessions,
                                              coverage_start=playlog.since(),
                                              set_aside=aside)
    equal(sum(h["movies"] for h in hours), sum(1 for e in sessions if e["kind"] == "movies"
                                               and e["hour"] >= 0),
          "the clock counts live sessions only, not the set aside dates")

    dates = library.LastWatched()
    logged = add_play("Recent 0", (today - timedelta(days=1)).strftime("%Y-%m-%d"),
                      year=2004)
    dates = library.LastWatched()
    rows = webdata._recent(items, [], {}, offset, dates=dates)
    equal(rows[0]["name"], "Recent 0", "the logged sitting puts the title first")
    equal(rows[0]["played"], logged[:16].replace("T", " "), "recent shows the log's date")
    equal(rows[1]["played"], bulk_local.strftime("%Y-%m-%d %H:%M"),
          "the others fall back to Jellyfin's date made local")
    favourite = webdata._item_row(MOVIES[0], offset, dates=dates)
    equal(favourite["played"][:10], "2026-09-10", "favourites follow the rule")
    shows = webdata._top_shows(EPISODES, {"s-show": ["Comedy"]}, offset, dates=dates)
    # Show's newest logged episode is the change detected "One" on the
    # 20th September, added by test_bulk_detection; "Two" in August is older.
    equal(shows[0]["last_played"], "2026-09-20", "top shows follow the rule")


def test_month_tile():
    """<summary>The month tile behind the Home properties counts by the rule.</summary>"""
    print("running test_month_tile")
    # The tile counts everything last watched since the first of the month,
    # with no upper bound (as it always has), so a "now" in October sees:
    # Beta, Jellyfin 1st October 2026, and nothing else. Alpha's Jellyfin
    # date is December 2025 but its logged sitting is 10th September, Gamma
    # has only an unfinished sitting so December stands, and both episodes
    # resolve to September and August.
    october = screentime.tiles(datetime(2026, 10, 15, 12, 0, 0))
    equal(october["this_month"]["movies"], 1, "Beta is October's only film")
    equal(october["this_month"]["episodes"], 0, "no episode in October")
    equal(october["this_month"]["minutes"], 90, "Beta's runtime counted once")
    september = screentime.tiles(datetime(2026, 9, 15, 12, 0, 0))
    equal(september["this_month"]["movies"], 2,
          "from September: Alpha by its sitting and Beta, not Gamma")
    equal(september["this_month"]["episodes"], 1, "episode One from September")
    equal(september["this_month"]["shows"], 1, "one show")
    equal(september["this_month"]["minutes"], 270, "three runtimes")
    check(september["all_time"]["movies"] >= 3, "all time still counts the whole mirror")


def main():
    """
    <summary>
    Run every test and return the exit code: 0 when all passed, 1 otherwise.
    </summary>
    """
    try:
        test_rule_and_fallback()
        test_shows()
        test_bulk_detection()
        test_bulk_thresholds()
        test_calendar_and_recent()
        test_month_tile()
    finally:
        kodi_stubs.cleanup()
    if FAILURES:
        print("test_lastwatched: %d failure(s)" % len(FAILURES))
        return 1
    print("test_lastwatched: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
