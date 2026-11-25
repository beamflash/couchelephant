#!/bin/sh
# Run the app as the user in PUID and PGID, when they are set.
#
# Plex makes its library folders as its own user. CouchElephant can write into
# whatever Plex makes only when it runs as that same user, so on Unraid and
# other NAS boxes set PUID and PGID to Plex's (Unraid's usual is 99 and 100).
# Without them the container stays root, as it always was.
set -e

if [ -n "$PUID" ] && [ "$(id -u)" = "0" ]; then
  case "$PUID$PGID" in
    *[!0-9]*) echo "PUID and PGID must each be a number, such as PUID=99 PGID=100." >&2; exit 1 ;;
  esac
  PGID="${PGID:-$PUID}"
  # Our own folders only. The library belongs to Plex and is never touched.
  for d in ${CE_OWNED_DIRS:-/data /recordings}; do
    if [ -d "$d" ]; then
      chown -R "$PUID:$PGID" "$d"
    fi
  done
  export HOME="${CE_HOME:-/data}"
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi

exec "$@"
