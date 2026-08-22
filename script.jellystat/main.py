# -*- coding: utf-8 -*-
"""JellyStat - watch statistics from your Jellyfin server.

Talks to the Jellyfin server API and shows what you have watched,
each item's genre(s) and rating. Credentials are auto-discovered from
the Jellyfin for Kodi addon (plugin.video.jellyfin) when installed,
otherwise the server URL and API key from this addon's settings are used.
"""

import json
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import xbmcaddon
import xbmcgui
import xbmcvfs

ADDON = xbmcaddon.Addon()
ADDON_NAME = "JellyStat"

JELLYFIN_DATA = "special://profile/addon_data/plugin.video.jellyfin/data.json"


class JellyStatError(Exception):
    """Raised for any problem talking to the Jellyfin server."""


def setting_bool(name, default=True):
    value = ADDON.getSetting(name)
    if value == "":
        return default
    return value == "true"


def setting_int(name, default):
    try:
        return int(ADDON.getSetting(name))
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def credentials_from_jellyfin_addon():
    """Reuse the login the Jellyfin for Kodi addon already has."""
    path = xbmcvfs.translatePath(JELLYFIN_DATA)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    for server in data.get("Servers") or []:
        token = server.get("AccessToken")
        user_id = server.get("UserId")
        address = (server.get("address") or server.get("ManualAddress")
                   or server.get("LocalAddress"))
        if token and user_id and address:
            return {"base": address.rstrip("/"), "token": token,
                    "user_id": user_id}
    return None


def credentials_from_settings():
    """Fall back to the server URL + API key configured in addon settings."""
    base = ADDON.getSetting("server_url").strip().rstrip("/")
    token = ADDON.getSetting("api_key").strip()
    username = ADDON.getSetting("username").strip()
    if not base or not token:
        return None
    users = api_get(base, token, "/Users")
    if not users:
        raise JellyStatError("The API key is valid but no users were returned.")
    user = None
    if username:
        matches = [u for u in users
                   if u.get("Name", "").lower() == username.lower()]
        if not matches:
            raise JellyStatError('No Jellyfin user named "%s" found.' % username)
        user = matches[0]
    else:
        user = users[0]
    return {"base": base, "token": token, "user_id": user["Id"]}


def get_credentials():
    creds = credentials_from_jellyfin_addon()
    if creds:
        return creds
    creds = credentials_from_settings()
    if creds:
        return creds
    raise JellyStatError(
        "No Jellyfin login found.\n"
        "Sign in with the Jellyfin for Kodi addon, or set the server URL "
        "and API key in JellyStat's addon settings.")


# ---------------------------------------------------------------------------
# Jellyfin API
# ---------------------------------------------------------------------------

def api_get(base, token, path, params=None):
    url = base + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={
        "X-Emby-Token": token,
        "Accept": "application/json",
    })
    context = None
    if url.startswith("https") and ADDON.getSetting("ignore_ssl") == "true":
        context = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(request, timeout=30,
                                    context=context) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        if err.code == 401:
            raise JellyStatError(
                "Jellyfin rejected the login (401). The saved token or API "
                "key may have expired.")
        raise JellyStatError("Jellyfin returned HTTP %d for %s" % (err.code,
                                                                   path))
    except (urllib.error.URLError, OSError) as err:
        raise JellyStatError("Could not reach the Jellyfin server at %s:\n%s"
                             % (base, err))


def get_watched(creds, item_type):
    """Return all played items of the given type for the signed-in user."""
    result = api_get(creds["base"], creds["token"],
                     "/Users/%s/Items" % creds["user_id"], {
                         "IncludeItemTypes": item_type,
                         "Recursive": "true",
                         "Filters": "IsPlayed",
                         # ProviderIds carries IMDb/TMDb/TVDb, which is how
                         # imported history from other trackers is matched
                         # to this library exactly rather than by title.
                         "Fields": "Genres,CommunityRating,CriticRating,"
                                   "ProductionYear,ProviderIds",
                         "SortBy": "DatePlayed",
                         "SortOrder": "Descending",
                     })
    return result.get("Items", [])


def get_series(creds):
    """Every series in the library, with the fields the mirror stores.

    Shows are items in their own right: they carry genres their episodes
    lack, a rating of their own, and provider ids that imported data is
    matched on. get_series_genres() is the older, narrower view of this.
    """
    result = api_get(creds["base"], creds["token"],
                     "/Users/%s/Items" % creds["user_id"], {
                         "IncludeItemTypes": "Series",
                         "Recursive": "true",
                         "Fields": "Genres,CommunityRating,CriticRating,"
                                   "ProductionYear,ProviderIds",
                     })
    return result.get("Items", [])


def get_series_genres(creds, series=None):
    """Map series id -> genres, since episodes carry no genres of their own."""
    if series is None:
        series = get_series(creds)
    return {item["Id"]: item.get("Genres", []) for item in series}


def parse_played_date(value):
    """Parse Jellyfin's LastPlayedDate ('2026-06-15T20:31:12.0000000Z')."""
    if not value:
        return None
    value = value[:19]
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")
    except TypeError:
        # Kodi's embedded Python can lose datetime.strptime after first use;
        # fall back to time.strptime (well-known interpreter quirk).
        return datetime(*(time.strptime(value, "%Y-%m-%dT%H:%M:%S")[0:6]))
    except ValueError:
        return None


def filter_recent(items, days=30):
    """Keep only items last played within the given number of days."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    recent = []
    for item in items:
        played = parse_played_date(
            (item.get("UserData") or {}).get("LastPlayedDate"))
        if played and played >= cutoff:
            recent.append(item)
    return recent


# ---------------------------------------------------------------------------
# Genres
# ---------------------------------------------------------------------------

GENRE_ALIASES = {
    "sci-fi": "Science Fiction",
    "sci fi": "Science Fiction",
    "scifi": "Science Fiction",
    "science-fiction": "Science Fiction",
    "science fiction": "Science Fiction",
    # Fantasy is its own genre. TMDb's *combined* TV genre has no way to be
    # split, so it lands on Science Fiction rather than being dropped.
    "sci-fi & fantasy": "Science Fiction",
    "sci fi & fantasy": "Science Fiction",
    "science fiction & fantasy": "Science Fiction",
    "animated": "Animation",
    "kids": "Family",
    "children": "Family",
}


def canonical_genre(name):
    name = (name or "").strip()
    if not name:
        return "Unknown"
    return GENRE_ALIASES.get(name.lower(), name)


def effective_genres(item, genre_lookup=None):
    """An item's genres, aliased and deduped.

    Every genre a title carries counts, so a science fiction horror film is
    both. Counts across genres therefore sum to more than the number of
    titles, which is why the genre views offer a "primary only" mode when a
    breakdown that adds up to 100% is what is wanted.

    (Until v0.15.0 a "sci-fi dominance" rule collapsed anything science
    fiction down to that single genre. It suppressed 1,771 genre tags on a
    2,200 film library - 524 films stopped being Action - while every other
    multi-genre title still counted in each of its genres, so it bought no
    consistency. Primary-only does that job properly.)

    No genres at all -> ["Unknown"]. For episodes pass genre_lookup
    (series id -> genres); episodes carry no genres of their own.
    """
    if genre_lookup is not None:
        raw = genre_lookup.get(item.get("SeriesId")) or []
    else:
        raw = item.get("Genres") or []
    genres = []
    seen = set()
    for genre in raw:
        canon = canonical_genre(genre)
        if canon.lower() not in seen:
            seen.add(canon.lower())
            genres.append(canon)
    return genres or ["Unknown"]


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_genres(genres):
    return ", ".join(genres) if genres else "No genre"


def format_rating(item):
    parts = []
    rating = item.get("CommunityRating")
    if rating:
        parts.append(u"★ %.1f" % rating)
    critic = item.get("CriticRating")
    if critic:
        parts.append("Critics: %d%%" % critic)
    if (item.get("UserData") or {}).get("IsFavorite"):
        parts.append(u"♥")
    return " | ".join(parts) if parts else "Not rated"


def movie_label(movie, show_rating=True):
    year = movie.get("ProductionYear")
    title = "%s (%s)" % (movie["Name"], year) if year else movie["Name"]
    label = "%s  [%s]" % (title, format_genres(effective_genres(movie)))
    if show_rating:
        label += "  %s" % format_rating(movie)
    return label


def episode_label(episode, series_genres, show_rating=True):
    code = "S%02dE%02d" % (episode.get("ParentIndexNumber") or 0,
                           episode.get("IndexNumber") or 0)
    label = "%s %s - %s  [%s]" % (episode.get("SeriesName", "?"), code,
                                  episode.get("Name", "?"),
                                  format_genres(
                                      effective_genres(episode,
                                                       series_genres)))
    if show_rating:
        label += "  %s" % format_rating(episode)
    return label


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def show_list(heading, labels):
    if not labels:
        xbmcgui.Dialog().ok(ADDON_NAME, "Nothing watched yet in this category.")
        return
    # select() is only used for browsing; picking an entry just closes it.
    xbmcgui.Dialog().select("%s - %s (%d)" % (ADDON_NAME, heading, len(labels)),
                            labels)


def genre_breakdown(items, genre_lookup=None):
    counts = {}
    for item in items:
        for genre in effective_genres(item, genre_lookup):
            counts[genre] = counts.get(genre, 0) + 1
    return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)


def genre_lines(items, genre_lookup=None, percentages=False):
    """Genre breakdown lines - counts, or percentages when the setting is on.

    The percentage is the share of watched items carrying that genre; items
    with several genres count towards each, so the total can exceed 100%.
    """
    if not items:
        return ["Nothing watched in this period."]
    total = len(items)
    if percentages:
        return ["%s: %.0f%%" % (genre, 100.0 * count / total)
                for genre, count in genre_breakdown(items, genre_lookup)]
    return ["%s: %d" % (genre, count)
            for genre, count in genre_breakdown(items, genre_lookup)]


def average_rating(items):
    ratings = [item["CommunityRating"] for item in items
               if item.get("CommunityRating")]
    return (sum(ratings) / len(ratings)) if ratings else None


def show_summary(movies, episodes, recent_movies, recent_episodes,
                 series_genres, days):
    percentages = setting_bool("show_percentages", False)
    total_plays = sum((item.get("UserData") or {}).get("PlayCount", 0)
                      for item in movies + episodes)
    lines = ["[B]Watched totals[/B]",
             "Movies - all time: %d" % len(movies),
             "Movies - last %d days: %d" % (days, len(recent_movies)),
             "Episodes - all time: %d" % len(episodes),
             "Episodes - last %d days: %d" % (days, len(recent_episodes)),
             "Total plays: %d" % total_plays,
             ""]

    if setting_bool("show_avg_ratings"):
        avg_movie = average_rating(movies)
        avg_episode = average_rating(episodes)
        lines.append("[B]Average rating[/B]")
        lines.append("Movies: %s" % (u"★ %.1f" % avg_movie
                                     if avg_movie else "n/a"))
        lines.append("Episodes: %s" % (u"★ %.1f" % avg_episode
                                       if avg_episode else "n/a"))
        lines.append("")

    lines.append("[B]Movies by genre - all time[/B]")
    lines.extend(genre_lines(movies, percentages=percentages))
    lines.append("")

    lines.append("[B]Movies by genre - last %d days[/B]" % days)
    lines.extend(genre_lines(recent_movies, percentages=percentages))
    lines.append("")

    lines.append("[B]Episodes by genre (by show) - all time[/B]")
    lines.extend(genre_lines(episodes, series_genres,
                             percentages=percentages))
    lines.append("")

    lines.append("[B]Episodes by genre (by show) - last %d days[/B]" % days)
    lines.extend(genre_lines(recent_episodes, series_genres,
                             percentages=percentages))

    if setting_bool("show_favourites"):
        favorites = [m for m in movies
                     if (m.get("UserData") or {}).get("IsFavorite")]
        if favorites:
            lines.append("")
            lines.append("[B]Favourite movies[/B]")
            for movie in favorites[:10]:
                lines.append(u"♥ %s" % movie["Name"])

    xbmcgui.Dialog().textviewer("%s - Summary" % ADDON_NAME, "\n".join(lines))


def send_stats_now():
    import stats_sender
    progress = xbmcgui.DialogProgress()
    progress.create(ADDON_NAME, "Sending stats to the website...")
    try:
        payload = stats_sender.send()
    except (stats_sender.SendError, JellyStatError) as err:
        progress.close()
        xbmcgui.Dialog().ok(ADDON_NAME, str(err))
        return
    finally:
        progress.close()
    movies = payload["movies"]
    tv = payload["tv"]
    xbmcgui.Dialog().ok(ADDON_NAME,
                        "Sent - site confirmed ok.\n"
                        "Movies: %d all time, %d last 30 days\n"
                        "Episodes: %d all time, %d last 30 days"
                        % (movies["all_time"]["total"],
                           movies["last_30_days"]["total"],
                           tv["all_time"]["total"],
                           tv["last_30_days"]["total"]))


def show_dashboard_info():
    """Tell the user where the dashboard is, which is the hard part of it.

    The addresses come from the running box, not from the settings, because
    "which IP does this Kodi have" is exactly what somebody standing at the
    TV cannot look up.
    """
    # Imported here so the menu still opens on a box where the service
    # modules are unavailable for any reason.
    import webserver
    if ADDON.getSetting("web_enabled") != "true":
        xbmcgui.Dialog().ok(
            ADDON_NAME,
            "The web dashboard is switched off.\n"
            "Turn it on in settings -> Web dashboard, then come back here "
            "for the address to open on your phone or laptop.")
        return
    port = setting_int("web_port", webserver.DEFAULT_PORT)
    lines = ["Open one of these in a browser on any device on this network:",
             ""]
    lines.extend(webserver.local_urls(port))
    if ADDON.getSetting("web_bind_all") == "false":
        lines.extend([
            "",
            "Only this machine can reach it at the moment: "
            '"Reachable from other machines" is off in settings -> '
            "Web dashboard."])
    if ADDON.getSetting("web_password").strip():
        lines.extend(["", "A password is set, so the page will ask for it "
                          "before showing anything."])
    lines.extend(["",
                  "The dashboard is served by the addon's background "
                  "service, which starts with Kodi. Just switched it on? "
                  "Close settings with OK first, then allow up to five "
                  "minutes for the service to pick the change up (or "
                  "restart Kodi). If nothing answers after that, check the "
                  "Kodi log for [JellyStat] - the usual cause is another "
                  "program already using port %d." % port])
    xbmcgui.Dialog().textviewer("%s - Web dashboard" % ADDON_NAME,
                                "\n".join(lines))


def main():
    progress = xbmcgui.DialogProgress()
    progress.create(ADDON_NAME, "Connecting to Jellyfin...")
    try:
        creds = get_credentials()
        progress.update(20, "Reading watched movies...")
        movies = get_watched(creds, "Movie")
        progress.update(50, "Reading watched episodes...")
        episodes = get_watched(creds, "Episode")
        progress.update(80, "Reading show genres...")
        series_genres = get_series_genres(creds)
    except JellyStatError as err:
        progress.close()
        xbmcgui.Dialog().ok(ADDON_NAME, str(err))
        return
    finally:
        progress.close()

    days = setting_int("recent_days", 30)
    list_ratings = setting_bool("show_list_ratings")
    recent_movies = filter_recent(movies, days)
    recent_episodes = filter_recent(episodes, days)

    menu = ["Summary",
            "Movies - all time (%d)" % len(movies),
            "Movies - last %d days (%d)" % (days, len(recent_movies)),
            "Episodes - all time (%d)" % len(episodes),
            "Episodes - last %d days (%d)" % (days, len(recent_episodes)),
            "Send stats to website now",
            "Web dashboard address"]
    while True:
        choice = xbmcgui.Dialog().select(ADDON_NAME, menu)
        if choice == -1:
            break
        if choice == 0:
            show_summary(movies, episodes, recent_movies, recent_episodes,
                         series_genres, days)
        elif choice == 1:
            show_list("Movies - all time",
                      [movie_label(m, list_ratings) for m in movies])
        elif choice == 2:
            show_list("Movies - last %d days" % days,
                      [movie_label(m, list_ratings) for m in recent_movies])
        elif choice == 3:
            show_list("Episodes - all time",
                      [episode_label(e, series_genres, list_ratings)
                       for e in episodes])
        elif choice == 4:
            show_list("Episodes - last %d days" % days,
                      [episode_label(e, series_genres, list_ratings)
                       for e in recent_episodes])
        elif choice == 5:
            send_stats_now()
        elif choice == 6:
            show_dashboard_info()


if __name__ == "__main__":
    # "dashboard_info" is the settings screen's "Web dashboard address"
    # button (RunScript with an argument); it jumps straight to the dialog
    # without connecting to Jellyfin, so it answers instantly even when the
    # server is down.
    if len(sys.argv) > 1 and sys.argv[1] == "dashboard_info":
        show_dashboard_info()
    else:
        main()
