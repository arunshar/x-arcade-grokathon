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
const HOST_LINES = {
  intro: "Welcome to the arcade. Tonight, one of the players at this cabinet is not a player at all.",
  round: "Four humans. One machine. Thirty seconds.",
  reveal: "Hands off the buttons. The decoy was...",
  win: "Got it! The machine never stood a chance.",
  lose: "Wrong! The machine walks free. House wins.",
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
  stop() {
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
  playUrl(url) {
    return new Promise((resolve, reject) => {
      this.stop();
      this.url = url;
      if (!this.el) this.el = new Audio();
      const a = this.el;
      a.onended = () => resolve();
      a.onerror = () => reject(new Error("audio element failed"));
      a.src = url;
      a.preload = "auto";
      const p = a.play();
      if (p && p.catch) p.catch(reject);
    });
  },
};

/**
 * Serial voice queue with epoch cancel.
 * When the game moves on (new round / phase), bump() hard-cuts audio and
 * drops every pending job from the old moment so lines never lag behind.
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
  /**
   * @param {() => Promise<void>|void} job
   * @param {{ phase?: string, roundId?: string|null, priority?: number }} [meta]
   */
  enqueue(job, meta) {
    const m = Object.assign({ epoch: this.epoch, phase: null, roundId: null }, meta || {});
    this.items.push({ run: job, meta: m });
    // Keep the lane snappy — drop oldest non-playing jobs first.
    while (this.items.length > 4) this.items.shift();
    this.kick();
  },
  _stale(meta) {
    if (!meta) return true;
    if (meta.epoch !== this.epoch) return true;
    if (meta.phase && state && state.phase && meta.phase !== state.phase) return true;
    const liveId = state && state.round && state.round.round_id;
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
        this.running = false;
        // If epoch moved mid-clip, don't chain old work.
        if (ep !== this.epoch) {
          this.items = this.items.filter((i) => i.meta.epoch === this.epoch);
        }
        this.kick();
      });
  },
};

function voiceMeta(extra) {
  const roundId = state && state.round && state.round.round_id ? state.round.round_id : null;
  const phase = state && state.phase ? state.phase : null;
  return Object.assign({ phase: phase, roundId: roundId }, extra || {});
}

/**
 * Grok Voice TTS via our server (POST /tts → Eve mp3).
 * Works whenever ARCADE_MODE=live + XAI_API_KEY, no realtime socket required.
 */
function speakGrokTts(text) {
  return (async () => {
    const line = String(text || "").trim();
    if (!line) return;
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
    const url = URL.createObjectURL(blob);
    // voiceBus.playUrl takes ownership of stop/revoke on next play; revoke after.
    try {
      await voiceBus.playUrl(url);
    } finally {
      // playUrl already revoked on next stop; if ended cleanly, revoke now.
      try { URL.revokeObjectURL(url); } catch (e) { /* ignore */ }
      if (voiceBus.url === url) voiceBus.url = null;
    }
  })();
}

/** Prefer Grok Voice TTS (stable) then realtime, then browser. Never overlaps. */
async function speakWithGrok(text) {
  const line = String(text || "").trim();
  if (!line || muted) return;
  // Prefer /tts — one clip at a time on voiceBus. Realtime is easy to stack.
  if (arcadeMode === "live" && !MOCK) {
    try {
      await speakGrokTts(line);
      return "tts";
    } catch (e) { /* try realtime */ }
    if (!liveVoice.disabled) {
      try {
        const ok = await warmLiveVoice();
        if (ok) {
          await speakLiveText(line);
          return "realtime";
        }
      } catch (e2) { /* browser */ }
    }
  }
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
    // Fresh Audio(src) each time — cloneNode of <audio> often fails silently.
    const clip = new Audio(url);
    clip.preload = "auto";
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      resolve();
    };
    clip.onended = finish;
    clip.onerror = finish;
    // Safety: never hang the voice queue if ended event is missed.
    const watchdog = setTimeout(finish, 12000);
    const clearWd = () => clearTimeout(watchdog);
    clip.addEventListener("ended", clearWd, { once: true });
    clip.addEventListener("error", clearWd, { once: true });

    voiceBus.stop();
    voiceBus.el = clip;
    const p = clip.play();
    if (p && p.then) {
      p.then(() => { /* playing */ }).catch(() => {
        // Autoplay still blocked — resolve so queue continues; user can click again.
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
    this.gen += 1;
  },

  /** Game advanced — invalidate in-flight agent calls and old speech. */
  onAdvance(phase, roundId) {
    this.gen += 1;
    this.dropPending = false;
    this.activePhase = phase || null;
    this.activeRoundId = roundId || null;
    this.lowClockSaid = false;
  },

  canSpeak() {
    if (muted || !this.enabled) return false;
    // Solo practice always gets commentary on this machine.
    if (typeof myRoom === "string" && isSoloFriendlyRoom(myRoom)) return true;
    if (this.hostOnly && !iAmHost && !MOCK) return false;
    return true;
  },

  stillCurrent(gen, phase, roundId) {
    if (gen !== this.gen) return false;
    if (this.dropPending) return false;
    if (phase && state && state.phase && phase !== state.phase) return false;
    const liveId = state && state.round && state.round.round_id;
    if (roundId && liveId && roundId !== liveId) return false;
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
      if (!w || w === "house") {
        return this._rotate([
          decoy ? ("House wins. Decoy was reply " + decoy + ".") : "House takes it.",
          decoy ? ("Nobody had it. Fake hid in reply " + decoy + ".") : "House keeps the point.",
          "Machine walks. House cashes.",
        ], rn + (decoy || 0));
      }
      const who = w === myName ? "You" : w;
      return this._rotate([
        decoy ? (who + " called it — decoy was reply " + decoy + ".") : (who + " called it! Plus one."),
        decoy ? (who + " sniffs out reply " + decoy + ". Plus one.") : (who + " takes the round."),
        decoy ? ("Point to " + who + ". Fake lived in reply " + decoy + ".") : ("Board goes to " + who + "."),
      ], rn + String(w).length + (decoy || 0));
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
    const roundId = (s && s.round && s.round.round_id) || (state && state.round && state.round.round_id) || null;

    const run = async () => {
      if (!this.stillCurrent(gen, phase, roundId) || !this.canSpeak()) return;
      this.inFlight += 1;
      let got = null;
      try {
        got = await this.askAgent(event, s, extra);
      } finally {
        this.inFlight -= 1;
      }
      if (!got || !got.line) return;
      // Only speak if we are still on this moment.
      if (!this.stillCurrent(gen, phase, roundId) || !this.canSpeak() || muted) return;
      const line = got.line;
      // Skip near-duplicates of something we just said.
      const low = line.toLowerCase();
      if (this.recentLines.some((r) => r.toLowerCase() === low)) return;
      voiceQueue.enqueue(async () => {
        if (!this.stillCurrent(gen, phase, roundId) || !this.canSpeak() || muted) return;
        await speakWithGrok(line);
        this.rememberLine(line);
      }, voiceMeta({ phase: phase, roundId: roundId }));
    };

    // Always async — round path never awaits this.
    const delay = opts.delayMs || 0;
    if (delay > 0) {
      setTimeout(() => {
        if (this.stillCurrent(gen, phase, roundId)) run();
      }, delay);
    } else {
      Promise.resolve().then(run);
    }
  },

  /** Local player clicked a reply card — cut openers; mp3 path unchanged. */
  onLocalPick(slot, s) {
    if (!this.canSpeak()) return;
    const replyNo = (typeof slot === "number") ? slot + 1 : null;
    const snap = s || state;
    // Cut leftover hype so the click feels instant; don't block on agent.
    voiceQueue.bump();
    this.gen = voiceQueue.epoch;
    // Async agent color only — if reveal arrives first, stillCurrent drops it.
    this.comment("player_pick", snap, {
      picker: myName,
      pick_reply: replyNo,
      just_locked: [myName],
    });
  },

  onState(s, was) {
    if (!this.canSpeak()) return;
    const board = getStandings(s);
    const players = s.players || [];
    const leader = board[0] && (board[0].score || 0) > 0 ? board[0].name : null;

    if (s.phase === "lobby") {
      const n = players.length;
      if (n > this.lastLobbyCount && n >= 1 && joined) {
        this.comment("lobby_join", s, null, { delayMs: 200 });
      }
      this.lastLobbyCount = n;
      this.lastGuessed = {};
      this.lowClockSaid = false;
    }

    if (s.phase === "guessing" && was !== "guessing") {
      this.lowClockSaid = false;
      this.lastGuessed = {};
    }

    if (s.phase === "guessing") {
      for (const p of players) {
        if (p.guessed && !this.lastGuessed[p.name]) {
          this.lastGuessed[p.name] = true;
          // Local pick already spoke via onLocalPick — only call out opponents.
          if (p.name !== myName) {
            this.comment("player_lock", s, { just_locked: [p.name], picker: p.name });
          }
        }
      }
      const left = typeof s.deadline_ms === "number" ? s.deadline_ms : null;
      if (left !== null && left <= 10000 && left > 0 && !this.lowClockSaid) {
        this.lowClockSaid = true;
        this.comment("clock_low", s);
      }
    }

    if (s.phase === "reveal" && was !== "reveal") {
      const w = s.reveal && s.reveal.winner;
      const mySlot = myGuessSlot;
      const decoy = s.reveal && typeof s.reveal.decoy_slot === "number" ? s.reveal.decoy_slot : null;
      const correct = (w && w === myName) || (decoy !== null && mySlot === decoy);
      // One reveal line after outcome stinger (same queue, same epoch).
      this.comment("reveal", s, {
        winner: w || "house",
        pick_reply: (typeof mySlot === "number") ? mySlot + 1 : null,
        picker: myName,
        correct: correct,
      });
      this.lastLeader = leader;
    }

    if (s.phase !== "lobby") this.lastLobbyCount = players.length;
  },

  afterHostStinger(s) {
    if (!s || !this.canSpeak()) return;
    if (state && state.phase !== "guessing") return;
    this.comment("round_start", s);
  },

  /**
   * Fresh opener every round: agent first (time-capped), else rotated template
   * spoken via Grok Voice — never the same stock mp3 line on loop.
   */
  openRound(s) {
    if (!this.canSpeak() || !s) return;
    const gen = this.gen;
    const phase = "guessing";
    const roundId = s.round && s.round.round_id ? s.round.round_id : null;
    const fallback = this.templateLine("round_start", s, {});

    // Always have a unique fallback ready on the queue so audio isn't silent
    // if the model is slow — but agent may replace flavor if it returns first.
    let spoken = false;
    const speakOnce = async (line, source) => {
      if (spoken) return;
      if (!this.stillCurrent(gen, phase, roundId) || !this.canSpeak() || muted) return;
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
        new Promise((resolve) => setTimeout(() => resolve({ t: "timeout" }), 900)),
      ]);
      if (!this.stillCurrent(gen, phase, roundId) || muted) return;
      if (raced.t === "agent" && raced.got && raced.got.line) {
        const src = raced.got.source ? String(raced.got.source) : "";
        // Prefer real agent lines; fallbacks are already rotated templates.
        await speakOnce(raced.got.line, src || "agent");
        return;
      }
      // Agent still running: use template now; ignore late agent (spoken gate).
      await speakOnce(fallback, "fallback_rotate");
      agentPromise.then((got) => {
        // Late agent — only queue if still same round and line is fresh.
        if (spoken) return;
        if (!got || !got.line) return;
        if (!this.stillCurrent(gen, phase, roundId)) return;
        voiceQueue.enqueue(async () => {
          await speakOnce(got.line, got.source || "agent_late");
        }, voiceMeta({ phase: phase, roundId: roundId }));
      }).catch(() => {});
    }, voiceMeta({ phase: phase, roundId: roundId }));
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
    if (joined) send({ t: "join", room: myRoom, name: myName, arena: iAmHost });
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
    const winner = s.reveal && s.reveal.winner;
    const outcome = (!winner || winner === "house") ? "lose" : "win";
    // Keep win/lose mp3 as hard sting; skip repeating reveal mp3 on solo.
    // Varied roast comes from the agent after (async).
    if (isSoloFriendlyRoom(myRoom)) {
      playHost(outcome);
    } else {
      playHost(outcome);
    }
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
    try { playHost("win"); } catch (e) { /* ignore */ }
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
  if (foot) {
    const n = board.length;
    foot.textContent = n
      ? (n + " PLAYER" + (n === 1 ? "" : "S") + " · +1 FIRST CORRECT")
      : "+1 FIRST CORRECT EACH ROUND";
  }

  box.innerHTML = "";
  board.forEach((row) => {
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
  if (board.length === 0) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "NO PLAYERS YET";
    ul.appendChild(li);
  }
  for (const p of board) {
    const li = document.createElement("li");
    if (p.rank === 1 && (p.score || 0) > 0) li.classList.add("is-leader");
    if (p.name === myName) li.classList.add("is-me");

    const rank = document.createElement("span");
    rank.className = "rank";
    rank.textContent = "#" + (p.rank || "—");

    const who = document.createElement("span");
    who.className = "who";
    who.textContent = p.name + (p.name === myName ? " (YOU)" : "");

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
  const minPlayers = (typeof s.min_players === "number")
    ? s.min_players
    : (isSoloFriendlyRoom(s.room || myRoom) ? 1 : 2);
  const canStart = (typeof s.can_start === "boolean")
    ? s.can_start
    : players.length >= minPlayers;
  if (!joined) {
    start.hidden = true;
    start.disabled = true;
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
  $("postTopic").textContent = topic
    ? (topic + " · " + (fmt === "gif" ? "GIF ROUND" : "TEXT ROUND"))
    : (fmt === "gif" ? "GIF ROUND" : "TEXT ROUND");
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
  for (const p of s.players || []) {
    if (p.name === myName) continue;
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
      const why = document.createElement("p");
      why.className = "rationale";
      why.textContent = reveal.rationale;
      front.appendChild(why);
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
  send({ t: "restart", room: myRoom });
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
  // Immediate click-aware voice; cuts any still-playing opener.
  try { commentary.onLocalPick(slot, state); } catch (e) { /* ignore */ }
}

// A scanned QR lands here with ?room=CODE: skip the mode picker and open Join.
const PREFILL_ROOM = (new URLSearchParams(location.search).get("room") || "").toUpperCase();

// Lobby path: null | "create" | "join". Prefill forces "join".
let lobbyMode = null;
// True when this client created the room (or is treated as host for START UI).
let iAmHost = false;

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
let selectedTopicGroups = []; // catalog group ids, e.g. ["ai","movies"]
let topicCatalog = null; // from GET /topics
const DEFAULT_TOPIC_GROUPS = [
  { id: "random", label: "RANDOM", topics: [], count: 0 },
  { id: "technology", label: "TECHNOLOGY", topics: ["ai", "tech", "startups", "crypto"], count: 0 },
  { id: "entertainment", label: "ENTERTAINMENT", topics: ["movies", "tv", "music", "gaming", "memes"], count: 0 },
  { id: "sports", label: "SPORTS", topics: ["sports", "nba", "baseball", "soccer"], count: 0 },
  { id: "science", label: "SCIENCE & SPACE", topics: ["science", "space"], count: 0 },
  { id: "lifestyle", label: "LIFESTYLE", topics: ["food", "travel", "fitness", "cars", "books", "photography"], count: 0 },
];

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
    btn.textContent = label;
    const on = id === "random" ? isRandom : selectedTopicGroups.indexOf(id) >= 0;
    if (on) btn.classList.add("is-on");
    btn.setAttribute("aria-pressed", on ? "true" : "false");
    btn.addEventListener("click", () => toggleTopicGroup(id));
    box.appendChild(btn);
  });
}

function toggleTopicGroup(id) {
  if (joined) return;
  id = String(id || "").toLowerCase();
  if (!id || id === "random") {
    selectedTopicGroups = [];
    renderTopicChips();
    return;
  }
  const idx = selectedTopicGroups.indexOf(id);
  if (idx >= 0) selectedTopicGroups.splice(idx, 1);
  else selectedTopicGroups.push(id);
  renderTopicChips();
}

/** Expand selected group ids → topic slugs for the join payload. */
function selectedTopicsPayload() {
  if (!selectedTopicGroups.length) return [];
  const groups = (topicCatalog && topicCatalog.groups) || DEFAULT_TOPIC_GROUPS;
  const byId = {};
  groups.forEach((g) => { byId[String(g.id || "").toLowerCase()] = g; });
  const out = [];
  const seen = {};
  selectedTopicGroups.forEach((id) => {
    const g = byId[id];
    const topics = (g && g.topics) || [id];
    topics.forEach((t) => {
      const k = String(t || "").toLowerCase();
      if (k && !seen[k]) { seen[k] = true; out.push(k); }
    });
  });
  return out;
}

function formatTopicFilterLabel(topics) {
  if (!topics || !topics.length) return "RANDOM MIX";
  return topics.map((t) => String(t).toUpperCase()).join(" · ");
}

function showModePick() {
  lobbyMode = null;
  $("modePick").hidden = false;
  $("lobbyForm").hidden = true;
  $("createFields").hidden = true;
  $("joinFields").hidden = true;
  $("lobbyQr").hidden = true;
  $("startBtn").hidden = true;
  $("waitLine").hidden = true;
}

function showLobbyForm(mode) {
  lobbyMode = mode;
  $("modePick").hidden = true;
  $("lobbyForm").hidden = false;
  $("createFields").hidden = mode !== "create";
  $("joinFields").hidden = mode !== "join";
  $("backToModeBtn").hidden = !!PREFILL_ROOM;
  if (!$("nameInput").value.trim()) $("nameInput").value = generatedName();

  if (mode === "create") {
    const code = generatedRoomCode();
    $("createdRoomDisplay").value = code;
    $("roomInput").value = code;
    iAmHost = true;
    setConn("ROOM " + code + " READY. ENTER WHEN YOU ARE.");
    // Preview QR before entering so host can share immediately.
    $("lobbyQr").hidden = false;
    loadPhoneJoinInfo(code);
    loadTopicCatalog();
  } else {
    iAmHost = false;
    $("lobbyQr").hidden = true;
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
  let name = $("nameInput").value.trim().toUpperCase();
  let room = "";
  if (lobbyMode === "create" || asHost) {
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
  iAmHost = asHost || lobbyMode === "create" || iAmHost;
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
  // Lock topic chips after enter.
  const chipBox = $("topicChips");
  if (chipBox) {
    chipBox.querySelectorAll("button").forEach((b) => { b.disabled = true; });
  }

  // arena:true marks creator/host. topics: filter X posts for this room
  // (empty = random mix). Only applied for the creator on the server.
  const payload = { t: "join", room: myRoom, name: myName, arena: iAmHost };
  if (iAmHost) {
    payload.topics = selectedTopicsPayload();
  }
  send(payload);

  // Hosts see QR + START; guests wait.
  $("lobbyQr").hidden = false;
  loadPhoneJoinInfo(myRoom);
  if (iAmHost) {
    $("startBtn").hidden = false;
    $("startBtn").disabled = false;
    $("waitLine").hidden = true;
    if (isSoloFriendlyRoom(myRoom)) {
      // Solo auto-starts on the server; START still works as a manual backup.
      setConn("SOLO " + myRoom + " · ROUND SHOULD START — OR TAP START.");
    } else {
      setConn("ROOM " + myRoom + " · YOU ARE HOST. TAP START WHEN READY.");
    }
  } else {
    $("startBtn").hidden = true;
    $("startBtn").disabled = true;
    $("waitLine").hidden = false;
    $("waitLine").textContent = "IN " + myRoom + " · WAITING FOR HOST TO START…";
    setConn("JOINED " + myRoom + ". WAIT FOR HOST.");
  }
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
  // Real server, one player: SOLO* room auto-starts on enter.
  if (!$("nameInput").value.trim()) $("nameInput").value = generatedName();
  showLobbyForm("create");
  const code = generatedSoloRoomCode();
  $("createdRoomDisplay").value = code;
  $("roomInput").value = code;
  $("createHint").textContent =
    "Solo practice. Pick topics (or RANDOM), then ENTER ROOM — the round starts automatically.";
  setConn("SOLO ROOM " + code + " · ONE PLAYER OK");
  loadPhoneJoinInfo(code);
  loadTopicCatalog();
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
  if (lobbyMode === "create") doJoin({ asHost: true });
  else doJoin({ asHost: false });
});

// The contract has no separate start message. "next" from the lobby kicks
// off the first round, the same way it advances rounds after a reveal.
function sendNext() {
  try { unlockAudio(); } catch (e) { /* ignore */ }
  // Player moved on — cut leftover reveal/hype immediately.
  try { voiceQueue.bump(); commentary.onAdvance(null, null); } catch (e) { /* ignore */ }
  send({ t: "next", room: myRoom });
}
$("startBtn").addEventListener("click", () => sendNext());
$("nextBtn").addEventListener("click", () => sendNext());
if ($("restartBtn")) {
  $("restartBtn").addEventListener("click", withAudioUnlock(() => sendRestart()));
}
if ($("homeBtn")) {
  $("homeBtn").addEventListener("click", withAudioUnlock(() => goHome()));
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

  const MATCH_ROUNDS = 2; // short mock match
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
    send(text) {
      const m = JSON.parse(text);
      if (m.t === "join") {
        if (!players.find((p) => p.name === m.name)) {
          players.push({ name: m.name, score: 0, streak: 0, guessed: false });
        }
        push();
        setTimeout(() => {
          if (!players.find((p) => p.name === "GLITCH")) {
            players.push({ name: "GLITCH", score: 0, streak: 0, guessed: false });
            push();
          }
        }, 900);
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
