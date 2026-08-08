#!/usr/bin/env python3
"""Live probes for every xAI surface X Arcade uses. Run once before building.

Writes redacted request/response artifacts with measured latency to
artifacts/probes/. Never writes the API key anywhere.
"""

import base64
import json
import os
import sys
import time
import urllib.request

KEY = os.environ.get("XAI_API_KEY", "")
if not KEY:
    sys.exit("XAI_API_KEY not set")

BASE = "https://api.x.ai/v1"
OUT = os.path.join(os.path.dirname(__file__), "..", "artifacts", "probes")
os.makedirs(OUT, exist_ok=True)


def call(path, payload, timeout=120):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return time.time() - t0, r.status, body
    except urllib.error.HTTPError as e:
        return time.time() - t0, e.code, e.read()


def save(name, record):
    with open(os.path.join(OUT, name), "w") as f:
        json.dump(record, f, indent=1, default=str)
    print(f"{name}: status={record.get('status')} latency={record.get('latency_s'):.2f}s")


results = {}

# 1. Image generation (the share-card path; must be live-fast)
lat, status, body = call("/images/generations", {
    "model": "grok-imagine-image",
    "prompt": "Retro arcade wanted poster, neon on black, a robot hiding among four humans, bold XArcade DECOY title",
    "n": 1,
    "response_format": "b64_json",
})
rec = {"surface": "image_gen", "status": status, "latency_s": lat}
if status == 200:
    data = json.loads(body)
    img = data.get("data", [{}])[0].get("b64_json", "")
    if img:
        with open(os.path.join(OUT, "probe_card.png"), "wb") as f:
            f.write(base64.b64decode(img))
        rec["image_bytes"] = len(img) * 3 // 4
        rec["saved"] = "probe_card.png"
else:
    rec["error"] = body[:500].decode(errors="replace")
save("image_gen.json", rec)
results["image_gen"] = status

# 2. x_search thread fetch (the RoundBuilder feed)
lat, status, body = call("/responses", {
    "model": "grok-4.5",
    "input": "Find one post trending in the last 12 hours about AI with at least 4 substantive replies. Return the post text and 4 distinct reply texts with their author handles.",
    "tools": [{"type": "x_search"}],
    "max_tool_calls": 2,
}, timeout=180)
rec = {"surface": "x_search", "status": status, "latency_s": lat}
if status == 200:
    data = json.loads(body)
    usage = data.get("usage", {})
    rec["usage"] = usage
    texts = []
    for item in data.get("output", []):
        for c in item.get("content", []) or []:
            if c.get("type") == "output_text":
                texts.append(c.get("text", ""))
    rec["output_chars"] = sum(len(t) for t in texts)
    rec["output_preview"] = (texts[0][:400] if texts else "")
else:
    rec["error"] = body[:500].decode(errors="replace")
save("x_search.json", rec)
results["x_search"] = status

# 3. Voice: mint an ephemeral client secret (browser-direct realtime)
lat, status, body = call("/realtime/client_secrets", {})
rec = {"surface": "voice_token", "status": status, "latency_s": lat}
if status == 200:
    data = json.loads(body)
    # record shape only, never the secret value
    rec["response_keys"] = sorted(data.keys())
    secret = data.get("client_secret") or {}
    if isinstance(secret, dict):
        rec["secret_keys"] = sorted(secret.keys())
        rec["secret_value_present"] = bool(secret.get("value"))
else:
    rec["error"] = body[:800].decode(errors="replace")
save("voice_token.json", rec)
results["voice_token"] = status

# 4. TTS (fallback rung 2 and the scripted host lines)
lat, status, body = call("/tts", {
    "text": "Round one. Four of these replies are human. One is not. [pause] Find the decoy.",
    "voice": "Eve",
    "response_format": "mp3",
})
rec = {"surface": "tts", "status": status, "latency_s": lat}
if status == 200:
    ct = "audio"
    try:
        maybe = json.loads(body)
        rec["response_keys"] = sorted(maybe.keys())
        b64 = maybe.get("audio") or maybe.get("data")
        if isinstance(b64, str):
            body = base64.b64decode(b64)
    except (ValueError, KeyError):
        pass
    with open(os.path.join(OUT, "probe_host_line.mp3"), "wb") as f:
        f.write(body if isinstance(body, bytes) else bytes(body))
    rec["audio_bytes"] = len(body)
    rec["saved"] = "probe_host_line.mp3"
else:
    rec["error"] = body[:800].decode(errors="replace")
save("tts.json", rec)
results["tts"] = status

print("\nSUMMARY:", json.dumps(results))
