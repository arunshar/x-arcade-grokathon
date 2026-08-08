# X Arcade internal contract

The spine every module builds against. Change this file only deliberately. Everything else conforms
to it.

## The Round (JSON, produced by cartridges/decoy/round_builder.py)

```json
{
  "round_id": "decoy-<sha12 of source post id + salt>",
  "source": {
    "post_text": "...",
    "post_author": "@handle",
    "post_url": "https://x.com/...",
    "topic": "ai"
  },
  "replies": [
    {"slot": 0, "text": "...", "author": "@handle", "is_decoy": false},
    {"slot": 1, "text": "...", "author": "@handle", "is_decoy": false},
    {"slot": 2, "text": "...", "author": "decoy", "is_decoy": true},
    {"slot": 3, "text": "...", "author": "@handle", "is_decoy": false},
    {"slot": 4, "text": "...", "author": "@handle", "is_decoy": false}
  ],
  "decoy_slot": 2,
  "decoy_rationale": "display-only, shown at reveal",
  "safety": {"screened": true, "gate_codes": []},
  "seed": 1234567
}
```

Rules: exactly 5 replies, exactly 1 decoy. `decoy_slot` duplicated at top level so the server never
parses reply objects to score. Slot order is shuffled by seed derived from the post id, so a round is
reproducible. Real reply authors are shown at reveal only, never during guessing.

## WebSocket protocol (server <-> web)

Client -> server: `{"t": "join", "room": "abc", "name": "PLAYER1"}`,
`{"t": "guess", "room": "abc", "slot": 2, "ms": 8450}`, `{"t": "next", "room": "abc"}`.

Server -> client: `{"t": "state", ...}` full room state on every change:

```json
{"t": "state", "room": "abc", "phase": "lobby|guessing|reveal",
 "players": [{"name": "PLAYER1", "score": 2, "guessed": true, "guess_slot": 2}],
 "round": <Round with is_decoy and decoy_slot STRIPPED during guessing>,
 "reveal": {"decoy_slot": 2, "rationale": "...", "winner": "PLAYER1"} | null,
 "deadline_ms": 30000, "auto_ms": 10000}
```

The server strips `is_decoy`, `decoy_slot`, and real `author` values before broadcasting during the
guessing phase. Reveal restores them. First correct guess wins the round. Both wrong = house wins.

Sessions are time driven, with no host. The first player landing in a lobby arms a countdown
(`LOBBY_SECONDS`), a reveal arms the next-round countdown (`REVEAL_SECONDS`), and `auto_ms` reports
the time remaining on whichever countdown is live, null during guessing. A `next` from any joined
player skips the wait; it can never be load bearing, because the clock advances the room regardless.
Solo play is a real game against the house. Per-player `guess_slot` appears at reveal and only at
reveal; during guessing the strip rule keeps everyone's pick server side.

## Config (config.py)

Every model id is one value: `MODEL_TEXT = "grok-4.5"`, `MODEL_IMAGE = "grok-imagine-image"`,
`MODEL_VIDEO = "grok-imagine-video-1.5"`, `MODEL_VOICE = "grok-voice-think-fast-2.0"` (pinned, not
-latest). `ARCADE_MODE` env var: `demo` (default, fixtures only, zero network) or `live`.

## Fixtures

`fixtures/api/` content-addressed replay, ported from Adjacency. The default is read-only replay,
and `ARCADE_RECORD=1` writes. The demo must complete with the network cable pulled.

## Measured surface behavior (probes)

image gen 6.5s (live-safe), x_search 42s (NEVER inline, rounds pre-build into a queue),
voice token mint 0.13s, TTS 1.78s (requires `language` field). Host lines are pre-rendered mp3s
via TTS at build time. Realtime S2S is a live enhancement, not a dependency.
