# Daily genre stats to a website

Matches JellyStat 0.28.9.

The background service (`service.py`) can POST a genre watch summary to your own website once a day. It starts with Kodi and stays off until an endpoint URL is set.

## Setting it up

1. Add-on settings → **Website stats** → **Website endpoint URL**.
2. **Auth token (optional)**: only if the site wants one; otherwise leave it blank.
3. The add-on's run menu has **Send stats to website now** for testing.

It uses the Jellyfin login JellyStat already has (from Jellyfin for Kodi, or the add-on's own server settings), so there is no extra authentication to set up. Only that one account is read; other users on the server are never touched.

## When it sends

Add-on settings → **Website stats** → **Schedule**:

- **When to send** (default **At a set time**): hold the day's snapshot back until a chosen hour, or send **As soon as Kodi loads** (the behaviour before 0.11.0).
- **Send at (hour, 0 to 23)** (default **18**). Nothing is sent before it, so the day's viewing arrives as one update at a predictable time.
- **Days to send** (default **Every day**), or **Weekdays (Mon to Fri)**, or **Weekends (Sat and Sun)**.

On the set time schedule the service checks every five minutes, so a Kodi box already switched on sends within a few minutes of the hour. A box switched on later catches up the same evening, at the first check after it starts. Days that are skipped, by the day filter or because Kodi was never on after the chosen hour, cost nothing: every send is the full current snapshot, so the next one carries everything.

A send is counted as done for the local calendar date, so there is exactly one a day whichever schedule is used. **Send stats to website now** counts as that day's send. Failed sends retry hourly until one succeeds.

The same hour also drives the daily snapshot for the dashboard's over-time charts, whether or not an endpoint is set (see [DASHBOARD.md](DASHBOARD.md)).

## What it sends

The full current snapshot each time: `version: 1` JSON with separate `movies` and `tv` blocks, each holding `last_30_days` and `all_time` windows. The legacy `windows` key duplicates `movies` for older site code. Full details are in [`genre-stats-spec.md`](../genre-stats-spec.md), with an example in [`genre-stats-example.json`](../genre-stats-example.json).

TV counts are per episode, with genres taken from the episode's show. Rules the sender applies:

- **Watched** means films with Jellyfin's *Played* flag (about 90% completion).
- **Last 30 days** is a rolling 30 by 24 hour window ending at send time (UTC), based on each film's last played date. It is approximate, since Jellyfin keeps only the most recent play date per item. The add-on's own **Recent window (days)** setting does not change it.
- **Genres** are alias merged ("Sci-Fi" becomes "Science Fiction", and so on). Films with no genre are reported as "Unknown", and a film with several genres counts **once per genre**, so genre counts can add up to more than `total` (which is distinct films). Lists are sorted by count, highest first.
- Since 0.15.0 science fiction no longer swallows a title's other genres (a science fiction action film used to count only as science fiction), and fantasy is its own genre rather than folded into science fiction.
- Each genre entry carries both `count` and `percent` (the share of the window's films with that genre, to 1 decimal place), so the site can show either without computing anything.
- Each window carries watch pace figures: `per_day` (items divided by the window's days; all time spans the earliest play to now) and `avg_gap_minutes` (the average minutes between watches, `null` under 2 items). All time pace is approximate for the same last played date reason.

Success needs HTTP 200 with `{"ok":true}`. Anything else is written to the Kodi log (search for `[JellyStat]`) and retried at the next hourly check.
