#!/bin/sh
# Download the pinned nfpm, check its hash, and print the path to the binary.
set -eu

VERSION=2.49.0
SHA_X86_64=2afaa5ffb5e3493402e31e7c67a5a1fb42133e3f66fc315ca0051934125561c7
SHA_ARM64=d4eee71839e92c68cbd3b101640bad32d2856fc551acae0635be9118e48c9d9c

case "$(uname -m)" in
    x86_64|amd64) arch=x86_64; want=$SHA_X86_64 ;;
    aarch64|arm64) arch=arm64; want=$SHA_ARM64 ;;
    *) echo "get_nfpm: unsupported machine $(uname -m)" >&2; exit 1 ;;
esac

here=$(cd "$(dirname "$0")" && pwd)
dir=$here/../tools/nfpm-$VERSION
if [ ! -x "$dir/nfpm" ]; then
    mkdir -p "$dir"
    tgz=$dir/nfpm.tar.gz
    curl -fsSL -o "$tgz" \
        "https://github.com/goreleaser/nfpm/releases/download/v$VERSION/nfpm_${VERSION}_Linux_$arch.tar.gz"
    got=$(sha256sum "$tgz" | cut -d' ' -f1)
    if [ "$got" != "$want" ]; then
        rm -f "$tgz"
        echo "get_nfpm: hash mismatch for nfpm $VERSION" >&2
        exit 1
    fi
    tar -xzf "$tgz" -C "$dir" nfpm
    rm -f "$tgz"
fi
echo "$dir/nfpm"
