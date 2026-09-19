# -*- coding: utf-8 -*-
"""
<summary>
Minimal stand-ins for the Kodi APIs JellyStat touches.
</summary>
"""

import json
import os
import sys

LOGDEBUG, LOGINFO, LOGWARNING, LOGERROR = 0, 1, 2, 3
_NAMES = {0: "DEBUG", 1: "INFO", 2: "WARN", 3: "ERROR"}

VERBOSE = os.environ.get("JS_HARNESS_VERBOSE") == "1"


def log(message, level=LOGINFO):
    """
    <summary>
    Print to stderr at warning level and above, or everything when VERBOSE.
    </summary>
    <param name="message">Text.</param>
    <param name="level">Kodi log level.</param>
    """
    if VERBOSE or level >= LOGWARNING:
        sys.stderr.write("[kodi:%s] %s\n" % (_NAMES.get(level, level), message))


def executeJSONRPC(request):
    """
    <summary>
    An empty successful JSON-RPC reply.
    </summary>
    <param name="request">Ignored.</param>
    <returns>A JSON string.</returns>
    """
    return json.dumps({"id": 1, "jsonrpc": "2.0", "result": {}})


class Monitor(object):
    """
    <summary>
    A monitor that never aborts.
    </summary>
    """
    def abortRequested(self):
        """
        <summary>
        Always False.
        </summary>
        """
        return False

    def waitForAbort(self, timeout=0.0):
        """
        <summary>
        Returns False at once; nothing waits in the harness.
        </summary>
        <param name="timeout">Ignored.</param>
        """
        return False


class Player(object):
    """
    <summary>
    A player with nothing playing.
    </summary>
    """
    def isPlaying(self):
        """
        <summary>
        Always False.
        </summary>
        """
        return False

    def isPlayingVideo(self):
        """
        <summary>
        Always False.
        </summary>
        """
        return False

    def getPlayingFile(self):
        """
        <summary>
        Always empty.
        </summary>
        """
        return ""

    def getTime(self):
        """
        <summary>
        Always zero.
        </summary>
        """
        return 0.0

    def getTotalTime(self):
        """
        <summary>
        Always zero.
        </summary>
        """
        return 0.0

    def getVideoInfoTag(self):
        """
        <summary>
        Raises, as Kodi does with nothing playing.
        </summary>
        <exception cref="RuntimeError">Always.</exception>
        """
        raise RuntimeError("nothing is playing")
