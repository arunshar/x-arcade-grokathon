# X Arcade teammate runbook

Khang (kunfupen) and Yiqi (Vivo50E), this is the day-of runbook. Today is Saturday 8 August 2026 and
the xAI Grokathon in San Francisco is today. Read section 1 and section 2, get the demo running on
your own machine, then pick a workstream from section 4.

---

## 1. Read this first

X Arcade is a retro arcade cabinet for X, and its first cartridge is Decoy. A round shows one real X
post and five replies, four written by real people and one written by Grok, and you get thirty
seconds to pick the machine. The server is a single FastAPI file that runs websocket rooms, the
client is vanilla JavaScript with no build step, and the rounds are pre-built JSON files committed to
the repo. Every round, host voice line, and share card is a committed asset replayed from fixtures,
so the whole thing plays end to end with the network cable pulled. Live mode turns on real xAI calls
and is an enhancement, not a dependency.

**State right now.** The shell was built shortly before the event, in public commits.
That is disclosed on stage and it is stated once here so nobody has to guess. The demo works. Two
browser clients play a full match offline today, and `integration_check.py` proves it with a socket
guard that hard-fails on any connection outside loopback. The committed run at
`artifacts/integration_trace.txt` shows 5 servable rounds, one gated out on purpose, 6 rounds played,
and the line `integration: ALL CHECKS PASSED (6 rounds played, zero network egress)`.

Nothing on the critical path is broken. The remaining work is enhancement and live-data refresh. The
single highest-value thing we can add today is rounds pulled from this morning's X threads, because
that is the part of the demo that can honestly be about today.

The two files that define everything are `CONTRACT.md` (the round shape, the websocket protocol, the
config rule, the fixture rule) and `DEMO.md` (the stage script, the preflight, and the fallback
tree). Read `CONTRACT.md` in full before you write any code. It is short.

---

## 2. Get running in five minutes

Clone it. The owner will paste the exact remote URL in chat, since the repo name in the docs is a
placeholder.

```bash
git clone <repo-url> x-arcade
```

```bash
cd x-arcade
```

```bash
python3 -m venv .venv && . .venv/bin/activate
```

```bash
pip install -r requirements.txt
```

```bash
./run.sh
```

`requirements.txt` holds three packages: `fastapi`, `uvicorn[standard]`, `websockets`. There is no
frontend build step and no npm.

Open two browser tabs at `http://localhost:8787`. In each tab type a player name and the same room
code, then hit JOIN ROOM. The round starts the moment the second player joins. Tap the reply you
think Grok wrote before the timer runs out.

Use any room code except `GROK` while you are testing. `GROK` is the one arena room in
`ARENA_ROOMS`, and arena rooms never auto-start. There you have to hit START from the first tab that
joined. See the arena bullet in section 3 and workstream C item 4.

**What you should see.** A dark arcade cabinet with a DECOY badge and a DEMO badge in the top bar. A
source post, five reply cards, and a countdown ring. After both players guess, a reveal panel with a
winner banner, a score strip, the decoy rationale, and a share card image.

Confirm the server agrees:

```bash
curl http://localhost:8787/health
```

You should get `{"mode": "demo", "rounds_available": 5}`. If the count is not 5, something changed
the round files or the safety gates. Find out which round before you go further.

If you want to see every screen with no server, no venv, and no network at all, open the client
straight off disk with the mock flag:

```bash
open -a "Google Chrome" 'file:///Users/arunsharma/code/x-arcade-public/web/index.html?mock=1'
```

The `file://` prefix is required. Hand `open` a bare filesystem path with `?mock=1` on the end and it
looks for a file with a question mark in its name, then exits 1 with "does not exist."

That path is the owner's checkout. Use your own clone path. Mock mode plays two hardcoded fixture
rounds against a bot player called GLITCH. It is also the last-ditch stage fallback in `DEMO.md`, so
it matters more than a dev toy.

---

## 3. What already works

Each line names the file that proves it.

**Game core**

- Websocket rooms, three client message types (`join`, `guess`, `next`), full room state broadcast on
  every change. `server/app.py`
- Server-authoritative 30 second round timer driven by an asyncio task, with the value in one place
  as `ROUND_SECONDS = 30`. `config.py`, `server/app.py`
- The guessing-phase strip is real. `_round_view` removes `decoy_slot` and `decoy_rationale` and
  rebuilds each reply as `{slot, text}` only, so `is_decoy` and the real author never reach a client
  during guessing. `server/app.py`
- First correct guess in server arrival order wins. Both wrong means the house wins. The client
  reported milliseconds are display data only. `server/app.py`
- Leaderboard computed server-side at every reveal, sorted by score then name, truncated to the top
  five. `server/app.py`
- Arena rooms are a real room type. `ARENA_ROOMS = {"GROK"}`, the host is the first joiner, and a
  `next` from a non-host in an arena room is ignored. `server/app.py`

**Content pipeline**

- Six committed round files, five of which pass the safety gates and serve.
  `cartridges/decoy/rounds/`
- Rounds load with zero registration. `queue.py` globs `rounds/*.json`, sorts by filename, and cycles.
  `cartridges/decoy/queue.py`
- Every round is re-screened at load time and the on-disk safety block is overwritten with the fresh
  result, so a gated round is never served. `server/app.py`
- Five deterministic safety gates with no model call, no network, and no randomness: `G_SOURCE`,
  `G_SLURS`, `G_DECOY_COUNT`, `G_AUTHOR`, `G_URL`. `plugins/safety/screen.py`, `plugins/safety/SAFETY.md`
- The live round builder pulls a real post and its replies from X, writes the decoy, shuffles by a
  seed derived from the post id, and validates against the contract before saving.
  `cartridges/decoy/round_builder.py`
- Fixtures are content-addressed, verify their own integrity on replay, refuse to cache credentials,
  and write atomically. `fixtures_core.py`
- Round provenance is documented per topic, including the source post URLs and why two topics carry
  richer search queries. `cartridges/decoy/rounds/README.md`

**Client**

- Vanilla JS arcade client, no build step, single-column reply grid under 620px, 100dvh body, iOS
  safe input sizing. `web/game.js`, `web/style.css`
- QR join by URL parameter. `?room=GROK` prefills the room field and leaves the on-screen QR block
  hidden, so the big screen shows the code and the phone does not. `#lobbyQr` ships with the `hidden`
  attribute in `web/index.html` and `web/game.js` only unhides it when there is no `?room` parameter.
  `web/game.js`, `web/index.html`
- Audio unlock on first pointerdown, mute state persisted to `localStorage`, every audio failure path
  swallowed so sound can never break the game. `web/game.js`
- Two host voice lines fire today, on the transition into guessing and into reveal. `web/game.js`
- A complete no-server fallback under `?mock=1` that reimplements the contract in the browser,
  including the stripping rules. `web/game.js`

**Voice and assets**

- All five host lines are rendered and committed as mp3s under `web/static-assets/`.
- The realtime ephemeral token mint is implemented and has a recorded live probe.
  `services/voice_host.py`, `artifacts/probes/voice_token.json`
- Every model id is pinned in one file, including a voice id pinned deliberately because the
  `-latest` alias moved on 5 Aug 2026. `config.py`
- The full browser wiring plan for live realtime voice is written down, with an honest UNVERIFIED
  marker on the parts that were never probed. `services/REALTIME_NOTES.md`

**Verification and deploy**

- `integration_check.py` boots the server in-process, monkeypatches `socket.socket.connect` to
  hard-fail on any non-loopback host, builds its own answer key by re-screening the round files, and
  plays every servable round plus one wrap round over two websocket clients.
- `server/selfcheck.py` plays three scripted rounds and asserts a distinct outcome for each,
  including a round where nobody guesses and the server timer alone forces the reveal.
- A Hugging Face Space deployment that is offline by construction, with `ENV ARCADE_MODE=demo` baked
  into the image. `deploy/huggingface/Dockerfile`, `deploy/huggingface/stage.sh`
- Probe artifacts back the surface numbers quoted in the docs. `artifacts/probes/`

---

## 4. What is left

**Nobody is assigned to anything here.** I do not know your skill sets, so every workstream carries a
"you want to be comfortable with" line instead of a name. Read those lines, pick what fits, and say
in chat which one you took so two people do not land in the same file. Ordered by value to the demo,
highest first.

Every **Effort** figure below is my estimate. None of this work has been done or timed, so treat the
minutes as a rough ordering signal and not as a measurement.

### A. Same-day rounds from live X

This is the workstream that changes what we can honestly say on stage. `DEMO.md` preflight step 8 is
a checkbox: if the same-day refresh ran and passed the gates, the rounds on screen are from this
morning's threads. If it did not run, the rounds are from the original build and the presenter says so.

**Effort:** about 100 minutes total. **Difficulty:** medium.
**You want to be comfortable with:** Python, shell, and reading rule code closely. Several steps in
this workstream block on slow API calls.
**Prerequisites:** a working `XAI_API_KEY` in the environment, and `ARCADE_REUSE_FIXTURES` unset.
**Files to touch:** `cartridges/decoy/round_builder.py`, `cartridges/decoy/rounds/`,
`cartridges/decoy/rounds/README.md`, `fixtures/api/`.

Step one is ALREADY DONE as of commit `7a4e012`. Do not redo it. It is described here so you
understand what the builder now reports and why.

`round_builder.py` line 451 used to read `from plugins.safety import screen_round`. That import
always raised `ImportError`, because `screen_round` lives in the submodule `plugins.safety.screen`
and there is no `__init__.py` anywhere in this repo. The `except` branch then stamped every round it
built with `{"screened": false, "gate_codes": []}`, so the builder never told you a round was
unservable. Serving was never at risk, because `server/app.py` re-screens every round at load. The
builder was the blind part. It now reads `from plugins.safety.screen import screen_round`, the form
`integration_check.py` already used correctly.

Confirm it is still correct before you build anything:

```bash
python3 -c "import sys; sys.path.insert(0,'.'); from cartridges.decoy.round_builder import _screen; import json; print(_screen(json.load(open('cartridges/decoy/rounds/decoy_ai.json'))))"
```

That must print `{'screened': False, 'gate_codes': ['G_SOURCE', 'G_URL']}`. If it prints an empty
gate list, the import is still broken.

Step two, build. One topic first, so you learn the timing before spending six of them:

```bash
ARCADE_RECORD=1 python3 cartridges/decoy/round_builder.py --live --topic music
```

Then the full set:

```bash
ARCADE_RECORD=1 python3 cartridges/decoy/round_builder.py --live --all
```

Each topic makes two grounded search calls and one writer call. `CONTRACT.md` records `x_search` at
about 42 seconds measured at build time, and there is a broad-search retry that can double the two
search calls, so budget generously.

Step three, screen everything and decide what to do with failures:

```bash
python3 -c "import json,glob,sys; sys.path.insert(0,'.'); from plugins.safety.screen import screen_round; [print(p, screen_round(json.load(open(p)))) for p in sorted(glob.glob('cartridges/decoy/rounds/*.json'))]"
```

**How to verify the whole workstream:** the builder prints an `OK <topic>` line with a fresh round id
and source URL per topic, the screen above reports `screened: True` for every round you intend to
serve, `curl http://localhost:8787/health` reports the matching count, and `python integration_check.py`
reaches ALL CHECKS PASSED with the new round ids in the trace. Then update the provenance table in
`cartridges/decoy/rounds/README.md` so every `post_url` in it matches the JSON on disk.

If a theme word is announced today and you only need new content rather than new rules, do not create
a new cartridge directory. There is no cartridge discovery. Hand-author or generate
`cartridges/decoy/rounds/decoy_<theme>.json` and it joins the cycle with no code change. Copy
`decoy_music.json` as the template, never `decoy_ai.json`, because music passes every gate and ai
does not.

### B. Wire the three unused host voice lines

`host_round.mp3`, `host_win.mp3`, and `host_lose.mp3` are rendered, committed, and served, but the
`sounds` object in `web/game.js` only registers `intro` and `reveal`. Anyone reading `DEMO.md`
expects five cues and hears two. This needs no key, no socket, and no live mode.

**Effort:** 40 minutes. **Difficulty:** easy.
**You want to be comfortable with:** vanilla JS and `HTMLAudioElement`.
**Prerequisites:** none.
**Files to touch:** `web/game.js`.

Add the three entries to the `sounds` object and fire win or lose off `state.reveal.winner` in the
reveal branch of `handleState()`. Register every sound before the first user gesture. `unlockAudio()`
iterates `Object.values(sounds)` once and is bound with `{ once: true }`, so a sound added later never
gets unlocked.

**How to verify:** open the client with `?mock=1`, click once to unlock audio, play a full round, and
confirm in DevTools Network that all five mp3s are requested across a round. Guess the decoy to hear
the win line, guess wrong to hear the lose line.

### C. Stage-path fixes

Five small things that each look cosmetic and each break something the stage script depends on.

**Effort:** 85 minutes for all five, summing the per-item figures below. **Difficulty:** easy, except
the arena gate which is medium.
**You want to be comfortable with:** vanilla JS for the first three, plus Python and FastAPI
websockets for the arena gate.
**Prerequisites:** none.

1. **Mock-mode share card is a broken image.** `web/game.js` sets `share_card_url` to
   `static-assets/share_card.png`, and that file does not exist. The one committed card is
   `web/static-assets/cards/decoy-3f2710c0a9e6_demo.jpg`. The `onerror` handler hides the element, so
   it fails silently, on the applause beat of our last-ditch fallback. Repoint it. 10 minutes.
   Verify: open `?mock=1`, reach a reveal, confirm the card renders with no failed request in Network.

2. **The QR points at the wrong port.** `README.md` states `web/static-assets/qr.png` currently
   encodes `http://localhost:7860/?room=GROK`. 7860 is the Hugging Face Space port from
   `deploy/huggingface/Dockerfile`. `run.sh` serves 8787. A phone scanning that code during a
   laptop-served demo reaches nothing. Regenerate it for the URL we will actually serve, using the
   `segno` one-liner already in `README.md`. That package is not in `requirements.txt`, so
   `pip install segno` into the venv first. 15 minutes, blocked only on knowing the final URL.
   Verify: scan the regenerated PNG with a phone on the hotspot and confirm the join screen opens with
   the room prefilled.

3. **Every QR-joined phone sees an enabled START button.** `renderLobby` enables `#startBtn` whenever
   joined and `players.length >= 1`, but in an arena room the server drops `next` from anyone who is
   not the host. The state payload carries no host field. The practical fix inside an hour is to hide
   `#startBtn` when `PREFILL_ROOM` is set and show a waiting line instead. 20 minutes.
   Verify: `/?room=GROK` on a phone shows no START button, and `/` on the laptop still shows one.

4. **Arena cold start.** In the `next` handler, a lobby start requires two players, but an arena host
   who opens the room before anyone scans in is alone. The host taps a lit button and the server
   silently drops the message. Allow a lobby start when the room is an arena and the sender is the
   host. This one touches `server/app.py`. 20 minutes, medium.
   Verify: join room GROK alone, tap START, confirm the round begins, then confirm
   `python server/selfcheck.py` and `python integration_check.py` both still pass.

5. **Connection state is invisible during play.** `#connLine` sits inside the lobby section, which
   gets the `hidden` attribute the moment the phase leaves lobby, so "LINK LOST, RETRYING..." never
   shows during a round. Add a status chip to the top bar and have `setConn()` write to it too.
   20 minutes. Verify: join a round, kill the server, confirm the top bar shows the retry state.

### D. Phone and projector polish

The demo runs on a volunteer's phone in front of a projector. Every item here is CSS or a few lines
of JS, and each is independently shippable.

**Effort:** 10 to 20 minutes each, so 90 to 180 minutes for all nine. **Difficulty:** easy.
**You want to be comfortable with:** CSS, responsive layout, and iOS Safari quirks.
**Prerequisites:** none.
**Files to touch:** `web/style.css`, `web/index.html`, and small edits in `web/game.js`.

- Scroll the reveal into view when the phase flips. On a phone the single-column reply grid pushes the
  winner banner and the NEXT button below the fold, so the reveal lands with no visible change.
- Add `env(safe-area-inset-*)` padding. `index.html` declares `viewport-fit=cover` but no rule in
  `style.css` uses the insets, so the top bar runs under the notch in landscape.
- Make the join form work with the phone keyboard. There is no `<form>` and no keydown handler, so
  Return does nothing. Add an Enter handler plus `autocapitalize`, `autocorrect`, and `enterkeyhint`.
- Add a `theme-color` meta so the browser chrome is dark instead of the system default. One line, no
  new asset. An apple-touch-icon would need an image that does not exist in the repo.
- Declare `--accent`. `style.css` uses `var(--accent, #22d3ee)` for the QR border and the leader score
  glow, but `--accent` is never defined in `:root`, so two slightly different cyans sit next to each
  other on the reveal strip.
- Reserve space for the share card with an aspect-ratio, so the NEXT button does not jump when the
  image lands.
- Pin NEXT ROUND with `position: sticky` during reveal so the host can always reach it.
- Grow the lobby QR on wide viewports. It is fixed at 180px while the source PNG is 296px.
- Guard animations behind `prefers-reduced-motion`.

**How to verify:** run `./run.sh --host 0.0.0.0`, open the app on an actual phone at
`http://<laptop-ip>:8787/?mock=1`, and walk a full round in both portrait and landscape.

### E. Live realtime voice

The biggest single piece left, and the one most likely to eat the day. `services/REALTIME_NOTES.md`
is the whole spec in 103 lines and its five sections map one to one onto the work. Take this only if
the earlier workstreams have owners.

**Effort:** 180 minutes for the downlink alone, plus about 40 minutes of prerequisites. Add 150 for
microphone uplink and 60 for the fallback ladder. **Difficulty:** hard.
**You want to be comfortable with:** the WebAudio API, base64 pcm16 decoding, WebSocket subprotocols,
and browser autoplay policy.
**Prerequisites:** a real `XAI_API_KEY` and `ARCADE_MODE=live`, because demo mode returns an empty
token value. Do the two prerequisites below first.

Prerequisite one, 15 minutes: reconcile the token path. `REALTIME_NOTES.md` names
`GET /api/voice-token` in both the prose and the copy-paste fetch snippet, but the route implemented
in `server/app.py` is `GET /token`. Pick one or register an alias. Because static files are mounted
at `/`, a request to the wrong path does not 404 cleanly at the router. It falls through to the static
handler and looks like an asset problem.

Prerequisite two, 25 minutes: return the pinned model id from the token endpoint. `config.MODEL_VOICE`
is a Python constant no client can read, and the notes hardcode the id into the wss URL in JS. That
defeats the reason the id is pinned at all.

Then build `web/voice.js`: fetch the token, open the socket, send `session.update`, and play the audio
delta events. Output only, no microphone. That alone gives a live talking host.

**How to verify:** run with `ARCADE_MODE=live` and a real key, open the app, and confirm in DevTools
that the socket reaches `readyState` 1, that `session.update` is acknowledged, and that audible host
speech comes out with no console errors.

Note honestly what is not proven here. `REALTIME_NOTES.md` marks the three WebSocket subprotocol
strings UNVERIFIED, because the handshake itself was never probed. On the voice path only the token
mint and the TTS render have recorded probe artifacts, `artifacts/probes/voice_token.json` and
`artifacts/probes/tts.json`. Treat the wss snippet as a plan, not as tested code. If you do
connect successfully, add a probe to `services/probe_surfaces.py` that records the negotiated
subprotocol into `artifacts/probes/`, then edit the UNVERIFIED paragraph to cite that artifact.

Whatever you build, keep the committed mp3s as the fallback rung. Any socket error must drop to them
instantly and silently.

### F. Harden the verification gate

Low glamour, real value, because everything else in this document depends on these two scripts telling
the truth.

**Effort:** 15 to 60 minutes per item. **Difficulty:** easy to medium.
**You want to be comfortable with:** Python, asyncio, sockets, and shell or Make.
**Prerequisites:** none.
**Files to touch:** `integration_check.py`, `server/selfcheck.py`, `services/probe_surfaces.py`, and a
new `Makefile` if you add one.

- One entry point that runs both checks and fails fast. There is no `tests/` directory, no Makefile,
  and no CI config, so the gate is two commands in the right order that a teammate has to remember.
- Port preflight. `integration_check.py` hardcodes 8788 and `selfcheck.py` hardcodes 8899, and neither
  probes before binding. A stale server from an aborted run currently shows up as a confusing
  assertion failure instead of a clear message.
- Stop the gate from dirtying the tree. `integration_check.py` unconditionally rewrites
  `artifacts/integration_trace.txt`, which is a tracked file. The trace embeds `deadline_ms` values,
  so two runs almost never produce identical bytes and `git status` comes back dirty after a passing
  run. That trains people to ignore it.
- Cover arena rooms. `server/app.py` defines `ARENA_ROOMS = {"GROK"}` with host-only advancement, and
  that is the exact room the stage demo uses. Neither check script touches it.
- Fix the stale nested-shape parsing in `services/probe_surfaces.py`. It reads `client_secret`, but
  the recorded artifact shows the response is flat with keys `expires_at` and `value`, so the probe
  reports a false negative on a 200 response. 15 minutes.
- Add an opt-in live-mode test for `/token` that skips without `XAI_API_KEY`, so the default suite
  stays offline.

**How to verify:** each item ships with its own check. Break one assertion deliberately and confirm
the gate exits nonzero.

### G. Post back to X

`services/poster.py` is staged. It prints four lines and returns a deterministic fake permalink built
from a sha256 digest. It imports nothing but `hashlib`, `pathlib`, and `typing`, has no network code,
and has zero callers anywhere in the repo.

**Effort:** 240 minutes for the real call alone, plus safety screening and a server trigger.
**Difficulty:** hard.
**You want to be comfortable with:** OAuth user-context auth and hand-rolled HTTP, because this repo
has no OAuth library and every outbound call is stdlib `urllib`.
**Prerequisites:** OAuth user-context credentials for the arcade account. None exist in the repo.
**Files to touch:** `services/poster.py`, `config.py`, `server/app.py`, `plugins/safety/screen.py`,
`web/index.html`, `web/game.js`, `DEMO.md`, `README.md`.

Read this one before you take it. The target endpoint is a different host from `config.API_BASE` and a
different auth model from the bearer key every other service uses, so `XAI_API_KEY` will not
authenticate there. The exact media upload URL, whether the upload is chunked, the OAuth flavor, and
any rate limits are all UNVERIFIED. None of them appear anywhere in this repo. The `$0.015` per post
figure lives in the module docstring of `services/poster.py`, which labels it provider-quoted,
unverified, and not yet incurred, so it is a note and not a measurement. There is also no
`ARCADE_POST` switch anywhere in the repo, even though that same docstring instructs gating on it.

My honest read: this is the lowest-value workstream for today and the highest-risk one. `DEMO.md`
scripts the presenter to say posting back is staged, not live, and wiring the code without updating
both `DEMO.md` and `README.md` makes a spoken statement false. If you want an hour of value here
instead, do the three small safe pieces: add the `ARCADE_POST` switch so the default path can never
fire, fix the `__main__` block that points at a card file which does not exist and reports 0 bytes
instead of raising, and add a text-level safety gate for the player-supplied winner name.

### Lower priority, only if you have slack

- Resolve the status of `plugins/ads`. `sponsored_arena` has zero callers, while `ADS.md` and other
  docs assert that gates run before any arena is served. Either wire it or add a line to `ADS.md`
  saying the module has no caller today.
- Decide whether `G_URL` should also scan `source.post_text`. It scans replies only, though the post
  text does render in the client.
- Remove the unreachable `"decoy"` branch in `_gate_author`, or comment why it stays. Short-circuit
  order means only `"@decoy"` can ever match.
- Add unit tests for the five safety gates, including malformed-shape cases that pin the fail-closed
  contract.
- Add a regression check that offline replay reproduces every committed round exactly. The rounds
  README claims this property and nothing enforces it.
- Resolve the `ARCADE_MODE` no-op in the round builder. Every documented command sets it for a script
  that never reads it.

---

## 5. Suggested three-way split

**This is a suggestion, not an assignment. Renegotiate it in chat in the first ten minutes.** It is
built to minimize merge conflicts, not to match anyone's strengths, because I do not know your
strengths. If two of you want to swap lanes, swap lanes and just tell the third person.

**Lane 1, content and Python.** Workstream A, then the safety and builder items from the lower
priority list. Owns `cartridges/`, `plugins/safety/`, `fixtures/`, and the rounds README. This lane
touches almost nothing anyone else touches, and it is the one with a hard dependency on wall-clock
time, so start it first.

**Lane 2, presentation.** Workstream D, plus stage-path items 1, 2, and 3 from workstream C. Owns
`web/style.css` and `web/index.html` outright, and owns the render functions and the QR prefill block
inside `web/game.js`.

**Lane 3, server and client wiring.** Workstream B, plus stage-path items 4 and 5 from workstream C,
then workstream F. Owns `server/app.py`, `integration_check.py`, `server/selfcheck.py`, and owns the
audio layer, `handleState`, and `mockSocket` inside `web/game.js`.

Lanes 2 and 3 both edit `web/game.js`, which is the one real conflict surface. Split it by region:
lane 3 owns the sounds object, `handleState`, and the mock socket at the bottom, and lane 2 owns
`renderLobby`, `renderReplies`, `renderReveal`, and the QR block. Commit small and push often so a
conflict is ten lines, not a hundred.

Workstream E is the stretch goal for whoever clears their lane first, and workstream G is the one to
leave alone unless someone turns up OAuth credentials.

---

## 6. Before you push

From the repo root, with the venv active. All four commands, in this order.

```bash
cd /Users/arunsharma/code/x-arcade-public
```

```bash
. .venv/bin/activate
```

```bash
python server/selfcheck.py
```

```bash
python integration_check.py
```

Both scripts must exit 0. `integration_check.py` must print ALL CHECKS PASSED and the zero-network-egress
line. Do not run either script from a subdirectory. `integration_check.py` resolves the repo root from
`__file__`, but the trace path and the round glob both hang off that and running it from elsewhere is
untested.

If you touched round files or the safety gates, also start the server and check the count:

```bash
curl http://localhost:8787/health
```

If you touched the client, walk one full round with `?mock=1` and one full round against the real
server. The mock path is a second implementation of the contract living in the browser and nothing in
the gate tests it.

A modified `artifacts/integration_trace.txt` after a passing run is expected, not a symptom. It is a
tracked file and the check rewrites it every time.

---

## 7. Gotchas

The traps that would burn an hour.

1. **The safety screen never runs at build time.** `round_builder.py` line 451 imports `screen_round`
   from `plugins.safety`, which is a namespace package with no `__init__.py`, so the import always
   raises and the `except` branch stamps every round `screened: false` with an empty gate list. All
   six committed round files carry that false stamp on disk even though five of them pass the real
   gates. Do not read the on-disk safety block as authoritative and do not hand-edit it to true. Fix
   the import. A pairing of `screened: false` with an empty `gate_codes` list is impossible output
   from `screen_round`, so treat it as a wiring signal rather than a gate result.

2. **`decoy_ai.json` fails on purpose.** It trips `G_SOURCE` (post text is 2650 characters against a
   560 limit) and `G_URL` (a youtube link in reply slot 2). Do not shorten it or strip the link to
   make it pass. The rounds README documents the rejection, `DEMO.md` scripts a spoken beat that names
   both gate codes on stage, and `integration_check.py` asserts the servable count. Making it pass
   breaks all three at once.

3. **There is no cartridge discovery.** `server/app.py` hardcodes one import of the decoy queue.
   Nothing scans `cartridges/` for subdirectories. Creating a new cartridge directory and walking away
   produces a server that still serves only Decoy, with no error and no warning.

4. **A gated round is skipped in silence.** `_next_round()` wraps the queue loop in a bare except and
   falls back to a hardcoded round. If every new theme round is gated out, the demo plays the same
   fallback round repeatedly with no log line saying why. Confirm with `/health` and
   `integration_check.py`, never by watching the UI.

5. **`ARCADE_MODE` does nothing in the round builder.** Live is selected only by the `--live` CLI
   flag. Setting `ARCADE_MODE=live` and forgetting `--live` gives you a silent offline replay that
   looks like a successful build. Separately, `--live` without `ARCADE_RECORD=1` records nothing, so
   the round lands but can never replay offline afterward.

6. **`ARCADE_REUSE_FIXTURES=1` defeats a fresh pull.** With it set, the fixture store returns the
   cached response instead of calling the API even in record mode. `services/card_forge.py` defaults
   that same variable to `1` while the round builder defaults it off, so a shell that exports it for
   one script quietly changes the other. Check it is unset before recording.

7. **Serve order is sorted filename order.** A theme round named `decoy_aardvark.json` jumps to the
   front of the demo sequence and becomes the first thing the audience sees. Name deliberately.

8. **The queue caches its file list.** `queue._paths` is loaded once and `queue._index` is a module
   global shared by every room in the process. Dropping a round file in while the server is running
   does not change what is served until restart, even though `/health` picks it up immediately because
   it globs fresh. The two can disagree.

9. **Four ports, and none of the scripts probes before binding.** `run.sh` uses 8787,
   `integration_check.py` uses 8788, `server/selfcheck.py` uses 8899, and the Space Dockerfile uses
   7860. A stale listener on any of them makes a healthy codebase look broken.

10. **`run.sh` binds localhost only.** Without `./run.sh --host 0.0.0.0` no phone can reach the
    laptop. The script passes extra flags straight through to uvicorn, so the flag works.

11. **Passing `selfcheck.py` says nothing about the round files.** It sets `ARCADE_FORCE_FALLBACK=1`,
    so the server serves a hardcoded round and never touches `cartridges/decoy/rounds/`. It also has
    no socket guard, so it is not offline proof. Only `integration_check.py` exercises the real queue
    and proves zero egress.

12. **Neither check uses the shipped round timer.** `integration_check.py` sets `ROUND_SECONDS` to 15
    and `selfcheck.py` sets it to 2. The real value is 30 in `config.py`. Never read a timer figure out
    of a check script.

13. **Static files are mounted at `/` last, on purpose,** so `/ws`, `/health`, and `/token` win the
    route match. Any new route added below that mount is shadowed by the static handler and its
    failure looks like a missing asset.

14. **`?mock=1` is a second implementation of the contract** living in `web/game.js`, with its own
    fixture rounds, its own stripping logic, and its own hardcoded 30000ms deadline. Nothing in the
    gate tests it, so it drifts from `server/app.py` silently. If you change the protocol, change it
    in both places.

15. **The player name is attacker-controlled.** The 16-character cap is a `maxlength` attribute in the
    browser only. The server accepts whatever a hand-rolled websocket client sends, and that name
    already flows into the share-card image prompt.

---

## 8. House rules

Three rules. They are not style preferences. They are what lets us stand behind everything we say
today.

**Never invent a number for the deck or the pitch.** Every figure said out loud has a file path in the
numbers card at the bottom of `DEMO.md`, and anything not on that card does not get said. The measured
surface numbers live in `CONTRACT.md` and trace to recorded artifacts under `artifacts/probes/`. If
you need a number that does not exist yet, run the thing and record the artifact, or leave the claim
out. A "COMPLETED" job is not a result until you have read the actual output.

**Every claim cites an artifact path.** If you assert a latency, a pass rate, a round count, or a
response shape, name the file that proves it. `artifacts/integration_trace.txt` is a recording of a
past run, not a live result, so re-run `integration_check.py` yourself before quoting a number from
it. Anything you infer but did not verify gets marked UNVERIFIED in the doc where you write it, the
way `services/REALTIME_NOTES.md` already marks the WebSocket subprotocol strings.

**Anything staged is described as staged.** `services/poster.py` returns a fake permalink and touches
no network, and the docs say so in three places. The host voice lines are mp3s pre-rendered at build
time, not synthesized on stage. The shell was built before the event. Every one of those is fine, and every
one of them stays true only if we keep saying it. If you make a staged thing real, update the code and
both `DEMO.md` and `README.md` in the same change, so the spoken script never describes a state the
repo left behind.
