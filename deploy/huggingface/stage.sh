#!/usr/bin/env bash
# Stage the smallest complete offline demo tree for the Hugging Face Space.
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
cp -R "$repo_root/web" "$target/web"
cp -R "$repo_root/cartridges" "$target/cartridges"
cp -R "$repo_root/plugins" "$target/plugins"
cp -R "$repo_root/services" "$target/services"

find "$target" -type d -name __pycache__ -prune -exec rm -r {} +
find "$target" -type f -name '*.pyc' -delete

echo "staged offline demo in $target"
