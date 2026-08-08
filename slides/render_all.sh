#!/usr/bin/env bash
# Render every slide to a PNG through headless Chrome.
set -euo pipefail

cd "$(dirname "$0")"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
DECK="file://$(cd .. && pwd)/X_Arcade_deck_portable.html"
mkdir -p slides_png

if [[ "$#" -gt 0 ]]; then
  IDS=("$@")
else
  IDS=(1 2 3 4 5 6 7 8 9)
fi

for id in "${IDS[@]}"; do
  padded=$(printf "%02d" "$id")
  "$CHROME" --headless=new --disable-gpu --hide-scrollbars \
    --force-device-scale-factor=2 --virtual-time-budget=2500 \
    --window-size=1280,720 --default-background-color=FFFFFFFF \
    --screenshot="slides_png/slide_${padded}.png" "${DECK}?clean=1#${id}" >/dev/null 2>&1
  echo "rendered slide_${padded}.png"
done

echo "done: $(find slides_png -maxdepth 1 -name 'slide_*.png' | wc -l | tr -d ' ') slides"
