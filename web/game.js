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
let ttsVoice = "eve"; // Grok speaker id — from /health or ARCADE_VOICE
let firstRoundOfSession = true;

// Scripted host lines — keep in sync with services/voice_host.py LINES.
// Live realtime forces these exact strings; mp3s were rendered from the same text.
// win/lose mp3s are legacy; reveal outcomes now use varied Grok Voice lines.
const HOST_LINES = {
  intro: "Welcome to the arcade. Tonight, one of the players at this cabinet is not a player at all.",
  round: "Four humans. One machine. Thirty seconds.",
  reveal: "Hands off the buttons. The decoy was...",
  win: "Point locked. The machine never stood a chance.",
  lose: "House cashes. The machine walks free.",
};

// ---------- audio (mp3 always available; live voice is best-effort) ----------
// Absolute URLs so playback works no matter the page path.
const HOST_MP3 = {
  intro: "static-assets/host_intro.mp3",
  round: "static-assets/host_round.mp3",
  reveal: "static-assets/host_reveal.mp3",
  win: "static-assets/host_win.mp3",
  lose: "static-assets/host_lose.mp3",
};
function hostMp3Url(name) {
  const rel = HOST_MP3[name];
  if (!rel) return "";
  try { return new URL(rel, location.href).href; } catch (e) { return rel; }
}
function makeSound(src) {
  const a = new Audio(src);
  a.preload = "auto";
  a.dataset.ok = "maybe";
  a.addEventListener("error", () => { a.dataset.ok = "no"; });
  return a;
}
const sounds = {
  intro: makeSound(HOST_MP3.intro),
  round: makeSound(HOST_MP3.round),
  reveal: makeSound(HOST_MP3.reveal),
  win: makeSound(HOST_MP3.win),
  lose: makeSound(HOST_MP3.lose),
};

function unlockAudio() {
  // May be called on every major click — only the unlock work runs once,
  // but we always try to resume contexts (browsers suspend aggressively).
  if (!audioUnlocked) {
    audioUnlocked = true;
    for (const a of Object.values(sounds)) {
      try {
        a.muted = true;
        const p = a.play();
        if (p && p.then) {
          p.then(() => { a.pause(); a.currentTime = 0; a.muted = false; })
            .catch(() => { a.muted = false; });
        } else {
          a.muted = false;
        }
      } catch (e) { /* autoplay blocked */ }
    }
  }
  if (pcmPlayer.ctx && pcmPlayer.ctx.state === "suspended") {
    pcmPlayer.ctx.resume().catch(() => {});
  }
  // Prime a silent Audio element so later voiceBus.playUrl is allowed.
  try {
    if (!voiceBus.el) voiceBus.el = new Audio();
    const silent = voiceBus.el;
    silent.muted = true;
    const sp = silent.play();
    if (sp && sp.then) {
      sp.then(() => { silent.pause(); silent.muted = false; }).catch(() => { silent.muted = false; });
    }
  } catch (e) { /* ignore */ }
  if (arcadeMode === "live" && !MOCK) warmLiveVoice();
}
// Unlock on first gesture anywhere (and again on lobby buttons below).
document.addEventListener("pointerdown", unlockAudio, { once: true });

function playSound(name) {
  if (muted) return;
  unlockAudio();
  const url = hostMp3Url(name);
  if (!url) return;
  try {
    const a = new Audio(url);
    a.play().catch(() => {});
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
          voice: ttsVoice || "eve",
          instructions:
            "You are the Decoy arcade commentator. Short hype lines only. " +
            "Never mention which reply is fake, decoy, robot, or the answer. " +
            "You may talk about scores, leaders, locks, and winners after reveal.",
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

/** Speak exact text over live realtime voice. */
function speakLiveText(text) {
  return new Promise((resolve, reject) => {
    if (!liveVoice.ready || !liveVoice.ws || liveVoice.ws.readyState !== WebSocket.OPEN) {
      reject(new Error("voice not ready"));
      return;
    }
    const line = String(text || "").trim();
    if (!line) {
      resolve();
      return;
    }
    if (liveVoice.pending) {
      clearTimeout(liveVoice.pending.timer);
      const prev = liveVoice.pending;
      liveVoice.pending = null;
      prev.reject(new Error("superseded"));
    }
    // Escape quotes so the force-instruction stays one string.
    const safe = line.replace(/\\/g, "/").replace(/"/g, "'");
    pcmPlayer.resetClock();
    const timer = setTimeout(() => {
      if (liveVoice.pending) {
        liveVoice.pending = null;
        reject(new Error("speak timeout"));
      }
    }, 14000);
    liveVoice.pending = { resolve, reject, timer };
    try {
      liveVoice.ws.send(JSON.stringify({ type: "response.cancel" }));
      liveVoice.ws.send(JSON.stringify({
        type: "response.create",
        response: {
          modalities: ["audio", "text"],
          instructions: 'Say exactly this, nothing more: "' + safe + '"',
        },
      }));
    } catch (e) {
      clearTimeout(timer);
      liveVoice.pending = null;
      reject(e);
    }
  });
}

function speakLive(lineKey) {
  const text = HOST_LINES[lineKey];
  if (!text) return Promise.reject(new Error("unknown line"));
  return speakLiveText(text);
}

/** Browser TTS last-resort fallback (not Grok). */
function speakBrowser(text) {
  return new Promise((resolve) => {
    try {
      if (!window.speechSynthesis) {
        resolve();
        return;
      }
      window.speechSynthesis.cancel();
      const u = new SpeechSynthesisUtterance(String(text || ""));
      u.rate = 1.06;
      u.pitch = 1.0;
      u.onend = () => resolve();
      u.onerror = () => resolve();
      window.speechSynthesis.speak(u);
    } catch (e) {
      resolve();
    }
  });
}

/**
 * One audio channel for the whole client. Stops any previous clip so Grok
 * lines never stack on top of each other.
 */
const voiceBus = {
  el: null,
  url: null,
  /** Bumps on every stop/play so a late fetch cannot start a second clip. */
  playGen: 0,
  speaking: false,
  stop() {
    this.playGen += 1;
    this.speaking = false;
    try {
      if (this.el) {
        this.el.onended = null;
        this.el.onerror = null;
        this.el.pause();
        this.el.removeAttribute("src");
        this.el.load();
      }
    } catch (e) { /* ignore */ }
    if (this.url) {
      try { URL.revokeObjectURL(this.url); } catch (e) { /* ignore */ }
      this.url = null;
    }
    if (window.speechSynthesis) {
      try { window.speechSynthesis.cancel(); } catch (e) { /* ignore */ }
    }
    // Cancel in-flight realtime utterance.
    if (liveVoice.ws && liveVoice.ws.readyState === WebSocket.OPEN) {
      try { liveVoice.ws.send(JSON.stringify({ type: "response.cancel" })); } catch (e) { /* ignore */ }
    }
    if (liveVoice.pending) {
      clearTimeout(liveVoice.pending.timer);
      const p = liveVoice.pending;
      liveVoice.pending = null;
      try { p.reject(new Error("cancelled")); } catch (e) { /* ignore */ }
    }
  },
  /** True while this playGen is still the active one (not stopped/superseded). */
  isCurrent(gen) {
    return typeof gen === "number" && gen === this.playGen;
  },
  playUrl(url) {
    return new Promise((resolve, reject) => {
      // stop() advances playGen — capture the id for THIS play only.
      this.stop();
      const gen = this.playGen;
      this.speaking = true;
      this.url = url;
      if (!this.el) this.el = new Audio();
      const a = this.el;
      const finish = (err) => {
        if (this.playGen !== gen) {
          resolve();
          return;
        }
        this.speaking = false;
        if (err) reject(err);
        else resolve();
      };
      a.onended = () => finish(null);
      a.onerror = () => finish(new Error("audio element failed"));
      a.src = url;
      a.preload = "auto";
      const p = a.play();
      if (p && p.catch) {
        p.catch((err) => finish(err || new Error("play failed")));
      }
    });
  },
};

/**
 * Serial voice queue with epoch cancel.
 * When the game moves on (new round / phase), bump() hard-cuts audio and
 * drops every pending job from the old moment so lines never lag behind.
 * Only one job runs at a time — Grok Voice never overlaps itself.
 */
const voiceQueue = {
  items: [],
  running: false,
  epoch: 0,
  /** Hard cut: stop current clip and wipe backlog (call on phase/round change). */
  bump() {
    this.epoch += 1;
    this.items = [];
    voiceBus.stop();
    this.running = false;
  },
  clear() {
    this.bump();
  },
  /** True if this epoch is still the live one (jobs re-check before play). */
  isLive(epoch) {
    return typeof epoch === "number" && epoch === this.epoch;
  },
  /**
   * @param {() => Promise<void>|void} job
   * @param {{ phase?: string, roundId?: string|null, priority?: number, epoch?: number }} [meta]
   */
  enqueue(job, meta) {
    const m = Object.assign({ epoch: this.epoch, phase: null, roundId: null }, meta || {});
    // If caller stamped an older epoch, drop immediately.
    if (typeof m.epoch === "number" && m.epoch !== this.epoch) return;
    m.epoch = this.epoch;
    this.items.push({ run: job, meta: m });
    // One line ahead max — backlog is what makes voice feel late/stacked.
    while (this.items.length > 2) this.items.shift();
    this.kick();
  },
  _stale(meta) {
    if (!meta) return true;
    if (meta.epoch !== this.epoch) return true;
    if (!state) return true;
    if (meta.phase && state.phase && meta.phase !== state.phase) return true;
    if (state.phase === "results" && meta.phase && meta.phase !== "results") return true;
    const liveId = state.round && state.round.round_id;
    if (meta.roundId && liveId && meta.roundId !== liveId) return true;
    return false;
  },
  kick() {
    if (this.running) return;
    // Drop anything that belongs to a past moment.
    while (this.items.length && this._stale(this.items[0].meta)) {
      this.items.shift();
    }
    if (!this.items.length) return;
    this.running = true;
    const item = this.items.shift();
    const ep = item.meta.epoch;
    Promise.resolve()
      .then(() => {
        if (ep !== this.epoch || this._stale(item.meta)) return;
        return item.run();
      })
      .catch(() => {})
      .then(() => {
        // Only the job that owns the current run flag may clear it. If bump()
        // already reset running mid-flight, do not stomp a newer job's kick.
        if (ep === this.epoch) {
          this.running = false;
          this.kick();
        }
      });
  },
};

function voiceMeta(extra) {
  const roundId = state && state.round && state.round.round_id ? state.round.round_id : null;
  const phase = state && state.phase ? state.phase : null;
  return Object.assign({ phase: phase, roundId: roundId }, extra || {});
}

/**
 * Drop model prompt-echo / instruction dumps before anything hits TTS.
 * Server also cleans; this is the last client guard so Grok Voice never
 * reads "You are the live commentator…" out loud.
 */
function sanitizeHostLine(text) {
  let raw = String(text || "").trim();
  if (!raw) return "";
  // Never speak multi-line skill/JSON dumps.
  if (raw.indexOf("\n") >= 0) {
    const parts = raw.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
    raw = parts.find((p) => !looksLikePromptEcho(p)) || "";
  }
  raw = raw.replace(/^["'“‘]|["'”’]$/g, "").trim();
  raw = raw.replace(
    /^(line|host|commentator|commentary|announcer|output|response|answer)\s*[:\-–—]\s*/i,
    ""
  ).trim();
  if (!raw || looksLikePromptEcho(raw)) return "";
  if (raw.length > 180) {
    const cut = raw.slice(0, 180);
    const sp = cut.lastIndexOf(" ");
    raw = (sp > 40 ? cut.slice(0, sp) : cut).trim();
  }
  return raw;
}

function looksLikePromptEcho(text) {
  const s = String(text || "").trim();
  if (!s || s.length < 2) return true;
  if (s.length > 280) return true;
  if (/^[\{\[`#]/.test(s)) return true;
  if (s.indexOf('"event"') >= 0 && (s.indexOf('"phase"') >= 0 || s.indexOf('"standings"') >= 0)) {
    return true;
  }
  const meta = /you are the live commentator|output only the (spoken )?line|write the next commentator|pre-?reveal safety|do not introduce yourself|one short punchy sentence|no hashtags,? no emojis|recent_lines|observation json|reply with only the spoken|sports-desk arcade|never name the decoy|pick_reply \(1-5\)|##\s*(style|variety)/i;
  if (meta.test(s)) return true;
  if ((s.match(/\s-\s/g) || []).length >= 2) return true;
  if (s.split(/\s+/).length > 40) return true;
  return false;
}

/**
 * Fetch Grok TTS bytes → object URL. Does not play. Caller must revoke.
 * Returns null when muted / empty / failed.
 */
function fetchGrokTtsUrl(text) {
  return (async () => {
    const line = sanitizeHostLine(text);
    if (!line || muted) return null;
    if (arcadeMode !== "live" || MOCK) return null;
    const r = await fetch("/tts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: line }),
    });
    if (!r.ok) {
      const detail = await r.text().catch(() => "");
      throw new Error("grok tts " + r.status + " " + detail.slice(0, 120));
    }
    const blob = await r.blob();
    return URL.createObjectURL(blob);
  })();
}

/**
 * Grok Voice TTS via our server (POST /tts → Eve mp3).
 * Works whenever ARCADE_MODE=live + XAI_API_KEY, no realtime socket required.
 * Re-checks voiceQueue epoch after the network hop so a late reply never plays
 * over the next round's line.
 */
function speakGrokTts(text, opts) {
  return (async () => {
    opts = opts || {};
    const epoch = typeof opts.epoch === "number" ? opts.epoch : voiceQueue.epoch;
    const line = sanitizeHostLine(text);
    if (!line) return;
    if (!voiceQueue.isLive(epoch) || muted) return;
    let url = null;
    try {
      url = await fetchGrokTtsUrl(line);
    } catch (e) {
      throw e;
    }
    // Stale after fetch — drop silently (round already moved on).
    if (!url || !voiceQueue.isLive(epoch) || muted) {
      if (url) {
        try { URL.revokeObjectURL(url); } catch (e) { /* ignore */ }
      }
      return;
    }
    try {
      await voiceBus.playUrl(url);
    } finally {
      try { URL.revokeObjectURL(url); } catch (e) { /* ignore */ }
      if (voiceBus.url === url) voiceBus.url = null;
    }
  })();
}

/** Prefer Grok Voice TTS (stable) then realtime, then browser. Never overlaps. */
async function speakWithGrok(text, opts) {
  opts = opts || {};
  const epoch = typeof opts.epoch === "number" ? opts.epoch : voiceQueue.epoch;
  const line = sanitizeHostLine(text);
  if (!line || muted) return;
  if (!voiceQueue.isLive(epoch)) return;
  // Prefer /tts — one clip at a time on voiceBus. Realtime is easy to stack.
  if (arcadeMode === "live" && !MOCK) {
    try {
      await speakGrokTts(line, { epoch: epoch });
      return "tts";
    } catch (e) { /* try realtime */ }
    if (!voiceQueue.isLive(epoch) || muted) return;
    if (!liveVoice.disabled) {
      try {
        const ok = await warmLiveVoice();
        if (!voiceQueue.isLive(epoch) || muted) return;
        if (ok) {
          // Hard-cut any prior realtime audio before starting a new line.
          voiceBus.stop();
          if (!voiceQueue.isLive(epoch) || muted) return;
          await speakLiveText(line);
          return "realtime";
        }
      } catch (e2) { /* browser */ }
    }
  }
  if (!voiceQueue.isLive(epoch) || muted) return;
  voiceBus.stop();
  if (!voiceQueue.isLive(epoch) || muted) return;
  await speakBrowser(line);
  return "browser";
}

function playMp3Cue(name) {
  return new Promise((resolve) => {
    if (muted) {
      resolve();
      return;
    }
    unlockAudio();
    const url = hostMp3Url(name);
    if (!url) {
      resolve();
      return;
    }
    // Route through voiceBus so mp3 + Grok TTS share one channel (no overlap).
    const clip = new Audio(url);
    clip.preload = "auto";
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      voiceBus.speaking = false;
      resolve();
    };
    // stop() advances playGen; this clip owns the bus until end/error/bump.
    voiceBus.stop();
    const gen = voiceBus.playGen;
    voiceBus.speaking = true;
    voiceBus.el = clip;
    clip.onended = finish;
    clip.onerror = finish;
    const watchdog = setTimeout(finish, 8000);
    const clearWd = () => clearTimeout(watchdog);
    clip.addEventListener("ended", clearWd, { once: true });
    clip.addEventListener("error", clearWd, { once: true });
    // If bump() already moved on, don't start.
    if (voiceBus.playGen !== gen) {
      finish();
      return;
    }
    const p = clip.play();
    if (p && p.then) {
      p.then(() => { /* playing */ }).catch(() => {
        finish();
      });
    }
  });
}

/**
 * Hard host stinger: the five committed mp3s are the guaranteed path.
 * Never waits on the agent or live TTS. Agent color is separate and async.
 */
function playHost(name, andThen) {
  if (muted) {
    if (andThen) andThen();
    return;
  }
  unlockAudio();
  // Capture meta AFTER unlock; tag with current phase/round.
  const meta = voiceMeta();
  // Stamp epoch now so a concurrent bump doesn't false-stale us incorrectly
  // if we already belong to the new epoch.
  meta.epoch = voiceQueue.epoch;
  voiceQueue.enqueue(async () => {
    if (muted || voiceQueue._stale(meta)) return;
    await playMp3Cue(name);
  }, meta);
  if (andThen) {
    voiceQueue.enqueue(async () => {
      if (!voiceQueue._stale(meta)) andThen();
    }, meta);
  }
}

function setMuted(v) {
  muted = v;
  $("muteBtn").textContent = muted ? "SND OFF" : "SND ON";
  $("muteBtn").classList.toggle("off", muted);
  try { localStorage.setItem("arcade_muted", muted ? "1" : "0"); } catch (e) {}
  if (muted) {
    voiceQueue.clear();
    if (typeof commentary !== "undefined" && commentary.clearQueue) commentary.clearQueue();
  }
}

// ---------- host AGENT + Grok Voice ----------
// Observe safe state → POST /agent/commentate (Grok decides the line) → speak
// with Grok Voice. Templates are fallback only. Never sends decoy secrets.
const commentary = {
  enabled: true,
  hostOnly: true,
  useAgent: true,
  lastLeader: null,
  lastGuessed: {},
  lowClockSaid: false,
  lastLobbyCount: 0,
  inFlight: 0,
  dropPending: false,
  // Generation token — bumped when the game advances so late agent replies are dropped.
  gen: 0,
  activePhase: null,
  activeRoundId: null,
  // Last spoken lines — fed back to the agent so openers don't repeat.
  recentLines: [],
  rememberLine(line) {
    const t = String(line || "").trim();
    if (!t) return;
    this.recentLines.push(t);
    if (this.recentLines.length > 10) this.recentLines.shift();
  },

  setEnabled(v) {
    this.enabled = !!v;
    const btn = $("commBtn");
    if (btn) {
      btn.textContent = this.enabled ? "COMM ON" : "COMM OFF";
      btn.title = this.enabled
        ? "Host agent + Grok Voice (cuts when the round moves on)"
        : "Commentator off";
      btn.classList.toggle("off", !this.enabled);
    }
    try { localStorage.setItem("arcade_comm", this.enabled ? "1" : "0"); } catch (e) {}
    if (!this.enabled) this.clearQueue();
  },

  clearQueue() {
    this.dropPending = true;
    voiceQueue.bump();
    // Keep gen in lockstep with the audio epoch so late TTS cannot speak.
    this.gen = voiceQueue.epoch;
  },

  /** Game advanced — invalidate in-flight agent calls and old speech.
   *  Caller should voiceQueue.bump() first; we sync gen to that epoch. */
  onAdvance(phase, roundId) {
    this.dropPending = false;
    this.gen = voiceQueue.epoch;
    this.activePhase = phase || null;
    this.activeRoundId = roundId || null;
    this.lowClockSaid = false;
    // Don't carry lock-memory across rounds/phases.
    if (phase !== "guessing") this.lastGuessed = {};
  },

  canSpeak() {
    if (muted || !this.enabled) return false;
    // Solo practice always gets commentary on this machine.
    if (typeof myRoom === "string" && isSoloFriendlyRoom(myRoom)) return true;
    if (this.hostOnly && !iAmHost && !MOCK) return false;
    return true;
  },

  /** Events allowed to speak in each phase (blocks mistimed lines). */
  eventAllowed(event, phase) {
    const p = phase || (state && state.phase) || "";
    const map = {
      lobby: { lobby_join: 1 },
      guessing: {
        round_start: 1,
        player_lock: 1,
        player_pick: 1,
        clock_low: 1,
      },
      reveal: { reveal: 1 },
      results: {},
    };
    const allow = map[p];
    if (!allow) return false;
    return !!allow[event];
  },

  stillCurrent(gen, phase, roundId) {
    if (gen !== this.gen) return false;
    if (gen !== voiceQueue.epoch) return false;
    if (this.dropPending) return false;
    if (!state) return false;
    if (phase && state.phase && phase !== state.phase) return false;
    // No speech while parked on results.
    if (state.phase === "results") return false;
    const liveId = state.round && state.round.round_id;
    if (roundId && liveId && roundId !== liveId) return false;
    // Guessing lines must die once the round has a reveal payload (race).
    if (phase === "guessing" && state.phase === "guessing" && state.reveal) {
      return false;
    }
    return true;
  },

  /**
   * Phase-aware observation.
   * Pre-reveal: spoiler-free. Reveal: decoy is public — include it for funny lines.
   */
  safeObservation(event, s, extra) {
    const board = getStandings(s).map((p) => ({
      rank: p.rank,
      name: p.name,
      score: p.score || 0,
      streak: p.streak || 0,
    }));
    const phase = (s && s.phase) || "";
    const topic = (s && s.round && s.round.source && s.round.source.topic) || null;
    const obs = {
      event: event,
      phase: phase,
      round: roundNo || null,
      deadline_ms: typeof s.deadline_ms === "number" ? s.deadline_ms : null,
      standings: board,
      listener: myName || null,
      recent_lines: this.recentLines.slice(-8),
      topic: topic,
    };
    if (extra && typeof extra === "object") {
      if (extra.just_locked) obs.just_locked = extra.just_locked;
      if (extra.winner != null) obs.winner = extra.winner;
      if (typeof extra.pick_reply === "number") obs.pick_reply = extra.pick_reply;
      if (extra.picker) obs.picker = extra.picker;
      if (extra.correct != null) obs.correct = !!extra.correct;
    }
    // Reveal only: host may see the answer (already on screen).
    if (phase === "reveal" || event === "reveal") {
      const rev = (s && s.reveal) || {};
      if (typeof rev.decoy_slot === "number") obs.decoy_slot = rev.decoy_slot;
      if (rev.rationale) obs.rationale = rev.rationale;
      if (extra && typeof extra.decoy_slot === "number") obs.decoy_slot = extra.decoy_slot;
      if (extra && extra.rationale) obs.rationale = extra.rationale;
      const replies = s && s.round && s.round.replies;
      if (Array.isArray(replies)) {
        obs.replies = replies.map((r) => ({
          slot: r.slot,
          text: r.text || "",
          author: r.author || "",
          is_decoy: !!r.is_decoy,
        }));
      }
    }
    return obs;
  },

  // Client-side ceiling so a slow model never blocks the round path.
  AGENT_TIMEOUT_MS: 1800,

  async askAgent(event, s, extra) {
    const obs = this.safeObservation(event, s, extra);
    const localFallback = this.templateLine(event, s, extra);
    if (!this.useAgent || MOCK) return { line: localFallback, source: "fallback_local" };
    const ctrl = (typeof AbortController !== "undefined") ? new AbortController() : null;
    const timer = setTimeout(() => { try { ctrl && ctrl.abort(); } catch (e) { /* ignore */ } }, this.AGENT_TIMEOUT_MS);
    try {
      const r = await fetch("/agent/commentate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(obs),
        signal: ctrl ? ctrl.signal : undefined,
      });
      if (!r.ok) return { line: localFallback, source: "fallback_http" };
      const j = await r.json();
      const line = (j && j.line) ? String(j.line).trim() : "";
      if (!line) return { line: localFallback, source: "fallback_empty" };
      return {
        line: line,
        source: j.source || "agent",
        latency_ms: j.latency_ms,
      };
    } catch (e) {
      return { line: localFallback, source: "fallback_timeout" };
    } finally {
      clearTimeout(timer);
    }
  },

  _rotate(options, salt) {
    const recent = this.recentLines.map((l) => l.toLowerCase());
    const fresh = options.filter((o) => recent.indexOf(String(o).toLowerCase()) < 0);
    const pool = fresh.length ? fresh : options;
    if (!pool.length) return "New round. Stay sharp.";
    const idx = Math.abs(salt || 0) % pool.length;
    return pool[idx];
  },

  templateLine(event, s, extra) {
    const board = getStandings(s);
    const top = board[0];
    extra = extra || {};
    const rn = roundNo || 1;
    const topic = (s && s.round && s.round.source && s.round.source.topic) || "this thread";
    const leader = top ? top.name : "the field";
    const score = top ? (top.score || 0) : 0;
    if (event === "lobby_join") {
      const n = (s.players || []).length;
      if (n <= 1) {
        return this._rotate([
          "Lobby is live. Waiting on a challenger.",
          "Cabinet's warm. Need one more body.",
          "Open lobby. Who's stepping up?",
        ], n);
      }
      const names = (s.players || []).map((p) => p.name).slice(0, 4).join(", ");
      return this._rotate([
        n + " players in the room. " + names + ".",
        "Board's filling up: " + names + ".",
        "Crowd check — " + names + " are in.",
      ], n + names.length);
    }
    if (event === "round_start") {
      return this._rotate([
        score > 0
          ? ("Round " + rn + ". " + leader + " sits on " + score + ".")
          : ("Round " + rn + ". Fresh board, no leader yet."),
        "Round " + rn + " on " + topic + ". Tap fast.",
        "New deal, round " + rn + ". Don't blink.",
        score > 0
          ? ("Round " + rn + ". Pressure's on " + leader + ".")
          : ("Round " + rn + ". First blood's open."),
        "Clock's live for round " + rn + ". Hunt the fake.",
        score > 0
          ? ("Round " + rn + ". " + leader + " has the belt at " + score + ".")
          : ("Round " + rn + ". Clean slate."),
        "Shuffle up. Round " + rn + " on " + topic + ".",
        "Round " + rn + ". Make it count.",
      ], rn * 17 + score + leader.length);
    }
    if (event === "player_lock" || event === "player_pick") {
      const who = extra.picker || (extra.just_locked && extra.just_locked[0]) || "Someone";
      const n = extra.pick_reply;
      const card = (typeof n === "number") ? (" reply " + n) : "";
      const label = (who === myName || who === "you") ? "You" : who;
      return this._rotate([
        label + " locked" + card + ".",
        label + " slams" + card + ".",
        "Locked in — " + label + card + ".",
        label + " commits" + card + ".",
      ], (n || 0) + label.length + rn);
    }
    if (event === "clock_low") {
      return this._rotate([
        "Ten seconds!",
        "Clock's screaming — ten left!",
        "Final ten. Decide!",
        "Ten on the clock!",
      ], rn);
    }
    if (event === "reveal") {
      const w = extra.winner;
      const rev = (s && s.reveal) || {};
      const decoy = (typeof rev.decoy_slot === "number") ? rev.decoy_slot + 1
        : (typeof extra.decoy_slot === "number" ? extra.decoy_slot + 1 : null);
      const myPick = (typeof extra.pick_reply === "number") ? extra.pick_reply : null;
      const localCorrect = !!(extra.correct) || (w && w === myName);
      const seed = rn * 31
        + (decoy || 0) * 7
        + (myPick || 0) * 3
        + String(w || "house").length
        + (localCorrect ? 17 : 0);

      // Nobody found it — house point.
      if (!w || w === "house") {
        return this._rotate([
          decoy
            ? ("Nobody snagged it. Fake sat on reply " + decoy + ".")
            : "Nobody snagged it. House takes the point.",
          decoy
            ? ("Clean miss. The bot hid in reply " + decoy + ".")
            : "Clean miss. Machine walks.",
          decoy
            ? ("House cashes — decoy was reply " + decoy + ".")
            : "House cashes this one.",
          "The machine slips through. Point to the house.",
          decoy
            ? ("Tough board. Reply " + decoy + " was the imposter.")
            : "Tough board. House keeps it.",
          "All humans fooled. Arcade laughs.",
          decoy
            ? ("Swing and a miss. Bot lived in reply " + decoy + ".")
            : "Swing and a miss. House wins the beat.",
          "Robot night. Nobody read the room.",
        ], seed);
      }

      // Local player found the decoy.
      if (localCorrect) {
        return this._rotate([
          decoy
            ? ("You sniffed out reply " + decoy + ". Point yours.")
            : "You sniffed it out. Point yours.",
          decoy
            ? ("Sharp eye — reply " + decoy + " was the bot.")
            : "Sharp eye. That's a point.",
          "Machine busted. Nice read.",
          decoy
            ? ("You had the read. Fake was reply " + decoy + ".")
            : "You had the read. Plus one.",
          "Caught the imposter cold. Well played.",
          decoy
            ? ("That's the one — reply " + decoy + ". Clean pick.")
            : "That's the one. Clean pick.",
          "Bot exposed. You take the board.",
          decoy
            ? ("You pinned reply " + decoy + ". Machine never blended.")
            : "You pinned the fake. Machine never blended.",
          "Arcade nods. That was the tell.",
          decoy
            ? ("Dead giveaway on reply " + decoy + ". Point to you.")
            : "Dead giveaway. Point to you.",
        ], seed);
      }

      // Someone else got it (local wrong or no pick).
      const who = String(w);
      return this._rotate([
        decoy
          ? (who + " nails it — decoy was reply " + decoy + ".")
          : (who + " nails it. Point theirs."),
        decoy
          ? (who + " had the read. Fake hid in reply " + decoy + ".")
          : (who + " had the read."),
        myPick
          ? ("Not this time — " + who + " got there first.")
          : (who + " takes the round."),
        decoy
          ? ("Point to " + who + ". Reply " + decoy + " was the machine.")
          : ("Point to " + who + "."),
        myPick
          ? (who + " saw it. Your card wasn't the one.")
          : (who + " saw through the noise."),
        decoy
          ? (who + " sniffs reply " + decoy + ". Board goes their way.")
          : (who + " sniffs the fake. Board goes their way."),
        "Credit " + who + " — machine's busted.",
        myPick
          ? ("Close, but " + who + " claimed the point.")
          : (who + " claims the point."),
        decoy
          ? (who + " calls reply " + decoy + ". That's the decoy.")
          : (who + " calls it clean."),
      ], seed);
    }
    if (event === "next_round") {
      return this._rotate([
        "Next round. Fresh board.",
        "Reset. New thread.",
        "Again — new five.",
      ], rn);
    }
    return this._rotate([
      "Eyes on the replies.",
      "Stay sharp.",
      "Don't sleep on this board.",
    ], rn);
  },

  /**
   * Fire agent async + time-capped. Never blocks mp3 stingers or the round.
   * If the model is slow or the phase moved on, the result is dropped.
   */
  comment(event, s, extra, opts) {
    opts = opts || {};
    if (!this.canSpeak()) return;
    if (this.inFlight > 2) return;
    const gen = this.gen;
    const phase = (s && s.phase) || (state && state.phase) || null;
    const roundId = (s && s.round && s.round.round_id)
      || (state && state.round && state.round.round_id)
      || null;
    if (!this.eventAllowed(event, phase)) return;

    const run = async () => {
      // Re-check phase at fire time (delayed lobby_join / late agent).
      if (!this.stillCurrent(gen, phase, roundId) || !this.canSpeak()) return;
      if (!this.eventAllowed(event, state && state.phase)) return;
      this.inFlight += 1;
      let got = null;
      try {
        got = await this.askAgent(event, s, extra);
      } finally {
        this.inFlight -= 1;
      }
      if (!got || !got.line) {
        // Timed-out agent: short template only if still on the same beat.
        if (!this.stillCurrent(gen, phase, roundId)) return;
        if (!this.eventAllowed(event, state && state.phase)) return;
        const fb = sanitizeHostLine(this.templateLine(event, s || state, extra || {}));
        if (!fb) return;
        const lowFb = fb.toLowerCase();
        if (this.recentLines.some((r) => r.toLowerCase() === lowFb)) return;
        voiceQueue.enqueue(async () => {
          if (!this.stillCurrent(gen, phase, roundId) || !this.canSpeak() || muted) return;
          if (!this.eventAllowed(event, state && state.phase)) return;
          await speakWithGrok(fb);
          this.rememberLine(fb);
        }, voiceMeta({ phase: phase, roundId: roundId, epoch: gen }));
        return;
      }
      // Only speak if we are still on this moment.
      if (!this.stillCurrent(gen, phase, roundId) || !this.canSpeak() || muted) return;
      if (!this.eventAllowed(event, state && state.phase)) return;
      // Drop prompt-echo before queueing so we never TTS instruction text.
      const line = sanitizeHostLine(got.line);
      if (!line) return;
      // Skip near-duplicates of something we just said.
      const low = line.toLowerCase();
      if (this.recentLines.some((r) => r.toLowerCase() === low)) return;
      voiceQueue.enqueue(async () => {
        if (!this.stillCurrent(gen, phase, roundId) || !this.canSpeak() || muted) return;
        if (!this.eventAllowed(event, state && state.phase)) return;
        await speakWithGrok(line);
        this.rememberLine(line);
      }, voiceMeta({ phase: phase, roundId: roundId, epoch: gen }));
    };

    // Always async — round path never awaits this.
    const delay = opts.delayMs || 0;
    if (delay > 0) {
      setTimeout(() => {
        if (!this.stillCurrent(gen, phase, roundId)) return;
        if (!this.eventAllowed(event, state && state.phase)) return;
        run();
      }, delay);
    } else {
      Promise.resolve().then(run);
    }
  },

  /** Local player clicked a reply card — cut openers; mp3 path unchanged. */
  onLocalPick(slot, s) {
    if (!this.canSpeak()) return;
    const snap = s || state;
    if (!snap || snap.phase !== "guessing") return;
    // If everyone is about to reveal, skip pick chatter — reveal line wins.
    const players = snap.players || [];
    const allIn = players.length > 0 && players.every((p) => p.guessed || p.name === myName);
    if (allIn && players.length > 1) {
      // Still cut openers so the lock feels snappy; don't start a pick line.
      voiceQueue.bump();
      this.gen = voiceQueue.epoch;
      return;
    }
    const replyNo = (typeof slot === "number") ? slot + 1 : null;
    // Cut leftover hype so the click feels instant; don't block on agent.
    voiceQueue.bump();
    this.gen = voiceQueue.epoch;
    this.lastGuessed[myName] = true;
    // Async agent color only — if reveal arrives first, stillCurrent drops it.
    this.comment("player_pick", snap, {
      picker: myName,
      pick_reply: replyNo,
      just_locked: [myName],
    });
  },

  onState(s, was) {
    if (!this.canSpeak() || !s) return;
    const board = getStandings(s);
    const players = s.players || [];
    const leader = board[0] && (board[0].score || 0) > 0 ? board[0].name : null;

    if (s.phase === "lobby") {
      const n = players.length;
      // Only announce when count rises while we stay in lobby (not on re-entry).
      if (was === "lobby" && n > this.lastLobbyCount && n >= 1 && joined) {
        this.comment("lobby_join", s, null, { delayMs: 150 });
      }
      this.lastLobbyCount = n;
      this.lastGuessed = {};
      this.lowClockSaid = false;
      return;
    }

    if (s.phase === "guessing" && was !== "guessing") {
      this.lowClockSaid = false;
      this.lastGuessed = {};
    }

    if (s.phase === "guessing") {
      const allGuessed = players.length > 0 && players.every((p) => p.guessed);
      // No lock spam in the last beat before reveal.
      if (!allGuessed) {
        for (const p of players) {
          if (p.guessed && !this.lastGuessed[p.name]) {
            this.lastGuessed[p.name] = true;
            // Local pick already spoke via onLocalPick — only call out opponents.
            if (p.name !== myName) {
              this.comment("player_lock", s, { just_locked: [p.name], picker: p.name });
            }
          }
        }
      }
      // Backup: server deadline_ms (primary trigger is the local timer tick).
      const left = typeof s.deadline_ms === "number" ? s.deadline_ms : null;
      if (left !== null) this.onClockLow(left, s);
    }

    if (s.phase === "reveal" && was !== "reveal") {
      // handleState calls speakReveal — don't double-fire here.
      this.lastLeader = leader;
    }

    if (s.phase === "results") {
      // Silence agent color on the results screen (mp3 stinger only).
      this.lastGuessed = {};
      this.lowClockSaid = false;
    }

    if (s.phase !== "lobby") this.lastLobbyCount = players.length;
  },

  /**
   * Reveal outcome line — varied by correct / wrong / house.
   * Skips the old fixed "Got it!" / "Wrong!" mp3s; speaks Grok Voice (or
   * template) immediately so every reveal sounds different.
   */
  speakReveal(s) {
    if (!this.canSpeak() || !s || s.phase !== "reveal") return;
    this.gen = voiceQueue.epoch;
    const gen = this.gen;
    const phase = "reveal";
    const roundId = s.round && s.round.round_id ? s.round.round_id : null;
    const w = s.reveal && s.reveal.winner;
    const mySlot = myGuessSlot;
    const decoy = s.reveal && typeof s.reveal.decoy_slot === "number"
      ? s.reveal.decoy_slot
      : null;
    const correct = (w && w === myName)
      || (decoy !== null && mySlot === decoy);
    const extra = {
      winner: w || "house",
      pick_reply: (typeof mySlot === "number") ? mySlot + 1 : null,
      picker: myName,
      correct: correct,
      decoy_slot: decoy,
    };
    const fallback = sanitizeHostLine(this.templateLine("reveal", s, extra))
      || (correct ? "You sniffed it out." : "House cashes this one.");

    let spoken = false;
    const speakOnce = async (line) => {
      if (spoken) return;
      if (!this.stillCurrent(gen, phase, roundId) || !this.canSpeak() || muted) return;
      if (!this.eventAllowed("reveal", state && state.phase)) return;
      const text = sanitizeHostLine(line);
      if (!text) return;
      // Avoid back-to-back identical reveals across rounds.
      const low = text.toLowerCase();
      if (this.recentLines.some((r) => r.toLowerCase() === low)) return;
      spoken = true;
      await speakWithGrok(text, { epoch: gen });
      this.rememberLine(text);
    };

    // Race a short agent color line vs a ready template so audio starts fast.
    this.inFlight += 1;
    const agentPromise = this.askAgent("reveal", s, extra).finally(() => {
      this.inFlight -= 1;
    });

    try { voiceQueue.items = []; } catch (e) { /* ignore */ }
    voiceQueue.enqueue(async () => {
      if (!this.stillCurrent(gen, phase, roundId) || !this.canSpeak() || muted) return;
      const raced = await Promise.race([
        agentPromise.then((got) => ({ t: "agent", got: got })),
        new Promise((resolve) => setTimeout(() => resolve({ t: "timeout" }), 450)),
      ]);
      if (!this.stillCurrent(gen, phase, roundId) || muted) return;
      if (state && state.phase !== "reveal") return;
      if (raced.t === "agent" && raced.got && raced.got.line) {
        await speakOnce(raced.got.line);
        if (spoken) return;
      }
      await speakOnce(fallback);
    }, voiceMeta({ phase: phase, roundId: roundId, epoch: gen }));
  },

  /**
   * ~10s left on the guess clock. Fired from the local timer (reliable) and
   * from state broadcasts as backup. Speaks a short template immediately —
   * no agent wait — so the line lands while the clock still matters.
   */
  onClockLow(leftMs, s) {
    if (this.lowClockSaid) return;
    if (!this.canSpeak() || muted) return;
    const snap = s || state;
    if (!snap || snap.phase !== "guessing") return;
    const left = typeof leftMs === "number" ? leftMs : null;
    if (left === null || left > 10000 || left <= 800) return;
    const players = snap.players || [];
    // Everyone already locked — reveal is imminent; skip the ten-second yell.
    if (players.length > 0 && players.every((p) => p.guessed)) return;
    if (!this.eventAllowed("clock_low", "guessing")) return;

    this.lowClockSaid = true;
    const gen = this.gen;
    const phase = "guessing";
    const roundId = snap.round && snap.round.round_id ? snap.round.round_id : null;
    const line = sanitizeHostLine(this.templateLine("clock_low", snap, {}))
      || "Ten seconds!";
    // Drop pending chatter so "ten seconds" isn't stuck behind an opener tail.
    // Do NOT bump epoch — that would cancel the current clip mid-word and
    // invalidate this job's gen token.
    try { voiceQueue.items = []; } catch (e) { /* ignore */ }

    voiceQueue.enqueue(async () => {
      if (!this.stillCurrent(gen, phase, roundId) || !this.canSpeak() || muted) return;
      if (!this.eventAllowed("clock_low", state && state.phase)) return;
      // Re-check clock — if we're already past the wire, don't yell late.
      const stillLeft = timerEndAt ? (timerEndAt - performance.now()) : left;
      if (stillLeft <= 400) return;
      await speakWithGrok(line, { epoch: gen });
      this.rememberLine(line);
    }, voiceMeta({ phase: phase, roundId: roundId, epoch: gen }));

    // Optional color from the agent — only if template already spoke and we're
    // still in the window; skip agent to keep this snappy (template is enough).
  },

  /**
   * Fresh opener every round: agent first (time-capped), else rotated template
   * spoken via Grok Voice — never the same stock mp3 line on loop.
   */
  openRound(s) {
    if (!this.canSpeak() || !s) return;
    if (s.phase !== "guessing") return;
    const gen = this.gen;
    const phase = "guessing";
    const roundId = s.round && s.round.round_id ? s.round.round_id : null;
    if (!roundId) return;
    const fallback = this.templateLine("round_start", s, {});

    // Always have a unique fallback ready on the queue so audio isn't silent
    // if the model is slow — but agent may replace flavor if it returns first.
    let spoken = false;
    const speakOnce = async (line, source) => {
      if (spoken) return;
      if (!this.stillCurrent(gen, phase, roundId) || !this.canSpeak() || muted) return;
      if (!this.eventAllowed("round_start", state && state.phase)) return;
      const text = String(line || "").trim();
      if (!text) return;
      const low = text.toLowerCase();
      if (this.recentLines.some((r) => r.toLowerCase() === low)) return;
      spoken = true;
      await speakWithGrok(text);
      this.rememberLine(text);
    };

    // Fast path: kick agent immediately (async).
    this.inFlight += 1;
    const agentPromise = this.askAgent("round_start", s, null).finally(() => {
      this.inFlight -= 1;
    });

    voiceQueue.enqueue(async () => {
      if (!this.stillCurrent(gen, phase, roundId) || !this.canSpeak() || muted) return;
      // Wait briefly for agent; if too slow, use rotated template.
      const raced = await Promise.race([
        agentPromise.then((got) => ({ t: "agent", got: got })),
        new Promise((resolve) => setTimeout(() => resolve({ t: "timeout" }), 700)),
      ]);
      if (!this.stillCurrent(gen, phase, roundId) || muted) return;
      if (state && state.phase !== "guessing") return;
      if (raced.t === "agent" && raced.got && raced.got.line) {
        const src = raced.got.source ? String(raced.got.source) : "";
        await speakOnce(raced.got.line, src || "agent");
        return;
      }
      // Agent still running: use template now; drop late agent entirely
      // (late openers during locks/reveal are the main "wrong time" bug).
      await speakOnce(fallback, "fallback_rotate");
    }, voiceMeta({ phase: phase, roundId: roundId, epoch: gen }));
  },
};

try {
  const saved = localStorage.getItem("arcade_comm");
  commentary.setEnabled(saved === null ? true : saved === "1");
} catch (e) {
  commentary.setEnabled(true);
}
$("commBtn").addEventListener("click", () => commentary.setEnabled(!commentary.enabled));

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
    if (j.tts_voice) ttsVoice = String(j.tts_voice).toLowerCase();
    if (j.mode === "demo" || j.demo === true) {
      $("demoBadge").hidden = false;
    } else if (j.mode === "live") {
      $("demoBadge").textContent = "LIVE";
      $("demoBadge").hidden = false;
    }
  }).catch(() => {});
}

// ---------- transport ----------
// Outbox: join/start often fire on the same click that opens audio; the WS
// may not be OPEN yet. Dropped joins used to lose the theme filter forever.
const wsOutbox = [];

function wsIsOpen() {
  if (!sock) return false;
  // Mock transport has no browser readyState — treat as always open.
  if (MOCK) return typeof sock.send === "function";
  return sock.readyState === 1; // OPEN
}

function send(obj) {
  if (!obj || typeof obj !== "object") return;
  if (!wsIsOpen()) {
    // Coalesce join messages — only the latest join matters.
    if (obj.t === "join") {
      for (let i = wsOutbox.length - 1; i >= 0; i--) {
        if (wsOutbox[i] && wsOutbox[i].t === "join") wsOutbox.splice(i, 1);
      }
    }
    wsOutbox.push(obj);
    return;
  }
  try {
    sock.send(JSON.stringify(obj));
  } catch (e) {
    wsOutbox.push(obj);
  }
}

function flushWsOutbox() {
  if (!wsIsOpen() || !wsOutbox.length) return;
  const batch = wsOutbox.splice(0, wsOutbox.length);
  for (let i = 0; i < batch.length; i++) {
    try {
      sock.send(JSON.stringify(batch[i]));
    } catch (e) {
      // Put back and stop — reconnect will retry.
      wsOutbox.unshift.apply(wsOutbox, batch.slice(i));
      return;
    }
  }
}

/** Host join payload — includes theme when we know it; never wipe server filter. */
function buildJoinPayload() {
  const payload = {
    t: "join",
    room: myRoom,
    name: myName,
    arena: !!(iAmHost || lobbyMode === "solo" || isSoloFriendlyRoom(myRoom)),
  };
  if (payload.arena) {
    const theme = activeThemePayload();
    const topics = theme.topics || [];
    if (topics.length) {
      // Non-empty filter — always re-assert.
      Object.assign(payload, theme);
    } else if (theme._explicitRandom) {
      // Host deliberately chose RANDOM.
      Object.assign(payload, theme);
      payload.clear_topics = true;
      payload.topics_random = true;
    }
    // else: omit topic keys so a reconnect cannot clear a set filter.
  }
  return payload;
}

/** Push theme to server (multiplayer host, lobby only). */
function pushThemeToServer() {
  if (!joined || !myRoom) return;
  if (!(iAmHost || lobbyMode === "solo" || isSoloFriendlyRoom(myRoom))) return;
  const theme = activeThemePayload();
  const msg = Object.assign(
    { t: "set_topics", room: myRoom, arena: true },
    theme
  );
  if (theme._explicitRandom || !(theme.topics || []).length) {
    msg.clear_topics = true;
    msg.topics_random = true;
  }
  send(msg);
}

/** Drop the legacy mock bot if it ever appears in a state payload. */
function stripFakePlayers(s) {
  if (!s || typeof s !== "object") return s;
  const kill = (list) => (list || []).filter(
    (p) => p && String(p.name || "").toUpperCase() !== "GLITCH"
  );
  if (Array.isArray(s.players)) s.players = kill(s.players);
  if (Array.isArray(s.standings)) {
    s.standings = kill(s.standings).map((p, i) => Object.assign({}, p, { rank: i + 1 }));
  }
  if (s.reveal && Array.isArray(s.reveal.leaderboard)) {
    s.reveal.leaderboard = kill(s.reveal.leaderboard).map((p, i) =>
      Object.assign({}, p, { rank: i + 1 })
    );
  }
  if (s.results && Array.isArray(s.results.standings)) {
    s.results.standings = kill(s.results.standings).map((p, i) =>
      Object.assign({}, p, { rank: i + 1 })
    );
  }
  return s;
}

function handleRaw(text) {
  let msg = null;
  try { msg = JSON.parse(text); } catch (e) { return; }
  if (msg && msg.t === "state") handleState(stripFakePlayers(msg));
}

function connect() {
  if (MOCK) {
    sock = mockSocket(handleRaw);
    setConn("MOCK LINK ACTIVE");
    flushWsOutbox();
    return;
  }
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/ws`);
  sock = ws;
  ws.addEventListener("open", () => {
    setConn("LINKED");
    // Re-join with theme locked at enter — never a bare join that wipes topics.
    if (joined && myRoom && myName) {
      // Prefer a fresh join at the front; drop stale queued joins.
      for (let i = wsOutbox.length - 1; i >= 0; i--) {
        if (wsOutbox[i] && wsOutbox[i].t === "join") wsOutbox.splice(i, 1);
      }
      wsOutbox.unshift(buildJoinPayload());
    }
    flushWsOutbox();
  });
  ws.addEventListener("message", (ev) => handleRaw(ev.data));
  ws.addEventListener("close", () => { setConn("LINK LOST, RETRYING..."); setTimeout(connect, 1500); });
  ws.addEventListener("error", () => { try { ws.close(); } catch (e) {} });
}

function setConn(text) {
  const line = $("connLine");
  if (line) line.textContent = text;
  // Top-bar chip stays visible during play (lobby connLine is hidden with the lobby).
  const chip = $("connChip");
  if (!chip) return;
  const short = String(text || "").replace(/\.\.\.$/, "…");
  chip.textContent = short.length > 18 ? short.slice(0, 16) + "…" : short;
  chip.title = text || "";
  chip.classList.remove("ok", "warn", "bad");
  const u = short.toUpperCase();
  if (u.indexOf("LINKED") !== -1 || u.indexOf("JOINED") !== -1 || u.indexOf("MOCK") !== -1) {
    chip.classList.add("ok");
  } else if (u.indexOf("LOST") !== -1 || u.indexOf("RETRY") !== -1 || u.indexOf("FAIL") !== -1) {
    chip.classList.add("bad");
  } else {
    chip.classList.add("warn");
  }
}

// ---------- state handling ----------
function handleState(s) {
  const was = state ? state.phase : null;
  const wasRoundId = state && state.round ? state.round.round_id : null;
  state = s;
  noteAutoDeadline(s);

  // Keep top-bar theme chip + lobby status in sync with server filter.
  try { updateTopicSelectionStatus(s.topic_filter); } catch (e) { /* ignore */ }
  // Recover if host locked a theme but server still has [].
  try { ensureThemeOnServer(s); } catch (e) { /* ignore */ }
  // Multiplayer host: chips stay on in lobby; lock once the match is live.
  if (iAmHost && lobbyMode === "create") {
    if (s.phase === "lobby") setTopicChipsEnabled(true);
    else setTopicChipsEnabled(false);
  }

  const newRoundId = s.round && s.round.round_id ? s.round.round_id : null;
  const phaseChanged = was !== null && was !== s.phase;
  const roundChanged = !!(newRoundId && wasRoundId && newRoundId !== wasRoundId);

  // Game moved on → cut leftover lines from the old moment, THEN queue new ones.
  if (phaseChanged || roundChanged) {
    voiceQueue.bump();
    if (typeof commentary !== "undefined") commentary.onAdvance(s.phase, newRoundId);
  }

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
  // Hard stingers first (phase transitions only), then commentary.onState.
  if (s.phase === "guessing" && was !== "guessing") {
    unlockAudio();
    // Session welcome once. After that, skip the same host_round.mp3 every time —
    // each round gets a fresh spoken line (agent or rotated template) instead.
    if (firstRoundOfSession) {
      firstRoundOfSession = false;
      if (!isSoloFriendlyRoom(myRoom)) playHost("intro");
    }
    // Varied round opener (Grok TTS / agent) — not the identical mp3 loop.
    try { commentary.openRound(s); } catch (e) { /* ignore */ }
  }
  if (s.phase === "reveal" && was !== "reveal") {
    unlockAudio();
    // Varied Grok Voice line by correct/wrong/house — never the old fixed
    // "Got it!" / "Wrong!" mp3 loop.
    try { commentary.speakReveal(s); } catch (e) { /* ignore */ }
    requestAnimationFrame(() => {
      const panel = $("revealPanel");
      if (panel && !panel.hidden && panel.scrollIntoView) {
        try { panel.scrollIntoView({ behavior: "smooth", block: "start" }); }
        catch (e) { try { panel.scrollIntoView(true); } catch (e2) { /* ignore */ } }
      }
    });
  }
  if (s.phase === "results" && was !== "results") {
    unlockAudio();
    // Match-end: short varied line, not the legacy win mp3.
    try {
      if (commentary.canSpeak() && !muted) {
        const board = getStandings(s);
        const top = board[0];
        const name = top && top.name ? top.name : null;
        const youWin = name && name === myName;
        const line = commentary._rotate(
          youWin
            ? [
              "Match over. You take the cabinet.",
              "Final board is yours. Well played.",
              "You close it out. Arcade lights up.",
              "Session done — you finish on top.",
            ]
            : name
              ? [
                "Match over. " + name + " owns the cabinet.",
                "Final call — " + name + " leads the board.",
                "Session done. Crown goes to " + name + ".",
                name + " closes the night on top.",
              ]
              : [
                "Match over. That's a wrap.",
                "Session done. Reset when you're ready.",
                "Arcade dark. Good games.",
              ],
          roundNo * 13 + (name ? name.length : 0)
        );
        voiceQueue.enqueue(async () => {
          if (muted || !commentary.canSpeak()) return;
          await speakWithGrok(line);
          commentary.rememberLine(line);
        }, voiceMeta({ phase: "results" }));
      }
    } catch (e) { /* ignore */ }
    requestAnimationFrame(() => {
      const panel = $("screen-results");
      if (panel && !panel.hidden && panel.scrollIntoView) {
        try { panel.scrollIntoView({ behavior: "smooth", block: "start" }); }
        catch (e) { try { panel.scrollIntoView(true); } catch (e2) { /* ignore */ } }
      }
    });
  }
  try { commentary.onState(s, was); } catch (e) { /* never break the game on voice */ }
  prevPhase = was;
  render(s);
}

// ---------- standings / points ----------
/** Ranked rows from server standings, or derive from players for older payloads. */
function getStandings(s) {
  if (s.standings && s.standings.length) return s.standings.slice();
  if (s.reveal && s.reveal.leaderboard && s.reveal.leaderboard.length) {
    return s.reveal.leaderboard.map((p, i) => ({
      rank: p.rank || i + 1,
      name: p.name,
      score: p.score || 0,
      streak: p.streak || 0,
    }));
  }
  const rows = (s.players || []).map((p) => ({
    name: p.name,
    score: p.score || 0,
    streak: p.streak || 0,
  }));
  rows.sort((a, b) => b.score - a.score || a.name.localeCompare(b.name));
  return rows.map((p, i) => ({ rank: i + 1, name: p.name, score: p.score, streak: p.streak }));
}

function renderTopPoints(s) {
  const board = getStandings(s);
  const meChip = $("myPoints");
  const leadChip = $("leadChip");
  if (!joined || !myName) {
    if (meChip) meChip.hidden = true;
    if (leadChip) leadChip.hidden = true;
    return;
  }
  const me = board.find((p) => p.name === myName)
    || (s.players || []).find((p) => p.name === myName);
  const myScore = me ? (me.score || 0) : 0;
  const leader = board[0] || null;
  const iLead = !!(leader && leader.name === myName && board.length > 0);

  if (meChip) {
    meChip.hidden = false;
    meChip.textContent = iLead && board.length > 1
      ? ("YOU " + myScore + " · LEAD")
      : ("YOU " + myScore + " PTS");
    meChip.classList.toggle("leading", iLead && (leader.score || 0) > 0);
    meChip.title = "Your points (+1 each round you spot the decoy)";
  }
  if (leadChip) {
    if (leader && board.length > 0) {
      leadChip.hidden = false;
      leadChip.textContent = iLead
        ? ("#1 YOU · " + (leader.score || 0))
        : ("#1 " + leader.name + " · " + (leader.score || 0));
      leadChip.title = "Current leader by points";
    } else {
      leadChip.hidden = true;
    }
  }
}

/**
 * Live side scoreboard — every player, ranked, with score + streak + lock state.
 * Updates on every state broadcast during guessing and reveal.
 */
function renderLiveStandings(s) {
  const box = $("liveStandings");
  const aside = $("sideScoreboard");
  const meta = $("sbMeta");
  const foot = $("sbFoot");
  if (!box) return;

  const phase = s.phase || "";
  const show = phase === "guessing" || phase === "reveal";
  if (aside) aside.hidden = !show;
  document.body.classList.toggle("has-scoreboard", show);
  if (!show) {
    box.innerHTML = "";
    return;
  }

  const board = getStandings(s);
  const playersByName = {};
  for (const p of s.players || []) {
    if (p && p.name) playersByName[p.name] = p;
  }

  const matchRounds = s.match_rounds || 0;
  const played = s.rounds_played || 0;
  if (meta) {
    meta.classList.remove("is-live", "is-reveal");
    if (phase === "reveal") {
      meta.textContent = "REVEAL";
      meta.classList.add("is-reveal");
    } else {
      meta.textContent = matchRounds
        ? ("RND " + played + "/" + matchRounds)
        : "LIVE";
      meta.classList.add("is-live");
    }
  }
  const soloBoard = isSoloFriendlyRoom(s.room || myRoom) || lobbyMode === "solo";
  if (foot) {
    if (soloBoard) {
      foot.textContent = "SOLO · YOU VS THE HOUSE · +1 IF YOU SPOT IT";
    } else {
      const n = board.filter((r) => String(r.name || "").toUpperCase() !== "GLITCH").length;
      foot.textContent = n
        ? (n + " PLAYER" + (n === 1 ? "" : "S") + " · +1 EACH CORRECT")
        : "+1 EACH CORRECT EACH ROUND";
    }
  }

  box.innerHTML = "";
  board.forEach((row) => {
    // Hide mock bot and never list phantom multiplayer rows in solo.
    if (String(row.name || "").toUpperCase() === "GLITCH") return;
    if (soloBoard && row.name !== myName) return;
    const live = playersByName[row.name] || {};
    const isMe = row.name === myName;
    const isLead = row.rank === 1 && (row.score || 0) > 0;
    const locked = !!(live.guessed || (isMe && myGuessSlot !== null));
    const streak = row.streak || live.streak || 0;

    const li = document.createElement("li");
    li.className = "sb-row"
      + (isLead ? " is-leader" : "")
      + (isMe ? " is-me" : "")
      + (locked && phase === "guessing" ? " is-locked" : "");
    li.setAttribute("aria-label",
      "#" + (row.rank || "?") + " " + row.name + " " + (row.score || 0) + " points");

    const rank = document.createElement("span");
    rank.className = "sb-rank";
    rank.textContent = "#" + (row.rank || "—");

    const who = document.createElement("div");
    who.className = "sb-who";
    const name = document.createElement("span");
    name.className = "sb-name";
    name.textContent = isMe ? "YOU" : String(row.name || "?");
    const status = document.createElement("span");
    status.className = "sb-status";
    if (phase === "guessing") {
      status.textContent = locked ? "LOCKED IN" : "PICKING…";
    } else {
      // Reveal: show whether they got it (guess_slot only public at reveal).
      const decoy = s.reveal && typeof s.reveal.decoy_slot === "number"
        ? s.reveal.decoy_slot
        : null;
      if (decoy !== null && typeof live.guess_slot === "number") {
        status.textContent = live.guess_slot === decoy ? "HIT" : "MISS";
      } else {
        status.textContent = "—";
      }
    }
    who.append(name, status);

    const scoreCol = document.createElement("div");
    scoreCol.className = "sb-score-col";
    const pts = document.createElement("span");
    pts.className = "sb-pts";
    pts.textContent = String(row.score || 0);
    scoreCol.appendChild(pts);
    if (streak > 1) {
      const st = document.createElement("span");
      st.className = "sb-streak";
      st.textContent = streak + "× STREAK";
      scoreCol.appendChild(st);
    }

    li.append(rank, who, scoreCol);
    box.appendChild(li);
  });
}

// ---------- rendering ----------
function render(s) {
  const matchRounds = s.match_rounds || 0;
  const played = s.rounds_played || roundNo || 0;
  if (matchRounds > 0 && (s.phase === "guessing" || s.phase === "reveal")) {
    $("roundCounter").textContent =
      "RND " + String(played).padStart(2, "0") + "/" + String(matchRounds).padStart(2, "0");
  } else if (s.phase === "results") {
    $("roundCounter").textContent = "FINAL";
  } else {
    $("roundCounter").textContent = "RND " + String(roundNo).padStart(2, "0");
  }
  const screened = !!(s.round && s.round.safety && s.round.safety.screened);
  $("safetyChip").hidden = !screened;
  renderTopPoints(s);

  const phase = s.phase || "lobby";
  const inLobby = phase === "lobby";
  const inResults = phase === "results";
  const inGame = phase === "guessing" || phase === "reveal";
  $("screen-lobby").hidden = !inLobby;
  $("screen-game").hidden = !inGame;
  const resultsScreen = $("screen-results");
  if (resultsScreen) resultsScreen.hidden = !inResults;
  // Clear side scoreboard chrome when leaving the arena.
  if (!inGame) {
    document.body.classList.remove("has-scoreboard");
    const aside = $("sideScoreboard");
    if (aside) aside.hidden = true;
  }

  if (inLobby) renderLobby(s);
  else if (inResults) renderResults(s);
  else renderGame(s);
}

function renderLobby(s) {
  const ul = $("lobbyPlayers");
  ul.innerHTML = "";
  const board = getStandings(s);
  const players = s.players || [];
  // Show room topic filter under standings label.
  let topicLine = $("lobbyTopicLine");
  if (!topicLine) {
    const wrap = ul.parentElement;
    if (wrap) {
      topicLine = document.createElement("p");
      topicLine.id = "lobbyTopicLine";
      topicLine.className = "lobby-topic-line";
      wrap.insertBefore(topicLine, ul);
    }
  }
  if (topicLine) {
    const tf = s.topic_filter || [];
    topicLine.innerHTML = "TOPICS · <b>" + formatTopicFilterLabel(tf) + "</b>";
  }
  const soloLobby = isSoloFriendlyRoom(s.room || myRoom) || lobbyMode === "solo";
  const lobbyBoard = soloLobby
    ? board.filter((p) => p.name === myName)
    : board.filter((p) => String(p.name || "").toUpperCase() !== "GLITCH");
  if (lobbyBoard.length === 0) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = soloLobby ? "YOU · SOLO PRACTICE" : "NO PLAYERS YET";
    ul.appendChild(li);
  }
  for (const p of lobbyBoard) {
    const li = document.createElement("li");
    if (p.rank === 1 && (p.score || 0) > 0) li.classList.add("is-leader");
    if (p.name === myName) li.classList.add("is-me");

    const rank = document.createElement("span");
    rank.className = "rank";
    rank.textContent = "#" + (p.rank || "—");

    const who = document.createElement("span");
    who.className = "who";
    who.textContent = p.name === myName
      ? (soloLobby ? "YOU (SOLO)" : "YOU")
      : p.name;

    const pts = document.createElement("span");
    pts.className = "pts";
    pts.textContent = (p.score || 0) + " PTS";

    li.append(rank, who, pts);
    if ((p.streak || 0) > 1) {
      const st = document.createElement("span");
      st.className = "streak";
      st.textContent = p.streak + "×";
      li.appendChild(st);
    }
    ul.appendChild(li);
  }
  // START skips the lobby wait. Multiplayer needs 2+ players; solo needs 1.
  const start = $("startBtn");
  const wait = $("waitLine");
  const soloRoom = isSoloFriendlyRoom(s.room || myRoom) || lobbyMode === "solo";
  const minPlayers = (typeof s.min_players === "number")
    ? s.min_players
    : (soloRoom ? 1 : 2);
  const canStart = (typeof s.can_start === "boolean")
    ? s.can_start
    : players.length >= minPlayers;
  // Solo never shows join QR / share chrome.
  if (soloRoom) {
    const qr = $("lobbyQr");
    if (qr) qr.hidden = true;
  }
  // One-tap solo: once the server accepts the join, kick the round.
  if (pendingSoloStart && joined && canStart && soloRoom) {
    pendingSoloStart = false;
    try { sendNext(); } catch (e) { /* ignore */ }
  }
  if (!joined) {
    // Solo setup screen keeps START GAME visible before WS join.
    if (lobbyMode === "solo") {
      start.hidden = false;
      start.disabled = false;
      start.textContent = "START GAME";
      if (wait) wait.hidden = true;
    } else {
      start.hidden = true;
      start.disabled = true;
      if (wait) wait.hidden = true;
    }
  } else if (soloRoom) {
    start.hidden = false;
    start.disabled = !canStart;
    start.textContent = "START GAME";
    if (wait) wait.hidden = true;
  } else {
    start.hidden = false;
    start.disabled = !canStart;
    start.textContent = canStart
      ? "START NOW"
      : ("NEED " + minPlayers + " PLAYERS");
    if (wait) {
      wait.hidden = false;
      if (!canStart) {
        wait.textContent = "WAITING FOR PLAYERS · "
          + players.length + "/" + minPlayers;
      } else {
        wait.textContent = autoCountdownText(s, "ROUND STARTS")
          || ("READY · " + players.length + " PLAYERS");
      }
    }
  }
}

// The server sends auto_ms, the time until the session clock advances on its
// own. Freeze it into a wall-clock deadline at receipt so the ticker can
// count down between broadcasts.
let autoEndAt = 0;
function noteAutoDeadline(s) {
  autoEndAt = typeof s.auto_ms === "number" ? performance.now() + s.auto_ms : 0;
}
function autoCountdownText(s, prefix) {
  if (!autoEndAt) return "";
  const left = Math.max(0, Math.ceil((autoEndAt - performance.now()) / 1000));
  return prefix + " IN " + left + "s";
}
setInterval(() => {
  if (!state || !autoEndAt) return;
  if (state.phase === "lobby" && $("waitLine") && !$("waitLine").hidden) {
    $("waitLine").textContent = autoCountdownText(state, "ROUND STARTS");
  }
  if (state.phase === "reveal" && !$("revealPanel").hidden) {
    const final = !!(state.match_over || (state.reveal && state.reveal.final_round));
    const label = final ? "RESULTS" : "NEXT ROUND";
    const t = autoCountdownText(state, label);
    if (t) $("nextBtn").textContent = t + " · TAP TO SKIP";
  }
  if (state.phase === "results") {
    const wait = $("resultsWait");
    if (wait) {
      const t = autoCountdownText(state, "RETURNING TO LOBBY");
      if (t) {
        wait.hidden = false;
        wait.textContent = t;
      }
    }
  }
}, 500);

function renderGame(s) {
  const r = s.round || {};
  const src = r.source || {};
  $("postAuthor").textContent = src.post_author || "@unknown";
  $("postAvatar").textContent = (src.post_author || "?").replace("@", "").charAt(0) || "?";

  // During guessing, freeze format on first paint for this round_id so a late
  // server media attach cannot pop GIFs in after the user already picked.
  let fmt = (r.format === "gif") ? "gif" : "text";
  if (s.phase === "guessing" && r.round_id) {
    if (guessingMediaFreezeRid !== r.round_id) {
      guessingMediaFreezeRid = r.round_id;
      guessingMediaFreezeGif = fmt === "gif"
        && (r.replies || []).some((rep) => rep && rep.media_url);
    }
    // If we started without media, keep text for the whole guess.
    if (!guessingMediaFreezeGif) {
      fmt = "text";
      // Strip media so renderReplies cannot paint late-arriving URLs.
      if (r.replies) {
        r.replies = r.replies.map((rep) => {
          if (!rep || typeof rep !== "object") return rep;
          const copy = Object.assign({}, rep);
          delete copy.media_url;
          delete copy.media_type;
          delete copy.media_status;
          return copy;
        });
      }
      r.format = "text";
    }
  } else if (s.phase !== "guessing") {
    guessingMediaFreezeRid = "";
    guessingMediaFreezeGif = false;
  }

  const topic = src.topic || "";
  const themeLabel = formatTopicFilterLabel(s.topic_filter || []);
  const roundKind = fmt === "gif" ? "GIF ROUND" : "TEXT ROUND";
  // Show room theme + post tag so players can see the filter is stuck.
  if (themeLabel && themeLabel !== "RANDOM MIX") {
    $("postTopic").textContent = themeLabel
      + (topic ? " · " + String(topic).toUpperCase() : "")
      + " · " + roundKind;
  } else {
    $("postTopic").textContent = topic
      ? (String(topic).toUpperCase() + " · " + roundKind)
      : roundKind;
  }
  $("postText").textContent = src.post_text || "";
  $("timerWrap").style.visibility = s.phase === "guessing" ? "visible" : "hidden";
  const postCard = $("postCard");
  if (postCard) {
    postCard.classList.toggle("is-gif-round", fmt === "gif");
    postCard.classList.toggle("is-text-round", fmt !== "gif");
  }

  // Post stays text. Replies are plain text or GIF cards depending on format.
  hidePostRoundArt();
  renderLiveStandings(s);
  renderOpponents(s);
  renderReplies(s);
  renderReveal(s);
}

// Fingerprint of reply media set last used for recenter — avoid jitter.
let lastCenteredReplyArtKey = "";
// Freeze gif-vs-text for the current guessing round so late media does not
// pop in after the player has already locked a pick.
let guessingMediaFreezeRid = "";
let guessingMediaFreezeGif = false;

/** Hide legacy post-level Imagine block. */
function hidePostRoundArt() {
  const wrap = $("roundArt");
  if (wrap) wrap.hidden = true;
  const img = $("roundArtImg");
  if (img) {
    img.hidden = true;
    img.removeAttribute("src");
  }
  const pending = $("roundArtPending");
  if (pending) pending.hidden = true;
}

/** Scroll the reply grid into view when GIF cards land. */
function centerRepliesInView(smooth) {
  const target = $("replies") || $("screen-game");
  if (!target || target.hidden) return;
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      try {
        target.scrollIntoView({
          behavior: smooth === false ? "auto" : "smooth",
          block: "start",
          inline: "nearest",
        });
      } catch (e) {
        try { target.scrollIntoView(true); } catch (e2) { /* ignore */ }
      }
    });
  });
}

/** True for Grok Imagine decoy videos (never human pool .gif / probe paths). */
function isImagineVideoUrl(url) {
  if (!url) return false;
  const u = String(url).split("?")[0].toLowerCase();
  if (u.indexOf("/reply-gifs/decoy/") < 0) return false;
  // Shared offline probe is not a real Grok Imagine generation.
  if (/_probe\.mp4$/i.test(u)) return false;
  return /\.(mp4|webm|mov)$/i.test(u);
}

/** True for human reaction pool GIFs. */
function isHumanPoolGifUrl(url) {
  if (!url) return false;
  const u = String(url).split("?")[0].toLowerCase();
  if (u.indexOf("/reply-gifs/decoy/") >= 0) return false;
  return /\/reply-gifs\/[^/]+\.gif$/i.test(u);
}

/**
 * Build looping media for a reply card.
 *
 * During guessing every card must look identical — no "GIF" vs "GROK IMAGINE"
 * labels, no different pending copy. Source is only named on the revealed decoy.
 */
function buildReplyMedia(reply, isRevealDecoy, onReady) {
  let url = reply && reply.media_url ? String(reply.media_url) : "";
  const status = reply && reply.media_status
    ? String(reply.media_status)
    : (url ? "ready" : "none");
  const mtype = reply && reply.media_type ? String(reply.media_type) : "";

  // Never show a human pool .gif on the decoy card (Imagine only).
  if (isRevealDecoy && url && isHumanPoolGifUrl(url)) {
    url = "";
  }
  // Never use legacy still art for decoy — must be Imagine video.
  let legacyUrl = "";
  if (!url && !isRevealDecoy && reply && reply.art_url) {
    legacyUrl = String(reply.art_url);
  }
  const effectiveUrl = url || legacyUrl;
  if (status === "none" && !effectiveUrl) return null;

  const frame = document.createElement("div");
  frame.className = "reply-media";

  // Labels only after reveal: name the decoy. Guessing stays anonymous.
  if (isRevealDecoy) {
    const label = document.createElement("div");
    label.className = "reply-media-label is-imagine";
    label.textContent = "GROK IMAGINE";
    frame.appendChild(label);
  }

  const box = document.createElement("div");
  box.className = "reply-media-frame";

  const markReady = (key) => {
    if (typeof onReady === "function") onReady(key || effectiveUrl);
  };

  // Pending / empty — same chrome on every card (never "GROK IMAGINE…" pre-reveal).
  if (!effectiveUrl && (status === "pending" || isRevealDecoy)) {
    const pending = document.createElement("div");
    pending.className = "reply-media-pending";
    pending.textContent = isRevealDecoy ? "GROK IMAGINE…" : "LOADING…";
    box.appendChild(pending);
    frame.appendChild(box);
    return frame;
  }
  if (!effectiveUrl) {
    if (status === "pending") {
      const pending = document.createElement("div");
      pending.className = "reply-media-pending";
      pending.textContent = "LOADING…";
      box.appendChild(pending);
      frame.appendChild(box);
      return frame;
    }
    if (status === "failed") {
      const fail = document.createElement("div");
      fail.className = "reply-media-pending";
      fail.textContent = "UNAVAILABLE";
      box.appendChild(fail);
      frame.appendChild(box);
      return frame;
    }
    return null;
  }

  // Prefer <video> for all mp4/webm so human pool and decoy match in the DOM.
  const isVideo = mtype === "video"
    || isImagineVideoUrl(effectiveUrl)
    || /\.(mp4|webm|mov)(\?|$)/i.test(effectiveUrl)
    || effectiveUrl.indexOf("/media/") === 0;

  if (isRevealDecoy && isHumanPoolGifUrl(effectiveUrl)) {
    const pending = document.createElement("div");
    pending.className = "reply-media-pending";
    pending.textContent = "GROK IMAGINE…";
    box.appendChild(pending);
    frame.appendChild(box);
    return frame;
  }

  if (isVideo) {
    const vid = document.createElement("video");
    vid.className = "reply-media-el reply-media-video";
    vid.src = effectiveUrl;
    vid.muted = true;
    vid.defaultMuted = true;
    vid.playsInline = true;
    vid.setAttribute("playsinline", "");
    vid.setAttribute("webkit-playsinline", "");
    vid.loop = true;
    vid.autoplay = true;
    vid.preload = "auto";
    vid.setAttribute("aria-label", "reply media");
    vid.onloadeddata = () => {
      vid.classList.add("is-ready");
      try { vid.play().catch(() => {}); } catch (e) { /* ignore */ }
      markReady(effectiveUrl);
    };
    vid.oncanplay = () => {
      try { vid.play().catch(() => {}); } catch (e) { /* ignore */ }
    };
    vid.onerror = () => {
      vid.remove();
      const fail = document.createElement("div");
      fail.className = "reply-media-pending";
      fail.textContent = "UNAVAILABLE";
      box.appendChild(fail);
    };
    box.appendChild(vid);
    if (vid.readyState >= 2) {
      vid.classList.add("is-ready");
      try { vid.play().catch(() => {}); } catch (e) { /* ignore */ }
      markReady(effectiveUrl);
    }
  } else {
    // Fallback still/gif — still no source label during guessing.
    const img = document.createElement("img");
    img.className = "reply-media-el reply-media-img";
    img.alt = "reply media";
    img.loading = "lazy";
    img.decoding = "async";
    img.onload = () => {
      img.classList.add("is-ready");
      markReady(effectiveUrl);
    };
    img.onerror = () => {
      img.remove();
      const fail = document.createElement("div");
      fail.className = "reply-media-pending";
      fail.textContent = "UNAVAILABLE";
      box.appendChild(fail);
    };
    img.src = effectiveUrl;
    box.appendChild(img);
    if (img.complete && img.naturalWidth > 0) {
      img.classList.add("is-ready");
      markReady(effectiveUrl);
    }
  }

  frame.appendChild(box);
  return frame;
}

function renderOpponents(s) {
  const strip = $("oppStrip");
  strip.innerHTML = "";
  if (s.phase !== "guessing") return;
  // Solo = you vs the house — never show a second human row.
  if (isSoloFriendlyRoom(s.room || myRoom) || lobbyMode === "solo") {
    const el = document.createElement("span");
    el.className = "opp house-opp";
    el.textContent = "VS THE HOUSE";
    strip.appendChild(el);
    return;
  }
  for (const p of s.players || []) {
    if (p.name === myName) continue;
    // Never surface the offline mock bot in live UI.
    if (String(p.name || "").toUpperCase() === "GLITCH") continue;
    const el = document.createElement("span");
    el.className = "opp" + (p.guessed ? " locked" : "");
    const pts = typeof p.score === "number" ? p.score : 0;
    el.textContent = p.name
      + " · " + pts + "p"
      + (p.guessed ? " · LOCKED" : " · PICKING");
    strip.appendChild(el);
  }
}

function renderReplies(s) {
  const grid = $("replies");
  grid.innerHTML = "";
  const r = s.round || {};
  const reveal = s.reveal;
  const canTap = s.phase === "guessing" && myGuessSlot === null && !myGuessConfirmed(s);
  const replies = r.replies || [];
  const isReveal = s.phase === "reveal" && !!reveal;
  // Server sets format explicitly. Only gif rounds show media cards.
  const isGifRound = r.format === "gif";

  // Recenter once when the media set for this round is ready.
  const mediaKey = isGifRound
    ? replies
        .map((rep) => (rep && rep.media_url) || (rep && rep.media_status) || "")
        .join("|")
    : "";
  let didScheduleCenter = false;
  const maybeCenter = (url) => {
    if (!isGifRound || didScheduleCenter) return;
    if (!mediaKey || mediaKey === lastCenteredReplyArtKey) return;
    if (!replies.some((rep) => rep && rep.media_url)) return;
    didScheduleCenter = true;
    lastCenteredReplyArtKey = mediaKey;
    centerRepliesInView(true);
  };

  for (const reply of replies) {
    const card = document.createElement("div");
    card.className = "card" + (isGifRound ? " has-media" : "");
    card.dataset.slot = reply.slot;

    const inner = document.createElement("div");
    inner.className = "card-inner";
    const front = document.createElement("div");
    front.className = "card-face card-front";

    const tag = document.createElement("span");
    tag.className = "slot-tag";
    tag.textContent = "REPLY " + (reply.slot + 1);
    front.appendChild(tag);

    const isDecoy = isReveal && reveal.decoy_slot === reply.slot;
    if (isDecoy) {
      const badge = document.createElement("span");
      badge.className = "robot-badge";
      badge.textContent = "ROBOT";
      front.appendChild(badge);
    }

    // GIF rounds only: human reaction gifs + Imagine decoy loop.
    if (isGifRound) {
      const media = buildReplyMedia(reply, isDecoy, maybeCenter);
      if (media) {
        card.classList.add("has-media");
        front.appendChild(media);
      }
    }

    const text = document.createElement("p");
    text.className = "reply-text";
    text.textContent = reply.text || "";
    front.appendChild(text);

    const author = document.createElement("span");
    author.className = "reply-author";
    // Never tip the decoy during guessing. Only name Grok after reveal.
    if (isDecoy) {
      author.textContent = isGifRound ? "grok imagine · this one" : "grok wrote this one";
    } else if (isReveal && reply.author) {
      author.textContent = reply.author;
    } else {
      author.textContent = "@·····";
    }
    front.appendChild(author);

    if (isDecoy && reveal.rationale) {
      const whyWrap = document.createElement("div");
      whyWrap.className = "rationale-wrap";
      const whyLab = document.createElement("span");
      whyLab.className = "rationale-label";
      whyLab.textContent = "THE TELL";
      const why = document.createElement("p");
      why.className = "rationale";
      why.textContent = reveal.rationale;
      whyWrap.append(whyLab, why);
      front.appendChild(whyWrap);
    }
    if (s.phase === "reveal") {
      // Who picked this reply. The server only exposes guess_slot at reveal,
      // so this can never render during guessing.
      const pickers = (s.players || []).filter((p) => p.guess_slot === reply.slot);
      if (pickers.length) {
        const row = document.createElement("div");
        row.className = "picked-by";
        for (const p of pickers) {
          const chip = document.createElement("span");
          chip.className = "picker-chip" + (p.name === myName ? " me" : "");
          chip.textContent = p.name === myName ? "YOU" : p.name;
          row.appendChild(chip);
        }
        front.appendChild(row);
      }
    }

    const back = document.createElement("div");
    back.className = "card-face card-back";
    const lock = document.createElement("span");
    lock.className = "lock-label";
    lock.textContent = "LOCKED IN";
    back.appendChild(lock);

    inner.append(front, back);
    card.appendChild(inner);

    if (isReveal) {
      card.classList.add(isDecoy ? "is-decoy" : "is-real");
      if (myGuessSlot === reply.slot) {
        card.classList.add("my-pick");
        // Green if you spotted the decoy; red if you picked a human.
        card.classList.add(isDecoy ? "pick-correct" : "pick-wrong");
      }
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

  // Flash who scored this round (+1 each correct pick).
  const flash = $("pointsFlash");
  if (flash) {
    const awarded = s.reveal.points_awarded || [];
    if (!awarded.length) {
      flash.hidden = false;
      flash.textContent = "NO POINTS THIS ROUND";
    } else if (awarded.length === 1) {
      const a = awarded[0];
      flash.hidden = false;
      flash.textContent = (a.name === myName ? "YOU" : a.name)
        + " +" + (a.delta || 1) + " POINT";
    } else {
      flash.hidden = false;
      const names = awarded.map((a) => (a.name === myName ? "YOU" : a.name));
      flash.textContent = names.join(" · ") + " +1 EACH";
    }
  }

  const strip = $("scoreStrip");
  strip.innerHTML = "";
  // Full ranked standings (prefer server standings, then reveal.leaderboard).
  const board = getStandings(s);
  board.forEach((p) => {
    const el = document.createElement("span");
    const isLead = p.rank === 1 && (p.score || 0) > 0;
    el.className = "score"
      + (isLead ? " leader" : "")
      + (p.name === myName ? " is-me" : "");
    const label = document.createElement("span");
    label.textContent = "#" + (p.rank || "?") + " "
      + (p.name === myName ? "YOU" : p.name);
    const b = document.createElement("b");
    b.textContent = (p.score || 0) + " PTS";
    el.append(label, b);
    if ((p.streak || 0) > 1) {
      const st = document.createElement("span");
      st.className = "score-streak";
      st.textContent = p.streak + " streak";
      el.appendChild(st);
    }
    strip.appendChild(el);
  });

  const img = $("shareCard");
  const frame = img && img.parentElement;
  if (img) {
    const url = s.reveal.share_card_url || "";
    const pending = !!s.reveal.share_card_pending;
    if (frame) {
      frame.classList.toggle("is-pending", pending && !!url);
      frame.classList.toggle("is-ready", !pending && !!url);
    }
    if (url) {
      // Only reset src when it changes — avoids flicker/reload on rebroadcast.
      if (img.getAttribute("data-src") !== url) {
        img.setAttribute("data-src", url);
        img.hidden = false;
        img.classList.remove("is-ready");
        img.onload = () => { img.classList.add("is-ready"); };
        img.onerror = () => {
          // Fall back to demo poster if a live path 404s.
          if (url.indexOf("decoy-3f2710c0a9e6_demo") < 0) {
            img.src = "/static-assets/cards/decoy-3f2710c0a9e6_demo.jpg";
            img.setAttribute("data-src", img.src);
          } else {
            img.hidden = true;
          }
        };
        img.src = url;
      } else {
        img.hidden = false;
      }
    } else {
      img.hidden = true;
      img.removeAttribute("data-src");
    }
  }

  const next = $("nextBtn");
  if (next) {
    const final = !!(s.match_over || s.reveal.final_round);
    next.textContent = final ? "SEE RESULTS" : "NEXT ROUND";
  }
}

function renderResults(s) {
  const res = s.results || {};
  const board = (res.standings && res.standings.length)
    ? res.standings
    : getStandings(s);
  const title = $("resultsTitle");
  const sub = $("resultsSub");
  const list = $("resultsBoard");
  if (!list) return;

  const champ = res.champion || null;
  const co = res.co_champions || [];
  const house = !!res.house_wins || (!champ && !co.length);
  const matchN = res.match_rounds || s.match_rounds || board.length || 0;
  const played = res.rounds_played || s.rounds_played || matchN;

  if (title) {
    title.classList.toggle("is-house", house);
    if (house) title.textContent = "HOUSE WINS THE MATCH";
    else if (co.length > 1) title.textContent = "DRAW";
    else if (champ === myName) title.textContent = "YOU WIN THE MATCH";
    else if (champ) title.textContent = String(champ).toUpperCase() + " WINS";
    else title.textContent = "FINAL SCORES";
  }
  if (sub) {
    if (co.length > 1) {
      sub.textContent = "Tied at the top: " + co.join(" · ")
        + " · " + played + "/" + matchN + " rounds";
    } else {
      sub.textContent = played + " of " + matchN + " rounds · correct = +1 pt";
    }
  }

  list.innerHTML = "";
  if (!board.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "NO SCORES";
    list.appendChild(li);
  }
  board.forEach((p) => {
    const li = document.createElement("li");
    if (p.rank === 1 && (p.score || 0) > 0) li.classList.add("is-leader");
    if (p.name === myName) li.classList.add("is-me");
    const rank = document.createElement("span");
    rank.className = "rb-rank";
    rank.textContent = "#" + (p.rank || "—");
    const who = document.createElement("span");
    who.className = "rb-name";
    who.textContent = p.name === myName ? "YOU" : p.name;
    const pts = document.createElement("span");
    pts.className = "rb-pts";
    pts.textContent = (p.score || 0) + " PTS";
    li.append(rank, who, pts);
    list.appendChild(li);
  });
}

/** Leave room and return to the mode picker (HOME). */
function goHome() {
  try { unlockAudio(); } catch (e) { /* ignore */ }
  try { voiceQueue.bump(); commentary.onAdvance(null, null); } catch (e) { /* ignore */ }
  if (joined && myRoom) {
    try { send({ t: "home", room: myRoom }); } catch (e) { /* ignore */ }
  }
  joined = false;
  myName = "";
  myRoom = "";
  iAmHost = false;
  pendingSoloStart = false;
  lockedThemePayload = null;
  wsOutbox.length = 0;
  myGuessSlot = null;
  lastRoundId = null;
  roundNo = 0;
  firstRoundOfSession = true;
  state = null;
  try {
    $("nameInput").disabled = false;
    $("roomInput").disabled = false;
    if ($("createdRoomDisplay")) $("createdRoomDisplay").disabled = false;
    if ($("joinBtn")) $("joinBtn").disabled = false;
    if ($("createEnterBtn")) $("createEnterBtn").disabled = false;
    if ($("modeCreateBtn")) $("modeCreateBtn").disabled = false;
    if ($("modeJoinBtn")) $("modeJoinBtn").disabled = false;
    if ($("modeSoloBtn")) $("modeSoloBtn").disabled = false;
    if ($("modeMockBtn")) $("modeMockBtn").disabled = false;
    const chipBox = $("topicChips");
    if (chipBox) {
      chipBox.querySelectorAll("button").forEach((b) => { b.disabled = false; });
    }
  } catch (e) { /* ignore */ }
  showModePick();
  $("screen-lobby").hidden = false;
  $("screen-game").hidden = true;
  const rs = $("screen-results");
  if (rs) rs.hidden = true;
  $("myPoints").hidden = true;
  $("leadChip").hidden = true;
  $("roundCounter").textContent = "RND 00";
  setConn("HOME");
  // Drop mock socket so a fresh connect is clean.
  if (MOCK) {
    try { sock = mockSocket(handleRaw); } catch (e) { /* ignore */ }
  }
}

function sendRestart() {
  try { unlockAudio(); } catch (e) { /* ignore */ }
  try { voiceQueue.bump(); commentary.onAdvance(null, null); } catch (e) { /* ignore */ }
  firstRoundOfSession = true;
  roundNo = 0;
  lastRoundId = null;
  myGuessSlot = null;
  const payload = { t: "restart", room: myRoom };
  if (iAmHost || lobbyMode === "solo" || isSoloFriendlyRoom(myRoom)) {
    payload.arena = true;
    Object.assign(payload, activeThemePayload());
  }
  send(payload);
}

// ---------- timer (display only, server enforces the real deadline) ----------
function startTimer() {
  cancelAnimationFrame(timerRaf);
  const tick = () => {
    const left = Math.max(0, timerEndAt - performance.now());
    $("timerNum").textContent = String(Math.ceil(left / 1000));
    // Ring length assumes ~30s rounds; clamp so short rounds still draw.
    const total = Math.max(left, 30000);
    $("ringFill").style.strokeDashoffset = String(RING_LEN * (1 - left / total));
    // Visual urgency from 10s — matches the host "ten seconds" line.
    $("timerWrap").classList.toggle("low", left <= 10000 && left > 0);
    // Primary clock_low voice trigger (state broadcasts often miss this window).
    if (left <= 10000 && left > 800) {
      try { commentary.onClockLow(left, state); } catch (e) { /* ignore */ }
    }
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
  // Immediate click-aware voice; cuts any still-playing opener.
  try { commentary.onLocalPick(slot, state); } catch (e) { /* ignore */ }
}

// A scanned QR lands here with ?room=CODE: skip the mode picker and open Join.
const PREFILL_ROOM = (new URLSearchParams(location.search).get("room") || "").toUpperCase();

// Lobby path: null | "create" | "join" | "solo". Prefill forces "join".
let lobbyMode = null;
// True when this client created the room (or is treated as host for START UI).
let iAmHost = false;
// Solo: one-tap START GAME joins a SOLO* room then kicks the first round.
let pendingSoloStart = false;

// A scanned phone gets a generated name too, otherwise "one tap" is a lie.
const HANDLES = ["NEON", "VOLT", "PIXEL", "GHOST", "RELAY", "QUARK", "ORBIT", "FLUX",
                 "VAPOR", "CIPHER", "NOVA", "RIFT", "ECHO", "DRIFT", "PRISM", "ONYX"];
const ROOM_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"; // no 0/O/1/I

function generatedName() {
  const word = HANDLES[Math.floor(Math.random() * HANDLES.length)];
  return word + Math.floor(Math.random() * 90 + 10);
}

/** Short private room codes friends can type (4 chars). */
function generatedRoomCode() {
  let code = "";
  if (window.crypto && crypto.getRandomValues) {
    const buf = new Uint8Array(4);
    crypto.getRandomValues(buf);
    for (let i = 0; i < 4; i++) code += ROOM_ALPHABET[buf[i] % ROOM_ALPHABET.length];
  } else {
    for (let i = 0; i < 4; i++) {
      code += ROOM_ALPHABET[Math.floor(Math.random() * ROOM_ALPHABET.length)];
    }
  }
  return code;
}

/** Solo practice codes: SOLO + 2 chars (server allows 1-player start). */
function generatedSoloRoomCode() {
  let tail = "";
  if (window.crypto && crypto.getRandomValues) {
    const buf = new Uint8Array(2);
    crypto.getRandomValues(buf);
    for (let i = 0; i < 2; i++) tail += ROOM_ALPHABET[buf[i] % ROOM_ALPHABET.length];
  } else {
    for (let i = 0; i < 2; i++) tail += ROOM_ALPHABET[Math.floor(Math.random() * ROOM_ALPHABET.length)];
  }
  return "SOLO" + tail;
}

/** Rooms that may start with a single player (matches server rules). */
function isSoloFriendlyRoom(room) {
  const r = String(room || "").toUpperCase();
  return r === "GROK" || r.indexOf("SOLO") === 0 || MOCK;
}

/** Topic filter for create/solo lobby. Empty = random mix. */
let selectedTopicGroups = []; // catalog group ids, e.g. ["sports","gaming"]
/** Frozen at ENTER / START GAME so reconnect + START always re-send the same filter. */
let lockedThemePayload = null;
let topicCatalog = null; // from GET /topics
// Keep in sync with cartridges/decoy/themes.py TOPIC_CATALOG
const DEFAULT_TOPIC_GROUPS = [
  { id: "random", label: "RANDOM", blurb: "any topic", topics: [], count: 0 },
  { id: "technology", label: "TECHNOLOGY", blurb: "AI · tech · startups · crypto", topics: ["ai", "tech", "startups", "crypto"], count: 0 },
  { id: "movies_tv", label: "MOVIES & TV", blurb: "films · series · streaming", topics: ["movies", "tv"], count: 0 },
  { id: "music", label: "MUSIC", blurb: "artists · albums · concerts", topics: ["music"], count: 0 },
  { id: "gaming", label: "GAMING", blurb: "games · studios · esports", topics: ["gaming"], count: 0 },
  { id: "sports", label: "SPORTS", blurb: "NBA · soccer · baseball", topics: ["sports", "nba", "baseball", "soccer"], count: 0 },
  { id: "science", label: "SCIENCE & SPACE", blurb: "research · NASA · cosmos", topics: ["science", "space"], count: 0 },
  { id: "food", label: "FOOD", blurb: "cooking · restaurants · recipes", topics: ["food"], count: 0 },
  { id: "lifestyle", label: "LIFESTYLE", blurb: "travel · fitness · cars · books", topics: ["travel", "fitness", "cars", "books", "photography"], count: 0 },
];
// Older clients/servers used a mega entertainment bucket.
const LEGACY_TOPIC_GROUP_MAP = { entertainment: "movies_tv" };

function loadTopicCatalog() {
  if (MOCK) {
    topicCatalog = { groups: DEFAULT_TOPIC_GROUPS.slice() };
    renderTopicChips();
    return Promise.resolve(topicCatalog);
  }
  return fetch("/topics")
    .then((r) => (r.ok ? r.json() : null))
    .then((j) => {
      if (j && j.groups && j.groups.length) topicCatalog = j;
      else topicCatalog = { groups: DEFAULT_TOPIC_GROUPS.slice() };
      renderTopicChips();
      return topicCatalog;
    })
    .catch(() => {
      topicCatalog = { groups: DEFAULT_TOPIC_GROUPS.slice() };
      renderTopicChips();
      return topicCatalog;
    });
}

function updateTopicSelectionStatus(serverTopics) {
  const topics = Array.isArray(serverTopics)
    ? serverTopics
    : (lockedThemePayload && lockedThemePayload.topics && lockedThemePayload.topics.length
      ? lockedThemePayload.topics
      : selectedTopicsPayload());
  const label = formatTopicFilterLabel(topics);
  const isRandom = !topics || !topics.length;
  const status = $("topicSelectionStatus");
  if (status) {
    status.innerHTML = isRandom
      ? "SELECTED · <b>RANDOM MIX</b> (every theme)"
      : ("SELECTED · <b>" + label + "</b>");
    status.classList.toggle("is-random", isRandom);
  }
  const chip = $("themeChip");
  if (chip) {
    chip.hidden = false;
    chip.textContent = isRandom ? "THEME · RANDOM" : ("THEME · " + label);
    chip.classList.toggle("is-random", isRandom);
    chip.title = isRandom
      ? "No filter — posts from every theme"
      : ("Only posts in: " + label);
  }
}

function renderTopicChips() {
  const box = $("topicChips");
  if (!box) return;
  const groups = (topicCatalog && topicCatalog.groups) || DEFAULT_TOPIC_GROUPS;
  box.innerHTML = "";
  const isRandom = !selectedTopicGroups.length;
  groups.forEach((g) => {
    const id = String(g.id || "").toLowerCase();
    if (!id) return;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "topic-chip";
    btn.dataset.topicId = id;
    const label = g.label || id.toUpperCase();
    const blurb = g.blurb || (g.topics && g.topics.length ? g.topics.join(" · ") : "");
    btn.innerHTML = blurb
      ? ("<span class=\"topic-chip-label\">" + label + "</span>"
        + "<span class=\"topic-chip-blurb\">" + blurb + "</span>")
      : ("<span class=\"topic-chip-label\">" + label + "</span>");
    const on = id === "random" ? isRandom : selectedTopicGroups.indexOf(id) >= 0;
    if (on) btn.classList.add("is-on");
    btn.setAttribute("aria-pressed", on ? "true" : "false");
    btn.title = blurb ? (label + " — " + blurb) : label;
    btn.addEventListener("click", () => toggleTopicGroup(id));
    box.appendChild(btn);
  });
  updateTopicSelectionStatus();
}

function toggleTopicGroup(id) {
  // Multiplayer host may change themes in lobby after enter; solo locks earlier.
  const mpHostLobby = joined && iAmHost && lobbyMode === "create"
    && state && state.phase === "lobby";
  if (joined && !mpHostLobby) return;
  id = String(id || "").toLowerCase();
  id = LEGACY_TOPIC_GROUP_MAP[id] || id;
  if (!id || id === "random") {
    selectedTopicGroups = [];
    lockedThemePayload = null;
    renderTopicChips();
    if (mpHostLobby) {
      lockThemeFromChips();
      pushThemeToServer();
    }
    return;
  }
  const idx = selectedTopicGroups.indexOf(id);
  if (idx >= 0) selectedTopicGroups.splice(idx, 1);
  else selectedTopicGroups.push(id);
  // Live preview of what will be sent (multi-select = union of member topics).
  lockedThemePayload = null;
  renderTopicChips();
  try {
    const label = formatTopicFilterLabel(selectedTopicsPayload());
    if ($("createHint") && (lobbyMode === "solo" || lobbyMode === "create")) {
      const base = lobbyMode === "solo"
        ? "Practice alone. "
        : (joined
          ? "Waiting for players. "
          : "Share the code (or QR) after you enter. ");
      $("createHint").textContent = selectedTopicGroups.length
        ? (base + "Filter ON · " + label + " — all 6 posts stay in this mix.")
        : (base + "Filter OFF · RANDOM MIX — posts from every theme.");
    }
  } catch (e) { /* ignore */ }
  // Multiplayer host: push live so auto-start / guest lobby see the filter.
  if (mpHostLobby) {
    lockThemeFromChips();
    pushThemeToServer();
  }
}

/** If we locked a theme but the server still shows RANDOM, re-push it. */
let themeResyncTries = 0;
function ensureThemeOnServer(s) {
  if (!joined || !iAmHost || !s) return;
  if (s.phase !== "lobby") return;
  const want = activeThemePayload();
  const wantTopics = (want.topics || []).slice().map((t) => String(t).toLowerCase()).sort();
  if (!wantTopics.length) return; // intentional random
  const got = (s.topic_filter || []).map((t) => String(t).toLowerCase()).sort();
  if (wantTopics.join(",") === got.join(",")) {
    themeResyncTries = 0;
    return;
  }
  if (themeResyncTries >= 4) return;
  themeResyncTries += 1;
  send(Object.assign({ t: "set_topics", room: myRoom, arena: true }, want));
}

/** Resolve chip id → member topic slugs (defaults if catalog is thin). */
function topicsForGroupId(id) {
  id = String(id || "").toLowerCase();
  id = LEGACY_TOPIC_GROUP_MAP[id] || id;
  if (!id || id === "random") return [];
  const groups = (topicCatalog && topicCatalog.groups) || DEFAULT_TOPIC_GROUPS;
  const defaults = DEFAULT_TOPIC_GROUPS;
  let g = null;
  for (let i = 0; i < groups.length; i++) {
    if (String(groups[i].id || "").toLowerCase() === id) {
      g = groups[i];
      break;
    }
  }
  let topics = (g && Array.isArray(g.topics) && g.topics.length) ? g.topics.slice() : null;
  if (!topics) {
    for (let i = 0; i < defaults.length; i++) {
      if (String(defaults[i].id || "").toLowerCase() === id) {
        topics = (defaults[i].topics || []).slice();
        break;
      }
    }
  }
  // Still nothing — send the chip id so the server can expand group ids.
  if (!topics || !topics.length) topics = [id];
  return topics.map((t) => String(t || "").toLowerCase()).filter(Boolean);
}

/** Expand selected group ids → topic slugs for the join/start payload. */
function selectedTopicsPayload() {
  if (!selectedTopicGroups.length) return [];
  const out = [];
  const seen = {};
  selectedTopicGroups.forEach((id) => {
    topicsForGroupId(id).forEach((k) => {
      if (k && !seen[k]) {
        seen[k] = true;
        out.push(k);
      }
    });
  });
  return out;
}

/** Full theme payload: slugs + chip ids (server expands either). */
function themePayloadFields() {
  const topics = selectedTopicsPayload();
  const groups = selectedTopicGroups
    .map((id) => LEGACY_TOPIC_GROUP_MAP[String(id || "").toLowerCase()] || String(id || "").toLowerCase())
    .filter((id) => id && id !== "random");
  return {
    topics: topics.slice(),
    topic_groups: groups.slice(),
    topic_filter: topics.slice(),
  };
}

/** Theme frozen at join, or live chip selection before join. */
function activeThemePayload() {
  if (lockedThemePayload && typeof lockedThemePayload === "object") {
    return {
      topics: (lockedThemePayload.topics || []).slice(),
      topic_groups: (lockedThemePayload.topic_groups || []).slice(),
      topic_filter: (lockedThemePayload.topic_filter || lockedThemePayload.topics || []).slice(),
      _explicitRandom: !!lockedThemePayload._explicitRandom,
    };
  }
  const live = themePayloadFields();
  live._explicitRandom = !(live.topics || []).length;
  return live;
}

/** Snapshot chip selection so later reconnects cannot drop the theme. */
function lockThemeFromChips() {
  lockedThemePayload = themePayloadFields();
  // Remember intentional RANDOM so reconnect can clear; empty without this
  // flag means "don't send topics" (keep server filter).
  lockedThemePayload._explicitRandom = !(lockedThemePayload.topics || []).length;
  themeResyncTries = 0;
  try { updateTopicSelectionStatus(lockedThemePayload.topics); } catch (e) { /* ignore */ }
  return lockedThemePayload;
}

function setTopicChipsEnabled(on) {
  const chipBox = $("topicChips");
  if (!chipBox) return;
  chipBox.querySelectorAll("button").forEach((b) => {
    b.disabled = !on;
  });
}

function formatTopicFilterLabel(topics) {
  if (!topics || !topics.length) return "RANDOM MIX";
  const want = {};
  topics.forEach((t) => { want[String(t || "").toLowerCase()] = true; });
  const groups = (topicCatalog && topicCatalog.groups) || DEFAULT_TOPIC_GROUPS;
  const labels = [];
  const covered = {};
  groups.forEach((g) => {
    const members = (g.topics || []).map((t) => String(t).toLowerCase()).filter(Boolean);
    if (!members.length) return;
    if (members.every((m) => want[m])) {
      labels.push(String(g.label || g.id || "").toUpperCase());
      members.forEach((m) => { covered[m] = true; });
    }
  });
  Object.keys(want).forEach((t) => {
    if (!covered[t]) labels.push(t.toUpperCase());
  });
  return labels.length ? labels.join(" · ") : "RANDOM MIX";
}

function showModePick() {
  lobbyMode = null;
  pendingSoloStart = false;
  lockedThemePayload = null;
  $("modePick").hidden = false;
  $("lobbyForm").hidden = true;
  $("createFields").hidden = true;
  $("joinFields").hidden = true;
  $("lobbyQr").hidden = true;
  $("startBtn").hidden = true;
  $("startBtn").disabled = true;
  $("startBtn").textContent = "START";
  $("waitLine").hidden = true;
  // Restore multiplayer create chrome in case we left solo setup.
  if ($("roomCodeField")) $("roomCodeField").hidden = false;
  if ($("createEnterBtn")) $("createEnterBtn").hidden = false;
}

function showLobbyForm(mode) {
  lobbyMode = mode;
  pendingSoloStart = false;
  $("modePick").hidden = true;
  $("lobbyForm").hidden = false;
  const isSolo = mode === "solo";
  const isCreate = mode === "create" || isSolo;
  $("createFields").hidden = !isCreate;
  $("joinFields").hidden = mode !== "join";
  $("backToModeBtn").hidden = !!PREFILL_ROOM;
  if (!$("nameInput").value.trim()) $("nameInput").value = generatedName();

  // Solo: name + topics + START GAME only (no room code / enter / QR).
  if ($("roomCodeField")) $("roomCodeField").hidden = isSolo;
  if ($("createEnterBtn")) $("createEnterBtn").hidden = isSolo;

  if (isSolo) {
    iAmHost = true;
    lockedThemePayload = null; // fresh pick each solo setup
    const code = generatedSoloRoomCode();
    $("createdRoomDisplay").value = code;
    $("roomInput").value = code;
    $("lobbyQr").hidden = true;
    if ($("createHint")) {
      $("createHint").textContent =
        "Practice alone. Pick themes (or RANDOM), then tap START GAME.";
    }
    if ($("topicFilterHint")) {
      $("topicFilterHint").innerHTML =
        "Optional — every post stays in the themes you pick. Leave <b>RANDOM</b> for the full mix.";
    }
    const start = $("startBtn");
    start.hidden = false;
    start.disabled = false;
    start.textContent = "START GAME";
    if ($("waitLine")) $("waitLine").hidden = true;
    setConn("SOLO PRACTICE · TAP START GAME");
    loadTopicCatalog();
    // Unlock topic chips if returning from a previous run.
    const chipBox = $("topicChips");
    if (chipBox) {
      chipBox.querySelectorAll("button").forEach((b) => { b.disabled = false; });
    }
    return;
  }

  if (mode === "create") {
    const code = generatedRoomCode();
    $("createdRoomDisplay").value = code;
    $("roomInput").value = code;
    iAmHost = true;
    if ($("createHint")) {
      $("createHint").textContent =
        "Share this code (or the QR below) so others can join.";
    }
    if ($("topicFilterHint")) {
      $("topicFilterHint").innerHTML =
        "Pick themes for this room — every post stays in those themes. Leave <b>RANDOM</b> for the full mix.";
    }
    $("startBtn").hidden = true;
    $("startBtn").disabled = true;
    setConn("ROOM " + code + " READY. ENTER WHEN YOU ARE.");
    // Preview QR before entering so host can share immediately.
    $("lobbyQr").hidden = false;
    loadPhoneJoinInfo(code);
    loadTopicCatalog();
  } else {
    iAmHost = false;
    $("lobbyQr").hidden = true;
    $("startBtn").hidden = true;
    $("startBtn").disabled = true;
    if (PREFILL_ROOM) {
      $("roomInput").value = PREFILL_ROOM;
      setConn("JOINING ROOM " + PREFILL_ROOM);
    } else {
      $("roomInput").value = "";
      setConn("ENTER A ROOM CODE TO JOIN");
    }
    try { $("roomInput").focus(); } catch (e) { /* ignore */ }
  }
}

function doJoin(opts) {
  opts = opts || {};
  const asHost = !!opts.asHost;
  const solo = lobbyMode === "solo" || !!opts.solo;
  let name = $("nameInput").value.trim().toUpperCase();
  let room = "";
  if (lobbyMode === "create" || lobbyMode === "solo" || asHost) {
    room = ($("createdRoomDisplay") && $("createdRoomDisplay").value || $("roomInput").value || "")
      .trim().toUpperCase();
  } else {
    room = $("roomInput").value.trim().toUpperCase();
  }
  if (!room) { setConn("ENTER A ROOM CODE"); return; }
  // Never block a player on an empty name. Fill it and let them in.
  if (!name) {
    name = generatedName();
    $("nameInput").value = name;
  }
  myName = name;
  myRoom = room;
  joined = true;
  iAmHost = asHost || lobbyMode === "create" || lobbyMode === "solo" || iAmHost;
  $("nameInput").disabled = true;
  $("roomInput").disabled = true;
  if ($("createdRoomDisplay")) $("createdRoomDisplay").disabled = true;
  if ($("joinBtn")) $("joinBtn").disabled = true;
  if ($("createEnterBtn")) $("createEnterBtn").disabled = true;
  if ($("modeCreateBtn")) $("modeCreateBtn").disabled = true;
  if ($("modeJoinBtn")) $("modeJoinBtn").disabled = true;
  if ($("modeSoloBtn")) $("modeSoloBtn").disabled = true;
  if ($("modeMockBtn")) $("modeMockBtn").disabled = true;
  if ($("backToModeBtn")) $("backToModeBtn").hidden = true;
  // Freeze theme NOW so reconnect keeps it.
  if (iAmHost || solo || lobbyMode === "solo") {
    lockThemeFromChips();
  }

  // Solo: lock chips immediately. Multiplayer host: keep chips editable until
  // START so themes can be set while friends join (was the main MP bug).
  if (solo || isSoloFriendlyRoom(myRoom) || !iAmHost) {
    setTopicChipsEnabled(false);
  } else {
    setTopicChipsEnabled(true);
  }

  // Join (queued if WS not open yet) — carries locked theme for host.
  send(buildJoinPayload());
  // Belt-and-suspenders: also push set_topics right after join.
  if (iAmHost || solo || lobbyMode === "solo") {
    setTimeout(() => { try { pushThemeToServer(); } catch (e) { /* ignore */ } }, 50);
  }

  const themeLabel = formatTopicFilterLabel(
    (lockedThemePayload && lockedThemePayload.topics) || selectedTopicsPayload()
  );

  if (solo || isSoloFriendlyRoom(myRoom)) {
    // Solo practice: no share code / QR — just start.
    $("lobbyQr").hidden = true;
    $("startBtn").hidden = false;
    $("startBtn").disabled = false;
    $("startBtn").textContent = "START GAME";
    if ($("waitLine")) $("waitLine").hidden = true;
    setConn("SOLO · " + themeLabel + " · STARTING…");
    return;
  }

  // Multiplayer hosts see QR + START; guests wait.
  $("lobbyQr").hidden = false;
  loadPhoneJoinInfo(myRoom);
  if (iAmHost) {
    $("startBtn").hidden = false;
    $("startBtn").disabled = false;
    $("waitLine").hidden = true;
    if ($("createHint")) {
      $("createHint").textContent =
        "Waiting for players. Confirm themes (chips stay editable), then START. Filter: " + themeLabel + ".";
    }
    setConn("ROOM " + myRoom + " · " + themeLabel + " · TAP START");
  } else {
    $("startBtn").hidden = true;
    $("startBtn").disabled = true;
    $("waitLine").hidden = false;
    $("waitLine").textContent = "IN " + myRoom + " · WAITING FOR HOST TO START…";
    setConn("JOINED " + myRoom + ". WAIT FOR HOST.");
  }
}

/** Solo CTA: join a private SOLO* room and start the first round. */
function beginSoloGame() {
  if (joined) {
    sendNext();
    return;
  }
  if (!$("nameInput").value.trim()) $("nameInput").value = generatedName();
  // Fresh code each run so a prior SOLO room state does not stick.
  const code = generatedSoloRoomCode();
  $("createdRoomDisplay").value = code;
  $("roomInput").value = code;
  lobbyMode = "solo";
  // Lock chips → theme before join so the first message is correct.
  lockThemeFromChips();
  pendingSoloStart = true;
  $("startBtn").disabled = true;
  $("startBtn").textContent = "STARTING…";
  doJoin({ asHost: true, solo: true });
}

// Mode picker — unlock audio on every lobby click (autoplay policy).
function withAudioUnlock(fn) {
  return (ev) => {
    try { unlockAudio(); } catch (e) { /* ignore */ }
    return fn(ev);
  };
}
$("modeCreateBtn").addEventListener("click", withAudioUnlock(() => showLobbyForm("create")));
$("modeJoinBtn").addEventListener("click", withAudioUnlock(() => showLobbyForm("join")));
$("modeSoloBtn").addEventListener("click", withAudioUnlock(() => {
  // Solo setup: name + topics + START GAME only (no room code / enter / QR).
  if (!$("nameInput").value.trim()) $("nameInput").value = generatedName();
  showLobbyForm("solo");
}));
$("modeMockBtn").addEventListener("click", withAudioUnlock(() => {
  const url = new URL(location.href);
  url.searchParams.set("mock", "1");
  url.searchParams.delete("room");
  location.href = url.toString();
}));
$("backToModeBtn").addEventListener("click", () => {
  if (joined) return;
  showModePick();
  setConn("LINKED");
});
$("createEnterBtn").addEventListener("click", withAudioUnlock(() => {
  if (!joined) doJoin({ asHost: true });
}));
$("copyRoomBtn").addEventListener("click", () => {
  const code = ($("createdRoomDisplay").value || "").trim();
  if (!code) return;
  const done = () => {
    $("copyRoomBtn").textContent = "COPIED";
    setTimeout(() => { $("copyRoomBtn").textContent = "COPY"; }, 1200);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(code).then(done).catch(() => {
      try {
        $("createdRoomDisplay").select();
        document.execCommand("copy");
        done();
      } catch (e) { /* ignore */ }
    });
  } else {
    try {
      $("createdRoomDisplay").select();
      document.execCommand("copy");
      done();
    } catch (e) { /* ignore */ }
  }
});

// Prefill from QR / deep link → join flow only.
if (PREFILL_ROOM) {
  showLobbyForm("join");
  $("roomInput").value = PREFILL_ROOM;
  if (!$("nameInput").value.trim()) $("nameInput").value = generatedName();
  iAmHost = false;
} else {
  showModePick();
}

// Form submit = join path (Enter / Go on phone keyboard).
$("lobbyForm").addEventListener("submit", (ev) => {
  ev.preventDefault();
  if (joined) return;
  if (lobbyMode === "solo") {
    beginSoloGame();
    return;
  }
  if (lobbyMode === "create") doJoin({ asHost: true });
  else doJoin({ asHost: false });
});

// The contract has no separate start message. "next" from the lobby kicks
// off the first round, the same way it advances rounds after a reveal.
function sendNext() {
  try { unlockAudio(); } catch (e) { /* ignore */ }
  // Player moved on — cut leftover reveal/hype immediately.
  try { voiceQueue.bump(); commentary.onAdvance(null, null); } catch (e) { /* ignore */ }
  const payload = { t: "next", room: myRoom };
  // Re-assert theme on START so multiplayer never deals a random mix.
  if (iAmHost || lobbyMode === "solo" || isSoloFriendlyRoom(myRoom)) {
    // Re-read chips if host is still in lobby (multiplayer editable chips).
    if (!state || state.phase === "lobby") {
      lockThemeFromChips();
      pushThemeToServer();
      setTopicChipsEnabled(false);
    }
    payload.arena = true;
    const theme = activeThemePayload();
    Object.assign(payload, theme);
    if (theme._explicitRandom || !(theme.topics || []).length) {
      payload.clear_topics = true;
      payload.topics_random = true;
    }
  }
  send(payload);
}
$("startBtn").addEventListener("click", withAudioUnlock(() => {
  if (lobbyMode === "solo" && !joined) {
    beginSoloGame();
    return;
  }
  sendNext();
}));
$("nextBtn").addEventListener("click", () => sendNext());
if ($("restartBtn")) {
  $("restartBtn").addEventListener("click", withAudioUnlock(() => sendRestart()));
}
if ($("homeBtn")) {
  $("homeBtn").addEventListener("click", withAudioUnlock(() => goHome()));
}
if ($("brandHome")) {
  $("brandHome").addEventListener("click", withAudioUnlock(() => goHome()));
}
// Join form submit also counts as a user gesture for audio.
$("lobbyForm").addEventListener("submit", () => { try { unlockAudio(); } catch (e) { /* ignore */ } }, true);

/** Populate QR + copyable URL for phone players on the same Wi‑Fi. */
function loadPhoneJoinInfo(room) {
  const code = (room || "GROK").toUpperCase();
  const label = $("qrLabel");
  if (label) label.textContent = "SCAN TO PLAY · ROOM " + code;
  fetch("/join-info?room=" + encodeURIComponent(code))
    .then((r) => r.json())
    .then((j) => {
      if (!j) return;
      const primary = j.primary || (j.urls && j.urls[0]) || "";
      const urlEl = $("joinUrl");
      const hint = $("joinHint");
      const img = $("qrImg");
      if (primary && urlEl) {
        urlEl.hidden = false;
        urlEl.textContent = primary;
      }
      if (img && j.qr_path) {
        img.src = j.qr_path + (j.qr_path.indexOf("?") >= 0 ? "&" : "?") + "t=" + Date.now();
        img.alt = "scan to join room " + code;
      }
      if (hint) {
        if (j.localhost_only) {
          hint.textContent =
            "Open this page as http://<your-laptop-ip>:8787 (not localhost), same Wi‑Fi as the phones, then rescan. Or set ARCADE_PUBLIC_URL.";
        } else {
          hint.textContent =
            "Share code " + code + " or scan the QR. Same Wi‑Fi. Room " + code + ".";
        }
      }
    })
    .catch(() => { /* offline static qr.png still shows */ });
}

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

  const MATCH_ROUNDS = 6; // same length as live multiplayer
  let phase = "lobby";
  let players = [];
  let ri = -1;
  let roundsPlayed = 0;
  let roundStart = 0;
  let reveal = null;
  let results = null;
  let deadlineTimer = 0;
  let botTimer = 0;
  let autoTimer = 0;
  const emit = (obj) => setTimeout(() => onMessage(JSON.stringify(obj)), 40);

  function publicRound() {
    if (ri < 0 || phase === "results" || phase === "lobby") return null;
    const r = JSON.parse(JSON.stringify(ROUNDS[ri % ROUNDS.length]));
    if (phase === "guessing") {
      delete r.decoy_slot;
      delete r.decoy_rationale;
      for (const rep of r.replies) { delete rep.is_decoy; delete rep.author; }
    }
    return r;
  }
  function mockStandings() {
    const rows = players.slice().sort((a, b) => (b.score || 0) - (a.score || 0)
      || String(a.name).localeCompare(String(b.name)));
    return rows.map((p, i) => ({
      rank: i + 1,
      name: p.name,
      score: p.score || 0,
      streak: p.streak || 0,
    }));
  }
  function push() {
    const left = phase === "guessing" ? Math.max(0, 30000 - (Date.now() - roundStart)) : 0;
    emit({
      t: "state",
      room: myRoom || "MOCK",
      phase,
      players,
      standings: mockStandings(),
      round: publicRound(),
      reveal,
      results,
      deadline_ms: left,
      match_rounds: MATCH_ROUNDS,
      rounds_played: roundsPlayed,
      match_over: phase === "results" || (phase === "reveal" && roundsPlayed >= MATCH_ROUNDS),
    });
  }
  function currentRound() { return ROUNDS[ri % ROUNDS.length]; }

  function startRound() {
    if (roundsPlayed >= MATCH_ROUNDS && phase !== "lobby") {
      enterResults();
      return;
    }
    if (phase === "results" || (phase === "lobby" && roundsPlayed >= MATCH_ROUNDS)) {
      roundsPlayed = 0;
      for (const p of players) { p.score = 0; p.streak = 0; }
    }
    ri += 1;
    roundsPlayed += 1;
    phase = "guessing";
    reveal = null;
    results = null;
    roundStart = Date.now();
    for (const p of players) p.guessed = false;
    clearTimeout(deadlineTimer);
    clearTimeout(botTimer);
    clearTimeout(autoTimer);
    deadlineTimer = setTimeout(() => doReveal(null), 30000);
    // No GLITCH bot — mock/solo is you vs the house only.
    push();
  }

  function doReveal(winner) {
    if (phase !== "guessing") return;
    phase = "reveal";
    clearTimeout(deadlineTimer);
    clearTimeout(botTimer);
    clearTimeout(autoTimer);
    const r = currentRound();
    const points_awarded = [];
    for (const p of players) {
      if (winner && p.name === winner) {
        p.score = (p.score || 0) + 1;
        p.streak = (p.streak || 0) + 1;
        points_awarded.push({ name: p.name, delta: 1, reason: "first_correct" });
      } else {
        p.streak = 0;
      }
    }
    const board = mockStandings();
    const finalRound = roundsPlayed >= MATCH_ROUNDS;
    reveal = {
      decoy_slot: r.decoy_slot,
      rationale: r.decoy_rationale,
      winner: winner || "house",
      leaderboard: board.slice(0, 5),
      points_awarded,
      share_card_url: "static-assets/cards/decoy-3f2710c0a9e6_demo.jpg",
      final_round: finalRound,
      match_rounds: MATCH_ROUNDS,
      rounds_played: roundsPlayed,
    };
    push();
    autoTimer = setTimeout(() => {
      if (finalRound) enterResults();
      else startRound();
    }, 4000);
  }

  function enterResults() {
    phase = "results";
    reveal = null;
    clearTimeout(autoTimer);
    const board = mockStandings();
    const top = board[0];
    const champ = top && (top.score || 0) > 0 ? top.name : null;
    results = {
      standings: board,
      champion: champ,
      co_champions: [],
      rounds_played: roundsPlayed,
      match_rounds: MATCH_ROUNDS,
      house_wins: !champ,
    };
    push();
  }

  function restartMatch() {
    roundsPlayed = 0;
    results = null;
    reveal = null;
    for (const p of players) {
      p.score = 0;
      p.streak = 0;
      p.guessed = false;
    }
    phase = "lobby";
    startRound();
  }

  return {
    readyState: 1, // OPEN — so the shared send() outbox flushes
    send(text) {
      const m = JSON.parse(text);
      if (m.t === "join") {
        if (!players.find((p) => p.name === m.name)) {
          players.push({ name: m.name, score: 0, streak: 0, guessed: false });
        }
        // Never inject a fake "GLITCH" bot — solo and mock are you vs the house.
        players = players.filter((p) => String(p.name || "").toUpperCase() !== "GLITCH");
        push();
      } else if (m.t === "next") {
        if (phase === "results") restartMatch();
        else if (phase === "reveal" && roundsPlayed >= MATCH_ROUNDS) enterResults();
        else startRound();
      } else if (m.t === "restart") {
        restartMatch();
      } else if (m.t === "home") {
        players = players.filter((p) => p.name !== myName);
        phase = "lobby";
        results = null;
        reveal = null;
        push();
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
