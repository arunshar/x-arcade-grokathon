# Realtime voice wiring notes

How the browser talks to Grok voice live, so wiring it tomorrow is a 30-minute
task. The demo does not depend on any of this. Pre-rendered mp3s in
`web/static-assets/` cover every scripted moment offline.

## What is verified (probed 7 Aug 2026)

- `POST https://api.x.ai/v1/realtime/client_secrets` with the server key and an
  empty JSON body returns 200 in 0.13s.
- Response shape is flat: `{"value": "<ephemeral token>", "expires_at": <unix seconds>}`.
  There is no nested `client_secret` object. Read `value` at the top level.
- `voice_host.mint_token()` wraps this call. `python3 services/voice_host.py mint`
  smoke-tests it and prints the shape only, never the secret.

## What is standard OpenAI-Realtime convention (verify on first connect)

The surface is OpenAI-Realtime compatible. The socket handshake itself was not
probed today, so treat the exact subprotocol strings as UNVERIFIED until the
first live connect. Everything else below is the compatible-API convention.

### 1. Token endpoint on our server

The FastAPI server exposes a tiny route, for example `GET /api/voice-token`,
which calls `voice_host.mint_token()` and returns `{"value": ..., "expires_at": ...}`
to the browser. The real `XAI_API_KEY` never leaves the server. Tokens are
short-lived, so mint per session, not at page load.

### 2. Browser connects

```js
const { value } = await (await fetch("/api/voice-token")).json();
const ws = new WebSocket(
  "wss://api.x.ai/v1/realtime?model=grok-voice-think-fast-2.0",
  [
    "realtime",
    "openai-insecure-api-key." + value,
    "openai-beta.realtime-v1",
  ],
);
```

Browser WebSocket cannot set an Authorization header, so the ephemeral token
rides in the subprotocol list. That is the OpenAI-compatible browser pattern.
The model id comes from `config.MODEL_VOICE` and is pinned on purpose.

### 3. Session setup

First message after `open`:

```js
ws.send(JSON.stringify({
  type: "session.update",
  session: {
    voice: "Eve",
    instructions: "You are the Decoy arcade host. Short lines, high energy, never reveal the decoy slot.",
    turn_detection: { type: "server_vad" },
    input_audio_format: "pcm16",
    output_audio_format: "pcm16",
  },
}));
```

Mic audio goes up as `input_audio_buffer.append` events with base64 pcm16.
Host audio comes back as `response.output_audio.delta` events. Play them
through a WebAudio `AudioWorklet` or queue decoded chunks into an
`AudioBufferSourceNode` chain.

### 4. The force-messages trick for scripted lines

The game needs the live host to say exact scripted lines on cue (round start,
reveal, win, lose) while staying a free-talking host between cues. Force a
line by creating a response with per-response instructions:

```js
ws.send(JSON.stringify({
  type: "response.create",
  response: {
    instructions: 'Say exactly this, nothing more: "Four humans. One machine. Thirty seconds."',
  },
}));
```

Two rules that make it reliable:

- Cancel any in-flight response first with `{"type": "response.cancel"}` or the
  scripted line queues behind whatever the host was riffing on.
- Per-response `instructions` override the session instructions for that one
  response only, which is exactly what a cue needs.

An alternative that also works: `conversation.item.create` with a `system`
role item containing the cue, then a bare `response.create`. The per-response
instructions form is one message shorter, so prefer it.

### 5. Fallback ladder

1. Live realtime host through the socket above.
2. Pre-rendered mp3s from `voice_host.LINES`, committed in
   `web/static-assets/`. The demo runs on this rung and it can never fail.

The client should treat any socket error as an instant, silent drop to rung 2.
The scripted line texts in `voice_host.LINES` are identical to the mp3
content, so the two rungs are interchangeable mid-game.
