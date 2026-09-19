# -*- coding: utf-8 -*-
"""
<summary>
An Addon whose settings live in one JSON file the harness can write.
</summary>
"""

import json
import os
import re
import threading

ADDON_PATH = os.environ["JS_HARNESS_ADDON"]
PROFILE = os.environ["JS_HARNESS_PROFILE"]
STORE = os.path.join(PROFILE, "harness_settings.json")

_lock = threading.Lock()


def _load():
    """
    <summary>
    The settings JSON, empty when missing or unreadable.
    </summary>
    <returns>A dict.</returns>
    """
    try:
        with open(STORE, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def _save(values):
    """
    <summary>
    Write the settings JSON.
    </summary>
    <param name="values">The whole settings dict.</param>
    """
    with open(STORE, "w", encoding="utf-8") as handle:
        json.dump(values, handle, indent=2, sort_keys=True)


def _version():
    """
    <summary>
    The version from the addon's addon.xml, or 0.0.0.
    </summary>
    <returns>A string.</returns>
    """
    try:
        with open(os.path.join(ADDON_PATH, "addon.xml"), encoding="utf-8") as h:
            text = h.read()
    except OSError:
        return "0.0.0"
    match = re.search(r'id="script\.jellystat"\s+[^>]*version="([^"]+)"', text)
    if not match:
        match = re.search(r'version="([^"]+)"', text)
    return match.group(1) if match else "0.0.0"


def _strings():
    """
    <summary>
    The string table from strings.po.
    </summary>
    <returns>Id to text.</returns>
    """
    path = os.path.join(ADDON_PATH, "resources", "language",
                        "resource.language.en_gb", "strings.po")
    table = {}
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return table
    for number, value in re.findall(r'msgctxt\s+"#(\d+)"\s*\nmsgid\s+"(.*?)"',
                                    text):
        table[int(number)] = value
    return table


_TRANSLATIONS = _strings()


class Addon(object):
    """
    <summary>
    The addon object: settings in one JSON file, info from the real addon.xml and strings.po.
    </summary>
    """
    def __init__(self, id=None):
        """
        <summary>
        Default to script.jellystat.
        </summary>
        <param name="id">An addon id, or None.</param>
        """
        self._id = id or "script.jellystat"

    # -- settings ---------------------------------------------------------
    def getSetting(self, key):
        """
        <summary>
        A setting as a string, JSON encoded when it was stored as something else.
        </summary>
        <param name="key">Setting id.</param>
        <returns>A string, empty when unset.</returns>
        """
        with _lock:
            value = _load().get(key, "")
        return value if isinstance(value, str) else json.dumps(value)

    def getSettingString(self, key):
        """
        <summary>
        Same as getSetting.
        </summary>
        <param name="key">Setting id.</param>
        """
        return self.getSetting(key)

    def getSettingBool(self, key):
        """
        <summary>
        True when the stored string is true.
        </summary>
        <param name="key">Setting id.</param>
        """
        return self.getSetting(key) == "true"

    def getSettingInt(self, key):
        """
        <summary>
        The stored value as an int, 0 when it will not parse.
        </summary>
        <param name="key">Setting id.</param>
        """
        try:
            return int(self.getSetting(key))
        except ValueError:
            return 0

    def setSetting(self, key, value):
        """
        <summary>
        Store a setting under the lock.
        </summary>
        <param name="key">Setting id.</param>
        <param name="value">The value.</param>
        """
        with _lock:
            values = _load()
            values[key] = value
            _save(values)

    setSettingString = setSetting

    def setSettingBool(self, key, value):
        """
        <summary>
        Store true or false.
        </summary>
        <param name="key">Setting id.</param>
        <param name="value">A bool.</param>
        """
        self.setSetting(key, "true" if value else "false")

    def setSettingInt(self, key, value):
        """
        <summary>
        Store a number as a string.
        </summary>
        <param name="key">Setting id.</param>
        <param name="value">An int.</param>
        """
        self.setSetting(key, str(value))

    def openSettings(self):
        """
        <summary>
        Nothing to open in the harness.
        </summary>
        """
        pass

    # -- metadata ---------------------------------------------------------
    def getAddonInfo(self, key):
        """
        <summary>
        path, profile, id, name or version.
        </summary>
        <param name="key">Which.</param>
        <returns>A string, empty when unknown.</returns>
        """
        return {
            "path": ADDON_PATH,
            "profile": "special://profile/addon_data/%s/" % self._id,
            "id": self._id,
            "name": "JellyStat",
            "version": _version(),
        }.get(key, "")

    def getLocalizedString(self, number):
        """
        <summary>
        The text for a string id, empty when unknown.
        </summary>
        <param name="number">The id.</param>
        <returns>A string.</returns>
        """
        return _TRANSLATIONS.get(number, "")
