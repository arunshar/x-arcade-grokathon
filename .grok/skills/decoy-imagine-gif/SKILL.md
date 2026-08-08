---
name: decoy-imagine-gif
description: >
  How the Decoy Imagine agent builds the robot's looping "gif" by studying the
  four human reaction GIFs in the round — style brief via vision, then Grok
  Imagine video with reference_images so the decoy is harder to distinguish.
  Use when editing reply GIFs, imagine_agent, reply_gifs decoy generation,
  MODEL_VIDEO, or /decoy-imagine-gif.
metadata:
  short-description: "Match human GIFs → Imagine decoy video"
---

# Decoy Imagine GIF agent

Source of truth for **how the robot gif is forged**. Runtime:
`services/imagine_agent.py` loads `references/*.md` and drives:

1. Human GIF assignment (`services/reply_gifs.attach_reply_media`)
2. Frame sample from those GIFs (Pillow)
3. Vision style brief (`MODEL_AGENT` chat + images)
4. `POST /v1/videos/generations` with `reference_images` + `image` anchor
5. Save `web/static-assets/reply-gifs/decoy/{round_id}_decoy.mp4`

## Goal

Make the decoy **harder to spot** by matching the visual language of the
actual human GIFs on the same round — palette, compression, framing, motion —
not a neon "AI poster" that screams generated.

## Architecture

```
attach human GIFs
  → sample frames from those files
  → style brief (vision, skill: style_brief.md)
  → video prompt (skill: video_prompt.md + brief + decoy text vibe)
  → grok-imagine-video-1.5  (reference_images = human frames)
  → decoy mp4 looped muted in the client (looks like a gif)
```

- **Never** put `media_source` on the wire during guessing (`server/app.py`).
- Prefer `reference_images` over bare text-to-video.
- Fixture keys must **not** embed base64 frames (hash prompt + n_refs + round_id).

## Prompt files

| File | Role |
|------|------|
| `references/style_brief.md` | System prompt for the vision style pass |
| `references/video_prompt.md` | Lead-in for the Imagine video prompt |

## Latency / mode

- Offline work: run before the event or in background after round start.
- Demo mode: serve committed per-round mp4 or `_probe.mp4` — no network.
- Live: `generate_matching_decoy` from `reply_gifs` / server background task.
- Do not block the 30s round clock on video completion.

## CLI

```bash
ARCADE_MODE=live python3 services/imagine_agent.py --round-id decoy-xxx --force
ARCADE_MODE=live python3 services/imagine_agent.py --all
```

## Related code

- `services/imagine_agent.py` — agent
- `services/reply_gifs.py` — human GIF pool + calls agent for decoy
- `server/app.py` — `_attach_decoy_imagine_gif`
- `web/game.js` — `buildReplyMedia` (gif img / video loop)
- `config.py` — `MODEL_VIDEO`, `MODEL_AGENT`, `MODEL_IMAGE`
