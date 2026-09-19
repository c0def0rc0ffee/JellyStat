# -*- coding: utf-8 -*-
"""
<summary>
Builds the demo library and its viewing timeline, deterministically.
</summary>
<remarks>
One timeline drives everything: Jellyfin's PlayCount and LastPlayedDate for
each item, and the individual sessions the play log would have recorded.
Seeding both from the same source is what keeps the calendar, the clock
chart and the sittings list telling the same story.
</remarks>
"""

import hashlib
import random
from datetime import datetime, timedelta

import catalogue

SEED = 20260825

LIBRARY_DAYS = 760

# Films from the tail of the shuffle are saved for this closing stretch.
RECENT_MOVIE_DAYS = 55

# Shows left untouched, so the library is not uniformly finished.
UNWATCHED_SHOWS = 4

# The play log only reaches back to when the addon was installed; anything
# older shows up through Jellyfin's last-played date alone. Keeping that
# split in the demo means the coverage note on the dashboard is truthful.
LOG_COVERAGE_DAYS = 430


def _spread(rating, critic):
    """
    <summary>
    Pull the catalogue's scores away from the middle of the scale.
    </summary>
    <remarks>
    The written scores all landed between 6 and 8, which draws a histogram
    of three bars. Stretching around the midpoint keeps every title's
    relative standing and gives the rating panels something to show.
    </remarks>
    """
    stretched = round(min(9.6, max(3.4, 7.2 + (rating - 7.2) * 2.15)), 1)
    if critic is None:
        return stretched, None
    return stretched, int(min(98, max(21, 74 + (critic - 74) * 1.9)))


def _id(*parts):
    """
    <summary>
    A stable id from its parts.
    </summary>
    <param name="parts">Anything stringable.</param>
    <returns>A hex string.</returns>
    """
    return hashlib.md5("|".join(str(p) for p in parts).encode()).hexdigest()


def _provider_ids(rng, kind):
    """
    <summary>
    Invented IMDb, TMDb and, for series, TVDb ids.
    </summary>
    <param name="rng">A seeded Random.</param>
    <param name="kind">movie or series.</param>
    <returns>A dict.</returns>
    """
    ids = {"Imdb": "tt%07d" % rng.randint(100000, 9999999),
           "Tmdb": str(rng.randint(1000, 999999))}
    if kind == "series":
        ids["Tvdb"] = str(rng.randint(10000, 400000))
    return ids


def _evening(rng, day):
    """
    <summary>
    A plausible clock time to start watching on the given date.
    </summary>
    """
    weekend = day.weekday() >= 5
    if weekend and rng.random() < 0.45:
        hour = rng.choice([11, 13, 14, 15, 16, 20, 21, 21, 22])
    else:
        hour = rng.choice([18, 19, 20, 20, 21, 21, 21, 22, 22, 23])
    return day.replace(hour=hour,
                       minute=rng.choice([0, 5, 10, 15, 20, 30, 35, 40, 45, 50]),
                       second=0, microsecond=0)


def _viewing_days(rng, now):
    """
    <summary>
    Which dates were watched on, and how heavily.
    </summary>
    <remarks>
    Real viewing is lumpy: most evenings something is on, some weeks nothing
    is, and a few weekends run long. Flat randomness produces a calendar
    that looks generated, so the shape is put in on purpose.
    </remarks>
    """
    days = {}
    start = now - timedelta(days=LIBRARY_DAYS)
    day = start
    while day <= now:
        weekday = day.weekday()
        chance = 0.62 if weekday >= 4 else 0.44
        age = (now - day).days
        # Two fallow stretches: a holiday and a busy month at work.
        if 250 <= age <= 271 or 96 <= age <= 110:
            chance = 0.05
        # A recent binge fortnight.
        if age <= 16:
            chance = 0.85
        if rng.random() < chance:
            weight = rng.random()
            if weekday >= 5 and weight > 0.72:
                count = rng.choice([3, 3, 4, 5])
            elif weight > 0.62:
                count = 2
            else:
                count = 1
            days[day.date()] = count
        day += timedelta(days=1)
    return days


def build():
    """
    <summary>
    Build the demo library: films, series and episodes from the catalogue, then the viewing timeline.
    </summary>
    <returns>The data dict with movies, series and episodes.</returns>
    """
    rng = random.Random(SEED)
    now = datetime.now().replace(hour=22, minute=0, second=0, microsecond=0)

    movies = []
    for title, year, genres, rating, critic, runtime in catalogue.MOVIES:
        rating, critic = _spread(rating, critic)
        movies.append({
            "kind": "movie",
            "id": _id("movie", title, year),
            "title": title,
            "year": year,
            "genres": list(genres),
            "rating": rating,
            "critic": critic,
            "runtime": runtime,
            "provider": _provider_ids(rng, "movie"),
            "overview": ("%s is a %s picture from %d, and one of the demo "
                         "titles this screenshot library is built from."
                         % (title, genres[0].lower(), year)),
            "people": rng.sample(catalogue.PEOPLE, 6),
        })

    series = []
    episodes = []
    for name, year, genres, rating, seasons, per_season, runtime in catalogue.SHOWS:
        rating = _spread(rating, None)[0]
        series_id = _id("series", name)
        series.append({
            "kind": "series",
            "id": series_id,
            "title": name,
            "year": year,
            "genres": list(genres),
            "rating": rating,
            "critic": None,
            "runtime": runtime,
            "provider": _provider_ids(rng, "series"),
            "overview": ("%s ran for %d series. It is invented for this "
                         "demo library." % (name, seasons)),
            "people": rng.sample(catalogue.PEOPLE, 5),
        })
        for season in range(1, seasons + 1):
            for number in range(1, per_season + 1):
                episodes.append({
                    "kind": "episode",
                    "id": _id("episode", name, season, number),
                    "title": "Episode %d" % number,
                    "year": year + season - 1,
                    "genres": [],
                    "rating": round(min(9.4, rating + rng.uniform(-0.6, 0.6)), 1),
                    "critic": None,
                    "runtime": runtime,
                    "series_id": series_id,
                    "series_name": name,
                    "season": season,
                    "episode": number,
                    "provider": _provider_ids(rng, "episode"),
                    "overview": "A demo episode of %s." % name,
                    "people": rng.sample(catalogue.PEOPLE, 4),
                })

    _schedule(rng, now, movies, episodes)
    return {"movies": movies, "series": series, "episodes": episodes,
            "now": now}


def _schedule(rng, now, movies, episodes):
    """
    <summary>
    Walk the viewing days and decide what was watched on each.
    </summary>
    <remarks>
    Television is watched in runs: two or three episodes of one show in an
    evening, the next few the following night, so shows are followed in
    order rather than sampled at random, which is also what makes the
    top-shows and rewatch panels look like somebody's actual habits.
    </remarks>
    """
    for item in movies + episodes:
        item["plays"] = []

    by_show = {}
    for episode in episodes:
        by_show.setdefault(episode["series_name"], []).append(episode)
    for name in by_show:
        by_show[name].sort(key=lambda e: (e["season"], e["episode"]))
    progress = {name: 0 for name in by_show}
    # A library nobody has finished: some shows were never started, which is
    # what gives Discover something to suggest and the TV library a
    # "not watched" count worth filtering on.
    shows = sorted(by_show)[UNWATCHED_SHOWS:]

    unseen_movies = list(movies)
    rng.shuffle(unseen_movies)
    # Days are walked oldest first, so an unreserved pool is spent long
    # before the recent weeks, and the dashboard's 30-day panels come up
    # with no film in them at all. These are held back for the tail.
    reserved = [unseen_movies.pop() for _ in range(18)]
    current_show = rng.choice(shows)

    for day, slots in sorted(_viewing_days(rng, now).items()):
        when = _evening(rng, datetime(day.year, day.month, day.day))
        recent = (now - when).days <= RECENT_MOVIE_DAYS
        pool = reserved if recent else unseen_movies
        # A film takes the evening; television fills it with a run.
        if slots <= 2 and pool and rng.random() < (0.5 if recent else 0.42):
            item = pool.pop()
            item["plays"].append(when)
            continue
        if rng.random() < 0.18 or progress[current_show] >= len(by_show[current_show]):
            candidates = [s for s in shows
                          if progress[s] < len(by_show[s])] or shows
            current_show = rng.choice(candidates)
        for _ in range(slots):
            queue = by_show[current_show]
            index = progress[current_show]
            if index >= len(queue):
                # Finished it. A favourite gets started again from the top.
                if rng.random() < 0.3:
                    progress[current_show] = 0
                    index = 0
                else:
                    break
            episode = queue[index]
            progress[current_show] = index + 1
            episode["plays"].append(when)
            when += timedelta(minutes=episode["runtime"] + rng.randint(2, 9))

    # A handful of films are worth watching twice.
    watched = [m for m in movies if m["plays"]]
    for movie in rng.sample(watched, min(11, len(watched))):
        first = movie["plays"][0]
        earlier = first - timedelta(days=rng.randint(120, 500))
        if earlier > now - timedelta(days=LIBRARY_DAYS):
            movie["plays"].insert(0, _evening(rng, earlier))
