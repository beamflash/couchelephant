#!/bin/sh
# Run the app as the user in PUID and PGID, when they are set.
#
# Plex makes its library folders as its own user. CouchElephant can write into
# whatever Plex makes only when it runs as that same user, so on Unraid and
# other NAS boxes set PUID and PGID to Plex's (Unraid's usual is 99 and 100).
# Without them the container stays root, as it always was.
#
# Nothing here is allowed to stop the container from starting: a data folder
# on a network disk that refuses a chown is said so and carried on with, never
# a crash loop.

if [ -n "${PUID:-}" ] && [ "$(id -u)" = "0" ]; then
  case "$PUID${PGID:-}" in
    *[!0-9]*) echo "PUID and PGID must each be a number, such as PUID=99 PGID=100." >&2; exit 1 ;;
  esac
  PGID="${PGID:-$PUID}"

  # Our own folders only. The library belongs to Plex and is never touched.
  # Only what is not already ours: a big folder is not walked and rewritten
  # every start.
  for d in ${CE_OWNED_DIRS:-/data /recordings}; do
    [ -d "$d" ] || continue
    find "$d" \( ! -user "$PUID" -o ! -group "$PGID" \) \
      -exec chown -h "$PUID:$PGID" {} + ||
      echo "CouchElephant: could not give $d to $PUID:$PGID. Carrying on; if it cannot write there, fix that folder's owner." >&2
  done

  # Groups that let the GPU be used: root's supplementary groups are dropped
  # with the user, and /dev/dri is usually root:render 0660. Each device's own
  # group is kept, so hardware encoding still works as this user.
  groups="$PGID"
  for dev in ${CE_DRI_DIR:-/dev/dri}/*; do
    # Character devices only: /dev/dri also holds a by-path folder.
    [ -c "$dev" ] || continue
    g="$(stat -L -c %g "$dev" 2>/dev/null)" || continue
    case ",$groups," in *",$g,"*) ;; *) groups="$groups,$g" ;; esac
  done

  export HOME="${CE_HOME:-/data}"
  exec setpriv --reuid="$PUID" --regid="$PGID" --groups="$groups" "$@"
fi

exec "$@"
