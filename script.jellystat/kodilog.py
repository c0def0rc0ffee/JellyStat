# -*- coding: utf-8 -*-
"""
<summary>
Kodi's own log, read for the dashboard's Settings page: the newest part of
kodi.log or kodi.old.log, optionally narrowed to one add-on or a search.
</summary>
<remarks>
JellyStat runs inside the Kodi whose log this is, so it reads the file
straight from special://logpath rather than asking another add-on. Only the
two fixed file names in SOURCES are ever opened; nothing in a request names a
path.

The log carries server addresses, user ids and sometimes stream tokens, so
webserver.py only serves any of this when a dashboard password is set.
</remarks>
"""

import os
import re

import xbmcvfs

# (id, file name, label). The id is what a request may name; the file name
# never comes from a request.
SOURCES = (
    ("current", "kodi.log", "This run"),
    ("previous", "kodi.old.log", "Last run"),
)

# (id, label, substrings). An entry belongs to a component when any of its
# substrings appears in it. "all" and "problems" are handled specially.
COMPONENTS = (
    ("all", "Everything", ()),
    ("problems", "Errors and warnings", ()),
    ("playback", "Playback", ("VideoPlayer::OpenFile", "CVideoPlayer::CloseFile",
                              "CPlayListPlayer", "skip pill", "sleep timer",
                              "OnPlayBack")),
    ("functional", "Functional", ("[functional/",)),
    ("engage", "Engage", ("Engage: ", "Engage API: ", "Engage lists: ",
                          "Engage manager: ", "Engage prompt: ",
                          "Engage slots: ", "Engage slots window: ")),
    ("jellyrate", "JellyRate", ("[service.jellyrate",)),
    ("dualplay", "JellyDualPlay", ("[service.jellyfin.dualplay]",)),
    ("jellystat", "JellyStat", ("[JellyStat]", "[script.jellystat]")),
    ("jellyfin", "Jellyfin", ("JELLYFIN.",)),
)

# How much of the end of the file one view reads. kodi.log with debug
# logging on runs to tens of megabytes; the view is for the recent past.
TAIL_BYTES = 4 * 1024 * 1024

DEFAULT_LIMIT = 400
MAX_LIMIT = 2000
MAX_SEARCH = 100

# One very long line (Kodi prints every GL extension on one) is cut here so a
# single entry cannot swamp the page.
MAX_LINE = 4000

# A new entry starts with Kodi's timestamp; anything else (a traceback, a
# wrapped message) continues the one before it.
_ENTRY_START = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+ T:\d+\s+(\w+) <")

_PROBLEM_LEVELS = ("warning", "error", "fatal")


def log_dir():
    """
    <summary>
    The folder Kodi writes its log to on this box.
    </summary>
    <returns>A filesystem path.</returns>
    """
    return xbmcvfs.translatePath("special://logpath")


def _path_of(source_id):
    """
    <summary>
    The file behind one of the fixed sources.
    </summary>
    <param name="source_id">An id from SOURCES.</param>
    <returns>(file name, full path), or (None, None) for an unknown id.</returns>
    """
    for sid, name, _label in SOURCES:
        if sid == source_id:
            return name, os.path.join(log_dir(), name)
    return None, None


def listing():
    """
    <summary>
    The log files and the ways a view can be narrowed, for the Settings panel.
    </summary>
    <returns>{"sources": [{id, name, label, exists, bytes, modified}],
    "components": [{id, label}]}.</returns>
    """
    sources = []
    for sid, name, label in SOURCES:
        path = os.path.join(log_dir(), name)
        try:
            stat = os.stat(path)
            sources.append({"id": sid, "name": name, "label": label,
                            "exists": True, "bytes": stat.st_size,
                            "modified": int(stat.st_mtime)})
        except OSError:
            sources.append({"id": sid, "name": name, "label": label,
                            "exists": False, "bytes": 0, "modified": None})
    return {"sources": sources,
            "components": [{"id": cid, "label": label}
                           for cid, label, _subs in COMPONENTS]}


def open_source(source_id):
    """
    <summary>
    Open one of the fixed log files for a download.
    </summary>
    <param name="source_id">An id from SOURCES.</param>
    <returns>(file name, open binary handle, size in bytes), or None when the
    id is unknown or the file is missing.</returns>
    <remarks>
    The size is taken once at open: Kodi keeps writing while the download
    runs, and the caller sends exactly that many bytes so the announced
    Content-Length holds.
    </remarks>
    """
    name, path = _path_of(source_id)
    if not name:
        return None
    try:
        handle = open(path, "rb")
    except OSError:
        return None
    try:
        size = os.fstat(handle.fileno()).st_size
    except OSError:
        handle.close()
        return None
    return name, handle, size


def split_entries(text, starts_mid_file):
    """
    <summary>
    Group log lines into entries: a timestamped line and whatever follows it
    until the next timestamp.
    </summary>
    <param name="text">Decoded log text.</param>
    <param name="starts_mid_file">True when text begins part way through the
    file, so lines before the first timestamp belong to an entry that was cut
    off and are dropped.</param>
    <returns>A list of (level, text) tuples, oldest first; level is the
    lowercased Kodi level word, or "" when the line had none.</returns>
    """
    entries = []
    current = None
    for line in text.splitlines():
        if len(line) > MAX_LINE:
            line = line[:MAX_LINE] + " [cut]"
        match = _ENTRY_START.match(line)
        if match:
            if current:
                entries.append((current[0], "\n".join(current[1])))
            current = (match.group(1).lower(), [line])
        elif current:
            current[1].append(line)
        elif not starts_mid_file and line:
            current = ("", [line])
    if current:
        entries.append((current[0], "\n".join(current[1])))
    return entries


def _matches(entry, component, search):
    """
    <summary>
    Whether one entry belongs in the view.
    </summary>
    <param name="entry">A (level, text) tuple.</param>
    <param name="component">A row of COMPONENTS.</param>
    <param name="search">Lowercased search text, or "".</param>
    <returns>True or False.</returns>
    """
    level, text = entry
    cid, _label, subs = component
    if cid == "problems":
        if level not in _PROBLEM_LEVELS:
            return False
    elif subs and not any(sub in text for sub in subs):
        return False
    return not search or search in text.lower()


def tail(source_id, component_id="all", search="", limit=DEFAULT_LIMIT):
    """
    <summary>
    The newest entries of one log, narrowed by component and search text.
    </summary>
    <param name="source_id">An id from SOURCES.</param>
    <param name="component_id">An id from COMPONENTS; unknown means all.</param>
    <param name="search">Case insensitive text an entry must contain; cut to
    MAX_SEARCH characters.</param>
    <param name="limit">Most entries to return, clamped to 1 to MAX_LIMIT.</param>
    <returns>{"source", "name", "exists", "bytes", "modified", "partial",
    "matched", "entries"}: partial is True when only the last TAIL_BYTES were
    read, matched counts every matching entry in what was read, and entries
    holds the newest of them, oldest first.</returns>
    <exception cref="ValueError">The source id is not one of SOURCES.</exception>
    """
    name, path = _path_of(source_id)
    if not name:
        raise ValueError("Unknown log.")
    component = next((row for row in COMPONENTS if row[0] == component_id),
                     COMPONENTS[0])
    search = (search or "")[:MAX_SEARCH].strip().lower()
    limit = max(1, min(int(limit), MAX_LIMIT))
    result = {"source": source_id, "name": name, "exists": False, "bytes": 0,
              "modified": None, "partial": False, "matched": 0, "entries": []}
    try:
        with open(path, "rb") as handle:
            stat = os.fstat(handle.fileno())
            size = stat.st_size
            start = max(0, size - TAIL_BYTES)
            handle.seek(start)
            blob = handle.read(size - start)
    except OSError:
        return result
    result.update({"exists": True, "bytes": size,
                   "modified": int(stat.st_mtime), "partial": start > 0})
    entries = split_entries(blob.decode("utf-8", "replace"), start > 0)
    picked = [entry[1] for entry in entries
              if _matches(entry, component, search)]
    result["matched"] = len(picked)
    result["entries"] = picked[-limit:]
    return result
