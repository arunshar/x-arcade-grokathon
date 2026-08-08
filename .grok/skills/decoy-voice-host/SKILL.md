---
name: decoy-voice-host
description: >
  Defines how the Decoy arcade host / commentator voice should speak — tone,
  phase rules (guessing vs reveal), event lines, safety, latency, and Grok Voice
  wiring. Use when editing host commentary, host_agent, voice TTS, playHost,
  COMM toggle, or when the user mentions commentator voice, Grok Voice style,
  host agent prompts, or /decoy-voice-host.
metadata:
  short-description: "Decoy host voice style + safety rules"
---

# Decoy voice host

This skill is the **source of truth** for how the in-game host sounds and what
it is allowed to say. Runtime code loads the prompt files under `references/`
into `services/host_agent.py`. Do not invent a second personality in JS or
server code — edit the references, then keep `host_agent.py` as a thin loader.

## Architecture (do not break)

```
Game event
  → five committed mp3s (HARD PATH — never blocked by the model)
  → async POST /agent/commentate  (time-capped ~1.8s client / 2.5s server)
  → optional Grok Voice TTS color line if still on the same phase/round
```

- **Stingers:** `web/static-assets/host_{intro,round,reveal,win,lose}.mp3`
- **Brain:** `services/host_agent.py` + prompts in this skill’s `references/`
- **Mouth:** Grok Voice TTS (`POST /tts`) / realtime; browser TTS last resort
- **Cancel:** `voiceQueue.bump()` on phase/round change and on local pick / NEXT

## Prompt files (edit these to change the voice)

| File | When used |
|------|-----------|
| `references/guessing.md` | lobby + guessing (spoiler-strict) |
| `references/reveal.md` | reveal (decoy is public — be funny) |
| `references/events.md` | event catalog + example lines for humans |

`host_agent.py` reads `guessing.md` / `reveal.md` at call time (with in-code
fallback strings if files are missing).

## Personality (summary)

- Sports-desk arcade hype, not a corporate assistant
- One short sentence preferred (under ~12 words when possible)
- No hashtags, emojis, stage directions, or wrapping quotes
- Output is **only** the spoken line

## Phase rules (non-negotiable)

### Pre-reveal (lobby / guessing)
- Never name the decoy, robot, imposter, or “the answer”
- Never treat a slot as the answer; `pick_reply` is only which card was tapped
- Never quote reply text as if judging who is human
- May talk: names, scores, standings, locks, clock, pure hype

### Reveal
- Decoy is on screen — roast it, name the slot, quote a short bit of decoy text
- May praise the winner, note wrong picks, riff on `decoy_rationale`
- Still short and punchy

## Latency rules

- Hard path = mp3s. Agent must never stall a round.
- Prefer `MODEL_AGENT` / `grok-4-1-fast-non-reasoning` (~0.6s probed).
- Do **not** put `grok-4.5` on the commentate path (probed ~5–8s).
- Client aborts commentate ~1.8s; late results drop if phase/round moved on.

## When changing voice behavior

1. Edit `references/guessing.md` and/or `references/reveal.md`
2. Update `references/events.md` examples if event catalog changes
3. Keep `web/game.js` mp3-first + async agent; do not await commentate on START
4. Smoke:  
   `ARCADE_MODE=live python3 services/host_agent.py '{"event":"reveal","phase":"reveal","winner":"NEON","decoy_slot":2}'`
5. Optional probe: run host-agent section of `services/probe_surfaces.py`

## Related code

- `services/host_agent.py` — load prompts, sanitize observation, call chat
- `services/voice_host.py` — TTS Eve (or `ARCADE_VOICE`)
- `web/game.js` — `playHost`, `voiceQueue`, `commentary`
- `server/app.py` — `POST /agent/commentate`, `POST /tts`
- `artifacts/probes/host_agent_chat.json` — measured chat latency
