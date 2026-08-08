"""Build Decoy rounds: one real X thread plus one Grok-written imposter reply.

Round shape is defined in CONTRACT.md. The live path makes three xAI calls:

1. /v1/responses with the x_search tool finds one engaging recent post on the
   topic, returned as structured JSON.
2. /v1/responses with the x_search tool opens that exact thread (the
   conversation_id search operator) and reads at least 4 real replies verbatim.
   A single combined call was tried first and the model invented plausible
   replies instead of reading the thread, so post and replies stay separate
   grounded calls, and any response that made no x_search call is rejected.
3. /v1/chat/completions writes the single imposter reply in the register of the
   thread, plus a one sentence display-only rationale.

Both calls go through fixtures_core.FixtureStore so ARCADE_RECORD=1 captures
real responses into fixtures/api/ and demo mode replays them offline. Rounds
built for the demo are committed under cartridges/decoy/rounds/ and served by
queue.py, because x_search latency (about 42s measured) must never sit inline
in a request path.

CLI, run from anywhere:

    ARCADE_MODE=live ARCADE_RECORD=1 python3 cartridges/decoy/round_builder.py --live --topic ai
    ARCADE_MODE=live ARCADE_RECORD=1 python3 cartridges/decoy/round_builder.py --live --all
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config
from fixtures_core import FixtureMissError, FixtureStore

ROUNDS_DIR = Path(__file__).resolve().parent / "rounds"
ROUND_ID_SALT = "x-arcade-decoy-v1"
DEMO_TOPICS = ["ai", "sports", "movies", "crypto", "food", "music"]

POST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "post_id": {
            "type": "string",
            "description": "Numeric status id of the post",
        },
        "post_text": {"type": "string"},
        "post_author": {"type": "string", "description": "Handle like @name"},
        "post_url": {
            "type": "string",
            "description": "Canonical https://x.com/.../status/... url",
        },
    },
    "required": ["post_id", "post_text", "post_author", "post_url"],
    "additionalProperties": False,
}

REPLIES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "replies": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "author": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["author", "text"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["replies"],
    "additionalProperties": False,
}

DECOY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reply_text": {"type": "string"},
        "rationale": {
            "type": "string",
            "description": "One sentence naming the subtle artificial tell",
        },
    },
    "required": ["reply_text", "rationale"],
    "additionalProperties": False,
}

WRITER_SYSTEM = (
    "You write one fake reply that must hide among real replies to a real X post. "
    "Match the register of the thread exactly: typical reply length, tone, "
    "capitalization, punctuation habits, typo level, and slang. Stay on topic and "
    "plausible on its own. Do not copy phrases from the real replies. Never hint "
    "that you are an AI. Also write a one sentence rationale naming the subtle "
    "tell that makes your reply artificial. Players see the rationale only after "
    "the reveal."
)


class RoundBuildError(RuntimeError):
    """Raised when a topic cannot produce a valid round."""


# ---------------------------------------------------------------------------
# HTTP and fixture plumbing
# ---------------------------------------------------------------------------


def _http_json(path: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    key = os.environ.get("XAI_API_KEY", "")
    if not key:
        raise RoundBuildError("XAI_API_KEY is not set, live mode needs it")
    request = urllib.request.Request(
        config.API_BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        body = error.read()[:600].decode(errors="replace")
        raise RoundBuildError(f"xAI {path} returned {error.code}: {body}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise RoundBuildError(f"xAI {path} failed: {error}") from error


def _make_store(live: bool) -> FixtureStore | None:
    """Map ARCADE env vars onto the ported fixture layer.

    Replay is the default. live plus ARCADE_RECORD=1 records. live without
    ARCADE_RECORD calls the API directly and persists nothing, which is what
    the contract says live mode means.
    """
    if live and not config.RECORD:
        return None
    return FixtureStore(
        root=REPO_ROOT / "fixtures" / "api",
        record=live and config.RECORD,
        reuse_existing=os.environ.get("ARCADE_REUSE_FIXTURES") == "1",
    )


def _call_surface(
    store: FixtureStore | None,
    surface: str,
    payload: dict[str, Any],
    path: str,
    timeout: int,
) -> dict[str, Any]:
    if store is None:
        return _http_json(path, payload, timeout)
    return store.call(surface, payload, invoke=lambda: _http_json(path, payload, timeout))


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _extract_json(text: str) -> dict[str, Any]:
    """Parse a JSON object out of model text, tolerating code fences and prose."""
    candidate = text.strip()
    for attempt in (candidate, re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate)):
        try:
            parsed = json.loads(attempt)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    match = re.search(r"\{.*\}", candidate, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    raise RoundBuildError(f"model output is not parseable JSON: {candidate[:200]}")


def _responses_text(response: dict[str, Any]) -> str:
    chunks: list[str] = []
    for item in response.get("output", []):
        for content in item.get("content", []) or []:
            if content.get("type") == "output_text":
                chunks.append(content.get("text", ""))
    if not chunks:
        raise RoundBuildError("responses API returned no output_text")
    return "\n".join(chunks)


def _clean_handle(handle: str) -> str:
    handle = handle.strip()
    return handle if handle.startswith("@") else f"@{handle}"


def _substantive(text: str) -> bool:
    stripped = re.sub(r"https?://\S+", "", text).strip()
    return len(stripped) >= 12 and len(stripped.split()) >= 3


def _made_tool_calls(response: dict[str, Any]) -> bool:
    """True when the responses API actually invoked server-side x_search."""
    usage = response.get("usage", {})
    details = usage.get("server_side_tool_usage_details", {}) or {}
    if details.get("x_search_calls", 0) > 0:
        return True
    return any(
        item.get("type") in ("custom_tool_call", "tool_call", "x_search_call")
        for item in response.get("output", [])
    )


def _parse_post(raw: dict[str, Any]) -> dict[str, Any]:
    post_url = str(raw.get("post_url", "")).strip()
    post_id = str(raw.get("post_id", "")).strip()
    if not post_id.isdigit():
        match = re.search(r"/status/(\d+)", post_url)
        if match:
            post_id = match.group(1)
    if not post_id:
        raise RoundBuildError("no usable post id in fetched post")
    if not post_url.startswith("http"):
        raise RoundBuildError(f"no usable post url: {post_url!r}")
    post_text = str(raw.get("post_text", "")).strip()
    if not post_text:
        raise RoundBuildError("fetched post has empty text")
    return {
        "post_id": post_id,
        "post_text": post_text,
        "post_author": _clean_handle(str(raw.get("post_author", ""))),
        "post_url": post_url,
    }


def _parse_replies(raw: dict[str, Any], post_author: str) -> list[dict[str, str]]:
    seen_authors = {post_author.lower()}
    replies: list[dict[str, str]] = []
    for reply in raw.get("replies") or []:
        author = _clean_handle(str(reply.get("author", "")))
        text = str(reply.get("text", "")).strip()
        if not text or not _substantive(text) or author.lower() in seen_authors:
            continue
        seen_authors.add(author.lower())
        replies.append({"author": author, "text": text})
    if len(replies) < 4:
        raise RoundBuildError(
            f"thread too thin: {len(replies)} substantive replies from distinct authors"
        )
    return replies[:4]


# ---------------------------------------------------------------------------
# The two model calls
# ---------------------------------------------------------------------------


# Topics whose plain name drags in off-topic or charged posts get a richer
# query. The first live run on the bare words found a partisan rant for
# movies and a football team dinner for food.
TOPIC_QUERIES = {
    "movies": (
        "movies, a new film, box office numbers, or actors, from a film focused "
        "account. Skip posts that are mainly political commentary"
    ),
    "food": (
        "food, cooking, restaurants, or recipes. Skip posts that are mainly "
        "about sports teams"
    ),
}


def _find_prompt(topic: str, broad: bool) -> str:
    topic_query = TOPIC_QUERIES.get(topic, topic)
    if broad:
        return (
            f"Search X for any popular post from the last 7 days related to {topic_query} "
            "with a healthy reply count, at least 5 replies. Pick one whose replies "
            "are full sentences, not just emoji. Return the numeric post id, the "
            "post text verbatim, the author handle, and the canonical x.com post url."
        )
    return (
        f"Search X for one engaging post from the last 48 hours about {topic_query} "
        "with a healthy reply count, at least 5 replies. Pick a post whose replies "
        "are substantive full thoughts, not just emoji or tags. Return the numeric "
        "post id, the post text verbatim, the author handle, and the canonical "
        "x.com post url."
    )


def _search_payloads(prompt: str, schema_name: str, schema: dict[str, Any], plain_hint: str) -> list[dict[str, Any]]:
    structured = {
        "model": config.MODEL_TEXT,
        "input": prompt,
        "tools": [{"type": "x_search"}],
        "max_tool_calls": 8,
        "text": {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "schema": schema,
            }
        },
    }
    plain = {
        "model": config.MODEL_TEXT,
        "input": prompt + " " + plain_hint,
        "tools": [{"type": "x_search"}],
        "max_tool_calls": 8,
    }
    return [structured, plain]


def _find_post(
    store: FixtureStore | None, topic: str, broad: bool = False
) -> dict[str, Any]:
    prompt = _find_prompt(topic, broad)
    payloads = _search_payloads(
        prompt,
        "decoy_post",
        POST_SCHEMA,
        "Respond with only a JSON object with keys post_id, post_text, "
        "post_author, post_url. No prose.",
    )
    last_error: Exception | None = None
    for payload in payloads:
        try:
            response = _call_surface(store, "x_search_post", payload, "/responses", 300)
            if not _made_tool_calls(response):
                raise RoundBuildError("post finder made no x_search calls")
            return _parse_post(_extract_json(_responses_text(response)))
        except (RoundBuildError, FixtureMissError) as error:
            last_error = error
    raise RoundBuildError(f"post search failed for {topic!r}: {last_error}")


def _fetch_replies(
    store: FixtureStore | None, post: dict[str, Any]
) -> list[dict[str, str]]:
    prompt = (
        f"Open this X thread and read its replies: {post['post_url']} "
        f"(post id {post['post_id']}, posted by {post['post_author']}). "
        f"A reliable way is searching with the query "
        f"conversation_id:{post['post_id']} filter:replies . "
        "List up to 8 replies that you actually read from the thread, each with "
        "the real author handle and the reply text verbatim. Only include "
        "replies you read in the search results. Never invent, merge, or "
        "paraphrase a reply. Skip replies from the original poster. If you "
        "cannot read the thread, return an empty replies array."
    )
    payloads = _search_payloads(
        prompt,
        "decoy_replies",
        REPLIES_SCHEMA,
        "Respond with only a JSON object with one key, replies, an array of "
        "{author, text}. No prose.",
    )
    last_error: Exception | None = None
    for payload in payloads:
        try:
            response = _call_surface(store, "x_search_replies", payload, "/responses", 300)
            if not _made_tool_calls(response):
                raise RoundBuildError("reply fetch made no x_search calls, refusing unverified replies")
            return _parse_replies(
                _extract_json(_responses_text(response)), post["post_author"]
            )
        except (RoundBuildError, FixtureMissError) as error:
            last_error = error
    raise RoundBuildError(f"reply fetch failed for {post['post_url']}: {last_error}")


def _write_decoy(
    store: FixtureStore | None, thread: dict[str, Any]
) -> tuple[str, str]:
    briefing = json.dumps(
        {
            "post_text": thread["post_text"],
            "real_replies": [r["text"] for r in thread["replies"]],
        },
        ensure_ascii=False,
        indent=2,
    )
    user = (
        "Here is the post and its 4 real replies:\n"
        + briefing
        + "\nWrite the one imposter reply and the rationale."
    )
    structured = {
        "model": config.MODEL_TEXT,
        "messages": [
            {"role": "system", "content": WRITER_SYSTEM},
            {"role": "user", "content": user},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "decoy_reply", "schema": DECOY_SCHEMA},
        },
    }
    plain = {
        "model": config.MODEL_TEXT,
        "messages": [
            {"role": "system", "content": WRITER_SYSTEM},
            {
                "role": "user",
                "content": user
                + "\nRespond with only a JSON object with keys reply_text and "
                "rationale. No prose.",
            },
        ],
    }
    last_error: Exception | None = None
    for payload in (structured, plain):
        try:
            response = _call_surface(store, "decoy_write", payload, "/chat/completions", 180)
            parsed = _extract_json(response["choices"][0]["message"]["content"])
            reply_text = str(parsed.get("reply_text", "")).strip()
            rationale = str(parsed.get("rationale", "")).strip()
            if not reply_text or not rationale:
                raise RoundBuildError("decoy writer returned empty fields")
            return reply_text, rationale
        except (RoundBuildError, FixtureMissError, KeyError, IndexError) as error:
            last_error = error
    raise RoundBuildError(f"decoy write failed: {last_error}")


# ---------------------------------------------------------------------------
# Assembly, safety, validation
# ---------------------------------------------------------------------------


def _screen(round_dict: dict[str, Any]) -> dict[str, Any]:
    try:
        from plugins.safety.screen import screen_round
    except ImportError:
        return {"screened": False, "gate_codes": []}
    try:
        result = screen_round(round_dict)
        if isinstance(result, dict) and "screened" in result:
            return {
                "screened": bool(result["screened"]),
                "gate_codes": list(result.get("gate_codes", [])),
            }
    except Exception:
        pass
    return {"screened": False, "gate_codes": []}


def _assemble(
    topic: str, thread: dict[str, Any], decoy_text: str, rationale: str
) -> dict[str, Any]:
    post_id = thread["post_id"]
    seed = int(hashlib.sha256(post_id.encode()).hexdigest()[:12], 16) % 1_000_000_007
    round_id = (
        "decoy-"
        + hashlib.sha256(f"{post_id}:{ROUND_ID_SALT}".encode()).hexdigest()[:12]
    )

    entries = [
        {"text": r["text"], "author": r["author"], "is_decoy": False}
        for r in thread["replies"]
    ]
    entries.append({"text": decoy_text, "author": "decoy", "is_decoy": True})
    random.Random(seed).shuffle(entries)

    replies = [
        {
            "slot": slot,
            "text": entry["text"],
            "author": entry["author"],
            "is_decoy": entry["is_decoy"],
        }
        for slot, entry in enumerate(entries)
    ]
    decoy_slot = next(r["slot"] for r in replies if r["is_decoy"])

    round_dict = {
        "round_id": round_id,
        "source": {
            "post_text": thread["post_text"],
            "post_author": thread["post_author"],
            "post_url": thread["post_url"],
            "topic": topic,
        },
        "replies": replies,
        "decoy_slot": decoy_slot,
        "decoy_rationale": rationale,
        "safety": {"screened": False, "gate_codes": []},
        "seed": seed,
    }
    round_dict["safety"] = _screen(round_dict)
    return round_dict


def validate_round(round_dict: dict[str, Any]) -> None:
    """Raise ValueError if the round does not match CONTRACT.md."""
    problems: list[str] = []
    replies = round_dict.get("replies", [])
    if len(replies) != config.REPLIES_PER_ROUND:
        problems.append(f"expected {config.REPLIES_PER_ROUND} replies, got {len(replies)}")
    for index, reply in enumerate(replies):
        if reply.get("slot") != index:
            problems.append(f"reply {index} has slot {reply.get('slot')}")
        if not str(reply.get("text", "")).strip():
            problems.append(f"reply {index} has empty text")
        if not str(reply.get("author", "")).strip():
            problems.append(f"reply {index} has empty author")
    decoys = [r for r in replies if r.get("is_decoy")]
    if len(decoys) != 1:
        problems.append(f"expected exactly 1 decoy, got {len(decoys)}")
    elif decoys[0].get("slot") != round_dict.get("decoy_slot"):
        problems.append(
            f"decoy_slot {round_dict.get('decoy_slot')} does not match "
            f"decoy at slot {decoys[0].get('slot')}"
        )
    source = round_dict.get("source", {})
    for key in ("post_text", "post_author", "post_url", "topic"):
        if not str(source.get(key, "")).strip():
            problems.append(f"source.{key} missing or empty")
    if not str(source.get("post_url", "")).startswith("http"):
        problems.append("source.post_url is not a url")
    if not str(round_dict.get("round_id", "")).startswith("decoy-"):
        problems.append("round_id missing decoy- prefix")
    if not isinstance(round_dict.get("seed"), int):
        problems.append("seed is not an int")
    if not str(round_dict.get("decoy_rationale", "")).strip():
        problems.append("decoy_rationale missing")
    safety = round_dict.get("safety", {})
    if not isinstance(safety, dict) or "screened" not in safety or "gate_codes" not in safety:
        problems.append("safety block malformed")
    if problems:
        raise ValueError("round fails contract: " + "; ".join(problems))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_round(topic: str, live: bool = False) -> dict[str, Any]:
    """Build one Decoy round for a topic. See CONTRACT.md for the shape.

    live=False replays recorded fixtures and never touches the network.
    live=True calls the xAI API, and with ARCADE_RECORD=1 also records
    fixtures under fixtures/api/ for offline replay.
    """
    store = _make_store(live)
    try:
        post = _find_post(store, topic)
        replies = _fetch_replies(store, post)
    except RoundBuildError:
        post = _find_post(store, topic, broad=True)
        replies = _fetch_replies(store, post)
    thread = {**post, "replies": replies}
    decoy_text, rationale = _write_decoy(store, thread)
    round_dict = _assemble(topic, thread, decoy_text, rationale)
    validate_round(round_dict)
    return round_dict


# ---------------------------------------------------------------------------
# CLI for pre-building the committed demo rounds
# ---------------------------------------------------------------------------


def _build_and_save(topic: str, live: bool) -> dict[str, Any]:
    round_dict = build_round(topic, live=live)
    ROUNDS_DIR.mkdir(parents=True, exist_ok=True)
    path = ROUNDS_DIR / f"decoy_{topic}.json"
    path.write_text(
        json.dumps(round_dict, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return round_dict


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Decoy rounds")
    parser.add_argument("--topic", help="single topic to build")
    parser.add_argument("--all", action="store_true", help="build every demo topic")
    parser.add_argument("--live", action="store_true", help="call the real API")
    args = parser.parse_args()

    topics = DEMO_TOPICS if args.all else ([args.topic] if args.topic else [])
    if not topics:
        parser.error("pass --topic NAME or --all")

    failed: list[str] = []
    for topic in topics:
        try:
            round_dict = _build_and_save(topic, live=args.live)
            print(
                f"OK {topic}: {round_dict['round_id']} "
                f"decoy_slot={round_dict['decoy_slot']} "
                f"url={round_dict['source']['post_url']}"
            )
        except (RoundBuildError, ValueError) as error:
            failed.append(topic)
            print(f"FAIL {topic}: {error}")
    if failed:
        print(f"failed topics: {', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
