"""X Arcade backend. One FastAPI app is the entire server.

It serves the static web client, runs the Decoy game rooms over a websocket,
exposes a health check, and proxies voice token minting to services.voice_host.

Run from the repo root: uvicorn server.app:app

Rooms live in memory. The websocket protocol is defined in CONTRACT.md and this
module conforms to it. The one rule that matters most: during the guessing
phase the broadcast state never contains is_decoy, decoy_slot, decoy_rationale,
or real reply authors. Reveal restores them. The client is never trusted.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config
from cartridges.decoy import queue as decoy_queue
from plugins.safety import screen as safety_screen

# Internal knob, not part of the contract. When set to 1 the server skips the
# round queue and always serves FALLBACK_ROUND. selfcheck.py sets it so the
# scripted guesses stay valid even after the real queue lands.
FORCE_FALLBACK = os.environ.get("ARCADE_FORCE_FALLBACK", "") == "1"

# The committed share card for demo mode. Live mode renders a fresh one
# through services.card_forge after the reveal broadcast.
DEMO_CARD_URL = "/static-assets/cards/decoy-3f2710c0a9e6_demo.jpg"

# A complete Round in the CONTRACT.md shape. Lets the server run standalone
# before cartridges/decoy/queue.py is integrated. All handles are fictional.
FALLBACK_ROUND: dict[str, Any] = {
    "round_id": "decoy-fallback00001",
    "source": {
        "post_text": (
            "If you could only keep one debugging tool for the rest of your "
            "career, what is it and why?"
        ),
        "post_author": "@stack_sage",
        "post_url": "https://x.com/stack_sage/status/0",
        "topic": "tech",
    },
    "replies": [
        {
            "slot": 0,
            "text": "print statements. Not glamorous but they have never once lied to me.",
            "author": "@printf_pete",
            "is_decoy": False,
        },
        {
            "slot": 1,
            "text": "A real debugger. Watching state change line by line beats guessing every time.",
            "author": "@gdb_gal",
            "is_decoy": False,
        },
        {
            "slot": 2,
            "text": (
                "Honestly, it depends on the context. Every tool has strengths, "
                "and the best engineers know when to reach for each one."
            ),
            "author": "decoy",
            "is_decoy": True,
        },
        {
            "slot": 3,
            "text": "strace. The bug is usually in the syscalls, not in your code.",
            "author": "@strace_steve",
            "is_decoy": False,
        },
        {
            "slot": 4,
            "text": "The rubber duck. Explaining the bug out loud finds it before the tooling does.",
            "author": "@rubber_duck_rd",
            "is_decoy": False,
        },
    ],
    "decoy_slot": 2,
    "decoy_rationale": (
        "Hedges on every side and commits to nothing. Real replies pick a "
        "favorite and defend it."
    ),
    "safety": {"screened": True, "gate_codes": []},
    "seed": 424242,
}


class PlayerState:
    """One connected player inside a room."""

    def __init__(self, name: str, ws: WebSocket):
        self.name = name
        self.ws = ws
        self.score = 0
        self.streak = 0
        self.guessed = False
        self.guess_slot: int | None = None
        self.guess_order: int | None = None
        self.client_ms: Any = None


# room id -> room dict. Single event loop, no locks needed at demo scale.
ROOMS: dict[str, dict[str, Any]] = {}

app = FastAPI(title="X Arcade")


# Arena rooms are crowd rooms: any number of players join by scanning the QR,
# auto-start is disabled, and only the host (the first joiner, the host on stage)
# advances rounds. Everything else keeps the normal duel behavior.
ARENA_ROOMS = {"GROK"}


def _get_room(room_id: str) -> dict[str, Any]:
    room = ROOMS.get(room_id)
    if room is None:
        room = {
            "room_id": room_id,
            "phase": "lobby",
            "players": {},
            "round": None,
            "reveal": None,
            "deadline_at": None,
            "timer": None,
            "guess_counter": 0,
            "arena": room_id in ARENA_ROOMS,
            "host": None,
        }
        ROOMS[room_id] = room
    return room


async def _next_round() -> dict[str, Any]:
    """Pull the next safety-screened round from the decoy queue.

    Every round is re-screened at load time and carries the fresh gate result
    in its safety dict. A round that fails any gate is skipped, never served
    (fail closed, see plugins/safety/SAFETY.md). FALLBACK_ROUND stays the last
    resort for a broken or fully gated-out queue, and it is screened the same
    way as everything else.
    """
    if not FORCE_FALLBACK:
        try:
            for _ in range(decoy_queue.round_count()):
                rnd = decoy_queue.next_round()
                gates = safety_screen.screen_round(rnd)
                rnd["safety"] = gates
                if gates["screened"]:
                    return rnd
        except Exception:
            pass
    fallback = copy.deepcopy(FALLBACK_ROUND)
    fallback["safety"] = safety_screen.screen_round(fallback)
    return fallback


def _rounds_available() -> int:
    """How many queued rounds pass the safety gates right now.

    The fallback round always counts as at least one, so /health never
    reports an unplayable server.
    """
    if not FORCE_FALLBACK:
        try:
            count = 0
            for path in sorted(decoy_queue.ROUNDS_DIR.glob("*.json")):
                rnd = json.loads(path.read_text(encoding="utf-8"))
                if safety_screen.screen_round(rnd)["screened"]:
                    count += 1
            if count:
                return count
        except Exception:
            pass
    return 1


def _deadline_ms(room: dict[str, Any]) -> int | None:
    if room["phase"] != "guessing" or room["deadline_at"] is None:
        return None
    remaining = room["deadline_at"] - asyncio.get_running_loop().time()
    return max(0, int(remaining * 1000))


def _round_view(room: dict[str, Any]) -> dict[str, Any] | None:
    """The round as clients may see it in the current phase.

    During guessing this is the contract's critical strip: no decoy_slot, no
    decoy_rationale, and replies reduced to slot and text only, which removes
    both is_decoy and the real author values. Any other phase gets the full
    round.
    """
    rnd = room["round"]
    if rnd is None or room["phase"] != "guessing":
        return rnd
    safe = {k: v for k, v in rnd.items() if k not in ("decoy_slot", "decoy_rationale")}
    safe["replies"] = [{"slot": r["slot"], "text": r["text"]} for r in rnd["replies"]]
    return safe


def _public_state(room: dict[str, Any]) -> dict[str, Any]:
    return {
        "t": "state",
        "room": room["room_id"],
        "phase": room["phase"],
        "players": [
            {
                "name": p.name,
                "score": p.score,
                "streak": p.streak,
                "guessed": p.guessed,
            }
            for p in room["players"].values()
        ],
        "round": _round_view(room),
        "reveal": room["reveal"],
        "deadline_ms": _deadline_ms(room),
    }


async def _broadcast(room: dict[str, Any]) -> None:
    state = _public_state(room)
    dead: list[str] = []
    for player in list(room["players"].values()):
        try:
            await player.ws.send_json(state)
        except Exception:
            dead.append(player.name)
    for name in dead:
        room["players"].pop(name, None)


def _cancel_timer(room: dict[str, Any]) -> None:
    timer = room.get("timer")
    if timer is not None and not timer.done():
        timer.cancel()
    room["timer"] = None


async def _round_timer(room: dict[str, Any]) -> None:
    """Server-side deadline. Fires the reveal when the clock runs out."""
    try:
        await asyncio.sleep(config.ROUND_SECONDS)
    except asyncio.CancelledError:
        return
    if room["phase"] == "guessing":
        await _do_reveal(room)


async def _start_round(room: dict[str, Any]) -> None:
    _cancel_timer(room)
    room["round"] = await _next_round()
    room["reveal"] = None
    room["phase"] = "guessing"
    room["guess_counter"] = 0
    for p in room["players"].values():
        p.guessed = False
        p.guess_slot = None
        p.guess_order = None
        p.client_ms = None
    room["deadline_at"] = asyncio.get_running_loop().time() + config.ROUND_SECONDS
    room["timer"] = asyncio.create_task(_round_timer(room))
    await _broadcast(room)


async def _do_reveal(room: dict[str, Any]) -> None:
    _cancel_timer(room)
    rnd = room["round"]
    decoy_slot = rnd["decoy_slot"]
    # Winner is the first correct guess in server arrival order. The client's
    # self-reported ms is display data only and never decides the winner.
    correct = [p for p in room["players"].values() if p.guess_slot == decoy_slot]
    correct.sort(key=lambda p: p.guess_order if p.guess_order is not None else 1 << 30)
    winner = correct[0].name if correct else "house"
    for p in room["players"].values():
        if p.name == winner:
            p.score += 1
            p.streak += 1
        else:
            p.streak = 0
    room["phase"] = "reveal"
    room["deadline_at"] = None
    standings = sorted(
        room["players"].values(), key=lambda p: (-p.score, p.name.lower())
    )
    room["reveal"] = {
        "decoy_slot": decoy_slot,
        "rationale": rnd.get("decoy_rationale", ""),
        "winner": winner,
        # Top of the crowd at every reveal. In a duel this is just both
        # players, in an arena it is the scoreboard beat on the big screen.
        "leaderboard": [
            {"name": p.name, "score": p.score} for p in standings[:5]
        ],
        # Demo mode attaches the committed card instantly. Live mode starts
        # with no card and a background task fills it in a few seconds later.
        "share_card_url": None if config.MODE == "live" else DEMO_CARD_URL,
    }
    await _broadcast(room)
    if config.MODE == "live":
        asyncio.create_task(_attach_live_card(room, rnd, winner))


async def _attach_live_card(room: dict[str, Any], rnd: dict[str, Any], winner: str) -> None:
    """Render the live share card off the event loop, then re-broadcast.

    card_forge takes about 6.5 seconds per image, so the reveal itself is
    never delayed by it. Any failure here leaves the reveal without a card
    and the game keeps going.
    """
    reveal = room.get("reveal")
    if reveal is None:
        return
    try:
        from services.card_forge import make_share_card

        display = winner if winner != "house" else "The House"
        path = await asyncio.to_thread(make_share_card, rnd, display)
        url = "/static-assets/cards/" + path.name
    except Exception as exc:
        # Keep the game moving; log so live Imagine failures are visible.
        print(f"card_forge failed: {exc}", file=sys.stderr)
        return
    if room.get("reveal") is reveal and room["phase"] == "reveal":
        reveal["share_card_url"] = url
        await _broadcast(room)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "mode": config.MODE,
        "rounds_available": _rounds_available(),
        "voice_model": config.MODEL_VOICE,
        "image_model": config.MODEL_IMAGE,
    }


def _lan_base_urls(port: int) -> list[str]:
    """Best-effort LAN origins a phone on the same Wi‑Fi can open.

    Avoids socket.getaddrinfo(hostname) — on macOS that can stall on mDNS and
    block the lobby QR for seconds.
    """
    import socket

    bases: list[str] = []
    seen: set[str] = set()

    def add_origin(origin: str) -> None:
        origin = origin.strip().rstrip("/")
        if not origin or origin in seen:
            return
        seen.add(origin)
        bases.append(origin)

    def add_host(host: str) -> None:
        host = host.strip().strip("[]")
        if not host:
            return
        # Skip loopback and link-local IPv6 noise.
        if host.startswith("127.") or host == "localhost" or host.startswith("::"):
            return
        if ":" in host:
            add_origin(f"http://[{host}]:{port}")
        else:
            add_origin(f"http://{host}:{port}")

    public = os.environ.get("ARCADE_PUBLIC_URL", "").strip().rstrip("/")
    if public:
        add_origin(public)

    # UDP trick: pick the interface used for outbound traffic (no packets sent).
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.settimeout(0.2)
        probe.connect(("8.8.8.8", 80))
        add_host(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass

    return bases


@app.get("/join-info")
async def join_info(
    request: Request, room: str = Query(default="GROK", min_length=1, max_length=16)
) -> dict[str, Any]:
    """URLs + QR path for phone players. Used by the host lobby."""
    room_code = room.strip().upper() or "GROK"
    port = request.url.port or (443 if request.url.scheme == "https" else 80)
    # When bound on 8787 behind uvicorn, port is usually set on the request.
    if port in (80, 443) and os.environ.get("ARCADE_PORT"):
        try:
            port = int(os.environ["ARCADE_PORT"])
        except ValueError:
            port = 8787
    if port in (80, 443):
        # Local dev default from run.sh.
        port = 8787

    urls: list[str] = []
    for base in _lan_base_urls(port):
        urls.append(f"{base.rstrip('/')}/?room={room_code}")

    # Prefer a non-localhost request base when the host already opened via LAN IP.
    host = request.headers.get("host", "")
    if host and not host.startswith("127.") and not host.startswith("localhost"):
        scheme = request.url.scheme or "http"
        candidate = f"{scheme}://{host}/?room={room_code}"
        if candidate not in urls:
            urls.insert(0, candidate)

    localhost_only = not urls
    if localhost_only:
        urls = [f"http://127.0.0.1:{port}/?room={room_code}"]

    primary = urls[0]
    return {
        "room": room_code,
        "primary": primary,
        "urls": urls,
        "qr_path": f"/qr.png?room={room_code}",
        "localhost_only": localhost_only,
    }


@app.get("/qr.png")
async def qr_png(
    request: Request, room: str = Query(default="GROK", min_length=1, max_length=16)
) -> Response:
    """Dynamic join QR encoding a phone-reachable URL when possible."""
    info = await join_info(request, room=room)
    target = str(info["primary"])
    try:
        import io

        import segno

        buf = io.BytesIO()
        qr = segno.make(target, error="h")
        qr.save(buf, kind="png", scale=8, border=2, dark="#04070B", light="#FFFFFF")
        return Response(
            content=buf.getvalue(),
            media_type="image/png",
            headers={"Cache-Control": "no-store"},
        )
    except Exception:
        # Fall back to the committed static asset so the lobby never blanks.
        static_path = REPO_ROOT / "web" / "static-assets" / "qr.png"
        if static_path.is_file():
            return Response(
                content=static_path.read_bytes(),
                media_type="image/png",
                headers={"Cache-Control": "no-store"},
            )
        raise HTTPException(status_code=501, detail="qr generation unavailable") from None


@app.get("/token")
async def token() -> Any:
    """Mint a realtime voice token via services.voice_host.

    Demo mode is fully offline, so it returns a clearly labeled stub instead
    of touching the network. Realtime voice is a live-mode enhancement, and
    the pre-rendered host line mp3s cover the demo.
    """
    if config.MODE != "live":
        return {
            "demo": True,
            "value": "",
            "expires_at": None,
            "model": config.MODEL_VOICE,
            "detail": "demo mode is offline. Run ARCADE_MODE=live to mint a realtime token.",
        }
    try:
        from services.voice_host import mint_token
    except ImportError:
        raise HTTPException(status_code=501, detail="voice_host is not available")
    try:
        minted = await asyncio.to_thread(mint_token)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"voice token mint failed: {exc}") from exc
    if not isinstance(minted, dict):
        raise HTTPException(status_code=502, detail="voice token mint returned unexpected shape")
    # Always surface the pinned model id so the browser does not hardcode it.
    out = dict(minted)
    out.setdefault("model", config.MODEL_VOICE)
    return out


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    joined: tuple[str, str] | None = None
    try:
        while True:
            try:
                msg = await ws.receive_json()
            except WebSocketDisconnect:
                raise
            except Exception:
                continue
            if not isinstance(msg, dict):
                continue
            t = msg.get("t")
            room_id = str(msg.get("room", "")).strip()
            if not t or not room_id:
                continue

            if t == "join":
                name = str(msg.get("name", "")).strip() or "anon"
                room = _get_room(room_id)
                existing = room["players"].get(name)
                if existing is not None:
                    # Reconnect under the same name keeps score and streak.
                    existing.ws = ws
                else:
                    room["players"][name] = PlayerState(name=name, ws=ws)
                joined = (room_id, name)
                if room["host"] is None:
                    room["host"] = name
                if (
                    not room["arena"]
                    and room["phase"] == "lobby"
                    and len(room["players"]) >= 2
                ):
                    # Two players in a duel lobby auto-start the first round.
                    # Arena rooms never auto-start; the host starts on stage
                    # once the crowd has scanned in.
                    await _start_round(room)
                else:
                    await _broadcast(room)

            elif t == "guess":
                room = ROOMS.get(room_id)
                if room is None or room["phase"] != "guessing" or joined is None:
                    continue
                player = room["players"].get(joined[1])
                slot = msg.get("slot")
                if (
                    player is None
                    or player.guessed
                    or not isinstance(slot, int)
                    or isinstance(slot, bool)
                    or not 0 <= slot < config.REPLIES_PER_ROUND
                ):
                    continue
                player.guessed = True
                player.guess_slot = slot
                room["guess_counter"] += 1
                player.guess_order = room["guess_counter"]
                player.client_ms = msg.get("ms")
                if all(p.guessed for p in room["players"].values()):
                    await _do_reveal(room)
                else:
                    await _broadcast(room)

            elif t == "next":
                room = ROOMS.get(room_id)
                if room is None:
                    continue
                if room["arena"] and (joined is None or joined[1] != room["host"]):
                    # Only the stage host advances an arena. A scanned-in
                    # player tapping NEXT must not skip the room forward.
                    continue
                in_reveal = room["phase"] == "reveal"
                # A duel needs both players before the first round. An arena is
                # driven by the host, who must be able to open a round on stage
                # before anyone has scanned in. The client enables START at one
                # player, so requiring two here made the button silently dead.
                lobby_ready = room["phase"] == "lobby" and (
                    room["arena"] or len(room["players"]) >= 2
                )
                if in_reveal or lobby_ready:
                    await _start_round(room)

    except WebSocketDisconnect:
        pass
    finally:
        if joined is not None:
            room = ROOMS.get(joined[0])
            if room is not None:
                player = room["players"].get(joined[1])
                if player is not None and player.ws is ws:
                    room["players"].pop(joined[1], None)
                    if room["players"]:
                        if room["phase"] == "guessing" and all(
                            p.guessed for p in room["players"].values()
                        ):
                            await _do_reveal(room)
                        else:
                            await _broadcast(room)
                    else:
                        _cancel_timer(room)
                        ROOMS.pop(joined[0], None)


# Mounted last so /ws, /health, /token, /join-info, and /qr.png win the route
# match. An empty directory serves 404s, which is fine for a standalone run.
app.mount("/", StaticFiles(directory=str(REPO_ROOT / "web"), html=True), name="web")
