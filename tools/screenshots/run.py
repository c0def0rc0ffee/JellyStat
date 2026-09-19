# -*- coding: utf-8 -*-
"""
<summary>
Runs the real JellyStat dashboard against the demo Jellyfin.
</summary>
<remarks>
Nothing in the addon is patched: the stubs stand in for Kodi, the fake
server stands in for Jellyfin, and everything between them is the shipping
code. A fresh profile each run, so the screenshots do not drift.
</remarks>
"""

import os
import random
import shutil
import sys
import time
from datetime import timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(HERE))
ADDON = os.environ.get("JS_ADDON_DIR") or os.path.join(PROJECT,
                                                       "script.jellystat")
PROFILE = os.environ.get("JS_HARNESS_PROFILE") or os.path.join(
    HERE, ".profile")

JELLYFIN_PORT = int(os.environ.get("JS_JELLYFIN_PORT", 8096))
WEB_PORT = int(os.environ.get("JS_WEB_PORT", 8099))

SETTINGS = {
    "username": "demo",
    "ignore_ssl": "false",
    "show_percentages": "true",
    "show_avg_ratings": "true",
    "show_favourites": "true",
    "show_list_ratings": "true",
    "recent_days": "30",
    "web_enabled": "true",
    "web_show_address": "true",
    "web_bind_all": "false",
    "web_password": "",
    "web_cache_minutes": "10",
    "rating_sets_favourite": "false",
    "history_enabled": "true",
    "playlog_enabled": "true",
    "playlog_min_minutes": "2",
}


def main():
    """
    <summary>
    Wire everything up: a fresh profile, the fake Jellyfin, the stubbed Kodi modules, a seeded play log and history, then the real dashboard server.
    </summary>
    """
    if not os.path.isdir(ADDON):
        raise SystemExit("no addon source at %s" % ADDON)
    if os.path.isdir(PROFILE):
        shutil.rmtree(PROFILE)
    os.makedirs(os.path.join(PROFILE, "addon_data", "script.jellystat"))

    os.environ["JS_HARNESS_PROFILE"] = PROFILE
    os.environ["JS_HARNESS_ADDON"] = ADDON
    sys.path.insert(0, os.path.join(HERE, "stubs"))
    sys.path.insert(0, HERE)
    sys.path.insert(0, ADDON)

    import fake_jellyfin
    fake_jellyfin.start(JELLYFIN_PORT)
    data = fake_jellyfin.DATA
    print("fake Jellyfin on http://127.0.0.1:%d" % JELLYFIN_PORT)

    import xbmcaddon
    addon = xbmcaddon.Addon()
    for key, value in SETTINGS.items():
        addon.setSetting(key, value)
    addon.setSetting("server_url", "http://127.0.0.1:%d" % JELLYFIN_PORT)
    addon.setSetting("api_key", fake_jellyfin.TOKEN)
    addon.setSetting("web_port", str(WEB_PORT))

    seed_playlog(data)
    seed_history(data)

    import webserver
    webserver.start()
    print("dashboard on http://127.0.0.1:%d/" % WEB_PORT)
    print("ready")
    sys.stdout.flush()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def seed_playlog(data):
    """
    <summary>
    Turn the demo timeline into the sessions the Kodi logger would hold.
    </summary>
    <remarks>
    Only the recent stretch is logged: the play log starts when the addon
    was installed, and everything before it reaches the dashboard through
    Jellyfin's last-played dates instead.
    </remarks>
    """
    import generate
    import playlog

    rng = random.Random(generate.SEED + 3)
    cutoff = data["now"] - timedelta(days=generate.LOG_COVERAGE_DAYS)
    kept = 0
    for item in data["movies"] + data["episodes"]:
        for started in item.get("plays") or []:
            if started < cutoff:
                continue
            runtime = item["runtime"] * 60
            # Most sittings run to the end; some stop part way.
            roll = rng.random()
            if roll < 0.82:
                watched = int(runtime * rng.uniform(0.94, 1.0))
            elif roll < 0.94:
                watched = int(runtime * rng.uniform(0.55, 0.88))
            else:
                watched = int(runtime * rng.uniform(0.12, 0.4))
            if playlog.record({
                "started_at": started,
                "ended_at": started + timedelta(seconds=watched),
                "media": "movie" if item["kind"] == "movie" else "episode",
                "title": item["title"],
                "show": item.get("series_name"),
                "season": item.get("season"),
                "episode": item.get("episode"),
                "year": item["year"],
                "runtime_seconds": runtime,
                "watched_seconds": watched,
                "source": "kodi",
                "device": "Living room",
            }, min_seconds=120):
                kept += 1
    print("play log seeded with %d sessions" % kept)


def seed_history(data, days=180):
    """
    <summary>
    A run of daily snapshots, so the trend charts have a line to draw.
    </summary>
    <remarks>
    Each day is recomputed from the demo timeline as it stood that evening
    rather than by scaling today's numbers, so the trend the charts draw is
    the one the viewing actually made.
    </remarks>
    """
    import fake_jellyfin
    import history
    import stats_sender

    rng = random.Random(1)
    series_genres = {s["id"]: list(s["genres"]) for s in data["series"]}
    now = data["now"]

    def as_of(item, day):
        """
        <summary>
        The item as Jellyfin would have reported it on a given day, or None if unplayed by then.
        </summary>
        <param name="item">A catalogue item.</param>
        <param name="day">A datetime.</param>
        <returns>A Jellyfin item dict or None.</returns>
        """
        plays = [p for p in (item.get("plays") or []) if p <= day]
        if not plays:
            return None
        row = fake_jellyfin._to_jellyfin(item, rng)
        row["UserData"] = dict(row["UserData"], PlayCount=len(plays),
                               Played=True,
                               LastPlayedDate=fake_jellyfin._utc(max(plays)))
        return row

    for days_ago in range(days, -1, -1):
        day = now - timedelta(days=days_ago)
        window = day - timedelta(days=30)
        movies, recent_movies, episodes, recent_episodes = [], [], [], []
        for item in data["movies"]:
            row = as_of(item, day)
            if row:
                movies.append(row)
                if max(p for p in item["plays"] if p <= day) >= window:
                    recent_movies.append(row)
        for item in data["episodes"]:
            row = as_of(item, day)
            if row:
                episodes.append(row)
                if max(p for p in item["plays"] if p <= day) >= window:
                    recent_episodes.append(row)
        history.record(stats_sender.build_payload(movies, recent_movies,
                                                  episodes, recent_episodes,
                                                  series_genres), when=day)
    print("history seeded with %d daily snapshots" % (days + 1))


if __name__ == "__main__":
    main()
