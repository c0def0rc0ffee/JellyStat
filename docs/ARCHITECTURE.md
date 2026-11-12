# How it is built

Matches JellyStat 0.28.9.

## Project layout

```
script.jellystat/            the add-on itself, and all that goes in the install zip
├── addon.xml                manifest: the script, the background service, release notes
├── main.py                  entry point: Jellyfin API, the sign in order, the Kodi dialogs
├── service.py               background service: dashboard, daily jobs, skin properties
├── webserver.py             the dashboard's HTTP server and password gate
├── webdata.py               everything the dashboard shows, as one payload
├── screentime.py            headline tiles and the screen time panel
├── library.py               a full local mirror of the watched library
├── media.py                 artwork, cast and technical detail, fetched live
├── playback.py              play or queue on the Kodi box the dashboard runs on
├── player.py                Kodi player listener that feeds the play log
├── playlog.py               per sitting play log (date, time, position)
├── dates.py                 writes the play log's last watched dates to Jellyfin
├── history.py               daily snapshots in SQLite, for the over-time charts
├── importer.py              file import: parse, stage, compare, commit
├── trakt.py                 Trakt export: read the .zip, parse, match ids
├── trakt_import.py          Trakt staging, import modes, commit
├── lists.py                 named sets of titles, yours and Trakt's
├── ratings.py               your own ratings, kept in step with Jellyfin
├── reconcile.py             makes Jellyfin's ratings agree with JellyStat's
├── recommend.py             recommendations and similar titles, from your own history
├── backup.py                automatic database snapshots around anything destructive
├── addon_api.py             the dashboard's side of the shared add-on API
├── stats_sender.py          the daily website snapshot and its POST
├── timeparse.py             date parsing that survives Kodi's embedded Python
└── resources/
    ├── icon.png             add-on icon
    ├── settings.xml         server, display, dashboard and website settings
    ├── language/
    │   └── resource.language.en_gb/strings.po
    └── web/
        └── dashboard.html   the whole dashboard, no external requests
docs/                        these pages and the README images
tests/                       the test suite (python3 tests/run.py)
tools/screenshots/           demo library and harness that redraw the README images
genre-stats-spec.md          the website snapshot format
genre-stats-example.json     an example snapshot
build-zip.ps1                superseded Windows build, kept for reference only
```

The add-on has no third party Python dependencies.

## Tests

```bash
python3 tests/run.py
```

Runs every `tests/test_*.py` against stand-ins for the Kodi modules, in a temporary profile, and stops on the first failure.

## Screenshots and banner

The README images come from an invented demo library served by the real, unmodified add-on code. See [`tools/screenshots/README.md`](../tools/screenshots/README.md). The banner and social preview are drawn by `tools/screenshots/banner.py`.

## Building the install zip on Linux

Kodi needs the zip named `script.jellystat-<version>.zip`, with the `script.jellystat` folder at its root, forward slashes in every entry, and directory entries. `VERSION` and the `version` in `addon.xml` must match. Build from a clean stage, never into an existing archive:

```bash
version=$(cat VERSION)
stage=$(mktemp -d)
rsync -a --exclude '__pycache__/' --exclude '*.pyc' script.jellystat "$stage/"
(cd "$stage" && zip -qr "script.jellystat-$version.zip" script.jellystat)
unzip -l "$stage/script.jellystat-$version.zip"
```

The zip is left in the stage folder. Check the listing before installing it.

`build-zip.ps1` is superseded and nobody runs it. Never package with PowerShell's `Compress-Archive`: it writes backslashes that Kodi on Linux refuses.
