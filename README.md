# JellyStat: Kodi addon for Jellyfin watch statistics

A Kodi script addon that shows what you have watched on your **Jellyfin
server**, including each item's **genre** and **rating** (community rating,
critic rating when available, and a ♥ marker for favourites). It also serves
a **web dashboard** you can open from your phone or laptop.

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

## Web dashboard

The addon can serve its own dashboard over HTTP, so the stats are readable
from any device on your network without installing anything else. It runs
inside the addon's background service, which starts with Kodi, and stays off
until you switch it on.

Setup: addon settings → **Web dashboard** → **Enable the web dashboard**,
then press the **Web dashboard address** button right below it: a box shows
the addresses to type into a browser, worked out from the box itself, so you
do not have to go looking for its IP. The default is port **8099**, i.e.
something like `http://your-kodi-box:8099/`. (The same dialog is also in
JellyStat's run menu.) The background service picks a settings change up
within five minutes; restarting Kodi applies it immediately.

### What's on it

At the top sit two **headline blocks**, this month and all time, counting
episodes, shows, films, plays, hours watched and ratings given. Hours are
estimated from each title's runtime times how often it was played, so they
cover the whole library including everything watched before this addon
existed.

Below them, **Screen time** is measured rather than estimated: it comes from
the play log's real sittings, so it only covers the period the log reaches
back to. Daily bars for the last week, daily average, the split between
morning, afternoon, evening and night, totals, the share of waking hours,
and films against shows, each compared with the previous week.

A **Now playing** card appears in the corner whenever the Kodi box is
playing something, with the poster, a progress bar, how long is left and the
clock time it will finish at. It polls every fifteen seconds and can be
dismissed until the next thing starts.

The page has a left menu: **Overview** (the stats below), **Recommended**,
**Movies**, **TV**, **Rate**, **Data** (backup and import) and
**Settings** (⚙).

- **Recommended** suggests films and shows in separate sections, scored
  entirely from your own watching: each candidate on your server is weighted
  by how much of your viewing falls in its genres, times its rating. The
  genre match is cosine-normalised, so a title is not promoted merely for
  carrying a long list of tags. Nothing
  leaves the box and no external service is consulted. Clicking any
  suggestion opens its page, watched or not. Each section has a **genre
  filter** (tick any number of genres; a title must carry **every** genre ticked, and
  each chip shows how many titles would be left if you added it, so the
  filter never leads to a dead end) and **Back** / **Forward** buttons to page through the whole ranked
  list, with a running "showing 21 to 40 of 6,170". Filtering narrows the
  list without re-ranking it, so picking Horror answers "the horror you
  would most likely enjoy" rather than "your favourites that happen to be
  horror".
- **Movies** and **TV** each carry their own stats (totals, plays, average
  rating, films by decade, most replayed, best rated, most watched shows).
- Every film and show page ends with **Similar films** / **Similar shows**,
  by shared genres with a nudge for a similar era and better rating.
- Film, show and person pages carry **artwork**: posters, episode stills and
  cast photographs, all proxied through the addon so the browser never sees
  a Jellyfin token and a phone that can reach the dashboard can see the
  pictures even when it cannot reach Jellyfin itself.
- A film or show page shows its **overview**, and badges for **resolution**
  (4K, 1080p, 720p, 576p, 480p, judged on width so a scope film is not
  mislabelled), codec, container, file size, runtime and certificate.
- **Cast and crew** appear as a strip of faces. Click anyone for their own
  page: every title of theirs on your server, split into what you have
  watched and what you have not.
- A show lists its **episodes as cards** with the still, the description and
  that episode's own resolution badges, ticked where you have watched it.
- Every film and show page can **play or queue on the Kodi box** the
  dashboard is served from, so the phone in your hand works as a remote. A
  show's buttons pick its next unwatched episode. Queueing while nothing is
  playing starts the item rather than leaving it sitting in a playlist.
  Titles are resolved through Kodi's own library, so resume points, watched
  marking and the player's artwork all behave as if you had started it from
  the television.
- Titles in the **Rate** queue link through to their own page.
- **Rate** lists watched titles that have no rating anywhere and lets you
  score them; see "Rating what you have watched" below. Settings currently holds the
site-wide date format (ISO, day/month, month/day, or "20 Aug 2026") and the
clock style (24-hour or am/pm), plus what recommendations may suggest, set
separately for films and TV: **only ones I have not seen**, or **seen and
new**. That choice drives both the Recommended page and the Similar section
on every item page. All of these are stored in the browser, so each device
viewing the dashboard can have its own. A **search box** in the header finds any watched film
or TV show and opens its own page. For a film that page carries ratings,
play count and logged sittings; for a show, episodes watched per season,
the watched episode list, and its sittings.

- **Cards**: films and episodes watched, all time and in the recent window,
  total plays, watch pace, current and longest daily streak, average rating.
- **Watching calendar**, a year of daily squares, purple where films
  dominated the day and green where episodes did, darker on quieter days.
- **Time of day** and **day of the week**, the two charts that actually
  describe a habit rather than a library, counted from the play log's real
  sessions once it has any (see below).
- **Genres**, switchable between films and TV, the recent window and all
  time, and three ways of counting: by title, weighted by plays (so
  rewatches pull their genre up), or first-listed genre only (so the shares
  add up to 100%).
- **Recent days**, **ratings you watch**, **top shows** and a
  **recently watched** list.
- **Over time**, newly watched titles per day and how the genre mix has
  moved, drawn from the daily history described below.

The page adapts to a phone screen and follows the browser's light or dark
setting. It is plain HTML with no external requests, so it works on a
network with no internet access.

### Settings

Addon settings → **Web dashboard**:

- **Enable the web dashboard** (default off).
- **Port** (default 8099).
- **Reachable from other machines** (default on). Turning it off binds to
  `127.0.0.1`, so only the Kodi box itself can open the page.
- **Password (optional)**, blank by default. With a password set, the page
  asks for it and the JSON endpoints require it too (send it as an
  `X-Auth-Token` header if you want to script against them).
- **Refresh data at most every (minutes)** (default 10). Reading a whole
  Jellyfin library takes a few seconds, so the result is cached for this
  long; the **Refresh** button on the page bypasses the cache.

This is designed for a home network. It speaks plain HTTP with no
certificate, and the password is a convenience for a shared household, not
protection against the open internet, so don't port-forward it.

### The play log: every sitting, with its date and time

Jellyfin's API cannot say *when* you watched something beyond each item's
single most-recent play date, so a film watched three times reports one
date. The addon therefore keeps its own log: a background listener notes
every movie or episode played on this Kodi box as a session with its real
**start and end clock time** and how much was actually watched (position
reached, so a long pause doesn't count; Kodi being closed mid-film still
logs the sitting).
Sessions shorter than a threshold (default 2 minutes) are ignored as
mis-clicks. Settings: **Web dashboard → History → Log each play with its
date and time** (default on) and **Ignore sessions shorter than (minutes)**.

Once the log has sessions, the calendar, time-of-day and day-of-week charts
count **real plays** from its first covered date onwards, so three episodes
in an evening count as three, and the calendar marks the changeover
(`→ real plays`). Days before it, and titles played on other devices with no
logged session, still count once from Jellyfin's last-played date, so
nothing disappears. The dashboard also gains a **Rewatches** table: titles
with more than one logged sitting, with first and latest dates.

Only what plays on this Kodi box is captured live; history from other
devices or other tools comes in through the importer below.

### Importing watch history from a file

The dashboard's **Import watch history** section backfills the play log from
a file. Nothing is written on upload: the file is parsed and **staged**, you
see what it contains (plays, date range, devices, rows without clock times)
and how it compares with what is already logged (duplicates, new plays,
overlapping days), and only then choose how to apply it:

- **Merge** adds only what is not already logged (matched on title and
  day); existing rows are never touched.
- **Replace** lets this file win for its date range. You pick what it may
  delete first: previously imported sessions in that range (default), and
  optionally this box's own logged sessions. Deletion is permanent and the
  page says so before it happens.

Every import is stored as a batch and listed under the importer with a
**Remove** button, so a wrong file is one click to take back out (a
replace-mode deletion, however, cannot be restored).

Recognised formats, sniffed from the content rather than the extension:

| File | What it gives you |
|---|---|
| Jellyfin **Playback Reporting** plugin backup (JSON) or TSV export | The best source: every play on **every device**, with clock times and device names, years of backdated history at once |
| Another box's JellyStat `history.db` | Merge a second Kodi machine into one picture |
| **Trakt** history CSV | Full timestamps from before you ran Jellyfin |
| **Letterboxd** diary CSV | Films with dates only, kept out of the time-of-day chart rather than inventing an hour |
| Generic CSV/JSON | Columns `started_at, media, title` (plus optional `show, season, episode, year, watched_seconds, device`) |

Uploads are capped at 50 MB, and with a dashboard password set the import
endpoints require it like everything else.

### Rating what you have watched

**Rate** queues up what you have watched, films and episodes separately,
newest watch first, and gives each a 1 to 10 slider with a step arrow either
side and the score beside it. Two scopes:

- **Not rated by me** (the default), everything you have not scored
  yourself, whatever rating the internet has for it. Each row shows the
  community rating as context.
- **Rated by me**, what you have already scored. The slider starts on your
  current score so changing your mind is one drag, and each row says plainly
  where that score is stored: two dots, JellyStat then Jellyfin, followed by
  one of **Saved in JellyStat**, **Saved in Jellyfin**, or **Saved in
  JellyStat and Jellyfin**. A push that failed reads *Saved in JellyStat
  only, Jellyfin refused it* with the second dot red, and if the two ever
  hold different numbers the line shows both. The status is computed from
  the two stored scores rather than from how the rating got there, so the
  dots and the words can never disagree.
- **No rating anywhere**, only titles Jellyfin has no community rating for
  either, which on a well-scraped library is a very short list.

The score is written to Jellyfin as itself, 7 stays 7, using the per-user
`UserData.Rating` field. That is the same field and the same route
**JellyRate** writes when it asks for a score as the credits roll, so the
two addons agree by construction: a rating made in either turns up in the
other.

- Your score is stored by JellyStat in the mirror, and sent to Jellyfin.
- Ratings made anywhere else, in JellyRate, on a phone, or in the Jellyfin
  web app, are read back on every mirror sync, so they appear here without
  any bridge between the addons.
- Where the two disagree the server wins, since it is the copy every device
  shares. The one exception is a local score whose write to Jellyfin
  *failed*: that is a pending write rather than stale data, and is left
  alone until it succeeds.
- The thumbs up or down shown in Jellyfin's own interface is derived by the
  server from the number, so JellyStat does not try to set it.
- Optionally a 9 or 10 also marks the item a favourite
  (settings -> **Web dashboard** -> **Ratings**).
- The public **community rating** is item metadata, not per-user, and
  writing it needs an administrator API key. If the key in the addon's
  server settings is an administrator's, your score is written there too;
  otherwise the page says it cannot be.

A failed write never costs you the score: it is stored locally first, and
the sync state is kept beside it so the page can tell you what did and did
not reach the server.

### Importing a Trakt export

**Data → Import from Trakt** takes the export .zip exactly as Trakt sent it.
The archive is unpacked by the addon and its files classified by shape rather
than by filename, so there is nothing to extract or pick out first. If you
have already unzipped it, select the .json files instead and they are read in
the browser the same way. Either route produces the same import.

Watch history, ratings and your Trakt lists are all read; the rest of the
export is ignored and the staging screen says how many files that was.

Nothing is written until you have seen what was found. The staging screen
reports, per category, how many records there are, how many matched your
library, how many would be overwritten and with what, then you tick the
categories to import.

- **Matching** is by IMDb, TMDb and TVDb id first and by title only as a
  fallback, which is the difference between eight episodes in ten finding
  their row and virtually all of them. Anything the mirror cannot place is
  looked up on the Jellyfin server itself, because the mirror only holds
  items that have been *played* and a title sitting unwatched on the server
  has no row there.
- **Records that match nothing are still imported.** The play happened; this
  library simply has no copy of it.
- **Durations are assumed.** Trakt records that something was played and
  never for how long, so an imported sitting is credited with the item's
  runtime and flagged, and Screen time reports measured and assumed
  separately rather than presenting one as the other.
- **Where a rating exists in both places and they disagree, Trakt wins.**
  Imported ratings land in JellyStat, which is the record of what you think
  of a title; Jellyfin is expected to match it, not the other way round.
- **Lists come across too.** Custom lists, the watchlist and the collection,
  each named, and flagged where the name matches a list you already have.
  What happens to those is part of the mode you pick: leave them alone, add
  what they are missing, or replace their contents outright. Only the last
  deletes anything, and its card counts what it would delete before you
  press it.
- A **backup is taken automatically immediately before and immediately
  after** every import.

### Lists

**Lists** in the sidebar holds sets of titles you chose. It is the one thing
the rest of the addon has no way to express, since a list has no property in
common that a query could find. Make one, name it, and add anything to it
from a film or show page.

An entry keeps the title, year and provider ids it was created with, and
matches to a library item where one exists. So a list can name titles this
server does not hold: an imported Trakt list still names all forty films when
you have twenty of them, the rest are shown as not in your library rather
than dropped, and they attach themselves if those titles ever arrive.
**Re-check unmatched** runs that pass on demand. Lists that were ordered by
hand keep their order.

### Jellyfin agreement

JellyStat holds your score; Jellyfin keeps its own copy and the two drift,
because a Trakt import writes thousands of scores here that were never sent
there. **Data → Jellyfin agreement** reports how they compare and settles it
deliberately rather than silently:

- Jellyfin **missing** a rating is harmless and is only listed.
- Jellyfin holding a **different** one is a contradiction nobody can settle
  from the data, so those are listed with both values.
- **Push** writes JellyStat's score to Jellyfin so the two match; **remove
  Jellyfin's copies** clears them there, leaving the rating only here.
  Either way the end state is the same: never a contradiction, at worst a
  gap.

Both run in the background with progress, since thousands of ratings mean
thousands of requests.

A mirror sync never overwrites a rating JellyStat already holds; it only
fills gaps. Before that rule existed, the sync after the first Trakt import
reverted sixty imported ratings to Jellyfin's values within two minutes.

### Backup and restore

**Data → Automatic backups** lists the snapshots taken either side of every
import, newest first, each downloadable; the most recent twelve are kept and
older ones dropped. **Back up now** takes one on demand. Snapshots use
SQLite's own backup API, so one taken while the service is mid-write is
still a consistent database rather than a torn file.

**Data → Download backup** exports the whole database (mirror, snapshots,
sitting log) as one `.db` file, served as a point-in-time snapshot so a
mid-download write cannot corrupt it. Restoring is importing that file on
the same page.

### Your own copy of the library

Every dashboard load also folds the fetched library into a local mirror
(`items` in `history.db`): one row per watched film and episode, inserted or
updated only when something changed. The mirror is the addon's permanent
copy: items deleted from Jellyfin stay in it, and if Jellyfin is
unreachable the dashboard serves from the mirror ("as of the last sync")
instead of failing. It also enables change detection: when a mirrored
item's last-played date moves forward, that play is recorded in the play
log with Jellyfin's own timestamp, on any device, phone and web included.
Plays on this Kodi box are not double-counted; the player logger already
recorded those first-hand.

### History for the over-time charts

Jellyfin keeps only the *most recent* play date for each item, so it cannot
answer "what did my viewing look like in March". The addon therefore records
one snapshot a day into `history.db` in its own addon-data folder (roughly
10 KB per day, kept for three years) and the **Over time** charts are drawn
from that. It starts filling
in from the day you enable the dashboard, so those charts are thin at first.

The background service takes the snapshot at the same hour as the website
send (**Website stats → Schedule → Send at**, default 18:00), whether or not
a website endpoint is configured. Opening the dashboard also records the day
if it is still missing, so a box that is only switched on in the morning
still builds a history. Either way it is one row per day, and a send's
payload is reused rather than reading the library twice. Turn it off with **Web dashboard → History → Keep a daily
snapshot for the over-time charts**.

## Daily genre stats to the website

The addon includes a background service (`service.py`) that POSTs a
movie-genre watch summary to the website once per day. It starts with Kodi
and stays off until a URL is configured.

### When it sends

Addon settings → **Website stats** → **Schedule**:

- **When to send** (default **At a set time**), either hold the day's
  snapshot back until a chosen hour, or send as soon as Kodi loads (the
  behaviour before v0.11.0).
- **Send at** (default **18**), the hour of the day, 0 to 23. Nothing is sent
  before it, so the day's viewing arrives as one update at a predictable
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
  distinct movies). Lists are sorted descending by count. From v0.15.0 this
  applies to science fiction too: it previously swallowed a title's other
  genres, so a science fiction action film counted only as science fiction.
  Fantasy is likewise its own genre again rather than being folded into
  science fiction.
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
├── webdata.py           # dashboard payload: totals, habits, recent lists
├── webserver.py         # the dashboard's HTTP server and password gate
├── history.py           # daily snapshots in SQLite, for the trend charts
├── playlog.py           # per-sitting play log (date, time, position)
├── player.py            # xbmc.Player listener that feeds the play log
├── importer.py          # file import: parse, stage, compare, commit
├── trakt.py             # Trakt export: read the .zip, parse, match ids
├── trakt_import.py      # Trakt staging, import modes, commit
├── lists.py             # named sets of titles, yours and Trakt's
├── service.py           # background service: dashboard + daily jobs
└── resources/
    ├── icon.png         # addon icon
    ├── settings.xml     # server fallback, dashboard + website settings
    └── web/
        └── dashboard.html   # the whole dashboard, no external requests
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
