# Decoy demo rounds

Six rounds built live with real xAI calls (`ARCADE_MODE=live ARCADE_RECORD=1`).
Each round holds 4 real replies read verbatim from the source thread plus 1 Grok-written
imposter, shuffled by a seed derived from the source post id. These files are the offline
demo content. `queue.py` serves them in sorted filename order, so the demo sequence is
ai, crypto, food, movies, music, sports.

| topic  | source post                                                  | file              |
| ------ | ------------------------------------------------------------ | ----------------- |
| ai     | https://x.com/deedydas/status/2085642431723446579            | decoy_ai.json     |
| sports | https://x.com/DeadlineDayLive/status/2085793093144731845     | decoy_sports.json |
| movies | https://x.com/Variety/status/2085779253677707374             | decoy_movies.json |
| crypto | https://x.com/Cointelegraph/status/2085793340247671084       | decoy_crypto.json |
| food   | https://x.com/miles_commodore/status/2085787763488502009     | decoy_food.json   |
| music  | https://x.com/peegzy1/status/2085306680955334992             | decoy_music.json  |

All six topic builds succeeded. The safety screen later rejected the ai round on
G_SOURCE and G_URL, so five of six rounds serve (`artifacts/integration_trace.txt`).
Two notes from the build:

- movies and food were rebuilt once. The plain topic words pulled in a partisan rant for
  movies and a football team dinner for food, so those two topics carry richer search
  queries in `round_builder.TOPIC_QUERIES`.
- An earlier single-call fetch let the model invent plausible replies instead of reading
  the thread. The builder now finds the post and reads its replies in two separate
  grounded calls, and rejects any response that made no x_search call.

Rebuild any topic with:

```
ARCADE_MODE=live ARCADE_RECORD=1 python3 cartridges/decoy/round_builder.py --live --topic ai
```

The matching API fixtures live under `fixtures/api/`, so `build_round(topic, live=False)`
reproduces every committed round offline, byte for byte.
