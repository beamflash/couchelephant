#!/bin/sh
# One-line install:
#   curl -fsSL https://raw.githubusercontent.com/datbird/couchelephant/main/packaging/linux/install.sh | sudo sh
# COUCHELEPHANT_RELEASES_URL overrides the release feed. COUCHELEPHANT_FORMAT
# (deb, rpm or tar) overrides the package type picked from this system.
set -eu

RELEASES_URL=${COUCHELEPHANT_RELEASES_URL:-https://api.github.com/repos/datbird/couchelephant/releases/latest}

die() { echo "install.sh: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "this needs root. Run it again with sudo."
command -v curl >/dev/null 2>&1 || die "curl is needed. Install it and run this again."

case "$(uname -m)" in
    x86_64|amd64) deb_arch=amd64; rpm_arch=x86_64; tar_arch=x64 ;;
    aarch64|arm64) deb_arch=arm64; rpm_arch=aarch64; tar_arch=arm64 ;;
    *) die "unsupported processor: $(uname -m)" ;;
esac

ID="" ID_LIKE=""
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
fi

format=${COUCHELEPHANT_FORMAT:-}
if [ -z "$format" ]; then
    format=tar
    for name in $ID $ID_LIKE; do
        case "$name" in
            debian|ubuntu) format=deb; break ;;
            fedora|rhel|centos|rocky|almalinux|suse|opensuse*) format=rpm; break ;;
        esac
    done
fi

case "$format" in
    deb) pattern="_$deb_arch\\.deb" ;;
    rpm) pattern="\\.$rpm_arch\\.rpm" ;;
    tar) pattern="-linux-$tar_arch\\.tar\\.gz" ;;
    *) die "unknown format: $format" ;;
esac

feed=$(curl -fsSL "$RELEASES_URL") || die "could not read the release list at $RELEASES_URL"
url=$(printf '%s\n' "$feed" | grep '"browser_download_url"' \
    | sed 's/.*"browser_download_url"[^"]*"\([^"]*\)".*/\1/' \
    | grep -E -e "$pattern\$" | head -n 1 || true)
[ -n "$url" ] || die "the latest release has no $format package for $(uname -m)"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
chmod 755 "$work"          # apt downloads as another user and must read it
file=$work/${url##*/}
echo "Downloading ${url##*/}"
curl -fSL -o "$file" "$url" || die "download failed: $url"

case "$format" in
    deb)
        DEBIAN_FRONTEND=noninteractive apt-get install -y --reinstall "$file"
        ;;
    rpm)
        if command -v dnf >/dev/null 2>&1; then
            dnf install -y "$file"
        elif command -v zypper >/dev/null 2>&1; then
            zypper --non-interactive --no-gpg-checks install --allow-unsigned-rpm "$file"
        elif command -v yum >/dev/null 2>&1; then
            yum install -y "$file"
        else
            rpm -Uvh --replacepkgs "$file"
        fi
        ;;
    tar)
        tar -xzf "$file" -C "$work"
        [ -d /run/systemd/system ] || echo "No systemd here: the service is installed but will not start by itself."
        # postinstall stops it too, but the files are about to move.
        if [ -d /run/systemd/system ]; then
            systemctl stop couchelephant 2>/dev/null || true
        fi
        rm -rf /opt/couchelephant
        mv "$work/couchelephant" /opt/couchelephant
        mkdir -p /etc/systemd/system
        cp "$work/couchelephant.service" /etc/systemd/system/couchelephant.service
        bash "$work/postinstall.sh"
        ;;
esac
