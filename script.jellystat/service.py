# -*- coding: utf-8 -*-
"""Background service: the web dashboard, plus the daily snapshot jobs.

Three things run from this one loop, so the addon only ever holds one
background thread of its own:

1. The web dashboard's HTTP server, started as soon as the setting is on and
   restarted if the port or binding is changed.
2. The daily send of the genre snapshot to a website, if an endpoint URL is
   configured. By default it holds the day's snapshot back until a set hour
   (18:00) rather than sending on startup, so the site is updated at a
   predictable time; the older "send as soon as Kodi loads" behaviour is
   still available in the settings.
3. One snapshot a day recorded to the local history database, which is what
   gives the dashboard its "over time" charts. This happens whether or not a
   website endpoint is set, at the same hour as the send so a box that is on
   all day still only reads the library once for it.

It also owns the play logger: an xbmc.Player subclass that must live for the
whole Kodi session (Kodi only delivers callbacks while the object exists),
recording every sitting with its real start and end time into the play log.
"""

from datetime import datetime, timedelta

import xbmc
import xbmcaddon

import history
import player
import stats_sender
import webdata
import webserver

STARTUP_DELAY_S = 60
CHECK_INTERVAL_S = 300
RETRY_DELAY_S = 3600
# How quickly a settings change is noticed. Short, because "I enabled the
# dashboard and the page refuses to connect" is exactly the first-run
# experience; the daily jobs still only run on the CHECK_INTERVAL_S beat.
SETTINGS_POLL_S = 5

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
    # The chosen days apply to both schedules. They used not to on Kodi
    # load, while the settings screen still let the days be picked - so
    # "weekdays only" was set, looked set, and sent on Saturday anyway.
    if not day_allowed(setting_int(addon, "send_days", EVERY_DAY), now):
        return False
    if setting_int(addon, "send_when", AT_SET_TIME) == ON_KODI_LOAD:
        return True
    return now.hour >= setting_int(addon, "send_hour", 18)


def history_due(addon, now):
    """True when today's row is still missing and the hour has come.

    Deliberately ignores the "days to send" filter: history is the
    dashboard's own record and a gap in it is a hole in every chart, whereas
    a skipped website send costs nothing.
    """
    if addon.getSetting("history_enabled") == "false":
        return False
    if setting_int(addon, "send_when", AT_SET_TIME) == AT_SET_TIME \
            and now.hour < setting_int(addon, "send_hour", 18):
        return False
    return not history.recorded_today(now)


def web_config(addon):
    """The settings a running server cannot change under itself."""
    return (addon.getSetting("web_enabled"),
            addon.getSetting("web_port"),
            addon.getSetting("web_bind_all"))


def sync_web(addon, applied):
    """Start, stop or restart the dashboard to match the current settings."""
    wanted = web_config(addon)
    if wanted == applied:
        return applied
    webserver.stop()
    webserver.start()
    return wanted


class Monitor(xbmc.Monitor):
    """Flags settings changes so the loop can react between its beats.

    Only a flag: the callback arrives on Kodi's thread, and starting or
    stopping the HTTP server there would race the service loop doing the
    same. The loop polls the flag every few seconds instead.
    """

    def __init__(self):
        super().__init__()
        self.settings_changed = False

    def onSettingsChanged(self):
        self.settings_changed = True


def run():
    monitor = Monitor()
    # Created before the startup delay: a film started in Kodi's first
    # minute should still be logged.
    play_logger = player.PlayLogger()
    # The dashboard answers as soon as Kodi is up. The startup delay below
    # exists to let the network and Jellyfin settle before the daily jobs -
    # serving a local page needs neither.
    applied = sync_web(xbmcaddon.Addon(stats_sender.ADDON_ID), None)
    waited = 0
    while waited < STARTUP_DELAY_S:
        if monitor.waitForAbort(SETTINGS_POLL_S):
            play_logger.close()
            webserver.stop()
            return
        waited += SETTINGS_POLL_S
        if monitor.settings_changed:
            monitor.settings_changed = False
            applied = sync_web(xbmcaddon.Addon(stats_sender.ADDON_ID),
                               applied)
    retry_after = None
    since_jobs = CHECK_INTERVAL_S  # run the daily checks on the first pass
    while not monitor.abortRequested():
        addon = xbmcaddon.Addon(stats_sender.ADDON_ID)
        if monitor.settings_changed:
            monitor.settings_changed = False
            applied = sync_web(addon, applied)
        if since_jobs < CHECK_INTERVAL_S:
            if monitor.waitForAbort(SETTINGS_POLL_S):
                break
            since_jobs += SETTINGS_POLL_S
            continue
        since_jobs = 0
        now = datetime.now()
        applied = sync_web(addon, applied)

        if (addon.getSetting("endpoint_url").strip() and is_due(addon, now)
                and (retry_after is None or now >= retry_after)):
            try:
                payload = stats_sender.send()
                retry_after = None
                # The send already read the whole library; reuse it rather
                # than asking Jellyfin for the same thing again minutes later.
                if addon.getSetting("history_enabled") != "false":
                    history.record(payload, now)
            except Exception as err:  # never let one bad send kill the loop
                # Back off to hourly retries, so a site that is down does not
                # get hammered every check.
                retry_after = now + timedelta(seconds=RETRY_DELAY_S)
                stats_sender.log("Send failed, will retry in an hour: %s"
                                 % err, xbmc.LOGWARNING)

        if history_due(addon, now):
            try:
                data, _ = webdata.get(setting_int(addon, "recent_days", 30),
                                      max_age=3600)
                history.record(data["snapshot"], now)
            except Exception as err:
                stats_sender.log("Could not record today's history: %s" % err,
                                 xbmc.LOGWARNING)
    play_logger.close()
    webserver.stop()


if __name__ == "__main__":
    run()
