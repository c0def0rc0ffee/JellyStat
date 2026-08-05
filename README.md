# JellyStat: Kodi addon for Jellyfin watch statistics

A Kodi script addon that shows what you have watched on your **Jellyfin
server**, including each item's **genre** and **rating** (community rating,
critic rating when available, and a ♥ marker for favourites).

## How it connects

On launch the addon looks for a login in this order:

1. **Jellyfin for Kodi** (`plugin.video.jellyfin`). If you're signed in with
   that addon, JellyStat reuses its saved server address, access token and
   user automatically. No setup needed.
2. **Addon settings**. Otherwise open JellyStat's settings and enter your
   server URL and an API key (Jellyfin Dashboard → API Keys), plus optionally
   a username (defaults to the first user).

## What it shows

Run it from **Add-ons → Program add-ons → JellyStat**:

- **Summary**, watched totals for movies and episodes, both **all time** and
  **last 30 days**, total play count, average ratings, genre breakdowns as
  **percentages** for each of those four groups, and your favourite movies.
  (An item with several genres counts towards each, so percentages can sum
  to more than 100.)
- **Movies, all time / last 30 days**, every played movie as
  `Title (Year)  [Genres]  ★ 7.8 | Critics: 85% | ♥`, most recently
  watched first.
- **Episodes, all time / last 30 days**, every played episode as
  `Show S01E02 - Title  [Show genres]  ★ 8.1` (genres come from the show,
  as Jellyfin doesn't store genres per episode).

"Last 30 days" is based on each item's last-played date reported by Jellyfin.

### Display settings

Addon settings → **Display**:

- **Genre stats: show percentages instead of counts** (default off), genre
  breakdowns read `Action: 40%` instead of `Action: 12`.
- **Summary: show average ratings** (default on)
- **Summary: show favourite movies** (default on)
- **Lists: show ratings** (default on), hides the rating part of list rows.
- **Recent window (days)** (default 30, range 7 to 90), changes the "last N
  days" used everywhere in the addon UI. The website snapshot always uses a
  fixed 30 days regardless, to match the site's format.

## Installing in Kodi

1. Download the latest `script.jellystat-x.y.z.zip` from the
   [**Releases**](../../releases) page.
2. In Kodi go to **Settings → System → Add-ons** and enable
   **Unknown sources** (needed for installing from zip).
3. **Settings → Add-ons → Install from zip file** and pick the zip.
4. Run it from **Add-ons → Program add-ons → JellyStat**.

Requires Kodi 19 (Matrix) or newer.

## Daily genre stats to the website

The addon includes a background service (`service.py`) that POSTs a
movie-genre watch summary to the website once per day. It starts with Kodi
and stays off until a URL is configured.

### When it sends

Addon settings → **Website stats** → **Schedule**:

- **When to send** (default **At a set time**), either hold the day's
  snapshot back until a chosen hour, or send as soon as Kodi loads (the
  behaviour before v0.11.0).
- **Send time** (default **18**), the hour on a 24 hour clock. Nothing is
  sent before it, so the day's viewing arrives as one update at a predictable
  time.
- **Days to send** (default **Every day**), or weekdays only, or weekends
  only.

On the set-time schedule the service checks every five minutes, so a Kodi box
already switched on sends within a few minutes of the hour. A box switched on
later catches up the same evening, at the first check after it starts. Days
that are skipped, by the day filter or because Kodi was never on after the
chosen hour, cost nothing: every send is the full current snapshot, so the
next one carries everything.

A send is counted as done for the local calendar date, so there is exactly one
per day whichever schedule is used. **Send stats to website now** counts as
that day's send. Failed sends retry hourly until one succeeds.

It uses the Jellyfin login the Jellyfin for Kodi addon already has, so there
is no extra authentication to set up. Only that one account is read; other
users on the server are never touched.

Setup: JellyStat addon settings → **Website stats** → set the endpoint URL
(and the auth token only if the site wants one, otherwise leave blank).
The addon menu also has **Send stats to website now** for testing.

It sends the full current snapshot each time (`version: 1` JSON with
separate `movies` and `tv` blocks, each holding `last_30_days` and
`all_time` windows; the legacy `windows` key duplicates `movies` for
compatibility, full details in `genre-stats-spec.md`). TV counts are per
episode, with genres taken from the episode's show. Rules the sender
applies:

- **Watched** = movies with Jellyfin's *Played* flag (~90% completion).
- **Last 30 days** = rolling 30×24h window ending at send time (UTC), based
  on each movie's last-played date. Approximate, since Jellyfin keeps only
  the most recent play date per item.
- **Genres** are alias-merged ("Sci-Fi" → "Science Fiction" etc.), movies
  with **no genre are reported as "Unknown"**, and a multi-genre movie counts
  **once per genre**, so genre counts can sum to more than `total` (which is
  distinct movies). Lists are sorted descending by count.
- Each genre entry carries both `count` and `percent` (share of the window's
  movies with that genre, 1 decimal), so the site can display either without
  computing anything.
- Each window carries watch-pace figures: `per_day` (items ÷ window days;
  all-time spans earliest play → now) and `avg_gap_minutes` (average minutes
  between watches, `null` under 2 items). All-time pace is approximate for
  the same last-played-date reason.

Success requires HTTP 200 with `{"ok":true}`; anything else is logged to the
Kodi log (search `[JellyStat]`) and retried at the next hourly check.

## Project layout

```
script.jellystat/
├── addon.xml            # addon manifest (script + service)
├── main.py              # entry point, Jellyfin API + dialogs
├── stats_sender.py      # snapshot payload + website POST
├── service.py           # background service: daily send on Kodi load
└── resources/
    ├── icon.png         # addon icon
    └── settings.xml     # server fallback + website stats settings
JellyStat Dist/          # versioned addon build zips
```

## Rebuilding the install zip

Builds go in the `JellyStat Dist` folder, named
`script.jellystat-<version>.zip` with the version taken from `addon.xml`
(bump it there first). Zip entries must use forward slashes and contain the
`script.jellystat` folder at the root. The snippet below handles both; plain
`Compress-Archive` writes backslashes that break non-Windows Kodi:

```powershell
[xml]$manifest = Get-Content script.jellystat\addon.xml -Raw
$out = "$PWD\JellyStat Dist\script.jellystat-$($manifest.addon.version).zip"
Get-ChildItem script.jellystat -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
Remove-Item $out -ErrorAction SilentlyContinue
Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
$zip = [IO.Compression.ZipFile]::Open($out, 'Create')
Get-ChildItem "$PWD\script.jellystat" -Recurse -File | ForEach-Object {
  $name = $_.FullName.Substring("$PWD".Length + 1).Replace('\','/')
  [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $_.FullName, $name) | Out-Null
}
$zip.Dispose()
```

## License

[MIT](LICENSE)
