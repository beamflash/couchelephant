#!/usr/bin/env bash
# Build comskip for macOS with ffmpeg 7 and argtable linked in statically, and
# every piece built for macOS 13, so the app asks nothing of the system that
# macOS 13 lacks and nothing of Homebrew.
#   packaging/macos/build_comskip_static.sh <tools-dir>     writes <tools-dir>/bin/comskip
# Same versions and checksums as the Linux build (build_in_rocky.sh).
# Needs: autoconf automake libtool pkg-config nasm (brew install).
set -euo pipefail

FFMPEG_VERSION=7.1.1       # ffmpeg 8 dropped a field comskip still uses
FFMPEG_SHA256=733984395e0dbbe5c046abda2dc49a5544e7e0e1e2366bba849222ae9e3a03b1
ARGTABLE_SHA256=8f77e8a7ced5301af6e22f47302fdbc3b1ff41f2b83c43c77ae5ca041771ddbf

tools="${1:?usage: build_comskip_static.sh <tools-dir>}"
mkdir -p "$tools/bin"
tools="$(cd "$tools" && pwd)"
root="$(cd "$(dirname "$0")/../.." && pwd)"

export MACOSX_DEPLOYMENT_TARGET=13.0
export CFLAGS="-mmacosx-version-min=13.0" LDFLAGS="-mmacosx-version-min=13.0"
prefix="$(mktemp -d)/static"
work="$(mktemp -d)"
trap 'rm -rf "$work" "$(dirname "$prefix")"' EXIT
cd "$work"

curl -fsSL --retry 5 --retry-delay 10 -o ff.tar.xz "https://ffmpeg.org/releases/ffmpeg-$FFMPEG_VERSION.tar.xz"
echo "$FFMPEG_SHA256  ff.tar.xz" | shasum -a 256 -c -
tar xf ff.tar.xz
# --disable-autodetect keeps Homebrew libraries and Apple frameworks out.
(cd "ffmpeg-$FFMPEG_VERSION" && ./configure --prefix="$prefix" --enable-static --disable-shared \
    --enable-pic --disable-programs --disable-doc --disable-autodetect --enable-zlib \
    --extra-cflags="-mmacosx-version-min=13.0" --extra-ldflags="-mmacosx-version-min=13.0" \
    && make -j"$(getconf _NPROCESSORS_ONLN)" && make install)

curl -fsSL --retry 5 --retry-delay 10 -o at.tgz \
    "https://downloads.sourceforge.net/project/argtable/argtable/argtable-2.13/argtable2-13.tar.gz"
echo "$ARGTABLE_SHA256  at.tgz" | shasum -a 256 -c -
tar xf at.tgz
# An old autoconf tree: take current config.guess and config.sub so arm64 is known.
# -L: the brew prefix is a symlink into the Cellar.
guess="$(find -L "$(brew --prefix automake)" -name config.guess | head -1)"
sub="$(find -L "$(brew --prefix automake)" -name config.sub | head -1)"
[ -f "$guess" ] && [ -f "$sub" ] || { echo "no config.guess or config.sub in automake" >&2; exit 1; }
# It also calls isspace and toupper without ctype.h, which clang 16 refuses by default.
(cd argtable2-13 && cp "$guess" "$sub" . \
    && CFLAGS="$CFLAGS -Wno-implicit-function-declaration" ./configure --prefix="$prefix" --disable-shared --enable-static && make -j"$(getconf _NPROCESSORS_ONLN)" && make install)

export PKG_CONFIG_PATH="$prefix/lib/pkgconfig"
export CPPFLAGS="-I$prefix/include" LDFLAGS="-L$prefix/lib -mmacosx-version-min=13.0"
# The static ffmpeg libraries need their own dependencies spelled out.
export LIBS="$(pkg-config --static --libs libavformat libavcodec libavutil libswscale) -lm -lpthread"
"$root/packaging/build_comskip.sh" "$work/out"
install -m 755 "$work/out/bin/comskip" "$tools/bin/comskip"

echo "comskip links:"
otool -L "$tools/bin/comskip"
if otool -L "$tools/bin/comskip" | grep -E '^[[:space:]]+(/opt/homebrew|/usr/local)'; then
    echo "comskip still points at Homebrew" >&2
    exit 1
fi
