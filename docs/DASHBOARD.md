# Web dashboard

The add-on can serve its own dashboard over HTTP, so the stats can be read from any device on your network without installing anything else. It runs inside the add-on's background service, which starts with Kodi, and stays off until you switch it on.

Matches JellyStat 0.28.9.

## Switching it on

1. Add-on settings → **Web dashboard** → **Enable the web dashboard**.
2. Press **Web dashboard address** just below it. A box shows the addresses to type into a browser, worked out by the box itself, so there is no need to look up its IP. The same dialog is in JellyStat's run menu.
3. The default port is **8099**, so the address looks like `http://your-kodi-box:8099/`.

The service picks up a settings change within five seconds. Every dashboard setting is listed in [SETTINGS.md](SETTINGS.md).

## Security

This is designed for a home network. It speaks plain HTTP with no certificate, and the password is a convenience for a shared household, not protection against the open internet. Do not port forward it.

- With **Reachable from other machines** on and no password set, the dashboard refuses to open itself to the network and serves the Kodi box only, and says so on screen.
- With a password set, the page asks for it and the JSON endpoints require it too. Signing in issues a session that lasts twelve hours; restarting Kodi signs every browser out.
- A script can send an `X-Auth-Token` header instead of signing in. The token is derived from the password (PBKDF2 with SHA-256), not the password itself.
- Requests from another site, or without the password, get a clear refusal.

## Layout

A sidebar on a desktop, an icon rail on a tablet, and on a phone a bottom tab bar (Home, Discover, Films, TV and More) with filters folded behind a button. It follows the browser's light or dark setting. It is plain HTML with no external requests, so it works on a network with no internet access.

The sidebar holds **Home**, **Discover**, the library (**Films**, **TV**, **Rate**, **Lists**), then **Add-ons**, **Data** and **Settings**. A **search box** in the header finds any watched film or show and opens its page.

## Home

At the top, **At a glance** counts episodes, shows, films, plays, hours watched and ratings given, with a **This month** or **All time** switch. Hours are estimated from each title's runtime times how often it was played, so they cover the whole library, including everything watched before the add-on existed.

Below it, the panels are grouped into four tabs, **Summary**, **Habits**, **Taste** and **History**, so one set shows at a time. The page remembers the tab you last used. Each panel's explanation shows as one line until tapped.

- **Cards**: films and episodes watched, all time and in the recent window, total plays, watch pace, current and longest daily streak, and average rating.
- **Screen time** is measured, not estimated: it comes from the play log's real sittings, so it covers only the period the log reaches back to. Daily bars for the last week, the daily average, the split between morning, afternoon, evening and night, totals, the share of waking hours, and films against shows, each compared with the previous week.
- **Watching calendar**: a year of daily squares, purple where films dominated the day and green where episodes did, darker on quieter days.
- **Time of day** and **day of the week**: the two charts that describe a habit rather than a library, counted from the play log's real sittings once it has any.
- **Genres**: switchable between films and TV, the recent window and all time, and three ways of counting: by title, weighted by plays (so rewatches pull their genre up), or first listed genre only (so the shares add up to 100%). A title with several genres counts towards each, so by title the shares can add up to more than 100%.
- **Recent days**, **Ratings you watch**, **Top shows** and **Recently watched**.
- **Over time**: newly watched titles per day and how the genre mix has moved, drawn from the daily history below.
- **Rewatches**: titles with more than one logged sitting, with the first and latest dates.

Once the play log has sittings, the calendar, time of day and day of the week charts count **real plays** from the log's first date onwards, so three episodes in an evening count as three, and the calendar marks the changeover. Days before that, and titles played on other devices with no logged sitting, still count once from Jellyfin's last played date, so nothing disappears. See [PLAY-LOG-AND-IMPORT.md](PLAY-LOG-AND-IMPORT.md).

## Now playing

A **Now playing** popup appears whenever the Kodi box is playing something, with the poster, a progress bar, how long is left and the clock time it will finish. It polls every fifteen seconds and can be dismissed until the next thing starts. Its corner is chosen on the dashboard's Settings page.

## Discover

Suggests films and shows in separate sections, scored entirely from your own watching and ratings: the community rating, how you rate that sort of title compared with your own average, and how much of your viewing falls in its genres. The genre match is cosine normalised, so a title is not promoted merely for carrying a long list of tags. Genres you have rated little are trusted less, so the picture sharpens as you rate more. Nothing leaves the box and no external service is consulted.

- Each section has a **genre filter**: tick any number of genres and a title must carry every one ticked. Each chip shows how many titles would be left if you added it, so the filter never leads to a dead end.
- **Back** and **Forward** page through the whole ranked list, with a running count such as "Showing 21 to 40 of 6,170". Paging returns to the top of the new page.
- Filtering narrows the list without re-ranking it, so picking Horror answers "the horror you would most likely enjoy" rather than "your favourites that happen to be horror".
- **How these are chosen** shows the weights actually used.
- Clicking any suggestion opens its page, watched or not.

## Films and TV

Each has a **Library** and **Stats** switch.

- **Library**: every title, filterable by watched, rated by you, favourite, genre, year and minimum rating, and sortable.
- **Stats**: totals, plays, average rating, films by decade, most replayed, best rated, and most watched shows.

## Film and show pages

- **Artwork**: posters, episode stills and cast photographs, all proxied through the add-on, so the browser never sees a Jellyfin token and a phone that can reach the dashboard sees the pictures even when it cannot reach Jellyfin itself.
- The **overview**, and badges for **resolution** (4K, 1080p, 720p, 576p, 480p, judged on width so a scope film is not mislabelled), codec, container, file size, runtime and certificate.
- A film shows ratings, play count and logged sittings; a show shows episodes watched per season, the watched episode list, and its sittings.
- **Cast and crew** as a strip of faces. Click anyone for their own page: every title of theirs on your server, split into watched and not watched.
- A show lists its **episodes as cards** with the still, the description and that episode's own resolution badges, ticked where watched.
- **Play** or **Queue** on the Kodi box the dashboard is served from, so a phone works as a remote. A show's buttons pick its next unwatched episode. Queueing while nothing is playing starts the item rather than leaving it in a playlist. Titles are resolved through Kodi's own library, so resume points, watched marking and the player's artwork behave as if you had started it from the television.
- **Dates** opens the list of when you actually watched it: the date and clock time of each sitting, how long you watched, and where the play came from. Titles played before the play log existed say so.
- **Add to list**: see [LISTS-AND-ADDONS.md](LISTS-AND-ADDONS.md).
- **Similar films** or **Similar shows** at the foot of the page: shared genres, with a nudge for a similar era and a better rating.

## The dashboard's own Settings page

These are stored in the browser, so each device can have its own.

- **Date format**: ISO, day/month, month/day, or "20 Aug 2026".
- **Clock**: 24 hour or am/pm.
- **Now playing popup**: which corner or edge it appears in.
- **Recommendations**: what Discover and Similar may suggest, set separately for films and TV: **Only ones I have not seen**, or **Seen and new**.

## Your own copy of the library

Every dashboard load also folds the fetched library into a local mirror (`items` in `history.db`): one row per watched film and episode, inserted or updated only when something changed.

- Items deleted from Jellyfin stay in the mirror.
- If Jellyfin is unreachable the dashboard serves from the mirror ("as of the last sync") instead of failing.
- When a mirrored item's last played date moves forward, that play is recorded in the play log with Jellyfin's own timestamp, on any device, phone and web included. Plays on this Kodi box are not counted twice; the player logger recorded those first hand.
- The **Refresh data at most every (minutes)** setting caches a library read, since reading a whole library takes a few seconds. The **Refresh** button on the page bypasses the cache.

## History for the over time charts

Jellyfin keeps only the most recent play date for each item, so it cannot answer "what did my viewing look like in March". The add-on records one snapshot a day into `history.db` in its own add-on data folder (roughly 10 KB a day, kept for three years), and the **Over time** charts are drawn from that. It starts filling in from the day you enable it, so those charts are thin at first.

- The service takes the snapshot at the same hour as the website send (**Website stats → Schedule → Send at**, default 18:00), whether or not a website endpoint is set.
- Opening the dashboard also records the day if it is still missing, so a box that is only on in the morning still builds a history.
- Either way it is one row per day, and a send's payload is reused rather than reading the library twice.
- Turn it off with **Web dashboard → History → Keep a daily snapshot for the over-time charts**.
