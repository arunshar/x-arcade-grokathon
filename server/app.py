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

from fastapi import Body, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
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


# Historical: rooms used to split into host-driven arenas and auto-starting
# duels, and the mismatch between the two produced three dead-button bugs in
# one day. Every room is now time driven by the session clock and these
# fields are kept only so older clients reading them do not break.
ARENA_ROOMS = {"GROK"}

# Solo practice: room codes starting with SOLO (e.g. SOLO7K) can start with one
# player. After you guess, reveal fires immediately (all players have guessed).
# Or set ARCADE_ALLOW_SOLO=1 to allow any room to start with one player.
ALLOW_SOLO = os.environ.get("ARCADE_ALLOW_SOLO", "") == "1"


def _allows_solo(room: dict[str, Any]) -> bool:
    """True when a single player may start a round from the lobby."""
    if room.get("arena"):
        return True
    if ALLOW_SOLO:
        return True
    rid = str(room.get("room_id") or "").upper()
    return rid.startswith("SOLO")


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
            "rounds_played": 0,
            "arena": room_id in ARENA_ROOMS,
            "host": None,
            "auto_timer": None,
            "auto_deadline_at": None,
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
    decoy_rationale, no media_source. Replies are slot + text + media only.

    GIF rounds show looping media on every card (4 human GIFs + 1 Imagine
    video-as-gif). media_source is stripped so clients cannot tell which is AI.
    """
    rnd = room["round"]
    if rnd is None or room["phase"] != "guessing":
        return rnd
    strip_keys = (
        "decoy_slot",
        "decoy_rationale",
        "reply_art_status",
        "art_url",
        "art_status",
        "decoy_media_status",
    )
    safe = {k: v for k, v in rnd.items() if k not in strip_keys}
    safe_replies = []
    for r in rnd.get("replies") or []:
        if not isinstance(r, dict) or "slot" not in r:
            continue
        item: dict[str, Any] = {"slot": r["slot"], "text": r.get("text") or ""}
        # Motion media is safe during guessing — every slot has it.
        if r.get("media_url"):
            item["media_url"] = r["media_url"]
        if r.get("media_type"):
            item["media_type"] = r["media_type"]
        if r.get("media_status"):
            item["media_status"] = r["media_status"]
        # Never send media_source / is_decoy / author during guessing.
        safe_replies.append(item)
    safe["replies"] = safe_replies
    # Always advertise format so clients can mix text vs gif presentation.
    fmt = str(rnd.get("format") or "text").lower()
    safe["format"] = "gif" if fmt == "gif" else "text"
    # Text rounds must not leak leftover media fields.
    if safe["format"] != "gif":
        for item in safe["replies"]:
            item.pop("media_url", None)
            item.pop("media_type", None)
            item.pop("media_status", None)
    return safe


def _standings(room: dict[str, Any]) -> list[dict[str, Any]]:
    """Players ranked by score (desc), then name. Always safe to show clients."""
    ordered = sorted(
        room["players"].values(), key=lambda p: (-p.score, p.name.lower())
    )
    return [
        {
            "rank": i + 1,
            "name": p.name,
            "score": p.score,
            "streak": p.streak,
        }
        for i, p in enumerate(ordered)
    ]


def _public_state(room: dict[str, Any]) -> dict[str, Any]:
    # Who picked which reply is public at reveal and only at reveal. During
    # guessing it would leak strategy, so the strip rule keeps it server side.
    at_reveal = room["phase"] == "reveal"
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
                **({"guess_slot": p.guess_slot} if at_reveal else {}),
            }
            for p in room["players"].values()
        ],
        "auto_ms": _auto_ms(room),
        # Full ranked board every broadcast so lobby / guessing / reveal
        # can show who is ahead without waiting for the reveal strip.
        "standings": _standings(room),
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


def _cancel_auto(room: dict[str, Any]) -> None:
    timer = room.get("auto_timer")
    if timer is not None and not timer.done():
        timer.cancel()
    room["auto_timer"] = None
    room["auto_deadline_at"] = None


def _schedule_auto(room: dict[str, Any], delay: float) -> None:
    """Arm the session clock: the round machine advances itself.

    The game is time driven rather than host driven. A lobby with anyone in
    it rolls into a round, and a reveal rolls into the next round. Manual
    START / NEXT ROUND just skips the wait.
    """
    _cancel_auto(room)
    room["auto_deadline_at"] = asyncio.get_running_loop().time() + delay
    room["auto_timer"] = asyncio.create_task(_auto_advance(room, delay))


async def _auto_advance(room: dict[str, Any], delay: float) -> None:
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    if ROOMS.get(room["room_id"]) is not room or not room["players"]:
        return
    if room["phase"] in ("lobby", "reveal"):
        await _start_round(room)


def _auto_ms(room: dict[str, Any]) -> int | None:
    """Milliseconds until the session clock advances, for the countdown UI."""
    if room["phase"] not in ("lobby", "reveal") or room["auto_deadline_at"] is None:
        return None
    remaining = room["auto_deadline_at"] - asyncio.get_running_loop().time()
    return max(0, int(remaining * 1000))


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
    _cancel_auto(room)
    rnd = await _next_round()
    # Mix text rounds (classic replies) with GIF rounds (human gifs + Imagine).
    session_index = int(room.get("rounds_played") or 0)
    room["rounds_played"] = session_index + 1
    try:
        from services.reply_gifs import prepare_round_presentation

        prepare_round_presentation(rnd, session_index=session_index)
    except Exception as exc:
        print(f"prepare_round_presentation failed: {exc}", file=sys.stderr)
        rnd.setdefault("format", "text")
        rnd.setdefault("decoy_media_status", "none")
    room["round"] = rnd
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
    # Live GIF rounds only: forge a unique decoy Imagine clip in the background.
    if (
        config.MODE == "live"
        and rnd.get("format") == "gif"
        and rnd.get("decoy_media_status") != "ready"
    ):
        asyncio.create_task(_attach_decoy_imagine_gif(room, rnd))


async def _do_reveal(room: dict[str, Any]) -> None:
    _cancel_timer(room)
    rnd = room["round"]
    decoy_slot = rnd["decoy_slot"]
    # Winner is the first correct guess in server arrival order. The client's
    # self-reported ms is display data only and never decides the winner.
    correct = [p for p in room["players"].values() if p.guess_slot == decoy_slot]
    correct.sort(key=lambda p: p.guess_order if p.guess_order is not None else 1 << 30)
    winner = correct[0].name if correct else "house"
    # Scoring: first correct guess in server order gets +1 point and keeps a
    # streak. Everyone else resets streak. House wins award no points.
    points_awarded: list[dict[str, Any]] = []
    for p in room["players"].values():
        if p.name == winner:
            p.score += 1
            p.streak += 1
            points_awarded.append({"name": p.name, "delta": 1, "reason": "first_correct"})
        else:
            p.streak = 0
    room["phase"] = "reveal"
    room["deadline_at"] = None
    standings = _standings(room)
    room["reveal"] = {
        "decoy_slot": decoy_slot,
        "rationale": rnd.get("decoy_rationale", ""),
        "winner": winner,
        # Top of the crowd at every reveal. In a duel this is just both
        # players, in an arena it is the scoreboard beat on the big screen.
        "leaderboard": [
            {
                "rank": row["rank"],
                "name": row["name"],
                "score": row["score"],
                "streak": row["streak"],
            }
            for row in standings[:5]
        ],
        "points_awarded": points_awarded,
        # Demo mode attaches the committed card instantly. Live mode starts
        # with no card and a background task fills it in a few seconds later.
        "share_card_url": None if config.MODE == "live" else DEMO_CARD_URL,
    }
    # Arm the session clock: the next round starts itself after the reveal
    # has had time to land. Anyone tapping NEXT ROUND just skips the wait.
    _schedule_auto(room, config.REVEAL_SECONDS)
    await _broadcast(room)
    if config.MODE == "live":
        asyncio.create_task(_attach_live_card(room, rnd, winner))
        if (
            rnd.get("format") == "gif"
            and rnd.get("decoy_media_status") != "ready"
        ):
            asyncio.create_task(_attach_decoy_imagine_gif(room, rnd))


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


async def _attach_decoy_imagine_gif(room: dict[str, Any], rnd: dict[str, Any]) -> None:
    """Imagine agent: study human GIFs on this round → matching decoy video.

    Uses vision style-brief + reference_images so the robot loop blends with
    the four human reaction GIFs. Safe to broadcast during guessing once ready
    (media_source stays stripped in _round_view). No-op for text rounds.
    """
    if room.get("round") is not rnd:
        return
    if rnd.get("format") != "gif":
        return
    try:
        # Prefer the full agent; fall back to reply_gifs wrapper.
        from services.imagine_agent import generate_matching_decoy as _gen
    except ImportError:
        try:
            from services.reply_gifs import generate_decoy_media as _gen
        except ImportError as exc:
            print(f"imagine agent import failed: {exc}", file=sys.stderr)
            return

    try:
        await asyncio.to_thread(_gen, rnd)
    except Exception as exc:
        rid = str(rnd.get("round_id") or "round")
        print(f"decoy imagine gif {rid} failed: {exc}", file=sys.stderr)
        rnd["decoy_media_status"] = "failed"
        return

    if room.get("round") is rnd and room["phase"] in ("guessing", "reveal"):
        await _broadcast(room)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "mode": config.MODE,
        "rounds_available": _rounds_available(),
        "voice_model": config.MODEL_VOICE,
        "tts_voice": getattr(config, "TTS_VOICE", "eve"),
        "image_model": config.MODEL_IMAGE,
        "video_model": getattr(config, "MODEL_VIDEO", ""),
        "gif_round_mode": getattr(config, "GIF_ROUND_MODE", "alternate"),
    }


@app.get("/voices")
async def list_voices() -> dict[str, Any]:
    """List Grok TTS voices (live) or a short offline catalog (demo)."""
    catalog = [
        {"voice_id": "eve", "name": "Eve", "blurb": "Energetic default"},
        {"voice_id": "helix", "name": "Helix", "blurb": "Bold commentary energy"},
        {"voice_id": "sirius", "name": "Sirius", "blurb": "Playful and witty"},
        {"voice_id": "leo", "name": "Leo", "blurb": "Authoritative host"},
        {"voice_id": "rex", "name": "Rex", "blurb": "Clear PA announcer"},
        {"voice_id": "ara", "name": "Ara", "blurb": "Warm and friendly"},
        {"voice_id": "orion", "name": "Orion", "blurb": "Cinematic narrator"},
        {"voice_id": "iris", "name": "Iris", "blurb": "Upbeat and charming"},
    ]
    current = getattr(config, "TTS_VOICE", "eve")
    if config.MODE != "live":
        return {"voices": catalog, "current": current, "source": "offline_catalog"}
    try:
        from services.xai_http import post_json  # noqa: F401 — ensure module loads
        import json
        import ssl
        import urllib.request

        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
        key = os.environ.get("XAI_API_KEY", "")
        req = urllib.request.Request(
            config.API_BASE + "/tts/voices",
            headers={"Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            data = json.loads(resp.read())
        voices = data.get("voices") or data
        return {"voices": voices, "current": current, "source": "xai"}
    except Exception as exc:
        return {
            "voices": catalog,
            "current": current,
            "source": "offline_catalog",
            "detail": str(exc),
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


@app.post("/agent/commentate")
async def agent_commentate(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Host agent: safe observation → one spoken line (Grok text).

    The client then plays the line with Grok Voice (/tts or realtime).
    Observation must not include decoy secrets; the agent also strips them.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON object required")
    try:
        from services.host_agent import generate_line
    except ImportError:
        raise HTTPException(status_code=501, detail="host_agent is not available")
    # Always run generate_line — it falls back cleanly in demo mode.
    result = await asyncio.to_thread(generate_line, payload)
    return result


@app.post("/tts")
async def tts_line(payload: dict[str, Any] = Body(...)) -> Response:
    """Grok Voice TTS for dynamic commentator lines (Eve / en → mp3).

    Live mode only — needs XAI_API_KEY. The browser posts short safe lines
    (leads, locks, winners); never send secrets or decoy answers here.
    """
    if config.MODE != "live":
        raise HTTPException(
            status_code=503,
            detail="Grok TTS needs ARCADE_MODE=live and XAI_API_KEY",
        )
    text = ""
    if isinstance(payload, dict):
        text = str(payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    if len(text) > 400:
        raise HTTPException(status_code=400, detail="text too long (max 400)")
    try:
        from services.voice_host import synthesize
    except ImportError:
        raise HTTPException(status_code=501, detail="voice_host is not available")
    try:
        audio = await asyncio.to_thread(synthesize, text)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Grok TTS failed: {exc}") from exc
    return Response(
        content=audio,
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-store"},
    )


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
                # No host. The session clock runs the room: the first player
                # in a lobby arms the countdown, and solo play is a real game
                # against the house. Later joiners land in whatever phase is
                # running and play the next beat.
                if room["phase"] == "lobby" and room["auto_timer"] is None:
                    _schedule_auto(room, config.LOBBY_SECONDS)
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
                if room is None or joined is None:
                    continue
                # Any joined player may skip the wait. The session clock will
                # advance on its own either way, so this button can never be
                # load bearing and never dead.
                if room["phase"] in ("lobby", "reveal") and room["players"]:
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
                        _cancel_auto(room)
                        ROOMS.pop(joined[0], None)


# Mounted last so /ws, /health, /token, /tts, /join-info, and /qr.png win the
# route match. An empty directory serves 404s, which is fine standalone.
app.mount("/", StaticFiles(directory=str(REPO_ROOT / "web"), html=True), name="web")
