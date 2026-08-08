# X Arcade: the engineering guide

## What this is, plainly

### The story

Picture a police lineup where the witness is looking at words instead of faces. Five short replies
to the same X post stand behind the glass. Four were typed by real people. One was written by a
machine that read the other four first. You get thirty seconds and one pick, and the first correct
pick in the room takes the point.

A small squad runs this lineup, and each of them has exactly one job.

The Scout goes out to X and finds a real conversation that happened in the last two days. He is not
allowed to make one up. He has to come back with proof that he actually went looking, and if he
cannot show that proof, he is sent out again.

The Forger reads the four real replies the Scout brought back. She writes a fifth reply that talks
the way they talk, misspells things the way they misspell things, and cares about what they care
about. Then, on a separate slip of paper, she writes down the one tell she thinks she left behind.

The Shuffler decides who stands where. He flips a coin that always lands the same way for the same
conversation, so the lineup can be rebuilt exactly, months later, with the power off.

The Sergeant inspects the lineup before a witness is let anywhere near it. Five standing orders, no
exceptions, no appeals. He has already thrown out one perfectly interesting lineup for breaking two
of them.

The Duty Officer runs the witness room. She holds the answer card, and the answer card never crosses
the glass. Not once, not for anyone, until the thirty seconds are up.

Then she turns the card over. The machine's reply lights up red, the four real names go up beside
the human replies, and the Forger's slip is read out, so the room leaves knowing what gave her away.

### The technical version

X Arcade is a single-process FastAPI application (`server/app.py`, 471 lines) that serves a
build-step-free vanilla JavaScript client over one WebSocket endpoint, with in-memory rooms and no
database. Its first cartridge, Decoy, presents a real X post with five replies, four pulled verbatim
from the source thread and one written by `grok-4.5`, and scores the first player to identify the
generated one within a server-enforced 30-second deadline. Every generative call was moved out of the
request path: rounds are built offline by `cartridges/decoy/round_builder.py` through three model
calls (two `x_search`-grounded `/responses` calls that are rejected unless the response shows real
tool use, and one non-searching `/chat/completions` write), then shuffled by a PRNG seeded from a
hash of the source post id and committed as JSON. Five deterministic safety gates in
`plugins/safety/screen.py` re-screen every round at load time and a fail-closed result skips it. The
guessing-phase broadcast is a projection that removes `decoy_slot`, `decoy_rationale`, and per-reply
`is_decoy` and `author`, so the answer never reaches a client that could read it. Host audio and the
demo share card are pre-rendered artifacts committed to the repo, and `integration_check.py`
monkeypatches `socket.socket.connect` so that any connection attempt off loopback fails the run. A
full match plays through under that guard. The guard's exact scope is in the fixture chapter.

---

## Contents

1. [The whole loop in one page](#the-whole-loop-in-one-page)
2. [How a round is born](#how-a-round-is-born)
3. [The safety gates](#the-safety-gates)
4. [The server, the rooms, and the anti-cheat](#the-server-the-rooms-and-the-anti-cheat)
5. [The client and the deployment](#the-client-and-the-deployment)
6. [Voice and generated media](#voice-and-generated-media)
7. [Demo mode, live mode, and the fixture layer](#demo-mode-live-mode-and-the-fixture-layer)
8. [What is real and what is staged](#what-is-real-and-what-is-staged)

---

## The whole loop in one page

One round, start to finish, in order, with the file that owns each step.

**Days before the demo, offline.**

1. `cartridges/decoy/round_builder.py` `build_round()` runs. `_find_post()` posts an `x_search` prompt
   to `/responses` and rejects any answer whose usage block shows no search calls.
2. `_fetch_replies()` makes a second grounded call against the same thread, then `_parse_replies()`
   drops short replies, repeat authors, and the original poster, and keeps the first four survivors.
3. `_write_decoy()` posts to `/chat/completions` with no tools at all, and gets back a fake reply plus
   a one-sentence rationale naming its own tell.
4. `_assemble()` hashes the post id into a seed and a `round_id`, appends the decoy to the four real
   replies, shuffles with `random.Random(seed)`, and assigns slots by index.
5. `validate_round()` checks the shape, and `_build_and_save()` writes
   `cartridges/decoy/rounds/decoy_<topic>.json`.
6. Every API call above routed through `fixtures_core.py`, which recorded request and response under
   `fixtures/api/<surface>/<sha256>.json`.
7. Separately, `services/voice_host.py` rendered five host lines to `web/static-assets/*.mp3`, and
   `services/card_forge.py` generated the share card now committed at
   `web/static-assets/cards/decoy-3f2710c0a9e6_demo.jpg`.

**At boot.**

8. `run.sh` starts uvicorn on `server/app.py`. That module imports `cartridges/decoy/queue.py` and
   `plugins/safety/screen.py` by name, and mounts `web/` at `/` last so the API routes win.

**A player joins.**

9. A phone scans `web/static-assets/qr.png`, which encodes
   `https://arun0808-x-arcade.hf.space/?room=GROK`, and the static mount serves `web/index.html`.
10. `web/game.js` opens a WebSocket to `/ws` at page load and sends `{"t":"join"}` when the player taps
    JOIN. `server/app.py` `_get_room()` creates the room and elects the first joiner as host.

**The round starts.**

11. The host taps NEXT. `server/app.py` `_start_round()` calls `_next_round()`, which pulls a file
    through `cartridges/decoy/queue.py` `next_round()` and screens it with
    `plugins/safety/screen.py` `screen_round()`, skipping any round that fails.
12. `_start_round()` sets a deadline on the loop clock, spawns `_round_timer()`, and broadcasts.
13. `_public_state()` calls `_round_view()`, which rebuilds the round without `decoy_slot`,
    `decoy_rationale`, `is_decoy`, or real author handles.

**The player guesses.**

14. `web/game.js` `handleState()` sees a new `round_id`, resets the local guess, restarts the ring
    timer, and plays `web/static-assets/host_intro.mp3`.
15. A tap runs `onGuess()`, which flips the card and sends `{"t":"guess","slot":N,"ms":…}`.
16. `server/app.py` validates the slot, stamps `guess_order` from a per-room counter, and stores the
    client's `ms` as display data only.

**The reveal.**

17. When every player has guessed, or when `_round_timer()` expires, `_do_reveal()` reads
    `rnd["decoy_slot"]`, awards the point to the lowest `guess_order` among correct guesses, builds
    the top-five leaderboard, and attaches `DEMO_CARD_URL` in demo mode.
18. In live mode only, `_attach_live_card()` renders through `services/card_forge.py` off the event
    loop and re-broadcasts.
19. The next broadcast carries the unstripped round. `web/game.js` `renderReplies()` marks the decoy
    card red, prints the rationale, and restores the real handles, `renderReveal()` fills the winner
    banner, the leaderboard, and the share card, and `handleState()` plays
    `web/static-assets/host_reveal.mp3`.
20. The host taps NEXT and step 11 runs again. `services/poster.py` would post the winner's card back
    to X at this point. It is staged, it has no caller, and it contains no network code.

---

## How a round is born

A round is the atom of X Arcade. One real X post, four real replies pulled verbatim from that post's
thread, one imposter reply written by Grok, shuffled into five slots. Players get thirty seconds to
point at the fake.

This chapter follows one round from nothing to a played game. Paths are relative to the repository
root, as they are everywhere else in this guide. Where a number appears it came out of a file or a
command that was actually run.

The worked example throughout is the `music` round, because its whole build is still on disk in the
fixture store and can be replayed end to end without a network.

### The destination: what a round has to look like

Read `CONTRACT.md` before anything else. It is 68 lines and it is the spine. Every other module
conforms to the Round JSON it declares.

Here is the real committed round, `cartridges/decoy/rounds/decoy_music.json`, annotated. This is the
file on disk, not a sketch.

```jsonc
{
  // sha256(post_id + ":" + ROUND_ID_SALT)[:12], prefixed "decoy-".
  // Stable across rebuilds: same source post always gives the same id.
  "round_id": "decoy-63ce6cb6a7de",

  "source": {
    // Verbatim post text. Safety caps this at 560 chars (MAX_POST_CHARS).
    "post_text": "tbh the music your parents played growing up stays with you for the rest of your life.",
    // Normalized to a leading "@" by _clean_handle().
    "post_author": "@peegzy1",
    // Canonical x.com status url. validate_round() requires it to start with "http".
    "post_url": "https://x.com/peegzy1/status/2085306680955334992",
    // Free-form string. It is whatever was passed to --topic, not an enum.
    "topic": "music"
  },

  // Exactly 5. slot MUST equal the array index.
  "replies": [
    {"slot": 0, "text": "I can't even lie, I no fit leave Ayefele songs.", "author": "@OlajideJ7", "is_decoy": false},
    {"slot": 1, "text": "It get stored in your head forever", "author": "@chizymusik", "is_decoy": false},
    {"slot": 2, "text": "Facts — hearing one old song and suddenly you’re back in the backseat of their car.", "author": "@devops_prashant", "is_decoy": false},
    // The imposter. Its author is the literal string "decoy", never an @handle.
    {"slot": 3, "text": "Ebenezer Obey songs still dey my head", "author": "decoy", "is_decoy": true},
    {"slot": 4, "text": "Tope alabi songs", "author": "@oluwasegun84872", "is_decoy": false}
  ],

  // Duplicated at the top level so the server never parses reply objects to score.
  "decoy_slot": 3,

  // Display only. Shown at reveal, stripped during guessing.
  "decoy_rationale": "The mix of correct artist capitalization with dropped articles feels slightly over-crafted compared to the uneven casual typos in the real replies.",

  // Written by the builder but effectively decorative. See the safety chapter.
  "safety": {"screened": false, "gate_codes": []},

  // int(sha256(post_id).hexdigest()[:12], 16) % 1_000_000_007. Drives the shuffle.
  "seed": 6214443
}
```

Note that the slot 2 reply text contains a literal em dash. That is what the human typed on X, and it
is reproduced verbatim here and in the reply-filter table later in this chapter. Quoted data and
quoted source comments are reproduced exactly, punctuation included. Nothing else in this guide uses
an em dash.

Two invariants everything downstream depends on: exactly five replies, exactly one decoy. `decoy_slot`
is carried at the top level so the scoring path in the server never has to walk the reply list looking
for `is_decoy`.

### Where the machine starts

The whole builder is one file, `cartridges/decoy/round_builder.py`, 622 lines. Its own docstring
states the design in the first 25 lines, and it is worth reading before the code.

The CLI is `main()` at line 593.

```
ARCADE_MODE=live ARCADE_RECORD=1 python3 cartridges/decoy/round_builder.py --live --topic ai
ARCADE_MODE=live ARCADE_RECORD=1 python3 cartridges/decoy/round_builder.py --live --all
```

`--all` expands to `DEMO_TOPICS = ["ai", "sports", "movies", "crypto", "food", "music"]` (line 50).
`--topic` accepts any string. `DEMO_TOPICS` only controls what `--all` iterates.

One trap, and it is the first thing to know about this file. `--live` is what selects the live path.
`ARCADE_MODE` is read in `config.py` into `config.MODE`, and `round_builder.py` never references
`config.MODE` at all. Setting `ARCADE_MODE=live` and forgetting `--live` gives you a silent offline
replay that prints a successful-looking `OK` line. The documented command works because of the flag,
not the environment variable.

`main()` loops topics, calls `_build_and_save(topic, live=args.live)` (line 583), prints one line per
topic, and returns 1 if any topic failed.

```python
print(
    f"OK {topic}: {round_dict['round_id']} "
    f"decoy_slot={round_dict['decoy_slot']} "
    f"url={round_dict['source']['post_url']}"
)
```

### build_round: the five stages

`build_round(topic, live=False)` at line 557 is the whole pipeline in thirteen lines of code, nineteen
counting its docstring.

```python
def build_round(topic: str, live: bool = False) -> dict[str, Any]:
    store = _make_store(live)
    try:
        post = _find_post(store, topic)
        replies = _fetch_replies(store, post)
    except RoundBuildError:
        post = _find_post(store, topic, broad=True)
        replies = _fetch_replies(store, post)
    thread = {**post, "replies": replies}
    decoy_text, rationale = _write_decoy(store, thread)
    round_dict = _assemble(topic, thread, decoy_text, rationale)
    validate_round(round_dict)
    return round_dict
```

Five stages: find a post, read its replies, write the imposter, assemble and shuffle, validate. Note
the `except RoundBuildError` block. Both search calls are re-run with `broad=True`, which widens the
window from 48 hours to 7 days. That retry doubles the search spend on a bad topic. None of the six
committed fixtures used the broad prompt, so the broad path is UNVERIFIED against a live response.

Also note where the retry does not reach. If `_write_decoy` fails, the whole build fails. There is no
second attempt at the writer beyond its own two-payload fallback.

`_make_store(live)` at line 145 is the entire mode-to-fixture mapping:

```python
def _make_store(live: bool) -> FixtureStore | None:
    if live and not config.RECORD:
        return None
    return FixtureStore(
        root=REPO_ROOT / "fixtures" / "api",
        record=live and config.RECORD,
        reuse_existing=os.environ.get("ARCADE_REUSE_FIXTURES") == "1",
    )
```

Three outcomes. `live=False` gives a store in replay mode, which never touches the network. `live=True`
with `ARCADE_RECORD=1` gives a store in record mode, which calls the API and writes fixtures.
`live=True` without `ARCADE_RECORD` returns `None`, so `_call_surface` bypasses the store entirely and
hits the API with nothing persisted. The full truth table is in the fixture chapter.

Because `root`, `record`, and `reuse_existing` are all passed explicitly, the `ADJ_RECORD`,
`ADJ_FIXTURE_DIR`, and `ADJ_REUSE_FIXTURES` environment variables documented in `fixtures_core.py`
have no effect on this code path. That docstring is inherited from the Adjacency lineage and will
mislead you.

### Stage one: finding a real trending thread with x_search

`_find_post()` lives at line 332. It builds a prompt, wraps it in two payload variants, and posts to
`/responses`.

The prompt comes from `_find_prompt(topic, broad)` at line 291. The non-broad form, which is what all
six committed rounds used:

```python
return (
    f"Search X for one engaging post from the last 48 hours about {topic_query} "
    "with a healthy reply count, at least 5 replies. Pick a post whose replies "
    "are substantive full thoughts, not just emoji or tags. Return the numeric "
    "post id, the post text verbatim, the author handle, and the canonical "
    "x.com post url."
)
```

`topic_query` is `TOPIC_QUERIES.get(topic, topic)`, so a bare topic word is used unless it has an
override. Two topics have overrides (line 279), and the comment above them records why:

```python
# Topics whose plain name drags in off-topic or charged posts get a richer
# query. The first live run on the bare words found a partisan rant for
# movies and a football team dinner for food.
TOPIC_QUERIES = {
    "movies": (
        "movies, a new film, box office numbers, or actors, from a film focused "
        "account. Skip posts that are mainly political commentary"
    ),
    "food": (
        "food, cooking, restaurants, or recipes. Skip posts that are mainly "
        "about sports teams"
    ),
}
```

That is the prompt-tuning surface. If a new theme pulls garbage, add an entry here rather than editing
the prompt template.

#### The exact structured-output shape

`_search_payloads()` at line 309 builds two payloads and returns them in order. The first asks for
structured output. The second drops the schema and adds a plain-English instruction instead.

```python
structured = {
    "model": config.MODEL_TEXT,
    "input": prompt,
    "tools": [{"type": "x_search"}],
    "max_tool_calls": 8,
    "text": {
        "format": {
            "type": "json_schema",
            "name": schema_name,
            "schema": schema,
        }
    },
}
plain = {
    "model": config.MODEL_TEXT,
    "input": prompt + " " + plain_hint,
    "tools": [{"type": "x_search"}],
    "max_tool_calls": 8,
}
```

`config.MODEL_TEXT` is `"grok-4.5"`. Every model id lives in `config.py` and nowhere else. That file is
quoted in full in the fixture chapter, along with the reason the ids are pinned rather than aliased.

The schema passed for this call is `POST_SCHEMA` at line 52:

```python
POST_SCHEMA = {
    "type": "object",
    "properties": {
        "post_id": {"type": "string", "description": "Numeric status id of the post"},
        "post_text": {"type": "string"},
        "post_author": {"type": "string", "description": "Handle like @name"},
        "post_url": {"type": "string",
                     "description": "Canonical https://x.com/.../status/... url"},
    },
    "required": ["post_id", "post_text", "post_author", "post_url"],
    "additionalProperties": False,
}
```

Note the shape difference between the two API families. The `/responses` calls nest the schema under
`text.format` with a flat `name` key. The `/chat/completions` call in stage three nests it under
`response_format.json_schema`. They are not interchangeable.

The call itself goes through `_call_surface`, with a 300 second timeout:

```python
response = _call_surface(store, "x_search_post", payload, "/responses", 300)
```

`"x_search_post"` is the fixture surface name. It becomes a directory under `fixtures/api/`. The 300
second timeout is not paranoia. `CONTRACT.md` records x_search at about 42s, measured by the probe at
`artifacts/probes/x_search.json`, and the music post finder in the committed fixture made eleven
server-side search calls in one request.

#### The grounding guard

This is the most important defensive check in the file. `_made_tool_calls()` at line 220:

```python
def _made_tool_calls(response: dict[str, Any]) -> bool:
    usage = response.get("usage", {})
    details = usage.get("server_side_tool_usage_details", {}) or {}
    if details.get("x_search_calls", 0) > 0:
        return True
    return any(
        item.get("type") in ("custom_tool_call", "tool_call", "x_search_call")
        for item in response.get("output", [])
    )
```

If it returns False, `_find_post` raises `RoundBuildError("post finder made no x_search calls")` and
falls through to the next payload. The model is not allowed to answer from memory.

This guard demonstrably fired during the recorded build. The `food` topic has two committed post
fixtures, and comparing them shows exactly what happened.

The structured attempt,
`fixtures/api/x_search_post/8d820f7652882d5f5ad4c3336327dae0cfd69386f9632fa3840da8820756e1f1.json`,
has an `output` array of just `['reasoning', 'message']`, its `usage` block has no
`server_side_tool_usage_details` key at all, and `num_server_side_tools_used` is 0. It returned a
clean, well-formed, entirely ungrounded post about garlic butter steak bites. Running
`_made_tool_calls` on that stored response returns `False`.

The plain attempt,
`fixtures/api/x_search_post/81264f262b4bb4324e09dc32a45feb4568d048503e3d0e14bc51ac44b1c41dca.json`,
has a 30-item `output` array full of `custom_tool_call` entries, has `server_side_tool_usage_details`
present, and reports `num_server_side_tools_used: 14`. `_made_tool_calls` returns `True`. Its post id
is `2085787763488502009`, which is the post that actually landed in `decoy_food.json`.

The hallucinated post was recorded as a fixture and then discarded by the guard. That is a real,
on-disk example of the check earning its place.

#### Parsing the post

`_responses_text()` (line 199) walks `response["output"]`, collects every `content` entry whose `type`
is `output_text`, and joins them. `_extract_json()` (line 178) then tries three strategies in order:
parse the string as-is, strip a markdown code fence and parse again, then regex out the outermost
`{...}` and parse that. It raises `RoundBuildError` if none work.

`_parse_post()` (line 232) normalizes. If `post_id` is not all digits it recovers one from
`/status/(\d+)` in the url. It rejects a missing id, a url that does not start with `http`, and empty
post text. `_clean_handle()` prepends `@` if the model omitted it.

For music, the recorded `output_text` was exactly this, from
`fixtures/api/x_search_post/64deca11f56e5a3e576135498760910501c34ccbfa9b8b16bef9febd483f516b.json`:

```json
{
  "post_id": "2085306680955334992",
  "post_text": "tbh the music your parents played growing up stays with you for the rest of your life.",
  "post_author": "@peegzy1",
  "post_url": "https://x.com/peegzy1/status/2085306680955334992"
}
```

That response's `usage.server_side_tool_usage_details.x_search_calls` is 11, and its `output` array is
24 items alternating `reasoning` and `custom_tool_call` before the final `message`.

### Stage two: pulling four real replies

`_fetch_replies()` at line 355 is a separate call, deliberately. The module docstring explains why:

> A single combined call was tried first and the model invented plausible replies instead of reading
> the thread, so post and replies stay separate grounded calls, and any response that made no x_search
> call is rejected.

The prompt hands the model the exact search operator to use:

```python
prompt = (
    f"Open this X thread and read its replies: {post['post_url']} "
    f"(post id {post['post_id']}, posted by {post['post_author']}). "
    f"A reliable way is searching with the query "
    f"conversation_id:{post['post_id']} filter:replies . "
    "List up to 8 replies that you actually read from the thread, each with "
    "the real author handle and the reply text verbatim. Only include "
    "replies you read in the search results. Never invent, merge, or "
    "paraphrase a reply. Skip replies from the original poster. If you "
    "cannot read the thread, return an empty replies array."
)
```

Same two-payload pattern, this time with `REPLIES_SCHEMA` (line 70), which is an object with a single
`replies` array of `{author, text}`. Same 300 second timeout, surface `"x_search_replies"`. Same
grounding guard, with a blunter message: `"reply fetch made no x_search calls, refusing unverified
replies"`.

#### The reply filter is strict

`_parse_replies()` at line 254 is where most thin threads die.

```python
def _parse_replies(raw, post_author):
    seen_authors = {post_author.lower()}
    replies = []
    for reply in raw.get("replies") or []:
        author = _clean_handle(str(reply.get("author", "")))
        text = str(reply.get("text", "")).strip()
        if not text or not _substantive(text) or author.lower() in seen_authors:
            continue
        seen_authors.add(author.lower())
        replies.append({"author": author, "text": text})
    if len(replies) < 4:
        raise RoundBuildError(
            f"thread too thin: {len(replies)} substantive replies from distinct authors"
        )
    return replies[:4]
```

Four rules. Drop non-substantive replies. Drop repeat authors. Drop the original poster, whose handle
seeds `seen_authors`. Require at least four survivors, then keep exactly the first four.

`_substantive()` at line 215 strips urls first, then requires at least 12 characters and at least 3
words:

```python
def _substantive(text: str) -> bool:
    stripped = re.sub(r"https?://\S+", "", text).strip()
    return len(stripped) >= 12 and len(stripped.split()) >= 3
```

The music thread shows this working. The recorded response in
`fixtures/api/x_search_replies/50f1e1f4f32edf2f8b2432125b3b2cc5b1578b8ee17db211280f4e2cdbef1f9a.json`
returned eight replies. Walking the filter in file order:

| # | author | text | verdict |
|---|---|---|---|
| 1 | `@oluwasegun84872` | "Tope alabi songs" | kept |
| 2 | `@chizymusik` | "It get stored in your head forever" | kept |
| 3 | `@Gee_fund_` | "Nbl" | dropped, 3 chars |
| 4 | `@devops_prashant` | "Facts — hearing one old song…" (verbatim) | kept |
| 5 | `@Ogologoxx` | "Not true" | dropped, 8 chars |
| 6 | `@OlajideJ7` | "I can't even lie, I no fit leave Ayefele songs." | kept, and this is the fourth |
| 7 | `@YX_cellency` | "It's true for all hues of music. Sound and character." | dropped by `[:4]` truncation |
| 8 | `@c_isiah_rich` | "True" | never reached |

Reply 7 would have passed the substantive check. It was cut by the slice, not the filter. `[:4]` is
positional, so the model's ordering of results decides which real replies make the round.

That reply fetch used just one `x_search_call`, against eleven for the post finder. The operator hint
does its job.

### Stage three: writing the imposter

`_write_decoy()` at line 390 is the only call that does not use `x_search`, and the only one that goes
to `/chat/completions`.

It first builds a briefing containing the post and the four surviving reply texts, with the authors
deliberately withheld:

```python
briefing = json.dumps(
    {
        "post_text": thread["post_text"],
        "real_replies": [r["text"] for r in thread["replies"]],
    },
    ensure_ascii=False,
    indent=2,
)
```

The system prompt, `WRITER_SYSTEM` at line 103, is the game design in one paragraph:

```python
WRITER_SYSTEM = (
    "You write one fake reply that must hide among real replies to a real X post. "
    "Match the register of the thread exactly: typical reply length, tone, "
    "capitalization, punctuation habits, typo level, and slang. Stay on topic and "
    "plausible on its own. Do not copy phrases from the real replies. Never hint "
    "that you are an AI. Also write a one sentence rationale naming the subtle "
    "tell that makes your reply artificial. Players see the rationale only after "
    "the reveal."
)
```

The rationale requirement is the interesting part. The model is asked to write a convincing fake and
then to name its own tell. That self-reported tell is what the reveal screen shows the player, so the
round teaches something after it is scored.

The structured payload for this API family looks different from the `/responses` ones:

```python
structured = {
    "model": config.MODEL_TEXT,
    "messages": [
        {"role": "system", "content": WRITER_SYSTEM},
        {"role": "user", "content": user},
    ],
    "response_format": {
        "type": "json_schema",
        "json_schema": {"name": "decoy_reply", "schema": DECOY_SCHEMA},
    },
}
```

`DECOY_SCHEMA` (line 90) requires `reply_text` and `rationale`. There are no `tools` and no
`max_tool_calls`, because this call must not search. The timeout is 180 seconds, surface
`"decoy_write"`.

The response is read at `response["choices"][0]["message"]["content"]`, passed through `_extract_json`,
and both fields are required non-empty. The `except` clause here catches `KeyError` and `IndexError`
too, because a malformed choices array is a realistic failure.

The music writer output, verbatim from
`fixtures/api/decoy_write/472fa009406d8d2f35588eab42a931cd9aa900fe9047f5f1e6476b74c9790b30.json`:

```json
{"reply_text":"Ebenezer Obey songs still dey my head","rationale":"The mix of correct artist capitalization with dropped articles feels slightly over-crafted compared to the uneven casual typos in the real replies."}
```

Look at what it did. The real thread is Nigerian, code-switching between English and Pidgin, and two
replies name Yoruba gospel and juju artists (Tope Alabi, Ayefele). The imposter names Ebenezer Obey and
uses "still dey my head". It matched the register.

### Stage four: assembly, the seed, and the shuffle

`_assemble()` at line 466 turns a thread plus a decoy into the contract shape.

```python
post_id = thread["post_id"]
seed = int(hashlib.sha256(post_id.encode()).hexdigest()[:12], 16) % 1_000_000_007
round_id = (
    "decoy-"
    + hashlib.sha256(f"{post_id}:{ROUND_ID_SALT}".encode()).hexdigest()[:12]
)
```

`ROUND_ID_SALT` is `"x-arcade-decoy-v1"` (line 49).

#### Why the seed derives from the post id

The seed is a pure function of the source post id. Nothing else feeds it. No timestamp, no random draw,
no build counter. That gives three properties the rest of the system relies on.

It makes the build reproducible. Rebuild the same post from the same fixtures and you get the same slot
order, so the round file is stable and diffable. That property is what
`cartridges/decoy/rounds/README.md` is claiming when it promises byte-for-byte offline reproduction.
The promise holds for the content and no longer holds for the whole file. The `safety` block is the
one key that has drifted, for reasons in the replay section below.

It makes the answer independent of the file. The shuffle is not stored anywhere secret. Anyone with the
post id can recompute the seed. The secrecy of the answer during a round comes entirely from the
server's stripping, never from obscurity in the seed.

It makes the id and the layout agree. `round_id` and `seed` both hash the same post id with different
treatments, so a round file cannot be half-updated into an inconsistent state.

Verified against the committed music round. Running the derivation on post id `2085306680955334992`
gives `seed = 6214443` and `round_id = decoy-63ce6cb6a7de`, which is exactly what the file holds.

#### The shuffle

```python
entries = [
    {"text": r["text"], "author": r["author"], "is_decoy": False}
    for r in thread["replies"]
]
entries.append({"text": decoy_text, "author": "decoy", "is_decoy": True})
random.Random(seed).shuffle(entries)

replies = [
    {"slot": slot, "text": entry["text"], "author": entry["author"],
     "is_decoy": entry["is_decoy"]}
    for slot, entry in enumerate(entries)
]
decoy_slot = next(r["slot"] for r in replies if r["is_decoy"])
```

Note the ordering. The decoy is always appended last, then the list is shuffled, then slots are assigned
by enumeration. Without the shuffle the decoy would sit at index 4 in every single round and the game
would be trivial.

`random.Random(seed)` is a dedicated generator instance, not the global `random` module state. That
matters. It means the shuffle cannot be perturbed by anything else in the process calling `random`.

The shuffle reproduces exactly. Feeding
`[@oluwasegun84872, @chizymusik, @devops_prashant, @OlajideJ7, decoy]`, which is the order those four
survived `_parse_replies` plus the appended decoy, through `random.Random(6214443).shuffle` yields slot
0 `@OlajideJ7`, slot 1 `@chizymusik`, slot 2 `@devops_prashant`, slot 3 `decoy`, slot 4
`@oluwasegun84872`. That is the committed file, position for position.

The author string for the decoy is the literal `"decoy"`, not an `@handle`. That is required by the
`G_AUTHOR` safety gate, which skips decoy replies and demands a leading `@` on every other author while
explicitly rejecting `"decoy"` and `"@decoy"` as real handles.

### Stage five: validation

`validate_round()` at line 512 is the machine-readable version of `CONTRACT.md`. It collects every
problem before raising, so one run tells you everything wrong.

What it enforces:

- reply count equals `config.REPLIES_PER_ROUND`, which is 5
- for every reply, `slot` equals its array index, `text` is non-empty, `author` is non-empty
- exactly one reply has `is_decoy` true
- that decoy's `slot` equals the top-level `decoy_slot`
- all four `source` fields present and non-empty
- `source.post_url` starts with `http`
- `round_id` starts with `decoy-`
- `seed` is an `int`
- `decoy_rationale` is non-empty
- `safety` is a dict containing both `screened` and `gate_codes`

The `slot == index` check is positional. A round whose reply array is in a different order from its slot
numbers parses fine as JSON and passes the safety screen, then fails here. That is the intended catch.

The last check is the weak one. `validate_round` verifies the safety block has the right keys. It does
not check that `screened` is true. A round that will never be served still passes validation and still
prints `OK`.

### The safety block the builder writes

`_assemble` calls `_screen(round_dict)` at line 508. `_screen` is at line 449:

```python
def _screen(round_dict: dict[str, Any]) -> dict[str, Any]:
    try:
        from plugins.safety.screen import screen_round
    except ImportError:
        return {"screened": False, "gate_codes": []}
    ...
```

That import is correct as the file stands today, so a fresh build now stamps the real gate result. It
did not always. Until commit `7a4e012` the line read `from plugins.safety import screen_round`, which
raises, and the `except ImportError` turned every build into `{"screened": false, "gate_codes": []}`.
All six committed round files were built before that fix and still carry the old stamp. The full
diagnosis and the reason the serving path was never affected are in the safety chapter.

Two build-loop consequences survive the fix. `_screen` still swallows a broken screen silently, so a
failure to import is indistinguishable from a dirty round. And `validate_round` still never checks the
value, so a round that will never be served prints `OK` either way.

One round in the committed set is genuinely unservable. `decoy_ai.json` fails two gates on real pulled
content. That result, and why you must not fix it, is the centerpiece of the next chapter.

### Landing on disk

`_build_and_save()` at line 583:

```python
def _build_and_save(topic: str, live: bool) -> dict[str, Any]:
    round_dict = build_round(topic, live=live)
    ROUNDS_DIR.mkdir(parents=True, exist_ok=True)
    path = ROUNDS_DIR / f"decoy_{topic}.json"
    path.write_text(
        json.dumps(round_dict, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return round_dict
```

`ROUNDS_DIR` is `Path(__file__).resolve().parent / "rounds"`, so `cartridges/decoy/rounds/`.

The filename is `decoy_<topic>.json` with no timestamp and no id. One file per topic, overwritten every
rebuild. `indent=2` and `ensure_ascii=False` mean the files stay readable and diffable in git, which is
the point. `write_text` is not atomic here, unlike the fixture writes.

Two consequences worth planning around. Rebuilding a topic destroys the previous round for that topic,
so if you want to keep it, copy it first or rename the new one. And the filename decides serve order.

### queue.py loads and serves it

`cartridges/decoy/queue.py` is 50 lines and holds the entire loading mechanism.

```python
ROUNDS_DIR = Path(__file__).resolve().parent / "rounds"

_paths: list[Path] = []
_index: int = 0


def _load_paths() -> list[Path]:
    global _paths
    if not _paths:
        _paths = sorted(p for p in ROUNDS_DIR.glob("*.json"))
        if not _paths:
            raise FileNotFoundError(
                f"no round files in {ROUNDS_DIR}. Build them with round_builder.py"
            )
    return _paths


def round_count() -> int:
    return len(_load_paths())


def next_round() -> dict[str, Any]:
    global _index
    paths = _load_paths()
    path = paths[_index % len(paths)]
    _index += 1
    return json.loads(path.read_text(encoding="utf-8"))


def reset() -> None:
    global _index, _paths
    _index = 0
    _paths = []
```

There is no manifest, no registry, no index file. Dropping a valid `.json` into `rounds/` is the entire
enrollment step. The current cycle is `decoy_ai, decoy_crypto, decoy_food, decoy_movies, decoy_music,
decoy_sports`, which is alphabetical, not build order and not topic order. A file named
`decoy_aardvark.json` becomes the first thing an audience sees.

Three properties of this module matter before you change anything near it.

The path list is cached on first call. A round file added while the server is running is invisible until
restart or a `reset()` call.

`_index` is a module global shared by every room in the process. Two concurrent rooms interleave through
one shared cycle rather than each walking the list independently.

`next_round` re-reads and re-parses the file on every call. Nothing is held in memory between rounds, so
editing a round file changes what is served on its next turn through the cycle, as long as the file set
has not changed.

`queue.py` never screens. It hands back whatever JSON it finds. Filtering is the server's job.

### Where the server takes over

`server/app.py` line 32 is the only place a cartridge is bound:

```python
from cartridges.decoy import queue as decoy_queue
from plugins.safety import screen as safety_screen
```

That import is hardcoded. Nothing scans `cartridges/` for subdirectories. There is no cartridge
discovery in this repository.

From here the round belongs to the server, and the serve path is the subject of the server chapter. Two
consequences of the queue design are worth carrying forward now, because they surprise people during a
demo.

A gated round still consumes a queue position. `decoy_ai.json` sorts first, so the first file the queue
offers is the one that fails screening. The server skips it and pulls the next, and `_index` has already
advanced. The sequence an audience actually sees is crypto, food, movies, music, sports, and back
around.

`/health` and the served cycle can disagree. The health endpoint globs the rounds directory fresh on
every request rather than using the queue, so it sees a newly added file immediately while the cached
`_paths` list does not.

### The fixture layer, in one paragraph

Every API call above went through `_call_surface`, which routes through `fixtures_core.py`. A fixture
key is `sha256` over a canonical JSON of `{format_version, request, surface}`, and the file lands at
`<root>/<surface>/<digest>.json`.
Replay verifies the shape, the format version, the surface, the request, the request hash against both
the stored field and the filename stem, and the response hash. The mechanics, the guards, and the
failure modes are the subject of the fixture chapter.

The payoff is verifiable here. Running `build_round(topic, live=False)` for all six topics with
`ARCADE_RECORD`, `ARCADE_MODE`, and `ARCADE_REUSE_FIXTURES` unset, and comparing each returned dict to
its committed file:

```
ai       replay==committed: True
crypto   replay==committed: True
food     replay==committed: True
movies   replay==committed: True
music    replay==committed: True
sports   replay==committed: True

ALL MATCH: True
```

Every key reproduces: `round_id`, `seed`, `decoy_slot`, every reply text, every author, the rationale,
and the `safety` block. The whole three-call live pipeline is reconstructible offline from committed
bytes, and that run touches no network.

This was briefly untrue. Before commit `7a4e012` the builder's safety import was broken, so it stamped
every round it wrote with `{"screened": false, "gate_codes": []}`, and those stamps went into the
committed files. Fixing the import meant a replay computed the real gate result while the committed
files still held the placeholder, so `safety` was the one key that diverged. The round files were
regenerated so their stamps match what the corrected screener returns. Only the `safety` block changed
in that regeneration. No post text, reply, author, seed, or slot was touched.

### Traps, in the order you are likely to hit them

**`ARCADE_MODE` does nothing in the builder.** Live is selected only by `--live`. Setting the variable
and omitting the flag gives a silent replay that prints `OK`.

**`--live` without `ARCADE_RECORD=1` records nothing.** `_make_store` returns `None`, the calls hit the
API directly, and no fixture is written. The round file lands, but the demo can no longer reproduce it
and `build_round(topic, live=False)` will raise `FixtureMissError`.

**`ARCADE_REUSE_FIXTURES=1` defeats a fresh pull.** With it set, `FixtureStore.call` returns the cached
fixture instead of invoking the API even in record mode. Note that `services/card_forge.py` defaults
this same variable to `"1"` while the builder defaults it off, so a shell that exports it for one script
quietly changes the other.

**The `ADJ_*` variables in the `fixtures_core.py` docstring do not apply here.** The builder passes
`root`, `record`, and `reuse_existing` explicitly and shadows all three.

**Re-recording overwrites the post fixture and orphans the rest.** The post prompt contains no date
literal, so its request hash is identical across days and a re-record replaces the file in place. The
reply and writer fixtures key off the new post id and the new briefing, so those land as new files and
the old ones stay on disk unreferenced.

**The safety block in a round file means nothing.** The six committed files carry a stale false stamp
from the old builder import bug, and the server overwrites the field on every serve anyway.

**A gated round costs you a queue position, silently.** No log line, no warning, and `/health` will not
tell you which round was skipped.

**The reply gate kills thin threads.** Under 12 characters or under 3 words after url stripping means
dropped. Duplicate authors dropped. Original poster dropped. Fewer than four survivors and the whole
build fails.

**Any URL in any reply text gates the round out.** Post text may contain a url without tripping
`G_URL`, but that url still counts against the 560 character cap in `G_SOURCE`.

**The reply count 5 is hardcoded in three independent places.** `config.REPLIES_PER_ROUND`, the
guess-slot bounds check in the websocket handler, and the `_replies()` length check in
`plugins/safety/screen.py`. Changing one changes nothing.

**Adding a round file while the server runs does not change the served sequence.** `queue._paths` is
cached. Restart, or call `queue.reset()`.

### The mental model, compressed

A round is built once, offline, by a script nobody runs during the demo. It is built from three model
calls: two grounded searches that must prove they searched, and one write that must not search. It is
turned into five slots by a shuffle seeded from the source post id, so the layout is a pure function of
the source. It is written to a JSON file whose name decides its place in the queue. It is picked up by a
fifty-line module that does nothing but sort and cycle. It is screened by five rules the moment it is
served, and stripped of every answer field before it reaches a player.

The whole design pushes latency and risk to build time. `x_search` at roughly 42 seconds cannot sit in a
request path, so it does not. A model that might hallucinate cannot be trusted at serve time, so it is
called days earlier and its output is committed, screened, and reproducible.

---

## The safety gates

`plugins/safety/` is two files and about 160 lines total. It is also the piece of X Arcade that the
pitch leans on hardest, because it is the only component that decides whether content reaches a human.
Read `plugins/safety/SAFETY.md` first (eleven lines, no code), then `plugins/safety/screen.py`.

### The public surface is one function

The whole module exports one callable and one constant tuple.

```python
def screen_round(round_dict: dict[str, Any]) -> dict[str, Any]:
    """Run every gate and report the failures.

    Returns {"screened": bool, "gate_codes": [failed codes]}. A round is
    screened only when the failure list is empty. Malformed input fails the
    gates that cannot verify it, which keeps the screen fail closed.
    """
    failed = [code for code, gate in _GATES if not gate(round_dict)]
    return {"screened": not failed, "gate_codes": failed}
```

That is the entire runner. Note what `screened` is: it is not an independent field, it is `not failed`
computed over the same list that gets returned. The two halves of the result cannot disagree. This
matters later, because it makes one particular value on disk provably impossible as real output.

`GATE_CODES` is derived, never hand-written:

```python
_GATES: tuple[tuple[str, Any], ...] = (
    ("G_SOURCE", _gate_source),
    ("G_SLURS", _gate_slurs),
    ("G_DECOY_COUNT", _gate_decoy_count),
    ("G_AUTHOR", _gate_author),
    ("G_URL", _gate_url),
)

GATE_CODES = tuple(code for code, _ in _GATES)
```

Verified by running it: `GATE_CODES` is `('G_SOURCE', 'G_SLURS', 'G_DECOY_COUNT', 'G_AUTHOR',
'G_URL')`. Adding a gate means adding one tuple entry. The code list and the runner cannot drift apart,
because there is only one list.

The module docstring states the design constraint plainly:

```python
"""Deterministic safety gates for Decoy rounds.

Every gate is a plain rule check. No model calls, no network, no randomness,
so screening is instant and gives the same answer every time. The rule is
fail closed. A round that fails any gate is never served (see SAFETY.md).
This is the lineage of Adjacency distilled to its demo relevant core.
"""
```

No model in the loop is a deliberate choice, not a shortcut. A screen that calls a model has latency,
cost, and nondeterminism, and it cannot be re-run on every round load without changing the answer
between runs. The server does re-run it on every round load. That only works because the gates are pure
functions of the round dict.

### Two helpers carry the fail-closed behavior

Four of the five gates start by calling `_replies`, which is the shape guard:

```python
def _replies(round_dict: Any) -> list[dict[str, Any]] | None:
    """Return the reply list, or None when the shape is wrong (fail closed)."""
    if not isinstance(round_dict, dict):
        return None
    replies = round_dict.get("replies")
    if not isinstance(replies, list) or len(replies) != REPLIES_PER_ROUND:
        return None
    if not all(isinstance(reply, dict) for reply in replies):
        return None
    return replies
```

`REPLIES_PER_ROUND` comes from `config.py` and is `5`. Every parameter type is checked, and every gate
that gets `None` back returns `False`. There is no branch that treats "I could not parse this" as "this
is fine".

The second helper, `_texts`, collects every string a text gate should scan:

```python
def _texts(round_dict: Any) -> list[str]:
    """Collect every string we can find so text gates scan all of them."""
```

It appends `source.post_text` when it is a str, then every `reply.text` that is a str. It never raises
on a wrong shape. It returns an empty list instead. Only `G_SLURS` uses it, which has a consequence
covered in the sharp-edges section.

### The five gates, one at a time

**`G_SOURCE`** (`_gate_source`) is the structural and length gate. It is the only one that checks the
source post at all. It requires:

- `_replies` returned a list, so exactly five reply dicts exist
- `round_dict["source"]` is a dict
- `source["post_text"]` is a `str`, is not blank after `.strip()`, and is at most `MAX_POST_CHARS = 560` characters
- every `reply["text"]` is a `str`, is not blank, and is at most `MAX_REPLY_CHARS = 280` characters

Why 560 and 280. 280 is one X reply. 560 is two of those, which gives the source post room to be a
long-form post without becoming a wall of text on the arena screen. `web/game.js` line 172 assigns the
post text straight into the DOM (`$("postText").textContent = src.post_text || ""`), so an unbounded
post is a layout failure on a projector, not just a data oddity.

**`G_SLURS`** (`_gate_slurs`) is a one-liner over `_texts`:

```python
def _gate_slurs(round_dict: Any) -> bool:
    """No denylist hit in the post text or any reply text."""
    return not any(_SLUR_PATTERN.search(text) for text in _texts(round_dict))
```

The pattern is a case-insensitive, word-boundary alternation built from a six-word tuple: `idiot`,
`moron`, `imbecile`, `scumbag`, `dumbass`, `jackass`. Each word goes through `re.escape` before the
join, so adding a word with regex metacharacters is safe.

The comment above the list is the important part, and you should not read past it:

```python
# Kept mild on purpose. The demo shows the mechanism. A real deployment swaps
# in a maintained wordlist behind the same gate code.
```

Six mild insults is not content moderation. `G_SLURS` passing tells you the mechanism is wired, not that
the round is clean. The gate code is the stable interface. The list behind it is a placeholder that a
real deployment replaces. `paper/VISION.md` says the same thing in its risks section: the shipped
denylist is small, and at scale the gates would need X's actual trust-and-safety tooling behind them.

**`G_DECOY_COUNT`** (`_gate_decoy_count`) protects scoring:

```python
decoys = [reply for reply in replies if reply.get("is_decoy") is True]
if len(decoys) != 1:
    return False
return round_dict.get("decoy_slot") == decoys[0].get("slot")
```

Note `is True`, not truthiness. A string, a `1`, or any other truthy value does not count as a decoy.

This gate exists because of a specific decision in `CONTRACT.md`. The contract duplicates `decoy_slot`
at the top level of the round "so the server never parses reply objects to score." Look at `_do_reveal`
in `server/app.py` and you will see it does exactly that: `decoy_slot = rnd["decoy_slot"]`, then it
compares every player's guess against that integer. The server never inspects `is_decoy` when deciding
a winner. That denormalization buys simplicity in the hot path, and it creates a consistency risk,
because two fields can now disagree. `G_DECOY_COUNT` is the check that pays for the denormalization. A
round where the top-level pointer and the flagged reply disagree would silently award the round to the
wrong slot forever. It never gets served.

**`G_AUTHOR`** (`_gate_author`) protects the reveal:

```python
for reply in replies:
    if reply.get("is_decoy") is True:
        continue
    author = reply.get("author")
    if not isinstance(author, str) or len(author) < 2:
        return False
    if not author.startswith("@") or author.lower() in _DECOY_MARKERS:
        return False
```

The decoy reply is skipped, because by contract it carries the literal author `"decoy"` (see the
`CONTRACT.md` round block and `_assemble` in `round_builder.py`, which hardcodes
`{"text": decoy_text, "author": "decoy", "is_decoy": True}`). Every other reply must carry a real
handle: a string, at least two characters, starting with `@`, and not equal to a decoy marker.
`_DECOY_MARKERS` is `("decoy", "@decoy")`.

The reason is the reveal. During guessing, `_round_view` in `server/app.py` reduces each reply to
`{"slot", "text"}`, which strips both `is_decoy` and the author. At reveal the full round goes out and
the handles appear next to the human replies. If a real reply could carry `@decoy` as its handle, the
reveal screen would label a human as the machine. That is the failure mode this gate blocks.

**`G_URL`** (`_gate_url`) rejects any reply whose text matches:

```python
_URL_PATTERN = re.compile(r"https?://\S+|www\.\S+|\bt\.co/\S+", re.IGNORECASE)
```

Three forms are covered: an `http://` or `https://` prefix, a bare `www.`, and a `t.co/` shortlink. The
docstring gives the stated reason, which is presentational rather than safety-critical: "URLs break the
game visually." A reply card in the arena is a short block of text on a projector. A pasted link is a
long unwrappable token that blows the card layout and, worse, gives away which replies were typed by a
human on a phone.

### What fail-closed means here, mechanically

Fail-closed in this codebase is a property of the control flow, not a convention anyone has to remember.

There is no `try/except` inside any gate. There is no "unknown" return value. Every gate returns `bool`.
Four of them return `False` the moment `_replies` hands back `None`. `screen_round` then computes
`screened` as `not failed`, so a single `False` anywhere makes the whole round unservable. `SAFETY.md`
states the operating rule in the same terms: "There is no override and no partial pass. The round
builder must produce a clean round instead."

I ran the real function against malformed input to confirm the behavior rather than infer it:

```
screen_round({})    -> {'screened': False, 'gate_codes': ['G_SOURCE', 'G_DECOY_COUNT', 'G_AUTHOR', 'G_URL']}
screen_round(None)  -> {'screened': False, 'gate_codes': ['G_SOURCE', 'G_DECOY_COUNT', 'G_AUTHOR', 'G_URL']}
```

A round with four replies instead of five gives the same four codes. `screen_round(None)` does not
raise, which matters because `_rounds_available` in the server iterates JSON files and screens each one.
A truncated or hand-edited file becomes a `False`, not an exception that takes out `/health`.

Look at which gate is missing from those lists. `G_SLURS` passes on `{}` and on `None`. That is not a
bug in the sense of letting bad content through, because `_texts` returned an empty list and there is
genuinely nothing to scan, and `G_SOURCE` already failed so the round is dead. It is worth knowing
anyway. `G_SLURS` is the one gate that does not fail closed on shape, and it should never be read alone
as evidence a round has real text in it.

### Where the gates run in the lifecycle

There are four call sites in the repo. All four work today. One of them was broken until recently, and
the round files on disk still show it.

**Build time, in `cartridges/decoy/round_builder.py`.** `_assemble` writes a `safety` block of
`{"screened": False, "gate_codes": []}` into the round dict, then overwrites it with
`round_dict["safety"] = _screen(round_dict)` before returning. `validate_round` afterwards only checks
the block is shaped like a dict with both keys, and never checks its value. This is the path that used
to swallow its own import error, covered below.

**Load time, in `server/app.py::_next_round`.** This is the one that actually protects a player, and it
is the canonical listing of how a round becomes servable:

```python
if not FORCE_FALLBACK:
    try:
        for _ in range(decoy_queue.round_count()):
            rnd = decoy_queue.next_round()
            gates = safety_screen.screen_round(rnd)
            rnd["safety"] = gates
            if gates["screened"]:
                return rnd
    except Exception:
        pass
fallback = copy.deepcopy(FALLBACK_ROUND)
fallback["safety"] = safety_screen.screen_round(fallback)
return fallback
```

Four things to see here.

`rnd["safety"]` is overwritten with the fresh result before the check, so the on-disk stamp is never
trusted and never consulted. All six files under `cartridges/decoy/rounds/` currently carry
`{"screened": false, "gate_codes": []}` even though five of them pass every gate. Never read that field
to decide whether a round works.

The loop is bounded by `round_count()`. The queue cycles forever, so without that bound a fully
gated-out queue would spin.

`FALLBACK_ROUND` is deep-copied and then screened by the same function on the same code path. There is
no unscreened exit from this function. I confirmed the fallback passes: `{'screened': True,
'gate_codes': []}`. The deep copy matters because `_do_reveal` and the card renderer both read the
served round, and a shared mutable module constant would accumulate state across rounds.

The `except Exception: pass` swallows a broken queue silently. There is no log line. If every round is
gated out, or the directory is missing, the demo plays the hardcoded fallback round over and over and
nothing tells you why. Check `/health` and run `integration_check.py`, never the UI.

**Health, in `server/app.py::_rounds_available`.** `/health` does not report how many files exist. It
re-screens every JSON file in the rounds directory on each call and counts the passes, falling back to
`1` when the count is zero or the read fails. That is why `DEMO.md` step 3 of preflight says to `curl`
`/health`, expect `rounds_available` of 5, and "If the count is not 5, stop and find out which round the
gates changed their mind on." The endpoint is a live gate result, not a file count.

**Proof, in `integration_check.py`.** `load_answer_key` screens every round file itself, splits the ids
into `servable` and `gated`, and then asserts per served round that `rid not in gated` and that
`rnd["safety"]["screened"] is True`. The check plays one full cycle plus one round to prove the queue
wraps. Its full assertion list is in the fixture chapter.

### The event: a live-pulled round the gates threw out

This is the part that is worth getting exactly right, because it is a real result and it is easy to
overstate.

The round builder was run once at build time against real xAI calls (`ARCADE_MODE=live
ARCADE_RECORD=1`) across six topics. All six builds succeeded. `cartridges/decoy/rounds/README.md`
records the source thread for each. Then the safety screen rejected one of them. The repository docs
no longer stamp that run with a date, so do not put one on it from memory. The recorded artifacts
under `artifacts/probes/` and `fixtures/api/` are the only dated evidence, and what they carry is the
timestamp of the X posts themselves, not of the run.

The rejected round is `cartridges/decoy/rounds/decoy_ai.json`, round id `decoy-4d18c911884a`, built from
the thread at `https://x.com/deedydas/status/2085642431723446579`. I ran the real `screen_round` over
all six committed files. The result:

```
decoy_ai.json      {'screened': False, 'gate_codes': ['G_SOURCE', 'G_URL']}
decoy_crypto.json  {'screened': True,  'gate_codes': []}
decoy_food.json    {'screened': True,  'gate_codes': []}
decoy_movies.json  {'screened': True,  'gate_codes': []}
decoy_music.json   {'screened': True,  'gate_codes': []}
decoy_sports.json  {'screened': True,  'gate_codes': []}
```

Two independent reasons, both checkable in the file:

- `G_SOURCE`: `source.post_text` is 2650 characters against a limit of 560. The pulled post is
  long-form, not a short one.
- `G_URL`: reply slot 2, authored `@ritsource`, reads `Reminds me of this video form @tomscott` followed
  by a `youtube.com/watch` link. `_URL_PATTERN` matches the `https://` prefix.

Neither failure is about anything toxic. This is the honest shape of the result and it should be
described that way. The gates caught a formatting failure and a link, on real pulled content, without
anyone reviewing it. That is what a deterministic screen is good at.

The rejection then propagates through the system without anyone hand-maintaining a blocklist. The queue
serves rounds in sorted filename order, so `decoy_ai.json` is offered first, `_next_round` screens it,
gets `screened: False`, and moves on. The committed trace at `artifacts/integration_trace.txt` opens
with:

```
answer key: 5 servable rounds, gated out: ['decoy-4d18c911884a']
== http checks ==
GET /health -> 200 {'mode': 'demo', 'rounds_available': 5}
```

and the first round actually played is `decoy-ebc5b68f8a7e`, which is `decoy_crypto.json`. The trace
ends with `integration: ALL CHECKS PASSED (6 rounds played, zero network egress)`. That file is a
committed recording of a past run, not a live result. Re-run `python3 integration_check.py` before
citing a number from it.

`DEMO.md` turns this into a scripted stage beat at 1:50, naming both codes out loud, and its own trace
table lists "5 of 6, ai round gated out on G_SOURCE + G_URL".

**Do not fix `decoy_ai.json`.** Shortening its post text or stripping the YouTube link would make it
pass, and that would break three things at once: `integration_check.py` asserts the servable count
matches, `DEMO.md` preflight tells the presenter to stop if `/health` is not 5, and the stage script
names the two gate codes. The failing round is load-bearing demo content.

### Sharp edges you will hit

**The build-time screen used to swallow its own import error, and the round files still show it.**
`round_builder.py` line 451 now reads `from plugins.safety.screen import screen_round`. Until commit
`7a4e012` it read `from plugins.safety import screen_round`. There is no `__init__.py` anywhere under
`plugins/` or `cartridges/`, so `plugins.safety` is a PEP 420 namespace package and `screen_round` is
not an attribute of it. It lives in the submodule `plugins.safety.screen`. The old form raises:

```
ImportError: cannot import name 'screen_round' from 'plugins.safety' (unknown location)
```

`_screen` catches that `ImportError` and returns `{"screened": False, "gate_codes": []}`, so every
round built before the fix carries a false stamp that no gate produced. `server/app.py` and
`integration_check.py` always imported correctly (`from plugins.safety import screen as safety_screen`
and `from plugins.safety.screen import screen_round`), which is why the serving path was never
affected.

The code is fixed and the artifacts were rebuilt to match. Verified by running
`build_round(topic, live=False)` from fixtures: `music` comes back `{'screened': True, 'gate_codes': []}`
and `ai` comes back `{'screened': False, 'gate_codes': ['G_SOURCE', 'G_URL']}`, which is the real gate
result, and both now equal what is committed on disk.

**The on-disk block is still advisory, not authoritative.** `server/app.py` re-screens every round at
load and overwrites the block before serving, so a stale or hand-edited value can never put a bad round
on screen. Never hand-edit it. Regenerate it from the screener so it stays a derived value.

There is a clean tell for this class of failure. `screened` is defined as `not failed` over the same
list, so `screened: false` with an empty `gate_codes` is impossible output from `screen_round`. If you
ever see that pair, the screen did not run. Treat it as a wiring signal, not a gate result.

**Both failure paths in `_screen` are still silent.** One catches `ImportError` and returns the
default. The other catches bare `Exception` with `pass`. Fail-closed is preserved in both, which is
correct. The problem is that a broken screen is indistinguishable from a dirty round, and that is
precisely how the import bug reached six committed files with nothing turning red. Fixing the import
did not fix that. Fail-closed should also be fail-noisy at build time.

**`G_URL` scans reply text only.** `_texts` does collect `source.post_text`, but only `G_SLURS` uses it.
`_gate_url` iterates `replies` and nothing else. I confirmed the behavior with a synthetic round whose
post text contains a link and whose replies are clean: it returns `{'screened': True, 'gate_codes':
[]}`. Since `web/game.js` renders the post text on screen, a URL in the post reaches the display even
though the gate's stated reason is that links break the game visually. Either extend the gate or record
the omission as intentional in `SAFETY.md`. Note that `decoy_ai.json` would still fail either way, on
`G_SOURCE`.

**One entry in `_DECOY_MARKERS` is unreachable.** The check is
`if not author.startswith("@") or author.lower() in _DECOY_MARKERS`. Python short-circuits, so a bare
`"decoy"` author is already rejected by the `startswith` test and never reaches the membership test.
Only `"@decoy"` can ever match. Behavior is correct. The code just reads as though it checks something
it never checks.

**The gates hardcode the Decoy round shape.** `_replies` enforces exactly `REPLIES_PER_ROUND` entries,
and `G_DECOY_COUNT` and `G_AUTHOR` require `is_decoy` and a top-level `decoy_slot`. Any cartridge whose
round is not shaped like a five-reply Decoy round fails `G_SOURCE`, `G_DECOY_COUNT`, and `G_AUTHOR` on
structure alone, so it can never be served. `paper/VISION.md` describes a second cartridge, Crux, as
running on the "same contract, same rooms, same gates". The current implementation does not support
that. Splitting cartridge-agnostic checks (length, denylist, URL) from Decoy-shape checks is the obvious
move, and it is blocked on the Crux round shape, which VISION.md describes only in prose and states
plainly is unbuilt.

**`screen.py` imports `REPLIES_PER_ROUND` from `config` at module load.** Changing `config.py` changes
what four of the five gates accept. `config` is treated as writable in at least one place:
`integration_check.py` sets `config.ROUND_SECONDS = 15` at import time.

**There is no test suite.** No `tests/` directory exists and no file matches `test_*` or `*_test.py`.
`integration_check.py` is the only automated proof, and it exercises the gates only through the server
path. Direct unit tests per gate, with a passing and a failing fixture each plus malformed-shape cases,
would pin the fail-closed contract that the rest of the system assumes.

### plugins/ads/, and why its numbers are fake on purpose

The ads plugin is 43 lines of Python and a 10-line doc. `plugins/ads/arenas.py` holds a static dict
keyed by topic:

```python
_SPONSORED_TOPICS: dict[str, dict[str, Any]] = {
    "ai": {
        "sponsor": "DemoBrand",
        "skin": {"accent": "#f97316"},
        "cpm_note": "ILLUSTRATIVE",
    },
    "space": {
        "sponsor": "OrbitCola",
        "skin": {"accent": "#38bdf8"},
        "cpm_note": "ILLUSTRATIVE",
    },
    "gaming": {
        "sponsor": "PixelPeak",
        "skin": {"accent": "#a78bfa"},
        "cpm_note": "ILLUSTRATIVE",
    },
}
```

One function reads it. `sponsored_arena(topic)` type-guards its input, then lowercases and strips the
topic so matching is case-insensitive. An unsponsored topic gets `None`. A sponsored one gets
`{**entry, "skin": dict(entry["skin"])}`, copied so a caller that decorates the result cannot mutate
the module-level config. The idea is that a sponsor changes nothing about play. It adds an accent color and
a name, and its mark rides the winner's share card.

**Every number and every name in that file is deliberately fictional, and the price field is a literal
placeholder string.** DemoBrand, OrbitCola, and PixelPeak are invented brands. `cpm_note` is the literal
string `"ILLUSTRATIVE"` in all three entries. It is not an empty slot waiting for a figure. It is the
value, chosen so that no invented market number can ship inside the module.

The reason is stated in `paper/VISION.md`: no measured share rate, fill rate, or CPM exists yet, and the
authors "would rather ship a placeholder that admits it than a projection dressed as a measurement."
`plugins/ads/ADS.md` says the same in its own words, that the demo ships zero real numbers.

So, concretely, for anyone touching this module. Do not replace `ILLUSTRATIVE` with a number. Do not add
a `cpm`, `fill_rate`, `impressions`, or `revenue` field. Do not compute anything. There is no pricing
arithmetic in the module today and that is the design. The only real values in the file are the three
hex accent colors.

One more thing to know before you cite the ads module in a conversation. `sponsored_arena` has zero
callers. Grepping the repo for `sponsored_arena`, `plugins.ads`, and `arenas` outside `plugins/ads/`
returns only prose, in `DEMO.md`, `paper/VISION.md`, and `slides/_deck_data.json`. Those documents
assert that the safety gates run before any arena is served, sponsored or not. That specific claim is
UNVERIFIED as implemented behavior, because no code path serves an arena. The narrower statement is true
and provable: the gates run before any round is served, at `_next_round`, on every round, with the
fallback screened the same way. Use the narrower one. Either wire `sponsored_arena` into the round or
share-card path, or say in `ADS.md` that the module is a shape sketch with no caller, so nobody reads
the sentence as a shipped guarantee.

---

## The server, the rooms, and the anti-cheat

`server/app.py` is 471 lines and it is the entire backend. One FastAPI app serves the static client,
runs every game room over a single WebSocket endpoint, answers a health check, and proxies voice-token
minting. There is no database, no Redis, no room service, and no second process. Rooms are a
module-level dict that dies with the process. The module docstring states the design constraint that
everything else follows from:

```python
Rooms live in memory. The websocket protocol is defined in CONTRACT.md and this
module conforms to it. The one rule that matters most: during the guessing
phase the broadcast state never contains is_decoy, decoy_slot, decoy_rationale,
or real reply authors. Reveal restores them. The client is never trusted.
```

Read that as the spec for the rest of this chapter.

### Boot and wiring

The first executable work the module does is fix the import path so the app can be started from
anywhere:

```python
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config
from cartridges.decoy import queue as decoy_queue
from plugins.safety import screen as safety_screen
```

Three dependencies, all local. `config` holds `MODE`, `ROUND_SECONDS = 30`, and `REPLIES_PER_ROUND = 5`.
`decoy_queue` is the round source. `safety_screen` is the deterministic gate set.

That cartridge import on line 32 is a hardcoded binding. Nothing scans `cartridges/` for other game
modules. A second cartridge cannot be served without editing this line. The server also reaches into
`decoy_queue.ROUNDS_DIR` directly inside `_rounds_available()` on line 180, so any swap has to preserve
that attribute name as well as `next_round` and `round_count`.

Two module-level constants shape behavior before any request arrives:

```python
FORCE_FALLBACK = os.environ.get("ARCADE_FORCE_FALLBACK", "") == "1"
DEMO_CARD_URL = "/static-assets/cards/decoy-3f2710c0a9e6_demo.jpg"
```

`FORCE_FALLBACK` is explicitly not part of the contract. Its comment says `selfcheck.py` sets it so the
scripted guesses stay valid even after the real queue lands, and `server/selfcheck.py` line 33 does
exactly that. `DEMO_CARD_URL` is the committed share card image, used in demo mode so the reveal never
waits on image generation.

`FALLBACK_ROUND` (lines 46 to 99) is a complete Round in the CONTRACT.md shape, with fictional handles,
hardcoded into the server. It exists so the server runs standalone with no cartridge at all.

### Route order matters

Four route registrations, and the last one is the reason the first three work. They are far apart in
the file, so this is a summary of the four rather than a contiguous quote:

```text
@app.get("/health")        decorates health()
@app.get("/token")         decorates token()
@app.websocket("/ws")      decorates ws_endpoint()
app.mount("/", StaticFiles(directory=str(REPO_ROOT / "web"), html=True), name="web")
```

The static mount is on line 471, after everything else, with the comment "Mounted last so /ws, /health,
and /token win the route match." A mount at `/` swallows every path it is asked about. Registering it
first would shadow the API. If you add an endpoint, add it above line 471.

`/health` returns `{"mode": config.MODE, "rounds_available": _rounds_available()}`. `_rounds_available()`
re-reads every JSON file in the rounds directory and counts the ones that pass screening, floored at 1
so the endpoint never reports an unplayable server. The committed run in
`artifacts/integration_trace.txt` shows `GET /health -> 200 {'mode': 'demo', 'rounds_available': 5}`
against six committed round files, because one is gated out.

`/token` short-circuits in demo mode and returns a labeled stub rather than touching the network. In
live mode it imports `services.voice_host.mint_token` and runs it with `asyncio.to_thread`, raising a
501 if the module is absent. The route body and the reasoning behind the ephemeral token are in the
voice chapter.

### The room model

A room is a plain dict created lazily by `_get_room` (lines 128 to 144):

```python
room = {
    "room_id": room_id,
    "phase": "lobby",
    "players": {},
    "round": None,
    "reveal": None,
    "deadline_at": None,
    "timer": None,
    "guess_counter": 0,
    "arena": room_id in ARENA_ROOMS,
    "host": None,
}
```

`players` maps name to `PlayerState`, which holds `ws`, `score`, `streak`, `guessed`, `guess_slot`,
`guess_order`, and `client_ms`. `guess_order` and `client_ms` are the two fields to keep straight. One
decides the winner and the other never does.

`ROOMS` is a bare module dict with the comment "Single event loop, no locks needed at demo scale." That
is accurate for a uvicorn worker running one loop. It is also the reason the process is the unit of
state. Two workers would give you two disjoint sets of rooms.

Note where `_get_room` is called. Only the `join` branch calls it. `guess` and `next` use
`ROOMS.get(room_id)` and bail on `None`, so neither message can conjure a room into existence.

### The phase machine

Three phases, and the transitions are few enough to enumerate.

```text
lobby ──(2nd join in a non-arena room)──> guessing
lobby ──(next: arena host any time, duel at >= 2)──> guessing
guessing ──(all players guessed)────────> reveal
guessing ──(server timer expires)───────> reveal
guessing ──(last un-guessed player disconnects)──> reveal
reveal ──(next)─────────────────────────> guessing
```

Every transition into guessing runs through `_start_round`, and every transition into reveal runs
through `_do_reveal`. There are no other writers of `room["phase"]`.

`_start_round` cancels any live timer, pulls a fresh round, clears the per-player guess fields, sets a
wall deadline on the loop clock, and starts the timer task:

```python
async def _start_round(room: dict[str, Any]) -> None:
    _cancel_timer(room)
    room["round"] = await _next_round()
    room["reveal"] = None
    room["phase"] = "guessing"
    room["guess_counter"] = 0
    for p in room["players"].values():
        p.guessed = False
        p.guess_slot = None
        p.guess_order = None
        p.client_ms = None
    room["deadline_at"] = asyncio.get_running_loop().time() + config.ROUND_SECONDS
    room["timer"] = asyncio.create_task(_round_timer(room))
    await _broadcast(room)
```

Scores and streaks are deliberately not reset here. They persist across rounds for the whole life of the
room.

### Where rounds come from

`_next_round` is the bridge between the queue and the room, and it carries the safety policy. Its full
body and the four properties that make it safe are in the safety chapter. Three facts belong here,
because they are the ones that bite during a demo.

`FORCE_FALLBACK` skips the queue entirely. When `ARCADE_FORCE_FALLBACK=1` is set, no round file is ever
read and every round is the hardcoded `FALLBACK_ROUND`. `server/selfcheck.py` depends on that.

The whole queue walk is wrapped in a bare `except Exception: pass`. A malformed new round file, or a
missing directory, silently drops the server into the fallback round forever with no log line. Confirm a
new round with `/health` and `integration_check.py`, never by watching the UI.

The `safety` block committed inside a round JSON is never read. It is overwritten with a fresh screen
result on every serve.

### The protocol, with real shapes

`CONTRACT.md` defines three client messages and one server message.

Client to server:

```json
{"t": "join",  "room": "abc", "name": "PLAYER1"}
{"t": "guess", "room": "abc", "slot": 2, "ms": 8450}
{"t": "next",  "room": "abc"}
```

Server to client, on every change:

```json
{"t": "state", "room": "abc", "phase": "lobby|guessing|reveal",
 "players": [{"name": "PLAYER1", "score": 2, "guessed": true}],
 "round": "<Round with is_decoy and decoy_slot STRIPPED during guessing>",
 "reveal": {"decoy_slot": 2, "rationale": "...", "winner": "PLAYER1"},
 "deadline_ms": 30000}
```

The server emits a superset of that example. `_public_state` adds `streak` to each player row, and
`_do_reveal` adds `leaderboard` and `share_card_url` to the reveal block. `web/game.js` line 294 handles
the older shape by falling back to `s.players` when `reveal.leaderboard` is missing.

The `state` message is a full snapshot every time. There are no deltas and no per-player messages.
`_broadcast` builds the state once and sends the same JSON to every socket in the room, pruning sockets
that raise:

```python
async def _broadcast(room: dict[str, Any]) -> None:
    state = _public_state(room)
    dead: list[str] = []
    for player in list(room["players"].values()):
        try:
            await player.ws.send_json(state)
        except Exception:
            dead.append(player.name)
    for name in dead:
        room["players"].pop(name, None)
```

One state object for the whole room is what forces the anti-cheat design to be a strip rather than a
per-player mask. Everyone in the room is looking at the same bytes.

The parser at the top of `ws_endpoint` is defensive by construction. A non-JSON frame hits
`except Exception: continue`. A non-dict payload is dropped. A message without both `t` and a non-empty
`room` is dropped. Only `WebSocketDisconnect` escapes, and it escapes deliberately with a bare `raise`
so the outer handler can run cleanup.

### join

```python
if t == "join":
    name = str(msg.get("name", "")).strip() or "anon"
    room = _get_room(room_id)
    existing = room["players"].get(name)
    if existing is not None:
        existing.ws = ws
    else:
        room["players"][name] = PlayerState(name=name, ws=ws)
    joined = (room_id, name)
    if room["host"] is None:
        room["host"] = name
    if (not room["arena"] and room["phase"] == "lobby" and len(room["players"]) >= 2):
        await _start_round(room)
    else:
        await _broadcast(room)
```

Identity is the name string. Rejoining under the same name rebinds the socket and keeps score and
streak, which is how a phone that drops off wifi recovers. It also means the name is the only
credential. Two people who type the same name into the same room are one player.

`joined` is a local in the socket handler, and it is the closest thing to a session this server has.
Everything downstream reads identity from `joined[1]`, not from the message body.

### guess

```python
elif t == "guess":
    room = ROOMS.get(room_id)
    if room is None or room["phase"] != "guessing" or joined is None:
        continue
    player = room["players"].get(joined[1])
    slot = msg.get("slot")
    if (player is None or player.guessed
        or not isinstance(slot, int) or isinstance(slot, bool)
        or not 0 <= slot < config.REPLIES_PER_ROUND):
        continue
    player.guessed = True
    player.guess_slot = slot
    room["guess_counter"] += 1
    player.guess_order = room["guess_counter"]
    player.client_ms = msg.get("ms")
    if all(p.guessed for p in room["players"].values()):
        await _do_reveal(room)
    else:
        await _broadcast(room)
```

The validation is worth reading closely. `isinstance(slot, bool)` is rejected explicitly because `True`
is an `int` in Python and `0 <= True < 5` is true. Without that clause, `{"slot": true}` would be a legal
guess for slot 1. The bound comes from `config.REPLIES_PER_ROUND`, not a literal.

`guess_order` is assigned from a monotonic per-room counter at the moment the server processes the
frame. That is the ordering that decides the winner. `client_ms` is stored and never compared against
anything.

The player is looked up by `joined[1]` while the room comes from the message body. A socket that joined
room A can therefore address a `guess` at room B and act on a same-named player who exists there. At
demo scale with typed room codes that is theoretical. If you ever make rooms meaningful, compare
`joined[0]` to `room_id` first.

### The server-side timer

```python
async def _round_timer(room: dict[str, Any]) -> None:
    try:
        await asyncio.sleep(config.ROUND_SECONDS)
    except asyncio.CancelledError:
        return
    if room["phase"] == "guessing":
        await _do_reveal(room)
```

One `asyncio.Task` per room, stored in `room["timer"]`, cancelled by `_cancel_timer` at the top of both
`_start_round` and `_do_reveal`. The re-check of `room["phase"]` before revealing guards the race where
the last guess arrives while the sleep is finishing.

The number the client shows is derived, not authoritative:

```python
def _deadline_ms(room: dict[str, Any]) -> int | None:
    if room["phase"] != "guessing" or room["deadline_at"] is None:
        return None
    remaining = room["deadline_at"] - asyncio.get_running_loop().time()
    return max(0, int(remaining * 1000))
```

`deadline_at` is on the event loop's monotonic clock, so it is immune to wall-clock adjustments, and
`deadline_ms` is recomputed on every broadcast. A client that reconnects mid-round gets the correct
remaining time in its first state message. The client comment in `web/game.js` line 317 says the same
thing from the other side: "timer (display only, server enforces the real deadline)."

`server/selfcheck.py` sets `config.ROUND_SECONDS = 2` before importing the app and asserts the deadline
path with nobody guessing. The check reads "server timer forces reveal at the deadline."

### Scoring and first correct guess wins

```python
correct = [p for p in room["players"].values() if p.guess_slot == decoy_slot]
correct.sort(key=lambda p: p.guess_order if p.guess_order is not None else 1 << 30)
winner = correct[0].name if correct else "house"
for p in room["players"].values():
    if p.name == winner:
        p.score += 1
        p.streak += 1
    else:
        p.streak = 0
```

Exactly one point per round goes to exactly one name. Nobody correct means `winner == "house"`, and
since no player is named "house" the loop resets every streak. That is the "both wrong = house wins" line
from the contract, implemented without a special case.

The sort key `1 << 30` is a sentinel for a player with no `guess_order`, which cannot co-occur with a
matching `guess_slot` in the current code but keeps the sort total.

The comment above the block states the anti-cheat position for scoring:

```python
# Winner is the first correct guess in server arrival order. The client's
# self-reported ms is display data only and never decides the winner.
```

A client that lies about `ms` gains nothing. There is no path from `client_ms` to `score`.

The reveal block is assembled next:

```python
room["reveal"] = {
    "decoy_slot": decoy_slot,
    "rationale": rnd.get("decoy_rationale", ""),
    "winner": winner,
    "leaderboard": [{"name": p.name, "score": p.score} for p in standings[:5]],
    "share_card_url": None if config.MODE == "live" else DEMO_CARD_URL,
}
```

`standings` is `sorted(room["players"].values(), key=lambda p: (-p.score, p.name.lower()))`, so ties
break alphabetically and the ordering is stable across broadcasts. The share card branch and the live
background render are covered in the media chapter.

### The anti-cheat: one function

Everything the contract promises about hidden answers is enforced in seven lines of code:

```python
def _round_view(room: dict[str, Any]) -> dict[str, Any] | None:
    rnd = room["round"]
    if rnd is None or room["phase"] != "guessing":
        return rnd
    safe = {k: v for k, v in rnd.items() if k not in ("decoy_slot", "decoy_rationale")}
    safe["replies"] = [{"slot": r["slot"], "text": r["text"]} for r in rnd["replies"]]
    return safe
```

During guessing, four things leave the wire.

`decoy_slot` is removed at the top level. `decoy_rationale` is removed at the top level. Per reply,
`is_decoy` is removed and the real `author` handle is removed, because each reply is rebuilt from
scratch as `{slot, text}` rather than filtered.

Reveal needs no restore step. `room["round"]` is never mutated. `_round_view` builds a fresh dict on
every broadcast and returns the stored round untouched once the phase is no longer `guessing`. The
stripping is a projection, not a destructive edit, so "reveal restores them" is really "reveal stops
projecting."

Two asymmetries in that code are load-bearing for anyone extending it.

The reply rebuild is an allowlist. Only `slot` and `text` survive. Any per-reply field a future cartridge
adds is dropped during guessing for free.

The top level is a denylist. Only `decoy_slot` and `decoy_rationale` are removed. `round_id`, `source`,
`safety`, and `seed` pass through, and so would any new top-level key. If a cartridge invents a field
that encodes the answer, it leaks straight to the client during guessing. That is the single sharpest
edge in this file.

Also note what `_public_state` does not send. The player rows carry `name`, `score`, `streak`, and
`guessed`. They never carry `guess_slot`. Opponents can see that you locked in, not what you picked.

### Why server-side stripping is the only version that works

The client is a browser. Whatever the server sends it, the player can read. `web/game.js` is unminified
vanilla JavaScript with no build step, so in this codebase the "hidden" answer would not even need
devtools. Open the file. But that is not the real argument, because minification would not help either.
Any client-side hiding scheme ships the answer to the machine you are asking not to look at it, and then
politely asks it not to look. A network tab, a `JSON.parse` in the console, or a WebSocket frame
inspector defeats all of it in seconds.

The server-side strip changes the class of the problem. The answer never crosses the wire while it can
still be used. There is nothing to find because nothing was sent. `paper/VISION.md` states the same
claim as an architectural property: the server strips the decoy flag, the answer slot, and the real
author handles from everything it broadcasts, "so the answer never crosses the wire to a client that
could cheat," and that property is what makes a game host that needs no knowledge of the game's content.

The rendering side confirms the client has nothing to work with. `web/game.js` line 215 computes
`const isDecoy = reveal && reveal.decoy_slot === reply.slot`. The decoy badge, the rationale text, and
the author reveal are all conditioned on `s.reveal`, which is `null` for the entire guessing phase. Line
230 falls back to `"@·····"` for the author because during guessing there is no `reply.author` key at
all.

The property is asserted in two scripted checks rather than trusted. `server/selfcheck.py` has a
reusable helper:

```python
def check_stripped(state: dict[str, Any]) -> None:
    rnd = state["round"]
    check(rnd is not None, "guessing state carries a round")
    check("decoy_slot" not in rnd, "guessing round has no decoy_slot")
    check("decoy_rationale" not in rnd, "guessing round has no decoy_rationale")
    clean = all("is_decoy" not in r and "author" not in r for r in rnd["replies"])
    check(clean, "guessing replies carry no is_decoy and no author")
    check(len(rnd["replies"]) == 5, "guessing round still has 5 replies")
```

`integration_check.py` runs the same assertions against every round in the real queue and adds the
mirror check at reveal, that `is_decoy` and `author` come back. The committed trace in
`artifacts/integration_trace.txt` shows both passing on all six rounds played.

One caveat about the strip's scope. The answer is also present in `?mock=1`, the browser-only fallback
in `web/game.js`, which reimplements the contract client-side including its own stripping. Mock mode is a stage safety net that runs from disk with no
server. It is not a security boundary and cannot be one, and nothing in the gate scripts tests it. The
client chapter covers what it does and where it can drift.

### Arena mode

Arena mode is roughly fifteen lines spread across three places, and it turns a two-player duel into a
crowd room.

```python
ARENA_ROOMS = {"GROK"}
```

A single hardcoded set. `_get_room` stamps `"arena": room_id in ARENA_ROOMS` at creation time, so
arena-ness is a property of the room code and is fixed for the room's life. `DEMO.md` explains the
staging: the QR on the projector resolves to `/?room=GROK`, `web/game.js` line 343 reads that query
parameter and prefills the room input, and the audience joins in one tap.

Arena mode changes exactly two behaviors.

Auto-start is off. The join branch guards on `not room["arena"]`, so a second scanned-in phone does not
start a round. The comment states why: "Arena rooms never auto-start; the host starts on stage once the
crowd has scanned in." A crowd trickling in over ten seconds would otherwise fire the first round on
person number two.

Advancing is host-only:

```python
elif t == "next":
    room = ROOMS.get(room_id)
    if room is None:
        continue
    if room["arena"] and (joined is None or joined[1] != room["host"]):
        continue
    in_reveal = room["phase"] == "reveal"
    lobby_ready = room["phase"] == "lobby" and (
        room["arena"] or len(room["players"]) >= 2
    )
    if in_reveal or lobby_ready:
        await _start_round(room)
```

The arena branch in `lobby_ready` is load-bearing and was missing until commit
`c005b28`. `renderLobby` enables START at one player, but this handler required two,
so a host alone in room `GROK` tapped a live-looking button and the server dropped the
message with no reply. An arena never auto-starts, so START was the only way out of
that lobby and the one path that could not work. A duel still needs both players before
its first round, which is correct, because a duel has nobody to host it.

Host election is first-come. `join` sets `room["host"] = name` when it is still `None`, so the first
socket to join the room owns it. The runbook depends on this: the presenter opens the room and joins
before the talk, and every phone that scans in afterward is an ordinary player whose NEXT taps are
dropped.

Three consequences follow directly from that code.

The host is never re-elected. If the host's socket closes while other players remain, the disconnect
handler pops the player from `room["players"]` but leaves `room["host"]` pointing at a name that is no
longer present. Nobody can advance the arena until someone rejoins under exactly that name. The recovery
path exists and it is the same as any other reconnect, which is to type the same name again.

The room is destroyed only when the last player leaves. The `finally` block pops the room and cancels
its timer when `room["players"]` is empty, so `host` and `arena` both reset naturally on a fresh start.

In a non-arena room, `next` has no identity check at all. Any socket that sends
`{"t": "next", "room": "abc"}` to a room sitting in reveal advances it, even a socket that never joined.
That is fine for two tabs on a laptop and it is worth knowing before you expose room codes more widely.

The leaderboard is the third piece of arena support, and it lives in the reveal payload rather than in a
separate message. `standings[:5]` is computed on every reveal for every room. The comment says it
plainly: "Top of the crowd at every reveal. In a duel this is just both players, in an arena it is the
scoreboard beat on the big screen." The client renders numbered positions only when the board has more
than two entries (`web/game.js` line 300), so the same code path produces a clean duel score strip and a
stage leaderboard.

One gap to know about. Neither check script exercises arena mode. `integration_check.py` uses
`ROOM = "ITG"` and `server/selfcheck.py` uses room `"abc"`, both ordinary rooms. The host-election and
host-only-advance paths that the live stage demo runs on are not covered by the automated gate.

### Disconnect handling

The cleanup block is the last thing in `ws_endpoint` and it handles more than it looks like:

```python
finally:
    if joined is not None:
        room = ROOMS.get(joined[0])
        if room is not None:
            player = room["players"].get(joined[1])
            if player is not None and player.ws is ws:
                room["players"].pop(joined[1], None)
                if room["players"]:
                    if room["phase"] == "guessing" and all(
                        p.guessed for p in room["players"].values()
                    ):
                        await _do_reveal(room)
                    else:
                        await _broadcast(room)
                else:
                    _cancel_timer(room)
                    ROOMS.pop(joined[0], None)
```

The `player.ws is ws` identity check is the important line. When a player reconnects under the same
name, `join` rebinds `existing.ws` to the new socket. The old socket's `finally` then runs and finds
that the stored socket is not itself, so it leaves the player alone. Without that check, a reconnect
would delete the player it just restored.

The `all(p.guessed for ...)` re-check catches the case where the only player still thinking drops out
mid-round. The room reveals immediately instead of waiting out the timer with nobody left to guess.

The empty-room branch cancels the timer before dropping the room. Skipping that would leave a task
holding a reference to a dead room until its sleep expires.

### If you are changing this file

Read `_round_view` before you touch the round shape. It is the only thing standing between the answer
and the client, and its top-level filter is a denylist.

Read `_next_round` before you add rounds. The gate result inside a round file is ignored, failures are
silent, and a fully gated-out queue plays the hardcoded fallback forever.

Read the `guess` branch before you touch scoring. `guess_order` is the winner. `client_ms` is
decoration.

Keep the static mount last.

---

## The client and the deployment

The entire front end is three files and 1031 lines. There is no build step, no framework, no
package.json, and no external request of any kind. `web/index.html` is 90 lines, `web/game.js` is 505,
and `web/style.css` is 436. The client renders state and sends three message types. Every rule of the
game lives on the server.

```
web/
  index.html          90 lines   the full DOM vocabulary, written once
  game.js            505 lines   transport, state handling, render, timer, mock server
  style.css          436 lines   dark neon arcade, CSS-only texture
  static-assets/
    qr.png                       41x41 module QR, version 6, error level H
    host_intro.mp3               loaded by game.js
    host_reveal.mp3              loaded by game.js
    host_round.mp3               present, not referenced by game.js
    host_win.mp3                 present, not referenced by game.js
    host_lose.mp3                present, not referenced by game.js
    cards/decoy-3f2710c0a9e6_demo.jpg   the committed demo share card
```

The five mp3 files are all committed, but `game.js:34-37` only builds `Audio` objects for
`host_intro.mp3` and `host_reveal.mp3`. The other three are unreferenced by the client.

### index.html is a fixed DOM, not a template

Nothing in the markup is generated by a template engine, and nothing is cloned from a `<template>` tag.
Every element that will ever appear on screen either exists in `index.html` from the first paint or is
built by hand with `document.createElement` in `game.js`. The split runs down the middle of the file.
The chrome (topbar, post card, timer ring, reveal panel shell) is static markup that gets its text
swapped. The two variable-length lists (opponent chips, reply cards) are torn down and rebuilt from
scratch on every state message.

The head is three lines of substance.

```html
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>X ARCADE // DECOY</title>
<link rel="stylesheet" href="style.css">
```

There is no font link, no CDN script, no analytics tag. That is what makes the "runs with the network
cable pulled" claim in `CONTRACT.md` true at the browser layer and not only at the server layer. The
typefaces are OS stacks declared in `style.css:15-16`.

```css
--font-display: "Impact", "Arial Black", "Franklin Gothic Medium", sans-serif;
--font-ui: ui-monospace, "SF Mono", Menlo, Consolas, "Courier New", monospace;
```

The body opens with `<div id="crt" aria-hidden="true">`, which is the scanline and vignette overlay,
then a sticky `<header id="topbar">`, then a `<main>` holding exactly two `<section class="screen">`
elements.

### The screen machine

There are three phases in the protocol and two screens in the DOM. `screen-lobby` covers
`phase: "lobby"`. `screen-game` covers both `guessing` and `reveal`, because the reveal is not a
different page. It is the same arena with the cards decorated and a panel unhidden underneath them.
`index.html:53` says so in a comment: "Screens 2 and 3: round and reveal share the arena, reveal
decorates it."

The switch is two lines.

```js
const inLobby = s.phase === "lobby";
$("screen-lobby").hidden = !inLobby;
$("screen-game").hidden = inLobby;
if (inLobby) renderLobby(s); else renderGame(s);
```

This works only because of one CSS rule at `style.css:114-115`.

```css
/* the hidden attribute always wins, even over display rules above */
[hidden] { display: none !important; }
```

`.screen` sets `display: flex`, and a class-based `display` declaration beats the user-agent default for
`[hidden]`. Without that `!important` override both screens would render at once. If you add a new
screen, give it `.screen` and let the same rule handle it.

The reveal panel is a third layer of the same mechanism. `renderReveal` returns early and sets
`panel.hidden = true` unless the phase is `reveal` and `s.reveal` is present.

### The transport is four event listeners and an infinite retry

```js
function connect() {
  if (MOCK) { sock = mockSocket(handleRaw); setConn("MOCK LINK ACTIVE"); return; }
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/ws`);
  sock = ws;
  ws.addEventListener("open", () => {
    setConn("LINKED");
    if (joined) send({ t: "join", room: myRoom, name: myName });
  });
  ws.addEventListener("message", (ev) => handleRaw(ev.data));
  ws.addEventListener("close", () => { setConn("LINK LOST, RETRYING..."); setTimeout(connect, 1500); });
  ws.addEventListener("error", () => { try { ws.close(); } catch (e) {} });
}
```

Three things matter here.

The scheme is derived from `location.protocol`, so the same file works over `ws://` on a laptop and
`wss://` on a Hugging Face Space with no configuration. The host comes from `location.host`, so there is
no hardcoded server address anywhere in the client.

The socket opens at boot, before the player has joined anything. `connect()` is the last line of the
file. Join is a message sent over an already-open socket, not a connection event.

Reconnect is automatic and re-sends the join. The server's join handler at `server/app.py:390-393` looks
up an existing `PlayerState` by name and rebinds `existing.ws = ws` rather than creating a new player,
so a phone that drops off Wi-Fi and comes back keeps its score and streak. The name is the identity.
There is no session token.

`send` swallows every exception.

```js
function send(obj) {
  try { sock.send(JSON.stringify(obj)); } catch (e) { /* retry loop reconnects */ }
}
```

That is deliberate for a stage demo. It also means a guess sent over a dead socket disappears with no
user-visible error, and the card still flips (see the reply card section).

`handleRaw` parses and filters. Only `{"t": "state"}` is handled. Any other message type, and any text
that is not valid JSON, is dropped silently.

### The state message, and the one function that reacts to it

The shape the client consumes is produced by `_public_state` in `server/app.py:214-231`.

```json
{
  "t": "state",
  "room": "GROK",
  "phase": "lobby|guessing|reveal",
  "players": [{"name": "ARUN", "score": 2, "streak": 1, "guessed": true}],
  "round": { "...Round, stripped during guessing..." },
  "reveal": null,
  "deadline_ms": 27431
}
```

`CONTRACT.md` documents `players` without `streak`. The server sends it. The client never reads it. That
is a documentation drift, not a bug.

During `guessing`, `_round_view` rebuilds the round so a reply is `{slot, text}` and nothing else. There
is no `author` field and no `is_decoy` field to inspect in devtools. At reveal the raw round is passed
through untouched, so replies regain `author` and `is_decoy`, and `round.decoy_slot` and
`round.decoy_rationale` come back. The server chapter covers why that projection is the whole anti-cheat.

`handleState` is the only place derived client state moves.

```js
function handleState(s) {
  const was = state ? state.phase : null;
  state = s;

  if (s.phase === "guessing" && s.round && s.round.round_id !== lastRoundId) {
    lastRoundId = s.round.round_id;
    roundNo += 1;
    myGuessSlot = null;
    guessStartAt = performance.now();
  }
  if (s.phase === "guessing") {
    timerEndAt = performance.now() + (s.deadline_ms || 0);
    startTimer();
  } else {
    stopTimer();
  }
  if (s.phase === "guessing" && was !== "guessing") playSound("intro");
  if (s.phase === "reveal" && was !== "reveal") playSound("reveal");
  prevPhase = was;
  render(s);
}
```

The new-round test keys on `round_id`, not on a phase edge. That is the right choice, because a
`guessing` state arrives several times per round (once per opponent lock-in) and only the first one for
a given `round_id` should reset `myGuessSlot` and restart the stopwatch.

`roundNo` is client-side and starts at zero. The server has no concept of a round number and never sends
one. Two phones that joined at different times will therefore show different `RND NN` values in the
topbar. That is a real divergence, not a rendering delay.

`timerEndAt` is recomputed on every `guessing` message, which means the client re-syncs to the server's
remaining time on every opponent lock-in. Clock drift on the phone cannot accumulate across a round.

The sound edges use `was !== phase`, so repeated `guessing` broadcasts inside one round do not retrigger
the intro line.

`prevPhase` is assigned and never read anywhere else in the file. It is dead state.

### The topbar

`render` handles the chrome before delegating.

```js
$("roundCounter").textContent = "RND " + String(roundNo).padStart(2, "0");
const screened = !!(s.round && s.round.safety && s.round.safety.screened);
$("safetyChip").hidden = !screened;
```

The safety chip reads `round.safety.screened`, which the server writes fresh at load time by re-running
`screen_round` on every round it serves. The chip is not a static label. It reflects the gate result on
the round currently on screen.

The DEMO badge is driven by a real HTTP call at boot, not by a build flag.

```js
fetch("/health").then((r) => r.json()).then((j) => {
  if (j && (j.mode === "demo" || j.demo === true)) $("demoBadge").hidden = false;
}).catch(() => {});
```

In mock mode the badge text is replaced with `MOCK` and forced visible, so a mock run can never be
mistaken for a demo run on a projector.

The mute button persists to `localStorage` under the key `arcade_muted`, and both the read and the write
are wrapped in `try/catch` so a browser with storage disabled still boots.

### The timer ring

The ring is two SVG circles at `r=52` inside a `viewBox="0 0 120 120"`.

```html
<svg class="timer-ring" viewBox="0 0 120 120" aria-hidden="true">
  <circle class="ring-track" cx="60" cy="60" r="52"></circle>
  <circle class="ring-fill" id="ringFill" cx="60" cy="60" r="52"></circle>
</svg>
```

The CSS rotates the whole SVG so the sweep starts at twelve o'clock, and sets the dash array to the full
circumference.

```css
.timer-ring { width: 120px; height: 120px; transform: rotate(-90deg); }
.ring-fill {
  stroke-dasharray: 326.7;
  stroke-dashoffset: 0;
  ...
}
```

`2 * pi * 52` is 326.7256. The constant appears twice, once as `RING_LEN = 326.7` in `game.js:9` with the
comment "circumference of the r=52 timer circle", and once as the `stroke-dasharray` literal in the CSS.
If you change the radius you must change both.

The animation loop is a plain `requestAnimationFrame` tick.

```js
function startTimer() {
  cancelAnimationFrame(timerRaf);
  const tick = () => {
    const left = Math.max(0, timerEndAt - performance.now());
    $("timerNum").textContent = String(Math.ceil(left / 1000));
    $("ringFill").style.strokeDashoffset = String(RING_LEN * (1 - left / 30000));
    $("timerWrap").classList.toggle("low", left < 5000 && left > 0);
    if (left > 0) timerRaf = requestAnimationFrame(tick);
  };
  timerRaf = requestAnimationFrame(tick);
}
```

The `30000` is a hardcoded full-scale value. The server's deadline comes from `config.ROUND_SECONDS = 30`.
Change `ROUND_SECONDS` to 45 and the countdown number stays correct, because it is computed from
`deadline_ms`, but the ring sweep breaks silently. At 45 seconds remaining, `1 - 45000/30000` is
negative, so `strokeDashoffset` goes negative and the ring renders full until the clock passes 30
seconds. No error is thrown. This is the single sharpest coupling between the client and `config.py`.

Under five seconds the `low` class flips the ring and the number from cyan to red and starts an infinite
pulse animation.

```css
.timer-wrap.low .ring-fill { stroke: var(--red); filter: drop-shadow(0 0 6px var(--red-glow)); }
.timer-wrap.low .timer-num { color: var(--red); ... animation: pulse 0.5s infinite alternate; }
```

The whole ring is display only. The comment above `startTimer` says so, and it is accurate. The
authoritative deadline is an `asyncio` task in `_round_timer` on the server. A player who freezes their
JavaScript still gets revealed on time.

The ring is hidden rather than removed outside the guessing phase, using `visibility` so the layout does
not reflow.

```js
$("timerWrap").style.visibility = s.phase === "guessing" ? "visible" : "hidden";
```

### The reply cards

`renderReplies` clears the grid and rebuilds all five cards on every state message.

```js
const grid = $("replies");
grid.innerHTML = "";
```

That is the simplest correct thing and it costs almost nothing at five cards. It does mean any per-card
DOM state is discarded whenever an opponent locks in, which happens several times per round. Whether the
CSS flip transition visibly restarts as a result is UNVERIFIED. I did not run this in a browser, and
browsers generally do not transition an element on its first paint, so a freshly created card with
`.locked` already applied would most likely render flipped with no animation.

Each card is a three-dimensional flip built from three nested divs.

```
.card                 perspective: 900px, min-height 150px
  .card-inner         transform-style: preserve-3d, transition: transform 0.45s
    .card-front       position: relative (in flow, so the card grows with its text)
    .card-back        position: absolute, inset 0, transform: rotateY(180deg)
```

The flip is one class on the outer element.

```css
.card.locked .card-inner { transform: rotateY(180deg); }
```

The front-in-flow and back-absolute split is called out in a CSS comment at `style.css:325-326`. It
matters because reply texts vary in length. If both faces were absolute the card would need a fixed
height and long replies would clip.

The card content is assembled in a fixed order: a `REPLY N` slot tag, an optional `ROBOT` badge, the
reply text, an author line, an optional rationale paragraph, and an optional `YOUR PICK` tag.

The author line is where the reveal logic is most visible.

```js
author.textContent = s.phase === "reveal" && reply.author && !isDecoy ? reply.author : "@·····";
if (isDecoy) author.textContent = "grok wrote this one";
```

During guessing there is no `reply.author` field at all, because the server stripped it, so every card
shows the masked `@·····`. The client is not choosing to hide something it received. It never received
it.

`isDecoy` is computed from `reveal.decoy_slot`, not from `reply.is_decoy`, even though `is_decoy` is
present on the wire at reveal time.

```js
const isDecoy = reveal && reveal.decoy_slot === reply.slot;
```

This mirrors the rule in `CONTRACT.md` that `decoy_slot` is duplicated at the top level "so the server
never parses reply objects to score." The client applies the same discipline. One field is the source of
truth for which card is the machine.

Tappability is gated three ways.

```js
const canTap = s.phase === "guessing" && myGuessSlot === null && !myGuessConfirmed(s);
```

`myGuessConfirmed` reads the server's own view of the local player back out of the broadcast.

```js
function myGuessConfirmed(s) {
  const me = (s.players || []).find((p) => p.name === myName);
  return !!(me && me.guessed);
}
```

So a reconnecting phone that lost its local `myGuessSlot` still cannot guess twice, because the server
says it already did. The click listener is only attached when `canTap` is true, so an untappable card has
no handler at all rather than a handler that returns early.

The guess itself is optimistic.

```js
function onGuess(slot, card) {
  if (!state || state.phase !== "guessing" || myGuessSlot !== null) return;
  myGuessSlot = slot;
  const ms = Math.round(performance.now() - guessStartAt);
  card.classList.add("locked");
  send({ t: "guess", room: myRoom, slot, ms });
}
```

The card flips before any acknowledgement. Combined with the swallowing `send`, a guess made while the
socket is down locks the card visually and is lost with no feedback. On the next state broadcast the
rebuild will re-apply `.locked` from `myGuessSlot`, so the card stays flipped and the player has no way
to notice.

The `ms` value is client-reported and, per the comment in `_do_reveal`, "is display data only and never
decides the winner." The server ranks correct guesses by `guess_order`, a counter it increments itself.

At reveal the outer card gets `is-decoy` or `is-real`, plus `my-pick` if it was the local player's
choice.

```css
.card.is-decoy .card-front {
  border-color: var(--red);
  box-shadow: 0 0 22px var(--red-glow), inset 0 0 18px rgba(255, 45, 85, 0.12);
  cursor: default;
}
.card.is-real .reply-author { color: var(--cyan); }
.card.my-pick .card-front { outline: 2px dashed var(--cyan); outline-offset: 3px; }
```

Red for the machine, cyan for the humans, a dashed cyan outline for your own pick. Note that at reveal
the code never adds `.locked`, so every card flips back to its front face. The reveal is readable even
for the card you locked.

### The reveal panel

`renderReveal` fills a static shell that lives in `index.html:79-84`.

```html
<div class="reveal-panel" id="revealPanel" hidden>
  <div class="winner-banner" id="winnerBanner">HOUSE WINS</div>
  <div class="score-strip" id="scoreStrip"></div>
  <img class="share-card" id="shareCard" alt="share card" hidden>
  <button class="big-btn btn-start" id="nextBtn" type="button">NEXT ROUND</button>
</div>
```

The banner has three states.

```js
const w = s.reveal.winner;
if (!w || w === "house") {
  banner.textContent = "THE HOUSE WINS";
  banner.classList.add("house");
} else {
  banner.textContent = w === myName ? "YOU CALLED IT" : w.toUpperCase() + " WINS";
  banner.classList.remove("house");
}
```

The server always sends a string, `"house"` when nobody guessed correctly. The `!w` branch is defensive.
The `house` class recolors the banner from cyan to red, and a `bannerIn` keyframe scales it from 0.8 with
a fade over 0.4 seconds.

The leaderboard comes from the server, computed at every reveal in `_do_reveal` as `standings[:5]`, score
descending with name ascending as the tiebreak. The truncation is the arena constraint. In a crowded room
the sixth player onward will never see their own name on the strip.

The client prefers that list and falls back to the raw player array.

```js
const board = (s.reveal.leaderboard && s.reveal.leaderboard.length)
  ? s.reveal.leaderboard
  : (s.players || []);
board.forEach((p, i) => {
  const el = document.createElement("span");
  el.className = "score" + (i === 0 ? " leader" : "");
  el.textContent = (board.length > 2 ? (i + 1) + ". " : "") + p.name;
  const b = document.createElement("b");
  b.textContent = String(p.score || 0);
  el.appendChild(b);
  strip.appendChild(el);
});
```

Rank numbers only appear when there are more than two entries. In a two-player duel the strip reads
`ARUN 2  GLITCH 1` with no `1.` and `2.` prefixes, because ranking two people is noise. In an arena it
reads `1. ARUN 3`, `2. SAM 2`, and so on. This is the one place in the client that changes shape between
duel and arena, and it does so without ever being told which mode it is in.

The share card is set from a URL and hides itself on failure.

```js
const img = $("shareCard");
if (s.reveal.share_card_url) {
  img.src = s.reveal.share_card_url;
  img.hidden = false;
  img.onerror = () => { img.hidden = true; };
} else {
  img.hidden = true;
}
```

Three sources feed that URL. In demo mode the server sends the committed
`"/static-assets/cards/decoy-3f2710c0a9e6_demo.jpg"`, which exists in the repo at 286348 bytes. In live
mode the server sends `null` at reveal and a background task fills it in later, which the media chapter
covers. In mock mode `mockSocket` sends `"static-assets/share_card.png"`, and that file does not exist
in the repo. I checked `web/static-assets/`. The `onerror` handler is what keeps a mock run from showing
a broken-image icon on a projector.

`.share-card` has `max-width: min(440px, 100%)` and no height or aspect ratio, so the slot is not
reserved before the bytes arrive.

### The QR lobby block

The join flow has two faces, and they are selected by the presence of a single URL parameter.

```js
// A scanned QR lands here with ?room=GROK: prefill the room so a phone joins
// with one tap, and show the QR block on the big screen (no ?room param) so
// the audience can scan it off the projector.
const PREFILL_ROOM = (new URLSearchParams(location.search).get("room") || "").toUpperCase();
if (PREFILL_ROOM) {
  $("roomInput").value = PREFILL_ROOM;
} else {
  try { $("lobbyQr").hidden = false; } catch (e) { /* qr block optional */ }
}
```

One URL serves both roles. The projector loads `/` with no parameter and shows the QR. A phone loads
`/?room=GROK` and shows a prefilled room field instead of the QR it just scanned. There is no separate
host page and no separate build.

The QR block itself is four lines of markup, hidden by default.

```html
<div class="lobby-qr" id="lobbyQr" hidden>
  <img src="static-assets/qr.png" alt="scan to join room GROK" class="qr-img">
  <div class="section-label">SCAN TO PLAY · ROOM GROK</div>
</div>
```

I decoded the committed `web/static-assets/qr.png` straight from the pixels, with no QR library
involved, because none is installed here and none is in `requirements.txt`. The method was: inflate
the PNG with `zlib`, sample the module grid, walk the standard zigzag placement, unmask, deinterleave
the four data blocks, and read the byte-mode payload. It is a 360 by 360 pixel image at scale 8 with a
2-module quiet zone, giving a 41 by 41 module grid, which is QR version 6. Mask pattern 6, error
level H. The payload is 45 bytes in byte mode:

```
https://arun0808-x-arcade.hf.space/?room=GROK
```

So the committed code already points at a hosted Space over HTTPS, not at localhost. It was
regenerated in commit `7a4e012` for exactly that reason: a phone cannot reach the presenter's laptop
at `localhost`, and a venue network may isolate clients from each other anyway.

`README.md:45-46` has not caught up. It still describes the file as a placeholder encoding
`http://localhost:8787/?room=GROK`, which is not what the bytes decode to. Trust the image, fix the
README. The regeneration one-liner at `README.md:50-52` is still correct: swap in your own host.

```sh
python -c "import segno; segno.make('https://YOUR-SPACE.hf.space/?room=GROK', error='h').save('web/static-assets/qr.png', scale=8, border=2, dark='#04070B', light='#FFFFFF')"
```

Error level `h` is the highest of the four, which is the right choice for a code that will be
photographed off a projector at an angle.

Room `GROK` is not special to the client. It is special to the server. `ARENA_ROOMS = {"GROK"}` at
`server/app.py:125` makes that room an arena: no two-player auto-start, and `next` from anyone other than
`room["host"]` is dropped.

The client is not told about any of this. `_public_state` carries no `host` field and no `arena` field.
`renderLobby` ends with:

```js
$("startBtn").disabled = !(joined && players.length >= 1);
```

So every scanned-in phone in room GROK sees a lit START button that the server will silently ignore. That
is a real gap between the two halves. `DEMO.md:241-243` documents the intended behavior in prose
("tapping NEXT on a phone does nothing"), which confirms the server side is the intent and the client
side is unfinished.

There is a second consequence of the same asymmetry. The server's `next` handler requires two players for
a lobby start, while the client enables the button at one. An arena host alone in the room has a lit
button that does nothing until a second person joins.

### Mock mode

`?mock=1` replaces the WebSocket with an object that has a single `send` method, and `mockSocket`
emulates enough server to play two complete rounds with no backend at all.

```js
function connect() {
  if (MOCK) { sock = mockSocket(handleRaw); setConn("MOCK LINK ACTIVE"); return; }
```

It is a real emulation, not a stub. It keeps a `players` array and adds a bot named `GLITCH` about
900ms after you join. It holds a 30-second deadline with `setTimeout` and marks the bot as locked in
somewhere between 5.2 and 7.2 seconds. It awards the round to whoever hits `decoy_slot` and returns
winner `"house"` when nobody does. It also reproduces the contract's stripping rule.

```js
if (phase === "guessing") {
  delete r.decoy_slot;
  delete r.decoy_rationale;
  for (const rep of r.replies) { delete rep.is_decoy; delete rep.author; }
}
```

The two fixture rounds are inline in the file at `game.js:374-415`, with real decoy rationales. Round A's
source post is the real one the `x_search` probe surfaced, the `@higgsfield_ai` post also used by
`card_forge.DEMO_ROUND`. The comment above `mockSocket` labels the content honestly: "All content
below is fixture data for the mock."

`DEMO.md`'s fallback tree lists this as the last resort: "Total catastrophe, server will not start: open
`web/index.html?mock=1` in any browser, straight from disk."

Two things to know before you rely on it.

`?mock=1` is a second, independent implementation of the game contract. Its stripping logic, its
deadline, and its round shape can drift away from `server/app.py` and nothing in the gate would notice.
Neither check script touches it. It is a stage safety net, not a security boundary.

Its reveal points at an asset that does not exist. `web/static-assets/` contains `cards/`, five
`host_*.mp3` files, and `qr.png`. There is no `share_card.png`. The `img.onerror` handler makes the
failure graceful rather than a broken-image icon, so the applause beat of the last-resort fallback shows
no card at all. The obvious fix is to repoint it at the committed
`static-assets/cards/decoy-3f2710c0a9e6_demo.jpg`.

### The visual design, as actually written

`:root` declares eleven color custom properties, plus two font stacks. These nine carry the arcade
look.

```css
--bg: #04070b;        /* near black with a blue cast */
--panel: #0a1018;
--panel-2: #0e1621;
--line: #14222f;      /* every border in the app */
--cyan: #00e5ff;      /* the single accent */
--cyan-soft: rgba(0, 229, 255, 0.14);
--cyan-glow: rgba(0, 229, 255, 0.45);
--red: #ff2d55;       /* used only for the decoy and the sub-5s timer */
--red-glow: rgba(255, 45, 85, 0.5);
```

The discipline is that red means exactly two things and nothing else: the machine, and time running out.
Everything else in the app is cyan on near black. When the decoy card turns red at reveal it is the only
red object on the screen, which is why the reveal reads instantly from the back of a room.

The texture is entirely CSS. There is no background image file.

```css
body {
  background-image:
    linear-gradient(rgba(0, 229, 255, 0.045) 1px, transparent 1px),
    linear-gradient(90deg, rgba(0, 229, 255, 0.045) 1px, transparent 1px);
  background-size: 42px 42px;
}
```

That is a 42px cyan grid at 4.5% opacity. On top of it sits `#crt`, fixed to the viewport with
`pointer-events: none` and `z-index: 50`.

```css
#crt {
  position: fixed;
  inset: 0;
  pointer-events: none;
  z-index: 50;
  background:
    repeating-linear-gradient(0deg, rgba(0,0,0,0.16) 0px, rgba(0,0,0,0.16) 1px, transparent 1px, transparent 3px),
    radial-gradient(ellipse at center, transparent 55%, rgba(0,0,0,0.5) 100%);
}
```

One-pixel dark lines every three pixels for the scanlines, and a radial vignette that starts darkening at
55% of the ellipse. `z-index: 50` puts it above the sticky topbar at `z-index: 40`, so the scanlines
cross the header too. `pointer-events: none` is what makes the full-viewport overlay harmless.

Glow is done with `text-shadow` and `box-shadow` rather than filters, layered two deep for the display
type.

```css
.wordmark {
  text-shadow: 0 0 8px var(--cyan-glow), 0 0 22px rgba(0, 229, 255, 0.25);
}
```

A tight bright halo plus a wide dim one. The same two-layer pattern appears on `.lobby-title` (8px and
48px) and `.winner-banner` (14px and 40px), scaled with the type size.

The lobby title is fluid.

```css
.lobby-title { font-size: clamp(56px, 16vw, 120px); letter-spacing: 10px; }
```

56px floor for a narrow phone, 120px ceiling so it does not become absurd on a projector, and 16vw
between. The winner banner uses the same technique with `clamp(28px, 7vw, 48px)`.

Buttons are outline-only until hover, when they invert.

```css
.big-btn {
  min-height: 58px;
  color: var(--cyan);
  background: transparent;
  border: 2px solid var(--cyan);
  box-shadow: inset 0 0 18px var(--cyan-soft), 0 0 18px rgba(0, 229, 255, 0.12);
  transition: background 0.12s, color 0.12s, transform 0.06s;
}
.big-btn:hover:not(:disabled) { background: var(--cyan); color: var(--bg); }
.big-btn:active:not(:disabled) { transform: translateY(2px); }
```

The 2px `translateY` on `:active` is a physical button press. The `inset` box shadow is what makes an
outline button read as a lit cabinet button rather than a wireframe.

The touch targets are sized for a phone. `min-height: 58px` on primary buttons, `min-height: 54px` on
inputs, `min-height: 32px` on the chip buttons, and `-webkit-tap-highlight-color: transparent` in the
universal reset so iOS does not paint a grey box over the neon.

The inputs are 20px, which is above the 16px threshold where iOS Safari auto-zooms on focus. That is a
real detail, not a coincidence, given every other font size in the file is 10 to 16px.

The reply grid is auto-fitting.

```css
.replies {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(270px, 1fr));
  gap: 14px;
}
```

At 980px, which is `main`'s `max-width`, that yields three columns. The single breakpoint at 620px
collapses it and shrinks the ring.

```css
@media (max-width: 620px) {
  .wordmark { font-size: 18px; letter-spacing: 2px; }
  .replies { grid-template-columns: 1fr; }
  .timer-wrap { width: 96px; height: 96px; }
  .timer-ring { width: 96px; height: 96px; }
  .timer-num { font-size: 32px; }
}
```

That is the entire responsive strategy. One media query, four rules.

Two rough edges in the CSS are worth knowing before you touch it.

`--accent` is used twice and never declared.

```css
.qr-img { border: 2px solid var(--accent, #22d3ee); ... }
.score.leader b { color: var(--accent, #22d3ee); text-shadow: 0 0 8px var(--accent, #22d3ee); }
```

`:root` defines `--cyan: #00e5ff` and nothing named `--accent`, so both fall through to `#22d3ee`. That
is a visibly different cyan sitting next to the rest of the palette on the reveal strip. `#22d3ee` is
also the deck brand color specified in the Claude Design prompt at `DEMO.md:254`, which suggests the
fallback was copied from the deck spec rather than the app palette. That connection is my reading of the
two files and is not stated anywhere.

Nothing in the file responds to `prefers-reduced-motion`. The scanline overlay, the 0.45s card flip, the
`bannerIn` scale, and the infinite `pulse` under five seconds all run regardless of the system setting.

`viewport-fit=cover` is declared in the meta tag but no rule uses `env(safe-area-inset-*)`, so the sticky
topbar and `main` have no notch padding.

### Deployment

The Space runs the same repo with no code changes and no configuration file. The whole deployment surface
is two files plus a README with front matter.

```dockerfile
FROM python:3.13-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The Space always runs the offline demo. No key, no secrets, no live calls.
ENV ARCADE_MODE=demo
EXPOSE 7860
CMD ["uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "7860"]
```

`requirements.txt` is three lines.

```
fastapi
uvicorn[standard]
websockets
```

There is no xAI client library, no image library, and no QR library in the image. The container
physically cannot make a model call.

The port is 7860 in three places that must agree. `EXPOSE 7860` and `--port 7860` in the Dockerfile, and
`app_port: 7860` in the Space front matter.

```yaml
---
title: X Arcade · Decoy
emoji: 🕹️
colorFrom: gray
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
license: mit
---
```

7860 is the Hugging Face Spaces convention, which is why it is the Docker port and why `run.sh` using
8787 locally is a deliberate separation rather than an inconsistency.

```sh
HOST="${ARCADE_HOST:-0.0.0.0}"
if [ -x .venv/bin/uvicorn ]; then
    exec .venv/bin/uvicorn server.app:app --host "$HOST" --port 8787 "$@"
fi
exec uvicorn server.app:app --host "$HOST" --port 8787 "$@"
```

`run.sh` binds all interfaces by default, so a phone on the same network can reach the laptop. Set
`ARCADE_HOST=127.0.0.1` to get loopback back. That default is recent. Commit `7a4e012` changed it,
because uvicorn's own default of 127.0.0.1 meant only the host machine could ever join. `DEMO.md:16-17`
still instructs the presenter to pass `--host 0.0.0.0` by hand as preflight step 2. That is now
redundant rather than wrong. `"$@"` forwards the flag, and uvicorn's CLI is click, where the last
occurrence of an option wins. Verified against the repo's own click 8.4.2.

`run.sh` also prefers `.venv/bin/uvicorn` when it exists, so a repo with a local virtualenv does not
depend on what is on `PATH`.

The Space carries no secrets, and that is structural rather than a policy note. `ENV ARCADE_MODE=demo` in
the image, and `config.MODE = os.environ.get("ARCADE_MODE", "demo")` reads it. Every live path is behind
a `config.MODE == "live"` check: `/token` short-circuits to an offline stub before importing anything
network-touching, `_do_reveal` attaches the committed JPEG and never schedules the live card render, and
rounds come from the six committed JSON files. Nothing in the serving path reads `fixtures/api/`.

`stage.sh` copies the smallest tree that can serve the demo.

```sh
cp "$repo_root/deploy/huggingface/Dockerfile" "$target/Dockerfile"
cp "$repo_root/deploy/huggingface/README.space.md" "$target/README.md"
cp "$repo_root/requirements.txt" "$target/requirements.txt"
cp "$repo_root/config.py" "$target/config.py"
cp "$repo_root/fixtures_core.py" "$target/fixtures_core.py"
cp -R "$repo_root/server" "$target/server"
cp -R "$repo_root/web" "$target/web"
cp -R "$repo_root/cartridges" "$target/cartridges"
cp -R "$repo_root/plugins" "$target/plugins"
cp -R "$repo_root/services" "$target/services"

find "$target" -type d -name __pycache__ -prune -exec rm -r {} +
find "$target" -type f -name '*.pyc' -delete
```

Note what is missing: `fixtures/`, `artifacts/`, `slides/`, `paper/`, `DEMO.md`, `CONTRACT.md`, the deck
files, and the two check scripts. `README.space.md` is renamed to `README.md` on the way in, because
Hugging Face reads the Space configuration out of the repo-root README's YAML front matter.

The script refuses to create its own target and exits 2 if the directory is not already there.

```sh
[[ -d "$target" ]] || { echo "target directory does not exist: $target" >&2; exit 2; }
```

The usual flow is to clone the Space repo, run `stage.sh` at it, then commit and push.

One asymmetry to be aware of if you ever flip the Space to live: `services/` is staged but `fixtures/` is
not, and `requirements.txt` does not include whatever `card_forge` needs. The live paths in
`_attach_live_card` are wrapped in a bare `except Exception: return`, so a live-mode Space would degrade
to reveals with no share card rather than crash. Whether it would fail at import or at call time is
UNVERIFIED. I did not run it.

The static mount in `server/app.py` is the last route registered, which is what makes `/` serve
`index.html`. Any new route added below it will be shadowed.

### What a phone user actually experiences

Assume the Space is warm and the QR points at it. Here is the whole path, step by step.

1. The phone camera reads the QR off the projector. The payload in the committed file is
   `https://arun0808-x-arcade.hf.space/?room=GROK`. The camera app shows a tap-to-open banner.

2. Safari or Chrome opens. There is no app install, no account, and no permission prompt. `GET /` is
   matched by the `StaticFiles` mount with `html=True`, which serves `web/index.html`.

3. The browser fetches exactly two subresources: `style.css` and `game.js`. Both are same-origin. There
   are no fonts, no CDN scripts, and no third-party requests. The page is near black with a cyan grid,
   the scanline overlay, and `DECOY` in Impact at up to 16vw.

4. `game.js` runs at the end of `<body>`, so the DOM is already parsed and every `$("id")` lookup
   resolves.

5. The mute state is restored from `localStorage.arcade_muted` and the SND button label is set.
   `fetch("/health")` fires. When it returns `{"mode": "demo", "rounds_available": N}` the cyan `DEMO`
   chip appears in the topbar.

6. `PREFILL_ROOM` reads `GROK` from the query string, uppercases it, and writes it into `#roomInput`. The
   `#lobbyQr` block stays hidden, because the phone just scanned the thing it would show.

7. `connect()` opens `wss://<space-host>/ws` immediately. The scheme is `wss:` because the Space is
   served over HTTPS. The connection line under the START button reads `LINKING...` then `LINKED`.

8. The player taps into the name field. The 20px font size keeps iOS from zooming. There is no `<form>`
   element and no keydown handler in `game.js`, so pressing Go or Return on the phone keyboard does
   nothing. The player has to dismiss the keyboard and tap `JOIN ROOM`.

9. The first touch anywhere on the page fires `unlockAudio` through a `{ once: true }` `pointerdown`
   listener. It sets each sound muted, calls `play()`, then pauses and unmutes on resolution. That
   satisfies the browser autoplay gesture requirement so the host lines can play later without a tap.

10. `JOIN ROOM` disables both inputs and the button, then sends
    `{"t":"join","room":"GROK","name":"NAME"}`. Names are uppercased and trimmed client-side. Empty name
    or room sets the connection line to `NAME AND ROOM CODE REQUIRED` and sends nothing.

11. The server creates or finds room `GROK`, adds a `PlayerState`, sets `room["host"]` if it was still
    `None`, and because `room["arena"]` is true it skips the two-player auto-start and just broadcasts.

12. The phone receives its first `state`, phase `lobby`. `renderLobby` rebuilds the player list, and the
    local player's row reads `NAME (YOU)` with `SCORE 0`. The list grows as the rest of the room scans
    in.

13. The phone also shows an enabled `START` button, because `renderLobby` enables it at
    `players.length >= 1`. Tapping it in an arena room sends `next`, and the server drops it because the
    sender is not the host. Nothing happens and nothing explains why. This is the one place the phone
    experience is currently wrong.

14. The host on stage taps START. `_start_round` pulls a safety-screened round, sets `phase` to
    `guessing`, sets `deadline_at` to now plus 30 seconds, starts the `asyncio` deadline task, and
    broadcasts.

15. On the phone, `handleState` sees a new `round_id`, increments `roundNo`, clears `myGuessSlot`, and
    stamps `guessStartAt`. `timerEndAt` is set from `deadline_ms`. The `host_intro.mp3` line plays.
    `render` hides the lobby and unhides the arena.

16. The screen now shows the source post card: the author handle, a one-letter avatar built from that
    handle, the topic in muted caps, and the post text with `white-space: pre-wrap` so its line breaks
    survive. The timer ring counts down beside it at 96px on a phone. Below that sit five reply cards in
    a single column, each at least 150px tall, each with a cyan hover and active border because they
    carry `.tappable`.

17. The player taps a card. It flips through 180 degrees over 0.45s to a cyan-bordered back face reading
    `LOCKED IN`. The guess is sent with the elapsed milliseconds.

18. Every other phone in the room gets a fresh broadcast and the opponent strip updates that player's
    chip from `NAME PICKING...` to `NAME LOCKED IN` in cyan.

19. Under five seconds the ring and the number turn red and the number pulses.

20. The reveal fires when every connected player has guessed or when the server's 30-second task expires.
    `host_reveal.mp3` plays. The decoy card is outlined in red with a `ROBOT` badge, the label `grok
    wrote this one`, and the rationale printed underneath in red above a dashed rule. The four real cards
    show their actual `@handles` in cyan. The player's own choice carries a dashed cyan outline and a
    `YOUR PICK` tag.

21. Below the cards the reveal panel unhides: the winner banner scaling in, the top-five leaderboard, the
    committed share card image, and `NEXT ROUND`.

Two honest caveats about steps 20 and 21 on a phone. First, the reveal panel is below the fold. Five
single-column cards at 150px minimum plus 14px gaps already exceed a typical phone viewport, and they
sit under the post card, the opponent strip, and the topbar. Nothing in `game.js` calls `scrollIntoView`
on the phase change, so the player has to scroll to see the banner. I derived this from the CSS rather
than measuring it on a device, so treat the exact fold position as UNVERIFIED. Second, `#connLine` lives inside
`<section id="screen-lobby">`, which gets the `hidden` attribute the moment the phase leaves `lobby`.
Every message `setConn` writes during play, including `LINK LOST, RETRYING...`, is written into a hidden
element. A phone that drops its connection mid-round gets no visible signal at all.

### Fastest way to get oriented

Open `web/index.html?mock=1` in a browser with no server running. `mockSocket` plays two complete rounds
with a bot opponent, a 30-second deadline, real decoy rationales, and a full reveal. That gives you every
screen and every CSS state in about ninety seconds. Then read `game.js` top to bottom, because it is
written in the order the app runs: audio, health badge, transport, `handleState`, render, timer, actions,
the QR prefill block, and the mock server last.

---

## Voice and generated media

Three services in this repo touch a generative surface: `services/voice_host.py` (Grok TTS and the
realtime voice token), `services/card_forge.py` (Grok Imagine share cards), and `services/poster.py`
(post-back to X, staged only). One rule explains all three, and it is the reason the demo is fast.

**Every generative call in the request path was moved out of the request path.** Voice lines are
rendered to mp3 at build time and committed. The demo share card was generated once and committed. The
only generative call that can happen while a round is playing is the live-mode share card, and even that
runs in a background task after the reveal has already been broadcast. In the default mode the game
makes zero outbound calls.

`paper/VISION.md` line 41 states the reasoning directly, citing the probe artifacts: `x_search` at 42
seconds, image generation at 6.5 seconds, TTS at 1.78 seconds, and the voice token mint at 0.13 seconds.
Forty-two seconds cannot sit in a request path. Once you accept that for rounds, you accept it for audio
and images too.

Two constants in `config.py` are declared and never read. `MODEL_IMAGE` is read once, at
`services/card_forge.py:91`. Outside `config.py` itself, `MODEL_VOICE` and `MODEL_VIDEO` appear only in
prose: `CONTRACT.md`, `services/REALTIME_NOTES.md`, and `ONBOARDING.md`. No Python or JavaScript in the
repo reads either one. If you wire live realtime voice, `MODEL_VOICE` is the value you have to plumb
through yourself.

### services/voice_host.py: two duties, one file

The module is 150 lines and does two unrelated jobs. Its own docstring says so:

```python
"""Voice host for X Arcade.

Two duties:
1. mint_token() gets a short-lived realtime client secret so the browser can
   talk to the realtime voice API directly. The server key never reaches the
   browser. Probed at build time: 200 in 0.13s.
2. render_host_lines() pre-renders every scripted host line to mp3 through
   /v1/tts at build time. The demo plays committed files and needs no network.
   Probed at build time: 1.78s per line, and the "language" field is required.
"""
```

Duty 2 runs at build time. Duty 1 runs at request time, through `GET /token`, and only in live mode.
That split is the whole design.

#### The five host lines

Every scripted line the game can say lives in one dict at `services/voice_host.py:36`.

```python
LINES: dict[str, str] = {
    "host_intro": (
        "Welcome to the arcade. [pause] Tonight, one of the players "
        "at this cabinet is not a player at all."
    ),
    "host_round": "Four humans. One machine. Thirty seconds.",
    "host_reveal": "Hands off the buttons. [pause] The decoy was...",
    "host_win": "Got it! The machine never stood a chance.",
    "host_lose": "Wrong! [pause] The machine walks free. House wins.",
}

TTS_VOICE = "Eve"
TTS_LANGUAGE = "en"
```

The dict key is the filename stem. `render_host_lines()` writes each one to
`web/static-assets/<key>.mp3`. All five files are committed and tracked in git:

| File | Bytes | Approx. duration |
|---|---|---|
| `web/static-assets/host_intro.mp3` | 96,768 | ~6.0 s |
| `web/static-assets/host_lose.mp3` | 66,432 | ~4.2 s |
| `web/static-assets/host_reveal.mp3` | 56,448 | ~3.5 s |
| `web/static-assets/host_round.mp3` | 53,760 | ~3.4 s |
| `web/static-assets/host_win.mp3` | 40,704 | ~2.5 s |

`file` reports all five as MPEG layer III, v2, 128 kbps, 24 kHz, mono. The durations above are derived
by dividing file size by the 128 kbps constant bitrate. They are arithmetic on the committed byte counts,
not measured playback.

The `[pause]` markers are speech tags the TTS surface accepts. They survive into the mp3 and are absent
from the spoken text. `DEMO.md` lines 135 to 142 reprint all five texts without the tags, as the presenter's
script for the case where browser audio is blocked and the host lines have to be spoken by a human.

#### Rendering: the network refusal

```python
def render_host_lines(force: bool = False) -> list[Path]:
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, text in LINES.items():
        out_path = ASSETS_DIR / f"{name}.mp3"
        if out_path.exists() and not force:
            written.append(out_path)
            continue
        if not (config.RECORD or config.MODE == "live"):
            raise RuntimeError(
                f"{out_path.name} is missing and this is demo mode. "
                "Run: ARCADE_MODE=live ARCADE_RECORD=1 python3 services/voice_host.py"
            )
        audio = _tts(text)
        out_path.write_bytes(audio)
        written.append(out_path)
```

Read the guard carefully. It is not "skip TTS in demo mode." It is "if the file is missing and you are in
demo mode, crash with instructions." That distinction matters. A silent skip would let a demo start with
a missing asset and fail on stage. This fails at build time, loudly, with the exact command to fix it.

The existence check before the guard also means re-running the renderer costs nothing. Pass `force=True`
to re-render.

`_tts()` is the actual call:

```python
def _tts(text: str) -> bytes:
    """Render one line to mp3 bytes. The language field is required."""
    body = _post(
        "/tts",
        {
            "text": text,
            "voice": TTS_VOICE,
            "language": TTS_LANGUAGE,
            "response_format": "mp3",
        },
        timeout=60,
    )
    # The surface returns raw mp3 bytes today (probe evidence). Tolerate a
    # JSON envelope with base64 audio in case the surface changes.
    if body[:1] in (b"{", b"["):
        ...
```

The response handling sniffs the first byte. Raw mp3 starts with `ID3` or a frame sync, never `{` or `[`,
so a leading brace means the surface started wrapping audio in JSON. The fallback path then looks for an
`audio` or `data` key holding base64. This is defensive code against a surface change, not a code path
that has ever been observed to fire.

`_post()` builds the request with `urllib.request` and an `Authorization: Bearer` header from
`os.environ["XAI_API_KEY"]`, against `config.API_BASE + path`. There is no HTTP client dependency.
`requirements.txt` is three lines: `fastapi`, `uvicorn[standard]`, `websockets`.

#### The build-time commands

```sh
# Render the committed mp3s (writes web/static-assets/*.mp3)
ARCADE_MODE=live ARCADE_RECORD=1 python3 services/voice_host.py

# Token smoke test (prints the response shape, never the secret)
python3 services/voice_host.py mint
```

The `mint` subcommand prints only `keys`, `value_present`, and `expires_at`. It never prints the token.
That redaction is repeated in `services/probe_surfaces.py:98`, which records `response_keys` and a
`secret_value_present` boolean into the probe artifact and nothing else.

#### A discrepancy in the TTS probe artifact

`artifacts/probes/tts.json` reads:

```json
{
 "surface": "tts",
 "status": 200,
 "latency_s": 1.7787859439849854,
 "audio_bytes": 82560,
 "saved": "probe_host_line.mp3",
 "note": "language field required"
}
```

The committed `services/probe_surfaces.py` cannot have produced this file exactly as it stands. Its TTS
payload at line 109 omits `language`, and the script never writes a `note` key into any record. The
`language` requirement that `voice_host._tts()` honors is documented only in that hand-added note and in
the module docstring. Treat the note as a real finding from the build session and the probe script as
having drifted from the version that produced the artifact. Which of the two changed is UNVERIFIED.

#### What the client actually plays: two of five

This is the gap that will surprise you first. `web/game.js` loads exactly two of the five committed
lines.

```js
const sounds = {
  intro: makeSound("static-assets/host_intro.mp3"),
  reveal: makeSound("static-assets/host_reveal.mp3"),
};
```

And plays them on exactly two phase transitions, at `web/game.js:126`:

```js
if (s.phase === "guessing" && was !== "guessing") playSound("intro");
if (s.phase === "reveal"   && was !== "reveal")   playSound("reveal");
```

`host_round.mp3`, `host_win.mp3`, and `host_lose.mp3` are rendered, committed, served by the static
mount, and never referenced by any client code. `host_intro` plays on every transition into guessing, so
the line "Tonight, one of the players at this cabinet is not a player at all" replays at the top of every
round, not only the first. Wiring win and lose is a `renderReveal` change: the winner is already in
`s.reveal.winner` and the client already compares it to `myName` for the banner text.

Three details in the audio layer are worth copying if you add a sound anywhere else.

The load-error latch. `makeSound` sets `a.dataset.ok = "no"` on an `error` event, and `playSound` refuses
to touch a sound marked `"no"`. A missing or corrupt mp3 degrades to silence instead of throwing on every
round.

The autoplay unlock. `unlockAudio` runs once on the first `pointerdown`, plays each sound muted, then
pauses and rewinds it. Browsers gate audio on a user gesture. The join tap is that gesture, and by the
time the first round starts, both clips are unlocked and buffered.

The mute switch. `muted` persists to `localStorage` under `arcade_muted`, and every `localStorage` access
sits inside a `try`/`catch`. `playSound` itself is wrapped in a `try` with the comment
`/* never let audio break the game */`.

### The ephemeral token endpoint

#### What voice_host.mint_token returns

```python
def mint_token() -> dict[str, Any]:
    """Mint an ephemeral realtime client secret for browser-direct voice.

    Returns the parsed response. Observed shape at build time:
    {"value": "<ephemeral token>", "expires_at": <unix seconds>}.
    The caller hands "value" to the browser and nothing else. Never log
    the value. See REALTIME_NOTES.md for the browser wiring.
    """
    body = _post("/realtime/client_secrets", {}, timeout=15)
    return json.loads(body)
```

A POST to `https://api.x.ai/v1/realtime/client_secrets` with an empty JSON body and a 15-second timeout.
The 15 is deliberate and much tighter than the 60 used for TTS. A token mint that has not returned in 15
seconds is not going to help a live session.

The response shape is flat. `services/REALTIME_NOTES.md` line 11 makes the point that the probe was
checking for: there is no nested `client_secret` object, so read `value` at the top level. The probe
artifact confirms it:

```json
{
 "surface": "voice_token",
 "status": 200,
 "latency_s": 0.13398122787475586,
 "response_keys": ["expires_at", "value"],
 "secret_keys": [],
 "secret_value_present": false
}
```

`secret_keys: []` and `secret_value_present: false` are the probe reporting that the nested-object shape
it was prepared for did not appear. That is a negative result recorded honestly, not an error.

#### The route in server/app.py

```python
@app.get("/token")
async def token() -> Any:
    """Mint a realtime voice token via services.voice_host.

    Demo mode is fully offline, so it returns a clearly labeled stub instead
    of touching the network. Realtime voice is a live-mode enhancement, and
    the pre-rendered host line mp3s cover the demo.
    """
    if config.MODE != "live":
        return {
            "demo": True,
            "value": "",
            "expires_at": None,
            "detail": "demo mode is offline. Run ARCADE_MODE=live to mint a realtime token.",
        }
    try:
        from services.voice_host import mint_token
    except ImportError:
        raise HTTPException(status_code=501, detail="voice_host is not available")
    return await asyncio.to_thread(mint_token)
```

Four things in eleven lines of body.

The demo stub keeps the shape. It returns `value` and `expires_at` alongside a `demo: true` flag and a
plain-English `detail`. A client that only reads `value` gets an empty string rather than a `KeyError`,
and a human reading the response learns why and what to do about it. It returns HTTP 200, not an error
status, because in demo mode nothing is wrong.

The condition is `!= "live"`, not `== "demo"`. Any unrecognized mode string falls into the offline stub,
which is the fail-safe direction.

The import is lazy and inside the live branch. Demo mode never imports `services.voice_host` at all, so
the demo cannot fail on a voice import.

`mint_token` uses blocking `urllib`, so it runs through `asyncio.to_thread`. The single event loop that
owns every room's websocket and round timer never blocks on a token mint.

`integration_check.py:150` asserts the demo behavior on every run:

```python
status, body, _ = await http_get("/token")
token = json.loads(body)
check(status == 200 and token.get("demo") is True, "/token returns the offline demo stub")
```

That check runs inside a harness that has already patched `socket.socket.connect` to raise on any host
outside `{"127.0.0.1", "::1", "localhost"}`. If `/token` ever tried to reach the network during the
offline proof, the run would fail.

#### Why browser-direct, and why no server secret reaches the client

The browser needs a bidirectional audio stream with sub-second turnaround. Proxying that through the
FastAPI server would mean relaying every microphone chunk up and every audio delta back down, doubling
the hops and putting the game's event loop in the audio path. Browser-direct removes the server from the
stream entirely after the handshake.

The obstacle is that browser-direct requires the browser to authenticate, and the browser is a hostile
environment. The ephemeral token is the answer. `XAI_API_KEY` is read only inside
`voice_host._api_key()`, which runs server-side. The server exchanges the long-lived key for a
short-lived client secret and hands the browser the short-lived one. The `expires_at` field is the whole
point: a leaked ephemeral token expires. `REALTIME_NOTES.md` line 27 draws the operational conclusion,
which is to mint per session rather than at page load.

The transport detail that forces this design is in `REALTIME_NOTES.md` line 43. A browser `WebSocket`
cannot set an `Authorization` header. The token has to ride somewhere else, and the OpenAI-compatible
convention puts it in the subprotocol list:

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

Note two mismatches between that snippet and the code as it stands. The path is written as
`/api/voice-token`, and the implemented route is `/token`. The model id is written literally, and
`config.MODEL_VOICE` is never read by anything. Both are one-line fixes when you wire this, and both are
places where a copy-paste would silently work against the wrong thing.

`REALTIME_NOTES.md` also flags its own limits. Line 18 says the socket handshake itself was not probed
and the subprotocol strings should be treated as UNVERIFIED until the first live connect. Everything in
the file below the token mint is documented convention, not measured behavior.

The rest of the file is the wiring plan, and it earns its length in one section. Section 4, the
force-messages trick, solves the problem a live host creates. The game needs exact scripted lines on cue
while the host free-talks between cues. The answer is `response.create` with per-response `instructions`
that override the session instructions for that one response, preceded by a `response.cancel` so the cue
does not queue behind whatever the host was riffing on. Section 5 defines the fallback ladder, and it is
short: rung 1 is the live socket, rung 2 is the committed mp3s. Any socket error drops to rung 2
instantly and silently. The line texts in `voice_host.LINES` are identical to the mp3 contents, so the
two rungs are interchangeable mid-game.

No browser code for any of this exists yet. `web/game.js` contains no WebSocket connection other than
the game socket to `/ws`, and no fetch other than `/health`.

### services/card_forge.py: the Imagine share card

132 lines. One public function.

```python
def make_share_card(round_data: dict[str, Any], winner: str) -> Path:
    topic = round_data["source"]["topic"]
    request = {
        "model": config.MODEL_IMAGE,
        "prompt": _card_prompt(topic, winner),
        "n": 1,
        "response_format": "b64_json",
    }
    response = STORE.call(
        "image_gen",
        request,
        invoke=lambda: _post_json("/images/generations", request, timeout=120),
    )
    raw = base64.b64decode(response["data"][0]["b64_json"])
    extension = "png" if raw[:4] == b"\x89PNG" else "jpg"
    CARDS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CARDS_DIR / f"{round_data['round_id']}_{_slug(winner)}.{extension}"
    out_path.write_bytes(raw)
    return out_path
```

The function reads exactly two fields off the round: `source.topic` and `round_id`. It never sees the
replies, the decoy slot, or the rationale. That narrow input is what makes the fixture hash stable across
everything else the round might contain.

The prompt is a fixed template with two slots:

```python
def _card_prompt(topic: str, winner: str) -> str:
    """Tight template. No real people, no logos, ever."""
    return (
        "Retro arcade wanted poster, neon on black, halftone print texture, "
        "scanlines. Huge DECOY branding across the top in chunky pixel type. "
        f"Theme of the round: {topic}. "
        f'Banner near the bottom reads "{winner} SPOTTED THE DECOY". '
        "Center art: one cartoon robot trying to blend into a lineup of four "
        "faceless human silhouettes, spotlight on the robot. "
        "No real people, no celebrity likeness, no brand logos, no X logo."
    )
```

The trailing negative constraint is a safety measure that lives in the prompt rather than in the safety
plugin. `plugins/safety/screen.py` screens rounds, and it has no entry point for a bare string, so
nothing downstream inspects the generated image. The prompt is the only gate here. Also note that
`winner` is interpolated straight into the prompt, and the winner name comes from a player-typed field
capped at `maxlength="16"` in the browser only. The server does not enforce that cap.

The extension sniff on line 102 is honest about a small surprise. `b64_json` does not promise PNG. The
committed card is a JPEG, 1280x720, and the fixture records `mime_type: "image/jpeg"`. The function
checks for the PNG magic bytes and falls back to `.jpg`, so the filename always matches the actual bytes.
The docstring says so in as many words.

#### The fixture store

```python
STORE = FixtureStore(
    root=REPO_ROOT / "fixtures" / "api",
    record=config.RECORD,
    reuse_existing=os.environ.get("ARCADE_REUSE_FIXTURES", "1") == "1",
)
```

Replay is the default. In replay mode the `invoke` callable is never called, so demo mode makes no
network call by construction, not by a flag check inside the request function. Two details in that
constructor are load-bearing. `fixtures_core` reads `ADJ_*` environment variables by default and X Arcade
uses `ARCADE_*`, so both switches are passed explicitly and the `ADJ_*` names never matter here. And
`reuse_existing` defaults on, so a repeat record run does not pay for the same image twice. To force a
re-render you delete the fixture file.

The recorded fixture is 382 KB and looks like this:

```json
{
  "format_version": 1,
  "request": { "model": "...", "n": 1, "prompt": "...", "response_format": "b64_json" },
  "request_sha256": "...",
  "response": {
    "data": [{ "b64_json": "<381803 chars>", "mime_type": "image/jpeg" }],
    "usage": { "cost_in_usd_ticks": 200000000 }
  },
  "response_sha256": "...",
  "surface": "image_gen"
}
```

`cost_in_usd_ticks: 200000000` is what the API returned in the recorded response. The tick-to-dollar
conversion is not defined anywhere in this repo, so do not convert it.

That fixture is broken in this public copy and cannot replay. The full diagnosis is in the fixture
chapter. What matters here is the blast radius, which is narrow: in demo mode `server/app.py` never calls
`card_forge` at all, it serves the committed JPEG directly. Only `ARCADE_MODE=live` reaches
`make_share_card`, and live mode records rather than replays. The broken fixture is a landmine for anyone
who runs the module's own demo check or tries to test the card path offline, not for the demo itself.

#### Where the card enters the game loop

The reveal is built in `_do_reveal`, and the card decision is one line:

```python
    # Demo mode attaches the committed card instantly. Live mode starts
    # with no card and a background task fills it in a few seconds later.
    "share_card_url": None if config.MODE == "live" else DEMO_CARD_URL,
}
await _broadcast(room)
if config.MODE == "live":
    asyncio.create_task(_attach_live_card(room, rnd, winner))
```

Demo mode attaches `DEMO_CARD_URL = "/static-assets/cards/decoy-3f2710c0a9e6_demo.jpg"` synchronously, in
the same broadcast that reveals the decoy. There is no wait. Live mode does the opposite. It broadcasts
the reveal with `share_card_url: None`, then spawns a task:

```python
async def _attach_live_card(room, rnd, winner) -> None:
    """Render the live share card off the event loop, then re-broadcast.

    card_forge takes about 6.5 seconds per image, so the reveal itself is
    never delayed by it. Any failure here leaves the reveal without a card
    and the game keeps going.
    """
    reveal = room.get("reveal")
    if reveal is None:
        return
    try:
        from services.card_forge import make_share_card
        display = winner if winner != "house" else "The House"
        path = await asyncio.to_thread(make_share_card, rnd, display)
        url = "/static-assets/cards/" + path.name
    except Exception:
        return
    if room.get("reveal") is reveal and room["phase"] == "reveal":
        reveal["share_card_url"] = url
        await _broadcast(room)
```

Three guards worth naming. The import is lazy and inside the `try`, so a broken `card_forge` cannot take
down the reveal. The bare `except Exception: return` means a failed render leaves the reveal card-less and
the game continues. And the identity check `room.get("reveal") is reveal` catches the race where the host
hit NEXT during the 6.5 seconds. It compares object identity, not equality, so a new round's reveal object
never gets last round's card stapled onto it.

`"The House"` is substituted for the internal sentinel `"house"` before the name reaches the prompt, so
the banner reads "THE HOUSE SPOTTED THE DECOY" rather than "HOUSE SPOTTED THE DECOY".

The client handles both shapes: a null URL hides the image and the re-broadcast a few seconds later fills
it in, and a URL that 404s hides the image too. There is no loading spinner and no placeholder, so in
live mode the card simply appears.

The committed demo card is `web/static-assets/cards/decoy-3f2710c0a9e6_demo.jpg`, 286,348 bytes, 1280x720
JPEG. The `decoy-3f2710c0a9e6` prefix is exactly what `DEMO_ROUND["round_id"]` evaluates to:
`"decoy-" + sha256(b"2085772302130753606:xarcade").hexdigest()[:12]`. The `_demo` suffix is not.
`make_share_card` would name that file `decoy-3f2710c0a9e6_player1.jpg`, because `DEMO_WINNER` is
`"PLAYER1"` and `_slug` lowercases it. The committed file was renamed by hand after generation.
UNVERIFIED how, but the consequence is concrete: regenerating the demo card writes a new filename that
`DEMO_CARD_URL` does not point at, and you have to rename it back.

`integration_check.py:208` asserts `reveal["share_card_url"] == DEMO_CARD_URL` on every round of the
offline proof, and line 160 fetches the URL and checks it serves more than 10,000 bytes. A broken rename
would fail both.

There is one dangling asset reference in mock mode, covered in the client chapter: `mockSocket` points at
`static-assets/share_card.png`, which does not exist.

### services/poster.py: post-back is STAGED and has never touched the X API

This is the shortest file in the media path, 43 lines, and the one to be most careful about describing.
Here is the entire module docstring, verbatim:

```python
"""STAGED share-card poster. Nothing here touches the X API yet.

post_to_x() logs exactly what a real post would contain and returns a staged
permalink so the UI flow can be demoed end to end. Post text carries no URLs
by design, the card image is the payload.

TODO (STAGED, real wiring): create the post with
POST https://api.x.com/2/tweets after uploading the card via the media
upload endpoint and passing its media id in the payload. Requires OAuth
user context for the arcade account. Cost noted at $0.015 per post create
(provider-quoted, unverified, not yet incurred).
Wire it only behind an explicit ARCADE_POST=1 switch, never by default.
"""
```

And the entire function:

```python
def post_to_x(card_path: str | Path, text: str) -> dict[str, Any]:
    """Stage a post. Logs what WOULD be sent and returns a fake permalink."""
    card = Path(card_path)
    size = card.stat().st_size if card.is_file() else 0
    digest = hashlib.sha256(f"{card.name}:{text}".encode()).hexdigest()[:10]
    permalink = f"https://x.com/xarcade/status/staged-{digest}"
    print("STAGED POST (not sent)")
    print(f"  text:  {text}")
    print(f"  media: {card.name} ({size} bytes)")
    print(f"  staged permalink: {permalink}")
    return {"status": "staged", "permalink": permalink, "media": card.name}
```

**The staged return shape, exactly:**

```python
{"status": "staged", "permalink": "https://x.com/xarcade/status/staged-<10 hex>", "media": <card filename>}
```

The `<10 hex>` is `sha256(f"{card.name}:{text}")` truncated to 10 hex characters, so identical inputs
always produce the same staged permalink. The `status` field is the literal string `"staged"`. There is
no code path in this module that returns anything else.

The evidence that nothing is sent is not a claim, it is the import list. The module imports `hashlib`,
`pathlib.Path`, and `typing.Any`. It does not import `config`. It does not import `urllib`. It does not
import any HTTP client. There is no network code in the file. `requirements.txt` contains no HTTP library
and no OAuth library, so there is nothing installed that could send a post either.

`post_to_x` has zero callers. A grep across `.py`, `.js`, `.md`, `.html`, and `.sh` returns three hits,
all inside `poster.py`: the docstring mention on line 3, the definition on line 22, and the `__main__`
demo on line 43. `server/app.py` imports `services.card_forge` and `services.voice_host` and nothing else
from `services`. The websocket handler accepts exactly three message types (`join`, `guess`, `next`) and
drops anything else. The HTTP routes are `GET /health`, `GET /token`, and the static mount. There is no
post route, no post message type, no post button in `web/index.html`, and no fourth `send()` call in
`web/game.js`. The reveal panel markup is four elements: `winnerBanner`, `scoreStrip`, the `shareCard`
img, and `nextBtn`.

`DEMO.md` pins the same status in two places. The run-of-show at lines 91 to 93 scripts the presenter
saying "Posting back to X is staged today, not live, and I will say exactly that every time it comes
up." The
numbers card at line 211 has the row `| Post-back status | STAGED, never sent | services/poster.py |`.

Two things in the TODO are not measurements and must never be repeated as if they were. The `$0.015 per
post create` is labeled in the code itself as provider-quoted, unverified, and not yet incurred. And
`api.x.com` appears nowhere else in the codebase and has no config constant. Every live call in this repo
goes to `config.API_BASE`, which is `https://api.x.ai/v1`, a different host.

`ARCADE_POST` does not exist as a switch. Grep returns one hit in a `.py` file, and it is the TODO text
itself. If you wire this, add the switch to `config.py` first, before writing any network code, so the
default path can never fire.

Two defects in the module as it stands. The `__main__` block points at
`web/static-assets/cards/demo.png`, which does not exist. The only committed card is
`decoy-3f2710c0a9e6_demo.jpg`. And `post_to_x` reports size 0 for a missing file instead of raising.
Running the module today produces:

```
STAGED POST (not sent)
  text:  PLAYER1 spotted the decoy. Can you?
  media: demo.png (0 bytes)
  staged permalink: https://x.com/xarcade/status/staged-eeeebfd760
```

That output looks like success. A missing card should be a hard error before any upload can ever be
attempted.

### Build time versus request time

The summary:

| Thing | When it runs | Command or trigger | Network in demo mode |
|---|---|---|---|
| Host line mp3s | Build time, once | `ARCADE_MODE=live ARCADE_RECORD=1 python3 services/voice_host.py` | None. Files are committed. |
| Playing a host line | Request time, client-side | `handleState` phase transition in `web/game.js` | None. Static file fetch. |
| Demo share card | Build time, once | `ARCADE_MODE=live ARCADE_RECORD=1 python3 services/card_forge.py` | None. JPEG is committed. |
| Live share card | Request time, after the reveal broadcast | `_attach_live_card` task, live mode only | Not reached. Demo mode never calls it. |
| Voice token mint | Request time | `GET /token`, live mode only | None. Demo returns the offline stub. |
| Post-back | Never | No caller exists | None. No network code exists. |

The demo runs from `run.sh` with `ARCADE_MODE` unset, which defaults to `demo`. The Hugging Face
`Dockerfile` sets `ENV ARCADE_MODE=demo` explicitly with the comment `# The Space always runs the offline
demo. No key, no secrets, no live calls.` Note that `deploy/huggingface/stage.sh` copies `services/` but
not `fixtures/`, which is consistent: the Space serves the committed card file and never calls
`card_forge`.

`paper/VISION.md:47` states the staging in the project's own words. Hold any change to this chapter's
code against those four sentences. The five host voice lines are pre-rendered mp3s, not synthesized on
stage. The share card was generated once and committed. Posting the card back to X is staged, not
live. The ephemeral token endpoint is real and returns no server secret, but realtime voice is a
live-mode enhancement the demo does not depend on.

### Where to start if you are changing this

Play the three unused mp3s. `host_round`, `host_win`, and `host_lose` are already rendered and served.
Wiring them is a `renderReveal` change plus two entries in the `sounds` object, and the win/lose choice is
`s.reveal.winner === myName`.

Fix the `image_gen` fixture before you touch anything else in the card path. Until
`ARCADE_MODE=demo python3 services/card_forge.py` produces an image, you have no offline test for that
code.

If you wire live voice, do the `config.MODEL_VOICE` plumbing and the `/token` path reconciliation as the
first two commits, and treat the subprotocol strings in `REALTIME_NOTES.md` as UNVERIFIED until your
first successful connect. The fallback ladder exists so the demo cannot regress while you work.

If you wire the poster, add `ARCADE_POST` to `config.py` first, make a missing card a hard error second,
and screen the winner name third. That name reaches an image prompt today and would reach post text
tomorrow, and the server enforces no cap on it.

---

## Demo mode, live mode, and the fixture layer

X Arcade has to survive a stage. The whole design of this layer follows from one requirement stated in
`DEMO.md`: "The demo runs in `ARCADE_MODE=demo`, fully offline." Everything below exists to make that
sentence true and to make it checkable by a command that returns an exit code.

Two files carry the mode decision. `config.py` is 20 lines and holds every model id and both env
switches. `fixtures_core.py` is the content-addressed record and replay store that lets a live API call
be captured once at build time and replayed forever after with no socket open. A third file,
`integration_check.py`, is the proof: it monkeypatches `socket.socket.connect` so that any attempt to
leave loopback is a hard failure, then plays a full two-player match through the real server.

The single most important structural fact, and the one that surprises people: **the stage demo never
calls into `fixtures_core.py` at all.** The fixture store is a build-time tool. The runtime demo path
reads committed JSON and committed media off disk. Understanding that split is the difference between
reading this codebase correctly and reading it wrong.

### config.py in full

```python
"""One place for every model id and mode switch. Nothing else hardcodes these."""

import os

MODEL_TEXT = "grok-4.5"
MODEL_IMAGE = "grok-imagine-image"
MODEL_VIDEO = "grok-imagine-video-1.5"
# Pinned deliberately: the grok-voice-latest alias was repointed on 5 Aug 2026,
# three days before the event. A pinned id cannot change under us on the day.
MODEL_VOICE = "grok-voice-think-fast-2.0"

API_BASE = "https://api.x.ai/v1"

# demo: fixtures only, zero network, the mode the stage demo runs in.
# live: real API calls, records fixtures when ARCADE_RECORD=1.
MODE = os.environ.get("ARCADE_MODE", "demo")
RECORD = os.environ.get("ARCADE_RECORD", "") == "1"

ROUND_SECONDS = 30
REPLIES_PER_ROUND = 5
```

That is the entire file. Two things about its shape are load-bearing.

`MODE` and `RECORD` are read at **import time**, not per request. Setting `ARCADE_MODE` after `config`
has been imported does nothing. This is why `integration_check.py` sets
`os.environ["ARCADE_MODE"] = "demo"` at line 29, before the `import config` at line 49, and why
`server/selfcheck.py` does its `os.environ.setdefault("ARCADE_MODE", "demo")` at line 30 before its
`import config` at line 38. If you add a new entry point, set the env var before the first `config`
import or the switch silently does nothing.

`ROUND_SECONDS = 30` is the shipped clock, and `DEMO.md`'s numbers card cites it as the "Round clock"
with the path `config.py ROUND_SECONDS`. Both check scripts override it in process
(`integration_check.py` sets 15, `server/selfcheck.py` sets 2). Never read a timer figure out of a check
script.

#### Why the model ids are pinned rather than -latest

The comment in the file is the reason, and it is a specific incident rather than a general principle: the
`grok-voice-latest` alias was repointed on 5 August 2026, three days before the event. `CONTRACT.md`
restates it as a rule for all four ids, describing them as "(pinned, not -latest)".

There is a second reason that only becomes visible once you understand the fixture key. The model id is
part of every request payload, and the request payload is what gets hashed into the fixture filename. A
floating alias would still hash to the same string, so replay would keep working, but the fixture would
now be replaying a response produced by a model the alias no longer points at. Worse, if the id itself
were interpolated from a resolved alias, the hash would change and every committed fixture would go stale
in one step. A pinned literal keeps the recorded artifact and the live call describing the same thing.

`services/REALTIME_NOTES.md` says the same in one line: "The model id comes from `config.MODEL_VOICE` and
is pinned on purpose."

### What ARCADE_MODE actually changes

Grepping every reader of `config.MODE` across the repo gives four sites, and only four. This is the
complete list.

**`server/app.py:310`, the reveal payload.** `"share_card_url": None if config.MODE == "live" else
DEMO_CARD_URL`. In demo mode the reveal broadcast carries the committed card in the same message as the
winner. No render, no wait.

**`server/app.py:313`, the live-only card render.** `_attach_live_card` does
`from services.card_forge import make_share_card` inside the function body. That lazy import is doing
real work. In demo mode the branch never runs, so `services/card_forge.py` is never imported, so
`fixtures_core.py` is never imported, so the fixture store is never constructed.

**`server/app.py:342`, `/health`.** `return {"mode": config.MODE, "rounds_available":
_rounds_available()}`. `mode` is the raw string, so `/health` is the honest answer to "which mode is this
process in". `DEMO.md` preflight step 3 curls it and expects `{"mode": "demo", "rounds_available": 5}`
exactly.

**`server/app.py:353`, `/token`.** The condition is `!= "live"`, so any unrecognized mode string falls
into the offline stub. The route body is in the voice chapter.

One more mode reader lives outside `server/`. `services/voice_host.py:126` guards the TTS render with
`if not (config.RECORD or config.MODE == "live")` and raises with the exact re-render command. That check
fires only when a host mp3 is missing. All five are committed, so the guard is a tripwire rather than a
normal path.

**Where `ARCADE_MODE` does nothing.** `cartridges/decoy/round_builder.py` never reads `config.MODE`. Live
is selected by the `--live` CLI flag alone.

### ARCADE_RECORD and the three-state truth table

`config.RECORD` is consumed in exactly two places, both in build-time tooling.
`services/card_forge.py:39` constructs its store once at module level with `record=config.RECORD`.
`cartridges/decoy/round_builder.py:145` builds its store per invocation in `_make_store`, whose docstring
states the policy:

```python
    """Map ARCADE env vars onto the ported fixture layer.

    Replay is the default. live plus ARCADE_RECORD=1 records. live without
    ARCADE_RECORD calls the API directly and persists nothing, which is what
    the contract says live mode means.
    """
```

Returning `None` is the third state. `_call_surface` at line 168 treats a `None` store as "bypass the
fixture layer entirely" and calls `_http_json` directly, so nothing is written to disk. The truth table
for the round builder:

| `--live` | `ARCADE_RECORD` | store | network | writes fixtures |
|---|---|---|---|---|
| absent | anything | `FixtureStore(record=False)` | none | no |
| present | unset | `None` | yes | no |
| present | `1` | `FixtureStore(record=True)` | yes | yes |

Two traps in that table. Recording is the only way a live round stays reproducible, so `--live` without
`ARCADE_RECORD=1` produces a round file that `build_round(topic, live=False)` can never rebuild. And the
two callers disagree on the default for `ARCADE_REUSE_FIXTURES`: `card_forge.py` defaults it to `"1"`
(reuse on, so a repeat record run does not pay for the same image twice), while `round_builder.py`
defaults it off. A shell that exports the variable for one script quietly changes the behavior of the
other.

### fixtures_core.py: content-addressed record and replay

The module docstring states the contract:

```python
"""Content-addressed recording and replay for external calls.

Replay is the default. Set ``ADJ_RECORD=1`` only while intentionally making live
calls and refreshing fixtures. A fixture key depends only on the named surface
and its normalized request, so identical inputs always select the same file.
"""
```

The `ADJ_*` names are lineage. `CONTRACT.md` calls this layer "ported from Adjacency," and
`FixtureStore.__init__` still reads `ADJ_FIXTURE_DIR`, `ADJ_RECORD`, and `ADJ_REUSE_FIXTURES` as
defaults. Both X Arcade callers pass `root`, `record`, and `reuse_existing` explicitly, which shadows all
three, and `card_forge.py` says so in a comment. Treat the `ADJ_*` names in the docstring as
documentation debt, not as a switch you can pull.

#### The key

```python
def request_hash(self, surface: str, request: Any) -> str:
    self._validate_surface(surface)
    normalized_request = _jsonable(request)
    _reject_secrets(normalized_request)
    return _sha256(
        {
            "format_version": FORMAT_VERSION,
            "request": normalized_request,
            "surface": surface,
        }
    )

def fixture_path(self, surface: str, request: Any) -> Path:
    digest = self.request_hash(surface, request)
    return self.root / surface / f"{digest}.json"
```

`_sha256` hashes the UTF-8 bytes of `_canonical_json`, which is:

```python
json.dumps(
    _jsonable(value),
    allow_nan=False,
    ensure_ascii=False,
    separators=(",", ":"),
    sort_keys=True,
)
```

Every flag there is deliberate. `sort_keys=True` makes dict key order irrelevant, so a refactor that
reorders a payload literal does not orphan a fixture. `separators=(",", ":")` removes whitespace
variance. `ensure_ascii=False` fixes one representation of non-ASCII text instead of leaving it to the
encoder's default. `allow_nan=False` turns `NaN` and `Infinity` into an error rather than a non-standard
JSON token that would round-trip differently.

`_jsonable` is the normalizer that runs before all of that. It handles pydantic `BaseModel` (via
`model_dump(mode="json")`), dataclasses, `Enum`, `Path`, mappings, and sequences, and it raises
`TypeError` on anything else: `"fixture values must be JSON-compatible, got {type}"`. Tuples become
lists, which means a tuple and a list with the same contents hash identically. That is a feature for
stability and a hazard if you ever need to distinguish them.

`pydantic` is not in `requirements.txt`, which lists only `fastapi`, `uvicorn[standard]`, and
`websockets`. It arrives transitively through FastAPI. Verified locally: a fresh venv from
`requirements.txt` gives pydantic 2.13.4.

I verified all three key properties directly against the real class, recording into a temp directory:

```
recorded ->              {'ok': True, 'n': 1}
replayed ->              {'n': 1, 'ok': True}
reordered key replays:   {'n': 1, 'ok': True}
MISS on changed request: fixture missing for demo_surface: .../demo_surface/<digest>.json
MISS on different surface: ok
```

#### Why the key is a hash and not a name

Four reasons, all visible in the code and its usage.

**Collision-free by construction.** The surfaces here take large, free-form payloads. `x_search_post`
sends a multi-sentence natural language prompt plus a JSON schema. `image_gen` sends a 402-character
prompt. There is no short human-readable name that distinguishes "the ai topic prompt" from "the ai topic
prompt with `broad=True`" without inventing a naming convention that someone will get wrong. SHA-256 over
the canonical request cannot get it wrong.

**Drift shows up as a miss, loudly.** Change one character of a prompt and the hash changes, the file is
not found, and `_replay` raises `FixtureMissError` with the exact recording instruction:
`"fixture missing for {surface}: {path}. Set ADJ_RECORD=1 to record it live."` A named fixture would keep
replaying a response that no longer corresponds to the request being made, which is the worst failure
mode a replay layer can have. Silence there means demoing a lie.

**Surface is in the hash, not just the path.** The hash envelope includes `"surface": surface`, so the
same request under a different surface name is a different key. Verified: `image_gen` and `other_surface`
over an identical request produce different digests. Without that, two surfaces sharing a payload shape
could shadow each other.

**`format_version` is in the hash too.** `FORMAT_VERSION = 1`. Bumping it invalidates every key at once,
which is the only sane way to change the envelope. Replay also checks it explicitly and raises
`FixtureCorruptError(f"fixture format version changed: {path}")`.

#### The on-disk shape

A fixture is one JSON object with exactly six keys, written sorted and indented:

```json
{
  "format_version": 1,
  "request": { "model": "grok-imagine-image", "n": 1, "prompt": "...", "response_format": "b64_json" },
  "request_sha256": "<the filename stem>",
  "response": { "data": [ { "b64_json": "...", "mime_type": "..." } ], "usage": { "cost_in_usd_ticks": 200000000 } },
  "response_sha256": "<sha256 of canonical response>",
  "surface": "image_gen"
}
```

The repo carries 21 fixtures across four surfaces: `decoy_write` (6), `image_gen` (1), `x_search_post`
(7), `x_search_replies` (7). `x_search_post` holds seven files for six topics because `_search_payloads`
builds a structured `json_schema` request and a plain fallback request, and `FixtureStore.call` writes
the fixture as soon as `invoke()` returns, before the caller parses. A failed structured attempt still
leaves a file.

#### Replay verifies five things before returning

`_replay` is deliberately paranoid, because a corrupted fixture that still deserializes is a demo that
goes wrong on stage:

```python
if not isinstance(document, dict) or not required.issubset(document):
    raise FixtureCorruptError(f"fixture has an invalid shape: {path}")
if document["format_version"] != FORMAT_VERSION:
    raise FixtureCorruptError(f"fixture format version changed: {path}")
if document["surface"] != surface or document["request"] != request:
    raise FixtureCorruptError(f"fixture request does not match its path: {path}")
if (
    document["request_sha256"] != expected_request_hash
    or path.stem != expected_request_hash
):
    raise FixtureCorruptError(f"fixture request hash does not match: {path}")
if document["response_sha256"] != _sha256(document["response"]):
    raise FixtureCorruptError(f"fixture response hash does not match: {path}")
```

The fourth check compares the recomputed hash against **both** the stored `request_sha256` and the
filename stem. A file that was renamed, or a file whose body was edited without renaming, both fail. The
fifth check is a plain integrity checksum on the response body. Verified: tampering with one integer
inside a recorded response makes the next replay raise `fixture response hash does not match`.

#### Two more guards worth knowing

**Secret rejection runs before anything is cached.** `_reject_secrets` walks the normalized request
recursively. It rejects on key name against `_SENSITIVE_KEYS` (`anthropic_api_key`, `api_key`,
`authorization`, `cookie`, `openai_api_key`, `password`, `secret`, `set_cookie`, `xai_api_key`, with `-`
normalized to `_` and lowercased), and on string value against `_SENSITIVE_VALUE_PREFIXES` (`"bearer "`,
`"xai-"`, `"sk-ant-"`, `"sk-proj-"`). Verified both paths:

```
SECRET (key):   credential field cannot be cached at request.authorization
SECRET (value): credential-like value cannot be cached at request.h.auth
```

This matters because `fixtures/api/` is committed to a public repo. In practice X Arcade passes the key
in a header and never in a payload, so the guard never fires. It is there so that a future refactor that
moves the key into the body fails immediately instead of publishing it.

**Writes are atomic.** `_write_atomic` creates a `NamedTemporaryFile` in the destination directory,
`json.dump`s with `ensure_ascii=True, indent=2, sort_keys=True`, flushes, `os.fsync`es, then
`os.replace`s into position. A killed record run cannot leave a half-written fixture that later replays
as truncated JSON.

**Surface names are validated.** `_SURFACE_PATTERN = re.compile(r"[a-z0-9][a-z0-9_.-]*\Z")`. A surface is
a directory name, so this is path hygiene as much as style.

### A real defect in the committed fixtures (verified)

I recomputed every committed fixture's hashes against the current `fixtures_core.py`. Twenty of 21
verify. One does not:
`fixtures/api/image_gen/28912dd48842f8c226ce850f41d08350df235b2034a47d098dadf054e8b324d8.json`.

Running the documented demo replay check reproduces the failure:

```
$ ARCADE_MODE=demo python3 services/card_forge.py
FixtureMissError: fixture missing for image_gen:
  .../fixtures/api/image_gen/fa2b2ff23456d0b3c46024110c42de313ee9bee38cd9158d3b7d4ba9b41ec11a.json
```

The cause is a public-repo name scrub that did a global find and replace of a personal name with the
literal `PLAYER1`, and it hit two things it should not have.

It rewrote `DEMO_WINNER` in `services/card_forge.py` and the matching `prompt` string inside the
fixture's `request`, but left `request_sha256` and the filename at their pre-scrub values. The key is a
hash of the request, so changing the request text moved the key. The file is now named after a request it
no longer contains.

It also rewrote four characters inside the base64 image payload. The committed card
`web/static-assets/cards/decoy-3f2710c0a9e6_demo.jpg` is 286348 bytes, which base64-encodes to exactly
381800 characters. The fixture's `b64_json` is 381803 characters, three too many. Diffing character by
character, the streams are identical for 127025 characters and then diverge:

```
committed card, re-encoded:  ...qp5aArunk+Q5GBz2zg5/Stq0+...
fixture b64_json:            ...qp5aPLAYER1k+Q5GBz2zg5/Stq0+...
```

The four-character run `Arun` appeared by chance in the base64 alphabet and was replaced with the
seven-character `PLAYER1`. `base64.b64decode` now raises `binascii.Error`.

**This does not affect the stage demo,** and knowing why is the whole point of the next section. The demo
never calls `card_forge`, because `_attach_live_card` runs only when `config.MODE == "live"`. The card the
demo shows is the committed jpg at `DEMO_CARD_URL`, which is intact. `integration_check.py` fetches it
and asserts a 200 with more than 10000 bytes, and that assertion passes. The blast radius is exactly one
thing: live-mode share card generation cannot reuse the recorded image, so a live run would re-render it
(or fail on a missing `XAI_API_KEY`).

The fix is to re-record it. Set `ARCADE_MODE=live ARCADE_RECORD=1` with a key present and run
`services/card_forge.py`, which will write a correctly named file. Deleting the stale one is a separate
step, since a new key means a new filename. Nothing in the current gate would have caught this, because
no check exercises the fixture layer.

### How the demo achieves zero network egress

The mechanism is not clever. It is that every artifact the demo needs was produced ahead of time and
committed.

| Demo needs | Where it comes from at runtime | Who produced it, offline |
|---|---|---|
| Round content (post, 4 real replies, 1 Grok reply, rationale) | `cartridges/decoy/rounds/*.json`, read by `cartridges/decoy/queue.py` | `round_builder.py --live --all` with `ARCADE_RECORD=1` |
| Host voice lines | `web/static-assets/host_*.mp3` | `services/voice_host.py` `render_host_lines()` at build time |
| Share card | `web/static-assets/cards/decoy-3f2710c0a9e6_demo.jpg` via `DEMO_CARD_URL` | `services/card_forge.py` in record mode |
| Realtime voice token | the offline stub from `/token` | not needed, host lines are pre-rendered |
| Safety verdict | `plugins/safety/screen.py`, recomputed at serve time | pure rule checks, no model calls, no network |

`cartridges/decoy/queue.py` is 50 lines of `glob`, `sorted`, and `json.loads` with a module-level index.
Its docstring states the timing reason directly: rounds "are pre-built by round_builder.py, never fetched
inline, because x_search latency is far too high for a request path." `CONTRACT.md` puts a number on it,
42s measured at build time, and `DEMO.md`'s numbers card cites `artifacts/probes/x_search.json` as the
path for that figure. The artifact records `latency_s: 42.24155116081238`.

One subtlety in the serve path. The `safety` block written into the round files on disk is
`{"screened": false, "gate_codes": []}` for all six, a stale stamp left by a builder import bug that
has since been fixed without the files being rebuilt. The server does
not trust that block. `_next_round` overwrites it with a fresh `screen_round` result before deciding.
Five of the six rounds pass. `decoy_ai.json` fails, which `DEMO.md` records as "5 of 6, ai round gated
out on G_SOURCE + G_URL." That is why `/health` reports `rounds_available: 5` for six files on disk, and
why the count is recomputed per request rather than cached.

### The socket guard in integration_check.py

Everything above is an argument. The guard turns it into a test.

```python
os.environ["ARCADE_MODE"] = "demo"
os.environ.pop("ARCADE_FORCE_FALLBACK", None)

# Any connection attempt that leaves loopback is an integration failure.
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_orig_connect = socket.socket.connect


def _guarded_connect(self: socket.socket, address: Any, *args: Any, **kwargs: Any) -> Any:
    host = address[0] if isinstance(address, tuple) else str(address)
    if host not in _LOOPBACK:
        raise AssertionError(f"network egress attempted to {host} in demo mode")
    return _orig_connect(self, address, *args, **kwargs)


socket.socket.connect = _guarded_connect  # type: ignore[method-assign]
```

Placement is the point. This block sits at lines 29 to 44, **before** `import uvicorn`,
`import websockets`, `import config`, and `from server.app import DEMO_CARD_URL, app` at lines 46 to 51.
Anything those modules do at import time is already inside the guard. If the store were constructed at
import and tried to reach out, the check would fail before a single assertion ran.

The address handling covers both shapes. AF_INET and AF_INET6 pass a tuple whose first element is the
host, so `address[0]` is right for TCP. A Unix domain socket passes a path string, which `str(address)`
turns into something that is not in `_LOOPBACK`, so it fails closed.

**What it proves, verified.** I wrapped `socket.socket.connect` with a logger *before* importing
`integration_check`, so the guard installed itself on top of my logger and every connect went guard,
logger, real syscall. Running the full check:

```
integration: ALL CHECKS PASSED (6 rounds played, zero network egress)
EXIT 0
CONNECT TARGETS SEEN: ["('127.0.0.1', 8788)"]
GUARD RAISED: network egress attempted to api.x.ai in demo mode
```

One connect target across the entire run, the check's own loopback server. And the guard does bite when
handed a real external host. That is a positive result and a negative control, not just a green
checkmark.

**What it does not prove.** Be precise about this, because the phrase "zero network egress" is printed in
the pass line and quoted in `DEMO.md`.

- It patches `socket.socket.connect` only. `socket.socket.connect_ex` is a separate method and is not
  wrapped. Nothing in the repo uses it, but a new dependency could.
- Name resolution is not covered. `socket.getaddrinfo` runs before `connect`, so a DNS query for an
  external hostname would leave the machine and the guard would fire only afterward. In practice the
  guard's message shows the hostname, so you would still learn about it.
- It is a same-process guard. A subprocess would have its own unpatched `socket` module.
- It proves the property for the paths the check walks. It exercises `/health`, `/token`, `/`, one mp3,
  the demo card, and a full websocket match through every servable round. It does not exercise
  `services/card_forge.py`, `services/voice_host.py`, or the live branch of `_attach_live_card`, because
  demo mode never reaches them. That is the correct scope, since demo mode is what the guard is asserting
  about. It is also why the corrupted `image_gen` fixture went unnoticed.

`server/selfcheck.py` has no equivalent guard. Do not cite it as offline proof.

### The verification surface: exact commands

`README.md` names two checks. Run both from the repo root, in this order, both must exit 0.

```sh
cd /Users/arunsharma/code/x-arcade-public
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

python server/selfcheck.py
python integration_check.py
```

Measured on one laptop with the repo's own `.venv`: `integration_check.py` 0.43, 0.46, and 0.43
seconds wall across three runs, `server/selfcheck.py` 2.54 seconds. Those are a handful of runs on one
machine, not a benchmark, and the absolute numbers will move. The ratio is the durable part, and it is
structural rather than noise.
Selfcheck's third round waits on the real server-side deadline, which it sets to 2 seconds. Integration
check never waits on a deadline, because both clients always guess.

#### python server/selfcheck.py

The narrow websocket contract check. It sets `ARCADE_FORCE_FALLBACK=1`, so the server skips the queue and
always serves `FALLBACK_ROUND` from `server/app.py`. It sets `config.ROUND_SECONDS = 2` so the deadline
round does not stall the script. It binds 127.0.0.1:8899.

Three scripted rounds, each with a distinct outcome:

1. P1 guesses wrong, P2 guesses the decoy. Asserts `winner == "P2"`, score 1, streak 1.
2. Both players guess wrong. Asserts `winner == "house"` and every streak resets to 0.
3. Nobody guesses. Asserts the server-side timer alone flips `phase` to `reveal` with winner `house`.

Its HTTP assertions are deliberately loose. `/health` accepts either `"demo"` or `"live"` for mode and
only requires the `rounds_available` key to be present. `/token` passes on 200 or 501.

Passing selfcheck says nothing about the round files or the safety gates, because `FORCE_FALLBACK` means
it never opens `cartridges/decoy/rounds/`.

#### python integration_check.py

The full gate. It binds 127.0.0.1:8788, room `ITG`, `config.ROUND_SECONDS = 15`.

It builds its own **answer key** first, independently of the server:

```python
for path in sorted(ROUNDS_DIR.glob("*.json")):
    rnd = json.loads(path.read_text(encoding="utf-8"))
    key[rnd["round_id"]] = rnd
    if screen_round(rnd)["screened"]:
        servable.append(rnd["round_id"])
    else:
        gated.append(rnd["round_id"])
```

Every later assertion is checked against that derived truth, not against the server's own output. That is
what makes it an integration proof rather than a tautology.

HTTP assertions, five of them:

- `/health` returns 200 with `mode == "demo"`.
- `/health` `rounds_available` equals the locally computed servable count.
- `/token` returns 200 with `demo is True`.
- `/` returns 200 and the body contains `b"DECOY"`.
- `/static-assets/host_intro.mp3` and `DEMO_CARD_URL` both return 200 with more than 10000 bytes.

Per-round websocket assertions, checked on every one of the six rounds played:

- the served `round_id` is in the answer key and is not in the gated list.
- `safety.screened is True`, which proves the server re-screened rather than trusting the file.
- no `decoy_slot` and no `decoy_rationale` on the round during guessing.
- no `is_decoy` and no `author` on any reply during guessing.
- exactly 5 replies.

Reveal assertions:

- one guess keeps `phase == "guessing"`, and the second flips it to `"reveal"`.
- `reveal.decoy_slot` equals the value read from the round file on disk.
- `reveal.winner == "P2"`, the first correct guesser.
- `reveal.rationale` is truthy.
- `reveal.share_card_url == DEMO_CARD_URL`.
- reveal restores `is_decoy` on at least one reply and `author` on all five.

Queue assertions: the script plays `len(servable) + 1` rounds so the cycle is proven to wrap. With 5
servable rounds that is 6 rounds and a final P2 score of 6. It asserts
`served_ids[:len(servable)] == servable` (order preserved) and `served_ids[len(servable)] == servable[0]`
(wrap). The score of 6 is not an off-by-one.

The trace lands at `artifacts/integration_trace.txt`, which is tracked in git. A passing run on
8 August 2026 reproduced the committed trace byte for byte, so `git status` stayed clean. If the trace
does come back
dirty after a passing run, something in the served content changed, and the diff tells you what.

One implementation note that will bite anyone adding an assertion. All HTTP goes through `http_get`,
which wraps `urllib` in `asyncio.to_thread`:

```python
async def http_get(path: str) -> tuple[int, bytes, str]:
    """Run the blocking urllib call in a worker thread.

    The server shares this process's event loop, so a synchronous request
    from the loop itself would deadlock the whole check.
    """
    return await asyncio.to_thread(_http_get_blocking, path)
```

uvicorn runs in-process on the same loop. A direct `urllib.request.urlopen` from the loop deadlocks the
entire check with no error message. Use the wrapper.

#### The DEMO.md preflight

The stage-side version of the gate, steps 3 and 4:

```sh
curl http://localhost:8787/health          # expect {"mode": "demo", "rounds_available": 5}
python integration_check.py                # expect ALL CHECKS PASSED + zero-network-egress line
```

`DEMO.md` says to run the integration check "once at home and once at the venue." The
`rounds_available: 5` figure is hardcoded in the runbook with the instruction "If the count is not 5,
stop and find out which round the gates changed their mind on." Adding, editing, or removing one round
file changes that count and breaks both the runbook step and the `/health` assertion, even when nothing
is actually broken.

Three ports are hardcoded and none of them is probed before binding: `run.sh` uses 8787,
`integration_check.py` uses 8788, `server/selfcheck.py` uses 8899. `deploy/huggingface/Dockerfile` uses
7860. A stale listener on any of them makes a healthy codebase look broken.

### The third mode: ?mock=1

`?mock=1` is a browser-only mode that replaces the WebSocket with an in-page emulation of the server. It
is the last rung of the stage fallback tree, reachable by opening `web/index.html?mock=1` straight from
disk with no server at all, and the badge reads `MOCK` so it can never be mistaken for a demo run. The
client chapter covers what it emulates and where it can drift. For the purposes of this chapter, two
facts matter: it is a second, independent implementation of the game contract, and no check script
exercises it.

### Changing this layer safely

- **Set env vars before the first `config` import.** `MODE` and `RECORD` are module-level. There is no
  re-read.
- **Adding a config value?** Put it in `config.py`. The docstring is a rule: "One place for every model
  id and mode switch. Nothing else hardcodes these."
- **Changing a model id, a prompt, or a request payload changes the fixture key.** Re-record, and delete
  the orphaned file, since the new key is a new filename.
- **Never edit a fixture file by hand.** `_replay` checks the request hash against the recomputed value
  *and* the filename stem, and the response hash against the stored response. The corrupted `image_gen`
  fixture is what hand editing looks like after the fact.
- **Changing the hash envelope means bumping `FORMAT_VERSION` and re-recording everything.** Replay
  checks the version explicitly.
- **Adding a round file changes `/health` `rounds_available`,** which breaks `DEMO.md` preflight step 3
  and the corresponding integration assertion. Update both.
- **New HTTP assertions in `integration_check.py` go through `http_get`,** never `urllib` directly.
- **Anything you add to the demo path must work with the network cable pulled.** `CONTRACT.md` states it
  plainly: "The demo must complete with the network cable pulled." Run `python integration_check.py` and
  confirm the pass line still ends with "zero network egress."

Two gaps in the gate, stated plainly because they are the places a change can slip through unnoticed.
Nothing exercises `fixtures_core.py`, which is how a corrupted fixture ships. And nothing exercises
`?mock=1`, which is how the browser-side reimplementation of the contract drifts.

---

## What is real and what is staged

This table exists so nobody on the team overclaims in the pitch. Every row is built from what the
chapters above actually verified against the repo. When two descriptions are both defensible, use the
narrower one.

| Piece | Status | What that means, precisely |
|---|---|---|
| Source posts and the four real replies | **Real, pulled live, then frozen** | Pulled from X through `x_search` at build time and committed as JSON. At demo time they are read off disk, not fetched. The repo docs carry no build date, so do not state one. |
| The imposter reply in each round | **Real Grok output, generated once** | Written by `grok-4.5` through `/chat/completions` at build time, committed. Not generated during play. |
| The decoy rationale shown at reveal | **Real, model self-reported** | The model was asked to name its own tell. That text is committed with the round. |
| Round ids, seeds, and slot order | **Real and deterministic** | Pure functions of the source post id. Replaying all six from fixtures offline, with no network, reproduces the committed files exactly, `safety` block included. |
| The grounding guard on search calls | **Real, and it fired** | The `food` topic has two committed post fixtures. The ungrounded one was recorded and then discarded by `_made_tool_calls`. |
| The five safety gates | **Real, running on every serve** | `screen_round` re-runs at load time on every round, including the hardcoded fallback. Fail closed, no override. |
| The `G_SLURS` denylist | **Illustrative placeholder** | Six mild insults. The comment in the code says a real deployment swaps in a maintained wordlist behind the same gate code. Passing `G_SLURS` is not evidence of moderation. |
| The gated `decoy_ai.json` round | **Real result, narrow reason** | Rejected on `G_SOURCE` (2650 chars against a 560 cap) and `G_URL` (a YouTube link in a reply). A formatting failure and a link, not toxicity. Do not fix the file. |
| The `safety` block inside round files | **Real, and it matches the screener** | Five read `{"screened": true, "gate_codes": []}` and `ai` reads `{"screened": false, "gate_codes": ["G_SOURCE", "G_URL"]}`. The stamps were regenerated after the builder import fix in `7a4e012`, so they now equal what `screen_round` returns. The server still re-screens and overwrites on every serve, so the on-disk value is advisory, never trusted. |
| Rooms, phases, scoring, and the round timer | **Real, live at request time** | In-memory rooms, one `asyncio` task per room, winner by server arrival order. Asserted by `server/selfcheck.py` and `integration_check.py`. |
| The anti-cheat strip | **Real, enforced server-side** | `_round_view` removes `decoy_slot`, `decoy_rationale`, `is_decoy`, and real author handles during guessing. Both check scripts assert it on every round. |
| Arena mode (room `GROK`) | **Real, untested by the gate** | Host-only advance and no auto-start are implemented. Neither check script exercises arena rooms. The client still shows a lit START button that the server ignores. |
| Host voice lines | **Pre-rendered, committed** | Five mp3s rendered once at build time. Not synthesized on stage. Only `host_intro` and `host_reveal` are wired into the client. The other three are unreferenced. |
| Realtime browser-direct voice | **Not built** | The token mint endpoint is real and was probed at 0.13s. No browser code exists. The subprotocol strings in `REALTIME_NOTES.md` are UNVERIFIED until a first live connect. |
| The `/token` endpoint in demo mode | **Deliberate offline stub** | Returns `{"demo": true, "value": "", ...}` with HTTP 200 and never touches the network. |
| The demo share card | **Pre-rendered, one image** | A single committed 1280x720 JPEG, served at every reveal in demo mode. The same picture every round. |
| Live share card generation | **Real code path, never exercised in demo** | `_attach_live_card` runs only when `ARCADE_MODE=live`. Demo mode never imports `card_forge`. |
| The committed `image_gen` fixture | **Corrupt in this public copy** | A name scrub moved the request hash and damaged the base64 payload. It cannot replay. Cause of the discrepancy between the public copy and the original is UNVERIFIED. |
| Post-back to X | **Staged, never sent** | `services/poster.py` imports no HTTP client, has no caller, no route, and no message type. It prints what would be sent and returns a fake permalink. |
| The `$0.015 per post create` figure | **Provider-quoted, not incurred** | Labeled unverified in the code comment itself. Never repeat it as a measurement. |
| Sponsored arenas (`plugins/ads/`) | **Illustrative, zero callers** | DemoBrand, OrbitCola, and PixelPeak are invented. `cpm_note` is the literal string `"ILLUSTRATIVE"`. No code path serves an arena, so "gates run before any arena is served" is UNVERIFIED as implemented. Use "gates run before any round is served," which is provable. |
| The zero-network-egress claim | **Real, with stated scope** | `integration_check.py` patches `socket.socket.connect` before every other import and passes with one loopback target. It does not cover `connect_ex`, name resolution, or subprocesses. |
| `artifacts/integration_trace.txt` | **A recording of a past run** | Committed output, not a live result. Re-run `python integration_check.py` before citing any number from it. |
| Probe latencies (42s search, 6.5s image, 1.78s TTS, 0.13s token) | **Real measurements, recorded once at build time** | Recorded under `artifacts/probes/` as `latency_s` values of 42.24, 6.50, 1.78, and 0.13. They are a recording, not a live number, and they carry no date. Note that `artifacts/probes/tts.json` carries a hand-added `note` field the committed probe script cannot produce, so the script has drifted from the version that made the artifact. |
| `?mock=1` | **Mock, browser-only** | A second implementation of the contract inside `web/game.js` with two hardcoded fixture rounds and a bot player. Not a security boundary. No check script touches it. Its share card URL points at a file that does not exist. |
| The committed QR code | **Real image, points at the hosted Space** | Decodes to `https://arun0808-x-arcade.hf.space/?room=GROK`, read out of the pixels: 41x41 modules, version 6, error level H. Regenerate it if you host elsewhere. `README.md:45-46` still calls it a localhost placeholder and is wrong. |
| A second cartridge (Crux) | **Unbuilt** | Described in prose in `paper/VISION.md`. The current safety gates hardcode the five-reply Decoy shape, so a differently shaped round cannot be served today. |
| Automated test coverage | **One integration script, no unit tests** | No `tests/` directory exists. `integration_check.py` and `server/selfcheck.py` are the entire automated gate. |
