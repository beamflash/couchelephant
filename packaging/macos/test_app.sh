#!/usr/bin/env bash
# Mount the dmg, run the app from a temp folder, check it answers, quit it.
#   packaging/macos/test_app.sh <dmg>
# Without a Developer ID build the Gatekeeper check is skipped, and says so
# (unless REQUIRE_SIGNED=1).
set -euo pipefail
dmg="${1:?usage: test_app.sh <dmg>}"
port=8710
work="$(mktemp -d)"
export COUCHELEPHANT_DATA="$work/data"     # never touch a real data folder
mnt="$work/mnt"
mkdir -p "$mnt"
cleanup() {
  pkill -x CouchElephant 2>/dev/null || true
  hdiutil detach "$mnt" -quiet 2>/dev/null || true
  rm -rf "$work"
}
trap cleanup EXIT

hdiutil attach "$dmg" -mountpoint "$mnt" -nobrowse -readonly -quiet
[ -L "$mnt/Applications" ] || { echo "FAILED: no Applications link in the dmg"; exit 1; }
cp -R "$mnt/CouchElephant.app" "$work/"
app="$work/CouchElephant.app"
echo "ok: dmg mounts and holds the app"

grep -q '<key>LSUIElement</key>' "$app/Contents/Info.plist" \
  || { echo "FAILED: LSUIElement missing"; exit 1; }
echo "ok: menu bar only (LSUIElement)"

# REQUIRE_SIGNED=1 comes from a build that had the signing secrets: then a missing
# or rejected signature is a failure, not something to skip.
signed=0
if codesign -dv "$app" 2>&1 | grep -q 'Authority=Developer ID Application'; then signed=1; fi
if [ "${REQUIRE_SIGNED:-0}" = 1 ]; then
  [ "$signed" = 1 ] || { echo "FAILED: signing was expected but the app has no Developer ID signature"; exit 1; }
  spctl --assess --type execute --verbose=2 "$app" \
    || { echo "FAILED: Gatekeeper rejects the app (spctl)"; exit 1; }
  echo "ok: Gatekeeper accepts the app (spctl)"
elif [ "$signed" = 1 ]; then
  spctl --assess --type execute --verbose=2 "$app" \
    || { echo "FAILED: Gatekeeper rejects the app (spctl)"; exit 1; }
  echo "ok: Gatekeeper accepts the app (spctl)"
else
  echo "SKIPPED: spctl check, unsigned build (no Developer ID certificate)"
fi

# The bundled programs must load their libraries from inside the app.
comskip="$(find "$app" -path '*/bin/comskip' -type f | head -1)"
[ -n "$comskip" ] || { echo "FAILED: comskip not in the app"; exit 1; }
out="$("$comskip" --help 2>&1 || true)"
if echo "$out" | grep -qi 'Library not loaded\|dyld'; then
  echo "$out"; echo "FAILED: comskip cannot load its libraries"; exit 1
fi
echo "ok: bundled comskip starts"

# Something else on the port would answer for the app and hide a dead build.
# curl exits 7 only when nothing accepts the connection; anything else means it is taken.
rc=0
curl -s -o /dev/null --max-time 3 "http://localhost:$port/" || rc=$?
if [ "$rc" != 7 ]; then
  echo "FAILED: port $port is already in use before the app started"; exit 1
fi
"$app/Contents/MacOS/CouchElephant" >"$work/app.log" 2>&1 &
pid=$!
for _ in $(seq 1 90); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:$port/api/tv/hello" || true)"
  [ "$code" = 200 ] && break
  kill -0 "$pid" 2>/dev/null || { cat "$work/app.log"; echo "FAILED: the app exited"; exit 1; }
  sleep 1
done
[ "$code" = 200 ] || { cat "$work/app.log"; echo "FAILED: no 200 from /api/tv/hello"; exit 1; }
echo "ok: /api/tv/hello answers 200"

kill "$pid"
for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
echo "ok: app stopped"
