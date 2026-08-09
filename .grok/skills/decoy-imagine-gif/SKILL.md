---
name: decoy-imagine-gif
description: >
  Vision agent studies human reply GIFs, then Grok Imagine creates a short
  original looping video in that style for the decoy slot. Use when editing
  imagine_agent, reply_gifs decoy generation, or /decoy-imagine-gif.
metadata:
  short-description: "Analyze human GIFs → original Imagine video"
---

# Decoy Imagine GIF agent

The **decoy reply** is always a Grok Imagine generation — never a pool GIF.

1. Human cards keep library GIFs (`reply_gifs`)
2. **Vision agent** samples those GIF frames + reply texts → style brief
3. `grok-imagine-image` → original still matching the brief + decoy mood
4. `grok-imagine-video` (~2s I2V) from that still
5. Save `web/static-assets/reply-gifs/decoy/{round_id}_decoy.mp4`
6. Write `*.imagine.json` certification sidecar (required in live)

## Rules

- Human GIF frames = **study only** (vision chat)
- Never pass human frames as video `reference_images` / `image` (remixes memes)
- Decoy is always a **new** Imagine clip related in style/energy
- `IMAGINE_DECOY_REQUIRED=1` (default): refuse pool `.gif`, `_probe.mp4`, uncertified files
- Live rounds schedule Imagine at round start when the decoy is not certified

## CLI

```bash
ARCADE_MODE=live python3 services/imagine_agent.py --round-id decoy-xxx --force
ARCADE_MODE=live python3 services/imagine_agent.py --all --force
```
