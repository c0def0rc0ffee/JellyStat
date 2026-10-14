# -*- coding: utf-8 -*-
"""
<summary>
Stand-ins for the four Kodi modules, for tests that import the add-on
without Kodi.
</summary>
<remarks>
install() puts xbmc, xbmcaddon, xbmcgui and xbmcvfs into sys.modules and the
add-on folder on sys.path, so a test can then import webserver, playlog or
webdata as the real service does. Settings live in SETTINGS, Home window
properties in PROPS, and special://profile resolves to a fresh temporary
folder, so every test run starts with no database and leaves nothing behind.
The screenshot harness in tools/screenshots/stubs does the same job from a
JSON file; these are kept separately so a test needs no environment variables
and no files to set up.
</remarks>
"""

import json
import os
import shutil
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADDON_DIR = os.path.join(ROOT, "script.jellystat")

SETTINGS = {}
PROPS = {}
LOGGED = []
PROFILE = None


def fake_jsonrpc(request):
    """
    <summary>
    Answer Addons.GetAddons from ADDONS; anything else gets an empty result.
    </summary>
    <param name="request">The JSON request text.</param>
    <returns>JSON text.</returns>
    """
    req = json.loads(request)
    if req.get("method") == "Addons.GetAddons":
        return json.dumps({"result": {"addons": [
            {"addonid": a, "name": n} for a, n in ADDONS]}})
    return json.dumps({"id": 1, "jsonrpc": "2.0", "result": {}})


ADDONS = []


class _Addon(object):
    """
    <summary>
    xbmcaddon.Addon over the SETTINGS dict, with the real add-on folder as its path.
    </summary>
    """

    def __init__(self, addon_id=None):
        """
        <summary>
        Any id is accepted; there is one settings store.
        </summary>
        <param name="addon_id">Ignored beyond being remembered.</param>
        """
        self.id = addon_id or "script.jellystat"

    def getSetting(self, key):
        """<summary>The setting as a string, empty when unset.</summary>"""
        return str(SETTINGS.get(key, ""))

    def setSetting(self, key, value):
        """<summary>Store a setting.</summary>"""
        SETTINGS[key] = value

    def getAddonInfo(self, key):
        """<summary>path, profile, id, name or version.</summary>"""
        return {"path": ADDON_DIR, "id": self.id, "name": "JellyStat",
                "version": "0.0.0-test",
                "profile": "special://profile/addon_data/script.jellystat/"
                }.get(key, "")


class _Window(object):
    """<summary>xbmcgui.Window over the PROPS dict.</summary>"""

    def __init__(self, window_id):
        """<summary>Any window id is accepted.</summary>"""
        self.window_id = window_id

    def getProperty(self, key):
        """<summary>The property, empty when unset.</summary>"""
        return PROPS.get(key, "")

    def setProperty(self, key, value):
        """<summary>Store a property.</summary>"""
        PROPS[key] = value

    def clearProperty(self, key):
        """<summary>Forget a property.</summary>"""
        PROPS.pop(key, None)


class _Dialog(object):
    """<summary>Dialogs that answer with their defaults and show nothing.</summary>"""

    def ok(self, heading, message=""):
        """<summary>True.</summary>"""
        return True

    def notification(self, heading, message, icon=None, time=5000, sound=True):
        """<summary>Nothing.</summary>"""
        pass

    def textviewer(self, heading, text, usemono=False):
        """<summary>Nothing.</summary>"""
        pass

    def select(self, heading, options, *args, **kwargs):
        """<summary>Cancelled.</summary>"""
        return -1


class _Monitor(object):
    """<summary>A monitor that never aborts.</summary>"""

    def abortRequested(self):
        """<summary>False.</summary>"""
        return False

    def waitForAbort(self, timeout=0.0):
        """<summary>False at once.</summary>"""
        return False


class _Player(object):
    """<summary>A player with nothing playing.</summary>"""

    def isPlayingVideo(self):
        """<summary>False.</summary>"""
        return False

    def getTime(self):
        """<summary>Zero.</summary>"""
        return 0.0

    def getTotalTime(self):
        """<summary>Zero.</summary>"""
        return 0.0


def _log(message, level=1):
    """<summary>Keep every line so a test can look for one.</summary>"""
    LOGGED.append((level, message))


def _translate(path):
    """
    <summary>
    special://profile/ resolved under the temporary profile folder.
    </summary>
    <param name="path">A Kodi path.</param>
    <returns>A filesystem path; anything else is returned as is.</returns>
    """
    prefix = "special://profile/"
    if path.startswith(prefix):
        return os.path.join(PROFILE, path[len(prefix):])
    return path


def install():
    """
    <summary>
    Put the stub modules in place and the add-on folder on sys.path.
    </summary>
    <returns>The temporary profile folder, which the caller may remove with
    cleanup() at the end.</returns>
    <remarks>
    Safe to call more than once in a process; the second call reuses the
    profile. Call it before importing any add-on module.
    </remarks>
    """
    global PROFILE
    if PROFILE is None:
        PROFILE = tempfile.mkdtemp(prefix="jellystat-test-")
    xbmc = types.ModuleType("xbmc")
    xbmc.LOGDEBUG, xbmc.LOGINFO, xbmc.LOGWARNING, xbmc.LOGERROR = 0, 1, 2, 3
    xbmc.log = _log
    xbmc.executeJSONRPC = fake_jsonrpc
    xbmc.Monitor = _Monitor
    xbmc.Player = _Player
    xbmcaddon = types.ModuleType("xbmcaddon")
    xbmcaddon.Addon = _Addon
    xbmcgui = types.ModuleType("xbmcgui")
    xbmcgui.Window = _Window
    xbmcgui.Dialog = _Dialog
    xbmcgui.NOTIFICATION_WARNING = "warning"
    xbmcgui.NOTIFICATION_INFO = "info"
    xbmcvfs = types.ModuleType("xbmcvfs")
    xbmcvfs.translatePath = _translate
    for name, module in (("xbmc", xbmc), ("xbmcaddon", xbmcaddon),
                         ("xbmcgui", xbmcgui), ("xbmcvfs", xbmcvfs)):
        sys.modules[name] = module
    if ADDON_DIR not in sys.path:
        sys.path.insert(0, ADDON_DIR)
    return PROFILE


def cleanup():
    """
    <summary>
    Remove the temporary profile folder.
    </summary>
    """
    global PROFILE
    if PROFILE and os.path.isdir(PROFILE):
        shutil.rmtree(PROFILE, ignore_errors=True)
    PROFILE = None


def logged(fragment):
    """
    <summary>
    Whether any logged line contains the fragment.
    </summary>
    <param name="fragment">Text to look for.</param>
    <returns>True or False.</returns>
    """
    return any(fragment in message for _, message in LOGGED)
