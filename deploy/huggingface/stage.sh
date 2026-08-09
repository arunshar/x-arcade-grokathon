#!/usr/bin/env bash
# Stage a complete Space tree: app code + all prebaked media (Imagine videos,
# human reply GIFs, share cards, round art, host mp3s).
set -euo pipefail

if [[ "$#" -ne 1 ]]; then
  echo "usage: $0 TARGET_DIRECTORY" >&2
  exit 2
fi

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
target="$1"
[[ -d "$target" ]] || { echo "target directory does not exist: $target" >&2; exit 2; }

cp "$repo_root/deploy/huggingface/Dockerfile" "$target/Dockerfile"
cp "$repo_root/deploy/huggingface/README.space.md" "$target/README.md"
cp "$repo_root/requirements.txt" "$target/requirements.txt"
cp "$repo_root/config.py" "$target/config.py"
cp "$repo_root/fixtures_core.py" "$target/fixtures_core.py"
cp -R "$repo_root/server" "$target/server"
# Full web tree including static-assets (mp4 / gif / jpg / mp3).
cp -R "$repo_root/web" "$target/web"
cp -R "$repo_root/cartridges" "$target/cartridges"
cp -R "$repo_root/plugins" "$target/plugins"
cp -R "$repo_root/services" "$target/services"

# Skill prompts used by host / imagine agents (optional but keeps live paths honest).
if [[ -d "$repo_root/.grok/skills" ]]; then
  mkdir -p "$target/.grok"
  cp -R "$repo_root/.grok/skills" "$target/.grok/skills"
fi

find "$target" -type d -name __pycache__ -prune -exec rm -r {} +
find "$target" -type f -name '*.pyc' -delete
# Never ship local env or venvs into the Space.
rm -rf "$target/.venv" "$target/.env" 2>/dev/null || true
find "$target" -type f -name '.DS_Store' -delete 2>/dev/null || true

# Auto-certify unique decoy mp4s so live mode serves them without regen.
if [[ -x "$repo_root/.venv/bin/python" ]]; then
  py="$repo_root/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  py="python3"
else
  py="python"
fi
(
  cd "$target"
  PYTHONPATH="$target" "$py" - <<'PY' || true
from pathlib import Path
try:
    from services.imagine_agent import certify_all_existing_decoys
    n = certify_all_existing_decoys()
    print(f"certified decoy mp4s: {n}")
except Exception as exc:
    print(f"certify skipped: {exc}")
PY
)

# Inventory + hard gates so we never ship a media-less Space by accident.
assets="$target/web/static-assets"
decoy_dir="$assets/reply-gifs/decoy"
gif_dir="$assets/reply-gifs"
cards_dir="$assets/cards"

mp4_n=$(find "$decoy_dir" -type f -name '*_decoy.mp4' 2>/dev/null | wc -l | tr -d ' ')
gif_n=$(find "$gif_dir" -maxdepth 1 -type f -name '*.gif' 2>/dev/null | wc -l | tr -d ' ')
card_n=$(find "$cards_dir" -type f \( -name '*.jpg' -o -name '*.png' -o -name '*.webp' \) 2>/dev/null | wc -l | tr -d ' ')
host_n=$(find "$assets" -maxdepth 1 -type f -name 'host_*.mp3' 2>/dev/null | wc -l | tr -d ' ')
art_n=$(find "$assets/round-art" -type f 2>/dev/null | wc -l | tr -d ' ')

echo "staged media inventory:"
echo "  decoy mp4     : $mp4_n"
echo "  human gifs    : $gif_n"
echo "  share cards   : $card_n"
echo "  host mp3      : $host_n"
echo "  round-art     : $art_n"

fail=0
if [[ "${mp4_n:-0}" -lt 5 ]]; then
  echo "::error::need at least 5 certified decoy mp4s under reply-gifs/decoy/ (have $mp4_n)" >&2
  fail=1
fi
if [[ "${gif_n:-0}" -lt 10 ]]; then
  echo "::error::need at least 10 human reply GIFs (have $gif_n)" >&2
  fail=1
fi
if [[ "${host_n:-0}" -lt 5 ]]; then
  echo "::error::need host_*.mp3 stingers (have $host_n)" >&2
  fail=1
fi
if [[ ! -f "$assets/qr.png" ]]; then
  echo "::error::missing web/static-assets/qr.png" >&2
  fail=1
fi
if [[ "$fail" -ne 0 ]]; then
  exit 1
fi

# Write a small manifest the Space /health can surface if needed.
mkdir -p "$target/artifacts"
{
  echo "{"
  echo "  \"decoy_mp4\": $mp4_n,"
  echo "  \"human_gifs\": $gif_n,"
  echo "  \"share_cards\": $card_n,"
  echo "  \"host_mp3\": $host_n,"
  echo "  \"round_art\": $art_n"
  echo "}"
} > "$target/artifacts/media_manifest.json"

echo "staged complete offline demo in $target"
