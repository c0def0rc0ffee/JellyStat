# -*- coding: utf-8 -*-
"""Make Jellyfin's ratings agree with JellyStat's, or say nothing at all.

JellyStat is the record of what you think of a title. Jellyfin holds its own
per-user rating, and the two drift: a Trakt import writes thousands of
scores here that were never sent there, and JellyRate writes scores there
that arrived here later.

Drift itself is harmless. **Contradiction** is not: the same film rated 8
in one place and 6 in the other is a question nobody can answer from the
data. So the goal is not that both sides always hold everything, but that
they never disagree - Jellyfin may be missing a rating, it may not hold a
different one.

Two ways to get there, both offered rather than assumed:

- **push** writes JellyStat's score to Jellyfin, so the two match;
- **clear** removes Jellyfin's score, leaving the rating only here.

Both run in the background against thousands of items, reporting progress,
because a rating is one HTTP request and three thousand of them is not
something to do inside a page load.
"""

import threading
import time

import xbmc

import library
import ratings

_lock = threading.Lock()
_job = {"state": "idle", "action": None, "done": 0, "total": 0,
        "failed": 0, "started_at": None, "message": None}
_worker = None


def log(message, level=xbmc.LOGINFO):
    xbmc.log("[JellyStat] %s" % message, level)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def survey():
    """How the two sides currently compare."""
    connection = library.connect()
    try:
        row = connection.execute(
            "SELECT "
            "SUM(CASE WHEN user_rating IS NOT NULL THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN user_rating IS NOT NULL AND jf_rating IS NULL "
            "         THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN user_rating IS NOT NULL AND jf_rating IS NOT NULL "
            "         AND ABS(user_rating - jf_rating) >= 0.001 "
            "         THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN user_rating IS NOT NULL AND jf_rating IS NOT NULL "
            "         AND ABS(user_rating - jf_rating) < 0.001 "
            "         THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN user_rating IS NULL AND jf_rating IS NOT NULL "
            "         THEN 1 ELSE 0 END) "
            "FROM items").fetchone()
        examples = [
            {"name": name, "here": here, "jellyfin": there}
            for name, here, there in connection.execute(
                "SELECT name, user_rating, jf_rating FROM items "
                "WHERE user_rating IS NOT NULL AND jf_rating IS NOT NULL "
                "AND ABS(user_rating - jf_rating) >= 0.001 "
                "ORDER BY ABS(user_rating - jf_rating) DESC LIMIT 8")]
    finally:
        connection.close()
    return {
        "rated_here": row[0] or 0,
        "missing_on_jellyfin": row[1] or 0,
        "contradicting": row[2] or 0,
        "agreeing": row[3] or 0,
        "only_on_jellyfin": row[4] or 0,
        "examples": examples,
        "job": progress(),
    }


def _targets(connection, scope):
    """The items an action applies to.

    "contradicting" is the narrow fix: only where the two hold different
    scores. "all" also sends the ones Jellyfin is simply missing, which is
    what makes the two copies identical rather than merely consistent.
    """
    if scope == "contradicting":
        where = ("user_rating IS NOT NULL AND jf_rating IS NOT NULL "
                 "AND ABS(user_rating - jf_rating) >= 0.001")
    else:
        where = ("user_rating IS NOT NULL AND (jf_rating IS NULL "
                 "OR ABS(user_rating - jf_rating) >= 0.001)")
    return connection.execute(
        "SELECT id, name, user_rating FROM items WHERE %s" % where).fetchall()


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def progress():
    with _lock:
        return dict(_job)


def start(action="push", scope="all"):
    """Begin a push or clear in the background. Returns the job state."""
    global _worker
    if action not in ("push", "clear"):
        raise ValueError("Action must be push or clear.")
    with _lock:
        if _job["state"] == "running":
            return dict(_job)
    connection = library.connect()
    try:
        targets = _targets(connection, scope)
    finally:
        connection.close()
    with _lock:
        _job.update(state="running", action=action, done=0,
                    total=len(targets), failed=0, message=None,
                    started_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    _worker = threading.Thread(target=_run, args=(action, targets),
                               name="JellyStatReconcile")
    _worker.daemon = True
    _worker.start()
    return progress()


def _run(action, targets):
    failed = 0
    for index, (item_id, name, score) in enumerate(targets, 1):
        try:
            # ratings.rate() is the one place that knows how to write a
            # score to Jellyfin and record what happened, so reconciling
            # goes through it rather than reimplementing the call.
            ratings.rate(item_id, None if action == "clear" else score,
                         push=True, keep_local=action == "clear")
        except Exception as err:
            failed += 1
            if failed <= 3:
                log("Reconcile could not %s %s: %s" % (action, name, err),
                    xbmc.LOGWARNING)
        with _lock:
            _job["done"] = index
            _job["failed"] = failed
    with _lock:
        _job.update(state="finished",
                    message="%s %d of %d%s"
                            % ("Pushed" if action == "push" else "Cleared",
                               _job["done"] - failed, _job["total"],
                               ", %d failed" % failed if failed else ""))
    log("Reconcile finished: %s" % _job["message"])
