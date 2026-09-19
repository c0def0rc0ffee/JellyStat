# -*- coding: utf-8 -*-
"""
<summary>
Automatic snapshots of the database, taken around anything destructive.
</summary>
<remarks>
An import can rewrite thousands of rows, and the interesting failures are
the ones nobody notices for a week: a rating policy that went the wrong
way, a timezone that shifted ten thousand timestamps by an hour. Undo needs
to still be possible then, not just in the next minute, so a copy is taken
immediately before an import and immediately after it.

The pair matters. The "before" is what you restore to. The "after" records
what the import actually produced, so a later problem can be traced to the
import that caused it rather than guessed at.

Copies are made with SQLite's own backup API, so one taken while the
service is mid-write is still a consistent database rather than a torn
file. They live beside the database and the oldest are dropped once there
are more than KEEP.
</remarks>
"""

import os
import re
import sqlite3
import time
from datetime import datetime

import xbmc

import history

FOLDER = "backups"
KEEP = 12
# "manual" belongs here as much as before/after: a snapshot this module
# wrote must be one it can also list and hand back, or the Back up now
# button produces a file nobody can reach.
NAME_RE = re.compile(
    r"^jellystat-(\d{8}-\d{6})-(before|after|manual)-([a-z0-9-]+)\.db$")


def log(message, level=xbmc.LOGINFO):
    """
    <summary>
    Write one line to Kodi's log under the JellyStat tag.
    </summary>
    <param name="message">Text to log.</param>
    <param name="level">Kodi log level, info by default.</param>
    """
    xbmc.log("[JellyStat] %s" % message, level)


def folder():
    """
    <summary>
    The folder automatic snapshots are kept in, beside the history database, created if missing.
    </summary>
    <returns>The folder path.</returns>
    <remarks>
    A folder that cannot be created is returned anyway; the write that follows reports the real error.
    </remarks>
    """
    path = os.path.join(os.path.dirname(history.db_path()), FOLDER)
    if not os.path.isdir(path):
        try:
            os.makedirs(path)
        except OSError:
            pass
    return path


def _slug(reason):
    """
    <summary>
    Turn a snapshot reason into a filename safe token.
    </summary>
    <param name="reason">Free text; empty means manual.</param>
    <returns>Lowercase letters and digits joined by single hyphens.</returns>
    """
    return re.sub(r"[^a-z0-9]+", "-", (reason or "manual").lower()).strip("-")


def create(when="before", reason="import", now=None):
    """
    <summary>
    Take one snapshot. Returns its details, or None if it could not.
    </summary>
    <remarks>
    Never raises: failing to back up is worth reporting loudly, but it is
    the caller's decision whether that should stop the operation, and for a
    restore-from-file it obviously must not.

    Written under a name nothing here will match, and moved into place only
    once it is complete. Connecting straight to the final name would create
    that file the moment the copy began, so a failure part-way, a full
    disk, a source locked past the timeout, the power going, left a
    truncated database sitting there under a perfectly good name: listed as
    a backup, offered for download, and counted against KEEP, where it
    could evict the last copy that actually worked. A decoy restore point
    is worse than no restore point, because the mistake it is supposed to
    undo has usually already been made by the time anyone opens it.
    </remarks>
    """
    now = now or datetime.now()
    name = "jellystat-%s-%s-%s.db" % (now.strftime("%Y%m%d-%H%M%S"),
                                      when, _slug(reason))
    target = os.path.join(folder(), name)
    # NAME_RE requires the name to end at ".db", so a leftover ".partial"
    # is invisible to listing(), prune() and path_of() alike.
    partial = target + ".partial"
    source = copy = None
    done = False
    try:
        source = sqlite3.connect(history.db_path(), timeout=30)
        copy = sqlite3.connect(partial)
        with copy:
            source.backup(copy)
        done = True
    except Exception as err:
        log("Could not take the %s backup: %s" % (when, err), xbmc.LOGERROR)
    finally:
        # Closed before the file is moved or removed: an open handle stops
        # either on Windows, and would leak on every failure elsewhere.
        for handle in (copy, source):
            try:
                if handle is not None:
                    handle.close()
            except Exception:
                pass
    if done:
        try:
            os.replace(partial, target)
        except OSError as err:
            log("Could not put the %s backup in place: %s" % (when, err),
                xbmc.LOGERROR)
            done = False
    if not done:
        try:
            os.unlink(partial)
        except OSError:
            pass
        return None
    # Guarded: this module promises never to raise, and pruning reads names
    # out of a directory anything at all could have dropped a file into.
    try:
        prune()
    except Exception as err:
        log("Could not prune old backups: %s" % err, xbmc.LOGWARNING)
    try:
        size = os.path.getsize(target)
    except OSError:
        size = 0
    log("Backup %s %s: %s (%.1f MB)"
        % (when, reason, name, size / 1048576.0))
    return {"name": name, "when": when, "reason": reason,
            "taken_at": now.strftime("%Y-%m-%dT%H:%M:%S"), "bytes": size}


def listing():
    """
    <summary>
    Every snapshot held, newest first.
    </summary>
    """
    out = []
    try:
        names = os.listdir(folder())
    except OSError:
        return out
    for name in names:
        match = NAME_RE.match(name)
        if not match:
            continue
        stamp, when, reason = match.groups()
        path = os.path.join(folder(), name)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        # The pattern proves the digits are digits, not that they are a
        # date: "20261399-256199" matches it and is not a moment in time.
        # One such name used to raise out of here, through prune(), into
        # create() and on into every import: a single stray file could
        # stop the backups page and block importing entirely.
        try:
            taken_at = datetime.strptime(stamp, "%Y%m%d-%H%M%S")
        except ValueError:
            log("Ignoring a backup with an impossible date: %s" % name,
                xbmc.LOGWARNING)
            continue
        out.append({
            "name": name,
            "when": when,
            "reason": reason,
            "taken_at": taken_at.strftime("%Y-%m-%dT%H:%M:%S"),
            "bytes": size,
        })
    out.sort(key=lambda row: row["taken_at"], reverse=True)
    return out


def prune(keep=KEEP):
    """
    <summary>
    Drop the oldest snapshots beyond `keep`.
    </summary>
    <remarks>
    Pairs are not kept together deliberately: the newest N files are what
    matter, and an orphaned "after" is still evidence of what an import
    produced.
    </remarks>
    """
    rows = listing()
    for row in rows[keep:]:
        try:
            os.unlink(os.path.join(folder(), row["name"]))
            log("Pruned old backup %s" % row["name"], xbmc.LOGDEBUG)
        except OSError:
            pass


def path_of(name):
    """
    <summary>
    The full path of a named snapshot, refusing anything else.
    </summary>
    <remarks>
    The name comes from an HTTP request, so it is checked against the
    pattern this module writes rather than trusted; no path separators can
    survive that.
    </remarks>
    """
    if not NAME_RE.match(name or ""):
        return None
    candidate = os.path.join(folder(), name)
    return candidate if os.path.isfile(candidate) else None


def around(reason):
    """
    <summary>
    Context manager taking a snapshot either side of an operation.
    </summary>
    <remarks>
    with backup.around("trakt-import") as pair:
        ...
    pair["before"], pair["after"]
    </remarks>
    """
    return _Around(reason)


class _Around(object):
    """
    <summary>
    Context manager body for around(): a snapshot before and after an operation.
    </summary>
    """
    def __init__(self, reason):
        """
        <summary>
        Remember the reason; nothing is taken yet.
        </summary>
        <param name="reason">Why the snapshots are being taken; ends up in their names.</param>
        """
        self.reason = reason
        self.pair = {"before": None, "after": None}

    def __enter__(self):
        """
        <summary>
        Take the before snapshot.
        </summary>
        <returns>The pair dict, filled in as the two snapshots are taken.</returns>
        """
        self.pair["before"] = create("before", self.reason)
        return self.pair

    def __exit__(self, exc_type, exc, tb):
        # Taken even when the operation failed: a half-finished import is
        # exactly the state worth being able to inspect afterwards.
        """
        <summary>
        Take the after snapshot, even when the operation failed.
        </summary>
        <returns>False, so any exception propagates.</returns>
        <remarks>
        A half finished import is exactly the state worth being able to inspect afterwards.
        </remarks>
        """
        self.pair["after"] = create("after", self.reason)
        return False
