"""Full offline integration proof for X Arcade.

Run from the repo root: python integration_check.py

Starts uvicorn in-process in demo mode with the real round queue, connects two
websocket clients, and plays through every queued round: join x2 auto-start,
stripped guessing state, guesses, reveal with decoy_slot, winner, and the demo
share card. A socket guard turns any non-loopback connection attempt into a
hard failure, which proves the demo needs zero network. The transition trace
lands in artifacts/integration_trace.txt and the exit code is nonzero on any
failed check.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ["ARCADE_MODE"] = "demo"
os.environ["ARCADE_NO_SHUFFLE"] = "1"  # suite asserts against committed round files
# The wrap proof plays every servable round plus one, which must exceed the
# default 6-round match cap. Cap is raised after the answer key is loaded
# (pool can grow past 20). The cap itself gets focused coverage below.
os.environ["ARCADE_MATCH_ROUNDS"] = "64"
os.environ.pop("ARCADE_FORCE_FALLBACK", None)

# Any connection attempt that leaves loopback is an integration failure.
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_orig_connect = socket.socket.connect


def _guarded_connect(self: socket.socket, address: Any, *args: Any, **kwargs: Any) -> Any:
    host = address[0] if isinstance(address, tuple) else str(address)
    if host not in _LOOPBACK:
        raise AssertionError(f"network egress attempted to {host} in demo mode")
    return _orig_connect(self, address, *args, **kwargs)


socket.socket.connect = _guarded_connect  # type: ignore[method-assign]

import uvicorn
import websockets

import config
from plugins.safety.screen import screen_round
from server.app import DEMO_CARD_URL, app

config.ROUND_SECONDS = 15

HOST = "127.0.0.1"
PORT = 8788
ROOM = "ITG"
ROUNDS_DIR = REPO_ROOT / "cartridges" / "decoy" / "rounds"

TRACE: list[str] = []
FAILURES: list[str] = []


def log(line: str) -> None:
    print(line)
    TRACE.append(line)


def check(ok: bool, label: str) -> None:
    log(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        FAILURES.append(label)


def load_answer_key() -> tuple[dict[str, dict[str, Any]], list[str], list[str]]:
    """Map round_id to its file's truth, split into servable and gated-out."""
    key: dict[str, dict[str, Any]] = {}
    servable: list[str] = []
    gated: list[str] = []
    for path in sorted(ROUNDS_DIR.glob("*.json")):
        rnd = json.loads(path.read_text(encoding="utf-8"))
        key[rnd["round_id"]] = rnd
        if screen_round(rnd)["screened"]:
            servable.append(rnd["round_id"])
        else:
            gated.append(rnd["round_id"])
    return key, servable, gated


def summarize(state: dict[str, Any]) -> str:
    rnd = state.get("round") or {}
    players = " ".join(
        f"{p['name']}(score={p['score']} guessed={p['guessed']})"
        for p in state.get("players", [])
    )
    bits = [f"phase={state['phase']}", f"round={rnd.get('round_id', '-')}", players]
    if state.get("deadline_ms") is not None:
        bits.append(f"deadline_ms={state['deadline_ms']}")
    reveal = state.get("reveal")
    if reveal:
        bits.append(
            f"reveal(decoy_slot={reveal.get('decoy_slot')} "
            f"winner={reveal.get('winner')} card={reveal.get('share_card_url')})"
        )
    return " ".join(bits)


async def recv_state(client: Any, who: str) -> dict[str, Any]:
    raw = await asyncio.wait_for(client.recv(), timeout=8)
    state = json.loads(raw)
    log(f"{who} <- {summarize(state)}")
    return state


def _http_get_blocking(path: str) -> tuple[int, bytes, str]:
    with urllib.request.urlopen(f"http://{HOST}:{PORT}{path}") as resp:
        return resp.status, resp.read(), resp.headers.get("content-type", "")


async def http_get(path: str) -> tuple[int, bytes, str]:
    """Run the blocking urllib call in a worker thread.

    The server shares this process's event loop, so a synchronous request
    from the loop itself would deadlock the whole check.
    """
    return await asyncio.to_thread(_http_get_blocking, path)


async def main() -> int:
    key, servable, gated = load_answer_key()
    log(f"answer key: {len(servable)} servable rounds, gated out: {gated}")

    # Serve-order truth is the server's theme pool, not the file listing: the
    # theme layer quarantines off-tag rounds (which must then never serve) and
    # orders by topic bucket. The pool must stay a subset of screened rounds.
    from server.app import _refresh_theme_pool, _theme_pool_cache

    by_topic = _refresh_theme_pool()
    pool = [str(r.get("round_id")) for r in (_theme_pool_cache.get("all") or [])]
    if not pool:
        pool = [str(r.get("round_id")) for rows in by_topic.values() for r in rows]
    if not set(pool) <= set(servable):
        leaked = sorted(set(pool) - set(servable))
        log(f"FATAL: theme pool contains unscreened rounds: {leaked}")
        return 1
    quarantined = sorted(set(servable) - set(pool))
    log(f"theme pool: {len(pool)} rounds; quarantined off-tag: {quarantined}")
    screened_total = len(servable)  # /health counts screened files, not the pool
    servable = pool

    # Match cap must fit the full wrap proof (every servable + 1).
    config.MATCH_ROUNDS = max(int(getattr(config, "MATCH_ROUNDS", 6) or 6), len(servable) + 5)
    log(f"match cap for wrap proof: {config.MATCH_ROUNDS}")

    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, log_level="warning"))
    serve_task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    if not server.started:
        log("server failed to start")
        return 1

    log("== http checks ==")
    status, body, ctype = await http_get("/health")
    health = json.loads(body)
    log(f"GET /health -> {status} {health}")
    check(status == 200 and health["mode"] == "demo", "/health reports demo mode")
    check(health["rounds_available"] == screened_total, "/health counts screened rounds")

    status, body, _ = await http_get("/token")
    token = json.loads(body)
    log(f"GET /token -> {status} keys={sorted(token.keys())}")
    check(status == 200 and token.get("demo") is True, "/token returns the offline demo stub")

    status, body, ctype = await http_get("/")
    check(status == 200 and b"DECOY" in body, "/ serves the web client")
    status, body, ctype = await http_get("/static-assets/host_intro.mp3")
    check(status == 200 and len(body) > 10000, "host_intro.mp3 serves with real bytes")
    status, body, ctype = await http_get(DEMO_CARD_URL)
    check(status == 200 and len(body) > 10000, "committed demo share card serves")

    url = f"ws://{HOST}:{PORT}/ws"
    served_ids: list[str] = []
    async with websockets.connect(url) as p1, websockets.connect(url) as p2:
        log("== join x2, session clock armed, skip the wait ==")
        await p1.send(json.dumps({"t": "join", "room": ROOM, "name": "P1"}))
        state = await recv_state(p1, "P1")
        check(state["phase"] == "lobby" and len(state["players"]) == 1, "first join lands in lobby")
        check(isinstance(state.get("auto_ms"), int), "lobby carries the session-clock countdown")

        await p2.send(json.dumps({"t": "join", "room": ROOM, "name": "P2"}))
        state = await recv_state(p1, "P1")
        await recv_state(p2, "P2")
        check(state["phase"] == "lobby", "second join waits for the clock, no auto-start")

        # Any joined player may skip the countdown. The clock would fire on
        # its own; the check skips it to stay fast and deterministic.
        await p2.send(json.dumps({"t": "next", "room": ROOM}))
        state = await recv_state(p1, "P1")
        await recv_state(p2, "P2")
        check(state["phase"] == "guessing", "any player skips the wait into guessing")

        # Play one full cycle plus one round so the queue is proven to wrap.
        total_rounds = len(servable) + 1
        for round_no in range(1, total_rounds + 1):
            rnd = state["round"]
            rid = rnd["round_id"]
            served_ids.append(rid)
            log(f"== round {round_no}: {rid} ==")
            check(rid in key, "served round comes from the committed queue")
            check(rid not in gated, "gated round is never served")
            check(rnd.get("safety", {}).get("screened") is True, "round carries fresh gate result")
            check("decoy_slot" not in rnd and "decoy_rationale" not in rnd, "guessing strips decoy fields")
            clean = all("is_decoy" not in r and "author" not in r for r in rnd["replies"])
            check(clean, "guessing replies carry no is_decoy and no author")
            check(len(rnd["replies"]) == 5, "round has exactly 5 replies")
            check(
                all("guess_slot" not in p for p in state["players"]),
                "guessing hides who picked what",
            )
            # Media uniformity: during guessing, either every reply carries an
            # opaque /media/ URL of identical shape and type, or none carries
            # media at all. Any real path, mixed extension, or lone pending
            # status is an oracle that marks the decoy.
            media = [(r.get("media_url"), r.get("media_type"), r.get("media_status"))
                     for r in rnd["replies"]]
            with_media = [m for m in media if m[0]]
            if with_media:
                import re as _re
                pat = _re.compile(r"^/media/[^/]+/[0-9a-f]{8}/\d\.mp4$")
                check(len(with_media) == 5, "gif round serves media on all five or none")
                check(all(pat.match(u) for u, _, _ in with_media),
                      "guessing media urls are opaque proxy paths")
                check(all(t == "video" and s == "ready" for _, t, s in with_media),
                      "guessing media type and status are uniform")
                check(not any("/decoy/" in (u or "") or ".gif" in (u or "")
                              for u, _, _ in with_media),
                      "no real media path reaches the client during guessing")

            decoy_slot = key[rid]["decoy_slot"]
            wrong_slot = next(s for s in range(5) if s != decoy_slot)

            log(f"P1 -> guess slot={wrong_slot} (wrong)")
            await p1.send(json.dumps({"t": "guess", "room": ROOM, "slot": wrong_slot, "ms": 4200}))
            state = await recv_state(p1, "P1")
            await recv_state(p2, "P2")
            check(state["phase"] == "guessing", "one guess keeps the round open")

            log(f"P2 -> guess slot={decoy_slot} (the decoy)")
            await p2.send(json.dumps({"t": "guess", "room": ROOM, "slot": decoy_slot, "ms": 8450}))
            state = await recv_state(p1, "P1")
            await recv_state(p2, "P2")
            check(state["phase"] == "reveal", "all guesses trigger reveal")
            reveal = state["reveal"]
            check(reveal["decoy_slot"] == decoy_slot, "reveal decoy_slot matches the round file")
            check(reveal["winner"] == "P2", "first correct guesser wins")
            check(bool(reveal["rationale"]), "reveal carries the rationale")
            check(reveal["share_card_url"] == DEMO_CARD_URL, "reveal carries the demo share card")
            restored = any(r.get("is_decoy") for r in state["round"]["replies"]) and all(
                "author" in r for r in state["round"]["replies"]
            )
            check(restored, "reveal restores is_decoy and authors")
            picks = {p["name"]: p.get("guess_slot") for p in state["players"]}
            check(
                picks.get("P1") == wrong_slot and picks.get("P2") == decoy_slot,
                "reveal shows who picked which reply",
            )

            if round_no < total_rounds:
                await p1.send(json.dumps({"t": "next", "room": ROOM}))
                state = await recv_state(p1, "P1")
                await recv_state(p2, "P2")
                check(state["phase"] == "guessing", "next starts the following round")

        # Score-tiered picking retired the sorted-order walk and wraparound.
        # What serving promises now: a bounded recent window (mirrors the
        # server's keep formula) with no repeat inside it, only theme-pool
        # rounds ever serve, and the walk never stalls.
        window = max(12, min(40, len(key) - 1))
        no_repeat = all(
            len(set(served_ids[i : i + window])) == min(window, len(served_ids) - i)
            for i in range(len(served_ids))
        )
        check(no_repeat, f"no repeats inside the {window}-round recent window")
        check(set(served_ids) <= set(servable), "walk serves only theme-pool rounds")
        check(len(served_ids) == total_rounds, "walk never stalls")
        p2 = next(p for p in state["players"] if p["name"] == "P2")
        check(p2["score"] == total_rounds, "winner score accumulates across rounds")

    log("== match cap: reveal at the cap goes to results, then back to lobby ==")
    config.MATCH_ROUNDS = 1
    # SOLO* rooms may start with one player; normal multiplayer needs 2.
    cap_room = "SOLOCAP"
    try:
        async with websockets.connect(url) as c1:
            await c1.send(json.dumps({"t": "join", "room": cap_room, "name": "C1"}))
            state = await recv_state(c1, "C1")
            await c1.send(json.dumps({"t": "next", "room": cap_room}))
            state = await recv_state(c1, "C1")
            check(state["phase"] == "guessing", "capped match round starts")
            await c1.send(json.dumps({"t": "guess", "room": cap_room, "slot": 0, "ms": 1}))
            state = await recv_state(c1, "C1")
            check(state["phase"] == "reveal", "capped match reveals")
            check(state.get("match_over") is True, "last reveal flags match_over")
            await c1.send(json.dumps({"t": "next", "room": cap_room}))
            state = await recv_state(c1, "C1")
            check(state["phase"] == "results", "next after the cap enters results")
            check(bool(state.get("results")), "results phase carries a results payload")
            # next from results is PLAY AGAIN: scores reset, round 1 starts.
            await c1.send(json.dumps({"t": "next", "room": cap_room}))
            state = await recv_state(c1, "C1")
            check(state["phase"] == "guessing", "next after results starts a new match")
            me = state["players"][0]
            check(me["score"] == 0 and state.get("rounds_played") == 1,
                  "new match resets score and round counter")
    finally:
        config.MATCH_ROUNDS = max(64, len(servable) + 5)

    log("== multiplayer lobby refuses to start with one player ==")
    try:
        async with websockets.connect(url) as m1:
            await m1.send(json.dumps({"t": "join", "room": "DUO1", "name": "A"}))
            state = await recv_state(m1, "A")
            check(state.get("min_players") == 2, "multiplayer min_players is 2")
            check(state.get("can_start") is False, "one player cannot start multiplayer")
            await m1.send(json.dumps({"t": "next", "room": "DUO1"}))
            state = await recv_state(m1, "A")
            check(state["phase"] == "lobby", "next with one player stays in lobby")
    except Exception as exc:
        check(False, f"multiplayer min-players check errored: {exc}")

    server.should_exit = True
    await serve_task

    log("== serve-time shuffle invariants (bypassed above via ARCADE_NO_SHUFFLE) ==")
    import copy as _copy

    from cartridges.decoy import queue as _q

    _no_shuffle = os.environ.pop("ARCADE_NO_SHUFFLE", None)
    try:
        base = json.loads((ROUNDS_DIR / "decoy_music.json").read_text(encoding="utf-8"))
        seen_slots: set[int] = set()
        sound = True
        for _ in range(20):
            shuffled = _q.randomize_decoy_position(_copy.deepcopy(base))
            slots = sorted(r["slot"] for r in shuffled["replies"])
            decoys = [r for r in shuffled["replies"] if r.get("is_decoy")]
            sound &= slots == [0, 1, 2, 3, 4]
            sound &= len(decoys) == 1 and decoys[0]["slot"] == shuffled["decoy_slot"]
            sound &= decoys[0]["text"] == next(
                r["text"] for r in base["replies"] if r.get("is_decoy")
            )
            seen_slots.add(shuffled["decoy_slot"])
        check(sound, "20 shuffles keep slots a permutation and decoy_slot true")
        check(len(seen_slots) >= 2, "shuffle actually moves the decoy across slots")
    finally:
        if _no_shuffle is not None:
            os.environ["ARCADE_NO_SHUFFLE"] = _no_shuffle

    verdict = "ALL CHECKS PASSED" if not FAILURES else f"FAILURES: {FAILURES}"
    log(f"\nintegration: {verdict} ({len(served_ids)} rounds played, zero network egress)")

    trace_path = REPO_ROOT / "artifacts" / "integration_trace.txt"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text("\n".join(TRACE) + "\n", encoding="utf-8")
    print(f"trace written to {trace_path.relative_to(REPO_ROOT)}")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
