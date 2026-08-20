# -*- coding: utf-8 -*-
"""A full local mirror of the watched library, in the addon's own database.

Jellyfin is treated as a feed, not as the store: every sync upserts the
current watched items into the `items` table, so the addon owns a complete
copy that survives whatever happens server-side - a bulk re-mark, a deleted
film, a rebuilt library. Only changes touch the disk; an unchanged item
costs one indexed lookup.

Two things fall out of owning the copy:

1. Change detection. When a mirrored item's LastPlayedDate moves forward,
   somebody played it - on any device, not just this Kodi box. That change
   becomes a row in the `plays` log, stamped with Jellyfin's own timestamp,
   so viewing on the phone or the web app lands in the habit charts too.
   (Plays on this box are skipped: the Kodi player logger already recorded
   them with better data.)

2. Independence. If Jellyfin is unreachable, the dashboard can be built
   from the mirror instead of failing - see as_jellyfin_items(), which
   returns rows in the same shape the server would have sent.

3. Ratings arriving from elsewhere. Jellyfin's per-user `UserData.Rating`
   is the field JellyRate writes when it asks for a score as the credits
   roll, and the same field JellyStat's own rating page writes. Reading it
   back on every sync means a score given anywhere - JellyRate, another
   device, the Jellyfin web app - lands in the mirror without needing a
   bridge between the addons.

Syncs are driven by webdata.build(), which has just fetched the whole
library anyway, so mirroring adds no Jellyfin traffic at all.
"""

import json
import sqlite3
from datetime import datetime, timedelta

import xbmc

import history
import playlog

# A change-detected play is skipped if the play log already holds a session
# for the same title within this window - that is the Kodi logger having
# seen the same sitting first-hand.
DEDUP_HOURS = 12

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id           TEXT PRIMARY KEY,
    media        TEXT NOT NULL,
    name         TEXT NOT NULL,
    year         INTEGER,
    series_id    TEXT,
    series_name  TEXT,
    season       INTEGER,
    episode      INTEGER,
    genres       TEXT NOT NULL DEFAULT '[]',
    rating       REAL,
    critic       REAL,
    favourite    INTEGER NOT NULL DEFAULT 0,
    play_count   INTEGER NOT NULL DEFAULT 0,
    last_played  TEXT,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    present      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS items_media ON items (media);
"""

# The rating columns live here rather than in ratings.py because sync()
# reads them: a database created before ratings.py first ran would
# otherwise break the mirror rather than just the rating page.
RATING_COLUMNS = [
    "ALTER TABLE items ADD COLUMN user_rating REAL",
    "ALTER TABLE items ADD COLUMN user_rating_at TEXT",
    "ALTER TABLE items ADD COLUMN rating_sync TEXT",
]


def log(message, level=xbmc.LOGINFO):
    xbmc.log("[JellyStat] %s" % message, level)


_schema_done = False


def connect():
    global _schema_done
    connection = sqlite3.connect(history.db_path(), timeout=10)
    if not _schema_done:
        connection.executescript(SCHEMA)
        for statement in RATING_COLUMNS:
            try:
                connection.execute(statement)
            except sqlite3.OperationalError:
                pass  # column already present
        _schema_done = True
    return connection


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def _row_from(item, media, series_genres):
    """Flatten a Jellyfin item into the mirror's columns."""
    user_data = item.get("UserData") or {}
    if media == "episode":
        genres = (series_genres or {}).get(item.get("SeriesId")) or []
    else:
        genres = item.get("Genres") or []
    return {
        "id": item["Id"],
        "media": media,
        "name": item.get("Name") or "?",
        "year": item.get("ProductionYear"),
        "series_id": item.get("SeriesId"),
        "series_name": item.get("SeriesName"),
        "season": item.get("ParentIndexNumber"),
        "episode": item.get("IndexNumber"),
        "genres": json.dumps(genres),
        "rating": item.get("CommunityRating"),
        "critic": item.get("CriticRating"),
        "favourite": 1 if user_data.get("IsFavorite") else 0,
        "play_count": int(user_data.get("PlayCount") or 0),
        "last_played": (user_data.get("LastPlayedDate") or "")[:19] or None,
        "jf_rating": _numeric(user_data.get("Rating")),
    }


def _numeric(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _adopt_rating(connection, row, existing):
    """Take Jellyfin's per-user rating when it is the newer truth.

    `existing` is (play_count, last_played, user_rating, rating_sync).

    - No local score: adopt whatever the server has. This is how a rating
      given in JellyRate, on a phone, or in the web app arrives here.
    - Local score that differs from the server's: the server wins, because
      it is the copy every device shares.
    - The one exception is a local score whose write to Jellyfin *failed*.
      That is a pending write rather than stale data, and adopting over it
      would silently discard what the user just typed.

    Note that "saved without pushing" is not an error: JellyRate writes the
    rating to Jellyfin itself and then hands it to us, so those rows are
    already in agreement with the server and must stay adoptable.
    """
    server = row["jf_rating"]
    local, sync_state = existing[2], existing[3]
    if server is None:
        return False
    if local is not None and abs(float(local) - server) < 0.001:
        return False
    if (sync_state or "").startswith("error"):
        return False
    connection.execute(
        "UPDATE items SET user_rating = ?, rating_sync = 'from jellyfin' "
        "WHERE id = ?", (server, row["id"]))
    return True


def _known_session(connection, row, played_local):
    """True when the play log already covers this sitting first-hand."""
    show, title = playlog.dedup_key(row["series_name"], row["name"])
    lo = (played_local - timedelta(hours=DEDUP_HOURS)).strftime(
        "%Y-%m-%dT%H:%M:%S")
    hi = (played_local + timedelta(hours=DEDUP_HOURS)).strftime(
        "%Y-%m-%dT%H:%M:%S")
    hit = connection.execute(
        "SELECT 1 FROM plays WHERE LOWER(COALESCE(show, '')) = ? "
        "AND LOWER(title) = ? AND started_at BETWEEN ? AND ? LIMIT 1",
        (show, title, lo, hi)).fetchone()
    return hit is not None


def _infer_play(connection, row, utc_offset, now):
    """Record a change-detected play with Jellyfin's own timestamp."""
    import main as core
    played = core.parse_played_date(row["last_played"])
    if not played:
        return False
    played_local = played + utc_offset
    if _known_session(connection, row, played_local):
        return False
    stamp = played_local.strftime("%Y-%m-%dT%H:%M:%S")
    connection.execute(
        "INSERT INTO plays (started_at, ended_at, day, hour, weekday, media, "
        "title, show, season, episode, year, runtime_seconds, "
        "watched_seconds, completed, source, device, batch_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, 1, "
        "'jellyfin-sync', NULL, NULL)",
        (stamp, stamp,
         played_local.strftime("%Y-%m-%d"),
         played_local.hour, played_local.weekday(),
         row["media"], row["name"], row["series_name"],
         row["season"], row["episode"], row["year"]))
    return True


def sync(movies, episodes, series_genres, utc_offset):
    """Upsert the fetched library into the mirror; returns what happened.

    Change-detected plays are only inferred for items the mirror already
    tracks: the very first sync would otherwise fabricate thousands of
    "plays" out of dates the charts already account for.
    """
    now = datetime.now()
    stamp = now.strftime("%Y-%m-%dT%H:%M:%S")
    added = updated = inferred = adopted = 0
    try:
        connection = connect()
        # playlog owns the plays table; make sure it exists before we write.
        playlog.connect().close()
        with connection:
            seen_ids = []
            for media, items in (("movie", movies), ("episode", episodes)):
                for item in items:
                    row = _row_from(item, media, series_genres)
                    seen_ids.append(row["id"])
                    old = connection.execute(
                        "SELECT play_count, last_played, user_rating, "
                        "rating_sync FROM items WHERE id = ?",
                        (row["id"],)).fetchone()
                    if old is None:
                        connection.execute(
                            "INSERT INTO items (id, media, name, year, "
                            "series_id, series_name, season, episode, "
                            "genres, rating, critic, favourite, play_count, "
                            "last_played, first_seen, last_seen, present) "
                            "VALUES (:id, :media, :name, :year, :series_id, "
                            ":series_name, :season, :episode, :genres, "
                            ":rating, :critic, :favourite, :play_count, "
                            ":last_played, '%s', '%s', 1)" % (stamp, stamp),
                            row)
                        if row["jf_rating"]:
                            connection.execute(
                                "UPDATE items SET user_rating = ?, "
                                "rating_sync = 'from jellyfin' WHERE id = ?",
                                (row["jf_rating"], row["id"]))
                        added += 1
                        continue
                    changed = (row["last_played"] or "") != (old[1] or "")
                    if changed and row["last_played"] \
                            and row["last_played"] > (old[1] or ""):
                        if _infer_play(connection, row, utc_offset, now):
                            inferred += 1
                    if _adopt_rating(connection, row, old):
                        adopted += 1
                    connection.execute(
                        "UPDATE items SET name = :name, year = :year, "
                        "series_id = :series_id, series_name = :series_name, "
                        "season = :season, episode = :episode, "
                        "genres = :genres, rating = :rating, "
                        "critic = :critic, "
                        "favourite = :favourite, play_count = :play_count, "
                        "last_played = :last_played, last_seen = '%s', "
                        "present = 1 WHERE id = :id" % stamp, row)
                    if changed or int(row["play_count"]) != int(old[0] or 0):
                        updated += 1
            # Items that vanished from Jellyfin stay in the mirror - that is
            # the point of having one - but are flagged so a future feature
            # can distinguish them.
            if seen_ids:
                marks = ",".join("?" * len(seen_ids))
                connection.execute(
                    "UPDATE items SET present = 0 WHERE id NOT IN (%s)"
                    % marks, seen_ids)
        connection.close()
        if added or inferred or adopted:
            log("Mirror sync: %d new items, %d updated, %d plays detected "
                "from other devices, %d ratings picked up"
                % (added, updated, inferred, adopted))
        return {"added": added, "updated": updated, "inferred": inferred,
                "adopted": adopted}
    except Exception as err:
        log("Mirror sync failed: %s" % err, xbmc.LOGWARNING)
        return {"added": 0, "updated": 0, "inferred": 0, "adopted": 0,
                "error": str(err)}


# ---------------------------------------------------------------------------
# Reading the mirror back
# ---------------------------------------------------------------------------

def as_jellyfin_items():
    """The mirror, reshaped as Jellyfin would have sent it.

    Lets webdata.build() run unchanged against the local copy when the
    server is unreachable: (movies, episodes, series_genres) exactly like
    the three API calls would return. Items deleted from Jellyfin are
    included - the mirror exists precisely so they still count.
    """
    connection = connect()
    rows = connection.execute(
        "SELECT id, media, name, year, series_id, series_name, season, "
        "episode, genres, rating, critic, favourite, play_count, "
        "last_played FROM items ORDER BY last_played DESC").fetchall()
    connection.close()
    movies, episodes, series_genres = [], [], {}
    for (item_id, media, name, year, series_id, series_name, season,
         episode, genres, rating, critic, favourite, play_count,
         last_played) in rows:
        item = {
            "Id": item_id,
            "Name": name,
            "ProductionYear": year,
            "CommunityRating": rating,
            "CriticRating": critic,
            "UserData": {
                "Played": True,
                "IsFavorite": bool(favourite),
                "PlayCount": play_count,
                "LastPlayedDate": (last_played + ".0000000Z"
                                   if last_played else None),
            },
        }
        try:
            genre_list = json.loads(genres)
        except ValueError:
            genre_list = []
        if media == "episode":
            item["SeriesId"] = series_id
            item["SeriesName"] = series_name
            item["ParentIndexNumber"] = season
            item["IndexNumber"] = episode
            if series_id:
                series_genres.setdefault(series_id, genre_list)
            episodes.append(item)
        else:
            item["Genres"] = genre_list
            movies.append(item)
    return movies, episodes, series_genres


def status():
    """Mirror size and freshness, for the dashboard's footer."""
    try:
        connection = connect()
        row = connection.execute(
            "SELECT COUNT(*), SUM(CASE WHEN media = 'movie' THEN 1 ELSE 0 "
            "END), SUM(CASE WHEN present = 0 THEN 1 ELSE 0 END), "
            "MAX(last_seen) FROM items").fetchone()
        connection.close()
        return {"items": row[0] or 0, "movies": row[1] or 0,
                "departed": row[2] or 0, "synced_at": row[3]}
    except Exception:
        return {"items": 0, "movies": 0, "departed": 0, "synced_at": None}


# ---------------------------------------------------------------------------
# Search and item pages
# ---------------------------------------------------------------------------

def _like(term):
    """A LIKE pattern for a user's search words, escaped."""
    cleaned = (term or "").strip().lower()
    cleaned = cleaned.replace("\\", "\\\\").replace("%", "\\%") \
                     .replace("_", "\\_")
    return "%" + cleaned + "%"


def search(term, limit=8):
    """Films and shows matching the term -> {movies: [...], shows: [...]}.

    Searches the mirror, not Jellyfin, so it answers instantly and works
    offline; the mirror holds every watched title anyway. Shows are grouped
    from their episodes, ranked by how much of them has been watched.
    """
    if not (term or "").strip():
        return {"movies": [], "shows": []}
    pattern = _like(term)
    connection = connect()
    movies = [{
        "id": row[0], "name": row[1], "year": row[2], "rating": row[3],
        "last_played": row[4], "play_count": row[5],
    } for row in connection.execute(
        "SELECT id, name, year, rating, last_played, play_count FROM items "
        "WHERE media = 'movie' AND LOWER(name) LIKE ? ESCAPE '\\' "
        "ORDER BY last_played DESC LIMIT ?", (pattern, limit))]
    shows = [{
        "name": row[0], "episodes": row[1], "last_played": row[2],
    } for row in connection.execute(
        "SELECT series_name, COUNT(*), MAX(last_played) FROM items "
        "WHERE media = 'episode' AND series_name IS NOT NULL "
        "AND LOWER(series_name) LIKE ? ESCAPE '\\' "
        "GROUP BY series_name ORDER BY MAX(last_played) DESC LIMIT ?",
        (pattern, limit))]
    connection.close()
    return {"movies": movies, "shows": shows}


def _sittings(connection, show, title=None):
    """Play-log rows for one film or one whole show, newest first."""
    if title is not None:
        where = ("LOWER(COALESCE(show, '')) = '' AND LOWER(title) = ? "
                 "AND media = 'movie'")
        params = [(title or "").strip().lower()]
    else:
        where = "LOWER(COALESCE(show, '')) = ?"
        params = [(show or "").strip().lower()]
    return [{
        "started_at": row[0], "ended_at": row[1], "title": row[2],
        "season": row[3], "episode": row[4], "watched_seconds": row[5],
        "completed": bool(row[6]), "source": row[7], "device": row[8],
    } for row in connection.execute(
        "SELECT started_at, ended_at, title, season, episode, "
        "watched_seconds, completed, source, device FROM plays "
        "WHERE %s ORDER BY started_at DESC LIMIT 200" % where, params)]


def movie_detail(item_id):
    """Everything the mirror and the play log know about one film."""
    connection = connect()
    row = connection.execute(
        "SELECT id, name, year, genres, rating, critic, favourite, "
        "play_count, last_played, first_seen, present FROM items "
        "WHERE id = ? AND media = 'movie'", (item_id,)).fetchone()
    if row is None:
        connection.close()
        return None
    detail = {
        "id": row[0], "name": row[1], "year": row[2],
        "genres": json.loads(row[3] or "[]"),
        "rating": row[4], "critic": row[5], "favourite": bool(row[6]),
        "play_count": row[7], "last_played": row[8], "first_seen": row[9],
        "present": bool(row[10]),
        "sittings": _sittings(connection, None, title=row[1]),
    }
    connection.close()
    return detail


def show_detail(name):
    """One show: its episodes from the mirror, its sittings from the log."""
    connection = connect()
    rows = connection.execute(
        "SELECT name, season, episode, rating, play_count, last_played, "
        "genres, present FROM items WHERE media = 'episode' "
        "AND LOWER(series_name) = ? "
        "ORDER BY last_played DESC",
        ((name or "").strip().lower(),)).fetchall()
    if not rows:
        connection.close()
        return None
    episodes = [{
        "name": row[0], "season": row[1], "episode": row[2],
        "rating": row[3], "play_count": row[4], "last_played": row[5],
        "present": bool(row[7]),
    } for row in rows]
    seasons = {}
    for episode in episodes:
        season = episode["season"] if episode["season"] is not None else 0
        seasons[season] = seasons.get(season, 0) + 1
    ratings = [e["rating"] for e in episodes if e["rating"]]
    detail = {
        "name": name,
        "genres": json.loads(rows[0][6] or "[]"),
        "episodes_watched": len(episodes),
        "plays": sum(e["play_count"] for e in episodes),
        "last_played": episodes[0]["last_played"],
        "avg_rating": (sum(ratings) / len(ratings)) if ratings else None,
        "seasons": [{"season": season, "episodes": count}
                    for season, count in sorted(seasons.items())],
        "episodes": episodes[:100],
        "sittings": _sittings(connection, name),
    }
    connection.close()
    return detail


# ---------------------------------------------------------------------------
# Per-medium stats (the Movies and TV pages)
# ---------------------------------------------------------------------------

def _decades(rows):
    """Watched titles grouped by release decade, oldest first."""
    buckets = {}
    for year in rows:
        if not year:
            continue
        decade = (int(year) // 10) * 10
        buckets[decade] = buckets.get(decade, 0) + 1
    return [{"decade": decade, "count": buckets[decade]}
            for decade in sorted(buckets)]


def movie_stats(limit=15):
    """Everything the Movies page shows, straight from the mirror."""
    connection = connect()
    totals = connection.execute(
        "SELECT COUNT(*), SUM(play_count), AVG(rating), "
        "SUM(CASE WHEN favourite = 1 THEN 1 ELSE 0 END) "
        "FROM items WHERE media = 'movie'").fetchone()
    years = [row[0] for row in connection.execute(
        "SELECT year FROM items WHERE media = 'movie'")]
    most_played = [{
        "id": row[0], "name": row[1], "year": row[2], "plays": row[3],
        "rating": row[4],
    } for row in connection.execute(
        "SELECT id, name, year, play_count, rating FROM items "
        "WHERE media = 'movie' AND play_count > 1 "
        "ORDER BY play_count DESC, name LIMIT ?", (limit,))]
    best = [{
        "id": row[0], "name": row[1], "year": row[2], "rating": row[3],
    } for row in connection.execute(
        "SELECT id, name, year, rating FROM items "
        "WHERE media = 'movie' AND rating IS NOT NULL "
        "ORDER BY rating DESC, name LIMIT ?", (limit,))]
    favourites = [{
        "id": row[0], "name": row[1], "year": row[2], "rating": row[3],
    } for row in connection.execute(
        "SELECT id, name, year, rating FROM items "
        "WHERE media = 'movie' AND favourite = 1 "
        "ORDER BY last_played DESC LIMIT ?", (limit,))]
    connection.close()
    return {
        "total": totals[0] or 0,
        "plays": totals[1] or 0,
        "avg_rating": round(totals[2], 2) if totals[2] else None,
        "favourites_count": totals[3] or 0,
        "decades": _decades(years),
        "most_played": most_played,
        "best_rated": best,
        "favourites": favourites,
    }


def tv_stats(limit=15):
    """Everything the TV page shows, aggregated per show."""
    connection = connect()
    totals = connection.execute(
        "SELECT COUNT(*), SUM(play_count), AVG(rating), "
        "COUNT(DISTINCT series_name) FROM items "
        "WHERE media = 'episode'").fetchone()
    shows = [{
        "name": row[0], "episodes": row[1], "plays": row[2],
        "avg_rating": round(row[3], 2) if row[3] else None,
        "last_played": row[4], "seasons": row[5],
    } for row in connection.execute(
        "SELECT series_name, COUNT(*), SUM(play_count), AVG(rating), "
        "MAX(last_played), COUNT(DISTINCT season) FROM items "
        "WHERE media = 'episode' AND series_name IS NOT NULL "
        "GROUP BY series_name ORDER BY COUNT(*) DESC LIMIT ?", (limit,))]
    best = [{
        "name": row[0], "avg_rating": round(row[1], 2), "episodes": row[2],
    } for row in connection.execute(
        "SELECT series_name, AVG(rating), COUNT(*) FROM items "
        "WHERE media = 'episode' AND series_name IS NOT NULL "
        "AND rating IS NOT NULL GROUP BY series_name "
        # Three episodes minimum: a single well-rated pilot is not a
        # "best show", it is a sample of one.
        "HAVING COUNT(*) >= 3 ORDER BY AVG(rating) DESC LIMIT ?", (limit,))]
    connection.close()
    return {
        "episodes": totals[0] or 0,
        "plays": totals[1] or 0,
        "avg_rating": round(totals[2], 2) if totals[2] else None,
        "shows": totals[3] or 0,
        "top_shows": shows,
        "best_rated": best,
    }
