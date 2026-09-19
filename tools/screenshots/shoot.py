# -*- coding: utf-8 -*-
"""
<summary>
Walks the dashboard's views and saves a full-page shot of each.
</summary>
<remarks>
Dark is the dashboard's own default: the light palette only comes out when
the system asks for it, so the Firefox profile pins prefers-color-scheme to
dark and the shots match what somebody opening the page actually sees.
</remarks>
"""

import json
import os
import sys
import time
import urllib.request

import marionette

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(HERE))

URL = os.environ.get("JS_DASHBOARD_URL", "http://127.0.0.1:8099/")
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    PROJECT, "docs", "screenshots")
PROFILE = os.path.join(HERE, ".ffprofile")

DESKTOP = (1500, 1200)
PHONE = (430, 1000)

VIEWS = [
    ("overview", "01-overview"),
    ("recommended", "02-discover"),
    ("movies", "03-movies"),
    ("movies-library", "04-movie-library"),
    ("tv", "05-tv"),
    ("tv-library", "06-tv-library"),
    ("rate", "07-rate"),
    ("lists", "08-lists"),
    ("data", "09-data"),
    ("settings", "10-settings"),
]

READY = "document.body.innerText.indexOf('Reading your Jellyfin') === -1"

# Firefox has no command line switch for the page's colour scheme, so the
# preference is written into a throwaway profile instead.
PREFS = '''// Written by shoot.py. 0 = dark, 1 = light.
user_pref("layout.css.prefers-color-scheme.content-override", 0);
user_pref("ui.systemUsesDarkTheme", 1);
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("datareporting.policy.dataSubmissionEnabled", false);
'''


def prepare_profile():
    """
    <summary>
    Write the Firefox profile prefs.
    </summary>
    """
    os.makedirs(PROFILE, exist_ok=True)
    with open(os.path.join(PROFILE, "user.js"), "w", encoding="utf-8") as h:
        h.write(PREFS)


def settle(ff, seconds=2.5):
    """
    <summary>
    Give charts and lazily fetched panels time to draw.
    </summary>
    """
    ff.wait_for("document.readyState === 'complete'", timeout=30)
    time.sleep(seconds)
    ff.wait_for(
        "Array.from(document.images).every(function(i){"
        "return i.complete || !i.getAttribute('src');})", timeout=25)
    time.sleep(0.8)


def payload():
    """
    <summary>
    The dashboard's overview data, fetched from the running server.
    </summary>
    <returns>The decoded JSON.</returns>
    """
    with urllib.request.urlopen(URL + "api/data", timeout=300) as response:
        return json.load(response)


def main():
    """
    <summary>
    Drive the dashboard through every view and detail page, saving a screenshot of each, desktop then mobile.
    </summary>
    """
    os.makedirs(OUT, exist_ok=True)
    prepare_profile()
    data = payload()

    ff = marionette.Firefox(PROFILE, DESKTOP[0], DESKTOP[1])
    try:
        # Firefox restores the window size from the profile, so the mobile
        # pass at the end of the last run is the width this one would open
        # at. Say the size out loud rather than trusting the command line.
        ff.set_size(*DESKTOP)
        time.sleep(1.2)
        ff.navigate(URL)
        if not ff.wait_for(READY, timeout=240):
            raise SystemExit("the dashboard never finished loading")
        settle(ff, 4)

        width = ff.script("return window.innerWidth")
        if width < DESKTOP[0] - 80:
            raise SystemExit("window came up %dpx wide, not %d"
                             % (width, DESKTOP[0]))
        if not ff.script("return window.matchMedia("
                         "'(prefers-color-scheme: dark)').matches"):
            print("  ! the browser is reporting a light theme")

        for view, name in VIEWS:
            clicked = ff.script(
                "var b = document.querySelector('[data-view=\"%s\"]');"
                "if (!b) return false; b.click(); return true;" % view)
            if not clicked:
                print("  ! no nav button for %s" % view)
                continue
            settle(ff)
            ff.screenshot(os.path.join(OUT, "%s.png" % name))
            print("  %s -> %s.png" % (view, name))

        film_detail(ff, data)
        show_detail(ff, data)
        mobile(ff)
    finally:
        ff.quit()
    print("written to %s" % OUT)


def _best_film(data):
    """
    <summary>
    The film whose page has the most on it.
    </summary>
    <remarks>
    Preference order is deliberate: a title in a well-populated genre fills
    the "similar films" strip, one watched since the play log started has
    sittings to show, and a rewatch gives the sittings table more than one
    row. A film from a corner of the library renders two empty panels.
    </remarks>
    """
    with urllib.request.urlopen(URL + "api/library?media=movie&limit=400",
                                timeout=300) as response:
        rows = json.load(response).get("items") or []
    since = (data.get("playlog") or {}).get("since") or ""
    logged = [r for r in rows
              if r.get("id") and (r.get("last_played") or "")[:10] >= since]
    pool = logged or [r for r in rows if r.get("id") and r.get("play_count")]
    if not pool:
        return None

    common = {}
    for row in rows:
        for genre in row.get("genres") or []:
            common[genre] = common.get(genre, 0) + 1

    def neighbours(row):
        """
        <summary>
        How many films share the row's most common genre.
        </summary>
        <param name="row">A film row.</param>
        <returns>An int.</returns>
        """
        return max((common.get(g, 0) for g in row.get("genres") or []),
                   default=0)

    pool.sort(key=lambda r: (neighbours(r),
                             (r.get("play_count") or 0) > 1,
                             bool(r.get("favourite")),
                             r.get("user_rating") or 0,
                             r.get("rating") or 0), reverse=True)
    return pool[0]


def film_detail(ff, data):
    """
    <summary>
    Open the richest film and screenshot its page.
    </summary>
    <param name="ff">The Firefox driver.</param>
    <param name="data">The overview payload.</param>
    """
    film = _best_film(data)
    if not film:
        print("  ! no film id to open")
        return
    ff.script("openMovie(arguments[0]);", [film["id"]])
    settle(ff, 3.5)
    ff.screenshot(os.path.join(OUT, "11-film-detail.png"))
    print("  film -> 11-film-detail.png (%s)" % film.get("name"))


def show_detail(ff, data):
    """
    <summary>
    Open the top show and screenshot its page.
    </summary>
    <param name="ff">The Firefox driver.</param>
    <param name="data">The overview payload.</param>
    """
    shows = data.get("top_shows") or []
    if not shows:
        print("  ! no show to open")
        return
    name = shows[0]["show"]
    ff.script("openShow(arguments[0]);", [name])
    settle(ff, 3.5)
    ff.screenshot(os.path.join(OUT, "12-show-detail.png"))
    print("  show -> 12-show-detail.png (%s)" % name)


def mobile(ff):
    """
    <summary>
    The page is used from a phone on the sofa as much as from a desk.
    </summary>
    """
    ff.set_size(*PHONE)
    time.sleep(1.5)
    ff.navigate(URL)
    ff.wait_for(READY, timeout=240)
    settle(ff, 4)
    ff.screenshot(os.path.join(OUT, "13-mobile-overview.png"))
    print("  mobile -> 13-mobile-overview.png")


if __name__ == "__main__":
    main()
