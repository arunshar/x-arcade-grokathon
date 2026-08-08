# X Arcade

A retro arcade cabinet for X. The first cartridge is Decoy: a real post, five
replies, four written by real people and one written by Grok. Spot the machine
in thirty seconds.

The stage demo runs fully offline. Rounds, host voice lines, and the share
card are pre-built assets committed to the repo. Live mode is an enhancement
that turns on real xAI calls.

## Quick start

```sh
git clone <repo-url> x-arcade
cd x-arcade
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
./run.sh
```

Open two browser tabs at http://localhost:8787. In each tab enter a player
name and the same room code, then hit JOIN ROOM. The round starts the moment
the second player joins. Tap the reply you think the machine wrote before the
timer runs out. First correct pick wins the round, and NEXT ROUND keeps the
match going.

## Modes

- `ARCADE_MODE=demo` (default): fixtures and committed assets only, zero
  network. This is the mode the demo runs in.
- `ARCADE_MODE=live`: real xAI API calls. Needs `XAI_API_KEY` in the
  environment. `ARCADE_RECORD=1` refreshes fixtures while live.

### Live Imagine + Voice

With a key from [console.x.ai](https://console.x.ai):

```sh
export XAI_API_KEY=xai-...
ARCADE_MODE=live ./run.sh
```

| Feature | What happens in live |
|---------|----------------------|
| **Imagine** | After reveal, `card_forge` calls `POST /v1/images/generations` and the share card URL is re-broadcast (~6.5s). Demo still serves the committed card instantly. |
| **Voice** | Browser `GET /token` mints an ephemeral realtime secret, opens Grok voice, and speaks host cues. Any failure falls back to the committed `host_*.mp3` files. |

Optional: `ARCADE_RECORD=1` writes fixtures while calling the API (useful for refreshing demo assets). Live without record hits the API directly and persists nothing.

## Deploying your own

Two placeholders are left in the docs on purpose. `github.com/arunshar/x-arcade-grokathon`
is wherever you push this, and `YOUR-SPACE.hf.space` is wherever you host it.
`deploy/huggingface/` holds a Dockerfile and a staging script for a Hugging
Face Space, which runs the app in demo mode with no secrets attached.

Note the two ports. `run.sh` serves on 8787 for local development. The Space
Dockerfile serves on 7860, because that is the port a Hugging Face Space
expects.

The lobby QR in `web/static-assets/qr.png` is a placeholder. It encodes
`http://localhost:8787/?room=GROK`, which only resolves on the machine running
the server. A phone cannot reach another machine's localhost, so you must
regenerate the QR against a hosted URL before any crowd demo:

```sh
python -c "import segno; segno.make('https://YOUR-SPACE.hf.space/?room=GROK', error='h').save('web/static-assets/qr.png', scale=8, border=2, dark='#04070B', light='#FFFFFF')"
```

## Checks

- `python server/selfcheck.py` runs the scripted websocket contract check.
- `python integration_check.py` plays a full offline two-client round against
  the real queue and writes the transition trace to
  `artifacts/integration_trace.txt`.

## Layout

- `server/app.py` FastAPI app: websocket rooms, health, voice token, static web
- `cartridges/decoy/` round builder and the committed round queue
- `plugins/safety/` deterministic gates applied to every round at load
- `services/` voice host, share card forge, poster
- `web/` vanilla JS arcade client, no build step
- `fixtures/api/` content-addressed record and replay of xAI calls
