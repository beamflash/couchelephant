#!/bin/sh
# Wrap a PyInstaller folder into a .deb, an .rpm and a tarball.
#   build_packages.sh <version> <amd64|arm64> <pyinstaller-folder> <out-dir>
set -eu

if [ $# -ne 4 ]; then
    echo "usage: $0 <version> <amd64|arm64> <pyinstaller-folder> <out-dir>" >&2
    exit 2
fi
version=$1 arch=$2 src=$3 out=$4

case "$arch" in
    amd64) rpm_arch=x86_64; tar_arch=x64 ;;
    arm64) rpm_arch=aarch64; tar_arch=arm64 ;;
    *) echo "build_packages: arch must be amd64 or arm64" >&2; exit 2 ;;
esac

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
src=$(cd "$src" && pwd)
mkdir -p "$out"
out=$(cd "$out" && pwd)
nfpm=$("$here/get_nfpm.sh")

cd "$root"
# nfpm does not expand variables in a tree path, so fill it in here.
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
sed "s|@SRC@|$src|" packaging/linux/nfpm.yaml > "$stage/nfpm.yaml"
export VERSION="$version" ARCH="$arch"
"$nfpm" package -f "$stage/nfpm.yaml" -p deb \
    -t "$out/couchelephant_${version}_$arch.deb"
"$nfpm" package -f "$stage/nfpm.yaml" -p rpm \
    -t "$out/couchelephant-$version.$rpm_arch.rpm"

# The tarball carries the unit and scripts so install.sh can set it up.
mkdir "$stage/tar"
cp -a "$src" "$stage/tar/couchelephant"
cp packaging/linux/couchelephant.service packaging/linux/postinstall.sh \
    packaging/linux/preremove.sh "$stage/tar/"
tar -czf "$out/couchelephant-$version-linux-$tar_arch.tar.gz" -C "$stage/tar" .
ls -l "$out"
