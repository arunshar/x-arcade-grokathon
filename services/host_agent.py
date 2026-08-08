"""Decoy host / commentator agent.

Brain half of Grok Voice commentary:

  phase-aware observation
       → Grok chat (one short line), time-capped
       → client may speak it AFTER the hard mp3 stingers

Guessing/lobby observations stay spoiler-free. At reveal the decoy is already
public, so the agent may see decoy_slot, rationale, and reply texts and be funny.

CLI:
    ARCADE_MODE=live python3 services/host_agent.py \\
      '{"event":"reveal","phase":"reveal","winner":"NEON","decoy_slot":2}'
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import config  # noqa: E402
from services.xai_http import post_json  # noqa: E402

# Client should use a tighter cap; this is the server-side socket ceiling.
CHAT_TIMEOUT_S = float(__import__("os").environ.get("ARCADE_AGENT_TIMEOUT", "2.5"))

# Source of truth for how the host sounds — edit the skill, not a second copy here.
_SKILL_REF = (
    REPO_ROOT / ".grok" / "skills" / "decoy-voice-host" / "references"
)

# Spoilers only blocked pre-reveal. At reveal these words are fair game.
_SPOILER_RE = re.compile(
    r"\b(decoy|imposter|impostor|robot|fake reply|slot\s*[0-4]|"
    r"reply\s*[1-5]\s+is|the answer is|machine wrote|grok wrote)\b",
    re.I,
)

# In-code fallbacks if the skill files are missing (deploy without .grok/).
_FALLBACK_GUESSING = """You are the live commentator for DECOY.
PRE-REVEAL: never name the decoy/robot/answer. pick_reply is only which card was tapped.
ONE short punchy sentence. Output ONLY the spoken line."""

_FALLBACK_REVEAL = """You are the live commentator for DECOY at REVEAL.
The decoy is public — be funny and specific. ONE short punchy sentence.
Output ONLY the spoken line."""


def _load_skill_prompt(name: str, fallback: str) -> str:
    """Load references/<name>.md from the decoy-voice-host skill."""
    path = _SKILL_REF / f"{name}.md"
    try:
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    except OSError:
        pass
    return fallback


def system_prompt_for(*, reveal: bool) -> str:
    """Phase-aware system prompt from the skill (or fallback)."""
    if reveal:
        return _load_skill_prompt("reveal", _FALLBACK_REVEAL)
    return _load_skill_prompt("guessing", _FALLBACK_GUESSING)


def _is_reveal(obs_or_raw: dict[str, Any]) -> bool:
    phase = str(obs_or_raw.get("phase") or "").lower()
    event = str(obs_or_raw.get("event") or "").lower()
    return phase == "reveal" or event == "reveal"


def sanitize_observation(raw: dict[str, Any]) -> dict[str, Any]:
    """Phase-aware sanitize: strict pre-reveal, open at reveal."""
    event = str(raw.get("event") or "tick").strip()[:64]
    phase = str(raw.get("phase") or "").strip()[:32]
    reveal = _is_reveal({"phase": phase, "event": event})

    round_no = raw.get("round")
    try:
        round_no = int(round_no) if round_no is not None else None
    except (TypeError, ValueError):
        round_no = None

    deadline_ms = raw.get("deadline_ms")
    try:
        deadline_ms = int(deadline_ms) if deadline_ms is not None else None
    except (TypeError, ValueError):
        deadline_ms = None

    standings: list[dict[str, Any]] = []
    for row in raw.get("standings") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()[:24]
        if not name:
            continue
        try:
            score = int(row.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        try:
            streak = int(row.get("streak") or 0)
        except (TypeError, ValueError):
            streak = 0
        try:
            rank = int(row.get("rank") or 0)
        except (TypeError, ValueError):
            rank = 0
        standings.append(
            {
                "rank": rank or len(standings) + 1,
                "name": name,
                "score": score,
                "streak": streak,
            }
        )
    standings = standings[:12]

    locked: list[str] = []
    for name in raw.get("just_locked") or []:
        n = str(name).strip()[:24]
        if n:
            locked.append(n)
    locked = locked[:12]

    winner = raw.get("winner")
    if winner is not None:
        winner = str(winner).strip()[:24] or None

    pick_reply = raw.get("pick_reply")
    try:
        pick_reply = int(pick_reply) if pick_reply is not None else None
    except (TypeError, ValueError):
        pick_reply = None
    if pick_reply is not None and not 1 <= pick_reply <= 5:
        pick_reply = None

    picker = raw.get("picker")
    if picker is not None:
        picker = str(picker).strip()[:24] or None

    correct = raw.get("correct")
    if correct is not None:
        correct = bool(correct)

    recent: list[str] = []
    for line in raw.get("recent_lines") or []:
        t = str(line or "").strip()
        if t:
            recent.append(t[:160])
    recent = recent[-8:]

    topic = raw.get("topic")
    if topic is not None:
        topic = str(topic).strip()[:48] or None

    out: dict[str, Any] = {
        "event": event,
        "phase": phase,
        "round": round_no,
        "deadline_ms": deadline_ms,
        "standings": standings,
        "just_locked": locked,
        "winner": winner,
        "listener": str(raw.get("listener") or "")[:24] or None,
        "pick_reply": pick_reply,
        "picker": picker,
        "correct": correct,
        "reveal_open": reveal,
        "recent_lines": recent,
        "topic": topic,
    }

    if reveal:
        # Decoy is public on the reveal screen — let the host be funny.
        decoy_slot = raw.get("decoy_slot")
        try:
            decoy_slot = int(decoy_slot) if decoy_slot is not None else None
        except (TypeError, ValueError):
            decoy_slot = None
        if decoy_slot is not None and 0 <= decoy_slot <= 4:
            out["decoy_slot"] = decoy_slot
            out["decoy_reply"] = decoy_slot + 1  # 1-based for speech

        rationale = raw.get("rationale") or raw.get("decoy_rationale")
        if isinstance(rationale, str) and rationale.strip():
            out["decoy_rationale"] = rationale.strip()[:280]

        replies_out: list[dict[str, Any]] = []
        for rep in raw.get("replies") or []:
            if not isinstance(rep, dict):
                continue
            try:
                slot = int(rep.get("slot"))
            except (TypeError, ValueError):
                continue
            text = str(rep.get("text") or "").strip()
            if len(text) > 160:
                text = text[:157] + "..."
            replies_out.append(
                {
                    "slot": slot,
                    "reply": slot + 1,
                    "text": text,
                    "author": str(rep.get("author") or "")[:32],
                    "is_decoy": bool(rep.get("is_decoy")),
                }
            )
        if replies_out:
            out["replies"] = replies_out[:5]
    # else: deliberately omit decoy_slot / replies / rationale

    return out


def _pick_unused(options: list[str], recent: list[str], salt: int = 0) -> str:
    """Rotate through options, skipping anything close to a recent line."""
    recent_l = [r.lower() for r in recent]
    fresh = [o for o in options if o.lower() not in recent_l]
    pool = fresh or options
    if not pool:
        return "New round. Fresh eyes."
    idx = abs(salt) % len(pool)
    return pool[idx]


def _fallback_line(obs: dict[str, Any]) -> str:
    """Deterministic line when the model is slow, down, or returns junk."""
    event = obs.get("event") or ""
    standings = obs.get("standings") or []
    top = standings[0] if standings else None
    recent = list(obs.get("recent_lines") or [])
    rn = obs.get("round") or 1
    try:
        rn_i = int(rn)
    except (TypeError, ValueError):
        rn_i = 1
    topic = obs.get("topic") or "this thread"
    leader = top["name"] if top else "the field"
    score = int(top["score"]) if top and top.get("score") is not None else 0

    if event == "lobby_join":
        n = len(standings)
        if n <= 1:
            return _pick_unused(
                [
                    "Lobby is live. Waiting on a challenger.",
                    "Cabinet's warm. Need one more body.",
                    "Open lobby. Who's stepping up?",
                ],
                recent,
                n,
            )
        names = ", ".join(r["name"] for r in standings[:4])
        return _pick_unused(
            [
                f"{n} players in the room. {names}.",
                f"Board's filling up: {names}.",
                f"Crowd check — {names} are in.",
            ],
            recent,
            n,
        )
    if event == "round_start":
        opts = [
            f"Round {rn_i}. {leader} sits on {score}." if score else f"Round {rn_i}. Fresh board, no leader yet.",
            f"Round {rn_i} on {topic}. Tap fast.",
            f"New deal, round {rn_i}. Don't blink.",
            f"Round {rn_i}. Pressure's on {leader}." if score else f"Round {rn_i}. First blood's open.",
            f"Clock's live for round {rn_i}. Hunt the fake.",
            f"Round {rn_i}. {leader} has the belt at {score}." if score else f"Round {rn_i}. Clean slate.",
            f"Shuffle up. Round {rn_i} on {topic}.",
            f"Round {rn_i}. Make it count.",
        ]
        return _pick_unused(opts, recent, rn_i + score)
    if event in ("player_lock", "player_pick"):
        who = obs.get("picker") or ((obs.get("just_locked") or [None])[0]) or "Someone"
        n = obs.get("pick_reply")
        card = f" reply {n}" if n else ""
        return _pick_unused(
            [
                f"{who} locks{card}.",
                f"{who} slams{card}.",
                f"Locked in — {who}{card}.",
                f"{who} commits{card}.",
            ],
            recent,
            hash(str(who) + str(n)) % 97,
        )
    if event == "clock_low":
        return _pick_unused(
            ["Ten seconds!", "Clock's screaming — ten left!", "Final ten. Decide!", "Ten on the clock!"],
            recent,
            rn_i,
        )
    if event == "reveal":
        w = obs.get("winner")
        decoy_reply = obs.get("decoy_reply")
        if not w or w == "house":
            opts = [
                f"House wins. The decoy was reply {decoy_reply}." if decoy_reply else "House takes it.",
                f"Nobody had it. Decoy hid in reply {decoy_reply}." if decoy_reply else "House keeps the point.",
                "Machine walks. House cashes.",
            ]
            return _pick_unused(opts, recent, rn_i)
        if decoy_reply:
            opts = [
                f"{w} called it — decoy was reply {decoy_reply}.",
                f"{w} sniffs out reply {decoy_reply}. Plus one.",
                f"Point to {w}. Fake lived in reply {decoy_reply}.",
            ]
        else:
            opts = [
                f"{w} called it! Plus one.",
                f"{w} takes the round.",
                f"Board goes to {w}.",
            ]
        return _pick_unused(opts, recent, rn_i + len(str(w)))
    if top and top.get("score", 0) > 0:
        return f"{top['name']} leads with {top['score']} points."
    return _pick_unused(
        ["Eyes on the replies.", "Stay sharp.", "Don't sleep on this board."],
        recent,
        rn_i,
    )

def _extract_chat_text(response: dict[str, Any]) -> str:
    choices = response.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("text", "output_text"):
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(parts).strip()
    return str(content or "").strip()


def _clean_line(text: str) -> str:
    line = (text or "").strip()
    if len(line) >= 2 and line[0] == line[-1] and line[0] in "\"'":
        line = line[1:-1].strip()
    line = re.sub(r"\s+", " ", line.split("\n")[0]).strip()
    if len(line) > 220:
        line = line[:220].rsplit(" ", 1)[0] or line[:220]
    return line


def line_is_safe(line: str, *, reveal: bool) -> bool:
    if not line or len(line) < 2:
        return False
    if reveal:
        return True
    if _SPOILER_RE.search(line):
        return False
    return True


def generate_line(observation: dict[str, Any]) -> dict[str, Any]:
    """Run one agent turn. Returns {line, source, event, latency_ms?}."""
    obs = sanitize_observation(observation)
    event = obs["event"]
    reveal = bool(obs.get("reveal_open"))
    fallback = _fallback_line(obs)

    if config.MODE != "live":
        return {"line": fallback, "source": "fallback_demo", "event": event}

    system = system_prompt_for(reveal=reveal)
    avoid = obs.get("recent_lines") or []
    instruction = "Write the next commentator line for this moment."
    if avoid:
        instruction += (
            " Do NOT repeat or paraphrase any recent_lines. "
            "Make this line clearly different from all of them."
        )
    if obs.get("event") == "round_start":
        instruction += (
            " This is a NEW ROUND opener — invent a fresh angle "
            "(scoreboard, topic, pressure, rivalry, or cold open)."
        )
    user_blob = {
        "instruction": instruction,
        "observation": obs,
    }
    agent_model = getattr(config, "MODEL_AGENT", None) or config.MODEL_TEXT
    payload = {
        "model": agent_model,
        "temperature": 0.85 if reveal else 0.75,
        "max_tokens": 60,
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": json.dumps(user_blob, ensure_ascii=False),
            },
        ],
    }
    t0 = time.perf_counter()
    try:
        response = post_json("/chat/completions", payload, timeout=CHAT_TIMEOUT_S)
        latency_ms = int((time.perf_counter() - t0) * 1000)
        line = _clean_line(_extract_chat_text(response))
        if not line_is_safe(line, reveal=reveal):
            return {
                "line": fallback,
                "source": "fallback_unsafe",
                "event": event,
                "latency_ms": latency_ms,
            }
        return {
            "line": line,
            "source": "agent",
            "event": event,
            "model": agent_model,
            "latency_ms": latency_ms,
        }
    except Exception as exc:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return {
            "line": fallback,
            "source": "fallback_error",
            "event": event,
            "model": agent_model,
            "detail": str(exc)[:200],
            "latency_ms": latency_ms,
        }


if __name__ == "__main__":
    raw: dict[str, Any]
    if len(sys.argv) > 1:
        raw = json.loads(sys.argv[1])
    else:
        raw = {
            "event": "reveal",
            "phase": "reveal",
            "round": 2,
            "winner": "NEON",
            "decoy_slot": 2,
            "rationale": "Too balanced and corporate.",
            "standings": [
                {"rank": 1, "name": "NEON", "score": 2, "streak": 2},
                {"rank": 2, "name": "VOLT", "score": 1, "streak": 0},
            ],
            "replies": [
                {"slot": 0, "text": "real dunk", "author": "@a", "is_decoy": False},
                {"slot": 1, "text": "real joke", "author": "@b", "is_decoy": False},
                {
                    "slot": 2,
                    "text": "This raises important questions about the discourse.",
                    "author": "decoy",
                    "is_decoy": True,
                },
            ],
        }
    print(json.dumps(generate_line(raw), indent=2))
