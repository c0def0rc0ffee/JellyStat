# -*- coding: utf-8 -*-
"""
<summary>
A Jellyfin server that only exists to be screenshotted.
</summary>
<remarks>
It answers the handful of endpoints JellyStat actually calls, out of the
generated demo library, so the addon runs its real code path (fetch, mirror,
build) against something that behaves like a server rather than against a
monkeypatched stub.
</remarks>
"""

import hashlib
import io
import json
import random
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from PIL import Image, ImageDraw, ImageFont

import catalogue
import generate

TICKS_PER_MINUTE = 600000000
USER_ID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
TOKEN = "demoapikeydemoapikeydemoapikey00"

DATA = None
_index = {}
_people = {}
_posters = {}
_poster_lock = threading.Lock()

ROLES = ["Lead", "Supporting", "Ensemble", "Featured", "Guest"]

# Poster colours, picked to sit apart from each other in a grid.
PALETTE = [
    (34, 51, 74), (72, 42, 58), (38, 66, 60), (79, 61, 34),
    (48, 40, 78), (30, 58, 78), (68, 48, 40), (44, 62, 44),
    (60, 36, 46), (36, 46, 70), (74, 66, 44), (40, 68, 72),
]


def _utc(local):
    """
    <summary>
    The demo timeline is local wall time; Jellyfin hands out UTC.
    </summary>
    """
    offset = datetime.now() - datetime.utcnow()
    return (local - offset).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


def _media_sources(rng, item):
    """
    <summary>
    An invented media source for an item: resolution, codec, size and audio.
    </summary>
    <param name="rng">A seeded Random.</param>
    <param name="item">A catalogue item.</param>
    <returns>A one entry list.</returns>
    """
    minutes = item["runtime"]
    height, width = rng.choice([(1080, 1920), (1080, 1920), (2160, 3840),
                                (720, 1280)])
    codec = "hevc" if height >= 2160 else "h264"
    gb = minutes / 60.0 * (9.5 if height >= 2160 else 3.4)
    return [{
        "Container": "mkv",
        "Size": int(gb * 1073741824),
        "MediaStreams": [
            {"Type": "Video", "Codec": codec, "Width": width,
             "Height": height,
             "VideoRange": "HDR" if height >= 2160 else "SDR"},
            {"Type": "Audio", "Codec": "eac3", "Channels": 6},
        ],
    }]


def _cast_rows(item):
    """
    <summary>
    Cast rows for an item: actors with roles and a director, each with an image tag so the dashboard asks for a portrait.
    </summary>
    <param name="item">A catalogue item.</param>
    <returns>A list of person dicts.</returns>
    """
    names = item["people"]

    # PrimaryImageTag is what tells the dashboard a portrait exists; without
    # it every cast card draws its empty placeholder instead of asking.
    def row(name, kind, role):
        """
        <summary>
        One person row with a stable id derived from the name.
        </summary>
        <param name="name">The person.</param>
        <param name="kind">Actor or Director.</param>
        <param name="role">The role, or None.</param>
        <returns>A dict.</returns>
        """
        person_id = hashlib.md5(name.encode()).hexdigest()
        return {"Name": name, "Type": kind, "Role": role, "Id": person_id,
                "PrimaryImageTag": person_id[:16]}

    rows = [row(name, "Actor", ROLES[i % len(ROLES)])
            for i, name in enumerate(names[:-1])]
    rows.append(row(names[-1], "Director", None))
    return rows


def _to_jellyfin(item, rng, full=False):
    """
    <summary>
    Reshape a catalogue item into the Jellyfin item JSON the addon reads.
    </summary>
    <param name="item">A catalogue item.</param>
    <param name="rng">A seeded Random.</param>
    <param name="full">Include the detail fields.</param>
    <returns>A dict.</returns>
    """
    plays = item.get("plays") or []
    row = {
        "Id": item["id"],
        "Name": item["title"],
        "Type": {"movie": "Movie", "episode": "Episode",
                 "series": "Series"}[item["kind"]],
        "ProductionYear": item["year"],
        "Genres": list(item["genres"]),
        "CommunityRating": item["rating"],
        "RunTimeTicks": item["runtime"] * TICKS_PER_MINUTE,
        "ProviderIds": item["provider"],
        "UserData": {
            "PlayCount": len(plays),
            "Played": bool(plays),
            "IsFavorite": item.get("favourite", False),
            "LastPlayedDate": _utc(max(plays)) if plays else None,
        },
    }
    if item.get("critic"):
        row["CriticRating"] = item["critic"]
    if item.get("user_rating"):
        row["UserData"]["Rating"] = item["user_rating"]
    if item["kind"] == "episode":
        row.update({
            "SeriesId": item["series_id"],
            "SeriesName": item["series_name"],
            "ParentIndexNumber": item["season"],
            "IndexNumber": item["episode"],
        })
    if full:
        row["Overview"] = item["overview"]
        row["People"] = _cast_rows(item)
        row["MediaSources"] = _media_sources(rng, item)
        row["Path"] = "/media/%s/%s.mkv" % (item["kind"], item["id"][:8])
    return row


def _poster(item_id, width=400, portrait=False):
    """
    <summary>
    A generated poster or portrait: a gradient in a colour chosen from the id, with the title drawn on, cached per size.
    </summary>
    <param name="item_id">The item or person.</param>
    <param name="width">Pixels.</param>
    <param name="portrait">True for a person.</param>
    <returns>JPEG bytes.</returns>
    """
    with _poster_lock:
        hit = _posters.get((item_id, width))
    if hit:
        return hit
    item = _index.get(item_id)
    if portrait:
        title = _people.get(item_id, "Cast")
    elif item and item["kind"] == "episode":
        title = "%s\nS%d E%d" % (item["series_name"], item["season"],
                                 item["episode"])
    else:
        title = item["title"] if item else "Demo"

    seed = int(hashlib.md5(item_id.encode()).hexdigest()[:8], 16)
    top = PALETTE[seed % len(PALETTE)]
    bottom = tuple(max(0, channel - 18) for channel in top)

    height = int(width * 1.5)
    image = Image.new("RGB", (width, height), top)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        blend = y / float(height)
        draw.line([(0, y), (width, y)], fill=tuple(
            int(top[i] + (bottom[i] - top[i]) * blend) for i in range(3)))
    # A band of lighter colour, so a grid of these does not read as flat.
    draw.rectangle([0, int(height * 0.62), width, int(height * 0.655)],
                   fill=tuple(min(255, channel + 60) for channel in top))
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            max(13, width // 14))
    except OSError:
        font = ImageFont.load_default()

    lines, line = [], ""
    for word in title.replace("\n", " \n ").split(" "):
        if word == "\n":
            lines.append(line.strip())
            line = ""
            continue
        trial = (line + " " + word).strip()
        if draw.textlength(trial, font=font) > width * 0.84 and line:
            lines.append(line)
            line = word
        else:
            line = trial
    if line:
        lines.append(line)

    step = max(16, width // 12)
    y = int(height * 0.70)
    for text in lines[:4]:
        draw.text((width * 0.08, y), text, font=font, fill=(238, 240, 245))
        y += step

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=88)
    blob = buffer.getvalue()
    with _poster_lock:
        _posters[(item_id, width)] = blob
    return blob


class Handler(BaseHTTPRequestHandler):
    """
    <summary>
    Answers the handful of Jellyfin endpoints the addon calls.
    </summary>
    """
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        """
        <summary>
        Silence the access log.
        </summary>
        """
        pass

    def _send(self, payload, status=200):
        """
        <summary>
        Send a JSON response.
        </summary>
        <param name="payload">A JSON serialisable value.</param>
        <param name="status">HTTP status.</param>
        """
        blob = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def _send_image(self, blob):
        """
        <summary>
        Send a JPEG.
        </summary>
        <param name="blob">The bytes.</param>
        """
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def do_GET(self):
        """
        <summary>
        Route the users list, item images, one item, and item queries.
        </summary>
        """
        route = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(route.query).items()}
        parts = [p for p in route.path.split("/") if p]
        rng = random.Random(route.path)

        if parts == ["Users"]:
            return self._send([{"Id": USER_ID, "Name": "demo"}])

        if len(parts) == 4 and parts[0] == "Items" and parts[2] == "Images":
            item_id = parts[1]
            known = item_id in _index
            person = item_id in _people
            if not (known or person):
                return self._send({"error": "no such item"}, 404)
            if parts[3] != "Primary":
                return self._send({"error": "no image"}, 404)
            width = int(query.get("maxWidth") or 400)
            return self._send_image(_poster(item_id, width, portrait=person))

        # /Users/{id}/Items/{itemId}
        if len(parts) == 4 and parts[0] == "Users" and parts[2] == "Items":
            item = _index.get(parts[3])
            if not item:
                return self._send({"error": "not found"}, 404)
            return self._send(_to_jellyfin(item, rng, full=True))

        # /Users/{id}/Items  and  /Items
        if parts == ["Items"] or (len(parts) == 3 and parts[0] == "Users"
                                  and parts[2] == "Items"):
            return self._send(self._items(query, rng))

        return self._send({"error": "unhandled %s" % route.path}, 404)

    def _items(self, query, rng):
        """
        <summary>
        Filter, sort and page the catalogue the way the Jellyfin Items endpoint would.
        </summary>
        <param name="query">Query parameters.</param>
        <param name="rng">A seeded Random.</param>
        <returns>The Items reply dict.</returns>
        """
        wanted = [t for t in (query.get("IncludeItemTypes") or "").split(",")
                  if t]
        pool = []
        if not wanted or "Movie" in wanted:
            pool += DATA["movies"]
        if not wanted or "Episode" in wanted:
            pool += DATA["episodes"]
        if not wanted or "Series" in wanted:
            pool += DATA["series"]

        if (query.get("Filters") or "") == "IsPlayed":
            pool = [i for i in pool if i.get("plays")]
        if query.get("SearchTerm"):
            term = query["SearchTerm"].lower()
            pool = [i for i in pool if term in i["title"].lower()
                    or term in (i.get("series_name") or "").lower()]
        if query.get("Genres"):
            names = set(query["Genres"].split("|"))
            pool = [i for i in pool if names & set(i["genres"])]
        if query.get("ParentId"):
            pool = [i for i in pool if i.get("series_id") == query["ParentId"]]

        if (query.get("SortBy") or "") == "DatePlayed":
            pool = sorted(pool, key=lambda i: max(i["plays"]) if i.get("plays")
                          else datetime.min, reverse=True)

        total = len(pool)
        start = int(query.get("StartIndex") or 0)
        limit = int(query.get("Limit") or 0)
        if limit:
            pool = pool[start:start + limit]
        elif start:
            pool = pool[start:]

        fields = query.get("Fields") or ""
        rows = [_to_jellyfin(i, rng, full="MediaSources" in fields)
                for i in pool]
        return {"Items": rows, "TotalRecordCount": total, "StartIndex": start}


def start(port=8096):
    """
    <summary>
    Build the library, mark favourites and personal scores, then serve.
    </summary>
    """
    global DATA
    DATA = generate.build()
    rng = random.Random(generate.SEED + 7)

    watched_movies = [m for m in DATA["movies"] if m["plays"]]
    for movie in rng.sample(watched_movies, min(14, len(watched_movies))):
        movie["favourite"] = True
    # Personal scores on a good share of the films, and none of the
    # episodes, which is how the rating page tends to look in practice.
    for movie in rng.sample(watched_movies, int(len(watched_movies) * 0.62)):
        drift = rng.choice([-1.5, -1.0, -0.5, 0, 0, 0.5, 0.5, 1.0, 1.5])
        movie["user_rating"] = max(1.0, min(10.0,
                                            round(movie["rating"] + drift)))
    for show in DATA["series"]:
        if rng.random() < 0.4:
            show["favourite"] = True

    for item in DATA["movies"] + DATA["episodes"] + DATA["series"]:
        _index[item["id"]] = item
    for name in catalogue.PEOPLE:
        _people[hashlib.md5(name.encode()).hexdigest()] = name

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, DATA
