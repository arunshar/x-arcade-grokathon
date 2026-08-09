"""Varied "why it's the robot" copy for the reveal screen.

Stored ``decoy_rationale`` strings from round build tend to collapse into the
same "too polished / balanced / explanatory" beat. At reveal we rebuild a
fresh one-liner from the decoy text (and optional live agent color) so each
round's tell feels different.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

# Patterns that mark common Grok-tells in short replies.
_HEDGE = re.compile(
    r"\b(however|although|that said|on the other hand|both|neither|"
    r"it('s| is) worth|important to|nuanced|complex|depends|"
    r"not (just|only)|while also|in (many|some) ways)\b",
    re.I,
)
_ESSAY = re.compile(
    r"\b(this (raises|highlights|underscores|speaks to)|"
    r"raises important|in conclusion|overall|furthermore|"
    r"additionally|moreover|ultimately)\b",
    re.I,
)
_CORPORATE = re.compile(
    r"\b(leverage|synergy|stakeholders|ecosystem|robust|seamless|"
    r"best[- ]in[- ]class|unlock|empower|optimize|paradigm|"
    r"innovative|cutting[- ]edge|game[- ]changer)\b",
    re.I,
)
_POLITE = re.compile(
    r"\b(great (point|question|catch)|love this|fascinating|"
    r"really appreciate|thanks for sharing|couldn't agree more)\b",
    re.I,
)
_QUESTION = re.compile(r"\?")
_EMOTE = re.compile(r"[\U0001F300-\U0001FAFF]|[:;]-?[)(/DpP]|lol|lmao|omg|fr\b|ngl\b", re.I)
_QUOTE = re.compile(r'[“"\'][^”"\']{8,}[”"\']')


def _snip(text: str, n: int = 42) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if len(t) <= n:
        return t
    cut = t[: n - 1].rsplit(" ", 1)[0]
    return (cut or t[: n - 1]) + "…"


def _features(text: str) -> dict[str, bool | int]:
    t = text or ""
    words = t.split()
    return {
        "len": len(t),
        "words": len(words),
        "hedge": bool(_HEDGE.search(t)),
        "essay": bool(_ESSAY.search(t)),
        "corp": bool(_CORPORATE.search(t)),
        "polite": bool(_POLITE.search(t)),
        "question": bool(_QUESTION.search(t)),
        "emote": bool(_EMOTE.search(t)),
        "quote": bool(_QUOTE.search(t)),
        "long": len(words) >= 28,
        "short": len(words) <= 6,
    }


def _angles(decoy: str, feat: dict[str, bool | int], slot: int | None) -> list[str]:
    """Candidate one-liners — pick one that matches the text's tells."""
    bit = _snip(decoy, 40)
    slot_bit = f"Reply {slot + 1}" if isinstance(slot, int) else "This reply"
    opts: list[str] = []

    if feat["essay"]:
        opts += [
            f"{slot_bit} opens like a mini-essay — real posters dunk first.",
            f"Sounds like a thesis defense: “{bit}” Real X is messier.",
            f"{slot_bit} lectures the thread. Humans usually just react.",
        ]
    if feat["hedge"]:
        opts += [
            f"Too carefully balanced — both-sides energy on “{bit}”.",
            f"{slot_bit} hedges like a press release. Street replies pick a side.",
            f"Neat little “however” sandwich. That's the tell.",
        ]
    if feat["corp"]:
        opts += [
            f"Corporate buzzword fog: “{bit}”. Nobody talks like that under a meme.",
            f"{slot_bit} reads like LinkedIn crashed the quote-tweet.",
            f"Slide-deck vocabulary in a reply chain. Robot fingerprints.",
        ]
    if feat["polite"] and not feat["emote"]:
        opts += [
            f"Polite seminar claps — “{bit}” — with zero chaos. Suspicious.",
            f"{slot_bit} is all manners, no bite. Real accounts get spicier.",
        ]
    if feat["long"]:
        opts += [
            f"Way too complete for a reply. “{bit}” is a paragraph in disguise.",
            f"{slot_bit} over-explains. Humans leave thoughts half-finished.",
            f"Full topic sentence + landing. That's homework energy.",
        ]
    if feat["short"] and not feat["emote"]:
        opts += [
            f"Sterile short take — no slang, no heat. “{bit}”.",
            f"{slot_bit} is clean to a fault. Real short posts still have grit.",
        ]
    if feat["question"] and feat["words"] > 12:
        opts += [
            f"Rhetorical question with a tidy setup: “{bit}”. Feels workshopped.",
            f"{slot_bit} asks like a moderator, not a fan in the replies.",
        ]
    if not feat["emote"] and feat["words"] > 10:
        opts += [
            f"Zero slang, zero mess — “{bit}”. That's the polish tell.",
            f"{slot_bit} never stubs a toe. Real posters typo and spiral.",
        ]
    if feat["quote"]:
        opts += [
            f"Quotes itself tidy: “{bit}”. Feels composed, not blurted.",
        ]

    # Always-available generic angles so we never return empty.
    opts += [
        f"{slot_bit} is oddly smooth: “{bit}”. Missing the human friction.",
        f"The cadence is too even. Real replies stumble; this one glides.",
        f"No personal stake — just tidy commentary. That's the robot.",
        f"Reads like it was optimized to offend nobody. Instant tell.",
        f"Too generic to be a person with a timeline. “{bit}”.",
        f"{slot_bit} blends in until you hear the plastic: “{bit}”.",
        f"Safe summary energy. Humans pick a fight or a joke.",
        f"The grammar is perfect and the soul is missing.",
        f"Looks like a reply. Sounds like a briefing note.",
        f"No scars, no slang, no side quests — machine-clean.",
    ]
    # De-dupe preserving order.
    seen: set[str] = set()
    uniq: list[str] = []
    for o in opts:
        k = o.lower()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(o)
    return uniq


def _pick(options: list[str], *, seed: str, avoid: list[str] | None = None) -> str:
    avoid_l = {a.strip().lower() for a in (avoid or []) if a}
    pool = [o for o in options if o.strip().lower() not in avoid_l] or list(options)
    if not pool:
        return "Too smooth to be human — that's the tell."
    h = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16)
    return pool[h % len(pool)]


def craft_reveal_rationale(
    round_data: dict[str, Any] | None,
    *,
    avoid: list[str] | None = None,
    seed_extra: str = "",
) -> str:
    """Build a varied reveal tell from the decoy reply text."""
    rnd = round_data or {}
    try:
        slot = int(rnd.get("decoy_slot"))
    except (TypeError, ValueError):
        slot = None
    decoy_text = ""
    for rep in rnd.get("replies") or []:
        if not isinstance(rep, dict):
            continue
        try:
            is_d = int(rep.get("slot")) == slot if slot is not None else bool(rep.get("is_decoy"))
        except (TypeError, ValueError):
            is_d = bool(rep.get("is_decoy"))
        if is_d:
            decoy_text = str(rep.get("text") or "")
            break
    if not decoy_text:
        decoy_text = str(rnd.get("decoy_text") or "")

    stored = str(rnd.get("decoy_rationale") or "").strip()
    feat = _features(decoy_text)
    rid = str(rnd.get("round_id") or "round")
    seed = f"{rid}|{slot}|{decoy_text[:40]}|{seed_extra}"
    options = _angles(decoy_text, feat, slot)

    # Prefer fresh angles; fall back to stored only if everything collides.
    line = _pick(options, seed=seed, avoid=avoid)
    if not line and stored:
        return stored[:220]
    # Soft blend: if stored is short and unique, sometimes surface its core.
    if (
        stored
        and len(stored) < 120
        and stored.lower() not in {a.lower() for a in (avoid or [])}
        and "polished" not in stored.lower()
        and int(hashlib.sha256((seed + "|mix").encode()).hexdigest()[:2], 16) % 5 == 0
    ):
        return stored
    return (line or stored or "Too smooth to be human — that's the tell.")[:220]


def agent_reveal_rationale(
    round_data: dict[str, Any] | None,
    *,
    avoid: list[str] | None = None,
) -> str | None:
    """Optional live one-liner via the fast agent model. None on failure."""
    try:
        import config
        from services.xai_http import post_json
    except Exception:
        return None
    if getattr(config, "MODE", "demo") != "live":
        return None

    rnd = round_data or {}
    try:
        slot = int(rnd.get("decoy_slot"))
    except (TypeError, ValueError):
        slot = None
    decoy_text = ""
    humans: list[str] = []
    for rep in rnd.get("replies") or []:
        if not isinstance(rep, dict):
            continue
        text = str(rep.get("text") or "").strip()
        if not text:
            continue
        try:
            is_d = int(rep.get("slot")) == slot if slot is not None else bool(rep.get("is_decoy"))
        except (TypeError, ValueError):
            is_d = bool(rep.get("is_decoy"))
        if is_d:
            decoy_text = text
        else:
            humans.append(text[:100])
    if not decoy_text:
        return None

    avoid_bits = "; ".join((avoid or [])[-4:]) or "(none)"
    prompt = (
        "You write the REVEAL tell for DECOY (arcade game). "
        "One short punchy sentence (under 22 words) explaining why THIS reply "
        "is the robot. Be specific to the wording. "
        "Do NOT say 'too polished' or 'too balanced' unless nothing else fits. "
        "Vary the angle: slang gap, essay tone, corporate fog, no stakes, "
        "over-complete, fake enthusiasm, etc. "
        "No hashtags, no quotes around the whole line, no preamble.\n\n"
        f"Decoy reply: {decoy_text[:280]}\n"
        f"Human vibe samples: {' | '.join(humans[:3]) or 'n/a'}\n"
        f"Avoid repeating: {avoid_bits}\n"
        "Output ONLY the sentence."
    )
    model = getattr(config, "MODEL_AGENT", None) or config.MODEL_TEXT
    try:
        resp = post_json(
            "/chat/completions",
            {
                "model": model,
                "temperature": 0.95,
                "max_tokens": 60,
                "messages": [
                    {
                        "role": "system",
                        "content": "You write one sharp reveal tell. Output only that sentence.",
                    },
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=2.2,
        )
    except Exception:
        return None
    choices = resp.get("choices") or []
    if not choices:
        return None
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                parts.append(str(b.get("text") or ""))
            else:
                parts.append(str(b))
        content = " ".join(parts)
    line = re.sub(r"\s+", " ", str(content or "").strip())
    if len(line) >= 2 and line[0] == line[-1] and line[0] in "\"'“”":
        line = line[1:-1].strip()
    # Reject stock collapse.
    low = line.lower()
    if not line or len(line) < 12:
        return None
    if low.startswith("too polished") or "too balanced and" in low:
        return None
    return line[:220]
