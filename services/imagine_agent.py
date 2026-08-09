"""Imagine decoy agent — short original video styled like the human reply GIFs.

Pipeline
--------
1. Attach human reply GIFs on the round (disk pool — left unchanged).
2. **Vision agent**: sample frames from those user GIFs + read reply texts.
3. Produce a style/reaction brief (palette, motion, meme energy).
4. Grok Imagine **image** → brand-new still matching that brief + decoy vibe.
5. Grok Imagine **video** (~2s loop) from that still (I2V), or T2V fallback.
6. Save ``web/static-assets/reply-gifs/decoy/{round_id}_decoy.mp4``.

Hard rules
----------
- Human GIF *files* are study-only (vision). Never pass them as video
  ``reference_images`` / ``image`` (that remixes the same meme).
- The decoy clip is always a new Imagine generation.
- Human cards keep their library GIFs; only the decoy slot is Imagine-made.

CLI:
    ARCADE_MODE=live python3 services/imagine_agent.py --round-id decoy-xxx --force
    ARCADE_MODE=live python3 services/imagine_agent.py --all --force
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import config  # noqa: E402
from fixtures_core import FixtureStore  # noqa: E402
from services.xai_http import post_json, ssl_context, _api_key  # noqa: E402

GIF_DIR = REPO_ROOT / "web" / "static-assets" / "reply-gifs"
DECOY_DIR = GIF_DIR / "decoy"
ROUNDS_DIR = REPO_ROOT / "cartridges" / "decoy" / "rounds"
_SKILL_REF = REPO_ROOT / ".grok" / "skills" / "decoy-imagine-gif" / "references"

# Vision brief should stay snappy — this is offline/pre-round work, not the host.
VISION_TIMEOUT_S = float(os.environ.get("ARCADE_IMAGINE_VISION_TIMEOUT", "18"))
VIDEO_POLL_S = float(os.environ.get("ARCADE_IMAGINE_VIDEO_POLL", "300"))
MAX_REF_IMAGES = int(os.environ.get("ARCADE_IMAGINE_REF_MAX", "4"))
FRAME_MAX_PX = int(os.environ.get("ARCADE_IMAGINE_FRAME_PX", "512"))
# Couple-second looping clip (API allows short durations).
VIDEO_DURATION = int(os.environ.get("ARCADE_IMAGINE_VIDEO_DURATION", "2") or "2")
VIDEO_DURATION = max(1, min(5, VIDEO_DURATION))

_FALLBACK_VISION_SYSTEM = """You are a visual scout for a party game.
You see still frames from the HUMAN reaction GIFs already on a chat thread.
Write ONE compact brief (under 55 words) so Grok Imagine can invent a NEW
2-second looping reaction clip that would sit naturally next to those GIFs.

Cover: shared palette/lighting, framing, motion energy, compression/grain,
overall meme vs cinematic feel, and the emotional reaction vibe.

Rules:
- Abstract qualities + reaction energy only
- Do NOT name real people, celebrities, or copyrighted characters
- Do NOT say "copy frame 2" or describe one gif to recreate exactly
- Never say AI, decoy, robot, or fake
- No markdown, no quotes — output ONLY the brief"""

_FALLBACK_STYLE_SYSTEM = """You invent visual STYLE for one new reaction GIF
in a group-chat thread from reply texts only. Write ONE compact brief:
palette mood, framing energy, motion feel, web-GIF grain/compression.
Abstract qualities only. Never name real people or celebrities.
Never say AI, decoy, robot, or fake. No markdown. Under 40 words."""


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "topic"


def _snippet(text: str, limit: int = 100) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rsplit(" ", 1)[0] + "…"


def _load_skill(name: str, fallback: str) -> str:
    path = _SKILL_REF / f"{name}.md"
    try:
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    except OSError:
        pass
    return fallback


def _decoy_slot(round_data: dict[str, Any]) -> int | None:
    try:
        return int(round_data.get("decoy_slot"))
    except (TypeError, ValueError):
        return None


def _decoy_reply(round_data: dict[str, Any]) -> dict[str, Any] | None:
    slot = _decoy_slot(round_data)
    if slot is None:
        return None
    for rep in round_data.get("replies") or []:
        if isinstance(rep, dict):
            try:
                if int(rep.get("slot")) == slot:
                    return rep
            except (TypeError, ValueError):
                continue
    return None


def _resolve_web_path(url: str) -> Path | None:
    """Map /static-assets/... → repo web path."""
    if not url:
        return None
    u = str(url).split("?", 1)[0]
    if u.startswith("/static-assets/"):
        path = REPO_ROOT / "web" / u.lstrip("/")
        return path if path.is_file() else None
    if u.startswith("static-assets/"):
        path = REPO_ROOT / "web" / u
        return path if path.is_file() else None
    return None


def human_gif_paths(round_data: dict[str, Any]) -> list[Path]:
    """Paths of human reply GIFs already stamped on the round."""
    decoy = _decoy_slot(round_data)
    paths: list[Path] = []
    seen: set[str] = set()
    for rep in round_data.get("replies") or []:
        if not isinstance(rep, dict):
            continue
        try:
            slot = int(rep.get("slot"))
        except (TypeError, ValueError):
            continue
        if decoy is not None and slot == decoy:
            continue
        src = str(rep.get("media_source") or "")
        if src and src != "human":
            continue
        url = str(rep.get("media_url") or "")
        path = _resolve_web_path(url)
        if path is None or path.suffix.lower() != ".gif":
            continue
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        paths.append(path)
    return paths


def _sample_gif_frames(path: Path, *, max_frames: int = 2) -> list[Any]:
    """Return RGB PIL frames (first + mid) from a GIF."""
    from PIL import Image

    frames: list[Any] = []
    try:
        im = Image.open(path)
    except OSError as exc:
        print(f"imagine_agent: open {path.name} failed: {exc}", file=sys.stderr)
        return frames

    n = 0
    try:
        n = int(getattr(im, "n_frames", 1) or 1)
    except Exception:
        n = 1

    indices = [0]
    if n > 2 and max_frames > 1:
        indices.append(n // 2)
    elif n > 1 and max_frames > 1:
        indices.append(min(1, n - 1))

    for idx in indices[:max_frames]:
        try:
            im.seek(idx)
            frame = im.convert("RGB")
            frame.thumbnail((FRAME_MAX_PX, FRAME_MAX_PX))
            frames.append(frame.copy())
        except Exception as exc:
            print(f"imagine_agent: frame {idx} {path.name}: {exc}", file=sys.stderr)
    return frames


def frames_to_data_urls(frames: list[Any], *, quality: int = 78) -> list[str]:
    urls: list[str] = []
    for frame in frames:
        buf = io.BytesIO()
        frame.save(buf, format="JPEG", quality=quality, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        urls.append(f"data:image/jpeg;base64,{b64}")
    return urls


def collect_reference_frames(
    round_data: dict[str, Any],
    *,
    max_images: int = MAX_REF_IMAGES,
) -> list[Any]:
    """Up to max_images frames sampled across human GIFs."""
    paths = human_gif_paths(round_data)
    if not paths:
        # Fallback: any pool gif so live gen still has a reaction look.
        paths = sorted(GIF_DIR.glob("*.gif"))[:4]
    frames: list[Any] = []
    per = 2 if len(paths) <= 2 else 1
    for path in paths:
        if len(frames) >= max_images:
            break
        for fr in _sample_gif_frames(path, max_frames=per):
            frames.append(fr)
            if len(frames) >= max_images:
                break
    return frames


def _make_store() -> FixtureStore | None:
    if config.MODE == "live" and not config.RECORD:
        return None
    return FixtureStore(
        root=REPO_ROOT / "fixtures" / "api",
        record=config.MODE == "live" and config.RECORD,
        reuse_existing=os.environ.get("ARCADE_REUSE_FIXTURES", "1") == "1",
    )


def _chat_brief(system: str, user_content: Any, *, fixture_kind: str, fixture_key: dict[str, Any]) -> str | None:
    """Call chat completions; return assistant text or None."""
    request = {
        "model": config.MODEL_AGENT,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": 140,
        "temperature": 0.4,
    }

    def invoke() -> dict[str, Any]:
        return post_json("/chat/completions", request, timeout=VISION_TIMEOUT_S)

    try:
        if config.MODE == "live" and not config.RECORD:
            data = invoke()
        elif config.MODE == "live" and config.RECORD:
            store = _make_store()
            data = store.call(  # type: ignore[union-attr]
                fixture_kind,
                fixture_key,
                invoke=invoke,
            )
        else:
            return None
        choice = (data.get("choices") or [{}])[0]
        msg = (choice.get("message") or {}).get("content") or ""
        brief = _snippet(str(msg).strip().strip('"'), 320)
        return brief or None
    except Exception as exc:
        print(f"imagine_agent: {fixture_kind} failed: {exc}", file=sys.stderr)
        return None


def analyze_user_reply_gifs(
    round_data: dict[str, Any],
    *,
    topic: str = "",
    reply_texts: list[str] | None = None,
    post_text: str = "",
    decoy_text: str = "",
) -> str:
    """Vision agent: study human reply GIF frames → style brief for Imagine.

    Frames are for analysis only — never fed into video generation as refs.
    """
    frames = collect_reference_frames(round_data, max_images=MAX_REF_IMAGES)
    data_urls = frames_to_data_urls(frames)
    human_bits = "; ".join(_snippet(t, 45) for t in (reply_texts or [])[:4] if t)
    post_bit = _snippet(post_text, 120)
    decoy_bit = _snippet(decoy_text, 80)
    fallback = (
        "Punchy square group-chat reaction energy, mild compression grain, "
        "casual framing, quick loopable motion, meme-adjacent — invent a NEW "
        "subject that matches that room vibe."
    )

    if data_urls:
        system = _load_skill("style_brief", _FALLBACK_VISION_SYSTEM)
        content: list[dict[str, Any]] = []
        for url in data_urls[:MAX_REF_IMAGES]:
            content.append({"type": "image_url", "image_url": {"url": url}})
        content.append(
            {
                "type": "text",
                "text": (
                    f"Topic: {topic or 'general'}. "
                    f"Post vibe (no on-screen text): {post_bit or 'n/a'}. "
                    f"Human reply captions: {human_bits or 'n/a'}. "
                    f"New decoy reply mood to illustrate: {decoy_bit or 'n/a'}. "
                    "These images are the HUMAN reply gifs on this round. "
                    "Write the brief for a NEW 2-second looping clip that feels "
                    "related and similar in energy/style — not a copy of any frame."
                ),
            }
        )
        brief = _chat_brief(
            system,
            content,
            fixture_kind="imagine_gif_vision_brief",
            fixture_key={
                "model": config.MODEL_AGENT,
                "topic": topic,
                "n_frames": len(data_urls),
                "decoy": decoy_bit,
                "kind": "imagine_gif_vision_brief",
            },
        )
        if brief:
            return brief

    # Text-only fallback when no frames or vision unavailable.
    return describe_reply_style(
        topic=topic,
        reply_texts=reply_texts,
        post_text=post_text,
        decoy_text=decoy_text,
    ) or fallback


def describe_reply_style(
    *,
    topic: str = "",
    reply_texts: list[str] | None = None,
    post_text: str = "",
    decoy_text: str = "",
) -> str:
    """Text-only style brief from the thread replies (no human GIF files)."""
    human_bits = "; ".join(_snippet(t, 50) for t in (reply_texts or [])[:4] if t)
    post_bit = _snippet(post_text, 140)
    decoy_bit = _snippet(decoy_text, 80)
    fallback = (
        "Compressed group-chat reaction GIF energy, punchy loop, casual framing, "
        "mild grain, meme ugliness — original subject, not a stock clip."
    )
    if not human_bits and not post_bit:
        return fallback

    system = _load_skill("style_brief_text", _FALLBACK_STYLE_SYSTEM)
    user_text = (
        f"Topic: {topic or 'general'}.\n"
        f"Post (do not render as on-screen text): {post_bit or 'n/a'}\n"
        f"Human replies in the thread: {human_bits or 'n/a'}\n"
        f"New decoy reply vibe to illustrate: {decoy_bit or 'n/a'}\n"
        "Write a style brief for ONE brand-new 2-second reaction gif that could sit "
        "next to those replies. Invent original visuals from the vibes only."
    )
    brief = _chat_brief(
        system,
        user_text,
        fixture_kind="imagine_reply_style_brief",
        fixture_key={
            "model": config.MODEL_AGENT,
            "topic": topic,
            "decoy": decoy_bit,
            "humans": human_bits[:200],
            "kind": "imagine_reply_style_brief",
        },
    )
    return brief or fallback


# Back-compat name used by older call sites / docs.
def describe_gif_style(
    data_urls: list[str] | None = None,
    *,
    topic: str = "",
    reply_texts: list[str] | None = None,
    post_text: str = "",
    decoy_text: str = "",
) -> str:
    """Prefer vision when frames provided; else text-only brief."""
    if data_urls:
        # Synthetic round shell so analyze can be called with bare urls.
        return describe_reply_style(
            topic=topic,
            reply_texts=reply_texts,
            post_text=post_text,
            decoy_text=decoy_text,
        )
    return describe_reply_style(
        topic=topic,
        reply_texts=reply_texts,
        post_text=post_text,
        decoy_text=decoy_text,
    )

def _sanitize_style_brief(brief: str) -> str:
    """Strip names / IP that often trip video moderation."""
    text = brief or ""
    # Drop obvious proper-name tokens the vision model sometimes emits.
    text = re.sub(
        r"\b(Homer|Simpson|Simpsons|Kratos|celebrity|famous|actor|actress)\b",
        "figure",
        text,
        flags=re.I,
    )
    return _snippet(text, 220)


def build_decoy_still_prompt(
    *,
    style_brief: str,
    topic: str,
    decoy_text: str,
    post_text: str = "",
    human_replies: list[str] | None = None,
) -> str:
    """Prompt for an ORIGINAL still styled like the human reply GIFs."""
    skill = _load_skill(
        "still_prompt",
        "Create a brand-new square reaction GIF still with Grok Imagine. "
        "Match the visual style and reaction energy of the human reply gifs "
        "described in the brief. Invent a NEW subject — do not recreate those gifs.",
    )
    vibe = _snippet(decoy_text, 100)
    post_bit = _snippet(post_text, 120)
    humans = "; ".join(_snippet(t, 45) for t in (human_replies or [])[:4] if t)
    brief = _sanitize_style_brief(style_brief)
    safety = (
        "CRITICAL SAFETY: no real people, no celebrity likeness, no copyrighted "
        "cartoon characters, no brand logos, no readable text, no watermarks, "
        "no UI chrome. Generic anonymous figures, objects, animals, or abstract "
        "shapes only."
    )
    return (
        f"{skill} "
        f"Topic: {topic or 'general'}. "
        f"Thread post vibe (do not render text): {post_bit or 'n/a'}. "
        f"Human reply captions (vibes): {humans or 'n/a'}. "
        f"THIS decoy reply mood (abstract, no on-screen text): {vibe}. "
        f"Style brief from analyzing the human reply GIFs: {brief}. "
        "Square 1:1, compressed chat-GIF still, same room energy as those gifs. "
        "ORIGINAL Imagine subject — related and similar, not a duplicate meme. "
        f"{safety}"
    )


def build_decoy_video_prompt(
    *,
    style_brief: str,
    topic: str,
    decoy_text: str,
    post_text: str = "",
    human_replies: list[str] | None = None,
    abstract_only: bool = False,
    from_own_still: bool = False,
) -> str:
    """Prompt for an ORIGINAL looping clip from reply vibes (I2V or T2V)."""
    skill = _load_skill(
        "video_prompt",
        "Create a seamless looping reaction gif with Grok Imagine for a group "
        "chat. Brand-new clip from the reply vibes — never an existing meme file.",
    )
    vibe = _snippet(decoy_text, 100)
    post_bit = _snippet(post_text, 110)
    humans = "; ".join(_snippet(t, 40) for t in (human_replies or [])[:4] if t)
    brief = _sanitize_style_brief(style_brief)
    safety = (
        "CRITICAL SAFETY: no real people, no celebrity likeness, no copyrighted "
        "cartoon characters, no brand logos, no readable text, no watermarks, "
        "no UI chrome. Use generic anonymous silhouettes, objects, animals, or "
        "abstract shapes only."
    )
    thread = f"Post vibe (no on-screen text): {post_bit}. " if post_bit else ""
    others = f"Other replies' vibes: {humans}. " if humans else ""
    original = (
        "ORIGINAL Grok Imagine generation only — do not reproduce any existing "
        "GIF, stock meme, or another reply's media. "
    )
    secs = VIDEO_DURATION
    if abstract_only:
        return (
            f"Short seamless looping reaction gif, square 1:1, about {secs} seconds, "
            "compressed web-meme look. "
            f"Topic: {topic or 'general'}. {thread}{others}"
            f"This reply's mood (abstract): {vibe}. Style from human gifs: {brief}. "
            f"{original}{safety}"
        )
    if from_own_still:
        return (
            f"{skill} "
            f"Animate THIS original Imagine still into a seamless ~{secs}-second loop "
            "(punchy gif motion, mild compression, chat energy matching the human "
            "reply gifs on this round). Keep this subject — do not replace it with "
            f"a known meme. Topic: {topic or 'general'}. "
            f"{thread}Mood: {vibe}. {safety}"
        )
    return (
        f"{skill} "
        f"Topic: {topic or 'general'}. {thread}{others}"
        f"This NEW reply mood (abstract, no readable text): {vibe}. "
        f"Style brief from the human reply GIFs: {brief}. "
        f"Square 1:1, ~{secs}-second seamless loop, chat-GIF energy. "
        f"{original}{safety}"
    )


def _get_json(path: str, timeout: int = 60) -> dict[str, Any]:
    request = urllib.request.Request(
        config.API_BASE + path,
        headers={"Authorization": f"Bearer {_api_key()}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=timeout, context=ssl_context()
        ) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        body = error.read()[:600].decode(errors="replace")
        raise RuntimeError(f"xAI GET {path} returned {error.code}: {body}") from error


def _download(url: str, dest: Path, timeout: int = 120) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "x-arcade-decoy/1.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as resp:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.read())


def _poll_video(request_id: str, timeout_s: float = VIDEO_POLL_S) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        body = _get_json(f"/videos/{request_id}", timeout=60)
        status = body.get("status")
        if status == "done":
            return body
        if status in ("failed", "expired"):
            raise RuntimeError(f"video {request_id} {status}: {body}")
        time.sleep(4)
    raise TimeoutError(f"video {request_id} not done within {timeout_s}s")


def _full_video_gen(payload: dict[str, Any]) -> dict[str, Any]:
    model = payload.get("model") or config.MODEL_VIDEO
    print(
        f"imagine_agent: POST /videos/generations model={model} "
        f"duration={payload.get('duration')} has_still={bool(payload.get('image'))}",
        file=sys.stderr,
    )
    # Refuse payloads that try to attach human pool media as references.
    if payload.get("reference_images"):
        raise RuntimeError("refusing video gen with reference_images (human gifs)")
    start = post_json("/videos/generations", payload, timeout=60)
    rid = start.get("request_id") or start.get("id")
    if not rid:
        raise RuntimeError(f"video start missing request_id: {start!r}")
    print(f"imagine_agent: video request_id={rid} polling…", file=sys.stderr)
    done = _poll_video(str(rid))
    print(f"imagine_agent: video {rid} status={done.get('status')}", file=sys.stderr)
    return done


def _video_payload(
    prompt: str,
    *,
    own_still_data_url: str | None = None,
) -> dict[str, Any]:
    """Build video request. Never attach human GIF frames.

    Optional ``own_still_data_url`` is an Imagine-generated still (ours only)
    used for image-to-video. Human reply gifs are study-only via the style brief.
    """
    payload: dict[str, Any] = {
        "model": config.MODEL_VIDEO,
        "prompt": prompt,
        "duration": VIDEO_DURATION,
        "aspect_ratio": "1:1",
        "resolution": "480p",
    }
    if own_still_data_url:
        payload["image"] = {"url": own_still_data_url}
    return payload


def generate_own_still(
    *,
    style_brief: str,
    topic: str,
    decoy_text: str,
    post_text: str = "",
    human_replies: list[str] | None = None,
) -> str | None:
    """Create an ORIGINAL square still via Imagine image. Returns data URL or None."""
    prompt = build_decoy_still_prompt(
        style_brief=style_brief,
        topic=topic,
        decoy_text=decoy_text,
        post_text=post_text,
        human_replies=human_replies,
    )
    request = {
        "model": config.MODEL_IMAGE,
        "prompt": prompt,
        "n": 1,
        "response_format": "b64_json",
    }

    def invoke() -> dict[str, Any]:
        print(
            f"imagine_agent: POST /images/generations model={config.MODEL_IMAGE}",
            file=sys.stderr,
        )
        return post_json("/images/generations", request, timeout=120)

    try:
        if config.MODE == "live" and not config.RECORD:
            data = invoke()
        elif config.MODE == "live" and config.RECORD:
            store = _make_store()
            fixture_key = {
                "model": request["model"],
                "prompt": prompt,
                "kind": "imagine_decoy_still",
            }
            data = store.call(  # type: ignore[union-attr]
                "imagine_decoy_still",
                fixture_key,
                invoke=invoke,
            )
        else:
            return None
        b64 = (data.get("data") or [{}])[0].get("b64_json") or ""
        if not b64:
            print("imagine_agent: image gen returned empty b64", file=sys.stderr)
            return None
        raw = base64.b64decode(b64)
        mime = "image/png" if raw[:4] == b"\x89PNG" else "image/jpeg"
        print(
            f"imagine_agent: got Imagine still ({len(raw)} bytes, {mime})",
            file=sys.stderr,
        )
        return f"data:{mime};base64,{b64}"
    except Exception as exc:
        print(f"imagine_agent: own still failed: {exc}", file=sys.stderr)
        return None


def decoy_video_path(round_id: str) -> Path:
    return DECOY_DIR / f"{_slug(round_id)}_decoy.mp4"


def decoy_meta_path(round_id: str | Path) -> Path:
    """Sidecar proving the mp4 came from Grok Imagine (not a pool gif / probe)."""
    if isinstance(round_id, Path):
        return round_id.parent / (round_id.stem + ".imagine.json")
    return DECOY_DIR / f"{_slug(str(round_id))}_decoy.imagine.json"


def write_imagine_meta(
    round_id: str,
    path: Path,
    *,
    style_brief: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    meta = {
        "source": "grok-imagine-video",
        "model_image": getattr(config, "MODEL_IMAGE", ""),
        "model_video": getattr(config, "MODEL_VIDEO", ""),
        "round_id": str(round_id),
        "file": path.name,
        "bytes": path.stat().st_size if path.is_file() else 0,
        "duration": VIDEO_DURATION,
        "style_brief": _snippet(style_brief, 200),
        "ts": int(time.time()),
    }
    if extra:
        meta.update(extra)
    decoy_meta_path(round_id).write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def is_imagine_certified(path: Path | None) -> bool:
    """True only if sidecar says this file was produced by Grok Imagine video."""
    if path is None or not path.is_file():
        return False
    meta_path = decoy_meta_path(path)
    if not meta_path.is_file():
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    src = str(meta.get("source") or "").lower()
    if "imagine" not in src and "grok" not in src:
        return False
    # File must still look like video, not a renamed gif.
    if path.suffix.lower() not in (".mp4", ".webm", ".mov"):
        return False
    return path.stat().st_size > 800


def _md5_file(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def is_placeholder_decoy(path: Path | None) -> bool:
    """True if path is missing, tiny, named probe, or a byte-clone of _probe.mp4.

    Early seeding copied the same probe clip to every round id — those must
    not count as unique Imagine generations.
    """
    if path is None or not path.is_file() or path.stat().st_size < 800:
        return True
    if path.name.startswith("_probe") or path.stem == "_probe":
        return True
    probe = DECOY_DIR / "_probe.mp4"
    if not probe.is_file():
        return False
    try:
        if path.resolve() == probe.resolve():
            return True
    except OSError:
        pass
    # Fast path: same size then hash.
    if path.stat().st_size == probe.stat().st_size:
        try:
            if _md5_file(path) == _md5_file(probe):
                return True
        except OSError:
            return True
    return False


def is_real_decoy_media(path: Path | None) -> bool:
    """True when the file is a unique per-round Imagine clip (not the probe).

    Live play requires an ``*.imagine.json`` sidecar so we never treat a
    leftover probe copy or random asset as Grok Imagine output.
    """
    if path is None or not path.is_file() or is_placeholder_decoy(path):
        return False
    if path.suffix.lower() not in (".mp4", ".webm", ".mov"):
        return False
    # Trust certified Imagine outputs always.
    if is_imagine_certified(path):
        return True
    # Optional escape hatch for offline demos with pre-baked files only.
    if os.environ.get("ARCADE_IMAGINE_TRUST_FILES", "") == "1":
        return not is_placeholder_decoy(path)
    return False


def purge_placeholder_decoys() -> list[str]:
    """Delete per-round files that are still just copies of _probe.mp4."""
    removed: list[str] = []
    if not DECOY_DIR.is_dir():
        return removed
    for path in DECOY_DIR.glob("*_decoy.mp4"):
        if is_placeholder_decoy(path):
            try:
                path.unlink()
                removed.append(path.name)
            except OSError as exc:
                print(f"imagine_agent: could not remove {path.name}: {exc}", file=sys.stderr)
    return removed


def generate_matching_decoy(
    round_data: dict[str, Any],
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Run the full agent: vision brief → Grok Imagine image → Imagine video.

    Mutates ``round_data``. Returns a small result dict for logs/CLI.
    The decoy slot is ALWAYS Imagine-made media when this succeeds — never a
    human pool GIF file.
    """
    rid = str(round_data.get("round_id") or "round")
    out = decoy_video_path(rid)
    result: dict[str, Any] = {
        "round_id": rid,
        "path": str(out),
        "style_brief": "",
        "status": "skipped",
        "engine": f"{config.MODEL_IMAGE}+{config.MODEL_VIDEO}",
    }

    # Reuse only certified Grok Imagine video — never probe / pool / uncertified.
    if not force and is_real_decoy_media(out) and is_imagine_certified(out):
        _stamp_decoy(round_data, out)
        result["status"] = "exists"
        result["certified"] = True
        result["engine"] = "grok-imagine-video (cached certified)"
        return result

    # Uncertified leftovers (old probe copies, unknown mp4s): regenerate.
    if out.is_file() and not is_imagine_certified(out):
        force = True
        print(
            f"imagine_agent: {out.name} lacks Imagine certification — regenerating",
            file=sys.stderr,
        )

    # Drop stale probe clones so we do not keep serving the same clip.
    if out.is_file() and is_placeholder_decoy(out):
        try:
            out.unlink()
        except OSError:
            pass
        meta = decoy_meta_path(rid)
        if meta.is_file():
            try:
                meta.unlink()
            except OSError:
                pass

    if config.MODE != "live" and not force:
        # Demo offline: only certified Imagine files count as ready decoy media.
        # Never promote human pool gifs. Probe is last-resort pending, not ready.
        if is_imagine_certified(out):
            _stamp_decoy(round_data, out, ready=True)
            result["status"] = "exists"
            result["certified"] = True
            return result
        result["status"] = "no_live"
        result["error"] = "demo mode needs pre-baked certified Imagine video"
        _mark_decoy_failed(round_data)
        return result

    # Live path MUST hit Grok Imagine APIs.
    print(
        f"imagine_agent: calling Grok Imagine "
        f"image={config.MODEL_IMAGE} video={config.MODEL_VIDEO} for {rid}",
        file=sys.stderr,
    )

    # Ensure human GIFs are on the round so we can analyze their frames.
    try:
        from services.reply_gifs import attach_reply_media

        attach_reply_media(round_data)
    except Exception as exc:
        print(f"imagine_agent: attach_reply_media: {exc}", file=sys.stderr)

    human_texts: list[str] = []
    decoy = _decoy_slot(round_data)
    for rep in round_data.get("replies") or []:
        if not isinstance(rep, dict):
            continue
        try:
            if decoy is not None and int(rep.get("slot")) == decoy:
                continue
        except (TypeError, ValueError):
            pass
        if rep.get("text"):
            human_texts.append(str(rep["text"]))

    src = round_data.get("source") or {}
    topic = str(src.get("topic") or "arcade")
    post_text = str(src.get("post_text") or "")
    decoy_rep = _decoy_reply(round_data)
    decoy_text = str((decoy_rep or {}).get("text") or topic)
    gif_paths = human_gif_paths(round_data)
    result["n_human_replies"] = len(human_texts)
    result["n_human_gifs"] = len(gif_paths)
    result["source"] = "vision_gif_agent"
    result["video_duration"] = VIDEO_DURATION

    # Agent step: analyze user reply GIFs (vision) → style brief.
    style_brief = analyze_user_reply_gifs(
        round_data,
        topic=topic,
        reply_texts=human_texts,
        post_text=post_text,
        decoy_text=decoy_text,
    )
    result["style_brief"] = style_brief
    print(
        f"imagine_agent: analyzed {len(gif_paths)} human gifs → brief: "
        f"{_snippet(style_brief, 100)}",
        file=sys.stderr,
    )

    # Original still matching that style, then short looping video.
    own_still = generate_own_still(
        style_brief=style_brief,
        topic=topic,
        decoy_text=decoy_text,
        post_text=post_text,
        human_replies=human_texts,
    )
    result["own_still"] = bool(own_still)

    if own_still:
        prompt = build_decoy_video_prompt(
            style_brief=style_brief,
            topic=topic,
            decoy_text=decoy_text,
            post_text=post_text,
            human_replies=human_texts,
            from_own_still=True,
        )
    else:
        prompt = build_decoy_video_prompt(
            style_brief=style_brief,
            topic=topic,
            decoy_text=decoy_text,
            post_text=post_text,
            human_replies=human_texts,
        )
    result["prompt"] = _snippet(prompt, 200)

    def _try_video(p: dict[str, Any], key_extra: dict[str, Any]) -> dict[str, Any]:
        fixture_key = {
            "model": p["model"],
            "prompt": p["prompt"],
            "duration": p["duration"],
            "aspect_ratio": p["aspect_ratio"],
            "resolution": p["resolution"],
            "has_own_still": bool(p.get("image")),
            "n_human_gif_refs": 0,  # study-only; never attach human frames
            "round_id": rid,
            "kind": "imagine_decoy_video",
            **key_extra,
        }
        store = _make_store()
        if store is None:
            return _full_video_gen(p)
        return store.call(
            "imagine_decoy_video",
            fixture_key,
            invoke=lambda: _full_video_gen(p),
        )

    payload = _video_payload(prompt, own_still_data_url=own_still)
    try:
        done = _try_video(payload, {"pass": "own_still_i2v" if own_still else "t2v"})
    except Exception as exc:
        err = str(exc)
        print(f"imagine_agent: video gen failed: {err}", file=sys.stderr)
        # Retry pure text-to-video (still no human frames as refs).
        try:
            safe_prompt = build_decoy_video_prompt(
                style_brief=style_brief,
                topic=topic,
                decoy_text=decoy_text,
                post_text=post_text,
                human_replies=human_texts,
                abstract_only=True,
            )
            result["prompt"] = _snippet(safe_prompt, 200)
            done = _try_video(
                _video_payload(safe_prompt, own_still_data_url=None),
                {"pass": "t2v_retry"},
            )
        except Exception as exc2:
            print(f"imagine_agent: t2v retry failed: {exc2}", file=sys.stderr)
            result["status"] = "failed"
            result["error"] = f"{err} | retry: {exc2}"
            # Never promote _probe.mp4 as the decoy reply — leave pending/failed
            # so the UI shows "GROK IMAGINE…" instead of a stock clip.
            _mark_decoy_failed(round_data)
            return result
    url = (done.get("video") or {}).get("url") or done.get("url")
    if not url:
        result["status"] = "failed"
        result["error"] = "no video url"
        _mark_decoy_failed(round_data)
        return result

    DECOY_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _download(str(url), out)
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = f"download: {exc}"
        _mark_decoy_failed(round_data)
        return result

    if is_placeholder_decoy(out):
        # Should not happen for a fresh download — refuse to mark ready.
        result["status"] = "failed"
        result["error"] = "downloaded file looks like probe placeholder"
        _mark_decoy_failed(round_data)
        return result

    write_imagine_meta(
        rid,
        out,
        style_brief=style_brief,
        extra={
            "n_human_gifs": result.get("n_human_gifs"),
            "own_still": bool(own_still),
            "pass": "imagine_video",
        },
    )
    _stamp_decoy(round_data, out, ready=True)
    result["status"] = "ready"
    result["certified"] = True
    result["bytes"] = out.stat().st_size if out.is_file() else 0
    # Stash brief on the round for debug / reveal flair (not shown pre-reveal).
    round_data["imagine_style_brief"] = style_brief
    return result


def _stamp_decoy(
    round_data: dict[str, Any],
    path: Path,
    *,
    ready: bool | None = None,
) -> None:
    decoy = _decoy_slot(round_data)
    if decoy is None or not path.is_file():
        return
    # Hard refuse: human pool GIFs and the shared probe never become "the reply".
    if path.suffix.lower() == ".gif" or path.name.endswith(".gif"):
        print(f"imagine_agent: refusing to stamp gif on decoy: {path}", file=sys.stderr)
        return
    if is_placeholder_decoy(path):
        print(
            f"imagine_agent: refusing probe/placeholder stamp on decoy: {path.name}",
            file=sys.stderr,
        )
        # Keep slot pending so live can still call Grok Imagine.
        for rep in round_data.get("replies") or []:
            if not isinstance(rep, dict):
                continue
            try:
                if int(rep.get("slot")) != decoy:
                    continue
            except (TypeError, ValueError):
                continue
            rep["media_url"] = None
            rep["media_type"] = "video"
            rep["media_status"] = "pending"
            rep["media_source"] = "imagine"
            rep["media_engine"] = f"{config.MODEL_IMAGE}+{config.MODEL_VIDEO}"
            break
        round_data["decoy_media_status"] = "pending"
        round_data["format"] = "gif"
        return

    rel = path.relative_to(REPO_ROOT / "web")
    url = "/" + str(rel).replace("\\", "/")
    # Never stamp a human pool path onto the decoy.
    if "/reply-gifs/" in url.replace("\\", "/") and "/decoy/" not in url.replace("\\", "/"):
        print(f"imagine_agent: refusing to stamp pool path on decoy: {url}", file=sys.stderr)
        return
    if path.suffix.lower() not in (".mp4", ".webm", ".mov"):
        print(f"imagine_agent: refusing non-video decoy media: {path}", file=sys.stderr)
        return

    require = bool(getattr(config, "IMAGINE_DECOY_REQUIRED", True))
    certified = is_imagine_certified(path)
    is_real = is_real_decoy_media(path)
    if ready is None:
        if require:
            ready = certified and is_real
        else:
            ready = is_real or (config.MODE != "live" and path.stat().st_size > 800)
    elif ready and require and not certified:
        # Caller asked ready=True but file isn't Imagine-certified — demote.
        ready = False
        print(
            f"imagine_agent: demoting uncertified decoy to pending: {path.name}",
            file=sys.stderr,
        )

    for rep in round_data.get("replies") or []:
        if not isinstance(rep, dict):
            continue
        try:
            if int(rep.get("slot")) != decoy:
                continue
        except (TypeError, ValueError):
            continue
        rep["media_url"] = url if ready or not require else (url if certified else None)
        # While waiting for Imagine, show pending chrome (no stock media).
        if not ready and require and not certified:
            rep["media_url"] = None
        rep["media_type"] = "video"  # Imagine decoy is always video
        rep["media_status"] = "ready" if ready else "pending"
        rep["media_source"] = "imagine"
        rep["media_engine"] = f"{config.MODEL_IMAGE}+{config.MODEL_VIDEO}"
        break
    round_data["decoy_media_status"] = "ready" if ready else "pending"
    round_data["reply_art_status"] = round_data["decoy_media_status"]
    round_data["format"] = "gif"


def _mark_decoy_failed(round_data: dict[str, Any]) -> None:
    decoy = _decoy_slot(round_data)
    round_data["decoy_media_status"] = "failed"
    if decoy is None:
        return
    for rep in round_data.get("replies") or []:
        if isinstance(rep, dict):
            try:
                if int(rep.get("slot")) == decoy:
                    rep["media_status"] = "failed"
            except (TypeError, ValueError):
                pass


def _load_rounds() -> list[dict[str, Any]]:
    rounds: list[dict[str, Any]] = []
    for path in sorted(ROUNDS_DIR.glob("decoy_*.json")):
        try:
            rounds.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"skip {path.name}: {exc}", file=sys.stderr)
    return rounds


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Imagine agent: match human GIFs → decoy Imagine video"
    )
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--round-id", type=str, default="")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--purge-placeholders",
        action="store_true",
        help="Delete per-round files that are still copies of _probe.mp4",
    )
    args = parser.parse_args()

    if args.purge_placeholders:
        removed = purge_placeholder_decoys()
        print(f"purged {len(removed)} placeholder decoys:", ", ".join(removed) or "(none)")
        if not args.all and not args.round_id:
            return

    rounds = _load_rounds()
    if args.round_id:
        rounds = [r for r in rounds if r.get("round_id") == args.round_id]
        if not rounds:
            sys.exit(f"no round {args.round_id!r}")
    elif not args.all:
        rounds = rounds[:1]

    for rnd in rounds:
        print(f"== {rnd.get('round_id')} ==")
        result = generate_matching_decoy(rnd, force=args.force)
        print(json.dumps({k: v for k, v in result.items() if k != "prompt"}, indent=2))
        if result.get("prompt"):
            print("  prompt:", result["prompt"][:160], "…")


if __name__ == "__main__":
    main()
