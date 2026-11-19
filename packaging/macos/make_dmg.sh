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
# hdiutil now and then says "Resource busy" on a busy machine (a disk
# image service still holding the last one). It passes on a second try.
for try in 1 2 3 4 5; do
  if hdiutil create -volname CouchElephant -srcfolder "$stage" -ov -format UDZO "$out"; then
    break
  fi
  [ "$try" = 5 ] && { echo "hdiutil failed 5 times" >&2; exit 1; }
  echo "hdiutil failed, trying again ($try of 5)" >&2
  sleep $((try * 5))
done
echo "built $out"
