# -*- coding: utf-8 -*-
"""Artwork, cast and technical detail, fetched live from Jellyfin.

The mirror deliberately stores facts, not pictures: images are large, they
change rarely, and Jellyfin is already a perfectly good image server. So the
dashboard asks this module, which fetches from Jellyfin with the addon's own
token and hands the bytes to the browser.

That proxying is the point. The browser showing the dashboard is often on a
different device from the Kodi box, may not be able to reach the Jellyfin
server at all, and must never be handed an access token. Routing artwork
through the addon keeps the token on the box and means a phone that can
reach the dashboard can see the pictures.

Everything here is best effort: a missing poster is a missing poster, never
a broken page.
"""

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict

import xbmc
import xbmcaddon

import main as core

ADDON_ID = "script.jellystat"

# Jellyfin ids are 32 hex characters. Anything else never reaches a URL.
ID_RE = re.compile(r"^[0-9a-f]{32}$", re.I)

IMAGE_KINDS = ("Primary", "Backdrop", "Thumb", "Logo")
IMAGE_TTL_S = 3600
IMAGE_CACHE_MAX = 120          # roughly 10-25 MB of posters
DETAIL_TTL_S = 600

_image_lock = threading.Lock()
_images = OrderedDict()        # (id, kind, width) -> (at, content_type, bytes)
_detail_lock = threading.Lock()
_details = {}                  # id -> (at, payload)


def log(message, level=xbmc.LOGINFO):
    xbmc.log("[JellyStat] %s" % message, level)


class MediaError(Exception):
    """Anything that stops artwork or detail being fetched."""


def valid_id(value):
    return bool(value and ID_RE.match(value))


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

# Judged on width, not height: a 2.39:1 film is 3840x1600, and going by
# height would call that 1080p.
RESOLUTIONS = ((3000, "4K", 2160), (1800, "1080p", 1080),
               (1200, "720p", 720), (900, "576p", 576), (0, "480p", 480))


def resolution_of(width, height):
    """A short label and its nominal line count, or None if unknown."""
    if not width:
        return None
    for threshold, label, lines in RESOLUTIONS:
        if width >= threshold:
            return {"label": label, "lines": lines,
                    "width": width, "height": height}
    return None


def _video_stream(source):
    for stream in (source.get("MediaStreams") or []):
        if stream.get("Type") == "Video":
            return stream
    return {}


def media_info(item):
    """Resolution, codec, container and size from an item's first source."""
    sources = item.get("MediaSources") or []
    if not sources:
        return None
    source = sources[0]
    stream = _video_stream(source)
    info = resolution_of(stream.get("Width"), stream.get("Height")) or {}
    info.update({
        "codec": (stream.get("Codec") or "").upper() or None,
        "range": stream.get("VideoRange"),
        "container": (source.get("Container") or "").upper() or None,
        "size_gb": (round(source.get("Size") / 1073741824.0, 1)
                    if source.get("Size") else None),
    })
    return info if info.get("label") else None


def _size_label(size):
    """Bytes as something readable, without pretending to precision."""
    if not size:
        return None
    gb = size / 1073741824.0
    if gb >= 1:
        return "%.1f GB" % gb
    return "%d MB" % round(size / 1048576.0)


def files(item_id):
    """Every file Jellyfin holds for one item: where it is, how big, what
    quality.

    Exists for the duplicate note. Knowing a film is in the library twice is
    only half an answer - the useful half is which two files, so the worse
    one can be deleted, and that means the path, the size and the quality
    side by side.

    Jellyfin only returns Path to a user allowed to see it, so this asks
    with the addon's own API key when one is configured (the same key the
    community-rating write needs) and falls back to the ordinary user
    token. Where the path is withheld, everything else is still returned
    and the caller says so rather than showing a blank.
    """
    if not valid_id(item_id):
        raise MediaError("Not a Jellyfin id.")
    creds = core.get_credentials()
    params = {"Fields": "MediaSources,Path"}
    base, key = _admin_credentials()
    if base and key:
        item = core.api_get(base, key, "/Items/%s" % item_id, params)
    else:
        item = core.api_get(creds["base"], creds["token"],
                            "/Users/%s/Items/%s" % (creds["user_id"],
                                                    item_id), params)
    out = []
    for source in (item.get("MediaSources") or []):
        stream = _video_stream(source)
        quality = resolution_of(stream.get("Width"),
                                stream.get("Height")) or {}
        path = source.get("Path") or item.get("Path") or None
        out.append({
            "path": path,
            # Both separators: the server may be running on either, and the
            # box reading this is not necessarily the one holding the file.
            "filename": path.replace("\\", "/").rsplit("/", 1)[-1]
                        if path else None,
            "size": source.get("Size"),
            "size_label": _size_label(source.get("Size")),
            "quality": quality.get("label"),
            "width": quality.get("width"),
            "height": quality.get("height"),
            "codec": (stream.get("Codec") or "").upper() or None,
            "range": stream.get("VideoRange"),
            "container": (source.get("Container") or "").upper() or None,
        })
    return out


def _admin_credentials():
    """The addon's configured API key, which may see paths a user cannot."""
    try:
        addon = xbmcaddon.Addon(ADDON_ID)
    except Exception:
        return None, None
    base = (addon.getSetting("server_url") or "").strip().rstrip("/")
    key = (addon.getSetting("api_key") or "").strip()
    return (base, key) if (base and key) else (None, None)


def runtime_minutes(item):
    ticks = item.get("RunTimeTicks")
    return int(ticks // 600000000) if ticks else None


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

def fetch_image(item_id, kind="Primary", width=400):
    """Bytes and content type for one image, cached in memory.

    Cached because a grid of forty posters re-requests the same handful of
    images every time the page is opened, and a Kodi box on a home network
    should not have to ask Jellyfin each time.
    """
    if not valid_id(item_id):
        raise MediaError("Not a Jellyfin id.")
    if kind not in IMAGE_KINDS:
        kind = "Primary"
    width = max(48, min(int(width or 400), 1920))
    key = (item_id.lower(), kind, width)

    with _image_lock:
        hit = _images.get(key)
        if hit and (time.time() - hit[0]) < IMAGE_TTL_S:
            _images.move_to_end(key)
            return hit[1], hit[2]

    creds = core.get_credentials()
    url = "%s/Items/%s/Images/%s?%s" % (
        creds["base"], item_id, kind,
        urllib.parse.urlencode({"maxWidth": width, "quality": 90}))
    request = urllib.request.Request(url, headers={
        "X-Emby-Token": creds["token"]})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            content_type = response.headers.get("Content-Type") or "image/jpeg"
            blob = response.read()
    except urllib.error.HTTPError as err:
        # 404 is ordinary: plenty of items have no logo or thumb.
        raise MediaError("No %s image (HTTP %d)" % (kind, err.code))
    except (urllib.error.URLError, OSError) as err:
        raise MediaError("Could not reach Jellyfin for artwork: %s" % err)

    with _image_lock:
        _images[key] = (time.time(), content_type, blob)
        _images.move_to_end(key)
        while len(_images) > IMAGE_CACHE_MAX:
            _images.popitem(last=False)
    return content_type, blob


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------

def _api(path, params=None):
    creds = core.get_credentials()
    return core.api_get(creds["base"], creds["token"], path, params), creds


def _person_rows(item, limit=24):
    """Cast and crew, actors first, only those Jellyfin can picture."""
    people = []
    for entry in (item.get("People") or []):
        if not valid_id(entry.get("Id")):
            continue
        people.append({
            "id": entry["Id"],
            "name": entry.get("Name") or "?",
            "role": entry.get("Role") or None,
            "type": entry.get("Type") or "Actor",
            "image": bool(entry.get("PrimaryImageTag")),
        })
    actors = [p for p in people if p["type"] == "Actor"]
    others = [p for p in people if p["type"] != "Actor"]
    return (actors + others)[:limit]


def detail(item_id):
    """Live Jellyfin detail for one item: overview, cast, technical info."""
    if not valid_id(item_id):
        raise MediaError("Not a Jellyfin id.")
    with _detail_lock:
        hit = _details.get(item_id)
        if hit and (time.time() - hit[0]) < DETAIL_TTL_S:
            return hit[1]

    creds = core.get_credentials()
    item = core.api_get(creds["base"], creds["token"],
                        "/Users/%s/Items/%s" % (creds["user_id"], item_id))
    tags = item.get("ImageTags") or {}
    payload = {
        "id": item.get("Id"),
        "name": item.get("Name"),
        "overview": item.get("Overview") or None,
        "tagline": (item.get("Taglines") or [None])[0],
        "official_rating": item.get("OfficialRating"),
        "runtime_minutes": runtime_minutes(item),
        "media": media_info(item),
        "people": _person_rows(item),
        "studios": [s.get("Name") for s in (item.get("Studios") or [])][:3],
        "has_primary": bool(tags.get("Primary")),
        "has_backdrop": bool(item.get("BackdropImageTags")),
        "has_thumb": bool(tags.get("Thumb")),
    }
    with _detail_lock:
        _details[item_id] = (time.time(), payload)
        if len(_details) > 300:
            _details.clear()      # cheap bound; these are small and rebuild
    return payload


def person(person_id, limit=60):
    """One person and everything of theirs in the library.

    Split into what you have watched and what you have not, because "show me
    only their films" is usually asked with one of those two in mind.
    """
    if not valid_id(person_id):
        raise MediaError("Not a Jellyfin id.")
    creds = core.get_credentials()
    result = core.api_get(creds["base"], creds["token"],
                          "/Users/%s/Items" % creds["user_id"], {
                              "PersonIds": person_id,
                              "Recursive": "true",
                              "IncludeItemTypes": "Movie,Series",
                              "Fields": "Genres,ProductionYear",
                              "SortBy": "ProductionYear",
                              "SortOrder": "Descending",
                              "Limit": limit,
                          })
    titles = []
    for item in result.get("Items") or []:
        user_data = item.get("UserData") or {}
        titles.append({
            "id": item.get("Id"),
            "name": item.get("Name"),
            "year": item.get("ProductionYear"),
            "type": "show" if item.get("Type") == "Series" else "movie",
            "genres": core.effective_genres(item),
            "rating": item.get("CommunityRating"),
            "watched": bool(user_data.get("Played")),
        })
    name = person_id
    try:
        info = core.api_get(creds["base"], creds["token"],
                            "/Users/%s/Items/%s" % (creds["user_id"],
                                                    person_id))
        name = info.get("Name") or person_id
    except Exception:
        pass
    return {
        "id": person_id,
        "name": name,
        "total": result.get("TotalRecordCount") or len(titles),
        "watched": [t for t in titles if t["watched"]],
        "unwatched": [t for t in titles if not t["watched"]],
    }


def episodes(series_id, season=None, limit=200):
    """Episodes of a show with stills, overviews and resolution."""
    if not valid_id(series_id):
        raise MediaError("Not a Jellyfin id.")
    creds = core.get_credentials()
    params = {"userId": creds["user_id"],
              "Fields": "Overview,MediaSources",
              "Limit": limit}
    if season is not None:
        params["season"] = season
    result = core.api_get(creds["base"], creds["token"],
                          "/Shows/%s/Episodes" % series_id, params)
    rows = []
    for item in result.get("Items") or []:
        user_data = item.get("UserData") or {}
        tags = item.get("ImageTags") or {}
        rows.append({
            "id": item.get("Id"),
            "name": item.get("Name"),
            "season": item.get("ParentIndexNumber"),
            "episode": item.get("IndexNumber"),
            "overview": item.get("Overview") or None,
            "runtime_minutes": runtime_minutes(item),
            "media": media_info(item),
            "has_image": bool(tags.get("Primary")),
            "watched": bool(user_data.get("Played")),
            "played_at": (user_data.get("LastPlayedDate") or "")[:19] or None,
        })
    return {"series_id": series_id, "episodes": rows,
            "total": result.get("TotalRecordCount") or len(rows)}
