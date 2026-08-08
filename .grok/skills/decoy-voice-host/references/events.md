# Host voice events

Catalog of moments the client may send to `POST /agent/commentate`.
Edit this file when adding events; keep examples short enough to speak.

| event | phase | Intent | Example line |
|--------|--------|--------|----------------|
| `lobby_join` | lobby | Players arriving | "Two in the room. NEON and VOLT." |
| `round_start` | guessing | Board is live | "Round two. NEON leads with two." |
| `player_pick` | guessing | Local player clicked a card | "You locked reply three." |
| `player_lock` | guessing | Opponent locked | "VOLT locks in." |
| `clock_low` | guessing | ~10s left | "Ten seconds!" |
| `reveal` | reveal | Answer public — be funny | "NEON called it — decoy was reply two." |
| `next_round` | lobby/guessing | Optional advance beat | "Next round. Fresh board." |

## Observation fields

### Always (when available)
- `event`, `phase`, `round`, `deadline_ms`
- `standings[]`: rank, name, score, streak
- `listener`: local player name
- `just_locked[]`, `picker`, `pick_reply` (1–5 card index)

### Reveal only (public)
- `decoy_slot` (0–4), `decoy_reply` (1–5)
- `decoy_rationale` / `rationale`
- `replies[]`: slot, text, author, is_decoy
- `winner`, `correct`

### Never pre-reveal
- decoy identity, full reply texts as judgment fodder, answer spoilers

## Delivery rules (client)

1. Play the matching host mp3 first when the event has one (`intro` / `round` / `reveal` / `win` / `lose`).
2. Fire commentate **async** with a ~1.8s abort; never block START or scoring.
3. If phase/round advanced before the model returns, **drop** the line.
4. On local pick or NEXT, **bump** the voice queue so old audio cuts immediately.
