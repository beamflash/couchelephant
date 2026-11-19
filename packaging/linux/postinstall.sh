#!/bin/bash
# Runs after the package is unpacked, on first install and on upgrade.
set -e

DATA=/var/lib/couchelephant
ENVFILE=/etc/default/couchelephant

have_systemd() { [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; }

# A connect that succeeds means something is already listening.
port_busy() {
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
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

mkdir -p "$DATA/recordings"
chown -R couchelephant:couchelephant "$DATA"

# Keep the port from last time unless something else has taken it.
port=""
if [ -f "$ENVFILE" ]; then
    port=$(sed -n 's/^COUCHELEPHANT_PORT=//p' "$ENVFILE" | tail -n 1)
fi
if [ -z "$port" ] || port_busy "$port"; then
    port=8710
    while port_busy "$port"; do
        port=$((port + 1))
    done
    echo "COUCHELEPHANT_PORT=$port" > "$ENVFILE"
    chmod 644 "$ENVFILE"
fi

if have_systemd; then
    systemctl daemon-reload
    systemctl enable couchelephant >/dev/null 2>&1 || true
    systemctl start couchelephant
fi

addr=$(hostname -I 2>/dev/null | cut -d' ' -f1)
echo
echo "CouchElephant is installed. Open http://${addr:-localhost}:$port/welcome to finish setup."
