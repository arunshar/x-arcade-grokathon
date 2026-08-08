# Safety screen: fail closed

Every Decoy round passes five deterministic gates before it can reach a player.
The gates are plain rule checks. They run instantly, call no model, and give the same answer every time.
G_SOURCE checks the post and all five replies are present, nonempty, and within length bounds.
G_SLURS scans the post and every reply against a small denylist.
G_DECOY_COUNT checks exactly one reply is the decoy and the round points at that slot.
G_AUTHOR checks every real reply carries a real author handle, never the decoy marker.
G_URL checks no reply text contains a URL, because links break the game visually.
The rule is fail closed. A round that fails any gate is never served, and malformed input counts as a failure.
There is no override and no partial pass. The round builder must produce a clean round instead.
