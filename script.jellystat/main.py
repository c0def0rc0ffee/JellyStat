# -*- coding: utf-8 -*-
"""
<summary>
JellyStat: watch statistics from your Jellyfin server.
</summary>
<remarks>
Talks to the Jellyfin server API and shows what you have watched,
each item's genre(s) and rating. Credentials are auto-discovered from
the Jellyfin for Kodi addon (plugin.video.jellyfin) when installed,
otherwise the server URL and API key from this addon's settings are used.
</remarks>
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
    """
    <summary>
    Raised for any problem talking to the Jellyfin server.
    </summary>
    """


def setting_bool(name, default=True):
    """
    <summary>
    A boolean addon setting, with the default for an unset one.
    </summary>
    <param name="name">Setting id.</param>
    <param name="default">Returned when the setting is empty.</param>
    """
    value = ADDON.getSetting(name)
    if value == "":
        return default
    return value == "true"


def setting_int(name, default):
    """
    <summary>
    An integer addon setting, with the default when it is unset or not a number.
    </summary>
    <param name="name">Setting id.</param>
    <param name="default">Returned when the value will not parse.</param>
    """
    try:
        return int(ADDON.getSetting(name))
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def credentials_from_jellyfin_addon():
    """
    <summary>
    Reuse the login the Jellyfin for Kodi addon already has.
    </summary>
    """
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
    """
    <summary>
    Fall back to the server URL + API key configured in addon settings.
    </summary>
    """
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
    """
    <summary>
    The first login that works: the Jellyfin for Kodi addon's, else the addon settings'.
    </summary>
    <returns>A credentials dict.</returns>
    <exception cref="JellyStatError">When neither is available.</exception>
    """
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
    """
    <summary>
    One GET against the Jellyfin API, decoded as JSON.
    </summary>
    <param name="base">Server URL without a trailing slash.</param>
    <param name="token">API key or access token.</param>
    <param name="path">Path from the server root.</param>
    <param name="params">Query parameters, or None.</param>
    <returns>The decoded reply.</returns>
    <exception cref="JellyStatError">A rejected login (401), any other HTTP error, or an unreachable server.</exception>
    <remarks>
    TLS verification is skipped when the ignore_ssl setting is on.
    </remarks>
    """
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
    """
    <summary>
    Return all played items of the given type for the signed-in user.
    </summary>
    """
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
    """
    <summary>
    Every series in the library, with the fields the mirror stores.
    </summary>
    <remarks>
    Shows are items in their own right: they carry genres their episodes
    lack, a rating of their own, and provider ids that imported data is
    matched on. get_series_genres() is the older, narrower view of this.
    </remarks>
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
    """
    <summary>
    Map series id -> genres, since episodes carry no genres of their own.
    </summary>
    """
    if series is None:
        series = get_series(creds)
    return {item["Id"]: item.get("Genres", []) for item in series}


def parse_played_date(value):
    """
    <summary>
    Parse Jellyfin's LastPlayedDate ('2026-06-15T20:31:12.0000000Z').
    </summary>
    """
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
    """
    <summary>
    Keep only items last played within the given number of days.
    </summary>
    """
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

# One label in, one *or more* genres out. Television and film are tagged
# from different vocabularies: TMDb gives films "Action" and "Adventure"
# separately but television a single "Action & Adventure" - and left alone
# that splits a genre's evidence in three: a show tagged the combined way
# counts toward neither of the two the films use. Everything downstream
# (stats, the taste profile, the genre filters) reads through
# effective_genres, so aliasing here is the one place that fixes all of it.
GENRE_ALIASES = {
    "sci-fi": "Science Fiction",
    "sci fi": "Science Fiction",
    "scifi": "Science Fiction",
    "science-fiction": "Science Fiction",
    "science fiction": "Science Fiction",
    # A combined label counts in every genre it names, with none of them
    # dropped and none of them guessed at. The alternative (picking the
    # "real" one) is the addon deciding a show is science fiction and not
    # fantasy on no evidence, and it silently loses a genre's worth of
    # titles from every count, filter and taste profile downstream.
    "sci-fi & fantasy": ("Science Fiction", "Fantasy"),
    "sci fi & fantasy": ("Science Fiction", "Fantasy"),
    "science fiction & fantasy": ("Science Fiction", "Fantasy"),
    "action & adventure": ("Action", "Adventure"),
    "action and adventure": ("Action", "Adventure"),
    "war & politics": ("War", "Politics"),
    "war and politics": ("War", "Politics"),
    "animated": "Animation",
    "kids": "Family",
    "children": "Family",
}


def genre_aliases(name):
    """
    <summary>
    Every genre a label stands for, in order. Always a tuple.
    </summary>
    """
    name = (name or "").strip()
    if not name:
        return ("Unknown",)
    mapped = GENRE_ALIASES.get(name.lower(), name)
    return (mapped,) if isinstance(mapped, str) else tuple(mapped)


def canonical_genre(name):
    """
    <summary>
    The single genre a label reduces to.
    </summary>
    <remarks>
    Where a label stands for more than one, this is the first, the one a
    "primary genre only" breakdown should use. Callers that want all of
    them want genre_aliases, or effective_genres for a whole item.
    </remarks>
    """
    return genre_aliases(name)[0]


def effective_genres(item, genre_lookup=None):
    """
    <summary>
    An item's genres, aliased and deduped.
    </summary>
    <remarks>
    Every genre a title carries counts, so a science fiction horror film is
    both. Counts across genres therefore sum to more than the number of
    titles, which is why the genre views offer a "primary only" mode when a
    breakdown that adds up to 100% is what is wanted.

    A label may stand for more than one genre: television's "Action &
    Adventure" is the two genres film tags separately, and is expanded to
    all of them, deduped against whatever else the item carries.

    (Until v0.15.0 a "sci-fi dominance" rule collapsed anything science
    fiction down to that single genre. It suppressed 1,771 genre tags on a
    2,200 film library: 524 films stopped being Action, while every other
    multi-genre title still counted in each of its genres, so it bought no
    consistency. Primary-only does that job properly.)

    No genres at all -> ["Unknown"]. For episodes pass genre_lookup
    (series id -> genres); episodes carry no genres of their own.
    </remarks>
    """
    if genre_lookup is not None:
        raw = genre_lookup.get(item.get("SeriesId")) or []
    else:
        raw = item.get("Genres") or []
    genres = []
    seen = set()
    for genre in raw:
        # One label can stand for several genres, so this expands rather
        # than maps. Dedup still runs across the lot: a film tagged both
        # "Action & Adventure" and "Action" is Action once, not twice.
        for canon in genre_aliases(genre):
            if canon.lower() not in seen:
                seen.add(canon.lower())
                genres.append(canon)
    return genres or ["Unknown"]


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_genres(genres):
    """
    <summary>
    Genres joined for a list label, or No genre.
    </summary>
    <param name="genres">A list of names.</param>
    <returns>A string.</returns>
    """
    return ", ".join(genres) if genres else "No genre"


def format_rating(item):
    """
    <summary>
    Community rating, critic rating and favourite mark for a list label, or Not rated.
    </summary>
    <param name="item">A Jellyfin item.</param>
    <returns>A string.</returns>
    """
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
    """
    <summary>
    One line for a film: title, year, genres and optionally its ratings.
    </summary>
    <param name="movie">A Jellyfin movie item.</param>
    <param name="show_rating">Append the ratings.</param>
    <returns>The label.</returns>
    """
    year = movie.get("ProductionYear")
    title = "%s (%s)" % (movie["Name"], year) if year else movie["Name"]
    label = "%s  [%s]" % (title, format_genres(effective_genres(movie)))
    if show_rating:
        label += "  %s" % format_rating(movie)
    return label


def episode_label(episode, series_genres, show_rating=True):
    """
    <summary>
    One line for an episode: show, season and episode code, title, genres and optionally its ratings.
    </summary>
    <param name="episode">A Jellyfin episode item.</param>
    <param name="series_genres">Series id to genres, since episodes carry none of their own.</param>
    <param name="show_rating">Append the ratings.</param>
    <returns>The label.</returns>
    """
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
    """
    <summary>
    Show labels in a select dialog for browsing, or an OK dialog when there is nothing.
    </summary>
    <param name="heading">Dialog heading.</param>
    <param name="labels">The lines.</param>
    <remarks>
    Picking an entry just closes the dialog.
    </remarks>
    """
    if not labels:
        xbmcgui.Dialog().ok(ADDON_NAME, "Nothing watched yet in this category.")
        return
    # select() is only used for browsing; picking an entry just closes it.
    xbmcgui.Dialog().select("%s - %s (%d)" % (ADDON_NAME, heading, len(labels)),
                            labels)


def genre_breakdown(items, genre_lookup=None):
    """
    <summary>
    Count items per effective genre, most common first.
    </summary>
    <param name="items">Jellyfin items.</param>
    <param name="genre_lookup">Series id to genres, for episodes.</param>
    <returns>A list of (genre, count).</returns>
    """
    counts = {}
    for item in items:
        for genre in effective_genres(item, genre_lookup):
            counts[genre] = counts.get(genre, 0) + 1
    return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)


def genre_lines(items, genre_lookup=None, percentages=False):
    """
    <summary>
    Genre breakdown lines: counts, or percentages when the setting is on.
    </summary>
    <remarks>
    The percentage is the share of watched items carrying that genre; items
    with several genres count towards each, so the total can exceed 100%.
    </remarks>
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
    """
    <summary>
    Mean community rating of the items that have one, or None.
    </summary>
    <param name="items">Jellyfin items.</param>
    <returns>A float or None.</returns>
    """
    ratings = [item["CommunityRating"] for item in items
               if item.get("CommunityRating")]
    return (sum(ratings) / len(ratings)) if ratings else None


def show_summary(movies, episodes, recent_movies, recent_episodes,
                 series_genres, days):
    """
    <summary>
    Show the watched totals, average ratings, genre breakdowns and favourites in a text viewer.
    </summary>
    <param name="movies">All watched films.</param>
    <param name="episodes">All watched episodes.</param>
    <param name="recent_movies">Films watched within the window.</param>
    <param name="recent_episodes">Episodes watched within the window.</param>
    <param name="series_genres">Series id to genres.</param>
    <param name="days">The window length.</param>
    <remarks>
    Percentages and the optional sections follow the display settings.
    </remarks>
    """
    percentages = setting_bool("show_percentages", False)
    total_plays = sum((item.get("UserData") or {}).get("PlayCount", 0)
                      for item in movies + episodes)
    lines = ["[B]Watched totals[/B]",
             "Movies, all time: %d" % len(movies),
             "Movies, last %d days: %d" % (days, len(recent_movies)),
             "Episodes, all time: %d" % len(episodes),
             "Episodes, last %d days: %d" % (days, len(recent_episodes)),
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

    lines.append("[B]Movies by genre, all time[/B]")
    lines.extend(genre_lines(movies, percentages=percentages))
    lines.append("")

    lines.append("[B]Movies by genre, last %d days[/B]" % days)
    lines.extend(genre_lines(recent_movies, percentages=percentages))
    lines.append("")

    lines.append("[B]Episodes by genre (by show), all time[/B]")
    lines.extend(genre_lines(episodes, series_genres,
                             percentages=percentages))
    lines.append("")

    lines.append("[B]Episodes by genre (by show), last %d days[/B]" % days)
    lines.extend(genre_lines(recent_episodes, series_genres,
                             percentages=percentages))

    if setting_bool("show_favourites"):
        favourites = [m for m in movies
                     if (m.get("UserData") or {}).get("IsFavorite")]
        if favourites:
            lines.append("")
            lines.append("[B]Favourite movies[/B]")
            for movie in favourites[:10]:
                lines.append(u"♥ %s" % movie["Name"])

    xbmcgui.Dialog().textviewer("%s: Summary" % ADDON_NAME, "\n".join(lines))


def send_stats_now():
    """
    <summary>
    Send the snapshot to the website behind a progress dialog and report the outcome.
    </summary>
    """
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
                        "Sent: site confirmed ok.\n"
                        "Movies: %d all time, %d last 30 days\n"
                        "Episodes: %d all time, %d last 30 days"
                        % (movies["all_time"]["total"],
                           movies["last_30_days"]["total"],
                           tv["all_time"]["total"],
                           tv["last_30_days"]["total"]))


def show_dashboard_info():
    """
    <summary>
    Tell the user where the dashboard is, which is the hard part of it.
    </summary>
    <remarks>
    The addresses come from the running box, not from the settings, because
    "which IP does this Kodi have" is exactly what somebody standing at the
    TV cannot look up.
    </remarks>
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
    elif webserver.lan_refused():
        lines.extend([
            "",
            "Only this machine can reach it at the moment. It is set to be "
            "reachable from other machines, but with no password anything "
            "on your network could read your whole viewing history and "
            "control playback, so it is being kept to this machine until "
            "there is one.",
            "",
            "Set a password in settings -> Web dashboard and the addresses "
            "above will start working, within a few seconds."])
    if ADDON.getSetting("web_password").strip():
        lines.extend(["", "A password is set, so the page will ask for it "
                          "before showing anything."])
    lines.extend(["",
                  "The dashboard is served by the addon's background "
                  "service, which starts with Kodi. Just switched it on? "
                  "Close settings with OK first, then allow up to five "
                  "minutes for the service to pick the change up (or "
                  "restart Kodi). If nothing answers after that, check the "
                  "Kodi log for [JellyStat]: the usual cause is another "
                  "program already using port %d." % port])
    xbmcgui.Dialog().textviewer("%s: Web dashboard" % ADDON_NAME,
                                "\n".join(lines))


def main():
    """
    <summary>
    The script entry point: read the watched library from Jellyfin, then offer the summary, the lists, a send and the dashboard address in a menu until the user backs out.
    </summary>
    """
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
            "Movies: all time (%d)" % len(movies),
            "Movies: last %d days (%d)" % (days, len(recent_movies)),
            "Episodes: all time (%d)" % len(episodes),
            "Episodes: last %d days (%d)" % (days, len(recent_episodes)),
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
            show_list("Movies: all time",
                      [movie_label(m, list_ratings) for m in movies])
        elif choice == 2:
            show_list("Movies: last %d days" % days,
                      [movie_label(m, list_ratings) for m in recent_movies])
        elif choice == 3:
            show_list("Episodes: all time",
                      [episode_label(e, series_genres, list_ratings)
                       for e in episodes])
        elif choice == 4:
            show_list("Episodes: last %d days" % days,
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
