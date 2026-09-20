# -*- coding: utf-8 -*-
"""
<summary>
Background service: the web dashboard, plus the daily snapshot jobs.
</summary>
<remarks>
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
4. The headline figures published as Home window properties on every check
   beat, so a skin can show them without touching this addon's database or
   its web server (see publish_skin_properties).

It also owns the play logger: an xbmc.Player subclass that must live for the
whole Kodi session (Kodi only delivers callbacks while the object exists),
recording every sitting with its real start and end time into the play log.
</remarks>
"""

from datetime import datetime, timedelta

import xbmc
import xbmcaddon
import xbmcgui

import history
import player
import screentime
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

# Window properties for skins. Written on Kodi's Home window (id 10000),
# which every skin can read from anywhere as
# $INFO[Window(home).Property(JellyStat.<name>)]. The names are part of the
# addon's public surface from 0.26.4 on: change them and every skin reading
# them goes blank, so add rather than rename.
HOME_WINDOW_ID = 10000
SKIN_PROPERTY_PREFIX = "JellyStat."
SKIN_PROPERTY_NAMES = (
    "Month", "Month.Hours", "Month.Minutes", "Month.Movies", "Month.Episodes",
    "Month.Titles", "Month.Plays",
    "AllTime.Hours", "AllTime.Minutes", "AllTime.Movies", "AllTime.Episodes",
    "AllTime.Shows", "AllTime.Titles", "AllTime.Plays", "AllTime.Ratings",
    "Week.Hours", "Week.Delta", "Week.Average", "Week.WakingPercent",
    "Week.Movies.Hours", "Week.Shows.Hours", "Week.Measured",
    "Updated",
) + tuple("Day.%d.%s" % (n, k) for n in range(1, 8)
          for k in ("Label", "Hours", "Minutes", "Percent", "Today")) \
  + tuple("Daypart.%d.%s" % (n, k) for n in range(1, 5)
          for k in ("Label", "Sessions"))
WEEK_DAYS = 7

# "When to send" setting values.
ON_KODI_LOAD = 0
AT_SET_TIME = 1

# "Days to send" setting values.
EVERY_DAY = 0
WEEKDAYS = 1
WEEKENDS = 2


def setting_int(addon, name, default):
    """
    <summary>
    An integer setting, with the default when it is unset or not a number.
    </summary>
    <param name="addon">The addon whose setting is read.</param>
    <param name="name">Setting id.</param>
    <param name="default">Returned when the value will not parse.</param>
    """
    try:
        return int(addon.getSetting(name))
    except ValueError:
        return default


def day_allowed(days, now):
    """
    <summary>
    Whether the daily send may run today under the weekdays, weekends or every day setting.
    </summary>
    <param name="days">The setting value.</param>
    <param name="now">A datetime.</param>
    <returns>True or False.</returns>
    """
    if days == WEEKDAYS:
        return now.weekday() < 5
    if days == WEEKENDS:
        return now.weekday() >= 5
    return True


def is_due(addon, now):
    """
    <summary>
    True when today's snapshot should go out at this moment.
    </summary>
    <remarks>
    One send per calendar day (local date), and on the set-time schedule not
    before the chosen hour and only on the chosen days. A day that is skipped
    loses nothing: every send is the full current snapshot.
    </remarks>
    """
    if addon.getSetting("last_send_date") == now.strftime("%Y-%m-%d"):
        return False
    # The chosen days apply to both schedules. They used not to on Kodi
    # load, while the settings screen still let the days be picked, so
    # "weekdays only" was set, looked set, and sent on Saturday anyway.
    if not day_allowed(setting_int(addon, "send_days", EVERY_DAY), now):
        return False
    if setting_int(addon, "send_when", AT_SET_TIME) == ON_KODI_LOAD:
        return True
    return now.hour >= setting_int(addon, "send_hour", 18)


def history_due(addon, now):
    """
    <summary>
    True when today's row is still missing and the hour has come.
    </summary>
    <remarks>
    Deliberately ignores the "days to send" filter: history is the
    dashboard's own record and a gap in it is a hole in every chart, whereas
    a skipped website send costs nothing.
    </remarks>
    """
    if addon.getSetting("history_enabled") == "false":
        return False
    if setting_int(addon, "send_when", AT_SET_TIME) == AT_SET_TIME \
            and now.hour < setting_int(addon, "send_hour", 18):
        return False
    return not history.recorded_today(now)


def web_config(addon):
    """
    <summary>
    The settings a running server cannot change under itself.
    </summary>
    """
    return (addon.getSetting("web_enabled"),
            addon.getSetting("web_port"),
            addon.getSetting("web_bind_all"))


def sync_web(addon, applied):
    """
    <summary>
    Start, stop or restart the dashboard to match the current settings.
    </summary>
    """
    wanted = web_config(addon)
    if wanted == applied:
        return applied
    webserver.stop()
    webserver.start()
    return wanted


def hours_label(minutes):
    """
    <summary>
    A short reading of a minute count for a skin label.
    </summary>
    <param name="minutes">Estimated minutes watched.</param>
    <returns>"12h 30m" below a hundred hours, then whole hours with a thousands separator, such as "1,234h".</returns>
    """
    minutes = int(minutes or 0)
    hours, rest = divmod(minutes, 60)
    if hours < 100:
        return "%dh %02dm" % (hours, rest)
    return "{0:,}h".format(hours)


def delta_label(minutes):
    """
    <summary>
    A signed reading of a minute difference, for "against the week before".
    </summary>
    <param name="minutes">Change in minutes, negative when less was watched.</param>
    <returns>"+2h 10m", "-31m" or "0m".</returns>
    """
    minutes = int(minutes or 0)
    sign = "-" if minutes < 0 else "+"
    hours, rest = divmod(abs(minutes), 60)
    if not minutes:
        return "0m"
    if hours:
        return "%s%dh %02dm" % (sign, hours, rest)
    return "%s%dm" % (sign, rest)


def week_values(week):
    """
    <summary>
    The last seven days as skin properties: totals, the daily bars and the daypart split.
    </summary>
    <param name="week">The dict screentime.screen_time returns for seven days.</param>
    <returns>Dict of property name (without prefix) to string value.</returns>
    <remarks>
    Each day's Percent is its share of the busiest day in the window, so a
    skin can draw the bars with a progress control without doing any
    arithmetic itself. The figures are the play log's measured sittings,
    so on a box whose log is young they cover only the days it has.
    </remarks>
    """
    days = week["breakdown"]
    top = max([d["minutes"] for d in days] + [1])
    values = {
        "Week.Hours": hours_label(week["total"]["minutes"]),
        "Week.Delta": delta_label(week["total"]["delta"]),
        "Week.Average": hours_label(week["daily_average"]["minutes"]),
        "Week.WakingPercent": "%g" % week["waking"]["percent"],
        "Week.Movies.Hours": hours_label(week["movies"]["minutes"]),
        "Week.Shows.Hours": hours_label(week["shows"]["minutes"]),
        "Week.Measured": "1" if week["measured"] else "",
    }
    names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    for n, day in enumerate(days[:WEEK_DAYS], 1):
        values["Day.%d.Label" % n] = names[day["weekday"]]
        values["Day.%d.Hours" % n] = hours_label(day["minutes"])
        values["Day.%d.Minutes" % n] = str(day["minutes"])
        values["Day.%d.Percent" % n] = str(int(round(100.0 * day["minutes"] / top)))
        values["Day.%d.Today" % n] = "1" if day["today"] else ""
    for n, part in enumerate(week["dayparts"][:4], 1):
        values["Daypart.%d.Label" % n] = part["name"]
        values["Daypart.%d.Sessions" % n] = str(part["sessions"])
    return values


def publish_skin_properties(now=None):
    """
    <summary>
    Write the headline figures (this month, all time and the last seven
    days) as JellyStat.* properties on the Home window, for skins.
    </summary>
    <param name="now">Clock to use, for tests; the real one when omitted.</param>
    <remarks>
    The figures are the tiles the dashboard shows, from screentime.tiles():
    titles counted and hours estimated from runtime times plays, so they
    reach back over the whole library. With no plays recorded at all the
    properties are cleared rather than published as zeroes, so a skin that
    gates on a non-empty property shows nothing on a box that has not yet
    read its library. A failure is logged and never reaches the loop.
    </remarks>
    """
    window = xbmcgui.Window(HOME_WINDOW_ID)
    try:
        data = screentime.tiles(now)
        week = screentime.screen_time(WEEK_DAYS, now)
    except Exception as err:  # the loop must outlive a broken database
        stats_sender.log("Could not publish the skin properties: %s" % err,
                         xbmc.LOGWARNING)
        return
    month, ever = data["this_month"], data["all_time"]
    if not ever["plays"]:
        for name in SKIN_PROPERTY_NAMES:
            window.clearProperty(SKIN_PROPERTY_PREFIX + name)
        return
    values = {
        "Month": data["month_label"],
        "Month.Hours": hours_label(month["minutes"]),
        "Month.Minutes": str(int(month["minutes"])),
        "Month.Movies": str(month["movies"]),
        "Month.Episodes": str(month["episodes"]),
        "Month.Titles": str(month["movies"] + month["episodes"]),
        "Month.Plays": str(month["plays"]),
        "AllTime.Hours": hours_label(ever["minutes"]),
        "AllTime.Minutes": str(int(ever["minutes"])),
        "AllTime.Movies": str(ever["movies"]),
        "AllTime.Episodes": str(ever["episodes"]),
        "AllTime.Shows": str(ever["shows"]),
        "AllTime.Titles": str(ever["movies"] + ever["episodes"]),
        "AllTime.Plays": str(ever["plays"]),
        "AllTime.Ratings": str(ever["ratings"]),
        "Updated": (now or datetime.now()).strftime("%H:%M"),
    }
    values.update(week_values(week))
    for name, value in values.items():
        window.setProperty(SKIN_PROPERTY_PREFIX + name, value)


class Monitor(xbmc.Monitor):
    """
    <summary>
    Flags settings changes so the loop can react between its beats.
    </summary>
    <remarks>
    Only a flag: the callback arrives on Kodi's thread, and starting or
    stopping the HTTP server there would race the service loop doing the
    same. The loop polls the flag every few seconds instead.
    </remarks>
    """

    def __init__(self):
        """
        <summary>
        Start with no pending settings change.
        </summary>
        """
        super().__init__()
        self.settings_changed = False

    def onSettingsChanged(self):
        """
        <summary>
        Kodi hook: flag that the loop should re-read the settings.
        </summary>
        """
        self.settings_changed = True


def run():
    """
    <summary>
    Service entry point: start the play logger and the dashboard, wait out the startup delay, then loop until Kodi exits.
    </summary>
    <remarks>
    Each pass restarts the dashboard when its settings changed, republishes the skin properties, runs the daily send when it is due (backing off after a failure), records the day's snapshot, and stops cleanly on abort.
    </remarks>
    """
    monitor = Monitor()
    # Created before the startup delay: a film started in Kodi's first
    # minute should still be logged.
    play_logger = player.PlayLogger()
    # The dashboard answers as soon as Kodi is up. The startup delay below
    # exists to let the network and Jellyfin settle before the daily jobs:
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
        publish_skin_properties(now)

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
