# -*- coding: utf-8 -*-
"""Named sets of titles: your own, and the ones a Trakt export carries.

A list is the one thing the rest of the addon has no way to express. The
library mirror answers "what is on the server", the play log answers "what
did I watch and when", ratings answer "what did I think of it". None of
them can hold *"films to watch with Dad at Christmas"* - a set somebody
decided on, with no property in common that a query could find.

Two things make a list here rather than a saved search:

- **It survives not being on the server.** An entry keeps the title, year
  and provider ids it was created with, and matches to a library item where
  one exists. So a list imported from Trakt still names all forty films
  after you have only twenty of them, and the other twenty attach
  themselves the day they arrive rather than being lost at import time.
  `attach()` is what re-runs that matching.

- **It has an order.** A list somebody ranked by hand is not the same
  object as the same titles alphabetically, so `rank` is stored and a
  Trakt import keeps the order it was given.

Entries are deduplicated within a list by `key()`: the library item id
where the entry matched one, and a normalised title identity where it did
not. Both halves are needed. Keying on the item id alone would let the
same unmatched film be added to a list five times, and keying on the title
alone would treat two different films sharing a name as one entry.
"""

import re
import sqlite3
from datetime import datetime

import xbmc

import library
import playlog

SCHEMA = """
CREATE TABLE IF NOT EXISTS lists (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    name_key    TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT 'mine',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS lists_name ON lists (name_key);
CREATE TABLE IF NOT EXISTS list_items (
    id       INTEGER PRIMARY KEY,
    list_id  INTEGER NOT NULL,
    entry_key TEXT NOT NULL,
    item_id  TEXT,
    media    TEXT NOT NULL,
    title    TEXT NOT NULL,
    show     TEXT,
    season   INTEGER,
    episode  INTEGER,
    year     INTEGER,
    imdb_id  TEXT,
    tmdb_id  TEXT,
    tvdb_id  TEXT,
    rank     INTEGER,
    added_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS list_items_key
    ON list_items (list_id, entry_key);
CREATE INDEX IF NOT EXISTS list_items_list ON list_items (list_id);
CREATE INDEX IF NOT EXISTS list_items_item ON list_items (item_id);
"""

MAX_NAME = 120


def log(message, level=xbmc.LOGINFO):
    xbmc.log("[JellyStat] %s" % message, level)


class ListError(Exception):
    """Anything a person did to a list that could not be done."""


def norm(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def now():
    return datetime.now().strftime(playlog.STAMP)


def connect():
    """A connection with both the library schema and this one in place."""
    connection = library.connect()
    connection.executescript(SCHEMA)
    return connection


def key(item_id, media, title, show=None, season=None, episode=None,
        year=None):
    """The identity an entry is deduplicated by within one list.

    A matched entry is its library item and nothing else, so the same film
    added from the catalogue and from a Trakt import is one entry. An
    unmatched one falls back to what it does have - which is why the year
    is in here: two films called "Persuasion" are two entries, not one.
    """
    if item_id:
        return "id:%s" % item_id
    if media == "episode":
        return "ep:%s|%s|%s" % (norm(show), season, episode)
    return "t:%s|%s|%s" % (media, norm(title), year or "")


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _row_to_list(row):
    return {"id": row[0], "name": row[1], "description": row[2],
            "source": row[3], "created_at": row[4], "updated_at": row[5],
            "count": row[6], "matched": row[7]}


def all_lists(connection=None):
    """Every list with its size, newest activity first."""
    owned = connection is None
    connection = connection or connect()
    try:
        rows = connection.execute(
            "SELECT l.id, l.name, l.description, l.source, l.created_at, "
            "l.updated_at, COUNT(i.id), "
            "COUNT(CASE WHEN i.item_id IS NOT NULL THEN 1 END) "
            "FROM lists l LEFT JOIN list_items i ON i.list_id = l.id "
            "GROUP BY l.id ORDER BY l.updated_at DESC, l.name"
        ).fetchall()
    finally:
        if owned:
            connection.close()
    return [_row_to_list(row) for row in rows]


def get(list_id, connection=None):
    """One list and its entries in order, or None."""
    owned = connection is None
    connection = connection or connect()
    try:
        row = connection.execute(
            "SELECT l.id, l.name, l.description, l.source, l.created_at, "
            "l.updated_at, COUNT(i.id), "
            "COUNT(CASE WHEN i.item_id IS NOT NULL THEN 1 END) "
            "FROM lists l LEFT JOIN list_items i ON i.list_id = l.id "
            "WHERE l.id = ? GROUP BY l.id", (list_id,)).fetchone()
        if row is None or row[0] is None:
            return None
        out = _row_to_list(row)
        # Left-joined to the mirror so an entry can show what the library
        # knows now - the poster year, whether it has been played - rather
        # than only what it was imported with.
        entries = connection.execute(
            "SELECT i.id, i.item_id, i.media, i.title, i.show, i.season, "
            "i.episode, i.year, i.rank, i.added_at, it.play_count, "
            "it.user_rating, it.present "
            "FROM list_items i LEFT JOIN items it ON it.id = i.item_id "
            "WHERE i.list_id = ? "
            "ORDER BY i.rank IS NULL, i.rank, i.added_at, i.id",
            (list_id,)).fetchall()
    finally:
        if owned:
            connection.close()
    out["items"] = [
        {"id": e[0], "item_id": e[1], "media": e[2], "title": e[3],
         "show": e[4], "season": e[5], "episode": e[6], "year": e[7],
         "rank": e[8], "added_at": e[9],
         "play_count": e[10] or 0, "user_rating": e[11],
         # An entry that matched an item which has since left the server is
         # still a real entry. It is flagged, not hidden.
         "in_library": bool(e[1]) and bool(e[12])}
        for e in entries]
    return out


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def create(name, description="", source="mine", connection=None):
    """A new empty list. Raises if the name is taken."""
    name = (name or "").strip()
    if not name:
        raise ListError("A list needs a name.")
    if len(name) > MAX_NAME:
        raise ListError("That name is longer than %d characters." % MAX_NAME)
    if connection is not None:
        # Already inside somebody else's transaction - the Trakt import
        # creates lists in the same one that writes the plays, so that a
        # failure halfway leaves neither. Committing here would end that
        # transaction early and quietly break the guarantee.
        return _create(connection, name, description, source)
    connection = connect()
    try:
        with connection:
            return _create(connection, name, description, source)
    finally:
        connection.close()


def _create(connection, name, description="", source="mine"):
    """The insert half of create(), for callers already in a transaction."""
    stamp = now()
    try:
        cursor = connection.execute(
            "INSERT INTO lists (name, name_key, description, source, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (name, norm(name), description or "", source, stamp, stamp))
    except sqlite3.IntegrityError:
        raise ListError("You already have a list called %r." % name)
    return cursor.lastrowid


def find_by_name(name, connection=None):
    """The id of the list with this name, or None."""
    owned = connection is None
    connection = connection or connect()
    try:
        row = connection.execute(
            "SELECT id FROM lists WHERE name_key = ?",
            (norm(name),)).fetchone()
    finally:
        if owned:
            connection.close()
    return row[0] if row else None


def rename(list_id, name, description=None, connection=None):
    name = (name or "").strip()
    if not name:
        raise ListError("A list needs a name.")
    if len(name) > MAX_NAME:
        raise ListError("That name is longer than %d characters." % MAX_NAME)
    owned = connection is None
    connection = connection or connect()
    try:
        try:
            with connection:
                if description is None:
                    connection.execute(
                        "UPDATE lists SET name = ?, name_key = ?, "
                        "updated_at = ? WHERE id = ?",
                        (name, norm(name), now(), list_id))
                else:
                    connection.execute(
                        "UPDATE lists SET name = ?, name_key = ?, "
                        "description = ?, updated_at = ? WHERE id = ?",
                        (name, norm(name), description, now(), list_id))
        except sqlite3.IntegrityError:
            raise ListError("You already have a list called %r." % name)
    finally:
        if owned:
            connection.close()


def delete(list_id, connection=None):
    """Remove a list and its entries.

    The entries go explicitly rather than by cascade: foreign keys are off
    by default in SQLite and turning them on for one table would be a
    surprise to every other writer sharing this connection.
    """
    owned = connection is None
    connection = connection or connect()
    try:
        with connection:
            connection.execute("DELETE FROM list_items WHERE list_id = ?",
                               (list_id,))
            connection.execute("DELETE FROM lists WHERE id = ?", (list_id,))
    finally:
        if owned:
            connection.close()


def _touch(connection, list_id):
    connection.execute("UPDATE lists SET updated_at = ? WHERE id = ?",
                       (now(), list_id))


def add(list_id, entry, connection=None):
    """Add one entry. Returns True if it was new, False if already there.

    Silent about duplicates by design: adding a film that is already on the
    list is not an error a person needs telling about, it is a no-op they
    expected to be one.
    """
    owned = connection is None
    connection = connection or connect()
    try:
        with connection:
            added = _add(connection, list_id, entry)
            if added:
                _touch(connection, list_id)
        return added
    finally:
        if owned:
            connection.close()


def _add(connection, list_id, entry):
    """The insert half of add(), for callers already inside a transaction."""
    ids = entry.get("ids") or {}
    entry_key = key(entry.get("item_id"), entry.get("media"),
                    entry.get("title"), entry.get("show"),
                    entry.get("season"), entry.get("episode"),
                    entry.get("year"))
    try:
        connection.execute(
            "INSERT INTO list_items (list_id, entry_key, item_id, media, "
            "title, show, season, episode, year, imdb_id, tmdb_id, tvdb_id, "
            "rank, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?)",
            (list_id, entry_key, entry.get("item_id"),
             entry.get("media") or "movie", entry.get("title") or "?",
             entry.get("show"), entry.get("season"), entry.get("episode"),
             entry.get("year"), ids.get("imdb"), ids.get("tmdb"),
             ids.get("tvdb"), entry.get("rank"),
             entry.get("added_at") or now()))
    except sqlite3.IntegrityError:
        return False
    return True


def remove(list_id, entry_id, connection=None):
    owned = connection is None
    connection = connection or connect()
    try:
        with connection:
            cursor = connection.execute(
                "DELETE FROM list_items WHERE list_id = ? AND id = ?",
                (list_id, entry_id))
            if cursor.rowcount:
                _touch(connection, list_id)
        return bool(cursor.rowcount)
    finally:
        if owned:
            connection.close()


def clear(list_id, connection=None):
    owned = connection is None
    connection = connection or connect()
    try:
        with connection:
            connection.execute("DELETE FROM list_items WHERE list_id = ?",
                               (list_id,))
            _touch(connection, list_id)
    finally:
        if owned:
            connection.close()


def add_item(list_id, item_id, connection=None):
    """Add a library item to a list, by its Jellyfin id.

    The entry is filled in from the mirror rather than from whatever the
    caller happened to send, so a list built from the dashboard and one
    built from an import hold the same fields.
    """
    owned = connection is None
    connection = connection or connect()
    try:
        row = connection.execute(
            "SELECT id, media, name, series_name, season, episode, year, "
            "imdb_id, tmdb_id, tvdb_id FROM items WHERE id = ?",
            (item_id,)).fetchone()
        if row is None:
            raise ListError("That title is not in your library.")
        entry = {"item_id": row[0], "media": row[1], "title": row[2],
                 "show": row[3], "season": row[4], "episode": row[5],
                 "year": row[6],
                 "ids": {"imdb": row[7], "tmdb": row[8], "tvdb": row[9]}}
        with connection:
            added = _add(connection, list_id, entry)
            if added:
                _touch(connection, list_id)
        return added
    finally:
        if owned:
            connection.close()


# ---------------------------------------------------------------------------
# Re-matching
# ---------------------------------------------------------------------------

def attach(connection=None, index=None):
    """Match unmatched entries against the library again.

    An imported list names titles this server may not have had yet. Rather
    than deciding at import time that those entries are junk, they are kept
    with their provider ids and run past the library whenever this is
    called - so the day a film is added, the list it has been sitting on
    for a year starts pointing at it.

    Returns how many found a home. Imports trakt lazily: that module pulls
    in the Jellyfin client, and this one is used by the dashboard on paths
    that have no business requiring a configured server.
    """
    import trakt
    owned = connection is None
    connection = connection or connect()
    try:
        rows = connection.execute(
            "SELECT id, list_id, media, title, show, season, episode, year, "
            "imdb_id, tmdb_id, tvdb_id FROM list_items "
            "WHERE item_id IS NULL").fetchall()
        if not rows:
            return 0
        index = index or trakt.Index()
        found = 0
        with connection:
            for (row_id, list_id, media, title, show, season, episode, year,
                 imdb, tmdb, tvdb) in rows:
                ids = {}
                for name, value in (("imdb", imdb), ("tmdb", tmdb),
                                    ("tvdb", tvdb)):
                    if value:
                        ids[name] = value
                item_id, _ = index.find(media, ids, title, show=show,
                                        season=season, episode=episode,
                                        year=year)
                if not item_id:
                    continue
                # The entry key is derived from the item id once matched, so
                # it has to be rewritten too. Where that collides, the list
                # already held this title under its matched identity and
                # this row is the same thing twice - so it goes.
                new_key = key(item_id, media, title, show, season, episode,
                              year)
                try:
                    connection.execute(
                        "UPDATE list_items SET item_id = ?, entry_key = ? "
                        "WHERE id = ?", (item_id, new_key, row_id))
                except sqlite3.IntegrityError:
                    connection.execute("DELETE FROM list_items WHERE id = ?",
                                       (row_id,))
                found += 1
        return found
    finally:
        if owned:
            connection.close()
