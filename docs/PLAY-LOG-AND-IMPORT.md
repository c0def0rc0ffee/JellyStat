# Play log, imports and backups

Matches JellyStat 0.28.9.

## The play log: every sitting, with its date and time

Jellyfin's API cannot say when you watched something beyond each item's single most recent play date, so a film watched three times reports one date. JellyStat therefore keeps its own log.

- A background listener notes every film or episode played on this Kodi box as a sitting, with its real **start and end clock time** and how much was actually watched (the position reached, so a long pause does not count, and Kodi closing mid film still logs the sitting).
- Resuming a film is credited correctly: the resume point is not counted as watched time.
- Sittings shorter than a threshold (default 2 minutes) are ignored as mis-clicks.
- Settings: **Web dashboard → History → Log each play with its date and time** (default on) and **Ignore sessions shorter than (minutes)**.
- Plays on other devices are picked up from the server through the library mirror (see [DASHBOARD.md](DASHBOARD.md)). History from other devices or other tools before that comes in through the importers below.

How the log changes the charts is described in [DASHBOARD.md](DASHBOARD.md#home).

## Last watched dates

A title's last watched date comes from the play log first: the newest completed sitting recorded here, imported, or detected from a Jellyfin date change. Jellyfin's own date is used only for a title with no logged sitting.

- **Data → Jellyfin dates**: when the server re-marks hundreds of titles on one day, change detection would copy every one of those dates in as a sitting. This panel finds such days and lets you set them aside. Nothing is deleted, and a day can be restored.
- **Data → Jellyfin follows these dates**: every title whose logged date differs from Jellyfin's has its last played date written across in the background, after an import or a day being set aside and on the service's regular check. Jellyfin for Kodi then carries the date into Kodi, so a skin's last watched label agrees with the dashboard. Only the date is written; play counts, the watched flag and resume positions are left alone. The toggle is **Web dashboard → History → Write last watched dates from the play log to Jellyfin** (default on).

## Importing watch history from a file

**Data → Import watch history** backfills the play log from a file. Nothing is written on upload: the file is parsed and **staged**, and you see what it contains (plays, date range, devices, rows without clock times) and how it compares with what is already logged (duplicates, new plays, overlapping days). Only then do you choose how to apply it.

- **Merge** adds only what is not already logged (matched on title and day). Existing rows are never touched.
- **Replace** lets this file win for its date range. You pick what it may delete first: previously imported sittings in that range (the default), and optionally this box's own logged sittings. A backup is taken first. Deletion is permanent and the page says so before it happens.

Every import is stored as a batch and listed under the importer with a **Remove** button, so a wrong file is one click to take back out. A replace mode deletion, however, cannot be restored from the batch.

Recognised formats, detected from the content rather than the extension:

| File | What it gives you |
|---|---|
| Jellyfin **Playback Reporting** plugin backup (JSON) or TSV export | The best source: every play on every device, with clock times and device names, years of backdated history at once |
| Another box's JellyStat `history.db` | Merge a second Kodi machine into one picture |
| **Trakt** history CSV | Full timestamps from before you ran Jellyfin |
| **Letterboxd** diary CSV | Films with dates only, kept out of the time of day chart rather than inventing an hour |
| Generic CSV or JSON | Columns `started_at`, `media`, `title`, plus optional `show`, `season`, `episode`, `year`, `watched_seconds`, `device` |

Uploads are capped at 50 MB, and with a dashboard password set the import endpoints require it like everything else.

## Importing a Trakt export

**Data → Import from a Trakt export file** takes the export .zip exactly as Trakt sent it. The archive is unpacked by the add-on and its files are classified by shape rather than by file name, so there is nothing to extract first. If you have already unzipped it, select the .json files instead and they are read in the browser the same way. Either route gives the same import.

Watch history, ratings and your Trakt lists are read. The rest of the export is ignored, and the staging screen says how many files that was.

Nothing is written until you have seen what was found. The staging screen reports, per category, how many records there are, how many matched your library, and how many would be overwritten and with what. Then you tick the categories to import and pick a mode.

- **Matching** is by IMDb, TMDb and TVDb id first, and by title only as a fallback. Anything the mirror cannot place is looked up on the Jellyfin server itself, because the mirror holds only items that have been played.
- **Records that match nothing are still imported.** The play happened; this library simply has no copy of it.
- **Durations are assumed.** Trakt records that something was played, never for how long, so an imported sitting is credited with the item's runtime and flagged. Screen time reports measured and assumed time separately.
- **Times are stored in your own timezone**, so evening viewing lands on the right hour and day.
- **Where a rating exists in both places and they disagree, Trakt wins.** Imported ratings land in JellyStat, which is the record of what you think of a title; Jellyfin is expected to match it. See [RATINGS.md](RATINGS.md).
- **Lists come across too.** Custom lists, the watchlist and the collection, each named, and flagged where the name matches a list you already have. What happens to them is part of the mode you pick: leave existing lists alone, add what they are missing, or (only under "Everything, including repeat plays") replace their contents. Only the last deletes anything, and its card counts what it would delete before you press it.
- Importing Trakt history corrects the last watched date of every title it matches.
- A **backup is taken automatically immediately before and immediately after** every import.

## Backup and restore

- **Data → Automatic backups** lists the snapshots taken either side of every import, newest first, each downloadable. The most recent twelve are kept and older ones are dropped. **Back up now** takes one on demand.
- Snapshots use SQLite's own backup API, so one taken while the service is mid write is still a consistent database rather than a torn file. A backup that fails part way leaves no truncated file behind.
- **Data → Backup → Download backup** exports the whole database (mirror, snapshots, sitting log) as one `.db` file, served as a point in time snapshot so a write during the download cannot corrupt it.
- Restoring is importing that file on the same page, through **Import watch history**.
