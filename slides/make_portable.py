#!/usr/bin/env python3
"""Inline the deck font files and share card image into one offline HTML file."""

from __future__ import annotations

import base64
import re
import subprocess
from pathlib import Path


HERE = Path(__file__).resolve().parent
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 Chrome/151 Safari/537.36"
)


def fetch(url: str, *, binary: bool = False) -> bytes | str:
    result = subprocess.run(
        ["curl", "-sL", "--fail", "--max-time", "30", "-A", USER_AGENT, url],
        check=True,
        capture_output=True,
    )
    if binary:
        return result.stdout
    return result.stdout.decode("utf-8", "replace")


html = (HERE / "index.html").read_text(encoding="utf-8")
families = (
    "family=Space+Grotesk:wght@400;500;600;700"
    "&family=Inter:wght@400;500;600"
    "&family=JetBrains+Mono:wght@400;500;600"
)
css = fetch(f"https://fonts.googleapis.com/css2?{families}&display=swap")
if not isinstance(css, str):
    raise TypeError("font stylesheet must be text")

faces: list[str] = []
for match in re.finditer(r"/\*\s*latin\s*\*/\s*@font-face\s*\{([^}]+)\}", css):
    block = match.group(1)
    family_match = re.search(r"font-family:\s*'([^']+)'", block)
    weight_match = re.search(r"font-weight:\s*(\d+)", block)
    style_match = re.search(r"font-style:\s*(\w+)", block)
    url_match = re.search(r"url\((https://[^)]+\.woff2)\)", block)
    if not all((family_match, weight_match, style_match, url_match)):
        continue
    font = fetch(url_match.group(1), binary=True)
    if not isinstance(font, bytes) or not font:
        continue
    encoded = base64.b64encode(font).decode("ascii")
    faces.append(
        "@font-face{"
        f"font-family:'{family_match.group(1)}';"
        f"font-style:{style_match.group(1)};"
        f"font-weight:{weight_match.group(1)};"
        "font-display:swap;"
        f"src:url(data:font/woff2;base64,{encoded}) format('woff2');"
        "}"
    )

html = re.sub(r'\s*<link rel="preconnect"[^>]*>', "", html)
html = re.sub(r'\s*<link href="https://fonts\.googleapis[^\"]*"[^>]*>', "", html)
html = html.replace("<style>", "<style>\n" + "\n".join(faces) + "\n", 1)

# Inline every figure image referenced by the deck as a data URI, so the
# portable file is fully self-contained. Add new figures here by filename.
_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
for name in ("share_card.jpg", "system_design.png"):
    fig = HERE / name
    if name in html and fig.is_file():
        mime = _MIME[fig.suffix.lower()]
        data = base64.b64encode(fig.read_bytes()).decode("ascii")
        html = html.replace(name, f"data:{mime};base64,{data}")

external = re.findall(r'(?:src|href)\s*=\s*["\'](?:https?:|share_card\.jpg|system_design\.png)', html)
if external:
    raise RuntimeError(f"portable deck still has {len(external)} external references")

output = HERE.parent / "X_Arcade_deck_portable.html"
output.write_text(html, encoding="utf-8")
print(f"wrote {output.name} ({len(html)} bytes, {len(faces)} embedded font faces)")
