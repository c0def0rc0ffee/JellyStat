#!/usr/bin/env python3
"""
<summary>
Tests for writing the play log's last watched dates back to Jellyfin.
</summary>
<remarks>
The rule under test: every present item whose newest completed live sitting
differs from the date the mirror holds for Jellyfin is a target; the whole
UserData object is read, its LastPlayedDate replaced and written back, the
write is read back to confirm, and the mirror is updated so the next sync
does not read the change as a play on another device. A server that keeps
its own date is counted as refused, remembered, and not pushed again until
a manual run. The setting turns the automatic run off.

No Kodi and no Jellyfin: kodi_stubs puts the profile in a temporary folder,
and ratings._request is replaced by a fake that records every call and plays
the server, including one item that refuses to change.

Usage:
    python3 tests/test_dates.py
</remarks>
"""

import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kodi_stubs  # noqa: E402

kodi_stubs.install()

import dates  # noqa: E402
import library  # noqa: E402
import main as core  # noqa: E402
import playlog  # noqa: E402
import ratings  # noqa: E402
import webdata  # noqa: E402

FAILURES = []
BULK_STAMP = "2025-12-15T20:00:00"


def check(condition, message):
    """<summary>Record a failure without stopping.</summary>"""
    if not condition:
        FAILURES.append(message)
        print("FAIL: %s" % message)


def equal(actual, expected, what):
    """<summary>check() for equality, printing both sides on failure.</summary>"""
    check(actual == expected, "%s: expected %r, got %r" % (what, expected, actual))


def jf_item(item_id, name, year, played_utc, series=None, season=None,
            episode=None):
    """<summary>A Jellyfin item as the mirror expects it.</summary>"""
    item = {"Id": item_id, "Name": name, "ProductionYear": year,
            "RunTimeTicks": 90 * 60 * 10000000,
            "UserData": {"PlayCount": 1, "IsFavorite": False, "Played": True,
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
    jf_item("m1", "Alpha", 2010, BULK_STAMP),
    jf_item("m2", "Beta", 2012, "2026-10-01T19:30:00"),
    jf_item("m3", "Gamma", 2015, BULK_STAMP),
    jf_item("m4", "Delta", 2018, BULK_STAMP),
]
SERIES = [{"Id": "s-show", "Name": "Show", "Genres": ["Comedy"], "UserData": {}}]
EPISODES = [
    jf_item("e1", "One", 2020, BULK_STAMP, "Show", 1, 1),
]


def add_play(title, stamp, show=None, year=None, source="trakt", completed=1):
    """<summary>One play log row written straight into the table.</summary>"""
    when = datetime.strptime(stamp, playlog.STAMP)
    connection = playlog.connect()
    with connection:
        connection.execute(
            "INSERT INTO plays (started_at, ended_at, day, hour, weekday, "
            "media, title, show, season, episode, year, runtime_seconds, "
            "watched_seconds, completed, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, 5400, 5400, ?, ?)",
            (stamp, stamp, stamp[:10], when.hour, when.weekday(),
             "episode" if show else "movie", title, show, year, completed,
             source))
    connection.close()


# ---- a fake Jellyfin ------------------------------------------------------

SERVER = {}     # item id -> UserData dict as the server holds it
CALLS = []      # (method, item id, body) in order
STUBBORN = set()  # item ids whose date the fake server refuses to change


def fake_request(url, token, method, body=None):
    """
    <summary>
    Play the UserData route: GET returns the stored object, POST stores it
    unless the item is stubborn, in which case the date is kept.
    </summary>
    """
    item_id = url.rstrip("/").split("/")[-2]
    CALLS.append((method, item_id, body))
    if method == "GET":
        return 200, dict(SERVER[item_id])
    if method == "POST":
        kept = SERVER[item_id].get("LastPlayedDate")
        SERVER[item_id] = dict(body)
        if item_id in STUBBORN:
            SERVER[item_id]["LastPlayedDate"] = kept
        return 204, None
    raise AssertionError("unexpected method %s" % method)


def fake_credentials():
    """<summary>A login the module can use against the fake.</summary>"""
    return {"base": "http://jellyfin.test", "token": "t", "user_id": "u"}


def seed():
    """<summary>Sync the invented library into the mirror and the fake server.</summary>"""
    offset = webdata._utc_offset()
    result = library.sync(MOVIES, EPISODES, {"s-show": ["Comedy"]}, offset, SERIES)
    equal(result["added"], 6, "mirror seeded")
    for item in MOVIES + EPISODES:
        SERVER[item["Id"]] = dict(item["UserData"], PlaybackPositionTicks=0)


def wait_for_job():
    """<summary>Block until the background run has finished.</summary>"""
    for _ in range(200):
        if dates.progress()["state"] != "running":
            return
        time.sleep(0.02)
    check(False, "the write back did not finish")


# ---- tests ----------------------------------------------------------------

def test_round_trip():
    """<summary>A local stamp goes to UTC and back to the same second.</summary>"""
    print("running test_round_trip")
    for stamp in ("2026-07-10T21:15:00", "2026-01-05T08:00:00",
                  "2025-12-15T20:00:00"):
        back = library.local_stamp(dates.to_utc_text(stamp))
        equal(back, stamp, "round trip of %s" % stamp)
    equal(dates.to_utc_text("garbage"), None, "garbage parses to None")


def test_targets():
    """<summary>Only titles with a sitting that disagrees with Jellyfin are targets.</summary>"""
    print("running test_targets")
    seed()
    equal(dates.targets(), [], "nothing logged, nothing to write")
    alpha = "2026-09-10T20:00:00"
    add_play("Alpha", alpha, year=2010)
    add_play("Gamma", "2026-09-11T20:00:00", year=2015, completed=0)
    add_play("Beta", library.local_stamp("2026-10-01T19:30:00"), year=2012)
    one = "2026-08-01T21:00:00"
    add_play("One", one, show="Show", year=2020)
    found = {row["id"]: row for row in dates.targets()}
    equal(sorted(found), ["e1", "m1"],
          "the finished Alpha and the episode differ; Beta agrees, Gamma is unfinished")
    equal(found["m1"]["wanted"], dates.to_utc_text(alpha), "Alpha wants its sitting, in UTC")
    equal(found["m1"]["have"], BULK_STAMP, "Alpha's mirror date is the bulk day")
    equal(found["e1"]["show"], "Show", "the episode carries its show")
    report = dates.survey()
    equal(report["differing"], 2, "the survey counts the same two")
    equal(report["refused"], 0, "nothing refused yet")
    check(report["enabled"], "on by default")
    equal(report["examples"][0]["jellyfin"], library.local_stamp(BULK_STAMP),
          "examples show Jellyfin's date made local")


def test_push_and_refusal():
    """<summary>The run writes each date, confirms it, updates the mirror, and remembers a refusal.</summary>"""
    print("running test_push_and_refusal")
    STUBBORN.add("e1")
    dates.start()
    wait_for_job()
    job = dates.progress()
    equal(job["state"], "finished", "the job finished")
    equal(job["total"], 2, "two targets")
    equal(job["refused"], 1, "the stubborn episode was refused")
    equal(job["failed"], 0, "no failures")
    check("Wrote 1 of 2" in (job["message"] or ""), "message: %r" % job["message"])

    posted = [(item, body) for method, item, body in CALLS if method == "POST"]
    equal(sorted(item for item, _ in posted), ["e1", "m1"], "both were posted")
    body = dict(posted)["m1"]
    equal(body["LastPlayedDate"], dates.to_utc_text("2026-09-10T20:00:00") + ".0000000Z",
          "the posted date is the sitting in Jellyfin's spelling")
    equal(body["PlayCount"], 1, "the play count went back unchanged")
    equal(body["Played"], True, "the watched flag went back unchanged")
    check("Likes" not in body, "Likes is left to the server")
    order = [method for method, item, _ in CALLS if item == "m1"]
    equal(order, ["GET", "POST", "GET"], "read, write, read back")

    connection = library.connect()
    mirror = dict(connection.execute(
        "SELECT id, last_played FROM items WHERE id IN ('m1', 'e1')").fetchall())
    connection.close()
    equal(mirror["m1"], dates.to_utc_text("2026-09-10T20:00:00"),
          "the mirror holds the pushed date so the next sync sees no change")
    equal(mirror["e1"], BULK_STAMP, "the refused item's mirror date is untouched")

    equal([row["id"] for row in dates.targets()], [],
          "the refused item is not offered to the service again")
    equal([row["id"] for row in dates.targets(include_refused=True)], ["e1"],
          "but the survey still sees it")
    equal(dates.survey()["refused"], 1, "the survey counts it as refused")

    CALLS.clear()
    STUBBORN.clear()
    dates.start(manual=True)
    wait_for_job()
    equal(dates.progress()["message"], "Wrote 1 of 1 dates to Jellyfin",
          "a manual run tries the refused item again and it takes")
    equal(dates.targets(include_refused=True), [], "everything agrees now")


def test_sync_does_not_undo():
    """<summary>A mirror sync after the push does not log a phantom play or move the date.</summary>"""
    print("running test_sync_does_not_undo")
    before = playlog.connect().execute("SELECT COUNT(*) FROM plays").fetchone()[0]
    for item in MOVIES + EPISODES:
        item["UserData"]["LastPlayedDate"] = SERVER[item["Id"]]["LastPlayedDate"]
    result = library.sync(MOVIES, EPISODES, {"s-show": ["Comedy"]},
                          webdata._utc_offset(), SERIES)
    equal(result["inferred"], 0, "no play inferred from the dates we wrote")
    after = playlog.connect().execute("SELECT COUNT(*) FROM plays").fetchone()[0]
    equal(after, before, "the play log is unchanged")
    equal(dates.targets(include_refused=True), [], "still nothing to write")


def test_setting_and_nudge():
    """<summary>nudge() does nothing when turned off or when nothing differs.</summary>"""
    print("running test_setting_and_nudge")
    equal(dates.nudge(), False, "nothing differs, nothing started")
    add_play("Delta", "2026-03-03T20:00:00", year=2018)
    kodi_stubs.SETTINGS["push_dates"] = "false"
    equal(dates.enabled(), False, "the setting turns it off")
    equal(dates.nudge(), False, "off means no run")
    equal(len(dates.targets()), 1, "the target is still reported")
    kodi_stubs.SETTINGS["push_dates"] = "true"
    equal(dates.nudge(), True, "on again, the run starts")
    wait_for_job()
    equal(dates.targets(include_refused=True), [], "and Delta was written")


def test_no_login():
    """<summary>Without a Jellyfin login the run ends with a message and writes nothing.</summary>"""
    print("running test_no_login")
    add_play("Delta", "2026-04-04T20:00:00", year=2018)
    CALLS.clear()

    def no_creds():
        raise core.JellyStatError("No Jellyfin login found.")
    core.get_credentials = no_creds
    dates.start()
    wait_for_job()
    check("No Jellyfin login" in (dates.progress()["message"] or ""),
          "the message names the missing login")
    equal(CALLS, [], "nothing was sent")
    core.get_credentials = fake_credentials


def main():
    """<summary>Run every test and exit non zero if any check failed.</summary>"""
    ratings._request = fake_request
    core.get_credentials = fake_credentials
    try:
        test_round_trip()
        test_targets()
        test_push_and_refusal()
        test_sync_does_not_undo()
        test_setting_and_nudge()
        test_no_login()
    finally:
        kodi_stubs.cleanup()
    if FAILURES:
        print("test_dates: %d check(s) failed" % len(FAILURES))
        return 1
    print("test_dates: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
