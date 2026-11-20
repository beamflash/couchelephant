#!/usr/bin/env bash
# The CouchElephant the Apple apps' clicking tests talk to, in a container on
# port 8790 (or $PORT). See scripts/apple_test_server.py. AWAY=1 turns on
# Use away from home (HTTPS with its own certificate on the same port). Stop it with
#   docker rm -f couchelephant-apple-test
set -euo pipefail
cd "$(dirname "$0")/.."
PORT=${PORT:-8790}
docker build -q -t couchelephant-apple-test . >/dev/null
docker rm -f couchelephant-apple-test >/dev/null 2>&1 || true
# The working tree's app, scripts and fakes, read only, over the image's copy.
docker run -d --name couchelephant-apple-test -p "$PORT:$PORT" -e PORT="$PORT" -e AWAY="${AWAY:-}" \
  -v "$PWD/app:/app/app:ro" -v "$PWD/tests:/app/tests:ro" -v "$PWD/scripts:/app/scripts:ro" \
  --entrypoint python couchelephant-apple-test scripts/apple_test_server.py >/dev/null
for _ in $(seq 1 60); do
  if curl -fs "http://127.0.0.1:$PORT/api/tv/hello" >/dev/null; then
    docker logs couchelephant-apple-test 2>&1 | tail -4
    exit 0
  fi
  sleep 1
done
docker logs couchelephant-apple-test 2>&1 | tail -30
echo "the test server did not come up" >&2
exit 1
