# -*- coding: utf-8 -*-
"""Your own ratings for watched titles, kept in step with Jellyfin.

What Jellyfin can and cannot store is the whole shape of this module, so it
is worth stating plainly:

- `CommunityRating` is the *public* score (TMDb and friends). It is item
  metadata, not per-user, and writing it needs an administrator key. The
  Kodi login normally is not one, so it answers 403.
- Per user, Jellyfin holds a numeric `UserData.Rating` **and** a thumbs
  up/down `UserData.Likes`. The numeric one is only writable through the
  whole-DTO route, `POST /Users/{user}/Items/{item}/UserData`; the
  convenience route `/UserItems/{item}/Rating?likes=` takes a bool and
  slams Rating to 10 or 0, which is why it is not used here.

So the score is written to Jellyfin as itself: 7 stays 7. Alongside it,

- `Likes` (the thumb Jellyfin's own interface shows) is left to the server,
  which derives it from the number and overrides anything sent alongside;
- optionally the favourite flag for a top score;
- and, only when an administrator API key is configured in the addon's
  settings, the community rating on the item itself.

This is deliberately the same field and the same route JellyRate
(`service.jellyrate`) writes when it asks for a score as the credits roll,
so the two addons agree by construction rather than by luck: a rating made
in either turns up in the other.

Every push reports what actually happened rather than claiming success, and
a failed push never loses the local score - it is stored first, and the sync
state is recorded alongside it so a later retry can find it.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

import xbmc
import xbmcaddon

import library
import main as core

ADDON_ID = "script.jellystat"

# At or above this, a score is a thumbs up to Jellyfin; below it, thumbs
# down. Six out of ten is the usual "I did not regret it" line.
LIKE_THRESHOLD = 6.0

# At or above this, the item is also marked a favourite (opt-in setting).
FAVOURITE_THRESHOLD = 9.0

def log(message, level=xbmc.LOGINFO):
    xbmc.log("[JellyStat] %s" % message, level)


def _connect():
    # library.connect() already creates the rating columns; see
    # library.RATING_COLUMNS for why they are defined over there.
    return library.connect()


# ---------------------------------------------------------------------------
# The queue of things to rate
# ---------------------------------------------------------------------------

# Which titles the rating queue offers. These answer genuinely different
# questions, and the difference is large: a library where TMDb has a score
# for nearly everything has almost nothing "unrated", while none of it has
# been rated by the viewer.
SCOPE_CLAUSES = {
    # Nothing has a score from anywhere - the gaps in the metadata.
    "missing": "rating IS NULL AND user_rating IS NULL",
    # You have not scored it yourself, whatever the public rating says.
    "mine": "user_rating IS NULL",
    # What you have already scored, so it can be reviewed or changed.
    "rated": "user_rating IS NOT NULL",
}
DEFAULT_SCOPE = "mine"

def describe_sync(local, remote, state=None):
    """Where a score is actually saved, named by place.

    Computed from the two stored values rather than from how the score got
    there: `local` is JellyStat's own column, `remote` is what the last sync
    or push saw on Jellyfin. A failed push leaves the two disagreeing, which
    is exactly the case worth showing.
    """
    has_local = local is not None
    has_remote = remote is not None
    if has_local and has_remote:
        text = "Saved in JellyStat and Jellyfin"
        if abs(float(local) - float(remote)) >= 0.001:
            # Both hold a score but not the same one, which the plain
            # sentence would hide.
            text = ("Saved in JellyStat (%g) and Jellyfin (%g)"
                    % (local, remote))
        return text
    if has_local:
        if (state or "").startswith("error:"):
            return "Saved in JellyStat only, Jellyfin refused it"
        return "Saved in JellyStat"
    if has_remote:
        return "Saved in Jellyfin"
    return None


def unrated(media="movies", scope=DEFAULT_SCOPE, limit=100, offset=0):
    """The rating queue, most recently watched first.

    `scope` picks the question being asked - see SCOPE_CLAUSES. Newest
    first, deliberately: what you watched last night is what you can still
    judge, whereas a film from four years ago needs remembering first.
    """
    clause = SCOPE_CLAUSES.get(scope) or SCOPE_CLAUSES[DEFAULT_SCOPE]
    connection = _connect()
    if media == "movies":
        rows = connection.execute(
            "SELECT id, name, year, last_played, play_count, genres, rating, "
            "user_rating, rating_sync, jf_rating "
            "FROM items WHERE media = 'movie' AND %s "
            "ORDER BY last_played DESC LIMIT ? OFFSET ?" % clause,
            (limit, offset)).fetchall()
        total = connection.execute(
            "SELECT COUNT(*) FROM items WHERE media = 'movie' AND %s"
            % clause).fetchone()[0]
        items = [{"id": row[0], "name": row[1], "year": row[2],
                  "last_played": row[3], "plays": row[4],
                  "genres": json.loads(row[5] or "[]"),
                  "community": row[6], "score": row[7],
                  "sync": row[8], "jf_score": row[9],
                  "sync_text": describe_sync(row[7], row[9], row[8]),
                  "media": "movie"}
                 for row in rows]
    else:
        rows = connection.execute(
            "SELECT id, name, series_name, season, episode, last_played, "
            "genres, rating, user_rating, rating_sync, jf_rating "
            "FROM items WHERE media = 'episode' AND %s "
            "ORDER BY last_played DESC LIMIT ? OFFSET ?" % clause,
            (limit, offset)).fetchall()
        total = connection.execute(
            "SELECT COUNT(*) FROM items WHERE media = 'episode' AND %s"
            % clause).fetchone()[0]
        items = [{"id": row[0], "name": row[1], "show": row[2],
                  "season": row[3], "episode": row[4], "last_played": row[5],
                  "genres": json.loads(row[6] or "[]"),
                  "community": row[7], "score": row[8],
                  "sync": row[9], "jf_score": row[10],
                  "sync_text": describe_sync(row[8], row[10], row[9]),
                  "media": "episode"}
                 for row in rows]
    connection.close()
    return {"items": items, "total": total, "media": media, "scope": scope}


def summary():
    """How much of the watched library carries a score, for the page head."""
    connection = _connect()
    row = connection.execute(
        "SELECT "
        "SUM(CASE WHEN media='movie' AND rating IS NULL "
        "         AND user_rating IS NULL THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN media='episode' AND rating IS NULL "
        "         AND user_rating IS NULL THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN media='movie' AND user_rating IS NULL "
        "         THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN media='episode' AND user_rating IS NULL "
        "         THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN user_rating IS NOT NULL THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN rating_sync LIKE 'error%' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN media='movie' AND user_rating IS NOT NULL "
        "         THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN media='episode' AND user_rating IS NOT NULL "
        "         THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN jf_rating IS NOT NULL THEN 1 ELSE 0 END) "
        "FROM items").fetchone()
    connection.close()
    return {
        "missing": {"movies": row[0] or 0, "episodes": row[1] or 0},
        "mine": {"movies": row[2] or 0, "episodes": row[3] or 0},
        "rated": {"movies": row[6] or 0, "episodes": row[7] or 0},
        "rated_by_you": row[4] or 0,
        "on_jellyfin": row[8] or 0,
        "sync_failures": row[5] or 0,
    }


# ---------------------------------------------------------------------------
# Writing back
# ---------------------------------------------------------------------------

def _request(url, token, method, body=None):
    headers = {"X-Emby-Token": token, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
    context = None
    if url.startswith("https") and \
            xbmcaddon.Addon(ADDON_ID).getSetting("ignore_ssl") == "true":
        import ssl
        context = ssl._create_unverified_context()
    with urllib.request.urlopen(request, timeout=30, context=context) as resp:
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw.strip() else None)


def _user_data_url(creds, item_id):
    return "%s/Users/%s/Items/%s/UserData" % (creds["base"],
                                              creds["user_id"], item_id)


def _push_score(creds, item_id, score):
    """Write the real number to Jellyfin's per-user rating.

    The whole UserData object has to go back, so it is read first and only
    the rating fields are changed - otherwise this would wipe the play
    count, the watched flag and the resume position.
    """
    if score is None:
        # Posting Rating=null is silently ignored by Jellyfin - the old value
        # survives and the next mirror sync would adopt it straight back. The
        # dedicated delete route is the only thing that really clears it.
        _request("%s/UserItems/%s/Rating" % (creds["base"], item_id),
                 creds["token"], "DELETE")
        return "rating cleared on Jellyfin"
    status, current = _request(_user_data_url(creds, item_id),
                               creds["token"], "GET")
    dto = dict(current) if isinstance(current, dict) else {}
    dto["Rating"] = float(score)
    # Likes is deliberately left alone: Jellyfin derives the thumb from the
    # number itself, and anything sent here is overwritten by the server.
    dto.pop("Likes", None)
    _request(_user_data_url(creds, item_id), creds["token"], "POST", dto)
    return "sent %g/10 to Jellyfin" % score


def _push_favourite(creds, item_id, wanted):
    method = "POST" if wanted else "DELETE"
    _request("%s/Users/%s/FavoriteItems/%s" % (creds["base"],
                                               creds["user_id"], item_id),
             creds["token"], method)


def _admin_key():
    """The addon's own API key, if one is configured. May be an admin's."""
    addon = xbmcaddon.Addon(ADDON_ID)
    base = addon.getSetting("server_url").strip().rstrip("/")
    key = addon.getSetting("api_key").strip()
    return (base, key) if (base and key) else (None, None)


def _push_community_rating(item_id, score):
    """Write the public score on the item. Administrator keys only.

    Jellyfin's metadata update wants the whole item back, so the item is
    read with the same key first. Anything short of an administrator answers
    403 here, which is reported rather than swallowed - a silent no-op would
    leave the user believing Jellyfin had been updated.
    """
    base, key = _admin_key()
    if not base or not key:
        return None
    status, item = _request("%s/Items/%s" % (base, item_id), key, "GET")
    if not isinstance(item, dict):
        raise core.JellyStatError("Could not read the item back to update it.")
    item["CommunityRating"] = score
    _request("%s/Items/%s" % (base, item_id), key, "POST", item)
    return "community rating written"


def rate(item_id, score, favourite=None, push=True, keep_local=False):
    """Store a score locally and mirror it to Jellyfin as far as it allows.

    Local first, deliberately: the addon's own database is the copy the user
    asked to keep, and a server that is down must not cost them the score
    they just gave. `score` of None clears the rating.

    `keep_local` clears the rating on Jellyfin while leaving JellyStat's
    own untouched. That is the "remove it from Jellyfin so the two cannot
    contradict each other" case: the rating still exists here, Jellyfin
    simply stops holding a different one.
    """
    if score is not None:
        score = max(0.0, min(10.0, float(score)))
    connection = _connect()
    row = connection.execute(
        "SELECT name, media FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        connection.close()
        raise core.JellyStatError("That item is not in the mirror.")
    name = row[0]

    actions = []
    # "not pushed" is a deliberate caller choice (JellyRate has already
    # written the score to Jellyfin), not a failure - the distinction
    # matters to library._adopt_rating.
    state = "not pushed"
    if push:
        try:
            creds = core.get_credentials()
            actions.append(_push_score(creds, item_id, score))
            if score is not None:
                addon = xbmcaddon.Addon(ADDON_ID)
                if addon.getSetting("rating_sets_favourite") == "true":
                    _push_favourite(creds, item_id,
                                    score >= FAVOURITE_THRESHOLD)
                    actions.append("favourite %s"
                                   % ("set" if score >= FAVOURITE_THRESHOLD
                                      else "cleared"))
                community = _push_community_rating(item_id, score)
                if community:
                    actions.append(community)
            state = "synced"
        except (core.JellyStatError, urllib.error.HTTPError,
                urllib.error.URLError, OSError) as err:
            detail = getattr(err, "code", None)
            if detail == 403:
                message = ("Jellyfin refused the write (403). The community "
                           "rating needs an administrator API key in the "
                           "addon settings; your score is saved here.")
            else:
                message = "Jellyfin did not accept it: %s" % err
            state = "error: %s" % message
            log("Rating push failed for %s: %s" % (name, err),
                xbmc.LOGWARNING)

    from datetime import datetime
    with connection:
        if keep_local:
            # Only the server side changes; the local score is the thing
            # being protected from contradiction, not removed.
            connection.execute(
                "UPDATE items SET jf_rating = NULL, rating_sync = ? "
                "WHERE id = ?", (state, item_id))
        else:
            connection.execute(
                "UPDATE items SET user_rating = ?, user_rating_at = ?, "
                "rating_sync = ? WHERE id = ?",
                (score, datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                 state, item_id))
        if state == "synced":
            # The push succeeded, so Jellyfin now holds this exact score.
            connection.execute("UPDATE items SET jf_rating = ? WHERE id = ?",
                               (score, item_id))
        if favourite is not None:
            connection.execute("UPDATE items SET favourite = ? WHERE id = ?",
                               (1 if favourite else 0, item_id))
    connection.close()
    log("Rated %s: %s (%s)" % (name, score, state))
    return {"id": item_id, "name": name, "score": score, "state": state,
            "actions": actions,
            "synced": state == "synced"}


def capabilities():
    """What this login can actually write, so the page can say so up front."""
    base, key = _admin_key()
    result = {"likes": True, "favourite": True, "community_rating": False,
              "note": ""}
    if not base or not key:
        result["note"] = ("Jellyfin stores only a thumbs up or down per "
                          "user, so that is what your score is sent as. To "
                          "have it also update the community rating shown "
                          "in Jellyfin, put an administrator API key in the "
                          "addon's server settings.")
        return result
    try:
        status, users = _request("%s/Users/Me" % base, key, "GET")
        policy = (users or {}).get("Policy") or {}
        result["community_rating"] = bool(policy.get("IsAdministrator"))
    except Exception:
        result["community_rating"] = False
    result["note"] = ("The configured API key is an administrator key, so "
                      "your score is also written to the item's community "
                      "rating in Jellyfin."
                      if result["community_rating"] else
                      "The configured API key is not an administrator key, "
                      "so the community rating in Jellyfin cannot be "
                      "updated; your score is sent as a thumbs up or down.")
    return result
