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
import re
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
    # What Jellyfin holds, kept as its own column so "where is this score
    # saved" is answered from data rather than guessed from history.
    "ALTER TABLE items ADD COLUMN jf_rating REAL",
    # Minutes, so watch-time totals cover the whole library rather than only
    # the period the play log happens to reach back to.
    "ALTER TABLE items ADD COLUMN runtime_minutes INTEGER",
    # The ids every other tracker also knows a title by. Imported history is
    # matched on these first and only falls back to the title, which is the
    # difference between 82% and ~99% of episodes finding their row.
    "ALTER TABLE items ADD COLUMN imdb_id TEXT",
    "ALTER TABLE items ADD COLUMN tmdb_id TEXT",
    "ALTER TABLE items ADD COLUMN tvdb_id TEXT",
]

# Looked up constantly by the importer, so they are worth indexing.
RATING_INDEXES = [
    "CREATE INDEX IF NOT EXISTS items_imdb ON items (imdb_id)",
    "CREATE INDEX IF NOT EXISTS items_tmdb ON items (tmdb_id)",
    "CREATE INDEX IF NOT EXISTS items_tvdb ON items (tvdb_id)",
    "CREATE INDEX IF NOT EXISTS items_series ON items (series_name)",
]


def log(message, level=xbmc.LOGINFO):
    xbmc.log("[JellyStat] %s" % message, level)


_schema_done = False


def _ulower(value):
    """Python's lowercase, for SQL to use.

    SQLite's own LOWER() only touches A-Z, but every parameter compared
    against it here was lowercased in Python, which folds the whole of
    Unicode. So "ELITE" matched and "\u00c9LITE" did not: its show page fell
    through to the catalog, its sittings list came back empty, search could
    not find it, and - worst of the four - the duplicate check missed, so
    every sync logged another play for it.
    """
    return (value or "").lower()


def connect():
    global _schema_done
    connection = sqlite3.connect(history.db_path(), timeout=10)
    connection.create_function("ULOWER", 1, _ulower)
    if not _schema_done:
        connection.executescript(SCHEMA)
        for statement in RATING_COLUMNS:
            try:
                connection.execute(statement)
            except sqlite3.OperationalError:
                pass  # column already present
        for statement in RATING_INDEXES:
            try:
                connection.execute(statement)
            except sqlite3.OperationalError:
                pass
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
        "runtime_minutes": (int(item["RunTimeTicks"] // 600000000)
                            if item.get("RunTimeTicks") else None),
        "imdb_id": _provider(item, "Imdb"),
        "tmdb_id": _provider(item, "Tmdb"),
        "tvdb_id": _provider(item, "Tvdb"),
    }


def _provider(item, name):
    value = (item.get("ProviderIds") or {}).get(name)
    return str(value).strip() if value else None


def _numeric(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _adopt_rating(connection, row, existing):
    """Fill in a rating Jellyfin has and JellyStat does not.

    JellyStat is the record of what you think of a title, and Jellyfin is
    expected to match it, not the other way round. So this only ever fills
    a gap: a score given in JellyRate, on a phone or in the web app arrives
    here, but where JellyStat already holds one, the server's copy is
    recorded for comparison and nothing is overwritten.

    Anything else would fight the user. Imported ratings deliberately
    differ from Jellyfin until they are pushed, and an "adopt the newer
    truth" rule undid sixty of them within two minutes of the first Trakt
    import. Disagreements are surfaced by ratings.contradictions() and
    settled deliberately, not silently on the next sync.
    """
    server = row["jf_rating"]
    local = existing[2]
    # Always record what the server holds, even when nothing is adopted:
    # the reconcile screen reports both sides, so both have to be true.
    connection.execute("UPDATE items SET jf_rating = ? WHERE id = ?",
                       (server, row["id"]))
    if server is None or local is not None:
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
        "SELECT 1 FROM plays WHERE ULOWER(COALESCE(show, '')) = ? "
        "AND ULOWER(title) = ? AND started_at BETWEEN ? AND ? LIMIT 1",
        (show, title, lo, hi)).fetchone()
    return hit is not None


def _infer_play(connection, row, utc_offset):
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


def _series_row(item):
    """A show as a mirror row.

    Shows are stored so that things which belong to a show rather than to
    one of its episodes have somewhere to live: a rating of the show, its
    provider ids, its own artwork. They carry media='series' and are
    excluded from every watched-item count, because a show is not something
    you watched - its episodes are.
    """
    user_data = item.get("UserData") or {}
    return {
        "id": item["Id"],
        "media": "series",
        "name": item.get("Name") or "?",
        "year": item.get("ProductionYear"),
        "series_id": item["Id"],
        "series_name": item.get("Name") or "?",
        "season": None,
        "episode": None,
        "genres": json.dumps(item.get("Genres") or []),
        "rating": item.get("CommunityRating"),
        "critic": item.get("CriticRating"),
        "favourite": 1 if user_data.get("IsFavorite") else 0,
        "play_count": 0,
        "last_played": None,
        "runtime_minutes": None,
        "imdb_id": _provider(item, "Imdb"),
        "tmdb_id": _provider(item, "Tmdb"),
        "tvdb_id": _provider(item, "Tvdb"),
        "jf_rating": _numeric(user_data.get("Rating")),
    }


def sync(movies, episodes, series_genres, utc_offset, series=None):
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
            groups = [("movie", movies), ("episode", episodes)]
            if series:
                groups.append(("series", series))
            # Everything is marked absent up front and the upserts below
            # mark back whatever Jellyfin still has, which leaves exactly
            # the vanished rows at zero. (Items that vanished stay in the
            # mirror - that is the point of having one - but are flagged so
            # a future feature can distinguish them.)
            #
            # The obvious spelling, one "NOT IN (?, ?, ...)" listing every
            # id seen, bound one SQL variable per item, and SQLite caps
            # those - at 999 on the builds some older Kodis ship. Past the
            # cap it raised inside this transaction and rolled the entire
            # sync back: no upserts, no inferred plays, no adopted ratings,
            # and nothing to show for it but a line in the log. A mirror
            # that silently stops updating is the worst outcome available
            # here, and it began exactly when a library grew big enough to
            # matter. Nothing below binds a variable per item.
            #
            # Not keyed on this run's timestamp either: two syncs in the
            # same second share it to the second, and the later one would
            # then flag nothing at all.
            if any(items for _, items in groups):
                connection.execute("UPDATE items SET present = 0")
            for media, items in groups:
                for item in items:
                    row = (_series_row(item) if media == "series"
                           else _row_from(item, media, series_genres))
                    old = connection.execute(
                        "SELECT play_count, last_played, user_rating, "
                        "rating_sync FROM items WHERE id = ?",
                        (row["id"],)).fetchone()
                    if old is None:
                        connection.execute(
                            "INSERT INTO items (id, media, name, year, "
                            "series_id, series_name, season, episode, "
                            "genres, rating, critic, favourite, play_count, "
                            "last_played, runtime_minutes, imdb_id, "
                            "tmdb_id, tvdb_id, first_seen, last_seen, "
                            "present) "
                            "VALUES (:id, :media, :name, :year, :series_id, "
                            ":series_name, :season, :episode, :genres, "
                            ":rating, :critic, :favourite, :play_count, "
                            ":last_played, :runtime_minutes, :imdb_id, "
                            ":tmdb_id, :tvdb_id, '%s', '%s', 1)"
                            % (stamp, stamp),
                            row)
                        # "is not None", not truthiness: a Jellyfin score
                        # of 0 is an opinion, and dropping it here while
                        # _adopt_rating would have taken it made the same
                        # item rated or unrated depending only on whether
                        # the mirror had seen it before.
                        if row["jf_rating"] is not None:
                            connection.execute(
                                "UPDATE items SET user_rating = ?, "
                                "jf_rating = ?, "
                                "rating_sync = 'from jellyfin' WHERE id = ?",
                                (row["jf_rating"], row["jf_rating"],
                                 row["id"]))
                        added += 1
                        continue
                    changed = (row["last_played"] or "") != (old[1] or "")
                    if media != "series" and changed and row["last_played"] \
                            and row["last_played"] > (old[1] or ""):
                        if _infer_play(connection, row, utc_offset):
                            inferred += 1
                    if _adopt_rating(connection, row, old):
                        adopted += 1
                    connection.execute(
                        "UPDATE items SET name = :name, year = :year, "
                        "series_id = :series_id, series_name = :series_name, "
                        "season = :season, episode = :episode, "
                        "genres = :genres, rating = :rating, "
                        "critic = :critic, "
                        "runtime_minutes = :runtime_minutes, "
                        "imdb_id = :imdb_id, tmdb_id = :tmdb_id, "
                        "tvdb_id = :tvdb_id, "
                        "favourite = :favourite, play_count = :play_count, "
                        "last_played = :last_played, last_seen = '%s', "
                        "present = 1 WHERE id = :id" % stamp, row)
                    if changed or int(row["play_count"]) != int(old[0] or 0):
                        updated += 1
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
        "last_played FROM items WHERE media IN ('movie', 'episode') "
        "ORDER BY last_played DESC").fetchall()
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
            "MAX(last_seen) FROM items "
            "WHERE media IN ('movie', 'episode')").fetchone()
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


def relevance(name, term):
    """How well a title answers what was typed. Lower is better.

    Ordering by recency, as this once did, answers a question nobody asked:
    typing "star" offered Star Trek Into Darkness before Star Wars because
    it had been played more recently, and an exact title match could be
    beaten by anything watched last week. Match quality first, and let
    recency break the ties it is actually good for.
    """
    name = (name or "").lower()
    term = (term or "").lower().strip()
    if not term:
        return 4
    if name == term:
        return 0
    if name.startswith(term):
        return 1
    # A word starting with the term: "star" should find "Lone Star" ahead of
    # "Starship", but well behind "Star Wars".
    if any(word.startswith(term) for word in re.split(r"[^\w']+", name)):
        return 2
    return 3


def search(term, limit=25):
    """Films and shows matching the term -> {movies: [...], shows: [...]}.

    Searches the mirror, so it answers instantly and works offline. The
    mirror holds only what has been *watched*, which is why the caller
    (webserver) merges in the unwatched side from the catalogue: a film
    sitting on the server unwatched is exactly the one somebody is most
    likely to be searching for, and for a long time it could not be found
    at all.

    Shows are grouped from their episodes. `limit` of None returns every
    match: the caller that merges the unwatched side in has to see all of
    them to know what it would be duplicating, and does its own ranking
    and cutting afterwards.
    """
    if not (term or "").strip():
        return {"movies": [], "shows": []}
    pattern = _like(term)
    connection = connect()
    movies = [{
        "id": row[0], "name": row[1], "year": row[2], "rating": row[3],
        "last_played": row[4], "play_count": row[5], "seen": True,
    } for row in connection.execute(
        "SELECT id, name, year, rating, last_played, play_count FROM items "
        "WHERE media = 'movie' AND ULOWER(name) LIKE ? ESCAPE '\\'",
        (pattern,))]
    shows = [{
        "name": row[0], "episodes": row[1], "last_played": row[2],
        "seen": True,
    } for row in connection.execute(
        "SELECT series_name, COUNT(*), MAX(last_played) FROM items "
        "WHERE media = 'episode' AND series_name IS NOT NULL "
        "AND ULOWER(series_name) LIKE ? ESCAPE '\\' "
        "GROUP BY series_name", (pattern,))]
    connection.close()
    return {"movies": rank_hits(movies, term, limit),
            "shows": rank_hits(shows, term, limit)}


def rank_hits(hits, term, limit):
    """Best match first; within equal matches, most recently watched.

    Done as two stable sorts rather than one clever key. The tie-break
    wants timestamps newest-first while everything above it wants
    ascending order, and a single key cannot express both without
    inverting strings by hand.
    """
    hits.sort(key=lambda hit: ((hit.get("last_played") or ""),
                               (hit["name"] or "").lower()), reverse=True)
    hits.sort(key=lambda hit: (relevance(hit["name"], term),
                               not hit.get("seen")))
    return hits[:limit]


def _sittings(connection, show, title=None, year=None):
    """Play-log rows for one film or one whole show, newest first.

    A film is matched on year as well as title where one is known. Title
    alone pooled the viewing history of same-name remakes - Dune 1984 and
    Dune 2021 each showed the union of both - which is the very confusion
    the duplicates feature elsewhere exists to point out. Rows logged
    without a year still match, since dropping them would lose real
    sittings to fix a rarer complaint.
    """
    if title is not None:
        where = ("ULOWER(COALESCE(show, '')) = '' AND ULOWER(title) = ? "
                 "AND media = 'movie'")
        params = [(title or "").strip().lower()]
        if year:
            where += " AND (year = ? OR year IS NULL)"
            params.append(year)
    else:
        where = "ULOWER(COALESCE(show, '')) = ?"
        params = [(show or "").strip().lower()]
    return [{
        "started_at": row[0], "ended_at": row[1], "title": row[2],
        "season": row[3], "episode": row[4], "watched_seconds": row[5],
        "completed": bool(row[6]), "source": row[7], "device": row[8],
    } for row in connection.execute(
        "SELECT started_at, ended_at, title, season, episode, "
        "watched_seconds, completed, source, device FROM plays "
        "WHERE %s ORDER BY started_at DESC LIMIT 200" % where, params)]


def dupe_key(name, year):
    """What makes two library entries the same film.

    Title and year, with punctuation and case thrown away. Not the id -
    duplicates have different ids, that being the whole problem - and not
    the title alone, because remakes are not duplicates and "Alien" and
    "Aliens" are certainly not.
    """
    flat = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    return "%s|%s" % (flat, year or "")


def duplicates_of(connection, item_id, name, year, media="movie"):
    """Other entries in the library that are the same title as this one.

    Jellyfin is happy to hold a film twice - two rips, two folders, a
    re-import - and nothing in the interface says so. It matters here
    because plays, ratings and the counts on every other page get split
    between the copies, so a film watched twice can look watched once.
    """
    key = dupe_key(name, year)
    out = []
    for row in connection.execute(
            "SELECT id, name, year, play_count, last_played, user_rating "
            "FROM items WHERE media = ? AND id != ?", (media, item_id)):
        if dupe_key(row[1], row[2]) != key:
            continue
        out.append({"id": row[0], "name": row[1], "year": row[2],
                    "play_count": row[3], "last_played": row[4],
                    "user_rating": row[5]})
    return out


def sittings_for(title=None, show=None):
    """Every logged viewing of one film or one show, newest first.

    Split out from the item pages so a list can offer "when did I watch
    this" without loading a whole detail page, artwork and cast included,
    for a question that is four rows of dates.
    """
    connection = connect()
    try:
        return _sittings(connection, show, title=title)
    finally:
        connection.close()


def browse_rows(media):
    """Every title of a medium the mirror holds, flat, for the library page.

    Films come straight from items. Shows are assembled from their episodes
    - the series row carries the name and artwork, the episodes carry what
    was actually watched - so "how much of this have I seen" is answered by
    counting them rather than by a play_count that series rows do not keep.
    """
    connection = connect()
    if media == "movies":
        rows = [{
            "id": r[0], "name": r[1], "year": r[2],
            "genres": json.loads(r[3] or "[]"), "rating": r[4],
            "user_rating": r[5], "play_count": r[6] or 0,
            "last_played": r[7], "favourite": bool(r[8]),
            "runtime_minutes": r[9], "seen": bool(r[6]), "episodes": None,
        } for r in connection.execute(
            "SELECT id, name, year, genres, rating, user_rating, play_count, "
            "last_played, favourite, runtime_minutes FROM items "
            "WHERE media = 'movie'")]
        connection.close()
        return rows

    watched = {}
    for name, count, last in connection.execute(
            "SELECT series_name, COUNT(*), MAX(last_played) FROM items "
            "WHERE media = 'episode' AND series_name IS NOT NULL "
            "AND play_count > 0 GROUP BY series_name"):
        watched[(name or "").strip().lower()] = (count, last)
    rows = []
    for r in connection.execute(
            "SELECT id, name, year, genres, rating, user_rating, favourite "
            "FROM items WHERE media = 'series'"):
        episodes, last = watched.get((r[1] or "").strip().lower(), (0, None))
        rows.append({
            "id": r[0], "name": r[1], "year": r[2],
            "genres": json.loads(r[3] or "[]"), "rating": r[4],
            "user_rating": r[5], "play_count": episodes,
            "last_played": last, "favourite": bool(r[6]),
            "runtime_minutes": None, "seen": episodes > 0,
            "episodes": episodes,
        })
    connection.close()
    return rows


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
        "sittings": _sittings(connection, None, title=row[1],
                              year=row[2]),
    }
    detail["duplicates"] = duplicates_of(connection, row[0], row[1], row[2])
    connection.close()
    return detail


def show_detail(name):
    """One show: its episodes from the mirror, its sittings from the log."""
    connection = connect()
    rows = connection.execute(
        "SELECT name, season, episode, rating, play_count, last_played, "
        "genres, present, series_id, id FROM items WHERE media = 'episode' "
        "AND ULOWER(series_name) = ? "
        "ORDER BY last_played DESC",
        ((name or "").strip().lower(),)).fetchall()
    if not rows:
        connection.close()
        return None
    episodes = [{
        "name": row[0], "season": row[1], "episode": row[2],
        "rating": row[3], "play_count": row[4], "last_played": row[5],
        "present": bool(row[7]), "id": row[9],
    } for row in rows]
    seasons = {}
    for episode in episodes:
        season = episode["season"] if episode["season"] is not None else 0
        seasons[season] = seasons.get(season, 0) + 1
    ratings = [e["rating"] for e in episodes if e["rating"]]
    detail = {
        "name": name,
        # The series id lets the page ask Jellyfin for artwork, cast and the
        # full episode list, none of which the mirror stores.
        "series_id": rows[0][8],
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
