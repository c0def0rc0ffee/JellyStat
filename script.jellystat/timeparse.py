# -*- coding: utf-8 -*-
"""
<summary>
Date parsing that survives Kodi's embedded Python.
</summary>
<remarks>
datetime.strptime keeps one reference to the _strptime module for the whole
Kodi process, shared by every add-on interpreter. When the interpreter that
first imported it ends (any script add-on that runs and exits, this add-on's
own menu included), that module's globals are cleared to None, and from then
until Kodi restarts every datetime.strptime call in every interpreter raises
TypeError: 'NoneType' object is not callable. time.strptime imports the module
afresh in the calling interpreter on every call, so it keeps working. Every
strptime in this add-on goes through here, which tries the fast path and falls
back to the slow one on that exact failure.

No Kodi imports, so any module can use it without a cycle.
</remarks>
"""

import time
from datetime import datetime


def strptime(value, fmt):
    """
    <summary>
    datetime.strptime, with the time.strptime fallback Kodi needs.
    </summary>
    <param name="value">The text to parse.</param>
    <param name="fmt">A strptime format.</param>
    <returns>A naive datetime.</returns>
    <exception cref="ValueError">When the text does not match the format, from
    either path.</exception>
    <remarks>
    Only TypeError is caught, and only because that is what the cleared module
    raises; a ValueError from a bad value is the caller's to handle, as it was
    before. The fallback drops anything finer than a second, which no format in
    this add-on asks for.
    </remarks>
    """
    try:
        return datetime.strptime(value, fmt)
    except TypeError:
        return datetime(*(time.strptime(value, fmt)[0:6]))
