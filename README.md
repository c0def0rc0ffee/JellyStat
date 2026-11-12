<p align="center"><img src="docs/screenshots/banner.png" width="100%" alt="JellyStat: watch statistics from Jellyfin, inside Kodi. Three tiles show hours watched this month, a watching calendar of daily squares and genre bars."></p>

<p align="center"><a href="#quick-start">Quick start</a> · <a href="#features">Features</a> · <a href="#dashboard-and-security">Dashboard</a> · <a href="docs/SETTINGS.md">Settings</a> · <a href="#more">Docs</a> · <a href="../../releases">Changelog</a></p>

<p align="center">
  <img src="https://img.shields.io/badge/version-0.28.9-blue" alt="Version 0.28.9">
  <img src="https://img.shields.io/badge/licence-MIT-green" alt="MIT licence">
  <img src="https://img.shields.io/badge/python-3-3776AB?logo=python&logoColor=white" alt="Python 3">
  <img src="https://img.shields.io/badge/Kodi-19%2B%20Matrix-17B2E7?logo=kodi&logoColor=white" alt="Kodi 19 Matrix or newer">
  <img src="https://img.shields.io/badge/works%20with-Jellyfin-00A4DC?logo=jellyfin&logoColor=white" alt="Works with Jellyfin">
  <img src="https://img.shields.io/badge/platform-any%20Kodi%20box-lightgrey" alt="Runs on any platform Kodi runs on">
</p>

Shows what you have watched on Jellyfin, with genres, ratings and a web dashboard, instead of reading it off the server's own pages.

<p align="center"><img src="docs/screenshots/hero-home.png" width="640" alt="The dashboard's Home page in dark mode: this month's episodes, shows, films, plays and hours, then tiles for films and episodes watched, viewing pace, streak and average rating, from an invented demo library."></p>

## Quick start

1. Install the [DeliciousCoffee repository](https://github.com/c0def0rc0ffee/DeliciousCoffee) once, then JellyStat from it, and Kodi keeps it up to date. Or download `script.jellystat-x.y.z.zip` from [Releases](../../releases) and use **Settings → Add-ons → Install from zip file** (switch on **Unknown sources** first).
2. Needs Kodi 19 (Matrix) or newer.
3. Signed in to **Jellyfin for Kodi**? JellyStat reuses that login. Otherwise enter your server URL and an API key in JellyStat's settings.
4. Run it from **Add-ons → Program add-ons → JellyStat**.

## Features

| | | |
|---|---|---|
| **Summary stats**<br>Films and episodes watched, all time and recent, with genres and average ratings. | **Web dashboard**<br>The same picture on a phone or laptop, served by the add-on itself. | **Screen time and play log**<br>Every sitting logged with its date, clock time and how much was watched. |
| **Recommendations**<br>Scored from your own history and ratings. Nothing leaves the box. | **Imports**<br>Playback Reporting, Trakt, Letterboxd or another JellyStat box, previewed before anything is written. | **Ratings sync**<br>Rate from the dashboard; the score is written to Jellyfin and read back from it. |
| **Lists and add-ons**<br>Your own lists of titles, and settings for other add-ons from the dashboard. | **Skin properties**<br>Headline figures as `JellyStat.*` window properties for any skin. | **Daily website snapshot**<br>Optional: a genre summary sent to your own site once a day. |

## Screenshots

| | |
|---|---|
| <img src="docs/screenshots/readme-discover.png" alt="Discover page: unwatched films ranked for you, with genre filter chips and year and rating filters."><br>**Discover**: suggestions from your own watching. | <img src="docs/screenshots/readme-film-library.png" alt="Film library: every film with year, community rating, your rating and play count, filterable by watched, rated, favourite and genre."><br>**Film library**: filter by watched, rated, favourite and genre. |
| <img src="docs/screenshots/readme-show-detail.png" alt="A show's page: poster, genres, resolution and codec badges, play and queue buttons, and episodes watched per season."><br>**A show's page**: badges, play on Kodi, episodes per season. | <img src="docs/screenshots/readme-phone-home.png" width="260" alt="The Home page on a phone: this month's figures above the stat tiles, with a bottom tab bar for Home, Discover, Films, TV and More."><br>**On a phone**: a bottom tab bar instead of the sidebar. |

Every screenshot comes from an invented demo library.

## Dashboard and security

Switch it on in **Settings → Web dashboard → Enable the web dashboard**, then press **Web dashboard address** to see what to type into a browser. The default port is 8099. It is built for a home network: plain HTTP, and the password is a household convenience. Never port forward it. Without a password it serves the Kodi box only.

## More

- [Web dashboard](docs/DASHBOARD.md)
- [Play log, imports and backups](docs/PLAY-LOG-AND-IMPORT.md)
- [Ratings](docs/RATINGS.md)
- [Lists and the Add-ons page](docs/LISTS-AND-ADDONS.md)
- [Window properties for skins](docs/SKINS.md)
- [Daily website snapshot](docs/WEBSITE-STATS.md)
- [Every setting](docs/SETTINGS.md)
- [How it is built](docs/ARCHITECTURE.md)

## Licence

[MIT](LICENSE)
