# -*- coding: utf-8 -*-
"""
<summary>
Everything the web dashboard shows, as one JSON-able dict.
</summary>
<remarks>
Built on the same primitives as the Kodi UI and the website sender, so the
three never disagree: credentials and genre rules come from main.py, the
genre/window blocks from stats_sender.py. What this module adds is the
"habits" side (when you watch, not just what) which it derives from each
item's last-played date.

The habit charts have two eras. Once the play log has sessions (recorded by
player.py, or imported), days from its first covered date onwards count real
plays with real clock times. Days before that fall back to Jellyfin's
last-played dates, which carry a caveat that shapes everything here: the
server keeps only the *most recent* play date per item, so a film watched
three times appears once, on its latest date: a picture of distinct items,
not of every play. The payload says where the changeover is so the page can
mark it.
</remarks>
"""

import threading
import time
from datetime import datetime, timedelta, timezone

import xbmc
import xbmcaddon

import library
import main as core
import playlog
import stats_sender

CALENDAR_DAYS = 365
RECENT_ITEMS = 25
TOP_SHOWS = 10

_lock = threading.Lock()
_cache = {"at": 0.0, "data": None}


def log(message, level=xbmc.LOGINFO):
    """
    <summary>
    Write one line to Kodi's log under the JellyStat tag.
    </summary>
    <param name="message">Text to log.</param>
    <param name="level">Kodi log level, info by default.</param>
    """
    xbmc.log("[JellyStat] %s" % message, level)


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------

def _utc_offset():
    """
    <summary>
    Local clock minus UTC clock, as a timedelta.
    </summary>
    <remarks>
    Jellyfin reports play dates in UTC; every habit below is about the hour
    the viewer was actually sitting there, so it has to be local. Taking the
    offset from the running box also means it follows DST changes.
    </remarks>
    """
    return datetime.now() - datetime.now(timezone.utc).replace(tzinfo=None)


def _local_played(item, offset):
    """
    <summary>
    An item's last played time shifted to local time, or None.
    </summary>
    <param name="item">A Jellyfin item.</param>
    <param name="offset">Local minus UTC as a timedelta.</param>
    <returns>A datetime or None.</returns>
    """
    played = core.parse_played_date(
        (item.get("UserData") or {}).get("LastPlayedDate"))
    return (played + offset) if played else None


# ---------------------------------------------------------------------------
# Habits
# ---------------------------------------------------------------------------

def _calendar(movies, episodes, offset, days=CALENDAR_DAYS,
              sessions=None, coverage_start=None):
    """
    <summary>
    Per-day counts for the last N days, oldest first (heatmap + bars).
    </summary>
    <remarks>
    Every day in the range is present, including the empty ones, a gap in
    the data and a day off must look different in the chart.

    Days on or after coverage_start are counted from the play log (every
    sitting counts); earlier days from the items' last-played dates. Items
    played after coverage_start with no matching session (watched on a
    phone, say) still count, so the changeover adds plays without losing
    any.
    </remarks>
    """
    today = datetime.now().date()
    start = today - timedelta(days=days - 1)
    covered = None
    if coverage_start:
        covered = datetime.strptime(coverage_start, "%Y-%m-%d").date()
    tally = {}

    def bump(day, kind):
        """
        <summary>
        Add one play of a kind to a day's tally.
        </summary>
        <param name="day">YYYY-MM-DD.</param>
        <param name="kind">movies or episodes.</param>
        """
        slot = tally.setdefault(day, {"movies": 0, "episodes": 0})
        slot[kind] += 1

    session_keys = set()
    for event in sessions or []:
        day = datetime.strptime(event["day"], "%Y-%m-%d").date()
        session_keys.add((playlog.dedup_key(event["show"], event["title"]),
                          event["day"]))
        if event["kind"] and start <= day <= today:
            bump(day, event["kind"])

    for kind, items in (("movies", movies), ("episodes", episodes)):
        for item in items:
            played = _local_played(item, offset)
            if not played or not (start <= played.date() <= today):
                continue
            if covered and played.date() >= covered:
                # The log's era: only count it if the log missed it.
                key = (playlog.dedup_key(item.get("SeriesName"),
                                         item.get("Name")),
                       played.strftime("%Y-%m-%d"))
                if key in session_keys:
                    continue
            bump(played.date(), kind)

    calendar = []
    for step in range(days):
        day = start + timedelta(days=step)
        slot = tally.get(day) or {"movies": 0, "episodes": 0}
        calendar.append({"date": day.strftime("%Y-%m-%d"),
                         "movies": slot["movies"],
                         "episodes": slot["episodes"]})
    return calendar


def _clock_and_week(movies, episodes, offset, sessions=None,
                    coverage_start=None):
    """
    <summary>
    When watching happens: 24 hour buckets and 7 weekday buckets.
    </summary>
    <remarks>
    Blended the same way as _calendar, so the two eras never fight: play-log
    sessions count as real sittings, and Jellyfin last-played dates fill in
    everything the log has no session for: all of the pre-log era, and
    anything watched on another device since. Without this blend the charts
    would collapse to a single day's data the moment logging starts.
    Date-only imports (hour < 0) are skipped by the clock but still count
    for the weekday.

    Weekdays are Monday-first to match datetime.weekday(); the dashboard
    labels them, so the order only has to be stated once, here.
    </remarks>
    """
    hours = [{"hour": hour, "movies": 0, "episodes": 0} for hour in range(24)]
    weekdays = [{"weekday": day, "movies": 0, "episodes": 0}
                for day in range(7)]
    covered = None
    if coverage_start:
        covered = datetime.strptime(coverage_start, "%Y-%m-%d").date()
    session_keys = set()
    for event in sessions or []:
        kind = event["kind"]
        if not kind:
            continue
        session_keys.add((playlog.dedup_key(event["show"], event["title"]),
                          event["day"]))
        if event["hour"] >= 0:
            hours[event["hour"]][kind] += 1
        weekdays[event["weekday"]][kind] += 1
    for kind, items in (("movies", movies), ("episodes", episodes)):
        for item in items:
            played = _local_played(item, offset)
            if not played:
                continue
            if covered and played.date() >= covered:
                key = (playlog.dedup_key(item.get("SeriesName"),
                                         item.get("Name")),
                       played.strftime("%Y-%m-%d"))
                if key in session_keys:
                    continue
            hours[played.hour][kind] += 1
            weekdays[played.weekday()][kind] += 1
    return hours, weekdays


def _streaks(calendar):
    """
    <summary>
    Current and longest run of consecutive days with something watched.
    </summary>
    <remarks>
    An empty *today* does not break a live streak, the evening has not
    happened yet, so the current run is measured back from yesterday in
    that case. Any earlier gap ends it.
    </remarks>
    """
    active = [bool(day["movies"] or day["episodes"]) for day in calendar]
    longest = 0
    run = 0
    for flag in active:
        run = run + 1 if flag else 0
        longest = max(longest, run)
    index = len(active) - 1
    if index >= 0 and not active[index]:
        index -= 1
    current = 0
    while index >= 0 and active[index]:
        current += 1
        index -= 1
    return {"current": current, "longest": longest}


def _top_shows(episodes, series_genres, offset, limit=TOP_SHOWS):
    """
    <summary>
    Shows ranked by distinct episodes watched, with their last watch.
    </summary>
    """
    shows = {}
    for episode in episodes:
        name = episode.get("SeriesName") or "Unknown show"
        entry = shows.setdefault(name, {
            "show": name,
            "episodes": 0,
            "plays": 0,
            "last_played": None,
            "genres": core.effective_genres(episode, series_genres),
        })
        entry["episodes"] += 1
        entry["plays"] += (episode.get("UserData") or {}).get("PlayCount") or 0
        played = _local_played(episode, offset)
        if played:
            stamp = played.strftime("%Y-%m-%d")
            if not entry["last_played"] or stamp > entry["last_played"]:
                entry["last_played"] = stamp
    ordered = sorted(shows.values(),
                     key=lambda show: (-show["episodes"], show["show"]))
    return ordered[:limit]


def _rating_spread(movies):
    """
    <summary>
    Community ratings in whole-number buckets: what you settle for.
    </summary>
    <remarks>
    Bucket N holds ratings N.0 to N.9, so a 7.8 lands in 7. Unrated items
    are counted separately rather than dropped, because "half the library
    has no rating" is itself worth seeing.
    </remarks>
    """
    buckets = [0] * 11
    unrated = 0
    for movie in movies:
        rating = movie.get("CommunityRating")
        if not rating:
            unrated += 1
            continue
        buckets[max(0, min(int(rating), 10))] += 1
    return {"buckets": buckets, "unrated": unrated}


def _item_row(item, offset, series_genres=None):
    """
    <summary>
    One recent item as the dashboard shows it: an episode when series_genres is given, else a film.
    </summary>
    <param name="item">A Jellyfin item.</param>
    <param name="offset">Local minus UTC as a timedelta.</param>
    <param name="series_genres">Series id to genres, or None for a film.</param>
    <returns>A dict.</returns>
    """
    played = _local_played(item, offset)
    user_data = item.get("UserData") or {}
    row = {
        "name": item.get("Name") or "?",
        "type": "episode" if series_genres is not None else "movie",
        "played": played.strftime("%Y-%m-%d %H:%M") if played else None,
        "genres": core.effective_genres(item, series_genres),
        "rating": item.get("CommunityRating"),
        "critic": item.get("CriticRating"),
        "favourite": bool(user_data.get("IsFavorite")),
        "plays": user_data.get("PlayCount") or 0,
    }
    if series_genres is not None:
        row["show"] = item.get("SeriesName") or "?"
        row["code"] = "S%02dE%02d" % (item.get("ParentIndexNumber") or 0,
                                      item.get("IndexNumber") or 0)
    else:
        row["year"] = item.get("ProductionYear")
    return row


def _recent(movies, episodes, series_genres, offset, limit=RECENT_ITEMS):
    """
    <summary>
    The last N things watched, films and episodes interleaved by date.
    </summary>
    """
    rows = [_item_row(movie, offset) for movie in movies]
    rows += [_item_row(episode, offset, series_genres)
             for episode in episodes]
    rows = [row for row in rows if row["played"]]
    rows.sort(key=lambda row: row["played"], reverse=True)
    return rows[:limit]


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def build(recent_days=30):
    """
    <summary>
    Compute the whole dashboard payload.
    </summary>
    <remarks>
    Normally fetched live from Jellyfin, after which the fetch is folded
    into the local mirror (library.sync), the mirror is the addon's own
    permanent copy, and syncing it here means it costs no extra Jellyfin
    traffic. When Jellyfin is unreachable the mirror serves instead, so the
    dashboard degrades to "as of the last sync" rather than to an error.
    </remarks>
    """
    offset = _utc_offset()
    source = "jellyfin"
    try:
        creds = core.get_credentials()
        movies = core.get_watched(creds, "Movie")
        episodes = core.get_watched(creds, "Episode")
        series = core.get_series(creds)
        series_genres = core.get_series_genres(creds, series)
    except core.JellyStatError:
        movies, episodes, series_genres = library.as_jellyfin_items()
        if not movies and not episodes:
            raise  # nothing mirrored yet: the real error is the useful one
        source = "mirror"
        creds = {"base": "local mirror (Jellyfin unreachable)"}
    else:
        library.sync(movies, episodes, series_genres, offset, series)
    recent_movies = core.filter_recent(movies, recent_days)
    recent_episodes = core.filter_recent(episodes, recent_days)

    # The genre/window blocks are exactly what the website receives, so the
    # dashboard and the site can never drift apart.
    snapshot = stats_sender.build_payload(movies, recent_movies,
                                          episodes, recent_episodes,
                                          series_genres)

    sessions = playlog.events()
    coverage_start = playlog.since()
    calendar = _calendar(movies, episodes, offset,
                         sessions=sessions, coverage_start=coverage_start)
    hours, weekdays = _clock_and_week(movies, episodes, offset,
                                      sessions=sessions,
                                      coverage_start=coverage_start)
    favourites = [_item_row(movie, offset) for movie in movies
                  if (movie.get("UserData") or {}).get("IsFavorite")]

    return {
        "version": 1,
        "addon_version": xbmcaddon.Addon("script.jellystat")
                         .getAddonInfo("version"),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "recent_days": recent_days,
        "server": creds["base"],
        "source": source,
        "mirror": library.status(),
        "snapshot": snapshot,
        "totals": {
            "movies": len(movies),
            "movies_recent": len(recent_movies),
            "episodes": len(episodes),
            "episodes_recent": len(recent_episodes),
            "plays": sum((item.get("UserData") or {}).get("PlayCount") or 0
                         for item in movies + episodes),
            "avg_movie_rating": core.average_rating(movies),
            "avg_episode_rating": core.average_rating(episodes),
        },
        "calendar": calendar,
        "hours": hours,
        "weekdays": weekdays,
        "streaks": _streaks(calendar),
        "top_shows": _top_shows(episodes, series_genres, offset),
        "ratings": _rating_spread(movies),
        "recent": _recent(movies, episodes, series_genres, offset),
        "favourites": favourites[:12],
        "playlog": {
            "since": coverage_start,
            "summary": playlog.summary(),
            "rewatches": playlog.rewatches(),
            "recent_sessions": playlog.recent(),
        },
    }


def get(recent_days=30, max_age=600, force=False):
    """
    <summary>
    Cached build(). A full library read is far too slow per page load.
    </summary>
    <remarks>
    One in-flight build at a time (the lock), so three browsers opening the
    page together make one Jellyfin round trip, not three.
    </remarks>
    """
    with _lock:
        fresh = (_cache["data"] is not None
                 and (time.time() - _cache["at"]) < max_age)
        if fresh and not force:
            return _cache["data"], False
        data = build(recent_days)
        _cache["at"] = time.time()
        _cache["data"] = data
        return data, True


def invalidate():
    """
    <summary>
    Drop the cache, so the next page load sees a just-finished import.
    </summary>
    """
    with _lock:
        _cache["data"] = None


def cached_age():
    """
    <summary>
    Seconds since the last successful build, or None if never built.
    </summary>
    """
    if _cache["data"] is None:
        return None
    return int(time.time() - _cache["at"])
