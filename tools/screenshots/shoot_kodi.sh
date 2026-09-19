#!/usr/bin/env bash
# <summary>
# Capture JellyStat's Kodi settings dialog, one image per category.
# </summary>
# <remarks>
# The addon installed in Kodi is usually an older release than the working
# tree, so the current source is swapped in for the run and the installed
# copy put back afterwards. The addon's database and settings are moved out
# of the way first: a newer version migrates the database it finds on the
# first launch, and that must not happen to real viewing history just to
# take a picture of a settings screen.
#
# Kodi is sent to its own Settings window before the dialog is opened. The
# dialog is slightly translucent, so whatever is behind it shows through,
# and the skin's home screen means somebody's library counts and the fanart
# of whatever they last watched.
#
# Usage: tools/screenshots/shoot_kodi.sh [output-dir]
# </remarks>

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(cd "$HERE/../.." && pwd)"
OUT="${1:-$PROJECT/docs/screenshots}"

FLATPAK_ID="tv.kodi.Kodi"
KODI_DATA="$HOME/.var/app/$FLATPAK_ID/data"
ADDON_DIR="$KODI_DATA/addons/script.jellystat"
ADDON_DATA="$KODI_DATA/userdata/addon_data/script.jellystat"
SOURCE="$PROJECT/script.jellystat"

STASH="$(mktemp -d)"
RAW="$STASH/raw"
mkdir -p "$RAW"

send() { flatpak run --command=kodi-send "$FLATPAK_ID" --action="$1" >/dev/null 2>&1; sleep "${2:-1.5}"; }
running() { pgrep -x kodi.bin >/dev/null; }

wait_until() {          # wait_until <predicate> <seconds>
  local i
  for ((i = 0; i < $2; i++)); do "$1" && return 0; sleep 1; done
  return 1
}
not_running() { ! running; }

stop_kodi() {
  running || return 0
  send "Quit" 1
  wait_until not_running 45 || { echo "Kodi would not stop" >&2; exit 1; }
}

restore() {
  echo "restoring your Kodi install"
  rm -rf "$ADDON_DIR"
  [ -d "$STASH/addon" ] && cp -a "$STASH/addon" "$ADDON_DIR"
  [ -f "$STASH/history.db" ] && { rm -f "$ADDON_DATA/history.db"; cp -a "$STASH/history.db" "$ADDON_DATA/history.db"; }
  [ -d "$STASH/backups" ] && { rm -rf "$ADDON_DATA/backups"; cp -a "$STASH/backups" "$ADDON_DATA/backups"; }
  [ -f "$STASH/settings.xml" ] && cp -a "$STASH/settings.xml" "$ADDON_DATA/settings.xml"
}
trap restore EXIT

echo "stopping Kodi"
stop_kodi

echo "stashing the installed addon and its data"
[ -d "$ADDON_DIR" ] && cp -a "$ADDON_DIR" "$STASH/addon"
[ -f "$ADDON_DATA/history.db" ] && mv "$ADDON_DATA/history.db" "$STASH/history.db"
[ -d "$ADDON_DATA/backups" ] && mv "$ADDON_DATA/backups" "$STASH/backups"
[ -f "$ADDON_DATA/settings.xml" ] && cp -a "$ADDON_DATA/settings.xml" "$STASH/settings.xml"

echo "installing $(grep -o 'version="[^"]*"' "$SOURCE/addon.xml" | sed -n 2p) for the run"
rm -rf "$ADDON_DIR"
cp -a "$SOURCE" "$ADDON_DIR"

echo "starting Kodi"
setsid nohup flatpak run "$FLATPAK_ID" >/dev/null 2>&1 < /dev/null &
wait_until running 60 || { echo "Kodi did not start" >&2; exit 1; }
sleep 20

# A plain backdrop, so nothing personal shows through the dialog.
send "ActivateWindow(Settings)" 3
send "Addon.OpenSettings(script.jellystat)" 5
send "Left" 2                       # focus the category list

names=(kodi-01-jellyfin-server kodi-02-display kodi-03-web-dashboard kodi-04-website-stats)
for i in "${!names[@]}"; do
  sleep 1
  DISPLAY="${DISPLAY:-:0}" gnome-screenshot -w -f "$RAW/${names[$i]}.png"
  echo "  captured ${names[$i]}"
  [ "$i" -lt $(( ${#names[@]} - 1 )) ] && send "Down"
done

send "Back" 2                       # cancel: nothing is written
stop_kodi

python3 "$HERE/crop_kodi.py" "$RAW" "$OUT"
echo "written to $OUT"
