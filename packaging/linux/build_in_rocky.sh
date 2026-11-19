#!/bin/bash
# Freeze the server, with a portable comskip, inside a Rocky Linux 9 container.
#   docker run --rm -v "$PWD:/src" -w /src rockylinux:9 \
#       packaging/linux/build_in_rocky.sh <x64|arm64> <tools-dir> [<owner uid:gid>]
# Rocky 9 has glibc 2.34, the oldest of the distros we install on (Debian 12,
# Ubuntu 22.04, Fedora 40, Rocky 9). A bundle frozen on a newer system asks for
# a newer glibc and will not start on those. comskip links ffmpeg and argtable
# statically, built here, so it needs nothing the base system does not have.
set -euo pipefail

arch=${1:?usage: build_in_rocky.sh <x64|arm64> <tools-dir> [uid:gid]}
tools=${2:?tools dir}
owner=${3:-}
FFMPEG_VERSION=7.1.1       # ffmpeg 8 dropped a field comskip still uses
FFMPEG_SHA256=733984395e0dbbe5c046abda2dc49a5544e7e0e1e2366bba849222ae9e3a03b1
ARGTABLE_SHA256=8f77e8a7ced5301af6e22f47302fdbc3b1ff41f2b83c43c77ae5ca041771ddbf

root=$(pwd)
mkdir -p "$tools/bin"
tools=$(cd "$tools" && pwd)

# nasm lives in the CRB repository, and only x86 needs it.
extra=""; [ "$arch" = x64 ] && extra=nasm
dnf install -y -q --enablerepo=crb gcc gcc-c++ make autoconf automake libtool pkgconf-pkg-config \
    $extra xz tar gzip findutils which diffutils zlib-devel python3.12 python3.12-pip >/dev/null

if [ ! -x "$tools/bin/comskip" ]; then
    prefix=/opt/static
    work=$(mktemp -d)
    cd "$work"

    curl -fsSL --retry 5 --retry-delay 10 -o ff.tar.xz "https://ffmpeg.org/releases/ffmpeg-$FFMPEG_VERSION.tar.xz"
    echo "$FFMPEG_SHA256  ff.tar.xz" | sha256sum -c -
    tar xf ff.tar.xz
    (cd "ffmpeg-$FFMPEG_VERSION" && ./configure --prefix=$prefix --enable-static --disable-shared \
        --enable-pic --disable-programs --disable-doc --disable-autodetect --enable-zlib \
        && sed -i 's/-latomic//g' ffbuild/config.mak && make -j"$(nproc)" && make install)

    curl -fsSL --retry 5 --retry-delay 10 -o at.tgz \
        "https://downloads.sourceforge.net/project/argtable/argtable/argtable-2.13/argtable2-13.tar.gz"
    echo "$ARGTABLE_SHA256  at.tgz" | sha256sum -c -
    tar xf at.tgz
    # An old autoconf tree: refresh config.guess so aarch64 is recognised.
    (cd argtable2-13 && cp /usr/share/automake-*/config.guess /usr/share/automake-*/config.sub . \
        && ./configure --prefix=$prefix --disable-shared --enable-static && make -j"$(nproc)" && make install)

    sed -i 's/-latomic//g' $prefix/lib/pkgconfig/*.pc
    export PKG_CONFIG_PATH=$prefix/lib/pkgconfig
    export CPPFLAGS="-I$prefix/include" LDFLAGS="-L$prefix/lib"
    # The static ffmpeg libraries need their own dependencies spelled out.
    export LIBS="$(pkg-config --static --libs libavformat libavcodec libavutil libswscale) -lm -lpthread"
    # ffmpeg probes -latomic first, but its atomics are all 8 bytes or less and
    # gcc inlines those, so leave libatomic out and the base system alone.
    LIBS=${LIBS//-latomic/}
    export LIBS
    "$root/packaging/build_comskip.sh" "$work/out"
    install -m 755 "$work/out/bin/comskip" "$tools/bin/comskip"
    cd "$root"
    rm -rf "$work"
fi

# Prove the glibc is the only thing comskip asks the system for.
echo "comskip links:"
ldd "$tools/bin/comskip" || true
extra=$(ldd "$tools/bin/comskip" | awk '{print $1}' \
    | grep -v -E '^(linux-vdso|linux-gate|ld-linux|libc\.|libm\.|libpthread|libdl|librt|libz\.|/lib)' || true)
[ -z "$extra" ] || { echo "comskip needs more than the base system: $extra" >&2; exit 1; }

python3.12 -m pip install -q -r requirements.txt "pyinstaller==6.*"
COUCHELEPHANT_TOOLS="$tools" python3.12 -m PyInstaller --noconfirm packaging/couchelephant.spec
python3.12 packaging/smoke.py dist/couchelephant/couchelephant

# Leave the files to the runner's user, not root.
if [ -n "$owner" ]; then chown -R "$owner" dist build "$tools" 2>/dev/null || true; fi
