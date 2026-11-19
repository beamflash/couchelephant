#!/bin/sh
# Fail when a release tag does not match VERSION in app/version.py.
#   scripts/check_tag_version.sh v1.5.25
set -eu
tag=${1:?usage: check_tag_version.sh <tag>}
root=$(cd "$(dirname "$0")/.." && pwd)
version=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$root/app/version.py")
[ -n "$version" ] || { echo "no VERSION in app/version.py" >&2; exit 2; }
if [ "${tag#v}" != "$version" ]; then
    echo "The tag $tag does not match VERSION $version in app/version.py. Fix one, then tag again." >&2
    exit 1
fi
echo "tag $tag matches VERSION $version"
