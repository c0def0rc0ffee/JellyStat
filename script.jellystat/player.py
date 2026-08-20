# -*- coding: utf-8 -*-
"""Watch Kodi playback and hand finished sessions to the play log.

Kodi tells an addon when playback starts and stops, but not how far it got:
by the time onPlayBackStopped fires there is nothing playing to ask. So a
small sampler thread notes the position every few seconds while a video runs,
and the last sample it took is what the session is credited with.

Everything here is defensive. A player callback runs on Kodi's own thread,
and an exception thrown there is an exception thrown into playback, so no
failure in this file is allowed to escape it.
"""

import threading
from datetime import datetime

import xbmc
import xbmcaddon

import playlog

SAMPLE_SECONDS = 5

# Below this, the sampler's last reading is treated as unusable and the
# session is credited with wall-clock time instead (see _finish).
MIN_SAMPLES = 1


def log(message, level=xbmc.LOGINFO):
    xbmc.log("[JellyStat] %s" % message, level)


class PlayLogger(xbmc.Player):
    """Records one row per sitting, with the clock time it happened."""

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._session = None
        # Each sampler gets its own Event: a shared one has a race on a
        # quick stop->start (playlist auto-advance), where the old thread
        # misses the set() because the new session's clear() lands first,
        # and then never exits.
        self._stop_sampler = None
        self._sampler = None

    # ---- Kodi callbacks ---------------------------------------------------

    def onAVStarted(self):
        # onAVStarted, not onPlayBackStarted: the latter fires before Kodi
        # knows the item's length, so getTotalTime() would read 0.
        try:
            self._begin()
        except Exception as err:
            log("Could not start logging this item: %s" % err, xbmc.LOGWARNING)

    def onPlayBackEnded(self):
        self._safe_finish()

    def onPlayBackStopped(self):
        self._safe_finish()

    def onPlayBackError(self):
        self._safe_finish()

    # ---- session ----------------------------------------------------------

    def _enabled(self):
        addon = xbmcaddon.Addon(playlog.history.ADDON_ID)
        return addon.getSetting("playlog_enabled") != "false"

    def _min_seconds(self):
        addon = xbmcaddon.Addon(playlog.history.ADDON_ID)
        try:
            return int(addon.getSetting("playlog_min_minutes")) * 60
        except (ValueError, TypeError):
            return playlog.DEFAULT_MIN_SECONDS

    def _begin(self):
        if not self._enabled() or not self.isPlayingVideo():
            return
        tag = self.getVideoInfoTag()
        media = (tag.getMediaType() or "").lower()
        # Music videos, live TV and loose files carry no watch history worth
        # charting; logging them would put noise in every habit chart.
        if media not in ("movie", "episode"):
            return
        try:
            runtime = int(self.getTotalTime())
        except RuntimeError:
            runtime = 0
        session = {
            "started_at": datetime.now(),
            "media": media,
            "title": tag.getTitle() or "?",
            "show": tag.getTVShowTitle() or None,
            "season": tag.getSeason() if tag.getSeason() >= 0 else None,
            "episode": tag.getEpisode() if tag.getEpisode() >= 0 else None,
            "year": tag.getYear() or None,
            "runtime_seconds": runtime,
            "position": 0,
            "samples": 0,
        }
        with self._lock:
            self._session = session
        self._start_sampler()

    def _safe_finish(self):
        try:
            self._finish()
        except Exception as err:
            log("Could not finish logging this item: %s" % err,
                xbmc.LOGWARNING)

    def _finish(self):
        if self._stop_sampler is not None:
            self._stop_sampler.set()
        with self._lock:
            session = self._session
            self._session = None
        if not session:
            return
        session["ended_at"] = datetime.now()
        elapsed = int((session["ended_at"]
                       - session["started_at"]).total_seconds())
        if session["samples"] >= MIN_SAMPLES:
            # Position, not elapsed time: a paused film should not count the
            # hour it sat on the pause screen.
            watched = session["position"]
        else:
            # Stopped before the first sample; wall clock is all there is.
            watched = elapsed
        session["watched_seconds"] = max(min(watched, elapsed or watched), 0)
        playlog.record(session, self._min_seconds())

    # ---- sampler ----------------------------------------------------------

    def _start_sampler(self):
        stop = threading.Event()
        self._stop_sampler = stop
        self._sampler = threading.Thread(target=self._sample, args=(stop,),
                                         name="JellyStatSampler")
        self._sampler.daemon = True
        self._sampler.start()

    def _sample(self, stop):
        while not stop.wait(SAMPLE_SECONDS):
            try:
                if not self.isPlayingVideo():
                    return
                position = int(self.getTime())
            except (RuntimeError, OSError):
                return
            with self._lock:
                if self._session is None:
                    return
                # Highest point reached, so skipping back near the end does
                # not undo a film that was watched through.
                self._session["position"] = max(self._session["position"],
                                                position)
                self._session["samples"] += 1

    # ---- shutdown ---------------------------------------------------------

    def close(self):
        """Flush a session in progress, so a Kodi shutdown mid-film counts."""
        self._safe_finish()
