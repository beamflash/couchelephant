#!/bin/bash
# Runs after the package is unpacked, on first install and on upgrade.
set -e

DATA=/var/lib/couchelephant
ENVFILE=/etc/default/couchelephant

have_systemd() { [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; }

PORT_FIRST=8710
PORT_LAST=8759     # the same range the Windows and macOS installs use

# A connect that succeeds means something is already listening. A server bound
# to only the machine's own address ignores 127.0.0.1, so ask on those too.
own_addrs=$(hostname -I 2>/dev/null || true)
port_busy() {
    local addr
    for addr in 127.0.0.1 $own_addrs; do
        case "$addr" in *:*) continue ;; esac     # IPv4 only; /dev/tcp cannot take a bare v6
        if (exec 3<>"/dev/tcp/$addr/$1") 2>/dev/null; then return 0; fi
    done
    return 1
}

# Stop the old copy first so its own port does not look taken.
if have_systemd; then
    systemctl stop couchelephant 2>/dev/null || true
fi

if ! getent passwd couchelephant >/dev/null; then
    nologin=$(command -v nologin || echo /bin/false)
    useradd --system --user-group --home-dir "$DATA" --no-create-home \
        --shell "$nologin" couchelephant
fi

# Only the folders themselves are re-owned each time. The recordings folder
# may be a mount or a link to the user's own storage, so never walk into it.
new_data=0; new_rec=0
[ -d "$DATA" ] || new_data=1
[ -e "$DATA/recordings" ] || new_rec=1
mkdir -p "$DATA/recordings"
if [ "$new_data" = 1 ]; then chown -R couchelephant:couchelephant "$DATA"; fi
if [ "$new_rec" = 1 ]; then chown -R couchelephant:couchelephant "$DATA/recordings"; fi
chown couchelephant:couchelephant "$DATA" "$DATA/recordings"
# The database holds the Plex token. Nobody but the service user reads it.
chmod 0750 "$DATA"

# Keep the port from last time unless something else has taken it.
port=""
if [ -f "$ENVFILE" ]; then
    port=$(sed -n 's/^COUCHELEPHANT_PORT=//p' "$ENVFILE" | tail -n 1)
fi
if [ -z "$port" ] || port_busy "$port"; then
    port=$PORT_FIRST
    while port_busy "$port"; do
        port=$((port + 1))
        if [ "$port" -gt "$PORT_LAST" ]; then
            echo "CouchElephant: ports $PORT_FIRST to $PORT_LAST are all in use." >&2
            exit 1
        fi
    done
    # Touch only our own line; the user may have set other variables here.
    if [ -f "$ENVFILE" ] && grep -q '^COUCHELEPHANT_PORT=' "$ENVFILE"; then
        sed -i "s/^COUCHELEPHANT_PORT=.*/COUCHELEPHANT_PORT=$port/" "$ENVFILE"
    else
        echo "COUCHELEPHANT_PORT=$port" >> "$ENVFILE"
    fi
    chmod 644 "$ENVFILE"
fi

# Hardware video encoding needs these groups; they exist only where a GPU stack does.
for g in render video; do
    if getent group "$g" >/dev/null; then usermod -aG "$g" couchelephant || true; fi
done

if have_systemd; then
    systemctl daemon-reload
    systemctl enable couchelephant >/dev/null 2>&1 || true
    systemctl start couchelephant
fi

addr=$(hostname -I 2>/dev/null | cut -d' ' -f1)
echo
echo "CouchElephant is installed. Open http://${addr:-localhost}:$port/welcome to finish setup."
