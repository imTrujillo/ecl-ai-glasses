/**
 * NAVI — panel /app: WebSocket, gafas, micrófono (toggle por click), ondas.
 *
 * Cambios clave vs v7:
 *   - Botón micrófono ahora es TOGGLE por click (1 click graba, otro click envía).
 *     Evita los bugs de mousedown/mouseup en mouseleave y en touch.
 *   - Logs siempre visibles para depurar (errores de mic, envío, etc.).
 *   - panel-status usa la clase .is-online en vez de innerHTML.
 *   - Mejor mensaje cuando getUserMedia falla (HTTPS / permisos).
 */
(function () {
  "use strict";

  const SAMPLE_RATE = 8000;

  const $ = (id) => document.getElementById(id);

  const els = {
    sessionBtn: $("btn-session"),
    labelSession: $("label-session"),
    recordBtn: $("btn-record"),
    labelRecord: $("label-record"),
    photoBtn: $("btn-photo"),
    photoInput: $("photo-input"),
    announce: $("announce"),
    panelStatus: $("panel-status"),
    vizCard: $("viz-card"),
    vizLabel: $("viz-label"),
    waveCanvas: $("wave-canvas"),
    pairSection: $("pair-section"),
    modeSection: $("mode-section"),
    modeChips: document.querySelectorAll(".mode-chip"),
    log: $("sr-log"),
    logSection: $("log-section"),
  };

  /** Frases cortas para speechSynthesis del navegador (modos / obstáculos). */
  const MODE_SPEAK = {
    assistant: "Modo asistente.",
    ocr: "Modo lectura.",
    describe: "Modo descripción.",
  };

  const IMG_CHUNK = 2048;
  let beepCtx = null;
  let photoBusy = false;

  /** Móvil / ngrok: speechSynthesis suele fallar; el servidor envía TTS corto. */
  const useServerAlerts = (function () {
    const ua = navigator.userAgent || "";
    const mobile = /Android|iPhone|iPad|iPod|Mobile/i.test(ua);
    const ngrok = /ngrok/i.test(location.hostname);
    return mobile || ngrok;
  })();

  /** Sala LiveKit compartida (misma room que bridge + agente). */
  let lkRoom = null;
  let lkConfig = { enabled: false, url: "", room: "gafas-test" };

  let ws = null;
  let pcmBuffer = [];
  let streaming = false;
  let mediaRecorder = null;
  let recordChunks = [];
  let isRecording = false;
  let panelConnected = false;
  let glassesDetected = false;
  let activeMode = "assistant";

  const wave = createWaveVisualizer(els.waveCanvas, els.vizCard, els.vizLabel);

  function createWaveVisualizer(canvas, card, label) {
    const ctx = canvas.getContext("2d");
    let mode = "idle";
    let analyser = null;
    let dataArray = null;
    let audioCtx = null;
    let sourceNode = null;
    let micStream = null;

    const colors = {
      idle: { bar: "#3a4760", glow: "rgba(74, 122, 184, 0.25)" },
      play: { bar: "#7da3d4", glow: "rgba(125, 163, 212, 0.55)" },
      record: { bar: "#f0998a", glow: "rgba(240, 153, 138, 0.55)" },
    };

    function resize() {
      const rect = canvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      const w = Math.max(rect.width || 320, 280);
      canvas.width = Math.floor(w * dpr);
      canvas.height = Math.floor(72 * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    function drawIdle() {
      const w = canvas.clientWidth || 320;
      const h = 72;
      ctx.clearRect(0, 0, w, h);
      const t = Date.now() / 1000;
      const bars = 48;
      const gap = w / bars;
      for (let i = 0; i < bars; i++) {
        const amp = 0.08 + Math.sin(t * 1.6 + i * 0.32) * 0.05;
        const bh = h * amp;
        ctx.fillStyle = colors.idle.bar;
        ctx.fillRect(i * gap + 1, (h - bh) / 2, Math.max(2, gap - 2), bh);
      }
    }

    function drawLive() {
      if (!analyser || !dataArray) {
        drawIdle();
        return;
      }
      analyser.getByteFrequencyData(dataArray);
      const w = canvas.clientWidth || 320;
      const h = 72;
      ctx.clearRect(0, 0, w, h);
      const c = colors[mode] || colors.idle;
      const bars = 44;
      const step = Math.floor(dataArray.length / bars);
      const gap = w / bars;
      for (let i = 0; i < bars; i++) {
        const v = dataArray[i * step] / 255;
        const bh = Math.max(4, v * h * 0.85);
        const grad = ctx.createLinearGradient(0, h, 0, 0);
        grad.addColorStop(0, c.bar);
        grad.addColorStop(1, c.glow);
        ctx.fillStyle = grad;
        ctx.fillRect(i * gap + 1, h - bh, Math.max(2, gap - 2), bh);
      }
    }

    function loop() {
      if (mode === "idle") drawIdle();
      else drawLive();
      requestAnimationFrame(loop);
    }

    function setMode(m, text) {
      mode = m;
      card.classList.remove("viz-card--play", "viz-card--record");
      if (m === "play") card.classList.add("viz-card--play");
      if (m === "record") card.classList.add("viz-card--record");
      if (text) label.textContent = text;
    }

    function attachAnalyser(stream) {
      stopAnalyser();
      audioCtx = audioCtx || new AudioContext();
      if (audioCtx.state === "suspended") audioCtx.resume();
      sourceNode = audioCtx.createMediaStreamSource(stream);
      analyser = audioCtx.createAnalyser();
      analyser.fftSize = 256;
      dataArray = new Uint8Array(analyser.frequencyBinCount);
      sourceNode.connect(analyser);
    }

    function stopAnalyser() {
      if (sourceNode) {
        try {
          sourceNode.disconnect();
        } catch (_) {}
        sourceNode = null;
      }
      analyser = null;
    }

    function stopMic() {
      if (micStream) {
        micStream.getTracks().forEach((t) => t.stop());
        micStream = null;
      }
      stopAnalyser();
    }

    function startMicVisualizer(stream) {
      micStream = stream;
      attachAnalyser(stream);
      setMode("record", "Grabando…");
    }

    async function playPcmAndVisualize(bytes) {
      stopMic();
      /* Detén cualquier reproducción anterior. */
      if (sourceNode && sourceNode.stop) {
        try {
          sourceNode.onended = null;
          sourceNode.stop();
        } catch (_) {}
      }
      setMode("play", "Reproduciendo…");
      try {
        audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
        if (audioCtx.state === "suspended") {
          try { await audioCtx.resume(); } catch (_) {}
        }
        const wav = new Blob([makeWavHeader(bytes.length), bytes], {
          type: "audio/wav",
        });
        const ab = await wav.arrayBuffer();
        let buf;
        try {
          buf = await audioCtx.decodeAudioData(ab);
        } catch (decodeErr) {
          /* Fallback: reproduce con <audio> (Safari iOS a veces falla decodeAudioData u8). */
          const url = URL.createObjectURL(wav);
          const audio = new Audio(url);
          audio.onended = () => {
            URL.revokeObjectURL(url);
            setMode("idle", "En espera");
          };
          await audio.play();
          return;
        }
        sourceNode = audioCtx.createBufferSource();
        sourceNode.buffer = buf;
        analyser = audioCtx.createAnalyser();
        analyser.fftSize = 256;
        dataArray = new Uint8Array(analyser.frequencyBinCount);
        sourceNode.connect(analyser);
        analyser.connect(audioCtx.destination);
        sourceNode.onended = () => {
          setMode("idle", "En espera");
          stopAnalyser();
        };
        sourceNode.start(0);
      } catch (err) {
        console.error("[navi] playPcm error:", err);
        setMode("idle", "En espera");
      }
    }

    /** Llamar en respuesta a un click del usuario para desbloquear autoplay. */
    function primeAudio() {
      try {
        audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
        if (audioCtx.state === "suspended") audioCtx.resume();
      } catch (_) {}
    }

    resize();
    window.addEventListener("resize", resize);
    requestAnimationFrame(loop);

    return {
      setIdle: () => {
        stopMic();
        setMode("idle", "En espera");
      },
      startRecord: startMicVisualizer,
      stopRecord: () => {
        stopMic();
        if (!streaming) setMode("idle", "En espera");
      },
      playPcm: playPcmAndVisualize,
      onStreamStart: () => setMode("play", "Recibiendo voz…"),
      primeAudio,
    };
  }

  function makeWavHeader(numSamples) {
    const dataSize = numSamples;
    const buffer = new ArrayBuffer(44);
    const view = new DataView(buffer);
    const w = (o, s) => {
      for (let i = 0; i < s.length; i++) view.setUint8(o + i, s.charCodeAt(i));
    };
    w(0, "RIFF");
    view.setUint32(4, 36 + dataSize, true);
    w(8, "WAVE");
    w(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, SAMPLE_RATE, true);
    view.setUint32(28, SAMPLE_RATE, true);
    view.setUint16(32, 1, true);
    view.setUint16(34, 8, true);
    w(36, "data");
    view.setUint32(40, dataSize, true);
    return buffer;
  }

  function wsUrl() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${location.host}/ws/phone`;
  }

  function announceA11y(msg) {
    if (!msg) return;
    els.announce.textContent = "";
    requestAnimationFrame(() => {
      els.announce.textContent = msg;
    });
  }

  /** Voz rápida del navegador (modos, obstáculos). */
  function speakLocal(msg) {
    if (!msg || !("speechSynthesis" in window)) return;
    window.speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(msg);
    u.lang = "es";
    u.rate = 1.15;
    window.speechSynthesis.speak(u);
    announceA11y(msg);
  }

  /** Pitido corto al empezar a grabar (sin frase hablada). */
  function playBeep() {
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      beepCtx = beepCtx || new Ctx();
      if (beepCtx.state === "suspended") beepCtx.resume();
      const t0 = beepCtx.currentTime;
      const osc = beepCtx.createOscillator();
      const gain = beepCtx.createGain();
      osc.type = "sine";
      osc.frequency.setValueAtTime(880, t0);
      gain.gain.setValueAtTime(0.0001, t0);
      gain.gain.exponentialRampToValueAtTime(0.22, t0 + 0.01);
      gain.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.09);
      osc.connect(gain);
      gain.connect(beepCtx.destination);
      osc.start(t0);
      osc.stop(t0 + 0.1);
    } catch (_) {}
  }

  function speakMode(mode) {
    if (useServerAlerts) return;
    const key = (mode || "assistant").toLowerCase();
    speakLocal(MODE_SPEAK[key] || `Modo ${key}.`);
  }

  function log(msg) {
    if (!els.log) return;
    els.logSection.hidden = false;
    const line = document.createElement("p");
    line.textContent = `${new Date().toLocaleTimeString()} — ${msg}`;
    els.log.appendChild(line);
    els.log.scrollTop = els.log.scrollHeight;
  }

  function updatePanelStatus() {
    els.panelStatus.classList.toggle("is-online", panelConnected);
    if (!panelConnected) {
      els.panelStatus.innerHTML = "Servidor: <strong>sin conexión</strong>";
      return;
    }
    const g = glassesDetected ? "gafas conectadas" : "esperando gafas";
    els.panelStatus.innerHTML = `Servidor: <strong>conectado</strong> · ${g}`;
  }

  function updatePairUI() {
    const show = panelConnected && glassesDetected;
    els.pairSection.hidden = !show;
    els.pairSection.classList.toggle("is-live", show);
  }

  function setSessionUI(connected) {
    panelConnected = connected;
    els.sessionBtn.classList.toggle("is-connected", connected);
    els.sessionBtn.setAttribute("aria-pressed", connected ? "true" : "false");
    els.labelSession.textContent = connected ? "Desconectar" : "Conectar";

    if (connected) {
      els.recordBtn.classList.remove("action-btn--hidden");
      els.recordBtn.disabled = false;
      els.photoBtn.classList.remove("action-btn--hidden");
      els.photoBtn.disabled = false;
      els.modeSection.hidden = false;
      els.modeChips.forEach((b) => {
        b.disabled = false;
      });
    } else {
      els.recordBtn.classList.add("action-btn--hidden");
      els.recordBtn.disabled = true;
      els.photoBtn.classList.add("action-btn--hidden");
      els.photoBtn.disabled = true;
      els.modeSection.hidden = true;
      els.modeChips.forEach((b) => {
        b.disabled = true;
      });
      glassesDetected = false;
      isRecording = false;
      wave.setIdle();
    }
    updatePanelStatus();
    updatePairUI();
  }

  function setGlassesDetected(on) {
    glassesDetected = on;
    if (!on) {
      els.pairSection.hidden = true;
      els.pairSection.classList.remove("is-live");
    }
    updatePanelStatus();
    updatePairUI();
    if (on) log("ESP32 conectado");
    else log("Gafas desconectadas");
  }

  function setMode(mode) {
    activeMode = mode;
    els.modeChips.forEach((btn) => {
      btn.classList.toggle("is-active", btn.dataset.mode === mode);
    });
  }

  function send(text) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(text);
      return true;
    }
    return false;
  }

  function applyRoomSync(msg) {
    if (msg.startsWith("MODE:") || msg.startsWith("NAVI:MODE:")) {
      const mode = msg.split(":").pop().trim().toLowerCase();
      if (mode && mode !== activeMode) {
        setMode(mode);
        log(`Modo: ${mode} (sala ${lkConfig.room})`);
        speakMode(mode);
      }
      return true;
    }
    if (msg.startsWith("OBSTACLE:") || msg.startsWith("NAVI:OBSTACLE:")) {
      const raw = msg.split(":").pop().trim();
      const cm = parseFloat(raw);
      if (!isFinite(cm) || cm <= 0) return true;
      log(`⚠ Obstáculo a ${Math.round(cm)} cm`);
      if (!useServerAlerts) {
        speakLocal(`Obstáculo a ${Math.round(cm)} centímetros.`);
      }
      flashObstacle(cm);
      return true;
    }
    if (msg === "ESP32:online" || msg === "NAVI:ESP32:online") {
      setGlassesDetected(true);
      return true;
    }
    if (msg === "ESP32:offline" || msg === "NAVI:ESP32:offline") {
      setGlassesDetected(false);
      log("Gafas desconectadas");
      return true;
    }
    return false;
  }

  async function publishRoomData(text) {
    if (!lkRoom?.localParticipant) return;
    try {
      const enc = new TextEncoder();
      await lkRoom.localParticipant.publishData(enc.encode(text), {
        reliable: true,
      });
    } catch (err) {
      log(`Sala LiveKit: ${err.message || err}`);
    }
  }

  async function connectLiveKitRoom() {
    if (!lkConfig.enabled || !lkConfig.livekit_url) return;
    const LK = window.LivekitClient;
    if (!LK || !LK.Room) {
      log("LiveKit client no cargado — solo WebSocket");
      return;
    }
    try {
      const id =
        "panel-" + Math.random().toString(36).slice(2, 9);
      const tokRes = await fetch(
        `/getToken?name=${encodeURIComponent(id)}&room=${encodeURIComponent(lkConfig.room)}`
      );
      if (!tokRes.ok) throw new Error("getToken falló");
      const { token, room } = await tokRes.json();
      const roomObj = new LK.Room({ adaptiveStream: false });
      roomObj.on(LK.RoomEvent.DataReceived, (payload) => {
        const msg = new TextDecoder().decode(payload);
        applyRoomSync(msg);
      });
      roomObj.on(LK.RoomEvent.Disconnected, () => {
        log("Desconectado de la sala LiveKit");
        lkRoom = null;
      });
      await roomObj.connect(lkConfig.livekit_url, token);
      lkRoom = roomObj;
      log(`En sala LiveKit «${room}» (sync con otros dispositivos)`);
    } catch (err) {
      log(`Sala LiveKit: ${err.message || err}`);
      lkRoom = null;
    }
  }

  async function disconnectLiveKitRoom() {
    if (!lkRoom) return;
    try {
      await lkRoom.disconnect();
    } catch (_) {}
    lkRoom = null;
  }

  async function loadNaviConfig() {
    try {
      const r = await fetch("/api/navi/config");
      if (r.ok) lkConfig = await r.json();
    } catch (_) {}
  }

  function toggleSession() {
    /* Click del usuario → desbloquear autoplay del navegador (móvil/Safari). */
    wave.primeAudio && wave.primeAudio();
    if (panelConnected) disconnect();
    else connect();
  }

  function connect() {
    if (ws && ws.readyState === WebSocket.OPEN) return;
    log(`Conectando a ${wsUrl()}`);
    ws = new WebSocket(wsUrl());
    ws.binaryType = "arraybuffer";
    ws.onopen = () => {
      setSessionUI(true);
      const hello = useServerAlerts
        ? "HELLO:web:server-alerts"
        : "HELLO:web:local-alerts";
      send(hello);
      log(
        useServerAlerts
          ? "WebSocket abierto (alertas por audio del servidor)"
          : "WebSocket abierto (alertas por voz del navegador)"
      );
      void (async () => {
        await loadNaviConfig();
        if (lkConfig.enabled) await connectLiveKitRoom();
      })();
    };
    ws.onclose = () => {
      void disconnectLiveKitRoom();
      setSessionUI(false);
      log("WebSocket cerrado");
      ws = null;
    };
    ws.onerror = () => {
      log("Error de conexión — revisa python main.py");
    };
    ws.onmessage = (ev) => {
      if (typeof ev.data === "string") handleText(ev.data);
      else handleBinary(ev.data);
    };
  }

  function disconnect() {
    void disconnectLiveKitRoom();
    if (ws) ws.close();
  }

  function handleBinary(data) {
    if (!streaming) return;
    pcmBuffer.push(new Uint8Array(data));
  }

  function handleText(msg) {
    if (msg === "PING") {
      send("PONG");
      return;
    }
    if (msg === "AUDIO_STREAM") {
      streaming = true;
      pcmBuffer = [];
      wave.onStreamStart();
      return;
    }
    if (msg === "AUDIO_END") {
      streaming = false;
      const total = pcmBuffer.reduce((a, b) => a + b.length, 0);
      const out = new Uint8Array(total);
      let off = 0;
      for (const c of pcmBuffer) {
        out.set(c, off);
        off += c.length;
      }
      pcmBuffer = [];
      if (total > 0) wave.playPcm(out);
      else wave.setIdle();
      return;
    }
    if (msg.startsWith("STATUS:")) {
      log(msg.slice(7));
      return;
    }
    if (msg.startsWith("MODE:") || msg.startsWith("NAVI:MODE:")) {
      applyRoomSync(msg);
      return;
    }
    if (msg.startsWith("PANELS:")) {
      log(`Paneles conectados: ${msg.split(":")[1]}`);
      return;
    }
    if (msg === "ESP32:online") {
      setGlassesDetected(true);
      return;
    }
    if (msg === "ESP32:offline") {
      setGlassesDetected(false);
      log("Gafas desconectadas");
      return;
    }
    if (msg.startsWith("TRANSCRIPT:")) {
      const t = msg.slice(11).trim();
      if (t) {
        log(`Tú: ${t}`);
        showToast({ kind: "you", title: "Tú dijiste", body: t });
      }
      return;
    }
    if (msg.startsWith("REPLY:")) {
      const t = msg.slice(6).trim();
      if (t) {
        log(`NAVI: ${t}`);
        showToast({ kind: "navi", title: "NAVI", body: t, ms: 7500 });
      }
      return;
    }
    if (msg.startsWith("EVENT:")) {
      handleEvent(msg.slice(6));
      return;
    }
    if (msg.startsWith("OBSTACLE:") || msg.startsWith("NAVI:OBSTACLE:")) {
      applyRoomSync(msg);
      return;
    }
    if (msg === "RECORD_BTN") {
      if (!isRecording) startRecord();
      else stopRecord();
      return;
    }
    if (msg === "RECORDING:on") {
      els.vizLabel.textContent = "Enviando…";
      return;
    }
    if (msg === "RECORDING:off") {
      wave.setIdle();
      return;
    }
    if (msg === "PHOTO:on") {
      els.photoBtn.classList.add("is-busy");
      els.vizLabel.textContent = "Enviando foto…";
      return;
    }
    if (msg === "PHOTO:off") {
      els.photoBtn.classList.remove("is-busy");
      photoBusy = false;
      if (!isRecording && !streaming) wave.setIdle();
      return;
    }
    if (msg.startsWith("ERROR:")) {
      log(`Error: ${msg.slice(6)}`);
      announceA11y(msg.slice(6));
      return;
    }
    if (msg === "LIVEKIT:off") {
      lkConfig.enabled = false;
      log("Sin sala LiveKit — sync solo por WebSocket");
      return;
    }
    log(msg);
  }

  function sendMode(mode) {
    setMode(mode);
    speakMode(mode);
    void publishRoomData(`MODE:${mode}`);
    send(`MODE:${mode}`);
  }

  function visionModeForPhoto() {
    if (activeMode === "assistant") return "describe";
    return activeMode;
  }

  async function blobToJpegBytes(blob, maxW) {
    const url = URL.createObjectURL(blob);
    try {
      const img = await new Promise((resolve, reject) => {
        const el = new Image();
        el.onload = () => resolve(el);
        el.onerror = reject;
        el.src = url;
      });
      const scale = Math.min(1, maxW / img.width);
      const w = Math.max(1, Math.round(img.width * scale));
      const h = Math.max(1, Math.round(img.height * scale));
      const canvas = document.createElement("canvas");
      canvas.width = w;
      canvas.height = h;
      const ctx = canvas.getContext("2d");
      ctx.drawImage(img, 0, 0, w, h);
      const jpegBlob = await new Promise((resolve) => {
        canvas.toBlob((b) => resolve(b || blob), "image/jpeg", 0.82);
      });
      return new Uint8Array(await jpegBlob.arrayBuffer());
    } finally {
      URL.revokeObjectURL(url);
    }
  }

  async function sendPhotoBytes(jpegBytes) {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      throw new Error("Sin conexión al servidor");
    }
    const mode = visionModeForPhoto();
    send(`IMG_START:${mode}:${jpegBytes.length}`);
    for (let off = 0; off < jpegBytes.length; off += IMG_CHUNK) {
      const slice = jpegBytes.subarray(off, off + IMG_CHUNK);
      ws.send(slice);
      await new Promise((r) => setTimeout(r, 0));
    }
    send("IMG_END");
    log(`Foto enviada (${mode}, ${(jpegBytes.length / 1024).toFixed(1)} kB)`);
    showToast({
      kind: "photo",
      title: "Foto enviada",
      body: `Modo ${mode}`,
      ms: 2800,
    });
  }

  async function captureAndSendPhoto(fileOrBlob) {
    if (!panelConnected || photoBusy) return;
    photoBusy = true;
    els.photoBtn.classList.add("is-busy");
    els.vizLabel.textContent = "Preparando foto…";
    try {
      const bytes = await blobToJpegBytes(fileOrBlob, 640);
      await sendPhotoBytes(bytes);
    } catch (err) {
      log(`Foto: ${err.message || err}`);
      photoBusy = false;
      els.photoBtn.classList.remove("is-busy");
      wave.setIdle();
    }
  }

  function openPhotoPicker() {
    if (!panelConnected || photoBusy) return;
    if (activeMode === "assistant") {
      log("En asistente la foto usa modo descripción.");
    }
    els.photoInput.value = "";
    els.photoInput.click();
  }

  function ensureToastContainer() {
    let host = document.getElementById("toast-host");
    if (!host) {
      host = document.createElement("div");
      host.id = "toast-host";
      host.className = "toast-host";
      document.body.appendChild(host);
    }
    return host;
  }

  function showToast({ kind = "info", title = "", body = "", ms = 4500 }) {
    const host = ensureToastContainer();
    const el = document.createElement("div");
    el.className = `toast toast--${kind}`;
    const icons = {
      you: "🗨",
      navi: "💬",
      photo: "📷",
      record: "🎙",
      think: "✨",
      btn: "👆",
      info: "ℹ",
      error: "⚠",
    };
    el.innerHTML = `
      <span class="toast__icon" aria-hidden="true">${icons[kind] || icons.info}</span>
      <div class="toast__body">
        ${title ? `<strong class="toast__title">${escapeHtml(title)}</strong>` : ""}
        <span class="toast__text">${escapeHtml(body)}</span>
      </div>`;
    host.appendChild(el);
    requestAnimationFrame(() => el.classList.add("is-in"));
    setTimeout(() => {
      el.classList.remove("is-in");
      el.classList.add("is-out");
      setTimeout(() => el.remove(), 320);
    }, ms);
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function handleEvent(ev) {
    if (ev.startsWith("esp-photo-in:")) {
      const [, mode, sz] = ev.split(":");
      showToast({
        kind: "photo",
        title: "Gafas → enviando foto",
        body: `Modo ${mode} · ${(parseInt(sz, 10) / 1024).toFixed(1)} kB`,
        ms: 3000,
      });
      els.vizLabel.textContent = "Recibiendo foto…";
      return;
    }
    if (ev.startsWith("esp-photo-done:")) {
      const [, mode, sz] = ev.split(":");
      showToast({
        kind: "photo",
        title: "Foto recibida",
        body: `Analizando (${mode}, ${(parseInt(sz, 10) / 1024).toFixed(1)} kB)`,
        ms: 4000,
      });
      els.vizLabel.textContent = "Analizando imagen…";
      return;
    }
    if (ev.startsWith("analyzing:")) {
      const mode = ev.split(":")[1];
      els.vizLabel.textContent = `IA pensando (${mode})…`;
      showToast({ kind: "think", title: "NAVI analiza", body: `Modo ${mode}` });
      return;
    }
    if (ev === "thinking") {
      els.vizLabel.textContent = "NAVI pensando…";
      showToast({ kind: "think", title: "NAVI", body: "Pensando respuesta…" });
      return;
    }
    if (ev === "btn-b:record") {
      showToast({
        kind: "btn",
        title: "Gafas: botón B",
        body: "Pediste micrófono",
        ms: 3000,
      });
      return;
    }
    if (ev.startsWith("btn:")) {
      const tag = ev.split(":")[1];
      showToast({
        kind: "btn",
        title: `Gafas: botón ${tag}`,
        body: "Pulsado",
        ms: 2200,
      });
      return;
    }
    if (ev === "no-audio") {
      showToast({
        kind: "error",
        title: "Sin audio",
        body: "El micrófono no captó voz.",
      });
      return;
    }
    if (ev === "error-audio") {
      showToast({ kind: "error", title: "Error", body: "Audio no procesado." });
      return;
    }
    if (ev === "error-image") {
      showToast({ kind: "error", title: "Error", body: "Imagen no procesada." });
      return;
    }
  }

  function flashObstacle(cm) {
    let banner = document.getElementById("obstacle-banner");
    if (!banner) {
      banner = document.createElement("div");
      banner.id = "obstacle-banner";
      banner.className = "obstacle-banner";
      banner.setAttribute("role", "alert");
      document.body.appendChild(banner);
    }
    banner.textContent = `Obstáculo a ${Math.round(cm)} cm`;
    banner.classList.remove("is-visible");
    void banner.offsetWidth;
    banner.classList.add("is-visible");
    clearTimeout(flashObstacle._t);
    flashObstacle._t = setTimeout(() => {
      banner.classList.remove("is-visible");
    }, 3500);
  }

  async function startRecord() {
    if (!panelConnected) {
      log("Conecta el servidor primero.");
      return;
    }
    if (isRecording) return;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      const insecure =
        location.protocol !== "https:" &&
        location.hostname !== "localhost" &&
        location.hostname !== "127.0.0.1";
      const msg = insecure
        ? "El navegador requiere https o localhost para el micrófono."
        : "Tu navegador no soporta micrófono.";
      log(msg);
      return;
    }
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (err) {
      const name = err && err.name ? err.name : "Error";
      if (name === "NotAllowedError" || name === "SecurityError") {
        log("Permiso de micrófono denegado.");
      } else if (name === "NotFoundError") {
        log("No hay micrófono.");
      } else {
        log(`Micrófono (${name}): ${err.message || err}`);
      }
      wave.setIdle();
      return;
    }
    try {
      wave.startRecord(stream);
      recordChunks = [];
      const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
        ? "audio/webm;codecs=opus"
        : "audio/webm";
      mediaRecorder = new MediaRecorder(stream, { mimeType: mime });
      mediaRecorder.ondataavailable = (e) => {
        if (e.data && e.data.size) recordChunks.push(e.data);
      };
      mediaRecorder.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        wave.stopRecord();
        try {
          const blob = new Blob(recordChunks, { type: mime });
          log(`Grabación: ${(blob.size / 1024).toFixed(1)} kB → subiendo…`);
          await uploadAudio(blob);
        } catch (err) {
          log(`Error al subir: ${err.message || err}`);
        }
        isRecording = false;
        mediaRecorder = null;
        els.recordBtn.setAttribute("aria-pressed", "false");
        els.labelRecord.textContent = "Grabar";
      };
      mediaRecorder.start();
      isRecording = true;
      els.recordBtn.setAttribute("aria-pressed", "true");
      els.labelRecord.textContent = "Detener";
      log("Grabando… (toca de nuevo para enviar)");
      playBeep();
      showToast({
        kind: "record",
        title: "Grabando",
        body: "Toca otra vez para enviar.",
        ms: 2800,
      });
    } catch (err) {
      log(`Error al iniciar grabación: ${err.message || err}`);
      try {
        stream.getTracks().forEach((t) => t.stop());
      } catch (_) {}
      wave.setIdle();
    }
  }

  function stopRecord() {
    if (mediaRecorder && mediaRecorder.state === "recording") {
      mediaRecorder.stop();
    } else {
      isRecording = false;
      els.recordBtn.setAttribute("aria-pressed", "false");
      els.labelRecord.textContent = "Grabar";
      wave.setIdle();
    }
  }

  async function uploadAudio(webmBlob) {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      throw new Error("WebSocket cerrado");
    }
    els.vizLabel.textContent = "Procesando…";

    // Usar el AudioContext con sampleRate NATIVO del navegador (forzar 16k rompe
    // decodeAudioData en webm/opus en muchos Chrome). Resamplear manualmente.
    const Ctx = window.AudioContext || window.webkitAudioContext;
    const ctx = new Ctx();
    let audioBuf;
    try {
      const ab = await webmBlob.arrayBuffer();
      audioBuf = await ctx.decodeAudioData(ab.slice(0));
    } catch (err) {
      await ctx.close();
      throw new Error(`No se pudo decodificar el audio: ${err.message || err}`);
    }
    const srcRate = audioBuf.sampleRate;
    const srcCh = audioBuf.getChannelData(0);
    const TARGET_RATE = 16000;
    const resampled = downsampleTo(srcCh, srcRate, TARGET_RATE);
    const pcm16 = floatToPcm16(resampled);
    const wav = buildWav16(pcm16, TARGET_RATE);
    send(`RECORD_START:${wav.byteLength}`);
    ws.send(wav);
    send("RECORD_END");
    log(`Enviado WAV ${(wav.byteLength / 1024).toFixed(1)} kB @ ${TARGET_RATE}Hz`);
    showToast({
      kind: "record",
      title: "Audio enviado",
      body: "Transcribiendo…",
      ms: 2500,
    });
    await ctx.close();
  }

  function downsampleTo(buf, fromRate, toRate) {
    if (toRate >= fromRate) return buf.slice();
    const ratio = fromRate / toRate;
    const newLen = Math.floor(buf.length / ratio);
    const out = new Float32Array(newLen);
    let i = 0;
    let j = 0;
    while (j < newLen) {
      const next = Math.floor((j + 1) * ratio);
      let sum = 0;
      let count = 0;
      for (; i < next && i < buf.length; i++) {
        sum += buf[i];
        count++;
      }
      out[j] = count ? sum / count : 0;
      j++;
    }
    return out;
  }

  function floatToPcm16(buf) {
    const out = new Int16Array(buf.length);
    for (let i = 0; i < buf.length; i++) {
      const s = Math.max(-1, Math.min(1, buf[i]));
      out[i] = s < 0 ? s * 32768 : s * 32767;
    }
    return out;
  }

  function buildWav16(samples, rate) {
    const dataSize = samples.length * 2;
    const buf = new ArrayBuffer(44 + dataSize);
    const view = new DataView(buf);
    const w = (o, s) => {
      for (let i = 0; i < s.length; i++) view.setUint8(o + i, s.charCodeAt(i));
    };
    w(0, "RIFF");
    view.setUint32(4, 36 + dataSize, true);
    w(8, "WAVE");
    w(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, rate, true);
    view.setUint32(28, rate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    w(36, "data");
    view.setUint32(40, dataSize, true);
    let off = 44;
    for (let i = 0; i < samples.length; i++, off += 2) {
      view.setInt16(off, samples[i], true);
    }
    return buf;
  }

  /* ── Eventos ─────────────────────────────────────────── */
  els.sessionBtn.addEventListener("click", toggleSession);

  els.recordBtn.addEventListener("click", () => {
    if (els.recordBtn.disabled) return;
    if (!isRecording) startRecord();
    else stopRecord();
  });

  els.modeChips.forEach((btn) => {
    btn.addEventListener("click", () => {
      if (!panelConnected || btn.disabled) return;
      sendMode(btn.dataset.mode);
    });
  });

  els.photoBtn.addEventListener("click", openPhotoPicker);
  els.photoInput.addEventListener("change", () => {
    const file = els.photoInput.files && els.photoInput.files[0];
    if (file) captureAndSendPhoto(file);
  });

  setSessionUI(false);
  setMode("assistant");
})();
