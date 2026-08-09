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

1. Human cards keep library GIFs (`reply_gifs`)
2. **Vision agent** samples those GIF frames + reply texts → style brief
3. Imagine **image** → original still matching the brief + decoy mood
4. Imagine **video** (~2s I2V) from that still
5. Save `web/static-assets/reply-gifs/decoy/{round_id}_decoy.mp4`

## Rules

- Human GIF frames = **study only** (vision chat)
- Never pass human frames as video `reference_images` / `image` (remixes memes)
- Decoy is always a **new** Imagine clip related in style/energy

## CLI

```bash
ARCADE_MODE=live python3 services/imagine_agent.py --round-id decoy-xxx --force
```
