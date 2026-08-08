"""Scripted two-player proof that the Decoy websocket loop works end to end.

Run from the repo root: python server/selfcheck.py

Starts uvicorn in-process, connects two websocket clients, and plays three
rounds against FALLBACK_ROUND: one won by a player, one won by the house with
both players wrong, and one ended by the server-side deadline with nobody
guessing. Prints every state broadcast and exits nonzero if any contract check
fails.

The round timer is shortened to two seconds here so the deadline round does
not stall the script. The served value in production stays config.ROUND_SECONDS.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("ARCADE_MODE", "demo")
os.environ["ARCADE_NO_SHUFFLE"] = "1"  # suite asserts against committed round files
# Pin the server to FALLBACK_ROUND so the scripted guesses below stay valid
# even after the real queue module is integrated.
os.environ["ARCADE_FORCE_FALLBACK"] = "1"

import uvicorn
import websockets

import config

config.ROUND_SECONDS = 2

from server.app import FALLBACK_ROUND, app

HOST = "127.0.0.1"
PORT = 8899
FAILURES: list[str] = []


def check(ok: bool, label: str) -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        FAILURES.append(label)


def summarize(state: dict[str, Any]) -> str:
    players = " ".join(
        f"{p['name']}(score={p['score']} streak={p['streak']} guessed={p['guessed']})"
        for p in state["players"]
    )
    bits = [f"phase={state['phase']}", players or "no-players"]
    if state.get("deadline_ms") is not None:
        bits.append(f"deadline_ms={state['deadline_ms']}")
    reveal = state.get("reveal")
    if reveal:
        bits.append(f"reveal(decoy_slot={reveal['decoy_slot']} winner={reveal['winner']})")
    return " ".join(bits)


async def recv_state(client: Any, who: str) -> dict[str, Any]:
    raw = await asyncio.wait_for(client.recv(), timeout=6)
    state = json.loads(raw)
    print(f"{who} <- {summarize(state)}")
    return state


def check_stripped(state: dict[str, Any]) -> None:
    rnd = state["round"]
    check(rnd is not None, "guessing state carries a round")
    if rnd is None:
        return
    check("decoy_slot" not in rnd, "guessing round has no decoy_slot")
    check("decoy_rationale" not in rnd, "guessing round has no decoy_rationale")
    clean = all(
        "is_decoy" not in r and "author" not in r for r in rnd["replies"]
    )
    check(clean, "guessing replies carry no is_decoy and no author")
    check(len(rnd["replies"]) == 5, "guessing round still has 5 replies")


def http_get(path: str) -> tuple[int, Any]:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{PORT}{path}") as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode("utf-8", "replace")


async def main() -> int:
    server = uvicorn.Server(
        uvicorn.Config(app, host=HOST, port=PORT, log_level="warning")
    )
    serve_task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    if not server.started:
        print("server failed to start")
        return 1

    status, body = await asyncio.to_thread(http_get, "/health")
    print(f"GET /health -> {status} {body}")
    check(status == 200 and body.get("mode") in ("demo", "live"), "/health reports mode")
    check("rounds_available" in body, "/health reports rounds_available")

    status, body = await asyncio.to_thread(http_get, "/token")
    print(f"GET /token -> {status} (501 expected until voice_host lands, 200 after)")
    check(status in (200, 501), "/token is either proxied or a clean 501")

    decoy = FALLBACK_ROUND["decoy_slot"]
    wrongs = [s for s in range(5) if s != decoy]
    url = f"ws://{HOST}:{PORT}/ws"

    async with websockets.connect(url) as p1, websockets.connect(url) as p2:
        print("\n== round 1: P1 wrong, P2 right, P2 wins ==")
        await p1.send(json.dumps({"t": "join", "room": "abc", "name": "P1"}))
        state = await recv_state(p1, "P1")
        check(state["phase"] == "lobby" and len(state["players"]) == 1, "P1 joins into lobby")

        await p2.send(json.dumps({"t": "join", "room": "abc", "name": "P2"}))
        state = await recv_state(p1, "P1")
        await recv_state(p2, "P2")
        check(state["phase"] == "lobby", "second join waits for the session clock")

        # Any joined player may skip the countdown into the round.
        await p1.send(json.dumps({"t": "next", "room": "abc"}))
        state = await recv_state(p1, "P1")
        await recv_state(p2, "P2")
        check(state["phase"] == "guessing", "skip starts guessing")
        check(state["deadline_ms"] is not None and state["deadline_ms"] > 0, "deadline is live")
        check_stripped(state)

        await p1.send(json.dumps({"t": "guess", "room": "abc", "slot": wrongs[0], "ms": 4200}))
        state = await recv_state(p1, "P1")
        await recv_state(p2, "P2")
        check(state["phase"] == "guessing", "one guess keeps the round in guessing")
        check(state["players"][0]["guessed"], "P1 shows as guessed")

        await p2.send(json.dumps({"t": "guess", "room": "abc", "slot": decoy, "ms": 8450}))
        state = await recv_state(p1, "P1")
        await recv_state(p2, "P2")
        check(state["phase"] == "reveal", "all players guessed triggers reveal")
        check(state["reveal"]["winner"] == "P2", "first correct guesser wins")
        check(state["reveal"]["decoy_slot"] == decoy, "reveal restores decoy_slot")
        check(bool(state["reveal"]["rationale"]), "reveal carries the rationale")
        restored = any(r.get("is_decoy") for r in state["round"]["replies"]) and all(
            "author" in r for r in state["round"]["replies"]
        )
        check(restored, "reveal restores is_decoy and authors in the round")
        p2_row = next(p for p in state["players"] if p["name"] == "P2")
        check(p2_row["score"] == 1 and p2_row["streak"] == 1, "winner scores and streaks")
        standings = state.get("standings") or []
        check(len(standings) >= 2, "state carries ranked standings")
        check(standings[0]["name"] == "P2" and standings[0]["score"] == 1, "standings leader is P2")
        lb = (state.get("reveal") or {}).get("leaderboard") or []
        check(lb and lb[0].get("name") == "P2", "reveal leaderboard tops with winner")
        awarded = (state.get("reveal") or {}).get("points_awarded") or []
        check(
            awarded and awarded[0].get("name") == "P2" and awarded[0].get("delta") == 1,
            "reveal reports +1 points_awarded",
        )

        print("\n== round 2: both wrong, house wins ==")
        await p1.send(json.dumps({"t": "next", "room": "abc"}))
        state = await recv_state(p1, "P1")
        await recv_state(p2, "P2")
        check(state["phase"] == "guessing", "next starts a new round")
        p2_row = next(p for p in state["players"] if p["name"] == "P2")
        check(p2_row["score"] == 1, "score persists across rounds")
        check(
            (state.get("standings") or [{}])[0].get("name") == "P2",
            "standings still lead with P2 during next round",
        )
        check_stripped(state)

        await p1.send(json.dumps({"t": "guess", "room": "abc", "slot": wrongs[0], "ms": 3000}))
        await recv_state(p1, "P1")
        await recv_state(p2, "P2")
        await p2.send(json.dumps({"t": "guess", "room": "abc", "slot": wrongs[1], "ms": 3500}))
        state = await recv_state(p1, "P1")
        await recv_state(p2, "P2")
        check(state["phase"] == "reveal", "both wrong still reveals")
        check(state["reveal"]["winner"] == "house", "both wrong means the house wins")
        streaks_reset = all(p["streak"] == 0 for p in state["players"])
        check(streaks_reset, "house win resets streaks")

        print("\n== round 3: nobody guesses, server deadline fires ==")
        await p2.send(json.dumps({"t": "next", "room": "abc"}))
        state = await recv_state(p1, "P1")
        await recv_state(p2, "P2")
        check(state["phase"] == "guessing", "next starts the deadline round")
        state = await recv_state(p1, "P1")
        await recv_state(p2, "P2")
        check(state["phase"] == "reveal", "server timer forces reveal at the deadline")
        check(state["reveal"]["winner"] == "house", "no guesses means the house wins")

    server.should_exit = True
    await serve_task

    print(f"\nselfcheck: {'ALL CHECKS PASSED' if not FAILURES else 'FAILURES: ' + str(FAILURES)}")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
