# comskip finds the ad breaks in a recording (app/adskip.py). Debian has no
# package for it, so it is built from a pinned commit of its source here and
# only the program is carried into the image below. GPL-2.0, see THIRD_PARTY.md.
FROM python:3.12-slim AS comskip
ARG COMSKIP_COMMIT=a140b6ac8bc8f596729e9052819affc779c3b377
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl build-essential \
      autoconf automake libtool pkg-config libargtable2-dev \
      libavformat-dev libavcodec-dev libavutil-dev libswscale-dev \
 && curl -fsSL "https://codeload.github.com/erikkaashoek/Comskip/tar.gz/${COMSKIP_COMMIT}" | tar xz \
 && cd "Comskip-${COMSKIP_COMMIT}" && ./autogen.sh && ./configure && make -j"$(nproc)" \
 && install -m 755 comskip /usr/local/bin/comskip

FROM python:3.12-slim

# Filled in by the build, so `docker inspect` and the GitHub package page can
# say what this image is and where it came from.
ARG VERSION=dev
LABEL org.opencontainers.image.title="CouchElephant" \
      org.opencontainers.image.description="A Plex DVR sidecar that records the live broadcast, not the repeat." \
      org.opencontainers.image.source="https://github.com/beamflash/couchelephant" \
      org.opencontainers.image.url="https://github.com/beamflash/couchelephant" \
      org.opencontainers.image.documentation="https://github.com/beamflash/couchelephant/blob/main/docs/INSTALL.md" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    COUCHELEPHANT_DB=/data/couchelephant.db

# ffmpeg makes the viewing copy of a recording CouchElephant makes itself. The
# two VA drivers let it use an Intel or AMD GPU when /dev/dri is passed in, and
# do nothing when it is not. An install that never records never runs any of it.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg mesa-va-drivers libargtable2-0 \
 # The Intel driver exists for amd64 only. Asking for it on arm64 fails the build.
 && if [ "$(dpkg --print-architecture)" = "amd64" ]; then \
      apt-get install -y --no-install-recommends intel-media-va-driver; fi \
 && rm -rf /var/lib/apt/lists/*

COPY --from=comskip /usr/local/bin/comskip /usr/local/bin/comskip

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

VOLUME ["/data"]
# /recordings and /library are optional. Mount them to turn the recorder on.
EXPOSE 8710

# Python asks, rather than curl: the slim image ships neither curl nor wget,
# and adding one to answer a health check is a package to keep patched.
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python3 -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('COUCHELEPHANT_PORT', '8710'), timeout=4).status == 200 else 1)"

# One process. The sync loop runs as a FastAPI background task rather than a
# second container, because it and the web UI share one SQLite file and
# splitting them buys nothing but a locking problem.
CMD ["python", "-m", "app", "--no-open"]
