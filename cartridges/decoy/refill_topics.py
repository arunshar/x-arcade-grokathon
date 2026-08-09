#!/usr/bin/env python3
"""Ensure each theme has enough X threads for text + GIF rounds.

Run from repo root with a live key:

    ARCADE_MODE=live python3 cartridges/decoy/refill_topics.py
    ARCADE_MODE=live python3 cartridges/decoy/refill_topics.py --min 4 --imagine

Builds unique decoy_*.json files per topic (does not overwrite older threads)
and optionally prebakes Grok Imagine decoy videos so GIF rounds are ready.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from cartridges.decoy import round_builder as rb  # noqa: E402
from plugins.safety.screen import screen_round  # noqa: E402

TOPICS = [
    "ai",
    "tech",
    "startups",
    "movies",
    "tv",
    "music",
    "sports",
    "nba",
    "baseball",
    "soccer",
    "gaming",
    "science",
    "space",
    "crypto",
    "food",
    "travel",
    "fitness",
    "cars",
    "books",
    "photography",
    "memes",
]


def _counts() -> dict[str, list[dict]]:
    c: dict[str, list[dict]] = defaultdict(list)
    for path in (rb.ROUNDS_DIR).glob("decoy_*.json"):
        try:
            rnd = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not screen_round(rnd).get("screened"):
            continue
        topic = str((rnd.get("source") or {}).get("topic") or "").lower()
        if topic:
            c[topic].append(rnd)
    return c


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min", type=int, default=4, help="min screened posts per topic")
    parser.add_argument(
        "--imagine",
        action="store_true",
        help="prebake Grok Imagine decoy video for rounds missing one",
    )
    parser.add_argument("--live", action="store_true", default=True)
    args = parser.parse_args()
    target = max(1, min(12, int(args.min or 4)))

    built = 0
    failed: list[str] = []
    for topic in TOPICS:
        have = len(_counts().get(topic, []))
        need = max(0, target - have)
        print(f"== {topic}: have={have} need={need}", flush=True)
        for _ in range(need):
            try:
                rnd = rb.build_round(topic, live=bool(args.live))
                gates = screen_round(rnd)
                rnd["safety"] = gates
                if not gates.get("screened"):
                    print(f"  FAIL gate {gates.get('gate_codes')}", flush=True)
                    failed.append(f"{topic}:gate")
                    continue
                path = rb._save_round(topic, rnd, replace_canonical=False)
                built += 1
                print(f"  OK {rnd.get('round_id')} -> {path.name}", flush=True)
            except Exception as exc:
                failed.append(f"{topic}:{exc}")
                print(f"  FAIL {exc}", flush=True)

    print(f"BUILD built={built} failed={len(failed)}", flush=True)

    if args.imagine:
        from services.imagine_agent import (
            decoy_video_path,
            ensure_certified,
            generate_matching_decoy,
            is_imagine_certified,
        )
        from services.reply_gifs import attach_reply_media

        ok = fail = 0
        for path in sorted(rb.ROUNDS_DIR.glob("decoy_*.json")):
            try:
                rnd = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not screen_round(rnd).get("screened"):
                continue
            rid = str(rnd.get("round_id") or "")
            vp = decoy_video_path(rid)
            if ensure_certified(rid, vp) or is_imagine_certified(vp):
                continue
            try:
                attach_reply_media(rnd)
                print(f"  imagine {rid}…", flush=True)
                res = generate_matching_decoy(rnd, force=False)
                if res.get("status") in ("ready", "exists") or is_imagine_certified(vp):
                    ok += 1
                    print(f"  OK imagine {rid}", flush=True)
                else:
                    fail += 1
                    print(f"  FAIL imagine {rid} {res.get('status')}", flush=True)
            except Exception as exc:
                fail += 1
                print(f"  FAIL imagine {rid}: {exc}", flush=True)
        print(f"IMAGINE ok={ok} fail={fail}", flush=True)

    print("== coverage ==", flush=True)
    from services.imagine_agent import decoy_video_path, is_imagine_certified

    for topic in TOPICS:
        rows = _counts().get(topic, [])
        ready = sum(
            1
            for r in rows
            if is_imagine_certified(decoy_video_path(str(r.get("round_id") or "")))
        )
        print(f"  {topic:12} posts={len(rows)} gif_ready={ready}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
