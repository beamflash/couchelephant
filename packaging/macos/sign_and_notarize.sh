#!/usr/bin/env bash
# Sign dist/CouchElephant.app inside out, notarize it, staple the ticket.
#   packaging/macos/sign_and_notarize.sh <identity> <asc-key.p8> <key-id> <issuer>
# Run it before make_dmg.sh so the dmg carries the stapled app; then sign the
# dmg itself with:  codesign --sign <identity> --timestamp <dmg>
set -euo pipefail
identity="${1:?identity}"; p8="${2:?p8 path}"; key_id="${3:?key id}"; issuer="${4:?issuer}"
cd "$(dirname "$0")/../.."
app=dist/CouchElephant.app
ents=packaging/macos/entitlements.plist

list="$(mktemp)"
trap 'rm -f "$list"' EXIT

# Every Mach-O file, deepest first, so a container is signed after its contents.
# The main executable goes last, with the entitlements.
find "$app" -type f \( -perm -u+x -o -name '*.dylib' -o -name '*.so' \) -print0 \
  | while IFS= read -r -d '' f; do
      file -b "$f" | grep -q 'Mach-O' && printf '%s\0' "$f"
    done | sort -z -r > "$list"
while IFS= read -r -d '' f; do
  [ "$f" = "$app/Contents/MacOS/CouchElephant" ] && continue
  codesign --force --options runtime --timestamp --sign "$identity" "$f"
done < "$list"
codesign --force --options runtime --timestamp --entitlements "$ents" --sign "$identity" "$app"
codesign --verify --deep --strict --verbose=2 "$app"

zip="$(mktemp -d)/CouchElephant.zip"
ditto -c -k --keepParent "$app" "$zip"
xcrun notarytool submit "$zip" --key "$p8" --key-id "$key_id" --issuer "$issuer" --wait
xcrun stapler staple "$app"
xcrun stapler validate "$app"
