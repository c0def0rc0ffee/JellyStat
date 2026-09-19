# -*- coding: utf-8 -*-
"""
<summary>
special:// paths resolved against the harness profile directory.
</summary>
"""

import os

PROFILE = os.environ["JS_HARNESS_PROFILE"]
HOME = os.environ.get("JS_HARNESS_HOME", PROFILE)


def translatePath(path):
    """
    <summary>
    Resolve special://profile, special://home and special://temp against the harness folders.
    </summary>
    <param name="path">A Kodi path.</param>
    <returns>A filesystem path; anything else is returned as is.</returns>
    """
    if path.startswith("special://profile/"):
        return os.path.join(PROFILE, path[len("special://profile/"):])
    if path.startswith("special://home/"):
        return os.path.join(HOME, path[len("special://home/"):])
    if path.startswith("special://temp/"):
        return os.path.join(PROFILE, "temp", path[len("special://temp/"):])
    return path


def exists(path):
    """
    <summary>
    Whether the resolved path exists.
    </summary>
    <param name="path">A Kodi path.</param>
    """
    return os.path.exists(translatePath(path))


def mkdirs(path):
    """
    <summary>
    Create the resolved folder.
    </summary>
    <param name="path">A Kodi path.</param>
    <returns>True.</returns>
    """
    real = translatePath(path)
    if not os.path.isdir(real):
        os.makedirs(real, exist_ok=True)
    return True
