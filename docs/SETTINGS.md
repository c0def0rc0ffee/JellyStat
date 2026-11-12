# Settings

Every setting in JellyStat's add-on settings, as of **JellyStat 0.28.9**. Update this page whenever `resources/settings.xml` changes.

Open them from **Add-ons → My add-ons → Program add-ons → JellyStat → Configure**. The dashboard's own per browser options (date format, clock, popup corner, recommendations) are in [DASHBOARD.md](DASHBOARD.md#the-dashboards-own-settings-page).

## Signing in

On launch JellyStat looks for a Jellyfin login in this order:

1. **Jellyfin for Kodi** (`plugin.video.jellyfin`). If you are signed in there, JellyStat reuses its saved server address, access token and user. No setup needed.
2. **Add-on settings**, below: a server URL and an API key (Jellyfin **Dashboard → API Keys**), plus optionally a username.

## Jellyfin server

Only needed if the Jellyfin for Kodi add-on is not signed in.

| Setting | Default | What it does |
|---|---|---|
| Server URL | blank | For example `http://jellyfin.local:8096` |
| API key | blank | Made in Jellyfin under **Dashboard → API Keys**. An administrator's key also lets your rating be written to the community rating (see [RATINGS.md](RATINGS.md)) |
| Username | blank | Blank means the first user |
| Ignore SSL certificate errors | Off | For a server with a self signed certificate |

## Display

These shape the Kodi window that opens from **Add-ons → Program add-ons → JellyStat**.

| Setting | Default | What it does |
|---|---|---|
| Genre stats: show percentages instead of counts | Off | Genre breakdowns read `Action: 40%` instead of `Action: 12` |
| Summary: show average ratings | On | |
| Summary: show favourite movies | On | |
| Lists: show ratings | On | Hides the rating part of list rows when off |
| Recent window (days) | 30 | 7 to 90. The "last N days" used everywhere in the add-on's own window. The website snapshot always uses a fixed 30 days |

The Kodi window shows:

- **Summary**: watched totals for films and episodes, both all time and over the recent window, total play count, average ratings, genre breakdowns for each of those four groups, and your favourite films. A title with several genres counts towards each, so percentages can add up to more than 100.
- **Movies**, all time and recent: every played film as `Title (Year)  [Genres]  ★ 7.8 | Critics: 85% | ♥`, most recently watched first. The ♥ marks a favourite, and the critic rating appears when Jellyfin has one.
- **Episodes**, all time and recent: every played episode as `Show S01E02: Title  [Show genres]  ★ 8.1`. Genres come from the show, as Jellyfin does not store genres per episode.

"Recent" is based on each item's last played date as Jellyfin reports it.

## Web dashboard

| Setting | Default | What it does |
|---|---|---|
| Enable the web dashboard | Off | Starts the dashboard in the background service |
| Web dashboard address | (button) | Shows the addresses to type into a browser |
| Port | 8099 | 1024 to 65535 |
| Reachable from other machines | On | Off binds to `127.0.0.1`, so only the Kodi box can open the page. On with no password set still serves the Kodi box only |
| Password (optional) | blank | The page and its JSON endpoints then ask for it. See [DASHBOARD.md](DASHBOARD.md#security) |
| Refresh data at most every (minutes) | 10 | 1 to 120. How long a library read is cached; the page's **Refresh** button bypasses it |

**Ratings**

| Setting | Default | What it does |
|---|---|---|
| A 9 or 10 also marks the item a favourite on Jellyfin | Off | |

**History**

| Setting | Default | What it does |
|---|---|---|
| Keep a daily snapshot for the over-time charts | On | One row a day in `history.db` |
| Log each play with its date and time | On | The play log (see [PLAY-LOG-AND-IMPORT.md](PLAY-LOG-AND-IMPORT.md)) |
| Write last watched dates from the play log to Jellyfin | On | Keeps Jellyfin's last played dates in step with the play log |
| Ignore sessions shorter than (minutes) | 2 | 1 to 30. Shorter sittings are treated as mis-clicks |

A settings change is picked up by the background service within five seconds.

## Website stats

Daily film and TV genre snapshot, off while the URL is blank. See [WEBSITE-STATS.md](WEBSITE-STATS.md).

| Setting | Default | What it does |
|---|---|---|
| Website endpoint URL | blank | Where the daily snapshot is sent |
| Auth token (optional) | blank | Sent only if set |
| When to send | At a set time | Or **As soon as Kodi loads** |
| Send at (hour, 0 to 23) | 18 | Also the hour of the dashboard's daily snapshot |
| Days to send | Every day | Or weekdays (Mon to Fri), or weekends (Sat and Sun) |
