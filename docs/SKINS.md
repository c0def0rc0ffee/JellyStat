# Window properties for skins

Matches JellyStat 0.28.9. Available from 0.26.4; the seven day figures from 0.26.5.

The background service publishes the dashboard's headline figures as properties on Kodi's Home window, refreshed every five minutes, so a skin can show them anywhere with:

```
$INFO[Window(home).Property(JellyStat.<name>)]
```

Nothing else is needed: no database access and no web dashboard. On a box that has not yet read its library the properties are cleared rather than published as zeroes, so a skin can gate on one being non-empty. The names are part of the add-on's public surface: new ones are added, existing ones are never renamed.

| Property | Value |
|---|---|
| `JellyStat.Month` | Name of the current month |
| `JellyStat.Month.Hours`, `JellyStat.AllTime.Hours` | Estimated viewing, `12h 30m` below a hundred hours, then `1,234h` |
| `JellyStat.Month.Minutes`, `JellyStat.AllTime.Minutes` | The same as a plain minute count |
| `JellyStat.Month.Titles`, `JellyStat.AllTime.Titles` | Films plus episodes watched |
| `JellyStat.Month.Movies`, `JellyStat.Month.Episodes`, `JellyStat.Month.Plays` | This month's breakdown |
| `JellyStat.AllTime.Movies`, `JellyStat.AllTime.Episodes`, `JellyStat.AllTime.Shows`, `JellyStat.AllTime.Plays`, `JellyStat.AllTime.Ratings` | All time breakdown |
| `JellyStat.Week.Hours`, `JellyStat.Week.Delta` | Measured viewing over the last seven days, and the change against the seven before (`+2h 10m`, `-31m`) |
| `JellyStat.Week.Average`, `JellyStat.Week.WakingPercent` | Daily average, and that as a percentage of a sixteen hour waking day |
| `JellyStat.Week.Movies.Hours`, `JellyStat.Week.Shows.Hours` | The week split by kind |
| `JellyStat.Week.Measured` | `1` once the play log holds a real sitting in either week, otherwise empty |
| `JellyStat.Day.1.Label` to `JellyStat.Day.7.Label` | Weekday names, oldest first, today last |
| `JellyStat.Day.N.Hours`, `JellyStat.Day.N.Minutes`, `JellyStat.Day.N.Percent`, `JellyStat.Day.N.Today` | That day's viewing, its share of the busiest day (0 to 100, for drawing bars) and `1` on today's entry |
| `JellyStat.Daypart.1.Label` to `JellyStat.Daypart.4.Label`, `JellyStat.Daypart.N.Sessions` | Morning, Afternoon, Evening and Night, and the sittings started in each over the seven days |
| `JellyStat.Updated` | Clock time of the last refresh |

The month and all time hours are the same estimate the dashboard uses: each title's runtime times how often it was played, over the whole library. The seven day figures are the play log's measured sittings, so on a box whose log is young they cover only the days it has.

The Functional skin uses these for a Watching block on its home screen and for its Stats page.
