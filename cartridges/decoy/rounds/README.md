# Decoy demo rounds

Six rounds built live with real xAI calls (`ARCADE_MODE=live ARCADE_RECORD=1`).
Each round holds 4 real replies read verbatim from the source thread plus 1 Grok-written
imposter, shuffled by a seed derived from the source post id. These files are the offline
demo content. `queue.py` serves them in sorted filename order, so the demo sequence is
ai, crypto, food, movies, music, sports.

| topic  | source post                                                  | file              | pulled       |
| ------ | ------------------------------------------------------------ | ----------------- | ------------ |
| ai     | https://x.com/Jeremybtc/status/2086174103225131136           | decoy_ai.json     | event day    |
| sports | https://x.com/DeadlineDayLive/status/2085793093144731845     | decoy_sports.json | earlier build |
| movies | https://x.com/DiscussingFilm/status/2086143411984208230      | decoy_movies.json | event day    |
| crypto | https://x.com/cryptorover/status/2086159645052215533         | decoy_crypto.json | event day    |
| food   | https://x.com/Suzierizzo1/status/2086181164620931494         | decoy_food.json   | event day    |
| music  | https://x.com/Rainmaker1973/status/2086110183114035484       | decoy_music.json  | event day    |

All six rounds pass the safety gates, so all six serve
(`artifacts/integration_trace.txt`).

Notes from the builds:

- sports is the one topic not refreshed on the event-day pull. Three consecutive live
  pulls all landed on the same divisive news story, and the third carried an embedded
  URL that `G_URL` rejected. The earlier clean transfer-news round was kept instead. Its
  fixture was reverted alongside it so offline replay still reproduces the committed file.
- movies and food were rebuilt once in an earlier session. The plain topic words pulled in
  a partisan rant for movies and a football team dinner for food, so those two topics carry
  richer search queries in `round_builder.TOPIC_QUERIES`.
- An earlier single-call fetch let the model invent plausible replies instead of reading
  the thread. The builder now finds the post and reads its replies in two separate
  grounded calls, and rejects any response that made no x_search call.

The safety gates are not a taste filter. They check source integrity, slurs, decoy count,
author fields, and URLs. They do not screen for a politically charged thread, which is why
the sports pull needed a human look. Read a fresh round before serving it.

Rebuild any topic with:

```
ARCADE_MODE=live ARCADE_RECORD=1 python3 cartridges/decoy/round_builder.py --live --topic ai
```

The matching API fixtures live under `fixtures/api/`, so `build_round(topic, live=False)`
reproduces every committed round offline, byte for byte.
