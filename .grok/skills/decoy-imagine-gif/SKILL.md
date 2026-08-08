---
name: decoy-imagine-gif
description: >
  How the Decoy Imagine agent builds the robot's looping "gif": study the four
  human reaction GIFs for style only, then generate an ORIGINAL Imagine still
  + video that feels similar — never regenerate or reference-to-video the
  human gifs. Use when editing reply GIFs, imagine_agent, reply_gifs decoy
  generation, MODEL_VIDEO, or /decoy-imagine-gif.
metadata:
  short-description: "Original Imagine decoy, style-matched to human GIFs"
---

# Decoy Imagine GIF agent

Source of truth for **how the robot gif is forged**. Runtime:
`services/imagine_agent.py` loads `references/*.md` and drives:

1. Human GIF assignment (`services/reply_gifs.attach_reply_media`) — unchanged pool files
2. Frame sample from those GIFs **for vision study only** (Pillow)
3. Vision style brief (`MODEL_AGENT` chat + images) — qualities, not scenes to copy
4. `POST /v1/images/generations` → **original** still (never human frames as input)
5. `POST /v1/videos/generations` → animate that still (I2V) or pure T2V fallback
6. Save `web/static-assets/reply-gifs/decoy/{round_id}_decoy.mp4`

## Goal

- Human replies keep their real reaction GIFs (not regenerated).
- Decoy is a **new** Grok Imagine clip that blends into the room by style
  (palette, compression, motion energy) so it is harder to spot.
- **Never** pass human GIF frames as `reference_images` or `image` to video gen.

## Architecture

```
attach human GIFs (disk pool, read-only for agent)
  → sample frames for vision study only
  → style brief (vision, skill: style_brief.md)
  → ORIGINAL still (image gen, skill: still_prompt.md)
  → ORIGINAL video from that still (I2V) or T2V fallback
  → decoy mp4 looped muted in the client
```

- **Never** put `media_source` on the wire during guessing (`server/app.py`).
- Fixture keys must **not** embed base64 frames (hash prompt + round_id).

## Prompt files

| File | Role |
|------|------|
| `references/style_brief.md` | System prompt for the vision style pass |
| `references/still_prompt.md` | Lead-in for the original Imagine still |
| `references/video_prompt.md` | Lead-in for Imagine video / I2V |

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
