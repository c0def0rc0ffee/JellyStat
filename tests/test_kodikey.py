#!/usr/bin/env python3
"""
<summary>
Tests for playback.kodi_key, which names a film, episode or show by its Kodi
library key so the dashboard can add it to a Functional skin list.
</summary>
<remarks>
No Kodi: kodi_stubs stands in for the Kodi modules, and the library index and
the JSON-RPC call are replaced with small fakes, so what is tested is the
mapping itself. Exits non zero when a check fails.

Usage:
    python3 tests/test_kodikey.py
</remarks>
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import kodi_stubs  # noqa: E402

kodi_stubs.install()

import playback  # noqa: E402

FAILURES = []


def equal(got, want, what):
    """
    <summary>Record a failure when got is not want.</summary>
    <param name="got">The value produced.</param>
    <param name="want">The value expected.</param>
    <param name="what">What is being checked, for the report.</param>
    """
    if got != want:
        FAILURES.append("{}: got {!r}, want {!r}".format(what, got, want))


def main():
    """
    <summary>Run the checks and return the exit code.</summary>
    <returns>0 when every check passed, 1 otherwise.</returns>
    """
    print("running test_kodi_key")
    index = {"aaa111": {"type": "movieid", "dbid": 12},
             "bbb222": {"type": "episodeid", "dbid": 340}}
    playback.index = lambda force=False: index
    shows = {"tvshows": [{"tvshowid": 5, "title": "Night Post"},
                         {"tvshowid": 9, "title": "Sundial"}]}
    asked = []

    def fake_rpc(method, params=None):
        """<summary>Answer GetTVShows from the fake library.</summary>"""
        asked.append(method)
        return shows if method == "VideoLibrary.GetTVShows" else {}

    playback.rpc = fake_rpc
    equal(playback.kodi_key(item_id="AAA111"), "movie:12", "a film by its Jellyfin id, any case")
    equal(playback.kodi_key(item_id="bbb222"), "episode:340", "an episode by its Jellyfin id")
    equal(playback.kodi_key(series_name=" night post "), "tvshow:5", "a show by name, ignoring case and spaces")
    equal(playback.kodi_key(series_name="Nowhere"), None, "a show Kodi does not have")
    equal(playback.kodi_key(), None, "nothing named, nothing found")
    playback._index["rebuilt_on_miss"] = 0
    equal(playback.kodi_key(item_id="zzz"), None, "a film Kodi does not have")
    equal(asked, ["VideoLibrary.GetTVShows", "VideoLibrary.GetTVShows"],
          "films and episodes come from the index, only shows ask Kodi")
    if FAILURES:
        print("\n".join(FAILURES))
        return 1
    print("test_kodikey: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
