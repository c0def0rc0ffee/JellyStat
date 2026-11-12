# Ratings

Matches JellyStat 0.28.9.

## Rating what you have watched

**Rate** queues up what you have watched, films and episodes separately, newest watch first. Each title gets a 1 to 10 slider with a step arrow either side and the score beside it. Titles in the queue link through to their own page. Three scopes:

- **Not rated by me** (the default): everything you have not scored yourself, whatever rating the internet has for it. Each row shows the community rating as context.
- **Rated by me**: what you have already scored. The slider starts on your current score, so changing your mind is one drag. Each row says where that score is stored: two dots, JellyStat then Jellyfin, followed by **Saved in JellyStat**, **Saved in Jellyfin**, or **Saved in JellyStat and Jellyfin**. A push that failed reads *Saved in JellyStat only, Jellyfin refused it* with the second dot red, and if the two ever hold different numbers the line shows both. The status is worked out from the two stored scores, not from how the rating got there, so the dots and the words always agree.
- **No rating anywhere**: only titles Jellyfin has no community rating for either, which on a well scraped library is a very short list.

## How a score reaches Jellyfin

The score is written to Jellyfin as itself (7 stays 7) in the per user `UserData.Rating` field. That is the same field and the same route **JellyRate** uses when it asks for a score as the credits roll, so the two add-ons agree by construction: a rating made in either turns up in the other.

- Your score is stored by JellyStat in its mirror, then sent to Jellyfin.
- Ratings made anywhere else (JellyRate, a phone, the Jellyfin web app) are read back on every mirror sync, so they appear here without any bridge between the add-ons.
- Where the two disagree the server wins, since it is the copy every device shares. The one exception is a local score whose write to Jellyfin failed: that is a pending write rather than stale data, and is left alone until it succeeds.
- A mirror sync never overwrites a rating JellyStat already holds; it only fills gaps. Before that rule existed, the sync after the first Trakt import reverted sixty imported ratings to Jellyfin's values within two minutes.
- Pushing ratings to Jellyfin keeps the dates they were originally given.
- The thumbs up or down in Jellyfin's own interface is derived by the server from the number, so JellyStat does not try to set it.
- Optionally a 9 or 10 also marks the item a favourite: add-on settings → **Web dashboard** → **Ratings** → **A 9 or 10 also marks the item a favourite on Jellyfin** (default off).
- The public **community rating** is item metadata, not per user, and writing it needs an administrator API key. If the key in the add-on's server settings is an administrator's, your score is written there too; otherwise the page says it cannot be.

A failed write never costs you the score. It is stored locally first, and the sync state is kept beside it, so the page can tell you what did and did not reach the server.

## Jellyfin agreement

JellyStat holds your score; Jellyfin keeps its own copy, and the two drift, because a Trakt import writes thousands of scores here that were never sent there. **Data → Jellyfin agreement** reports how they compare and settles it deliberately rather than silently.

- Jellyfin **missing** a rating is harmless and is only listed.
- Jellyfin holding a **different** one is a contradiction nobody can settle from the data, so those are listed with both values.
- **Push** writes JellyStat's score to Jellyfin so the two match. **Remove Jellyfin's copies** clears them there, leaving the rating only in JellyStat. Either way the end state is the same: never a contradiction, at worst a gap.

Both run in the background with progress, since thousands of ratings mean thousands of requests.
