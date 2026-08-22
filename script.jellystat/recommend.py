# -*- coding: utf-8 -*-
"""Recommendations and similar-title lookups, from the user's own history.

No external service and no tracking: the taste profile is computed from the
mirror (what was actually watched, weighted by plays), and candidates come
from the Jellyfin library itself - including its unwatched side, which is
the one thing the mirror deliberately does not hold. The full catalog is
fetched at most once an hour and kept in memory.

Scoring is deliberately explainable, and rests on two different signals
that must not be confused with each other:

- **Exposure** - what you watch, weighted by plays. It says a sci-fi-heavy
  history should surface sci-fi. It cannot say whether you *enjoyed* any of
  it: a film watched once and hated counts exactly as much as one watched
  once and loved.
- **Opinion** - what you thought, from your own ratings, as a deviation
  from your personal average rather than an absolute. Someone whose scores
  run 6-9 and someone whose scores run 2-10 both have a meaningful middle,
  and only the distance from it carries information.

A recommendation is exposure over the candidate's genres (cosine-normalised,
so a title is not rewarded merely for carrying more tags), scaled by a
quality factor from the community rating, and then modulated by opinion:
genres you rate above your own average are lifted, ones you rate below are
pushed down. Without the opinion term the list can only answer "more of what
you have watched", which is not the same question as "what you would like".

Opinion is shrunk toward neutral for genres with few ratings behind them
(see OPINION_PRIOR): three westerns rated highly is a hint, not a verdict,
and a recommender that treats it as a verdict recommends westerns forever.

The same two signals, read the other way round, give the opposite list -
titles whose genres you reliably rate below your own average. That is only
honest where you have actually rated enough of the genre to have an opinion,
so titles with no rating history behind them are left out of it rather than
being called disliked when they are merely unknown.

Similarity is genre overlap (Jaccard) plus a small closeness-in-year bonus
and a small rating tiebreak.

"Seen" for a film is Jellyfin's Played flag; for a show it is "any episode
in the mirror". Callers choose whether seen titles are included - that
choice is the dashboard's per-medium setting.

If Jellyfin is unreachable the catalog cannot refresh; with include_seen
the mirror alone still yields (rewatch) suggestions, otherwise the caller
gets the connection error to show.
"""

import json
import math
import threading
import time

import xbmc

import library
import main as core

CATALOG_TTL_S = 3600
RECOMMEND_LIMIT = 20
SIMILAR_LIMIT = 12

# How far a genre's ratings are trusted. A genre's opinion is pulled toward
# neutral by n / (n + OPINION_PRIOR), so one rated film moves it about a
# sixth of the way and twenty move it most of the way. Without this the
# strongest opinions in the profile are always the genres with the least
# evidence, which is exactly backwards.
OPINION_PRIOR = 5.0

# Ratings are 0-10, so a point either side of your own average is already a
# strong view. Dividing by this puts a typical opinion in roughly -1..+1.
OPINION_SPREAD = 2.0

# How the three signals combine:
#
#     score = quality**QUALITY_POWER
#           * exposure**EXPOSURE_POWER
#           * exp(OPINION_WEIGHT * opinion)
#
# These are not taste. They were measured, against 1,544 of this library's
# own rated films: build both profiles on 70% of them, rank the held-out
# 30%, and see how well the ranking agrees with the scores actually given.
#
#   exposure x quality (what this file did before)  rank agreement 0.07
#   quality alone                                   rank agreement 0.56
#   the weights below                               rank agreement 0.57
#
# and of the top twenty, the share rated 8 or better went from 0.70 to
# 0.76 against a 0.31 base rate. The lesson was that exposure, weighted
# equally, was drowning the one signal that predicts enjoyment well - a
# genre you watch constantly is not thereby a genre you enjoy.
#
# Exposure is kept, at a small power, deliberately. That test could only be
# run on films already watched, which are self-selected: within them
# exposure has little left to explain, so the number understates its real
# job, which is deciding what to put in front of you out of a catalogue of
# thousands. As a gate and a tiebreak it does that; as an equal multiplier
# it was ruining the ranking.
QUALITY_POWER = 2.0
EXPOSURE_POWER = 0.1
OPINION_WEIGHT = 0.5

# What the list can be ordered by. "match" and "avoid" are the two ends of
# the same score; the rest are plain facts about the title.
SORTS = ("match", "avoid", "rating", "year", "name")

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


def _rated_rows(media):
    """(genres, user_rating) for every rated title of a medium.

    Television is read from both episodes and series rows: a rating given to
    a whole show and one given to an episode are both opinions about the
    show's genres, and dropping either would throw away signal that exists.
    """
    kinds = ("movie",) if media == "movies" else ("episode", "series")
    connection = library.connect()
    rows = connection.execute(
        "SELECT genres, user_rating FROM items WHERE user_rating IS NOT NULL "
        "AND media IN (%s)" % ",".join("?" * len(kinds)), kinds).fetchall()
    connection.close()
    return rows


def opinion(media):
    """Genre -> how far you rate it from your own average, about -1..+1.

    Relative, not absolute: the number that matters is not that you gave
    horror a 6 but that you give everything else a 8. Genres with little
    behind them are shrunk toward neutral, so the profile grows more
    opinionated as the ratings accumulate rather than starting certain.
    """
    rows = _rated_rows(media)
    scores = [float(rating) for _, rating in rows]
    if len(scores) < 3:
        # Below this there is no personal average worth deviating from.
        return {}
    average = sum(scores) / len(scores)
    totals = {}
    for genres, rating in rows:
        for genre in _genres_of(genres):
            key = genre.lower()
            total, count = totals.get(key, (0.0, 0))
            totals[key] = (total + (float(rating) - average), count + 1)
    out = {}
    for genre, (total, count) in totals.items():
        mean = total / count
        confidence = count / (count + OPINION_PRIOR)
        out[genre] = max(-1.0, min(1.0,
                                   mean / OPINION_SPREAD)) * confidence
    return out


def watched_shows():
    """Lowercased names of every show with at least one watched episode."""
    return {(name or "").strip().lower()
            for _, _, name in _mirror_rows("episode") if name}


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------

def _entry(item, seen, score, match, view, covered):
    return {
        "id": item.get("Id"),
        "name": item.get("Name") or "?",
        "year": item.get("ProductionYear"),
        "rating": item.get("CommunityRating"),
        "genres": core.effective_genres(item),
        "seen": seen,
        "score": round(score, 4),
        # Kept apart so the page can say *why* - "matches what you watch"
        # and "you rate this sort of thing well" are different claims and a
        # single blended number cannot make either of them.
        "match": round(match, 4),
        "opinion": round(view, 4),
        "opinion_known": covered,
    }


def _seen_movie(item):
    return bool((item.get("UserData") or {}).get("Played"))


def recommendations(media, include_seen, limit=RECOMMEND_LIMIT, offset=0,
                    genres=None, exclude=None, sort="match", year_from=None,
                    year_to=None, rating_min=None):
    """Ranked suggestions for 'movies' or 'tv'.

    `genres` narrows the result to titles carrying **every** named genre,
    not merely one of them: picking science fiction and action asks for
    films that are both, which is what choosing two things means.
    `exclude` drops any title carrying **any** named genre - "not this,
    whatever else it also is", which is what refusing a genre means.

    Filtering and ordering are separate decisions and are kept that way.
    Filters say which titles are eligible; `sort` says what order they come
    in. "match" is the recommendation proper; "avoid" is the same score read
    backwards, and is restricted to titles your ratings actually say
    something about, so it lists what you would dislike rather than what you
    have never encountered.

    `offset` pages through the full ranked list. The counts returned describe
    the list after filtering, so the page can say 21-40 of 137 honestly.
    """
    if sort not in SORTS:
        sort = "match"
    taste = profile(media)
    views = opinion(media)
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
        # Cosine similarity between the title's genres and the taste
        # profile: the sum of affinities over the square root of how many
        # genres the title carries. (The profile's own length is the same
        # for every candidate, so it drops out of the ranking.)
        #
        # The two naive options are both wrong, and both were tried here.
        # A plain SUM rewards a title for carrying many tags - an eight
        # genre film collects eight scores and buries a focused one, which
        # is how the list filled with Robot Chicken when the sci-fi
        # dominance rule was dropped in v0.15.0. A plain MEAN over-corrects
        # the other way, handing the top to anything tagged with exactly
        # one popular genre. The square root sits between them: matching
        # two genres you love beats matching one, while a scattergun of
        # eight gains little.
        affinity = (sum(taste.get(genre.lower(), 0.0)
                        for genre in item_genres)
                    / math.sqrt(len(item_genres)))
        # What your ratings say about this sort of thing: the mean opinion
        # over the genres you have rated any of. Genres you have never
        # rated are left out of the mean rather than counted as neutral,
        # which would dilute a real opinion toward nothing.
        known = [views[genre.lower()] for genre in item_genres
                 if genre.lower() in views]
        view = sum(known) / len(known) if known else 0.0
        quality = (item.get("CommunityRating") or 5.5) / 10.0
        score = (quality ** QUALITY_POWER
                 * max(affinity, 1e-9) ** EXPOSURE_POWER
                 * math.exp(OPINION_WEIGHT * view))
        if sort == "avoid":
            # Never watched and never rated is not the same as disliked.
            if not known:
                continue
        elif affinity <= 0:
            continue
        scored.append(_entry(item, seen, score, affinity, view, bool(known)))

    scored = _filter(scored, genres, exclude, year_from, year_to, rating_min)
    _order(scored, sort)

    # Counted after filtering, so each chip says how many titles would be
    # left if it were added to the current selection. A genre that would
    # empty the list simply is not offered, so the filter has no dead ends.
    # An already-chosen genre is carried by every remaining title, so it
    # shows the full count and can always be unticked.
    tally = {}
    for entry in scored:
        for genre in entry["genres"]:
            tally[genre] = tally.get(genre, 0) + 1
    available = [{"genre": genre, "count": count}
                 for genre, count in sorted(tally.items(),
                                            key=lambda kv: (-kv[1], kv[0]))]
    years = [entry["year"] for entry in scored if entry["year"]]

    offset = max(0, int(offset or 0))
    liked = sorted(views.items(), key=lambda kv: -kv[1])
    return {
        "items": scored[offset:offset + limit],
        "total": len(scored),
        "offset": offset,
        "limit": limit,
        "sort": sort,
        "genres_available": available,
        "genres_selected": sorted(_clean(genres)),
        "genres_excluded": sorted(_clean(exclude)),
        "year_bounds": [min(years), max(years)] if years else None,
        "filters": {"year_from": year_from, "year_to": year_to,
                    "rating_min": rating_min},
        "offline": offline,
        "profile": sorted(taste.items(), key=lambda kv: -kv[1])[:5],
        # Both ends of the opinion profile, so the page can show its
        # working: these are the genres the ordering is actually built on.
        "likes": liked[:5],
        "dislikes": [pair for pair in liked[::-1] if pair[1] < 0][:5],
    }


def _clean(names):
    return {name.strip().lower() for name in (names or []) if name.strip()}


def _filter(scored, genres, exclude, year_from, year_to, rating_min):
    """Eligibility, applied in the order that discards most soonest."""
    wanted = _clean(genres)
    unwanted = _clean(exclude)
    out = []
    for entry in scored:
        held = {genre.lower() for genre in entry["genres"]}
        # Subset, not intersection: every chosen genre has to be present.
        if wanted and not wanted <= held:
            continue
        if unwanted & held:
            continue
        year = entry["year"]
        if year_from and (not year or year < year_from):
            continue
        if year_to and (not year or year > year_to):
            continue
        if rating_min:
            # An unrated title is not a title rated zero, but it cannot
            # satisfy "at least 7" either, so it goes.
            if not entry["rating"] or entry["rating"] < rating_min:
                continue
        out.append(entry)
    return out


def _order(scored, sort):
    """Sort in place. Ties always fall back to the name, so paging is
    stable - two equal scores in a different order on each request would
    duplicate one title across pages and drop another."""
    if sort == "match":
        scored.sort(key=lambda e: (-e["score"], -(e["rating"] or 0),
                                   e["name"]))
    elif sort == "avoid":
        scored.sort(key=lambda e: (e["opinion"], e["score"],
                                   e["rating"] or 0, e["name"]))
    elif sort == "rating":
        scored.sort(key=lambda e: (-(e["rating"] or 0), -e["score"],
                                   e["name"]))
    elif sort == "year":
        scored.sort(key=lambda e: (-(e["year"] or 0), -e["score"],
                                   e["name"]))
    else:
        scored.sort(key=lambda e: (e["name"].lower(), -e["score"]))


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
# Search across the whole library, watched or not
# ---------------------------------------------------------------------------

def search(term, limit=25):
    """Mirror hits plus everything on the server that has never been played.

    The mirror is a record of what was watched, so on its own it cannot
    answer "do I have this?" - only "have I seen this?". Those are different
    questions and the search box is nearly always asking the first.

    Lives here rather than in library.py because this is where the catalogue
    is fetched and cached; library.py is imported by this module, so it
    cannot reach back the other way.

    A server that cannot be reached is not an error: the mirror's answer is
    still a true answer, just a narrower one.
    """
    hits = library.search(term, limit)
    needle = (term or "").strip().lower()
    if not needle:
        return hits
    try:
        movies, series = catalog()
    except core.JellyStatError:
        hits["partial"] = True     # watched titles only; server unreachable
        return hits

    known = {(movie.get("id") or "") for movie in hits["movies"]}
    seen_shows = {(show["name"] or "").strip().lower()
                  for show in hits["shows"]}
    for item in movies:
        if needle not in (item.get("Name") or "").lower():
            continue
        if item.get("Id") in known:
            continue
        hits["movies"].append({
            "id": item.get("Id"), "name": item.get("Name") or "?",
            "year": item.get("ProductionYear"),
            "rating": item.get("CommunityRating"),
            "last_played": None, "play_count": 0, "seen": False,
        })
    for item in series:
        name = (item.get("Name") or "").strip()
        if needle not in name.lower() or name.lower() in seen_shows:
            continue
        hits["shows"].append({"name": name, "episodes": 0,
                              "last_played": None, "seen": False})
    return {"movies": library.rank_hits(hits["movies"], term, limit),
            "shows": library.rank_hits(hits["shows"], term, limit)}


# ---------------------------------------------------------------------------
# Browsing the whole library
# ---------------------------------------------------------------------------

BROWSE_SHOWS = ("all", "watched", "unwatched", "rated", "unrated",
                "favourites", "duplicates")
BROWSE_SORTS = ("name", "year", "rating", "mine", "plays", "watched",
                "runtime")


def browse(media, limit=48, offset=0, genres=None, exclude=None,
           sort="name", descending=None, year_from=None, year_to=None,
           rating_min=None, show="all"):
    """The library itself, filtered and ordered - watched or not.

    Distinct from recommendations, which answer "what should I watch": this
    answers "what have I got", so it starts from everything and narrows,
    rather than starting from a score and ranking. The catalogue is merged
    in for the same reason it is in search - a mirror of what was watched
    cannot answer a question about what is owned.
    """
    if sort not in BROWSE_SORTS:
        sort = "name"
    if show not in BROWSE_SHOWS:
        show = "all"
    rows = library.browse_rows(media)
    known = {row["id"] for row in rows}
    offline = False
    try:
        movies, series = catalog()
        for item in (movies if media == "movies" else series):
            if item.get("Id") in known:
                continue
            rows.append({
                "id": item.get("Id"), "name": item.get("Name") or "?",
                "year": item.get("ProductionYear"),
                "genres": core.effective_genres(item),
                "rating": item.get("CommunityRating"), "user_rating": None,
                "play_count": 0, "last_played": None, "favourite": False,
                "runtime_minutes": None, "seen": False, "episodes": None,
            })
    except core.JellyStatError:
        offline = True

    # Counted over the whole library before any filter, so "duplicates only"
    # is a filter on a fact rather than on whatever else is currently
    # selected - and so the count means the same thing every time.
    seen_keys = {}
    for row in rows:
        key = library.dupe_key(row["name"], row["year"])
        seen_keys[key] = seen_keys.get(key, 0) + 1
    for row in rows:
        row["copies"] = seen_keys[library.dupe_key(row["name"], row["year"])]
        row["genres"] = [core.canonical_genre(g) for g in row["genres"]] \
            or ["Unknown"]

    kept = [row for row in rows if _shown(row, show)]
    kept = _narrow(kept, genres, exclude, year_from, year_to, rating_min)
    _sort_browse(kept, sort, descending)

    tally = {}
    for row in kept:
        for genre in row["genres"]:
            tally[genre] = tally.get(genre, 0) + 1
    years = [row["year"] for row in kept if row["year"]]
    offset = max(0, int(offset or 0))
    return {
        "items": kept[offset:offset + limit],
        "total": len(kept),
        "offset": offset,
        "limit": limit,
        "sort": sort,
        "descending": bool(_descending(sort, descending)),
        "show": show,
        "counts": {
            "all": len(rows),
            "watched": sum(1 for r in rows if r["seen"]),
            "unwatched": sum(1 for r in rows if not r["seen"]),
            "rated": sum(1 for r in rows if r["user_rating"] is not None),
            "unrated": sum(1 for r in rows if r["user_rating"] is None),
            "favourites": sum(1 for r in rows if r["favourite"]),
            "duplicates": sum(1 for r in rows if r["copies"] > 1),
        },
        "genres_available": [{"genre": g, "count": c} for g, c in
                             sorted(tally.items(),
                                    key=lambda kv: (-kv[1], kv[0]))],
        "genres_selected": sorted(_clean(genres)),
        "genres_excluded": sorted(_clean(exclude)),
        "year_bounds": [min(years), max(years)] if years else None,
        "offline": offline,
    }


def _shown(row, show):
    if show == "watched":
        return row["seen"]
    if show == "unwatched":
        return not row["seen"]
    if show == "rated":
        return row["user_rating"] is not None
    if show == "unrated":
        return row["user_rating"] is None
    if show == "favourites":
        return row["favourite"]
    if show == "duplicates":
        return row["copies"] > 1
    return True


def _narrow(rows, genres, exclude, year_from, year_to, rating_min):
    wanted = _clean(genres)
    unwanted = _clean(exclude)
    out = []
    for row in rows:
        held = {g.lower() for g in row["genres"]}
        if wanted and not wanted <= held:
            continue
        if unwanted & held:
            continue
        year = row["year"]
        if year_from and (not year or year < year_from):
            continue
        if year_to and (not year or year > year_to):
            continue
        if rating_min and (not row["rating"] or row["rating"] < rating_min):
            continue
        out.append(row)
    return out


def _descending(sort, descending):
    """Which way round a column reads naturally when nobody has said.

    A name sorts A-Z; a rating, a year or a play count sorts biggest first,
    because "top rated" is what someone means by sorting on a rating.
    """
    if descending is not None:
        return descending
    return sort != "name"


def _sort_browse(rows, sort, descending):
    down = _descending(sort, descending)
    keys = {
        "name": lambda r: ((r["name"] or "").lower(),),
        "year": lambda r: (r["year"] or 0,),
        "rating": lambda r: (r["rating"] or 0,),
        "mine": lambda r: (r["user_rating"] if r["user_rating"] is not None
                           else -1,),
        "plays": lambda r: (r["play_count"] or 0,),
        "watched": lambda r: (r["last_played"] or "",),
        "runtime": lambda r: (r["runtime_minutes"] or 0,),
    }
    # Name always breaks the tie, so paging is stable: equal values in a
    # different order per request would repeat one title across pages and
    # silently drop another.
    rows.sort(key=lambda r: (r["name"] or "").lower())
    rows.sort(key=keys[sort], reverse=down)


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
