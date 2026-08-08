#!/usr/bin/env python3
"""Build a wide-screen PDF from the rendered deck images."""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image


HERE = Path(__file__).resolve().parent
PNG_DIR = HERE / "slides_png"
OUTPUT = HERE.parent / "X_Arcade_deck.pdf"
document = json.loads((HERE / "_deck_data.json").read_text(encoding="utf-8"))
slide_count = len(document.get("result", document)["slides"])

images: list[Image.Image] = []
for index in range(1, slide_count + 1):
    image_path = PNG_DIR / f"slide_{index:02d}.png"
    if not image_path.is_file():
        raise FileNotFoundError(image_path)
    images.append(Image.open(image_path).convert("RGB"))

images[0].save(OUTPUT, save_all=True, append_images=images[1:], resolution=150.0)
print(f"wrote {OUTPUT.name} with {slide_count} pages")
