# Lists and the Add-ons page

Matches JellyStat 0.28.9.

## Lists

**Lists** in the sidebar holds sets of titles you chose. It is the one thing the rest of the add-on has no way to express, since a list has no property in common that a query could find.

- Make one, name it, and add anything to it from a film, show or episode page. Each carries an **Add to list** menu, which also offers a new list.
- When the Functional skin is on the same Kodi with its lists switched on for JellyStat, the same menu lists the skin's lists first, under **On the TV**, and adds the title to that list on the television.
- Clicking a list opens it as its own page, where it can be renamed, emptied entry by entry, or deleted, and which shows when it was last changed.
- An entry keeps the title, year and provider ids it was created with, and is matched to a library item where one exists. So a list can name titles this server does not hold: an imported Trakt list still names all forty films when you have twenty of them. The rest are shown as not in your library rather than dropped, and they attach themselves if those titles ever arrive. **Re-check unmatched** runs that pass on demand.
- Lists that were ordered by hand keep their order.

Trakt lists, the watchlist and the collection can be imported: see [PLAY-LOG-AND-IMPORT.md](PLAY-LOG-AND-IMPORT.md#importing-a-trakt-export).

## Add-ons

**Add-ons** in the sidebar manages other add-ons on the same Kodi that offer the shared add-on API. The page is built entirely from what each add-on says about itself, so another add-on that offers the API appears here with no change to JellyStat.

- **Engage** (1.11.0 or newer): its slots can be listed, added, edited and deleted, and backed up or restored, without picking up the remote.
- **JellyRate** (1.5.0 or newer): its settings open as one form, and its recently played titles can be re-rated.

An add-on can offer ways to sort its list, and can let some fields be changed right on a record's line. With Engage 1.11.5 or newer, its slots sort by when each runs next (the default), lists before sequences before favourites, name, or time of day, and each slot shows when it next runs under its name. Its days are a row of toggles and its time a time box on the line itself, each saved as soon as it changes. The order you pick is remembered in that browser.

Each add-on checks every change itself, exactly as its own screens in Kodi would, and an edit to something changed in Kodi meanwhile is refused rather than saved over it. The add-ons listen on the Kodi box only; everything reaches them through this dashboard, behind its password. Anyone who can open the dashboard can make these changes, so set a password if that is not everyone in the house.
