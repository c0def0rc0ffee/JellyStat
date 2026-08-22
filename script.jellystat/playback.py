# -*- coding: utf-8 -*-
"""Play or queue something on the Kodi box the dashboard is served from.

The web server runs inside Kodi, so "play this" is a local JSON-RPC call
rather than anything networked - which is also why the dashboard can offer
it at all: the page is served by the very box that would do the playing.

Items are resolved through Kodi's own video library rather than by building
a plugin:// URL. Jellyfin for Kodi stores each item's Jellyfin id inside the
file path it writes (`...&id=<itemid>&mode=play`), so the mapping is exact,
and playing by library id (`movieid`/`episodeid`) is better than playing by
path: Kodi keeps the resume point, marks the item watched, and shows the
right artwork and title in the player.

The library index is built once and cached, because asking Kodi for eight
thousand file paths is not something to do on every button press.
"""

import json
import re
import threading
import time

import xbmc

INDEX_TTL_S = 900
VIDEO_PLAYLIST = 1

# The Jellyfin item id as Jellyfin for Kodi writes it into the file path.
ID_IN_PATH = re.compile(r"[?&]id=([0-9a-f]{32})", re.I)

_lock = threading.Lock()
_index = {"at": 0.0, "map": None}


def log(message, level=xbmc.LOGINFO):
    xbmc.log("[JellyStat] %s" % message, level)


class PlaybackError(Exception):
    """Anything that stops a play or queue request."""


def rpc(method, params=None):
    """One JSON-RPC call into the Kodi this addon is running in."""
    request = {"jsonrpc": "2.0", "id": 1, "method": method,
               "params": params or {}}
    raw = xbmc.executeJSONRPC(json.dumps(request))
    try:
        answer = json.loads(raw)
    except ValueError:
        raise PlaybackError("Kodi returned something that was not JSON.")
    if "error" in answer:
        raise PlaybackError(answer["error"].get("message")
                            or str(answer["error"]))
    return answer.get("result") or {}


# ---------------------------------------------------------------------------
# Resolving a Jellyfin id to a Kodi library item
# ---------------------------------------------------------------------------

def _collect(method, key, id_field):
    found = {}
    result = rpc(method, {"properties": ["file"]})
    for entry in result.get(key) or []:
        match = ID_IN_PATH.search(entry.get("file") or "")
        if match:
            found[match.group(1).lower()] = {"type": id_field,
                                             "dbid": entry[id_field]}
    return found


def index(force=False):
    """Jellyfin item id -> {type, dbid}, for everything Kodi has."""
    with _lock:
        fresh = (_index["map"] is not None
                 and (time.time() - _index["at"]) < INDEX_TTL_S)
        if fresh and not force:
            return _index["map"]
        mapping = {}
        mapping.update(_collect("VideoLibrary.GetMovies", "movies", "movieid"))
        mapping.update(_collect("VideoLibrary.GetEpisodes", "episodes",
                                "episodeid"))
        _index.update(at=time.time(), map=mapping)
        log("Playback index built: %d items" % len(mapping))
        return mapping


def resolve(item_id):
    """The Kodi library entry for a Jellyfin id, or None.

    Retries with a fresh index once, so something added to the library since
    the last build still plays rather than reporting "not in the library".
    """
    key = (item_id or "").lower()
    entry = index().get(key)
    if entry is None:
        entry = index(force=True).get(key)
    return entry


def _rpc_item(entry):
    return {entry["type"]: entry["dbid"]}


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def _label(entry):
    method = ("VideoLibrary.GetMovieDetails" if entry["type"] == "movieid"
              else "VideoLibrary.GetEpisodeDetails")
    key = "moviedetails" if entry["type"] == "movieid" else "episodedetails"
    field = entry["type"]
    try:
        details = rpc(method, {field: entry["dbid"],
                               "properties": ["title"]}).get(key) or {}
        return details.get("title") or "the item"
    except PlaybackError:
        return "the item"


def play(item_id):
    """Start it now, replacing whatever is playing."""
    entry = resolve(item_id)
    if entry is None:
        raise PlaybackError(
            "That title is not in this Kodi box's library, so it cannot be "
            "played from here. Jellyfin for Kodi syncs the library; an item "
            "added very recently may not have arrived yet.")
    rpc("Player.Open", {"item": _rpc_item(entry)})
    name = _label(entry)
    log("Playing %s on this box" % name)
    return {"action": "play", "name": name}


def queue(item_id):
    """Add to the end of the video playlist, leaving playback alone.

    If nothing is playing the queue would otherwise just sit there, so an
    idle player is started on the item that was queued - which is what
    "queue" means to someone with a stopped box.
    """
    entry = resolve(item_id)
    if entry is None:
        raise PlaybackError(
            "That title is not in this Kodi box's library, so it cannot be "
            "queued from here.")
    rpc("Playlist.Add", {"playlistid": VIDEO_PLAYLIST,
                         "item": _rpc_item(entry)})
    name = _label(entry)
    playing = bool(rpc("Player.GetActivePlayers"))
    position = len(rpc("Playlist.GetItems",
                       {"playlistid": VIDEO_PLAYLIST}).get("items") or [])
    if not playing:
        rpc("Player.Open", {"item": {"playlistid": VIDEO_PLAYLIST,
                                     "position": max(position - 1, 0)}})
        log("Queued and started %s" % name)
        return {"action": "queue-started", "name": name,
                "queued": position}
    log("Queued %s (position %d)" % (name, position))
    return {"action": "queue", "name": name, "queued": position}


def next_unwatched(series_name):
    """The earliest unwatched episode of a show, for the show page's buttons.

    Falls back to the first episode when a show has been watched through, so
    the button always does something rather than greying out on a rewatch.
    """
    result = rpc("VideoLibrary.GetTVShows", {"properties": ["title"]})
    wanted = (series_name or "").strip().lower()
    show = next((entry for entry in result.get("tvshows") or []
                 if (entry.get("title") or "").strip().lower() == wanted),
                None)
    if show is None:
        raise PlaybackError("That show is not in this Kodi box's library.")
    episodes = rpc("VideoLibrary.GetEpisodes", {
        "tvshowid": show["tvshowid"],
        "properties": ["title", "season", "episode", "playcount", "file"],
        "sort": {"method": "episode", "order": "ascending"},
    }).get("episodes") or []
    if not episodes:
        raise PlaybackError("That show has no episodes in the library.")
    unseen = [ep for ep in episodes if not (ep.get("playcount") or 0)]
    chosen = (unseen or episodes)[0]
    return {
        "episodeid": chosen["episodeid"],
        "label": "S%02dE%02d %s" % (chosen.get("season") or 0,
                                    chosen.get("episode") or 0,
                                    chosen.get("title") or ""),
        "unwatched": bool(unseen),
    }


def play_show(series_name, action="play"):
    """Play or queue a show's next unwatched episode."""
    chosen = next_unwatched(series_name)
    item = {"episodeid": chosen["episodeid"]}
    if action == "queue":
        rpc("Playlist.Add", {"playlistid": VIDEO_PLAYLIST, "item": item})
        playing = bool(rpc("Player.GetActivePlayers"))
        position = len(rpc("Playlist.GetItems",
                           {"playlistid": VIDEO_PLAYLIST}).get("items") or [])
        if not playing:
            rpc("Player.Open", {"item": {"playlistid": VIDEO_PLAYLIST,
                                         "position": max(position - 1, 0)}})
            return {"action": "queue-started", "name": chosen["label"],
                    "queued": position}
        return {"action": "queue", "name": chosen["label"],
                "queued": position}
    rpc("Player.Open", {"item": item})
    return {"action": "play", "name": chosen["label"]}


def _seconds(clock):
    if not isinstance(clock, dict):
        return 0
    return ((clock.get("hours") or 0) * 3600 + (clock.get("minutes") or 0) * 60
            + (clock.get("seconds") or 0))


def status():
    """What the box is doing now, in enough detail to draw the popup.

    Carries position and length so the page can show a progress bar and the
    clock time it will finish at, which is the thing anyone actually wants
    to know mid-episode.
    """
    players = rpc("Player.GetActivePlayers")
    queued = len(rpc("Playlist.GetItems",
                     {"playlistid": VIDEO_PLAYLIST}).get("items") or [])
    if not players:
        return {"playing": False, "queued": queued}
    player_id = players[0]["playerid"]
    item = rpc("Player.GetItem", {
        "playerid": player_id,
        "properties": ["title", "season", "episode", "showtitle", "file",
                       "thumbnail", "art", "runtime"],
    }).get("item") or {}
    props = rpc("Player.GetProperties", {
        "playerid": player_id,
        "properties": ["time", "totaltime", "percentage", "speed"],
    })
    position = _seconds(props.get("time"))
    total = _seconds(props.get("totaltime"))
    remaining = max(total - position, 0)

    title = item.get("title") or "Something"
    show = item.get("showtitle") or None
    label = title
    code = None
    if show:
        code = "S%d \u00b7 E%d" % (item.get("season") or 0,
                                    item.get("episode") or 0)
        label = "%s - %s" % (code, title)

    # The Jellyfin id is in the path Jellyfin for Kodi wrote, so the popup
    # can show real artwork rather than Kodi's cached thumbnail.
    found = ID_IN_PATH.search(item.get("file") or "")
    return {
        "playing": True,
        "paused": (props.get("speed") or 0) == 0,
        "now": ("%s %s" % (show, label)) if show else title,
        "title": title,
        "show": show,
        "code": code,
        "label": label,
        "item_id": found.group(1).lower() if found else None,
        "position": position,
        "total": total,
        "remaining": remaining,
        "percent": round(props.get("percentage") or 0.0, 1),
        "queued": queued,
    }
