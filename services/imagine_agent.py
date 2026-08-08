"""Imagine decoy agent — original clip, style-matched to the human GIFs.

Pipeline
--------
1. Collect the four human reaction GIFs already on the round (read-only).
2. Sample frames and ask vision for a short *style brief* only
   (palette, grain, motion energy) — never copy those frames into generation.
3. Grok Imagine **image** → a brand-new still that fits the room's look.
4. Grok Imagine **video** from that original still (image-to-video), or
   text-to-video if the still fails — never ``reference_images`` of human GIFs.
5. Save the short square MP4 as the decoy's looping "gif".

Human GIFs stay untouched. The decoy is always a new Imagine generation that
only *resembles* the room (compression, energy, vibe), not a regen/remix of
any user reply gif.

CLI:
    ARCADE_MODE=live python3 services/imagine_agent.py --round-id decoy-xxx
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
VISION_TIMEOUT_S = float(os.environ.get("ARCADE_IMAGINE_VISION_TIMEOUT", "12"))
VIDEO_POLL_S = float(os.environ.get("ARCADE_IMAGINE_VIDEO_POLL", "300"))
MAX_REF_IMAGES = int(os.environ.get("ARCADE_IMAGINE_REF_MAX", "4"))
FRAME_MAX_PX = int(os.environ.get("ARCADE_IMAGINE_FRAME_PX", "512"))

_FALLBACK_STYLE_SYSTEM = """You scout visual STYLE only for a party game.
Given still frames from human reply GIFs, write ONE compact style brief so a
NEW original reaction clip can match the room without copying those frames:
palette, lighting, framing, motion energy, grain/compression, meme ugliness.
Describe abstract qualities only — do NOT describe specific characters, faces,
or scenes to recreate. Never name real people or celebrities.
Never say AI, decoy, robot, or fake. No markdown. Under 45 words."""


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


def describe_gif_style(
    data_urls: list[str],
    *,
    topic: str = "",
    reply_texts: list[str] | None = None,
    post_text: str = "",
) -> str:
    """Vision pass: one short style brief matching the human GIF room."""
    if not data_urls:
        return (
            "Typical chat reaction GIF look: compressed web meme energy, "
            "punchy loop, casual framing, mild grain."
        )

    system = _load_skill("style_brief", _FALLBACK_STYLE_SYSTEM)
    content: list[dict[str, Any]] = []
    # Cap images in the vision call (token cost).
    for url in data_urls[: min(3, len(data_urls))]:
        content.append({"type": "image_url", "image_url": {"url": url}})

    human_bits = "; ".join(_snippet(t, 40) for t in (reply_texts or [])[:4] if t)
    post_bit = _snippet(post_text, 120)
    content.append(
        {
            "type": "text",
            "text": (
                f"Topic context: {topic or 'general'}. "
                f"Original post (do not quote as on-screen text): {post_bit or 'n/a'}. "
                f"Human reply vibes (text only): {human_bits or 'n/a'}. "
                "Write the style brief now so a fifth gif blends into this thread."
            ),
        }
    )

    request = {
        "model": config.MODEL_AGENT,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ],
        "max_tokens": 100,
        "temperature": 0.35,
    }

    def invoke() -> dict[str, Any]:
        return post_json("/chat/completions", request, timeout=VISION_TIMEOUT_S)

    try:
        # Live (no RECORD): hit the API. Demo: skip vision unless RECORD is on
        # with an existing fixture — never block rounds on a fixture miss.
        if config.MODE == "live" and not config.RECORD:
            data = invoke()
        elif config.MODE == "live" and config.RECORD:
            store = _make_store()
            fixture_req = {
                "model": request["model"],
                "topic": topic,
                "n_images": len(data_urls),
                "kind": "imagine_style_brief",
            }
            data = store.call(  # type: ignore[union-attr]
                "imagine_style_brief",
                fixture_req,
                invoke=invoke,
            )
        else:
            # demo / offline — use the deterministic fallback below
            data = None
        if data:
            choice = (data.get("choices") or [{}])[0]
            msg = (choice.get("message") or {}).get("content") or ""
            brief = _snippet(str(msg).strip().strip('"'), 280)
            if brief:
                return brief
    except Exception as exc:
        print(f"imagine_agent: style brief failed: {exc}", file=sys.stderr)

    return (
        "Match the attached reaction-GIF references: same palette, compression, "
        "framing, and loop energy. Casual meme look, not cinematic."
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
) -> str:
    """Prompt for an ORIGINAL still — style-matched, not a copy of human gifs."""
    skill = _load_skill(
        "still_prompt",
        "Create a brand-new square reaction-image still for a group chat. "
        "Match the room's visual STYLE only. Invent a new subject — do not "
        "recreate, remix, or trace any specific frame from the human gifs.",
    )
    vibe = _snippet(decoy_text, 90)
    post_bit = _snippet(post_text, 110)
    brief = _sanitize_style_brief(style_brief)
    safety = (
        "CRITICAL SAFETY: no real people, no celebrity likeness, no copyrighted "
        "cartoon characters, no brand logos, no readable text, no watermarks, "
        "no UI chrome. Generic anonymous figures, objects, animals, or abstract "
        "shapes only."
    )
    thread = f"Thread vibe (do not render text): {post_bit}. " if post_bit else ""
    return (
        f"{skill} "
        f"Style to match (qualities only, not scenes to copy): {brief} "
        f"Topic: {topic or 'general'}. {thread}"
        f"Mood of THIS new reply (abstract): {vibe}. "
        "Square 1:1, looks like a compressed chat GIF still, not a polished poster. "
        "ORIGINAL content only — different subject matter from the human replies. "
        f"{safety}"
    )


def build_decoy_video_prompt(
    *,
    style_brief: str,
    topic: str,
    decoy_text: str,
    post_text: str = "",
    abstract_only: bool = False,
    from_own_still: bool = False,
) -> str:
    """Prompt for an ORIGINAL looping clip (text-to-video or own-still I2V)."""
    skill = _load_skill(
        "video_prompt",
        "Animate into a seamless looping reaction gif for a group chat. "
        "Match the room's visual language and energy. This must be a NEW clip, "
        "not a recreation or remix of any human reply gif.",
    )
    vibe = _snippet(decoy_text, 90)
    post_bit = _snippet(post_text, 110)
    brief = _sanitize_style_brief(style_brief)
    safety = (
        "CRITICAL SAFETY: no real people, no celebrity likeness, no copyrighted "
        "cartoon characters, no brand logos, no readable text, no watermarks, "
        "no UI chrome. Use generic anonymous silhouettes, objects, animals, or "
        "abstract shapes only."
    )
    thread = (
        f"Original post vibe (do not render text): {post_bit}. "
        if post_bit
        else ""
    )
    original = (
        "ORIGINAL Imagine generation only — do not recreate, morph, or collage "
        "the human reply gifs from this round; invent new subject matter that "
        "merely fits beside them. "
    )
    if abstract_only:
        return (
            "Short seamless looping reaction gif, square 1:1, compressed web-meme look. "
            f"Topic mood: {topic or 'general'}. {thread}"
            f"Reply vibe (abstract): {vibe}. "
            f"Match style qualities only: {brief}. "
            f"{original}{safety}"
        )
    if from_own_still:
        return (
            f"{skill} "
            "Animate THIS original still into a 3-second seamless loop "
            "(subtle punchy motion, gif energy, mild compression). "
            f"Keep the same subject — do not swap in other gifs. "
            f"Topic: {topic or 'general'}. {thread}"
            f"Mood: {vibe}. {safety}"
        )
    return (
        f"{skill} "
        f"Style qualities to match (not scenes to copy): {brief} "
        f"Topic: {topic or 'general'}. {thread}"
        f"Mood of this one NEW reply (abstract, no readable text): {vibe}. "
        "Square 1:1, 3-second seamless loop, looks like a compressed chat GIF "
        f"not a polished film. {original}{safety}"
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
    start = post_json("/videos/generations", payload, timeout=60)
    rid = start.get("request_id") or start.get("id")
    if not rid:
        raise RuntimeError(f"video start missing request_id: {start!r}")
    return _poll_video(str(rid))


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
        "duration": 3,
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
) -> str | None:
    """Create an ORIGINAL square still via Imagine image. Returns data URL or None."""
    prompt = build_decoy_still_prompt(
        style_brief=style_brief,
        topic=topic,
        decoy_text=decoy_text,
        post_text=post_text,
    )
    request = {
        "model": config.MODEL_IMAGE,
        "prompt": prompt,
        "n": 1,
        "response_format": "b64_json",
    }

    def invoke() -> dict[str, Any]:
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
            return None
        raw = base64.b64decode(b64)
        mime = "image/png" if raw[:4] == b"\x89PNG" else "image/jpeg"
        return f"data:{mime};base64,{b64}"
    except Exception as exc:
        print(f"imagine_agent: own still failed: {exc}", file=sys.stderr)
        return None


def decoy_video_path(round_id: str) -> Path:
    return DECOY_DIR / f"{_slug(round_id)}_decoy.mp4"


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
    """True when the file is a unique per-round Imagine clip (not the probe)."""
    return path is not None and path.is_file() and not is_placeholder_decoy(path)


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
    """Run the full agent: style brief → Imagine video → stamp decoy media.

    Mutates ``round_data``. Returns a small result dict for logs/CLI.
    """
    rid = str(round_data.get("round_id") or "round")
    out = decoy_video_path(rid)
    result: dict[str, Any] = {
        "round_id": rid,
        "path": str(out),
        "style_brief": "",
        "status": "skipped",
    }

    # Reuse only a *real* unique generation — never a seeded probe clone.
    if not force and is_real_decoy_media(out):
        _stamp_decoy(round_data, out)
        result["status"] = "exists"
        return result

    # Drop stale probe clones so we do not keep serving the same clip.
    if out.is_file() and is_placeholder_decoy(out):
        try:
            out.unlink()
        except OSError:
            pass

    if config.MODE != "live" and not force:
        # Demo: serve probe only as a shared fallback (not copied per-round).
        probe = DECOY_DIR / "_probe.mp4"
        if probe.is_file():
            _stamp_decoy(round_data, probe)
            result["status"] = "demo_probe"
            result["path"] = str(probe)
            return result
        result["status"] = "no_live"
        return result

    # Ensure human GIFs are assigned first.
    try:
        from services.reply_gifs import attach_reply_media

        attach_reply_media(round_data)
    except Exception as exc:
        print(f"imagine_agent: attach_reply_media: {exc}", file=sys.stderr)

    # Study human GIFs for style ONLY — frames never go into video generation.
    frames = collect_reference_frames(round_data)
    study_urls = frames_to_data_urls(frames)
    result["n_study_frames"] = len(study_urls)

    human_texts = []
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

    style_brief = describe_gif_style(
        study_urls,
        topic=topic,
        reply_texts=human_texts,
        post_text=post_text,
    )
    result["style_brief"] = style_brief

    # Own still first (original Imagine image), then animate it.
    own_still = generate_own_still(
        style_brief=style_brief,
        topic=topic,
        decoy_text=decoy_text,
        post_text=post_text,
    )
    result["own_still"] = bool(own_still)

    if own_still:
        prompt = build_decoy_video_prompt(
            style_brief=style_brief,
            topic=topic,
            decoy_text=decoy_text,
            post_text=post_text,
            from_own_still=True,
        )
    else:
        prompt = build_decoy_video_prompt(
            style_brief=style_brief,
            topic=topic,
            decoy_text=decoy_text,
            post_text=post_text,
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
            "n_human_refs": 0,  # never attach human gifs
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
        # Retry pure text-to-video (still no human frames).
        try:
            safe_prompt = build_decoy_video_prompt(
                style_brief=style_brief,
                topic=topic,
                decoy_text=decoy_text,
                post_text=post_text,
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
            probe = DECOY_DIR / "_probe.mp4"
            if probe.is_file():
                _stamp_decoy(round_data, probe, ready=False)
                result["status"] = "failed_probe_fallback"
                result["path"] = str(probe)
            else:
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

    _stamp_decoy(round_data, out, ready=True)
    result["status"] = "ready"
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
    rel = path.relative_to(REPO_ROOT / "web")
    url = "/" + str(rel).replace("\\", "/")
    mtype = "video" if path.suffix.lower() in (".mp4", ".webm") else "gif"
    is_real = is_real_decoy_media(path)
    if ready is None:
        ready = is_real or config.MODE != "live"
    for rep in round_data.get("replies") or []:
        if not isinstance(rep, dict):
            continue
        try:
            if int(rep.get("slot")) != decoy:
                continue
        except (TypeError, ValueError):
            continue
        rep["media_url"] = url
        rep["media_type"] = mtype
        rep["media_status"] = "ready" if ready else "pending"
        rep["media_source"] = "imagine"
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
