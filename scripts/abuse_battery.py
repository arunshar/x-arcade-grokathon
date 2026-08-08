"""Abuse battery: everything a room full of phones can throw at the server.

Every test asserts two things: the specific behavior, and that the server is
still alive afterwards. A crash anywhere fails the whole battery.
"""
import asyncio
import os
import json
import urllib.request

import websockets

BASE = os.environ.get("ARCADE_CHECK_BASE", "http://127.0.0.1:8803")
WS = BASE.replace("http", "ws", 1) + "/ws"
PASS, FAIL = [], []


def check(ok, label):
    (PASS if ok else FAIL).append(label)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


def alive():
    try:
        with urllib.request.urlopen(BASE + "/health", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


async def state(ws, timeout=8):
    return json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))


async def drain(ws, pred, timeout=15):
    last = None
    loop = asyncio.get_event_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        try:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        except asyncio.TimeoutError:
            break
        last = m
        if pred(m):
            return m
    return last


async def main():
    print("== 1. malformed frames ==")
    ws = await websockets.connect(WS)
    for junk in [b"\x00\x01binary", "not json", "42", '"str"', "[]", "{}",
                 '{"t":"join"}', '{"room":"X"}', '{"t":"wat","room":"X"}',
                 '{"t":"join","room":"","name":"A"}']:
        await ws.send(junk)
    await ws.send(json.dumps({"t": "join", "room": "AB1", "name": "PROBE"}))
    s = await state(ws)
    check(s["phase"] == "lobby", "10 junk frames ignored, real join still works")
    await ws.close()
    check(alive(), "server alive after malformed frames")

    print("== 2. hostile field values ==")
    ws = await websockets.connect(WS)
    await ws.send(json.dumps({"t": "join", "room": "R" * 5000, "name": "N" * 100000}))
    s = await state(ws)
    huge_ok = s.get("phase") == "lobby"
    print(f"     (server accepted {len(s['players'][0]['name'])}-char name, "
          f"{len(s['room'])}-char room)")
    check(huge_ok, "megabyte-scale name/room does not crash (see cap finding)")
    await ws.close()

    ws = await websockets.connect(WS)
    await ws.send(json.dumps({"t": "join", "room": "XSS1",
                              "name": "<img src=x onerror=alert(1)>"}))
    s = await state(ws)
    check(s["players"][0]["name"].startswith("<img"),
          "html-in-name stored verbatim (client must render via textContent)")
    await ws.close()
    check(alive(), "server alive after hostile fields")

    print("== 3. guess abuse ==")
    ws = await websockets.connect(WS)
    await ws.send(json.dumps({"t": "join", "room": "GA1", "name": "G1"}))
    await state(ws)
    # guess in lobby must be ignored
    await ws.send(json.dumps({"t": "guess", "room": "GA1", "slot": 0, "ms": 1}))
    await ws.send(json.dumps({"t": "next", "room": "GA1"}))
    s = await drain(ws, lambda m: m.get("phase") == "guessing")
    check(s["phase"] == "guessing", "guess-in-lobby ignored, round starts")
    for slot in [-1, 5, 99, "2", 2.5, True, None, [2]]:
        await ws.send(json.dumps({"t": "guess", "room": "GA1", "slot": slot, "ms": 1}))
    s2 = await drain(ws, lambda m: m.get("phase") == "reveal", timeout=3)
    still_guessing = not (s2 and s2.get("phase") == "reveal")
    check(still_guessing, "8 invalid slots all rejected, round still open")
    await ws.send(json.dumps({"t": "guess", "room": "GA1", "slot": 2, "ms": "huge"}))
    s3 = await drain(ws, lambda m: m.get("phase") == "reveal")
    check(s3 and s3["phase"] == "reveal", "valid guess with junk ms reveals fine")
    # double guess after reveal
    await ws.send(json.dumps({"t": "guess", "room": "GA1", "slot": 3, "ms": 1}))
    await ws.close()
    check(alive(), "server alive after guess abuse")

    print("== 4. next-spam and timer race ==")
    ws = await websockets.connect(WS)
    await ws.send(json.dumps({"t": "join", "room": "NS1", "name": "SPAM"}))
    await state(ws)
    for _ in range(25):
        await ws.send(json.dumps({"t": "next", "room": "NS1"}))
    s = await drain(ws, lambda m: m.get("phase") == "guessing")
    check(s and s["phase"] == "guessing", "25x next-spam lands in exactly one round")
    rid1 = s["round"]["round_id"]
    # spamming next during guessing must not skip the round
    for _ in range(10):
        await ws.send(json.dumps({"t": "next", "room": "NS1"}))
    s2 = await drain(ws, lambda m: False, timeout=2) or s
    check(s2.get("round", {}).get("round_id", rid1) == rid1,
          "next during guessing does not skip the round")
    await ws.close()
    check(alive(), "server alive after next-spam")

    print("== 5. disconnect edge: leaver completes the guess set ==")
    a = await websockets.connect(WS)
    b = await websockets.connect(WS)
    await a.send(json.dumps({"t": "join", "room": "DC1", "name": "STAY"}))
    await state(a)
    await b.send(json.dumps({"t": "join", "room": "DC1", "name": "LEAVE"}))
    await drain(a, lambda m: len(m.get("players", [])) == 2)
    await a.send(json.dumps({"t": "next", "room": "DC1"}))
    s = await drain(a, lambda m: m.get("phase") == "guessing")
    await a.send(json.dumps({"t": "guess", "room": "DC1", "slot": 1, "ms": 5}))
    await drain(a, lambda m: False, timeout=1)
    await b.close()  # the non-guesser leaves; STAY has guessed -> reveal must fire
    s = await drain(a, lambda m: m.get("phase") == "reveal", timeout=10)
    check(s and s["phase"] == "reveal", "leaver mid-round triggers reveal for the rest")
    await a.close()
    check(alive(), "server alive after disconnect edge")

    print("== 6. crowd: 30 players in one room ==")
    socks = []
    for i in range(30):
        w = await websockets.connect(WS)
        await w.send(json.dumps({"t": "join", "room": "CROWD", "name": f"P{i:02d}"}))
        socks.append(w)
    s = await drain(socks[0], lambda m: len(m.get("players", [])) == 30, timeout=20)
    check(s and len(s["players"]) == 30, "30 players joined and broadcast")
    await socks[0].send(json.dumps({"t": "next", "room": "CROWD"}))
    s = await drain(socks[0], lambda m: m.get("phase") == "guessing", timeout=15)
    check(s and s["phase"] == "guessing", "30-player round starts")
    for i, w in enumerate(socks):
        await w.send(json.dumps({"t": "guess", "room": "CROWD", "slot": i % 5, "ms": i}))
    s = await drain(socks[0], lambda m: m.get("phase") == "reveal", timeout=20)
    check(s and s["phase"] == "reveal", "30 guesses reach reveal")
    if s and s["phase"] == "reveal":
        with_slot = sum(1 for p in s["players"] if isinstance(p.get("guess_slot"), int))
        check(with_slot == 30, "all 30 picks attributed at reveal")
    for w in socks:
        await w.close()
    check(alive(), "server alive after 30-player room")

    print("== 7. HTTP endpoints in demo mode ==")
    def http(path, data=None, method=None):
        req = urllib.request.Request(
            BASE + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    for path, want in [("/health", 200), ("/voices", None), ("/token", 200),
                       ("/join-info?room=ZZ", 200), ("/qr.png?room=ZZ", 200),
                       ("/qr.png?room=" + "Q" * 500, 422),
                       ("/static-assets/host_intro.mp3", 200),
                       ("/static-assets/reply-gifs/angry.gif", 200),
                       ("/nope-not-here", 404)]:
        code, _ = http(path)
        ok = (code == want) if want else (code in (200, 501, 503))
        check(ok, f"GET {path[:40]} -> {code}")

    code, body = http("/agent/commentate", data={"event": "reveal", "winner": "X"})
    check(code in (200, 501, 503), f"POST /agent/commentate demo-mode -> {code}")
    code, _ = http("/agent/commentate", data=None, method="POST")
    check(code in (400, 422), f"POST /agent/commentate empty body -> {code}")
    code, _ = http("/tts", data={"text": "hello"})
    check(code == 503, f"POST /tts demo-mode gated -> {code}")
    check(alive(), "server alive after endpoint battery")

    print(f"\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
    return len(FAIL)


raise SystemExit(asyncio.run(main()))
