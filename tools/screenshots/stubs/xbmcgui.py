# -*- coding: utf-8 -*-
"""
<summary>
Dialogs that never appear; the harness runs with nobody watching.
</summary>
"""

import sys

NOTIFICATION_INFO = "info"
NOTIFICATION_WARNING = "warning"
NOTIFICATION_ERROR = "error"


class Dialog(object):
    """
    <summary>
    Dialogs that print to stderr or answer with their defaults.
    </summary>
    """
    def ok(self, heading, message=""):
        """
        <summary>
        Print and answer True.
        </summary>
        <param name="heading">Heading.</param>
        <param name="message">Message.</param>
        """
        sys.stderr.write("[dialog:ok] %s | %s\n" % (heading, message))
        return True

    def yesno(self, heading, message="", *args, **kwargs):
        """
        <summary>
        Always False.
        </summary>
        <param name="heading">Heading.</param>
        <param name="message">Message.</param>
        """
        return False

    def notification(self, heading, message, icon=None, time=5000, sound=True):
        """
        <summary>
        Print.
        </summary>
        <param name="heading">Heading.</param>
        <param name="message">Message.</param>
        <param name="icon">Ignored.</param>
        <param name="time">Ignored.</param>
        <param name="sound">Ignored.</param>
        """
        sys.stderr.write("[dialog:note] %s | %s\n" % (heading, message))

    def textviewer(self, heading, text, usemono=False):
        """
        <summary>
        Nothing.
        </summary>
        <param name="heading">Ignored.</param>
        <param name="text">Ignored.</param>
        <param name="usemono">Ignored.</param>
        """
        pass

    def select(self, heading, options, *args, **kwargs):
        """
        <summary>
        Always cancelled.
        </summary>
        <param name="heading">Ignored.</param>
        <param name="options">Ignored.</param>
        <returns>Minus one.</returns>
        """
        return -1

    def input(self, heading, defaultt="", *args, **kwargs):
        """
        <summary>
        The default, unchanged.
        </summary>
        <param name="heading">Ignored.</param>
        <param name="defaultt">Returned as is.</param>
        """
        return defaultt

    def browseSingle(self, *args, **kwargs):
        """
        <summary>
        Always empty.
        </summary>
        """
        return ""


class DialogProgress(object):
    """
    <summary>
    A progress dialog that shows nothing.
    </summary>
    """
    def create(self, heading, message=""):
        """
        <summary>
        Nothing.
        </summary>
        <param name="heading">Ignored.</param>
        <param name="message">Ignored.</param>
        """
        pass

    def update(self, percent, message=""):
        """
        <summary>
        Nothing.
        </summary>
        <param name="percent">Ignored.</param>
        <param name="message">Ignored.</param>
        """
        pass

    def iscanceled(self):
        """
        <summary>
        Always False.
        </summary>
        """
        return False

    def close(self):
        """
        <summary>
        Nothing.
        </summary>
        """
        pass


class DialogProgressBG(DialogProgress):
    """
    <summary>
    Same as DialogProgress.
    </summary>
    """
    pass


class ListItem(object):
    """
    <summary>
    A list item holding only its label.
    </summary>
    """
    def __init__(self, label="", label2="", path=""):
        """
        <summary>
        Keep the label.
        </summary>
        <param name="label">Kept.</param>
        <param name="label2">Ignored.</param>
        <param name="path">Ignored.</param>
        """
        self.label = label
