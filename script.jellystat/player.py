# -*- coding: utf-8 -*-
"""
<summary>
Watch Kodi playback and hand finished sessions to the play log.
</summary>
<remarks>
Kodi tells an addon when playback starts and stops, but not how far it got:
by the time onPlayBackStopped fires there is nothing playing to ask. So a
small sampler thread notes the position every few seconds while a video runs,
and the ground this sitting covered (the furthest point reached, less the
point it started from) is what the session is credited with.

That subtraction is the whole reason resuming works. A film picked up at
1:30 has ninety minutes behind it that belong to the evening that watched
them, and crediting this sitting with the player's absolute position would
hand them over a second time.

Everything here is defensive. A player callback runs on Kodi's own thread,
and an exception thrown there is an exception thrown into playback, so no
failure in this file is allowed to escape it.
</remarks>
"""

import threading
import time
from datetime import datetime

import xbmc
import xbmcaddon

import playlog

SAMPLE_SECONDS = 5

# Below this, the sampler's last reading is treated as unusable and the
# session is credited with wall-clock time instead (see _finish).
MIN_SAMPLES = 1


def log(message, level=xbmc.LOGINFO):
    """
    <summary>
    Write one line to Kodi's log under the JellyStat tag.
    </summary>
    <param name="message">Text to log.</param>
    <param name="level">Kodi log level, info by default.</param>
    """
    xbmc.log("[JellyStat] %s" % message, level)


class PlayLogger(xbmc.Player):
    """
    <summary>
    Records one row per sitting, with the clock time it happened.
    </summary>
    """

    def __init__(self):
        """
        <summary>
        Set up the lock and the empty session; nothing is logged until playback starts.
        </summary>
        <remarks>
        Each sampler gets its own stop Event: a shared one has a race on a quick stop then start, where the old thread misses the set() and never exits.
        </remarks>
        """
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
        """
        <summary>
        Kodi hook: begin a session once the item's length is known.
        </summary>
        <remarks>
        onPlayBackStarted fires before Kodi knows the length, so getTotalTime() would read 0.
        </remarks>
        """
        try:
            self._begin()
        except Exception as err:
            log("Could not start logging this item: %s" % err, xbmc.LOGWARNING)

    def onPlayBackEnded(self):
        """
        <summary>
        Kodi hook: finish the session in progress.
        </summary>
        """
        self._safe_finish()

    def onPlayBackStopped(self):
        """
        <summary>
        Kodi hook: finish the session in progress.
        </summary>
        """
        self._safe_finish()

    def onPlayBackError(self):
        """
        <summary>
        Kodi hook: finish the session in progress.
        </summary>
        """
        self._safe_finish()

    # ---- session ----------------------------------------------------------

    def _enabled(self):
        """
        <summary>
        Whether play logging is switched on in the settings.
        </summary>
        <returns>True unless set to false.</returns>
        """
        addon = xbmcaddon.Addon(playlog.history.ADDON_ID)
        return addon.getSetting("playlog_enabled") != "false"

    def _min_seconds(self):
        """
        <summary>
        The minimum sitting length in seconds, from the settings or the default.
        </summary>
        <returns>An int.</returns>
        """
        addon = xbmcaddon.Addon(playlog.history.ADDON_ID)
        try:
            return int(addon.getSetting("playlog_min_minutes")) * 60
        except (ValueError, TypeError):
            return playlog.DEFAULT_MIN_SECONDS

    def _begin(self):
        """
        <summary>
        Start a session for the playing video, if it is a film or an episode.
        </summary>
        <remarks>
        Records the start position so a resumed film only counts what this sitting covers, and a monotonic clock reading so a clock correction during playback cannot make the elapsed time negative.
        </remarks>
        """
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
        try:
            # Where this sitting picks up. Zero for a fresh start, the
            # resume point for anything continued, and everything before
            # that point was watched on some other evening.
            start_position = int(self.getTime())
        except RuntimeError:
            start_position = 0
        session = {
            "started_at": datetime.now(),
            # Wall clock for the record, a monotonic reading for the
            # arithmetic. The service creates this logger before the
            # startup delay precisely so a film begun in the first minute
            # is caught, which is exactly when a box without a clock
            # battery gets its NTP correction. A backwards step made the
            # elapsed time negative and threw the whole sitting away.
            "started_monotonic": time.monotonic(),
            "media": media,
            "title": tag.getTitle() or "?",
            "show": tag.getTVShowTitle() or None,
            "season": tag.getSeason() if tag.getSeason() >= 0 else None,
            "episode": tag.getEpisode() if tag.getEpisode() >= 0 else None,
            "year": tag.getYear() or None,
            "runtime_seconds": runtime,
            "start_position": start_position,
            # Seeded at the start point, not zero: this tracks the furthest
            # point reached, and the sitting has already reached this one.
            "position": start_position,
            "samples": 0,
        }
        with self._lock:
            self._session = session
        self._start_sampler()

    def _safe_finish(self):
        """
        <summary>
        Finish the session, logging rather than raising on any failure.
        </summary>
        """
        try:
            self._finish()
        except Exception as err:
            log("Could not finish logging this item: %s" % err,
                xbmc.LOGWARNING)

    def _finish(self):
        """
        <summary>
        Close the session, work out how much was watched and hand it to the play log.
        </summary>
        <remarks>
        With enough samples, watched time is the furthest position reached less the start position, so a paused film does not collect its pause and a resumed one does not collect what was already past. Otherwise the wall clock is all there is. Either way it is capped at the elapsed time.
        </remarks>
        """
        if self._stop_sampler is not None:
            self._stop_sampler.set()
        with self._lock:
            session = self._session
            self._session = None
        if not session:
            return
        session["ended_at"] = datetime.now()
        elapsed = int(time.monotonic() - session["started_monotonic"])
        if session["samples"] >= MIN_SAMPLES:
            # The ground this sitting covered: how far playback reached,
            # less where it began. Position rather than elapsed time, so a
            # film left paused does not collect the hour it sat on the
            # pause screen; less the start, so a film resumed at 1:30 does
            # not collect the ninety minutes it was already past.
            watched = session["position"] - session["start_position"]
        else:
            # Stopped before the first sample; wall clock is all there is.
            watched = elapsed
        session["watched_seconds"] = max(min(watched, elapsed or watched), 0)
        playlog.record(session, self._min_seconds())

    # ---- sampler ----------------------------------------------------------

    def _start_sampler(self):
        """
        <summary>
        Start the daemon thread that samples the playback position.
        </summary>
        """
        stop = threading.Event()
        self._stop_sampler = stop
        self._sampler = threading.Thread(target=self._sample, args=(stop,),
                                         name="JellyStatSampler")
        self._sampler.daemon = True
        self._sampler.start()

    def _sample(self, stop):
        """
        <summary>
        Thread body: every SAMPLE_SECONDS record the furthest position reached, until stopped or playback ends.
        </summary>
        <param name="stop">Event that ends the loop.</param>
        <remarks>
        The first sample rebases the start point in case the resume seek landed late.
        </remarks>
        """
        while not stop.wait(SAMPLE_SECONDS):
            try:
                if not self.isPlayingVideo():
                    return
                position = int(self.getTime())
            except (RuntimeError, OSError):
                return
            with self._lock:
                session = self._session
                if session is None:
                    return
                if session["samples"] == 0:
                    self._rebase(session, position)
                # Highest point reached, so skipping back near the end does
                # not undo a film that was watched through.
                session["position"] = max(session["position"], position)
                session["samples"] += 1

    @staticmethod
    def _rebase(session, position):
        """
        <summary>
        Move the starting point if the resume seek landed late.
        </summary>
        <remarks>
        onAVStarted can fire before Kodi has finished seeking to a resume
        point, and getTime() still reads zero when it does. The first
        sample gives that away: a position further into the item than the
        time since playback began could possibly account for is somewhere
        playback jumped to, not somewhere it played to, so that is where
        this sitting actually started.

        Only the first sample is judged this way. Later jumps are someone
        skipping about inside a sitting they are watching, which is what
        the highest-point-reached rule above is deliberately there to
        absorb.
        </remarks>
        """
        elapsed = time.monotonic() - session["started_monotonic"]
        # One sample interval of slack: the reading is up to that old.
        reachable = session["start_position"] + elapsed + SAMPLE_SECONDS
        if position > reachable:
            session["start_position"] = position
            session["position"] = position

    # ---- shutdown ---------------------------------------------------------

    def close(self):
        """
        <summary>
        Flush a session in progress, so a Kodi shutdown mid-film counts.
        </summary>
        """
        self._safe_finish()
