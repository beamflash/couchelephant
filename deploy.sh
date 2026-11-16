#!/usr/bin/env bash
# Ship, build and RECREATE the container.
#
# `docker restart` keeps the container pinned to the image it was created with,
# so a rebuilt image is silently ignored. The container must be removed and run
# again for new code to take effect.
set -euo pipefail

HERE_EARLY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# A local .env keeps your host out of the repo. See .env.example.
[ -f "$HERE_EARLY/.env" ] && . "$HERE_EARLY/.env"

# Nothing about one person's network belongs in the repo, so the host has no
# default. Set it, or put these in a local env file that git ignores.
HOST="${COUCHELEPHANT_HOST:?set COUCHELEPHANT_HOST to the ssh host to deploy to}"
REMOTE_DIR="${COUCHELEPHANT_REMOTE_DIR:-/opt/couchelephant}"
PORT="${COUCHELEPHANT_PORT:-8710}"
TZ_NAME="${COUCHELEPHANT_TZ:-UTC}"
# CouchElephant DVR (our own recorder), all optional. A folder on the host for recordings turns it
# on, the Plex library folder lets it hand them over, and the graphics device
# makes the copy you watch on the GPU.
EXTRA=""
[ -n "${COUCHELEPHANT_RECORDINGS_DIR:-}" ] && EXTRA="$EXTRA -v $COUCHELEPHANT_RECORDINGS_DIR:/recordings"
[ -n "${COUCHELEPHANT_LIBRARY_DIR:-}" ] && EXTRA="$EXTRA -v $COUCHELEPHANT_LIBRARY_DIR:/library"
[ -n "${COUCHELEPHANT_DRI:-}" ] && EXTRA="$EXTRA --device /dev/dri"
TAG="couchelephant:local"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> packaging"
tar czf /tmp/couchelephant.tgz -C "$HERE" Dockerfile requirements.txt app/
scp -q /tmp/couchelephant.tgz "$HOST:/tmp/couchelephant.tgz"

echo "==> building on $HOST"
ssh -o ConnectTimeout=60 "$HOST" "
  set -e
  rm -rf $REMOTE_DIR/src && mkdir -p $REMOTE_DIR/src $REMOTE_DIR/data
  tar xzf /tmp/couchelephant.tgz -C $REMOTE_DIR/src
  cd $REMOTE_DIR/src
  docker build -t $TAG . 2>&1 | tail -2
"

echo "==> recreating container"
ssh -o ConnectTimeout=60 "$HOST" "
  set -e
  docker rm -f couchelephant >/dev/null 2>&1 || true
  docker run -d --name couchelephant --restart unless-stopped \
    -p $PORT:8710 \
    -v $REMOTE_DIR/data:/data \
    -e TZ=$TZ_NAME $EXTRA \
    $TAG >/dev/null
  sleep 8
  docker ps --filter name=couchelephant --format '    {{.Status}}'
"

echo "==> health"
ssh -o ConnectTimeout=30 "$HOST" "curl -s --max-time 15 http://127.0.0.1:$PORT/healthz"
echo
