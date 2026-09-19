# Screenshot harness

Regenerates every image in `docs/screenshots/`. Two independent halves: the
web dashboard, driven headlessly, and the Kodi settings dialog, which needs
Kodi itself.

Nothing here ships in the addon zip; it exists so the README's pictures can
be rebuilt when the interface changes, rather than being hand-taken once and
quietly going stale.

## The demo library

The screenshots must not contain anybody's viewing history, because the
README is public. So the dashboard is pointed at an invented library:
90 films and 16 shows in `catalogue.py`, all made up, watched on a timeline
`generate.py` builds from a fixed seed.

The timeline is deliberately shaped rather than uniformly random, viewing
runs in evenings and weekend afternoons, television is watched in runs of
consecutive episodes, two stretches are fallow, the last fortnight is a
binge, four shows were never started and six films never watched. Flat
randomness produces a calendar that looks generated.

The seed is fixed, so a rerun on the same day reproduces the same images.
The timeline is anchored to *today*, so dates move with the calendar.

## The web dashboard

`run.py` runs the real, unmodified addon:

- `stubs/` stands in for the four Kodi modules the addon imports.
- `fake_jellyfin.py` answers the Jellyfin endpoints the addon actually calls,
  posters and cast portraits included, out of the demo library.

Everything between the two is the shipping code: the same fetch, the same
mirror sync, the same `webdata.build()`. That is the point: the screenshots
show what the addon does, not what a mock says it does.

```bash
# terminal one: serve the demo dashboard
python3 tools/screenshots/run.py

# terminal two: drive Firefox over it
python3 tools/screenshots/shoot.py
```

`run.py` writes a throwaway profile to `tools/screenshots/.profile`,
rebuilt each run. Ports default to 8096 and 8099; set `JS_JELLYFIN_PORT`
and `JS_WEB_PORT` if a real Kodi on the same box already has 8099.

`shoot.py` drives Firefox through its own Marionette protocol
(`marionette.py`), so there is no Selenium or Playwright to install. It
pins the window to 1500px and forces `prefers-color-scheme: dark`, because
the dashboard is dark unless the system asks otherwise.

## The Kodi settings dialog

```bash
tools/screenshots/shoot_kodi.sh
```

This one touches a real Kodi install, so read what it does first.

The version installed in Kodi is usually older than the working tree, so
the script swaps the current source in for the run and puts the installed
copy back afterwards. Before it does, it **moves the addon's database and
settings out of the way**: a newer version migrates the database it finds on
first launch, and that must not happen to real viewing history just to
photograph a settings screen. Everything is restored on exit, including
after a failure.

Kodi is sent to its own Settings window before the dialog opens. The dialog
is slightly translucent, so whatever sits behind it shows through, and the
skin's home screen means library counts and the fanart of whatever was last
played. `crop_kodi.py` then trims each capture to the dialog itself.

Requires the Kodi flatpak (`tv.kodi.Kodi`), `gnome-screenshot`, and an X
display. The dialog is closed with Cancel, so no setting is ever written.

## Dependencies

Python 3, Pillow, and Firefox. No pip install needed beyond Pillow.
