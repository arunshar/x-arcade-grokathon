"use strict";
// X Arcade / Decoy client. The server owns all game truth. This file only
// renders the state messages defined in CONTRACT.md and sends the three
// client messages: join, guess, next. Add ?mock=1 to the URL to drive the
// whole flow from a built-in fake state sequence with no server at all.

const $ = (id) => document.getElementById(id);
const MOCK = new URLSearchParams(location.search).get("mock") === "1";
const RING_LEN = 326.7; // circumference of the r=52 timer circle

let sock = null;
let myName = "";
let myRoom = "";
let joined = false;
let state = null;
let prevPhase = null;
let roundNo = 0;
let lastRoundId = null;
let myGuessSlot = null;
let guessStartAt = 0;
let timerEndAt = 0;
let timerRaf = 0;
let muted = false;
let audioUnlocked = false;
let arcadeMode = "demo";
let voiceModel = "grok-voice-think-fast-2.0";
let firstRoundOfSession = true;

// Scripted host lines — keep in sync with services/voice_host.py LINES.
// Live realtime forces these exact strings; mp3s were rendered from the same text.
const HOST_LINES = {
  intro: "Welcome to the arcade. Tonight, one of the players at this cabinet is not a player at all.",
  round: "Four humans. One machine. Thirty seconds.",
  reveal: "Hands off the buttons. The decoy was...",
  win: "Got it! The machine never stood a chance.",
  lose: "Wrong! The machine walks free. House wins.",
};

// ---------- audio (mp3 always available; live voice is best-effort) ----------
function makeSound(src) {
  const a = new Audio(src);
  a.preload = "auto";
  a.dataset.ok = "maybe";
  a.addEventListener("error", () => { a.dataset.ok = "no"; });
  return a;
}
const sounds = {
  intro: makeSound("static-assets/host_intro.mp3"),
  round: makeSound("static-assets/host_round.mp3"),
  reveal: makeSound("static-assets/host_reveal.mp3"),
  win: makeSound("static-assets/host_win.mp3"),
  lose: makeSound("static-assets/host_lose.mp3"),
};

function unlockAudio() {
  if (audioUnlocked) return;
  audioUnlocked = true;
  for (const a of Object.values(sounds)) {
    try {
      a.muted = true;
      const p = a.play();
      if (p && p.then) p.then(() => { a.pause(); a.currentTime = 0; a.muted = false; })
        .catch(() => { a.muted = false; });
    } catch (e) { /* autoplay blocked, a later gesture retries */ }
  }
  // Resume WebAudio if live voice already warmed a suspended context.
  if (pcmPlayer.ctx && pcmPlayer.ctx.state === "suspended") {
    pcmPlayer.ctx.resume().catch(() => {});
  }
  // Warm the realtime host after a user gesture (required for autoplay + WS).
  if (arcadeMode === "live" && !MOCK) warmLiveVoice();
}
document.addEventListener("pointerdown", unlockAudio, { once: true });

function playSound(name) {
  const a = sounds[name];
  if (!a || muted || a.dataset.ok === "no") return;
  try {
    a.currentTime = 0;
    const p = a.play();
    if (p && p.catch) p.catch(() => {});
  } catch (e) { /* never let audio break the game */ }
}

// Live Grok voice: mint ephemeral token from our server, open realtime WS,
// force scripted lines. Any failure silently drops to the mp3 rung.
const pcmPlayer = {
  ctx: null,
  nextTime: 0,
  sampleRate: 24000,
  ensure() {
    if (!this.ctx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return false;
      this.ctx = new AC({ sampleRate: this.sampleRate });
      this.nextTime = this.ctx.currentTime;
    }
    if (this.ctx.state === "suspended") this.ctx.resume().catch(() => {});
    return true;
  },
  resetClock() {
    if (this.ctx) this.nextTime = this.ctx.currentTime;
  },
  pushBase64Pcm16(b64) {
    if (!b64 || !this.ensure()) return;
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const samples = new Int16Array(bytes.buffer, bytes.byteOffset, bytes.byteLength >> 1);
    const f32 = new Float32Array(samples.length);
    for (let i = 0; i < samples.length; i++) f32[i] = samples[i] / 32768;
    const buf = this.ctx.createBuffer(1, f32.length, this.sampleRate);
    buf.copyToChannel(f32, 0);
    const src = this.ctx.createBufferSource();
    src.buffer = buf;
    src.connect(this.ctx.destination);
    const start = Math.max(this.ctx.currentTime + 0.02, this.nextTime);
    src.start(start);
    this.nextTime = start + buf.duration;
  },
};

const liveVoice = {
  ws: null,
  ready: false,
  disabled: false, // sticky: after hard failure, stay on mp3s
  connecting: null,
  pending: null, // { resolve, reject, timer }
};

function disableLiveVoice(reason) {
  liveVoice.disabled = true;
  liveVoice.ready = false;
  if (liveVoice.ws) {
    try { liveVoice.ws.close(); } catch (e) { /* ignore */ }
    liveVoice.ws = null;
  }
  if (liveVoice.pending) {
    clearTimeout(liveVoice.pending.timer);
    const p = liveVoice.pending;
    liveVoice.pending = null;
    p.reject(new Error(reason || "live voice disabled"));
  }
}

async function mintVoiceToken() {
  const r = await fetch("/token");
  if (!r.ok) throw new Error("token http " + r.status);
  const j = await r.json();
  if (j.demo || !j.value) throw new Error(j.detail || "no token");
  if (j.model) voiceModel = j.model;
  return j.value;
}

function attachLiveVoiceHandlers(ws) {
  ws.addEventListener("message", (ev) => {
    let msg = null;
    try {
      if (typeof ev.data === "string") msg = JSON.parse(ev.data);
      else return; // binary frames unused; we expect base64 deltas in JSON
    } catch (e) { return; }
    if (!msg || !msg.type) return;

    if (msg.type === "error" || msg.type === "response.failed") {
      if (liveVoice.pending) {
        clearTimeout(liveVoice.pending.timer);
        const p = liveVoice.pending;
        liveVoice.pending = null;
        p.reject(new Error(msg.error?.message || msg.type));
      }
      return;
    }

    // Audio deltas — tolerate a few event name variants across API revisions.
    const delta =
      msg.delta ||
      msg.audio ||
      (msg.type && msg.type.indexOf("audio.delta") !== -1 ? msg.delta : null);
    if (
      msg.type === "response.output_audio.delta" ||
      msg.type === "response.audio.delta" ||
      msg.type === "response.output_audio_transcript.delta"
    ) {
      // Only play raw pcm audio deltas, not transcript text.
      if (msg.type.indexOf("transcript") === -1 && (msg.delta || msg.audio)) {
        pcmPlayer.pushBase64Pcm16(msg.delta || msg.audio);
      }
      return;
    }
    if (delta && msg.type && msg.type.indexOf("audio") !== -1 && msg.type.indexOf("transcript") === -1) {
      pcmPlayer.pushBase64Pcm16(delta);
      return;
    }

    if (
      msg.type === "response.done" ||
      msg.type === "response.output_audio.done" ||
      msg.type === "response.audio.done"
    ) {
      if (liveVoice.pending && msg.type === "response.done") {
        clearTimeout(liveVoice.pending.timer);
        const p = liveVoice.pending;
        liveVoice.pending = null;
        // Let the last PCM buffer finish playing before resolving.
        const waitMs = pcmPlayer.ctx
          ? Math.max(0, (pcmPlayer.nextTime - pcmPlayer.ctx.currentTime) * 1000) + 80
          : 80;
        setTimeout(() => p.resolve(), waitMs);
      }
    }
  });

  ws.addEventListener("close", () => {
    liveVoice.ready = false;
    liveVoice.ws = null;
    if (liveVoice.pending) {
      clearTimeout(liveVoice.pending.timer);
      const p = liveVoice.pending;
      liveVoice.pending = null;
      p.reject(new Error("voice socket closed"));
    }
  });

  ws.addEventListener("error", () => {
    // close handler cleans pending; mark not ready so next cue falls back.
    liveVoice.ready = false;
  });
}

function warmLiveVoice() {
  if (MOCK || liveVoice.disabled || arcadeMode !== "live") return liveVoice.connecting || Promise.resolve(false);
  if (liveVoice.ready && liveVoice.ws && liveVoice.ws.readyState === WebSocket.OPEN) {
    return Promise.resolve(true);
  }
  if (liveVoice.connecting) return liveVoice.connecting;

  liveVoice.connecting = (async () => {
    try {
      const value = await mintVoiceToken();
      const model = encodeURIComponent(voiceModel);
      const ws = new WebSocket(
        `wss://api.x.ai/v1/realtime?model=${model}`,
        [
          "realtime",
          "openai-insecure-api-key." + value,
          "openai-beta.realtime-v1",
        ]
      );
      await new Promise((resolve, reject) => {
        const t = setTimeout(() => reject(new Error("voice connect timeout")), 8000);
        ws.addEventListener("open", () => { clearTimeout(t); resolve(); });
        ws.addEventListener("error", () => { clearTimeout(t); reject(new Error("voice connect error")); });
      });
      attachLiveVoiceHandlers(ws);
      ws.send(JSON.stringify({
        type: "session.update",
        session: {
          voice: "Eve",
          instructions:
            "You are the Decoy arcade host. Short lines, high energy, never reveal the decoy slot.",
          // Scripted cues only — no mic VAD chatter during the duel.
          turn_detection: null,
          input_audio_format: "pcm16",
          output_audio_format: "pcm16",
        },
      }));
      liveVoice.ws = ws;
      liveVoice.ready = true;
      pcmPlayer.ensure();
      return true;
    } catch (e) {
      disableLiveVoice(String(e && e.message ? e.message : e));
      return false;
    } finally {
      liveVoice.connecting = null;
    }
  })();
  return liveVoice.connecting;
}

function speakLive(lineKey) {
  return new Promise((resolve, reject) => {
    if (!liveVoice.ready || !liveVoice.ws || liveVoice.ws.readyState !== WebSocket.OPEN) {
      reject(new Error("voice not ready"));
      return;
    }
    if (liveVoice.pending) {
      clearTimeout(liveVoice.pending.timer);
      const prev = liveVoice.pending;
      liveVoice.pending = null;
      prev.reject(new Error("superseded"));
    }
    const text = HOST_LINES[lineKey];
    if (!text) {
      reject(new Error("unknown line"));
      return;
    }
    pcmPlayer.resetClock();
    const timer = setTimeout(() => {
      if (liveVoice.pending) {
        liveVoice.pending = null;
        reject(new Error("speak timeout"));
      }
    }, 12000);
    liveVoice.pending = { resolve, reject, timer };
    try {
      // Cancel anything in flight so the cue is not queued behind a riff.
      liveVoice.ws.send(JSON.stringify({ type: "response.cancel" }));
      liveVoice.ws.send(JSON.stringify({
        type: "response.create",
        response: {
          modalities: ["audio", "text"],
          instructions: 'Say exactly this, nothing more: "' + text + '"',
        },
      }));
    } catch (e) {
      clearTimeout(timer);
      liveVoice.pending = null;
      reject(e);
    }
  });
}

/** Play a host cue: live voice if available, else committed mp3. Never throws. */
function playHost(name, andThen) {
  if (muted) {
    if (andThen) andThen();
    return;
  }
  const finish = () => { if (andThen) andThen(); };
  const fallback = () => { playSound(name); finish(); };

  if (arcadeMode !== "live" || liveVoice.disabled || MOCK) {
    fallback();
    return;
  }

  const run = async () => {
    const ok = await warmLiveVoice();
    if (!ok) {
      fallback();
      return;
    }
    try {
      await speakLive(name);
      finish();
    } catch (e) {
      // One soft failure → mp3 this cue. Repeated connect failures disable live.
      playSound(name);
      finish();
    }
  };
  run();
}

function setMuted(v) {
  muted = v;
  $("muteBtn").textContent = muted ? "SND OFF" : "SND ON";
  $("muteBtn").classList.toggle("off", muted);
  try { localStorage.setItem("arcade_muted", muted ? "1" : "0"); } catch (e) {}
  if (muted && liveVoice.ws) {
    try { liveVoice.ws.send(JSON.stringify({ type: "response.cancel" })); } catch (e) {}
  }
}
try { setMuted(localStorage.getItem("arcade_muted") === "1"); } catch (e) { setMuted(false); }
$("muteBtn").addEventListener("click", () => setMuted(!muted));

// ---------- health badge + mode detection ----------
if (MOCK) {
  $("demoBadge").textContent = "MOCK";
  $("demoBadge").hidden = false;
} else {
  fetch("/health").then((r) => r.json()).then((j) => {
    if (!j) return;
    if (j.mode) arcadeMode = j.mode;
    if (j.voice_model) voiceModel = j.voice_model;
    if (j.mode === "demo" || j.demo === true) {
      $("demoBadge").hidden = false;
    } else if (j.mode === "live") {
      $("demoBadge").textContent = "LIVE";
      $("demoBadge").hidden = false;
    }
  }).catch(() => {});
}

// ---------- transport ----------
function send(obj) {
  try { sock.send(JSON.stringify(obj)); } catch (e) { /* retry loop reconnects */ }
}

function handleRaw(text) {
  let msg = null;
  try { msg = JSON.parse(text); } catch (e) { return; }
  if (msg && msg.t === "state") handleState(msg);
}

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

function setConn(text) { $("connLine").textContent = text; }

// ---------- state handling ----------
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
  if (s.phase === "guessing" && was !== "guessing") {
    // First duel of the session gets the full welcome; later rounds get the short cue.
    if (firstRoundOfSession) {
      firstRoundOfSession = false;
      playHost("intro", () => playHost("round"));
    } else {
      playHost("round");
    }
  }
  if (s.phase === "reveal" && was !== "reveal") {
    const winner = s.reveal && s.reveal.winner;
    const outcome = (!winner || winner === "house") ? "lose" : "win";
    playHost("reveal", () => playHost(outcome));
  }
  prevPhase = was;
  render(s);
}

// ---------- rendering ----------
function render(s) {
  $("roundCounter").textContent = "RND " + String(roundNo).padStart(2, "0");
  const screened = !!(s.round && s.round.safety && s.round.safety.screened);
  $("safetyChip").hidden = !screened;

  const inLobby = s.phase === "lobby";
  $("screen-lobby").hidden = !inLobby;
  $("screen-game").hidden = inLobby;
  if (inLobby) renderLobby(s); else renderGame(s);
}

function renderLobby(s) {
  const ul = $("lobbyPlayers");
  ul.innerHTML = "";
  const players = s.players || [];
  if (players.length === 0) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "NO PLAYERS YET";
    ul.appendChild(li);
  }
  for (const p of players) {
    const li = document.createElement("li");
    const who = document.createElement("span");
    who.textContent = p.name + (p.name === myName ? " (YOU)" : "");
    const sc = document.createElement("span");
    sc.textContent = "SCORE " + (p.score || 0);
    li.append(who, sc);
    ul.appendChild(li);
  }
  $("startBtn").disabled = !(joined && players.length >= 1);
}

function renderGame(s) {
  const r = s.round || {};
  const src = r.source || {};
  $("postAuthor").textContent = src.post_author || "@unknown";
  $("postAvatar").textContent = (src.post_author || "?").replace("@", "").charAt(0) || "?";
  $("postTopic").textContent = src.topic || "";
  $("postText").textContent = src.post_text || "";
  $("timerWrap").style.visibility = s.phase === "guessing" ? "visible" : "hidden";

  renderOpponents(s);
  renderReplies(s);
  renderReveal(s);
}

function renderOpponents(s) {
  const strip = $("oppStrip");
  strip.innerHTML = "";
  if (s.phase !== "guessing") return;
  for (const p of s.players || []) {
    if (p.name === myName) continue;
    const el = document.createElement("span");
    el.className = "opp" + (p.guessed ? " locked" : "");
    el.textContent = p.name + (p.guessed ? " LOCKED IN" : " PICKING...");
    strip.appendChild(el);
  }
}

function renderReplies(s) {
  const grid = $("replies");
  grid.innerHTML = "";
  const r = s.round || {};
  const reveal = s.reveal;
  const canTap = s.phase === "guessing" && myGuessSlot === null && !myGuessConfirmed(s);

  for (const reply of r.replies || []) {
    const card = document.createElement("div");
    card.className = "card";
    card.dataset.slot = reply.slot;

    const inner = document.createElement("div");
    inner.className = "card-inner";
    const front = document.createElement("div");
    front.className = "card-face card-front";

    const tag = document.createElement("span");
    tag.className = "slot-tag";
    tag.textContent = "REPLY " + (reply.slot + 1);
    front.appendChild(tag);

    const isDecoy = reveal && reveal.decoy_slot === reply.slot;
    if (isDecoy) {
      const badge = document.createElement("span");
      badge.className = "robot-badge";
      badge.textContent = "ROBOT";
      front.appendChild(badge);
    }

    const text = document.createElement("p");
    text.className = "reply-text";
    text.textContent = reply.text || "";
    front.appendChild(text);

    const author = document.createElement("span");
    author.className = "reply-author";
    author.textContent = s.phase === "reveal" && reply.author && !isDecoy ? reply.author : "@·····";
    if (isDecoy) author.textContent = "grok wrote this one";
    front.appendChild(author);

    if (isDecoy && reveal.rationale) {
      const why = document.createElement("p");
      why.className = "rationale";
      why.textContent = reveal.rationale;
      front.appendChild(why);
    }
    if (s.phase === "reveal" && myGuessSlot === reply.slot) {
      const pick = document.createElement("span");
      pick.className = "pick-tag";
      pick.textContent = "YOUR PICK";
      front.appendChild(pick);
    }

    const back = document.createElement("div");
    back.className = "card-face card-back";
    const lock = document.createElement("span");
    lock.className = "lock-label";
    lock.textContent = "LOCKED IN";
    back.appendChild(lock);

    inner.append(front, back);
    card.appendChild(inner);

    if (s.phase === "reveal") {
      card.classList.add(isDecoy ? "is-decoy" : "is-real");
      if (myGuessSlot === reply.slot) card.classList.add("my-pick");
    } else if (myGuessSlot === reply.slot) {
      card.classList.add("locked");
    } else if (canTap) {
      card.classList.add("tappable");
      front.addEventListener("click", () => onGuess(reply.slot, card));
    }
    grid.appendChild(card);
  }
}

function myGuessConfirmed(s) {
  const me = (s.players || []).find((p) => p.name === myName);
  return !!(me && me.guessed);
}

function renderReveal(s) {
  const panel = $("revealPanel");
  if (s.phase !== "reveal" || !s.reveal) { panel.hidden = true; return; }
  panel.hidden = false;

  const banner = $("winnerBanner");
  const w = s.reveal.winner;
  if (!w || w === "house") {
    banner.textContent = "THE HOUSE WINS";
    banner.classList.add("house");
  } else {
    banner.textContent = w === myName ? "YOU CALLED IT" : w.toUpperCase() + " WINS";
    banner.classList.remove("house");
  }

  const strip = $("scoreStrip");
  strip.innerHTML = "";
  // Prefer the server-computed leaderboard (top 5, arena-ready). Fall back to
  // the raw player list for older state shapes.
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

  const img = $("shareCard");
  if (s.reveal.share_card_url) {
    img.src = s.reveal.share_card_url;
    img.hidden = false;
    img.onerror = () => { img.hidden = true; };
  } else {
    img.hidden = true;
  }
}

// ---------- timer (display only, server enforces the real deadline) ----------
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
function stopTimer() { cancelAnimationFrame(timerRaf); $("timerWrap").classList.remove("low"); }

// ---------- actions ----------
function onGuess(slot, card) {
  if (!state || state.phase !== "guessing" || myGuessSlot !== null) return;
  myGuessSlot = slot;
  const ms = Math.round(performance.now() - guessStartAt);
  card.classList.add("locked");
  send({ t: "guess", room: myRoom, slot, ms });
}

// A scanned QR lands here with ?room=GROK: prefill the room so a phone joins
// with one tap, and show the QR block on the big screen (no ?room param) so
// the audience can scan it off the projector.
const PREFILL_ROOM = (new URLSearchParams(location.search).get("room") || "").toUpperCase();
if (PREFILL_ROOM) {
  $("roomInput").value = PREFILL_ROOM;
} else {
  try { $("lobbyQr").hidden = false; } catch (e) { /* qr block optional */ }
}

$("joinBtn").addEventListener("click", () => {
  const name = $("nameInput").value.trim().toUpperCase();
  const room = $("roomInput").value.trim().toUpperCase();
  if (!name || !room) { setConn("NAME AND ROOM CODE REQUIRED"); return; }
  myName = name;
  myRoom = room;
  joined = true;
  $("nameInput").disabled = true;
  $("roomInput").disabled = true;
  $("joinBtn").disabled = true;
  send({ t: "join", room: myRoom, name: myName });
});

// The contract has no separate start message. "next" from the lobby kicks
// off the first round, the same way it advances rounds after a reveal.
$("startBtn").addEventListener("click", () => send({ t: "next", room: myRoom }));
$("nextBtn").addEventListener("click", () => send({ t: "next", room: myRoom }));

// ---------- mock mode: the last-ditch stage fallback ----------
// Emulates just enough server: join adds you plus a bot, next starts a round,
// a guess on the decoy slot wins, both wrong means the house wins. All content
// below is fixture data for the mock. Round A's source post is the real post
// captured by the x_search probe.
function mockSocket(onMessage) {
  const ROUNDS = [
    {
      round_id: "decoy-mockaaa00001",
      source: {
        post_text: "Paul W.S. Anderson joins the Higgsfield Global Film Festival as a jury member.\n\nDirector of the billion-dollar Resident Evil franchise, Mortal Kombat, and Alien vs. Predator.",
        post_author: "@higgsfield_ai",
        post_url: "https://x.com/higgsfield_ai/status/2085772302130753606",
        topic: "film",
      },
      replies: [
        { slot: 0, text: "no way the AvP guy is judging ai films now. cinema is healing or dying, cant tell", author: "@reelgrader" },
        { slot: 1, text: "A fitting choice. His work bridging games and film mirrors what this festival attempts with AI and cinema, and the jury benefits from that perspective.", author: "decoy" },
        { slot: 2, text: "mortal kombat 1995 still goes harder than anything released this year and i will not be taking questions", author: "@vhs_never_died" },
        { slot: 3, text: "ok but who's on the jury that has actually shipped an ai film lol", author: "@promptcinema" },
        { slot: 4, text: "Event horizon mentioned?? no?? fine ill mention it. best worst movie ever made", author: "@lowbudgethorror" },
      ],
      decoy_slot: 1,
      decoy_rationale: "Too balanced. It praises, summarizes, and lands a tidy closing clause. Nobody replying to film news footnotes their own enthusiasm.",
      safety: { screened: true, gate_codes: [] },
      seed: 4102394,
    },
    {
      round_id: "decoy-mockbbb00002",
      source: {
        post_text: "our new model writes replies so human that our own eval team cant tell anymore. shipping anyway",
        post_author: "@arcade_fixture",
        post_url: "https://x.com/arcade_fixture/status/0",
        topic: "ai",
      },
      replies: [
        { slot: 0, text: "shipping anyway is the most honest thing an ai lab has ever said", author: "@gpu_poor" },
        { slot: 1, text: "cant tell or wont tell. big difference when the bonus depends on it", author: "@evals_anon" },
        { slot: 2, text: "This raises important questions about disclosure. If evaluators cannot distinguish model output, users deserve to know when they are reading one.", author: "decoy" },
        { slot: 3, text: "my replies are also indistinguishable from a bot but thats a me problem", author: "@postingthrulife" },
        { slot: 4, text: "day 400 of asking for the eval set to be public", author: "@benchmarkwatch" },
      ],
      decoy_slot: 2,
      decoy_rationale: "Opens with 'This raises important questions' and speaks for users in the abstract. Real repliers dunk first and moralize never.",
      safety: { screened: true, gate_codes: [] },
      seed: 9174520,
    },
  ];

  let phase = "lobby";
  let players = [];
  let ri = -1;
  let roundStart = 0;
  let reveal = null;
  let deadlineTimer = 0;
  let botTimer = 0;
  const emit = (obj) => setTimeout(() => onMessage(JSON.stringify(obj)), 40);

  function publicRound() {
    if (ri < 0) return null;
    const r = JSON.parse(JSON.stringify(ROUNDS[ri % ROUNDS.length]));
    if (phase === "guessing") {
      delete r.decoy_slot;
      delete r.decoy_rationale;
      for (const rep of r.replies) { delete rep.is_decoy; delete rep.author; }
    }
    return r;
  }
  function push() {
    const left = phase === "guessing" ? Math.max(0, 30000 - (Date.now() - roundStart)) : 0;
    emit({ t: "state", room: myRoom || "MOCK", phase, players, round: publicRound(), reveal, deadline_ms: left });
  }
  function currentRound() { return ROUNDS[ri % ROUNDS.length]; }

  function startRound() {
    ri += 1;
    phase = "guessing";
    reveal = null;
    roundStart = Date.now();
    for (const p of players) p.guessed = false;
    clearTimeout(deadlineTimer);
    clearTimeout(botTimer);
    deadlineTimer = setTimeout(() => doReveal(null), 30000);
    botTimer = setTimeout(() => {
      const bot = players.find((p) => p.name === "GLITCH");
      if (bot && phase === "guessing") { bot.guessed = true; push(); }
    }, 5200 + Math.random() * 2000);
    push();
  }

  function doReveal(winner) {
    if (phase !== "guessing") return;
    phase = "reveal";
    clearTimeout(deadlineTimer);
    clearTimeout(botTimer);
    const r = currentRound();
    if (winner) {
      const p = players.find((x) => x.name === winner);
      if (p) p.score = (p.score || 0) + 1;
    }
    reveal = {
      decoy_slot: r.decoy_slot,
      rationale: r.decoy_rationale,
      winner: winner || "house",
      share_card_url: "static-assets/share_card.png",
    };
    push();
  }

  return {
    send(text) {
      const m = JSON.parse(text);
      if (m.t === "join") {
        players.push({ name: m.name, score: 0, guessed: false });
        push();
        setTimeout(() => {
          if (!players.find((p) => p.name === "GLITCH")) {
            players.push({ name: "GLITCH", score: 0, guessed: false });
            push();
          }
        }, 900);
      } else if (m.t === "next") {
        startRound();
      } else if (m.t === "guess" && phase === "guessing") {
        const p = players.find((x) => x.name === myName);
        if (p && !p.guessed) {
          p.guessed = true;
          if (m.slot === currentRound().decoy_slot) doReveal(myName);
          else if (players.every((x) => x.guessed)) doReveal(null);
          else { push(); setTimeout(() => doReveal(null), 2200); }
        }
      }
    },
  };
}

// ---------- boot ----------
connect();
