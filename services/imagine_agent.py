"""Imagine decoy agent — match the human GIFs so the robot is harder to spot.

Pipeline
--------
1. Collect the four human reaction GIFs already assigned on the round.
2. Sample frames from those GIFs (Pillow).
3. Ask a fast vision chat model for a short *style brief* (palette, subject,
   grain, motion) — never names people, never marks which is AI.
4. Call Grok Imagine video with ``reference_images`` = those frames + a prompt
   that blends the style brief with the decoy reply vibe.
5. Download the short square MP4 and save it as the decoy's looping "gif".

The agent deliberately does **not** invent neon arcade chrome when the human
GIFs are meme/reaction stock — matching the room is the whole point.

CLI:
    ARCADE_MODE=live python3 services/imagine_agent.py --round-id decoy-xxx
    ARCADE_MODE=live python3 services/imagine_agent.py --all --force
"""

from __future__ import annotations

import argparse
import base64
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

_FALLBACK_STYLE_SYSTEM = """You style-match reaction GIFs for a party game.
Given still frames from human reply GIFs, write ONE compact style brief for
video generation: palette, subject type, framing, motion energy, grain/compression.
Never name real people or celebrities. Never say AI, decoy, robot, or fake.
No markdown. Under 45 words."""


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
    content.append(
        {
            "type": "text",
            "text": (
                f"Topic context: {topic or 'general'}. "
                f"Human reply vibes (text only): {human_bits or 'n/a'}. "
                "Write the style brief now."
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


def build_decoy_video_prompt(
    *,
    style_brief: str,
    topic: str,
    decoy_text: str,
) -> str:
    """Prompt that steals the human GIF look while carrying the decoy vibe."""
    skill = _load_skill(
        "video_prompt",
        "Seamless looping reaction clip that could sit in a group chat next to "
        "the reference GIFs. Match their visual language exactly.",
    )
    vibe = _snippet(decoy_text, 90)
    return (
        f"{skill} "
        f"Style brief from the human GIFs in this round: {style_brief} "
        f"Topic: {topic or 'general'}. "
        f"Mood of this one reply (abstract, no readable text): {vibe}. "
        "Square 1:1, 3-second seamless loop, looks like a compressed chat GIF "
        "not a polished film. No readable text, no logos, no watermarks, "
        "no UI chrome, no 'AI' labels. Prefer stylized / meme subjects; "
        "if a face appears keep it generic — never a real celebrity likeness."
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


def _video_payload(prompt: str, data_urls: list[str]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": config.MODEL_VIDEO,
        "prompt": prompt,
        "duration": 3,
        "aspect_ratio": "1:1",
        "resolution": "480p",
    }
    if data_urls:
        # API forbids combining image + reference_images. Prefer multi-GIF
        # reference-to-video so the decoy blends with the whole human set.
        if len(data_urls) >= 2:
            payload["reference_images"] = [
                {"url": u} for u in data_urls[:MAX_REF_IMAGES]
            ]
        else:
            payload["image"] = {"url": data_urls[0]}
    return payload


def decoy_video_path(round_id: str) -> Path:
    return DECOY_DIR / f"{_slug(round_id)}_decoy.mp4"


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

    if not force and out.is_file() and out.stat().st_size > 800:
        # Already have a per-round file — stamp and exit.
        _stamp_decoy(round_data, out)
        result["status"] = "exists"
        return result

    if config.MODE != "live" and not force:
        probe = DECOY_DIR / "_probe.mp4"
        if probe.is_file():
            if not out.is_file():
                DECOY_DIR.mkdir(parents=True, exist_ok=True)
                out.write_bytes(probe.read_bytes())
            _stamp_decoy(round_data, out if out.is_file() else probe)
            result["status"] = "demo_probe"
            return result
        result["status"] = "no_live"
        return result

    # Ensure human GIFs are assigned first.
    try:
        from services.reply_gifs import attach_reply_media

        attach_reply_media(round_data)
    except Exception as exc:
        print(f"imagine_agent: attach_reply_media: {exc}", file=sys.stderr)

    frames = collect_reference_frames(round_data)
    data_urls = frames_to_data_urls(frames)
    result["n_refs"] = len(data_urls)

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

    topic = str((round_data.get("source") or {}).get("topic") or "arcade")
    decoy_rep = _decoy_reply(round_data)
    decoy_text = str((decoy_rep or {}).get("text") or topic)

    style_brief = describe_gif_style(
        data_urls, topic=topic, reply_texts=human_texts
    )
    result["style_brief"] = style_brief
    prompt = build_decoy_video_prompt(
        style_brief=style_brief, topic=topic, decoy_text=decoy_text
    )
    result["prompt"] = _snippet(prompt, 200)

    payload = _video_payload(prompt, data_urls)
    # Fixture key must not include megabytes of base64.
    fixture_key = {
        "model": payload["model"],
        "prompt": prompt,
        "duration": payload["duration"],
        "aspect_ratio": payload["aspect_ratio"],
        "resolution": payload["resolution"],
        "n_refs": len(data_urls),
        "round_id": rid,
        "kind": "imagine_decoy_video",
    }

    store = _make_store()
    try:
        if store is None:
            done = _full_video_gen(payload)
        else:
            done = store.call(
                "imagine_decoy_video",
                fixture_key,
                invoke=lambda: _full_video_gen(payload),
            )
    except Exception as exc:
        print(f"imagine_agent: video gen failed: {exc}", file=sys.stderr)
        result["status"] = "failed"
        result["error"] = str(exc)
        # Fall back to probe so the round still has motion.
        probe = DECOY_DIR / "_probe.mp4"
        if probe.is_file():
            DECOY_DIR.mkdir(parents=True, exist_ok=True)
            if not out.is_file():
                out.write_bytes(probe.read_bytes())
            _stamp_decoy(round_data, out)
            result["status"] = "failed_probe_fallback"
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

    _stamp_decoy(round_data, out)
    result["status"] = "ready"
    result["bytes"] = out.stat().st_size if out.is_file() else 0
    # Stash brief on the round for debug / reveal flair (not shown pre-reveal).
    round_data["imagine_style_brief"] = style_brief
    return result


def _stamp_decoy(round_data: dict[str, Any], path: Path) -> None:
    decoy = _decoy_slot(round_data)
    if decoy is None or not path.is_file():
        return
    rel = path.relative_to(REPO_ROOT / "web")
    url = "/" + str(rel).replace("\\", "/")
    mtype = "video" if path.suffix.lower() in (".mp4", ".webm") else "gif"
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
        rep["media_status"] = "ready"
        rep["media_source"] = "imagine"
        break
    round_data["decoy_media_status"] = "ready"
    round_data["reply_art_status"] = "ready"
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
    args = parser.parse_args()

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
