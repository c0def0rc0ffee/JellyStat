# -*- coding: utf-8 -*-
"""Compute the movie-genre snapshot and POST it to the website.

Shared by service.py (automatic daily send when Kodi loads) and main.py
(the "Send stats to website now" menu entry).

Rules this producer settles so the site never interprets:
- Watched = Jellyfin's Played flag, for the user signed in with the Jellyfin
  for Kodi addon.
- Last 30 days = rolling 30x24h window ending at send time (UTC), based on
  each movie's LastPlayedDate - approximate, since Jellyfin only keeps the
  most recent play date per item.
- Genres are alias-merged (Sci-Fi -> Science Fiction etc.), movies with no
  genre are reported as "Unknown", and a multi-genre movie counts once per
  genre - so genre counts can sum to more than `total` (distinct movies).
  Since v0.15.0 that applies to science fiction too: it used to swallow a
  title's other genres, and no longer does.
- Genre lists sorted descending by count, ties alphabetical.
"""

import json
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timezone

WINDOW_DAYS = 30

import xbmc
import xbmcaddon

import main as core

ADDON_ID = "script.jellystat"

# Genre aliasing lives in main.py so the on-screen stats and the website
# payload always agree.
GENRE_ALIASES = core.GENRE_ALIASES
canonical_genre = core.canonical_genre


class SendError(Exception):
    """Any problem that should fail this send (retried at the next check)."""


def log(message, level=xbmc.LOGINFO):
    xbmc.log("[JellyStat] %s" % message, level)


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------

def _plays(item):
    """Lifetime play count for an item, floored at 1 (it is marked played)."""
    plays = (item.get("UserData") or {}).get("PlayCount") or 0
    return max(int(plays), 1)


def _genre_tally(items, genre_lookup=None, use_plays=False,
                 primary_only=False):
    """Generic genre tally -> sorted [{genre, count|plays, percent}].

    - default: each item counts once per genre it carries (sum >= total).
    - use_plays: weighted by the item's lifetime play count, so rewatches
      count; percent base is total plays.
    - primary_only: only the item's FIRST listed genre counts, so each item
      counts exactly once and percentages sum to ~100.
    Genres come from core.effective_genres: aliased and deduped, with every
    genre a title carries counting.
    Sorted descending by the metric, ties alphabetical.
    """
    counts = {}
    display = {}
    base = 0
    for item in items:
        genres = core.effective_genres(item, genre_lookup)
        if primary_only:
            genres = genres[:1]
        weight = _plays(item) if use_plays else 1
        base += weight
        for canon in genres:
            key = canon.lower()
            display.setdefault(key, canon)
            counts[key] = counts.get(key, 0) + weight
    metric = "plays" if use_plays else "count"
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], display[kv[0]]))
    return [{"genre": display[key], metric: value,
             "percent": round(100.0 * value / base, 1) if base else 0.0}
            for key, value in ordered]


def genre_counts(items, genre_lookup=None):
    """Original view: distinct items per genre (kept for compatibility)."""
    return _genre_tally(items, genre_lookup)


def _played_dates(items):
    dates = []
    for item in items:
        played = core.parse_played_date(
            (item.get("UserData") or {}).get("LastPlayedDate"))
        if played:
            dates.append(played)
    return dates


def _rates(items, window_days=None):
    """Watch-pace numbers for a window: (per_day, avg_gap_minutes).

    per_day = count / window length in days. For last_30_days the length is
    the fixed window; for all time (window_days=None) it spans the earliest
    last-played date to now, floored at 1 day so a brand-new library can't
    produce a silly rate.

    avg_gap_minutes = first-to-last played span in minutes / count ("a movie
    every N minutes"); null with fewer than 2 dated items. All-time caveat:
    Jellyfin keeps only each item's most recent play date, so rewatches and
    bulk-marked libraries shrink the apparent span - approximate by nature.
    """
    count = len(items)
    dates = _played_dates(items)
    if window_days is not None:
        span_days = float(window_days)
    elif dates:
        now = datetime.utcnow()
        span_days = max((now - min(dates)).total_seconds() / 86400.0, 1.0)
    else:
        span_days = None
    per_day = round(count / span_days, 2) if (count and span_days) else 0.0
    avg_gap = None
    if len(dates) >= 2:
        span_minutes = (max(dates) - min(dates)).total_seconds() / 60.0
        if span_minutes > 0:
            avg_gap = int(round(span_minutes / count))
    return per_day, avg_gap


def _window(items, genre_lookup, per_day, avg_gap):
    return {
        "total": len(items),
        "plays": sum(_plays(item) for item in items),
        "per_day": per_day,
        "avg_gap_minutes": avg_gap,
        "genres": genre_counts(items, genre_lookup),
        "genres_by_plays": _genre_tally(items, genre_lookup, use_plays=True),
        "genres_primary": _genre_tally(items, genre_lookup,
                                       primary_only=True),
    }


def _media_block(all_time_items, recent_items, genre_lookup=None):
    recent_per_day, recent_gap = _rates(recent_items, WINDOW_DAYS)
    all_per_day, all_gap = _rates(all_time_items)
    return {
        "last_30_days": _window(recent_items, genre_lookup,
                                recent_per_day, recent_gap),
        "all_time": _window(all_time_items, genre_lookup,
                            all_per_day, all_gap),
    }


def build_payload(all_time_movies, recent_movies,
                  all_time_episodes, recent_episodes, series_genres):
    movies = _media_block(all_time_movies, recent_movies)
    tv = _media_block(all_time_episodes, recent_episodes, series_genres)
    return {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "source": "main-account",
        # "windows" is the original movies-only key; kept as a duplicate of
        # "movies" so existing site code keeps working.
        "windows": movies,
        "movies": movies,
        "tv": tv,
    }


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------

def clean_url(raw):
    """Rescue a URL polluted by the settings keyboard.

    Old-style settings dialogs could leave hint text in front of what was
    typed; keep everything from the first 'http' onwards and trim whitespace
    (including stray newlines from on-screen keyboards).
    """
    url = (raw or "").strip()
    idx = url.find("http")
    if idx > 0:
        url = url[idx:]
    return url.strip()


def describe_http_error(code, url):
    """Error text with fix instructions, shown in the same popup."""
    if code == 401:
        return ("The site rejected the auth token (HTTP 401).\n"
                "Fix: settings -> Website stats -> Auth token must exactly "
                "match the token in the site's config - it is case "
                "sensitive, so re-enter it carefully with no spaces before "
                "or after.")
    if code == 405:
        return ("The site answered 'method not allowed' (HTTP 405) for\n%s\n"
                "This usually means the URL is being redirected (a www. "
                "prefix or http vs https) and the data gets lost on the "
                "way.\nFix: settings -> Website stats -> Website endpoint "
                "URL must be the exact address with no www, e.g. "
                "http://yoursite/api/genre-stats.php" % url)
    if code == 404:
        return ("The site says that page does not exist (HTTP 404) at\n%s\n"
                "Fix: check the path part of the endpoint URL in settings "
                "-> Website stats (it should end /api/genre-stats.php)."
                % url)
    if code == 403:
        return ("The site refused this device (HTTP 403).\n"
                "Fix: the web server is only accepting requests from its own "
                "machine. Allow this device in the server's access rules, "
                "then try again.")
    return ("Site returned HTTP %d for\n%s\n"
            "Check the endpoint URL in settings -> Website stats; the "
            "full response is in the Kodi log (search [JellyStat])."
            % (code, url))


def post(url, token, payload, ignore_ssl):
    headers = {"Content-Type": "application/json",
               "Accept": "application/json"}
    if token:
        headers["X-Auth-Token"] = token
    request = urllib.request.Request(url,
                                     data=json.dumps(payload).encode("utf-8"),
                                     headers=headers)
    context = None
    if url.startswith("https") and ignore_ssl:
        # Deliberate - a home server with a self-signed certificate is the
        # ordinary reason - but it means anything on the path can read the
        # token and the payload, and alter them. Worth a line in the log
        # rather than being quietly insecure.
        log("Sending with TLS verification turned off: the token and "
            "payload can be read and altered in transit.", xbmc.LOGWARNING)
        context = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(request, timeout=60,
                                    context=context) as response:
            status = response.status
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        try:
            log("Site error body: %r" % err.read().decode("utf-8", "replace")[:500],
                xbmc.LOGWARNING)
        except Exception:
            pass
        raise SendError(describe_http_error(err.code, url))
    except (urllib.error.URLError, OSError) as err:
        raise SendError("Could not reach the site at %s\n%s" % (url, err))
    try:
        body = json.loads(raw) if raw.strip() else None
    except ValueError:
        raise SendError("Site response was not JSON: %r" % raw[:200])
    if status != 200 or not (isinstance(body, dict) and body.get("ok") is True):
        raise SendError("Unexpected site response (HTTP %d): %r"
                        % (status, body))


def send():
    """Compute and send the full snapshot. Returns the payload on success."""
    # Explicit id: never rely on context resolution from service vs script.
    addon = xbmcaddon.Addon(ADDON_ID)
    raw_url = addon.getSetting("endpoint_url")
    url = clean_url(raw_url)
    log("send(): v%s endpoint_url=%r (cleaned: %r)"
        % (addon.getAddonInfo("version"), raw_url, url))
    if not url:
        raise SendError(
            "No website endpoint URL found "
            "(addon v%s read endpoint_url=%r).\n"
            'It belongs in settings -> "Website stats" -> '
            '"Website endpoint URL" - NOT the Jellyfin "Server URL" field '
            "on the first page - and settings must be closed with OK."
            % (addon.getAddonInfo("version"), raw_url))
    if not url.startswith(("http://", "https://")):
        raise SendError(
            "The saved endpoint URL does not look like a URL: %r\n"
            "It should start with http:// - please re-enter it."
            % raw_url)
    if url != raw_url:
        # Self-heal: store the cleaned value so the settings box shows it.
        addon.setSetting("endpoint_url", url)
        log("Cleaned a polluted endpoint URL and saved it back: %r" % url,
            xbmc.LOGWARNING)
    token = addon.getSetting("site_token").strip()

    creds = core.get_credentials()
    movies = core.get_watched(creds, "Movie")
    episodes = core.get_watched(creds, "Episode")
    series_genres = core.get_series_genres(creds)
    recent_movies = core.filter_recent(movies)
    recent_episodes = core.filter_recent(episodes)

    payload = build_payload(movies, recent_movies,
                            episodes, recent_episodes, series_genres)
    post(url, token, payload, addon.getSetting("ignore_ssl") == "true")

    addon.setSetting("last_send_date", datetime.now().strftime("%Y-%m-%d"))
    log("Sent snapshot to %s - movies %d/%d, episodes %d/%d (all time / "
        "last 30 days)" % (url, len(movies), len(recent_movies),
                           len(episodes), len(recent_episodes)))
    return payload
