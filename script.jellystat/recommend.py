# -*- coding: utf-8 -*-
"""Recommendations and similar-title lookups, from the user's own history.

No external service and no tracking: the taste profile is computed from the
mirror (what was actually watched, weighted by plays), and candidates come
from the Jellyfin library itself - including its unwatched side, which is
the one thing the mirror deliberately does not hold. The full catalog is
fetched at most once an hour and kept in memory.

Scoring is deliberately explainable:

- A recommendation score is the sum of the viewer's genre weights over the
  candidate's genres, times a quality factor from the community rating. A
  sci-fi-heavy history therefore surfaces well-rated sci-fi first, not
  whatever a black-box model likes this week.
- Similarity is genre overlap (Jaccard) plus a small closeness-in-year
  bonus and a small rating tiebreak.

"Seen" for a film is Jellyfin's Played flag; for a show it is "any episode
in the mirror". Callers choose whether seen titles are included - that
choice is the dashboard's per-medium setting.

If Jellyfin is unreachable the catalog cannot refresh; with include_seen
the mirror alone still yields (rewatch) suggestions, otherwise the caller
gets the connection error to show.
"""

import json
import threading
import time

import xbmc

import library
import main as core

CATALOG_TTL_S = 3600
RECOMMEND_LIMIT = 20
SIMILAR_LIMIT = 12

_lock = threading.Lock()
_cache = {"at": 0.0, "movies": None, "series": None}


def log(message, level=xbmc.LOGINFO):
    xbmc.log("[JellyStat] %s" % message, level)


# ---------------------------------------------------------------------------
# Catalog (watched and unwatched, from Jellyfin)
# ---------------------------------------------------------------------------

def _fetch(creds, item_type):
    result = core.api_get(creds["base"], creds["token"],
                          "/Users/%s/Items" % creds["user_id"], {
                              "IncludeItemTypes": item_type,
                              "Recursive": "true",
                              "Fields": "Genres,CommunityRating,"
                                        "ProductionYear",
                          })
    return result.get("Items", [])


def catalog():
    """(movies, series) - the whole library, cached for an hour."""
    with _lock:
        fresh = (_cache["movies"] is not None
                 and (time.time() - _cache["at"]) < CATALOG_TTL_S)
        if fresh:
            return _cache["movies"], _cache["series"]
        creds = core.get_credentials()
        movies = _fetch(creds, "Movie")
        series = _fetch(creds, "Series")
        _cache.update(at=time.time(), movies=movies, series=series)
        log("Catalog fetched: %d movies, %d series" % (len(movies),
                                                       len(series)))
        return movies, series


# ---------------------------------------------------------------------------
# Taste profile and watched sets, from the mirror
# ---------------------------------------------------------------------------

def _mirror_rows(media):
    connection = library.connect()
    rows = connection.execute(
        "SELECT genres, play_count, series_name FROM items WHERE media = ?",
        (media,)).fetchall()
    connection.close()
    return rows


def _genres_of(raw_json):
    try:
        return core.effective_genres({"Genres": json.loads(raw_json or "[]")})
    except ValueError:
        return ["Unknown"]


def profile(media):
    """Genre -> share of viewing, for 'movies' or 'tv'. Sums to ~1."""
    weights = {}
    total = 0.0
    if media == "movies":
        for genres, plays, _ in _mirror_rows("movie"):
            weight = max(int(plays or 0), 1)
            for genre in _genres_of(genres):
                weights[genre.lower()] = weights.get(genre.lower(), 0.0) \
                    + weight
                total += weight
    else:
        # One vote per watched episode, so a 200-episode habit outweighs a
        # single pilot - which is what a taste profile should say.
        for genres, _, _ in _mirror_rows("episode"):
            for genre in _genres_of(genres):
                weights[genre.lower()] = weights.get(genre.lower(), 0.0) + 1
                total += 1
    if not total:
        return {}
    return {genre: weight / total for genre, weight in weights.items()}


def watched_shows():
    """Lowercased names of every show with at least one watched episode."""
    return {(name or "").strip().lower()
            for _, _, name in _mirror_rows("episode") if name}


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------

def _entry(item, seen, score):
    return {
        "id": item.get("Id"),
        "name": item.get("Name") or "?",
        "year": item.get("ProductionYear"),
        "rating": item.get("CommunityRating"),
        "genres": core.effective_genres(item),
        "seen": seen,
        "score": round(score, 4),
    }


def _seen_movie(item):
    return bool((item.get("UserData") or {}).get("Played"))


def recommendations(media, include_seen, limit=RECOMMEND_LIMIT, offset=0,
                    genres=None):
    """Ranked suggestions for 'movies' or 'tv'.

    `genres` narrows the result to titles carrying at least one of the named
    genres (case-insensitive, aliased the same way as everything else). It
    filters rather than re-ranks: the order still reflects overall taste, so
    picking "Horror" answers "the horror you would most likely enjoy", not
    "your favourite films that happen to be horror".

    `offset` pages through the full ranked list. The counts returned describe
    the list after filtering, so the page can say 21-40 of 137 honestly.
    """
    taste = profile(media)
    try:
        movies, series = catalog()
        if media == "movies":
            candidates = [(item, _seen_movie(item)) for item in movies]
        else:
            seen_set = watched_shows()
            candidates = [(item, (item.get("Name") or "").strip().lower()
                           in seen_set) for item in series]
        offline = False
    except core.JellyStatError:
        if not include_seen:
            raise  # nothing new can be suggested without the library
        # Rewatch-only fallback: the mirror is the seen side of the catalog.
        candidates = [(item, True) for item in _mirror_as_items(media)]
        offline = True
    scored = []
    for item, seen in candidates:
        if seen and not include_seen:
            continue
        # Not `genres`: that is this function's filter parameter, and
        # rebinding it here silently turned the filter into "whatever the
        # last candidate happened to be".
        item_genres = core.effective_genres(item)
        affinity = sum(taste.get(genre.lower(), 0.0)
                       for genre in item_genres)
        if affinity <= 0:
            continue
        quality = (item.get("CommunityRating") or 5.5) / 10.0
        scored.append(_entry(item, seen, affinity * quality))
    scored.sort(key=lambda entry: (-entry["score"],
                                   -(entry["rating"] or 0),
                                   entry["name"]))

    # The genre list offered by the page comes from what is actually
    # recommendable, so it never presents a filter that yields nothing.
    tally = {}
    for entry in scored:
        for genre in entry["genres"]:
            tally[genre] = tally.get(genre, 0) + 1
    available = [{"genre": genre, "count": count}
                 for genre, count in sorted(tally.items(),
                                            key=lambda kv: (-kv[1], kv[0]))]

    wanted = {name.strip().lower() for name in (genres or []) if name.strip()}
    if wanted:
        scored = [entry for entry in scored
                  if wanted & {g.lower() for g in entry["genres"]}]

    offset = max(0, int(offset or 0))
    return {
        "items": scored[offset:offset + limit],
        "total": len(scored),
        "offset": offset,
        "limit": limit,
        "genres_available": available,
        "genres_selected": sorted(wanted),
        "offline": offline,
        "profile": sorted(taste.items(), key=lambda kv: -kv[1])[:5],
    }


def _mirror_as_items(media):
    """Mirror rows reshaped as catalog items, for the offline fallback."""
    if media == "movies":
        movies, _, _ = library.as_jellyfin_items()
        return movies
    _, episodes, series_genres = library.as_jellyfin_items()
    shows = {}
    for episode in episodes:
        name = episode.get("SeriesName")
        if not name or name in shows:
            continue
        shows[name] = {
            "Id": episode.get("SeriesId"),
            "Name": name,
            "Genres": series_genres.get(episode.get("SeriesId")) or [],
            "CommunityRating": episode.get("CommunityRating"),
        }
    return list(shows.values())


# ---------------------------------------------------------------------------
# Similar titles
# ---------------------------------------------------------------------------

def _similarity(target_genres, target_year, item):
    genres = set(g.lower() for g in core.effective_genres(item))
    overlap = genres & target_genres
    if not overlap:
        return 0.0
    jaccard = len(overlap) / len(genres | target_genres)
    year_bonus = 0.0
    year = item.get("ProductionYear")
    if year and target_year:
        year_bonus = max(0.0, 1.0 - abs(year - target_year) / 30.0) * 0.25
    rating_bonus = (item.get("CommunityRating") or 0) / 40.0
    return jaccard + year_bonus + rating_bonus


def similar(media, item_id=None, name=None, include_seen=True,
            limit=SIMILAR_LIMIT):
    """Titles like the given one, honouring the seen/new setting."""
    try:
        movies, series = catalog()
        offline = False
    except core.JellyStatError:
        if not include_seen:
            raise
        movies = _mirror_as_items("movies")
        series = _mirror_as_items("tv")
        offline = True
    if media == "movie":
        pool = [(item, _seen_movie(item) if not offline else True)
                for item in movies]
        target = next((item for item, _ in pool
                       if item.get("Id") == item_id), None)
        if target is None and name:
            target = next((item for item, _ in pool
                           if (item.get("Name") or "").lower()
                           == name.lower()), None)
    else:
        seen_set = watched_shows()
        pool = [(item, (item.get("Name") or "").strip().lower() in seen_set)
                for item in series]
        target = next((item for item, _ in pool
                       if (item.get("Name") or "").strip().lower()
                       == (name or "").strip().lower()), None)
    if target is None:
        return {"items": [], "offline": offline}
    target_genres = set(g.lower() for g in core.effective_genres(target))
    target_year = target.get("ProductionYear")
    scored = []
    for item, seen in pool:
        if item is target or item.get("Id") == target.get("Id"):
            continue
        if seen and not include_seen:
            continue
        score = _similarity(target_genres, target_year, item)
        if score <= 0:
            continue
        scored.append(_entry(item, seen, score))
    scored.sort(key=lambda entry: (-entry["score"], entry["name"]))
    return {"items": scored[:limit], "offline": offline}


# ---------------------------------------------------------------------------
# Detail for titles the mirror does not hold
# ---------------------------------------------------------------------------

def catalog_detail(media, item_id=None, name=None):
    """A title's details straight from the catalog, watched or not.

    The mirror only holds what has been watched, so a recommendation for
    something new has no row there. This gives the item page enough to show
    anyway - the metadata Jellyfin has, and an honest "not watched yet"
    instead of play statistics.
    """
    movies, series = catalog()
    pool = movies if media == "movie" else series
    target = None
    if item_id:
        target = next((item for item in pool if item.get("Id") == item_id),
                      None)
    if target is None and name:
        wanted = name.strip().lower()
        target = next((item for item in pool
                       if (item.get("Name") or "").strip().lower() == wanted),
                      None)
    if target is None:
        return None
    user_data = target.get("UserData") or {}
    return {
        "id": target.get("Id"),
        "name": target.get("Name") or "?",
        "year": target.get("ProductionYear"),
        "genres": core.effective_genres(target),
        "rating": target.get("CommunityRating"),
        "critic": target.get("CriticRating"),
        "favourite": bool(user_data.get("IsFavorite")),
        "play_count": user_data.get("PlayCount") or 0,
        "last_played": None,
        "first_seen": None,
        "present": True,
        "watched": False,
        "sittings": [],
        "episodes_watched": 0,
        "plays": 0,
        "avg_rating": None,
        "seasons": [],
        "episodes": [],
    }
