---
name: decoy-imagine-gif
description: >
  How the Decoy Imagine agent builds the robot's looping gif from the thread's
  reply texts via Grok Imagine — never regenerating human pool GIFs. Use when
  editing imagine_agent, reply_gifs decoy generation, MODEL_VIDEO, or
  /decoy-imagine-gif.
metadata:
  short-description: "Original Imagine decoy from reply texts"
---

# Decoy Imagine GIF agent

Runtime: `services/imagine_agent.py`

1. Human cards: library GIFs from disk (`reply_gifs`) — agent does not touch them  
2. Read **post + human reply texts + decoy text**  
3. Text style brief (`MODEL_AGENT`) — no human GIF images  
4. `POST /v1/images/generations` → original still from those vibes  
5. `POST /v1/videos/generations` → I2V that still (or T2V)  
6. Save `web/static-assets/reply-gifs/decoy/{round_id}_decoy.mp4`

## Rules

- Decoy is always a **new** Grok Imagine clip from reply/post text  
- **Never** pass human GIF frames as `image` / `reference_images`  
- **Never** copy pool files onto the decoy slot  
- Live: decoy starts `pending` with no URL until Imagine finishes  

## CLI

```bash
ARCADE_MODE=live python3 services/imagine_agent.py --round-id decoy-xxx --force
```
