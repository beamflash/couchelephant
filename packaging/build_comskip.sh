#!/usr/bin/env bash
# Build comskip from the commit the Dockerfile pins, on Linux or macOS.
#   packaging/build_comskip.sh <dest>      writes <dest>/bin/comskip
# Needs a C toolchain, autoconf, automake, libtool, pkg-config, argtable2 and the
# ffmpeg development libraries (ffmpeg 7 or older; 8 removed ticks_per_frame)
# on macOS: brew install autoconf automake libtool pkg-config argtable ffmpeg).
set -euo pipefail

COMMIT=a140b6ac8bc8f596729e9052819affc779c3b377
dest="${1:?usage: build_comskip.sh <dest>}"
mkdir -p "$dest/bin"
dest="$(cd "$dest" && pwd)"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cd "$work"
curl -fsSL "https://codeload.github.com/erikkaashoek/Comskip/tar.gz/${COMMIT}" | tar xz
cd "Comskip-${COMMIT}"
./autogen.sh
./configure
make -j"$(getconf _NPROCESSORS_ONLN)"
install -m 755 comskip "$dest/bin/comskip"
echo "built $dest/bin/comskip"
