#!/usr/bin/env bash
# Wrap dist/CouchElephant.app in a dmg with an Applications link.
#   packaging/macos/make_dmg.sh <version> <arm64|x64>   writes dist/CouchElephant-<version>-macos-<arch>.dmg
set -euo pipefail
version="${1:?usage: make_dmg.sh <version> <arm64|x64>}"
arch="${2:?usage: make_dmg.sh <version> <arm64|x64>}"
cd "$(dirname "$0")/../.."

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
cp -R dist/CouchElephant.app "$stage/"
ln -s /Applications "$stage/Applications"
out="dist/CouchElephant-${version}-macos-${arch}.dmg"
rm -f "$out"
hdiutil create -volname CouchElephant -srcfolder "$stage" -ov -format UDZO "$out"
echo "built $out"
