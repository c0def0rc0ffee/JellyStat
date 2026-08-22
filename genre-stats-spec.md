# Genre-stats send: wire spec (v1)

What the JellyStat Kodi addon POSTs to the website. One request per calendar
day, always the full current snapshot. The site overwrites, nothing
incremental.

The addon decides when that request goes out (by default the first
opportunity at or after 18:00 local, configurable to a different hour, to
certain days of the week, or back to the old "as soon as Kodi loads"
behaviour; hourly retry on failure). The site should therefore treat
`generated_at` as the only reliable timestamp and never infer viewing times
from when a request arrives. A day with no request is normal and means
nothing was sent, not that nothing was watched.

## Request

```
POST /api/genre-stats.php HTTP/1.1
Content-Type: application/json
Accept: application/json
X-Auth-Token: <token>        (omitted entirely if no token is configured)
```

Body: one JSON object, UTF-8, no envelope.

## Response contract

Success is exactly: HTTP `200` with body `{"ok":true}`.
Anything else (other status, `ok` missing/false, non-JSON body) is treated as
failure. The addon logs it and retries at its next hourly check.

## Body fields

| Field | Type | Meaning |
|---|---|---|
| `version` | int | Always `1`. Bumped only on a breaking change. |
| `generated_at` | string | ISO 8601 UTC, `YYYY-MM-DDTHH:MM:SSZ`, when the snapshot was computed. |
| `source` | string | Always `"main-account"`, meaning the Jellyfin user the Kodi box is signed in as. |
| `movies` | object | Movie stats: `{last_30_days, all_time}` windows (see below). |
| `tv` | object | Episode stats, same shape. Items are **individual episodes**; genres come from the episode's **show** (episodes carry no genres of their own). Episodes of a show with no genre count as `Unknown`. |
| `windows` | object | **Deprecated.** Exact duplicate of `movies`, the original movies-only key, kept so earlier site code keeps working. New code should read `movies`/`tv`. |

Window semantics (both media types): `last_30_days` is a rolling 30×24h
window ending at `generated_at`, based on each item's last-played date
(approximate: Jellyfin keeps only the most recent play per item, so a
rewatch pulls an old item into the window); `all_time` is everything ever
marked watched.

Each window object:

| Field | Type | Meaning |
|---|---|---|
| `total` | int | Distinct watched items (movies or episodes) in the window. |
| `per_day` | number | Watch pace: `total ÷ window length in days`, 2 decimals. For `last_30_days` the length is the fixed 30 days; for `all_time` it spans the earliest last-played date to `generated_at`, floored at 1 day. `0.0` when the window is empty. |
| `avg_gap_minutes` | int or null | Average minutes between watches: first-to-last played span in minutes ÷ `total` ("a movie every N minutes"), rounded to whole minutes. `null` when fewer than 2 dated items or the span is zero. |
| `plays` | int | Total plays in the window including rewatches (sum of each item's lifetime play count, floored at 1 per item). |
| `genres` | array | One entry per genre, sorted by `count` descending, ties alphabetical by `genre`. |
| `genres[].genre` | string | Final display name. Aliases already merged producer-side (Sci-Fi/SciFi/Science-Fiction → `Science Fiction`; **Fantasy and "Sci-Fi & Fantasy" also fold into `Science Fiction`**; Animated → `Animation`; Kids/Children → `Family`); case-insensitive duplicates merged. Items with no genre appear under `Unknown`. A `Fantasy` entry will never appear. |
| `genres[].count` | int | Distinct items in the window carrying that genre. |
| `genres[].percent` | number | `100 × count / total`, rounded to 1 decimal. |
| `genres_by_plays` | array | Same genres, weighted by each item's play count, so **rewatches count**. Entries are `{genre, plays, percent}` where `percent` is of the window's `plays` total. Multi-genre items still count towards each genre, so percentages can sum past 100. Sorted by `plays` desc. |
| `genres_primary` | array | Each item counts **once, to its first-listed genre only** (aliased). Entries are `{genre, count, percent}` and percentages **sum to ~100** (rounding aside). The "what is this movie mainly?" view. Sorted by `count` desc. |

Rules the site must NOT re-derive:

- **Watched** = Jellyfin's Played flag (~90% completion by default).
- **Multi-genre items count once per every genre they have**, so
  `sum(counts) ≥ total` and percentages can sum past 100. `total` is always
  distinct items, never a sum of the genre list. Use `genres_primary` for a
  view that adds up to 100.
- **Changed in addon v0.15.0:** a "sci-fi dominance" rule used to make any
  item whose aliased genres included Science Fiction count **only** as
  Science Fiction. It is gone: such items now count towards each of their
  genres like everything else. Sites holding history from both sides of
  that change will see Science Fiction fall and Action, Adventure and
  Horror rise at the changeover, with no change to `total`.
- **Changed in addon v0.15.0:** Fantasy is no longer aliased to Science
  Fiction and appears as its own genre. TMDb's combined "Sci-Fi & Fantasy"
  TV genre, which cannot be split, still maps to Science Fiction.
- A window with nothing watched sends
  `"total": 0, "per_day": 0.0, "avg_gap_minutes": null, "genres": []`.
- TV counts are per **episode**, not per show; a 10-episode binge of a Drama
  show adds 10 to Drama in the `tv` block.
- All-time pace figures inherit the last-played-date approximation:
  rewatches erase an item's earlier date, so the all-time span (and
  therefore `per_day` / `avg_gap_minutes`) shifts as old items are
  rewatched. Treat them as indicative, not exact.
- Play counts are **lifetime** per item (Jellyfin has no per-window play
  history without the Playback Reporting plugin). In `last_30_days`,
  `plays`/`genres_by_plays` therefore mean "lifetime plays of the items
  active in this window", indicative rather than an exact 30-day play tally.
- `genres_primary` trusts the metadata provider's genre ordering (TMDb lists
  the dominant genre first for most titles, but it is not guaranteed).

## Example body

```json
{
  "version": 1,
  "generated_at": "2026-07-10T21:05:00Z",
  "source": "main-account",
  "windows": { "…exact duplicate of movies…": {} },
  "movies": {
    "last_30_days": {
      "total": 2,
      "genres": [
        { "genre": "Science Fiction", "count": 2, "percent": 100.0 },
        { "genre": "Action", "count": 1, "percent": 50.0 }
      ]
    },
    "all_time": {
      "total": 5,
      "genres": [
        { "genre": "Comedy", "count": 2, "percent": 40.0 },
        { "genre": "Science Fiction", "count": 2, "percent": 40.0 },
        { "genre": "Action", "count": 1, "percent": 20.0 },
        { "genre": "Drama", "count": 1, "percent": 20.0 },
        { "genre": "Unknown", "count": 1, "percent": 20.0 }
      ]
    }
  },
  "tv": {
    "last_30_days": {
      "total": 8,
      "genres": [
        { "genre": "Drama", "count": 6, "percent": 75.0 },
        { "genre": "Comedy", "count": 2, "percent": 25.0 }
      ]
    },
    "all_time": {
      "total": 340,
      "genres": [
        { "genre": "Drama", "count": 180, "percent": 52.9 },
        { "genre": "Comedy", "count": 120, "percent": 35.3 },
        { "genre": "Science Fiction", "count": 40, "percent": 11.8 }
      ]
    }
  }
}
```

As a single wire string (movies block duplicated into `windows` omitted here
for readability, on the wire it is present in full):

```
{"version": 1, "generated_at": "2026-07-10T21:05:00Z", "source": "main-account", "windows": {...}, "movies": {"last_30_days": {"total": 2, "genres": [{"genre": "Science Fiction", "count": 2, "percent": 100.0}, {"genre": "Action", "count": 1, "percent": 50.0}]}, "all_time": {"total": 5, "genres": [{"genre": "Comedy", "count": 2, "percent": 40.0}, {"genre": "Science Fiction", "count": 2, "percent": 40.0}, {"genre": "Action", "count": 1, "percent": 20.0}, {"genre": "Drama", "count": 1, "percent": 20.0}, {"genre": "Unknown", "count": 1, "percent": 20.0}]}}, "tv": {"last_30_days": {"total": 8, "genres": [{"genre": "Drama", "count": 6, "percent": 75.0}, {"genre": "Comedy", "count": 2, "percent": 25.0}]}, "all_time": {"total": 340, "genres": [{"genre": "Drama", "count": 180, "percent": 52.9}, {"genre": "Comedy", "count": 120, "percent": 35.3}, {"genre": "Science Fiction", "count": 40, "percent": 11.8}]}}}
```

## Reference test command

```
curl -X POST http://personalsite/api/genre-stats.php \
  -H "Content-Type: application/json" \
  -H "X-Auth-Token: <token>" \
  --data @genre-stats.json
```
