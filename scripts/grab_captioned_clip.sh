#!/bin/sh
# 25 s of a 1080i channel while it carries captions, for synth.py. In the container.
mkdir -p /tmp/synth && cd /tmp/synth
for i in $(seq 1 15); do
  ffmpeg -v quiet -y -t 25 -i "http://$1:5004/auto/v$2" -map 0 -c copy src.ts
  ffmpeg -v quiet -y -f lavfi -i "movie=src.ts[out0+subcc]" -map 0:1 s.srt
  n=$(grep -c -- '-->' s.srt); [ "$n" -ge 8 ] && { echo "clip ready, $n captions"; exit 0; }
done
echo "no captioned clip found"; exit 1
