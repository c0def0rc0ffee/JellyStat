# -*- coding: utf-8 -*-
"""
<summary>
Make Jellyfin's last played dates agree with JellyStat's.
</summary>
<remarks>
JellyStat is the record of when a title was watched: the newest completed,
live sitting in the play log (see library.LastWatched). Jellyfin keeps one
LastPlayedDate per item and overwrites it on every mark, so a bulk re-mark
on the server moves hundreds of titles to one day and nothing on the server
can put them back. Jellyfin for Kodi copies that date into Kodi's video
database, which is where a skin's "last watched" label comes from. So for
the skin to agree with JellyStat, JellyStat has to write the date across.

This module does that, and only that. For every item whose logged date
differs from the date Jellyfin holds, the whole UserData object is read,
its LastPlayedDate replaced, and the object written back: the same route and
the same read first, change one field, write back shape ratings.py uses,
so the play count, the watched flag and the resume position survive. Each
write is read back and compared, so a server that quietly kept its own
date is counted as a refusal rather than reported as a success.

It runs in the background from the service on every check while the
`push_dates` setting is on, and straight after a Trakt or file import and
after a bulk day is set aside or restored, so the user never has to press
anything. Without JellyStat installed none of this exists and Jellyfin's
dates stay exactly as they are. A date the server refused is remembered for
the rest of the session and not retried every five minutes; the Tools page
shows how many were refused and a manual run tries them again.

The only writes to Jellyfin are the UserData objects. The mirror's own
last_played column is updated to the pushed value so the next sync does not
read the change back as a play on another device.
</remarks>
"""

import threading
import time
import urllib.error
from datetime import datetime, timezone

import xbmc
import xbmcaddon

import library
import main as core
import playlog
import ratings
import timeparse

ADDON_ID = "script.jellystat"
SETTING = "push_dates"

# Two stamps this close are the same viewing: Jellyfin stores sub-second
# precision the log never had, and a conversion through the timezone
# database can shift a second either way.
TOLERANCE_S = 60

_lock = threading.Lock()
_job = {"state": "idle", "done": 0, "total": 0, "failed": 0,
        "refused": 0, "started_at": None, "message": None}
_worker = None
# item id -> the UTC stamp Jellyfin refused to take, so the service does not
# push the same value again on its next check. A manual run clears it.
_refused = {}


def log(message, level=xbmc.LOGINFO):
    """
    <summary>
    Write a line to Kodi's log under the add-on's prefix.
    </summary>
    <param name="message">The text.</param>
    <param name="level">An xbmc log level.</param>
    """
    xbmc.log("[%s] dates: %s" % (ADDON_ID, message), level)


def enabled():
    """
    <summary>
    Whether the automatic write back is on. Defaults to on.
    </summary>
    <returns>True unless the setting has been turned off.</returns>
    """
    return xbmcaddon.Addon(ADDON_ID).getSetting(SETTING) != "false"


# ---------------------------------------------------------------------------
# Deciding what to write
# ---------------------------------------------------------------------------

def to_utc_text(local_text):
    """
    <summary>
    A play log stamp (local wall time) as the UTC text Jellyfin stores.
    </summary>
    <param name="local_text">A YYYY-MM-DDTHH:MM:SS local stamp.</param>
    <returns>A YYYY-MM-DDTHH:MM:SS UTC stamp, or None if unparseable.</returns>
    <remarks>
    The inverse of playlog.to_local, through the same timezone database, so
    a July sitting goes back under July's offset and a round trip lands on
    the second it started from.
    </remarks>
    """
    when = playlog.parse_stamp(local_text)
    if when is None:
        return None
    moment = datetime.fromtimestamp(time.mktime(when.timetuple()),
                                    tz=timezone.utc)
    return moment.strftime(playlog.STAMP)


def _seconds_apart(one, other):
    """
    <summary>
    The gap between two UTC stamps in seconds, or None when either is missing.
    </summary>
    """
    if not one or not other:
        return None
    try:
        first = timeparse.strptime(one[:19], playlog.STAMP)
        second = timeparse.strptime(other[:19], playlog.STAMP)
    except ValueError:
        return None
    return abs((first - second).total_seconds())


def targets(connection=None, include_refused=False):
    """
    <summary>
    The items whose logged date differs from the date Jellyfin holds.
    </summary>
    <param name="connection">An open mirror connection to reuse, or None.</param>
    <param name="include_refused">True to list items the server refused
    earlier this session as well; the service leaves them out.</param>
    <returns>A list of dicts: id, name, media, show, wanted (UTC stamp the
    log says), have (the UTC stamp the mirror holds, or None).</returns>
    <remarks>
    Only present items are considered: a title Jellyfin no longer has
    cannot take a date. Only titles with a logged sitting are considered:
    a title the log knows nothing about already shows Jellyfin's date
    everywhere, so there is nothing to write.
    </remarks>
    """
    own = connection is None
    if own:
        playlog.connect().close()
        connection = library.connect()
    try:
        watched = library.LastWatched(connection)
        rows = connection.execute(
            "SELECT id, name, media, series_name, year, last_played "
            "FROM items WHERE media IN ('movie', 'episode') AND present = 1"
            ).fetchall()
    finally:
        if own:
            connection.close()
    found = []
    for item_id, name, media, show, year, have in rows:
        if media == "episode":
            logged = watched.logged(show, name)
        else:
            logged = watched.logged(None, name, year)
            show = None
        if not logged:
            continue
        wanted = to_utc_text(logged)
        if not wanted:
            continue
        have = (have or "")[:19] or None
        gap = _seconds_apart(wanted, have)
        if gap is not None and gap <= TOLERANCE_S:
            continue
        if not include_refused and _refused.get(item_id) == wanted:
            continue
        found.append({"id": item_id, "name": name, "media": media,
                      "show": show, "wanted": wanted, "have": have})
    return found


def survey():
    """
    <summary>
    How the two sides compare, for the Tools page.
    </summary>
    <returns>A dict: differing, refused, examples, enabled and job.</returns>
    <remarks>
    The job state is read before the counts, for the reason reconcile.survey
    gives: "finished" must mean the counts were taken after the last write.
    </remarks>
    """
    job = progress()
    pending = targets(include_refused=True)
    refused = sum(1 for row in pending if _refused.get(row["id"]) == row["wanted"])
    examples = []
    for row in pending[:4]:
        examples.append({
            "name": (row["show"] + ": " + row["name"]) if row["show"]
            else row["name"],
            "here": library.local_stamp(row["wanted"]),
            "jellyfin": library.local_stamp(row["have"]),
        })
    return {"differing": len(pending), "refused": refused,
            "examples": examples, "enabled": enabled(), "job": job}


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def push_one(creds, item_id, wanted):
    """
    <summary>
    Write one item's LastPlayedDate to Jellyfin and confirm it took.
    </summary>
    <param name="creds">A credentials dict from main.get_credentials.</param>
    <param name="item_id">The Jellyfin item.</param>
    <param name="wanted">The UTC YYYY-MM-DDTHH:MM:SS stamp to store.</param>
    <returns>True when Jellyfin reads the new date back, False when it
    kept its own.</returns>
    <exception cref="urllib.error.URLError">When the server cannot be
    reached or refuses the request outright.</exception>
    <remarks>
    The whole UserData object goes back with one field changed, as
    ratings._push_score does; Likes is dropped for the reason given there.
    </remarks>
    """
    url = ratings._user_data_url(creds, item_id)
    _, current = ratings._request(url, creds["token"], "GET")
    dto = dict(current) if isinstance(current, dict) else {}
    dto["LastPlayedDate"] = wanted + ".0000000Z"
    dto.pop("Likes", None)
    ratings._request(url, creds["token"], "POST", dto)
    _, after = ratings._request(url, creds["token"], "GET")
    stored = ((after or {}).get("LastPlayedDate") or "")[:19]
    gap = _seconds_apart(stored, wanted)
    return gap is not None and gap <= TOLERANCE_S


def progress():
    """
    <summary>
    A copy of the current job state.
    </summary>
    <returns>A dict.</returns>
    """
    with _lock:
        return dict(_job)


def start(manual=False):
    """
    <summary>
    Begin writing every differing date in the background.
    </summary>
    <param name="manual">True from the Tools page button: dates the server
    refused earlier are tried again. False from the service and the
    import hooks, which leave them alone.</param>
    <returns>The job state.</returns>
    <remarks>
    The running state is claimed before the targets are read, so a
    double click or the service beating during a manual run cannot start
    two workers over the same rows.
    </remarks>
    """
    global _worker
    with _lock:
        if _job["state"] == "running":
            return dict(_job)
        _job.update(state="running", done=0, total=0, failed=0, refused=0,
                    message=None,
                    started_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    try:
        if manual:
            _refused.clear()
        rows = targets()
    except Exception:
        with _lock:
            _job.update(state="idle", message="Could not read the dates.")
        raise
    if not rows:
        with _lock:
            _job.update(state="finished",
                        message="Jellyfin already holds every date")
        return progress()
    with _lock:
        _job.update(total=len(rows))
    _worker = threading.Thread(target=_run, args=(rows,),
                               name="JellyStatDates")
    _worker.daemon = True
    _worker.start()
    return progress()


def _run(rows):
    """
    <summary>
    Thread body: push each row, record the outcome, update the mirror.
    </summary>
    <param name="rows">The list targets() returned.</param>
    <remarks>
    A server that cannot be reached at all ends the run after the first
    failure rather than timing out once per title: with a thousand rows
    that is eight hours of doing nothing. Everything not reached stays a
    target for the next check.
    </remarks>
    """
    failed = refused = done = 0
    try:
        creds = core.get_credentials()
    except core.JellyStatError as err:
        with _lock:
            _job.update(state="finished", message="No Jellyfin login: %s" % err)
        log("No login, nothing written: %s" % err, xbmc.LOGWARNING)
        return
    unreachable = False
    for index, row in enumerate(rows, 1):
        try:
            if push_one(creds, row["id"], row["wanted"]):
                connection = library.connect()
                with connection:
                    connection.execute(
                        "UPDATE items SET last_played = ? WHERE id = ?",
                        (row["wanted"], row["id"]))
                connection.close()
                _refused.pop(row["id"], None)
                done += 1
            else:
                refused += 1
                _refused[row["id"]] = row["wanted"]
                if refused <= 3:
                    log("Jellyfin kept its own date for %s" % row["name"],
                        xbmc.LOGWARNING)
        except urllib.error.HTTPError as err:
            failed += 1
            _refused[row["id"]] = row["wanted"]
            if failed <= 3:
                log("Jellyfin answered %s for %s" % (err.code, row["name"]),
                    xbmc.LOGWARNING)
        except (urllib.error.URLError, OSError) as err:
            failed += 1
            unreachable = True
            log("Jellyfin unreachable, stopping: %s" % err, xbmc.LOGWARNING)
        except Exception as err:
            failed += 1
            if failed <= 3:
                log("Could not write %s: %s" % (row["name"], err),
                    xbmc.LOGWARNING)
        with _lock:
            _job.update(done=index, failed=failed, refused=refused)
        if unreachable:
            break
    parts = ["Wrote %d of %d dates to Jellyfin" % (done, len(rows))]
    if refused:
        parts.append("%d kept by the server" % refused)
    if failed:
        parts.append("%d failed" % failed)
    with _lock:
        _job.update(state="finished", message=", ".join(parts))
    log(_job["message"])


def nudge():
    """
    <summary>
    Start a run if the setting is on and nothing is running; never raises.
    </summary>
    <returns>True when a run was started.</returns>
    <remarks>
    The one call the service and the import hooks make. Reading the
    targets costs one grouped query and one scan of the items table,
    which is cheap enough for a five minute beat on a Pi.
    </remarks>
    """
    try:
        if not enabled():
            return False
        if progress()["state"] == "running":
            return False
        if not targets():
            return False
        start()
        return True
    except Exception as err:
        log("Could not start the date write back: %s" % err, xbmc.LOGWARNING)
        return False
