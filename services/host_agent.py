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
Never open with "Got it" or "Wrong". Vary the line every time.
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
        listener = str(obs.get("listener") or "").strip()
        correct = obs.get("correct")
        local_win = bool(correct) or (
            bool(w) and w != "house" and listener and str(w) == listener
        )
        seed = rn_i + (int(decoy_reply) if decoy_reply else 0) * 5

        if not w or w == "house":
            opts = [
                (
                    f"Nobody snagged it. Fake sat on reply {decoy_reply}."
                    if decoy_reply
                    else "Nobody snagged it. House takes the point."
                ),
                (
                    f"Clean miss. The bot hid in reply {decoy_reply}."
                    if decoy_reply
                    else "Clean miss. Machine walks."
                ),
                (
                    f"House cashes — decoy was reply {decoy_reply}."
                    if decoy_reply
                    else "House cashes this one."
                ),
                "The machine slips through. Point to the house.",
                (
                    f"Tough board. Reply {decoy_reply} was the imposter."
                    if decoy_reply
                    else "Tough board. House keeps it."
                ),
                "All humans fooled. Arcade laughs.",
                "Robot night. Nobody read the room.",
            ]
            return _pick_unused(opts, recent, seed)

        if local_win:
            opts = [
                (
                    f"You sniffed out reply {decoy_reply}. Point yours."
                    if decoy_reply
                    else "You sniffed it out. Point yours."
                ),
                (
                    f"Sharp eye — reply {decoy_reply} was the bot."
                    if decoy_reply
                    else "Sharp eye. That's a point."
                ),
                "Machine busted. Nice read.",
                (
                    f"You had the read. Fake was reply {decoy_reply}."
                    if decoy_reply
                    else "You had the read. Plus one."
                ),
                "Caught the imposter cold. Well played.",
                (
                    f"That's the one — reply {decoy_reply}. Clean pick."
                    if decoy_reply
                    else "That's the one. Clean pick."
                ),
                "Bot exposed. You take the board.",
                "Arcade nods. That was the tell.",
                (
                    f"Dead giveaway on reply {decoy_reply}. Point to you."
                    if decoy_reply
                    else "Dead giveaway. Point to you."
                ),
            ]
            return _pick_unused(opts, recent, seed + 11)

        who = str(w)
        opts = [
            (
                f"{who} nails it — decoy was reply {decoy_reply}."
                if decoy_reply
                else f"{who} nails it. Point theirs."
            ),
            (
                f"{who} had the read. Fake hid in reply {decoy_reply}."
                if decoy_reply
                else f"{who} had the read."
            ),
            f"Not this time — {who} got there first.",
            (
                f"Point to {who}. Reply {decoy_reply} was the machine."
                if decoy_reply
                else f"Point to {who}."
            ),
            f"{who} saw through the noise.",
            (
                f"{who} sniffs reply {decoy_reply}. Board goes their way."
                if decoy_reply
                else f"{who} sniffs the fake."
            ),
            f"Credit {who} — machine's busted.",
            f"Close, but {who} claimed the point.",
            (
                f"{who} calls reply {decoy_reply}. That's the decoy."
                if decoy_reply
                else f"{who} calls it clean."
            ),
        ]
        return _pick_unused(opts, recent, seed + len(who))
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


# Model sometimes dumps the system/user prompt instead of a host line.
# Reject anything that looks like instructions, JSON, or skill markdown.
_PROMPT_ECHO_RE = re.compile(
    r"(?i)"
    r"("
    r"you are the live commentator"
    r"|output only the (spoken )?line"
    r"|write the next commentator"
    r"|pre-?reveal safety"
    r"|never break these"
    r"|do not introduce yourself"
    r"|one short punchy sentence"
    r"|no hashtags,? no emojis"
    r"|recent_lines"
    r"|observation json"
    r"|\"instruction\"\s*:"
    r"|\"observation\"\s*:"
    r"|reply with only the spoken"
    r"|phase-aware"
    r"|sports-desk arcade"
    r"|never name the decoy"
    r"|pick_reply \(1-5\)"
    r"|##\s*(style|variety|what you may)"
    r"|^\s*[-*]\s+(never|do not|you may|one short)"
    r")"
)

_LABEL_PREFIX_RE = re.compile(
    r"(?i)^\s*("
    r"line|host|commentator|commentary|announcer|output|response|answer"
    r"|spoken line|say this|here'?s? (the|a) line"
    r")\s*[:\-–—]\s*"
)

_STAGE_DIR_RE = re.compile(r"^\s*[\(\[]?(pause|laughs?|cheers?|crowd|sfx)[\)\]]?\s*", re.I)


def _strip_wrapping_quotes(s: str) -> str:
    s = (s or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'“”‘’":
        return s[1:-1].strip()
    # Smart-quote pairs
    if len(s) >= 2 and s[0] in "“‘" and s[-1] in "”’":
        return s[1:-1].strip()
    return s


def _looks_like_prompt_echo(text: str) -> bool:
    """True if text is instructions/JSON/skill dump rather than a host line."""
    raw = (text or "").strip()
    if not raw:
        return True
    # Multi-line instruction dumps / markdown skill files
    if raw.count("\n") >= 2:
        return True
    if raw.lstrip().startswith(("{", "[", "```", "##", "###")):
        return True
    # JSON-ish observation echo
    if '"event"' in raw and ('"phase"' in raw or '"standings"' in raw):
        return True
    if _PROMPT_ECHO_RE.search(raw):
        return True
    # Bullet laundry list of rules
    if raw.count(" - ") >= 2 or raw.count("\n-") >= 2:
        return True
    # Way too long for arcade host (spoken line ~12 words)
    if len(raw) > 280:
        return True
    words = raw.split()
    if len(words) > 40:
        return True
    return False


def _candidate_lines(text: str) -> list[str]:
    """Split model output into candidate spoken lines (best first)."""
    raw = (text or "").strip()
    if not raw:
        return []
    # Drop fenced blocks entirely if the model wrapped junk
    raw = re.sub(r"```.*?```", " ", raw, flags=re.S)
    parts: list[str] = []
    for chunk in re.split(r"[\n\r]+", raw):
        chunk = chunk.strip()
        if not chunk:
            continue
        # Drop pure markdown headers / bullets of rules
        if re.match(r"^#{1,6}\s", chunk):
            continue
        if re.match(r"^[-*•]\s+(never|do not|you may|one short|no hashtags)", chunk, re.I):
            continue
        chunk = _LABEL_PREFIX_RE.sub("", chunk)
        chunk = _STAGE_DIR_RE.sub("", chunk)
        chunk = _strip_wrapping_quotes(chunk)
        chunk = re.sub(r"\s+", " ", chunk).strip()
        # Strip trailing label junk like " (line)" 
        chunk = re.sub(r"\s*\((line|spoken|output)\)\s*$", "", chunk, flags=re.I).strip()
        if chunk:
            parts.append(chunk)
    # If single blob with sentence breaks and first part is meta, try sentences
    if len(parts) == 1 and len(parts[0]) > 100:
        sents = re.split(r"(?<=[.!?])\s+", parts[0])
        if len(sents) > 1:
            parts = [s.strip() for s in sents if s.strip()] + parts
    return parts


def _clean_line(text: str) -> str:
    """Return one short speakable host line, or '' if nothing usable."""
    for cand in _candidate_lines(text):
        if _looks_like_prompt_echo(cand):
            continue
        line = cand
        if len(line) > 180:
            line = line[:180].rsplit(" ", 1)[0] or line[:180]
        line = line.strip()
        if len(line) >= 2:
            return line
    return ""


def line_is_safe(line: str, *, reveal: bool) -> bool:
    if not line or len(line) < 2:
        return False
    if _looks_like_prompt_echo(line):
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
    # Keep the user turn short and imperative so the model is less likely
    # to parrot a long instruction block or dump JSON keys out loud.
    bits = [
        "Speak the next DECOY host line for this moment.",
        "Reply with ONLY that one short sentence — no quotes, labels, JSON, or rules.",
    ]
    if avoid:
        bits.append("Do not repeat or paraphrase any recent_lines in the observation.")
    if obs.get("event") == "round_start":
        bits.append(
            "New round opener: fresh angle (scoreboard, topic, pressure, rivalry, or cold open)."
        )
    user_content = (
        "\n".join(bits)
        + "\n\nObservation:\n"
        + json.dumps(obs, ensure_ascii=False)
    )
    agent_model = getattr(config, "MODEL_AGENT", None) or config.MODEL_TEXT
    payload = {
        "model": agent_model,
        "temperature": 0.85 if reveal else 0.75,
        "max_tokens": 48,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
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
