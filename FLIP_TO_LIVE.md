# Flipping the Space to live mode for a demo slot

Demo mode needs no secrets and is the default. Live mode makes the Space mint
real voice tokens and generate a fresh Imagine card at every reveal. While it
is live, `/token` mints for anyone who has the URL and each reveal costs about
two cents, so treat live as a window you open and close, not a state you leave.

One more reason the order below matters: rooms live in server memory, and the
flip restarts the server. **Flip first, create your demo room after.** Flipping
mid-game destroys every active room.

## Open the window (about T minus 10 minutes)

1. Space settings: https://huggingface.co/spaces/Arun0808/x-arcade/settings
   under Variables and secrets:
   - New secret: name `XAI_API_KEY`, value from the laptop keychain. Never
     paste it anywhere else.
   - New variable: name `ARCADE_MODE`, value `live`.
   Saving triggers a restart on its own. Expect 15 to 90 seconds.

2. Wait for it, then verify from any terminal:

   ```bash
   bash scripts/space_mode_check.sh live && echo READY || echo NOT READY
   ```

   READY means the Space reports `mode=live` and actually minted a voice token,
   which proves the key works end to end. If it prints BROKEN, the mode flipped
   but the key is missing or mistyped; fix the secret and restart.

3. Open the bare URL (no `?room=`), tap CREATE ROOM, enter, put the QR up.

## What changes while live

- The host voice warms a realtime session after your first tap. If it fails
  for any reason the five pre-rendered mp3s still play; voice can never block
  a round.
- Each reveal fires a real Imagine card asynchronously. It lands a beat after
  the reveal broadcast and never delays the timer.
- The token endpoint returns real ephemeral tokens instead of the demo stub.

## Close the window (right after the slot)

1. Same settings page: delete the `XAI_API_KEY` secret, and delete the
   `ARCADE_MODE` variable (the Dockerfile default is demo). Restart happens
   on its own.
2. Verify:

   ```bash
   bash scripts/space_mode_check.sh demo && echo CLOSED || echo STILL LIVE
   ```

## If the Space hangs

Symptom seen today: HF says RUNNING but HTTP times out. Do not redeploy.
Space settings, Restart Space. It came back in about 15 seconds.
