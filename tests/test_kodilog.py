#!/usr/bin/env python3
"""
<summary>
Tests for the Kodi log on the Settings page: kodilog's entry grouping,
filters and tail, and the /api/logs routes with their password gate.
</summary>
<remarks>
special://logpath is pointed at a temporary folder holding a made up
kodi.log, and webserver.Handler is served on a loopback port with the Kodi
modules stubbed by kodi_stubs. Every check records a failure rather than
stopping, and the process exits non zero if any failed.

Usage:
    python3 tests/test_kodilog.py
</remarks>
"""

import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kodi_stubs  # noqa: E402

kodi_stubs.install()

import kodilog  # noqa: E402
import webserver  # noqa: E402

FAILURES = []
PORT = None
LOGDIR = tempfile.mkdtemp(prefix="jellystat-logs-")
kodilog.log_dir = lambda: LOGDIR

SAMPLE = (
    "2026-11-16 21:00:00.100 T:1    info <general>: VideoPlayer::OpenFile: plugin://one\n"
    "2026-11-16 21:40:10.200 T:2    info <general>: [functional/helper] skip pill: up (credits)\n"
    "2026-11-16 21:40:11.300 T:2    info <general>: [functional/helper] skip pill: down (skip)\n"
    "2026-11-16 21:40:12.400 T:3   error <general>: Engage: something broke\n"
    "Traceback (most recent call last):\n"
    "  File \"x.py\", line 1\n"
    "2026-11-16 21:40:13.500 T:1    info <general>: VideoPlayer::OpenFile: plugin://two\n"
    "2026-11-16 21:40:14.600 T:4  warning <general>: JELLYFIN.client -> slow reply\n"
)


def check(condition, message):
    """<summary>Record a failure without stopping.</summary>"""
    if not condition:
        FAILURES.append(message)
        print("FAIL: %s" % message)


def equal(actual, expected, what):
    """<summary>check() for equality, printing both sides on failure.</summary>"""
    check(actual == expected, "%s: expected %r, got %r" % (what, expected, actual))


def write_log(name, text):
    """<summary>Write one file into the fake log folder.</summary>"""
    with open(os.path.join(LOGDIR, name), "w", encoding="utf-8") as handle:
        handle.write(text)


def call(path, headers=None):
    """
    <summary>One GET against the server under test.</summary>
    <param name="path">Path and query.</param>
    <param name="headers">Extra headers, or None.</param>
    <returns>(status, response, body bytes).</returns>
    """
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    conn.request("GET", path, headers=dict({"Host": "127.0.0.1:%d" % PORT},
                                           **(headers or {})))
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, response, data


# ---- kodilog itself ----------------------------------------------------------

def test_entries():
    """<summary>Continuation lines join their entry; a cut off start is dropped.</summary>"""
    print("running test_entries")
    entries = kodilog.split_entries(SAMPLE, False)
    equal(len(entries), 6, "six timestamped entries")
    check("Traceback" in entries[3][1] and "x.py" in entries[3][1],
          "the traceback stays with its error")
    equal(entries[3][0], "error", "the level is read")
    cut = kodilog.split_entries("tail of an old line\n" + SAMPLE, True)
    equal(len(cut), 6, "a partial first line is dropped mid file")
    whole = kodilog.split_entries("header line\n" + SAMPLE, False)
    equal(len(whole), 7, "a first line without a stamp is kept at the file start")
    long_line = kodilog.split_entries("2026-11-16 21:00:00.100 T:1    info <general>: "
                                      + "x" * 9000, False)
    check(len(long_line[0][1]) < kodilog.MAX_LINE + 20, "a very long line is cut")


def test_tail_filters():
    """<summary>Components, problems, search and the limit.</summary>"""
    print("running test_tail_filters")
    write_log("kodi.log", SAMPLE)
    every = kodilog.tail("current")
    equal(every["matched"], 6, "everything")
    check(every["exists"] and not every["partial"], "whole small file read")
    equal(kodilog.tail("current", "functional")["matched"], 2, "Functional only")
    equal(kodilog.tail("current", "engage")["matched"], 1, "Engage only")
    equal(kodilog.tail("current", "jellyfin")["matched"], 1, "Jellyfin only")
    play = kodilog.tail("current", "playback")
    equal(play["matched"], 4, "playback: two opens and two skip pill lines")
    problems = kodilog.tail("current", "problems")
    equal(problems["matched"], 2, "errors and warnings")
    equal(kodilog.tail("current", "all", "SKIP PILL")["matched"], 2,
          "search ignores case")
    equal(kodilog.tail("current", "all", "most recent call")["matched"], 1,
          "search reaches continuation lines")
    limited = kodilog.tail("current", "all", "", 2)
    equal(len(limited["entries"]), 2, "the limit holds")
    check("plugin://two" in limited["entries"][0], "the newest entries are kept")
    equal(kodilog.tail("current", "nonsense")["matched"], 6,
          "an unknown component means everything")
    missing = kodilog.tail("previous")
    check(not missing["exists"] and missing["entries"] == [], "a missing file is reported")
    try:
        kodilog.tail("../../etc/passwd")
        check(False, "an unknown source raises")
    except ValueError:
        pass


def test_tail_partial():
    """<summary>A file bigger than TAIL_BYTES is read from its end only.</summary>"""
    print("running test_tail_partial")
    saved = kodilog.TAIL_BYTES
    kodilog.TAIL_BYTES = len(SAMPLE) // 2
    try:
        result = kodilog.tail("current")
        check(result["partial"], "partial is reported")
        check(0 < result["matched"] < 6, "only the end was read")
        check(all(e.startswith("2026-") for e in result["entries"]),
              "the cut off first entry was dropped")
    finally:
        kodilog.TAIL_BYTES = saved


# ---- the routes ----------------------------------------------------------------

def test_routes_without_password():
    """<summary>With no password the panel is off and the log is refused.</summary>"""
    print("running test_routes_without_password")
    kodi_stubs.SETTINGS["web_password"] = ""
    status, _, data = call("/api/logs")
    equal(status, 200, "the listing answers")
    equal(json.loads(data), {"enabled": False}, "and says only that it is off")
    status, _, _ = call("/api/logs/view?source=current")
    equal(status, 403, "the view is refused")
    status, _, data = call("/api/logs/download?source=current")
    equal(status, 403, "the download is refused")
    check(b"2026-11-16" not in data, "no log text leaks in the refusal")


def test_routes_with_password():
    """<summary>Behind a password: 401 without the session, the log with it.</summary>"""
    print("running test_routes_with_password")
    kodi_stubs.SETTINGS["web_password"] = "pw"
    try:
        for path in ("/api/logs", "/api/logs/view?source=current",
                     "/api/logs/download?source=current"):
            status, _, _ = call(path)
            equal(status, 401, "%s wants the password" % path)
        auth = {"X-Auth-Token": webserver._token_for("pw")}
        status, _, data = call("/api/logs", auth)
        info = json.loads(data)
        check(status == 200 and info["enabled"], "the listing is on")
        equal([s["id"] for s in info["sources"]], ["current", "previous"], "two logs")
        check(info["sources"][0]["exists"] and not info["sources"][1]["exists"],
              "only kodi.log is there")
        status, _, data = call("/api/logs/view?source=current&component=functional"
                               "&q=down&limit=50", auth)
        view = json.loads(data)
        equal(status, 200, "the view answers")
        equal(view["matched"], 1, "filters reach through the route")
        status, _, _ = call("/api/logs/view?source=../kodi.log", auth)
        equal(status, 404, "a source outside the list is refused")
        status, _, _ = call("/api/logs/view?source=current&limit=lots", auth)
        equal(status, 200, "a bad limit falls back to the default")
        status, response, data = call("/api/logs/download?source=current", auth)
        equal(status, 200, "the download answers")
        equal(data, SAMPLE.encode("utf-8"), "the whole file is sent unchanged")
        check('filename="kodi.log"' in (response.getheader("Content-Disposition") or ""),
              "it downloads as kodi.log")
        status, _, _ = call("/api/logs/download?source=previous", auth)
        equal(status, 404, "a missing file is a 404")
        status, _, _ = call("/api/logs/download?source=/etc/passwd", auth)
        equal(status, 404, "a path as the source is refused")
    finally:
        kodi_stubs.SETTINGS["web_password"] = ""


def main():
    """
    <summary>
    Run every test and return the exit code: 0 when all passed, 1 otherwise.
    </summary>
    """
    global PORT
    server = ThreadingHTTPServer(("127.0.0.1", 0), webserver.Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    PORT = server.server_address[1]
    try:
        test_entries()
        test_tail_filters()
        test_tail_partial()
        test_routes_without_password()
        test_routes_with_password()
    finally:
        server.shutdown()
        server.server_close()
        kodi_stubs.cleanup()
        shutil.rmtree(LOGDIR, ignore_errors=True)
    if FAILURES:
        print("test_kodilog: %d failure(s)" % len(FAILURES))
        return 1
    print("test_kodilog: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
