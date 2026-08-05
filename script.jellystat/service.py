# -*- coding: utf-8 -*-
"""Background service: send the genre snapshot to the website once per day.

Starts with Kodi. By default it holds the day's snapshot back until a set
hour (18:00) rather than sending on startup, so the site is updated at a
predictable time; the older "send as soon as Kodi loads" behaviour is still
available in the settings. It then checks every few minutes, so the send
lands close to the chosen hour on a box that is already on, and catches up
later the same day on a box switched on after it. Nothing runs until a
website endpoint URL is set in the addon settings.
"""

from datetime import datetime, timedelta

import xbmc
import xbmcaddon

import stats_sender

STARTUP_DELAY_S = 60
CHECK_INTERVAL_S = 300
RETRY_DELAY_S = 3600

# "When to send" setting values.
ON_KODI_LOAD = 0
AT_SET_TIME = 1

# "Days to send" setting values.
EVERY_DAY = 0
WEEKDAYS = 1
WEEKENDS = 2


def setting_int(addon, name, default):
    try:
        return int(addon.getSetting(name))
    except ValueError:
        return default


def day_allowed(days, now):
    if days == WEEKDAYS:
        return now.weekday() < 5
    if days == WEEKENDS:
        return now.weekday() >= 5
    return True


def is_due(addon, now):
    """True when today's snapshot should go out at this moment.

    One send per calendar day (local date), and on the set-time schedule not
    before the chosen hour and only on the chosen days. A day that is skipped
    loses nothing: every send is the full current snapshot.
    """
    if addon.getSetting("last_send_date") == now.strftime("%Y-%m-%d"):
        return False
    if setting_int(addon, "send_when", AT_SET_TIME) == ON_KODI_LOAD:
        return True
    if not day_allowed(setting_int(addon, "send_days", EVERY_DAY), now):
        return False
    return now.hour >= setting_int(addon, "send_hour", 18)


def run():
    monitor = xbmc.Monitor()
    if monitor.waitForAbort(STARTUP_DELAY_S):
        return
    retry_after = None
    while not monitor.abortRequested():
        now = datetime.now()
        addon = xbmcaddon.Addon(stats_sender.ADDON_ID)
        if (addon.getSetting("endpoint_url").strip() and is_due(addon, now)
                and (retry_after is None or now >= retry_after)):
            try:
                stats_sender.send()
                retry_after = None
            except Exception as err:  # never let one bad send kill the loop
                # Back off to hourly retries, so a site that is down does not
                # get hammered every check.
                retry_after = now + timedelta(seconds=RETRY_DELAY_S)
                stats_sender.log("Send failed, will retry in an hour: %s"
                                 % err, xbmc.LOGWARNING)
        if monitor.waitForAbort(CHECK_INTERVAL_S):
            return


if __name__ == "__main__":
    run()
