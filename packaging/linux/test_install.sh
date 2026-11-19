#!/bin/bash
# Install the packages in real distro containers and check they work.
#   test_install.sh <dir-with-packages-of-two-versions> <old-version> <new-version> [amd64|arm64]
# Containers have no systemd, so the unit's ExecStart is run directly.
# install.sh is pointed at a local fake release feed.
set -euo pipefail

# CE_TEST_ONLY runs a single image, for debugging.
if [ -n "${CE_TEST_ONLY:-}" ]; then DISTROS=("$CE_TEST_ONLY"); else DISTROS=(debian:12 ubuntu:22.04 fedora:40 rockylinux:9); fi

if [ "${1:-}" = "--inside" ]; then
    inside=1
else
    inside=0
fi

# ---------------------------------------------------------------- inside
if [ "$inside" = 1 ]; then
    shift
    distro=$1 feed_old=$2 feed_new=$3 format=${4:-}
    export COUCHELEPHANT_RELEASES_URL
    [ -n "$format" ] && export COUCHELEPHANT_FORMAT=$format

    fail() { echo "FAIL [$distro $format]: $*" >&2; exit 1; }

    start_service() {
        local exec_start
        exec_start=$(cat /usr/lib/systemd/system/couchelephant.service \
            /etc/systemd/system/couchelephant.service 2>/dev/null \
            | sed -n 's/^ExecStart=//p' | head -n 1 || true)
        [ -n "$exec_start" ] || fail "no ExecStart in the unit"
        set -a
        # shellcheck disable=SC1091
        . /etc/default/couchelephant
        set +a
        # shellcheck disable=SC2086
        COUCHELEPHANT_DATA=/var/lib/couchelephant runuser -u couchelephant -- \
            env COUCHELEPHANT_DATA=/var/lib/couchelephant \
            COUCHELEPHANT_PORT="$COUCHELEPHANT_PORT" $exec_start \
            >/tmp/ce.log 2>&1 &
        pid=$!
        for _ in $(seq 60); do
            curl -fs "localhost:$COUCHELEPHANT_PORT/api/tv/hello" >/dev/null && return 0
            sleep 1
        done
        cat /tmp/ce.log >&2
        fail "no answer on port $COUCHELEPHANT_PORT"
    }

    stop_service() {
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
        sleep 1
    }

    # Tools the test itself needs.
    if command -v apt-get >/dev/null; then
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq >/dev/null
        apt-get install -y -qq curl ca-certificates python3 sqlite3 >/dev/null
    else
        dnf install -y -q python3 sqlite util-linux hostname >/dev/null
    fi

    remove_package() {
        if command -v dpkg >/dev/null && dpkg -s couchelephant >/dev/null 2>&1; then
            apt-get purge -y -qq couchelephant >/dev/null
        elif rpm -q couchelephant >/dev/null 2>&1; then
            rpm -e couchelephant
        fi
        rm -rf /opt/couchelephant /etc/systemd/system/couchelephant.service
    }

    # 1. Fresh install on the default port.
    COUCHELEPHANT_RELEASES_URL=$feed_old sh /test/install.sh >/tmp/install1.log 2>&1 \
        || { cat /tmp/install1.log; fail "install.sh failed"; }
    grep -q "^COUCHELEPHANT_PORT=8710$" /etc/default/couchelephant || fail "port is not 8710"
    [ "$(stat -c %U /var/lib/couchelephant/recordings)" = couchelephant ] \
        || fail "recordings folder is not owned by the service user"
    [ "$(stat -c %a /var/lib/couchelephant)" = 750 ] \
        || fail "data folder mode is $(stat -c %a /var/lib/couchelephant), not 750"
    for line in UMask=0027 NoNewPrivileges=yes ProtectSystem=full; do
        cat /usr/lib/systemd/system/couchelephant.service /etc/systemd/system/couchelephant.service \
            2>/dev/null | grep -q "^$line$" || fail "unit lacks $line"
    done
    for g in render video; do
        if getent group "$g" >/dev/null; then
            id -nG couchelephant | tr ' ' '\n' | grep -qx "$g" || fail "service user is not in group $g"
        fi
    done
    start_service
    curl -fs localhost:8710/api/tv/hello >/dev/null || fail "no hello on 8710"

    # 2. Upgrade over a version that wrote a setting.
    sqlite3 /var/lib/couchelephant/couchelephant.db \
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('installer_test', 'kept')"
    stop_service
    # A root-owned file under recordings must keep its owner over the upgrade.
    touch /var/lib/couchelephant/recordings/keep.ts
    COUCHELEPHANT_RELEASES_URL=$feed_new sh /test/install.sh >/tmp/install2.log 2>&1 \
        || { cat /tmp/install2.log; fail "upgrade failed"; }
    got=$(sqlite3 /var/lib/couchelephant/couchelephant.db \
        "SELECT value FROM settings WHERE key = 'installer_test'")
    [ "$got" = kept ] || fail "setting lost over the upgrade (got '$got')"
    grep -q "^COUCHELEPHANT_PORT=8710$" /etc/default/couchelephant || fail "port changed on upgrade"
    [ "$(stat -c %U /var/lib/couchelephant/recordings/keep.ts)" = root ] \
        || fail "upgrade re-owned a file inside recordings"
    start_service
    stop_service

    # 3. 8710 and 8711 taken: the service must land on 8712.
    remove_package
    echo "COUCHELEPHANT_RECORDINGS=/srv/rec" >> /etc/default/couchelephant
    python3 -m http.server 8710 >/dev/null 2>&1 &
    blocker=$!
    # 8711 held on the machine's own address only: a probe of 127.0.0.1 alone misses it.
    own=$(hostname -I | cut -d' ' -f1)
    python3 -m http.server 8711 --bind "$own" >/dev/null 2>&1 &
    blocker2=$!
    sleep 1
    COUCHELEPHANT_RELEASES_URL=$feed_old sh /test/install.sh >/tmp/install3.log 2>&1 \
        || { cat /tmp/install3.log; fail "install with port taken failed"; }
    grep -q "^COUCHELEPHANT_PORT=8712$" /etc/default/couchelephant \
        || fail "port is not 8712: $(cat /etc/default/couchelephant)"
    grep -q "^COUCHELEPHANT_RECORDINGS=/srv/rec$" /etc/default/couchelephant \
        || fail "the user's own line was lost from /etc/default/couchelephant"
    start_service
    curl -fs localhost:8712/api/tv/hello >/dev/null || fail "no hello on 8712"
    stop_service
    kill "$blocker" "$blocker2" 2>/dev/null || true

    echo "PASS [$distro ${format:-native}]"
    exit 0
fi

# ---------------------------------------------------------------- outside
pkgs=$(cd "$1" && pwd)
old=$2 new=$3 arch=${4:-amd64}
here=$(cd "$(dirname "$0")" && pwd)

case "$arch" in
    amd64) rpm_arch=x86_64; tar_arch=x64 ;;
    arm64) rpm_arch=aarch64; tar_arch=arm64 ;;
    *) echo "arch must be amd64 or arm64" >&2; exit 2 ;;
esac

# A fake release feed shaped like the GitHub API, served over HTTP.
feed_dir=$(mktemp -d)
port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("",0)); print(s.getsockname()[1])')
base=http://host.docker.internal:$port
for v in "$old" "$new"; do
    {
        echo '{"assets": ['
        echo "{\"browser_download_url\": \"$base/pkgs/couchelephant_${v}_$arch.deb\"},"
        echo "{\"browser_download_url\": \"$base/pkgs/couchelephant-$v.$rpm_arch.rpm\"},"
        echo "{\"browser_download_url\": \"$base/pkgs/couchelephant-$v-linux-$tar_arch.tar.gz\"}"
        echo ']}'
    } > "$feed_dir/feed-$v.json"
done
ln -s "$pkgs" "$feed_dir/pkgs"
(cd "$feed_dir" && exec python3 -m http.server "$port" >/dev/null 2>&1) &
server=$!
trap 'kill $server 2>/dev/null; rm -rf "$feed_dir"' EXIT
sleep 1

run() {
    docker run --rm --add-host host.docker.internal:host-gateway \
        -v "$here/test_install.sh:/test/test_install.sh:ro" \
        -v "$here/install.sh:/test/install.sh:ro" \
        "$1" bash ${CE_TEST_TRACE:+-x} /test/test_install.sh --inside "$1" \
        "$base/feed-$old.json" "$base/feed-$new.json" "${2:-}"
}

status=0
for d in "${DISTROS[@]}"; do
    run "$d" || status=1
done
# The tarball route, for systems with neither tool.
run debian:12 tar || status=1
exit $status
