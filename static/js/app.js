// ASU — NPU Showcase frontend logic
(() => {
  const PERSONA_ENDPOINTS = {
    advisor: "/api/advisor",
    research: "/api/research",
    success: "/api/success",
    financial: "/api/financial",
    career: "/api/career",
    campus: "/api/campus",
  };

  // ---- Tabs ----
  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = btn.dataset.tab;
      document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b === btn));
      document.querySelectorAll(".panel").forEach((p) =>
        p.classList.toggle("active", p.id === `panel-${id}`)
      );
    });
  });

  // ---- Chip fill ----
  document.querySelectorAll(".chip").forEach((c) => {
    c.addEventListener("click", () => {
      const t = document.getElementById(c.dataset.fill);
      if (t) { t.value = c.textContent.trim(); t.focus(); }
    });
  });

  // ---- Submit handlers ----
  document.querySelectorAll("[data-submit]").forEach((btn) => {
    btn.addEventListener("click", () => submitPersona(btn.dataset.submit));
  });

  async function submitPersona(key) {
    const out = document.getElementById(`${key}-output`);
    out.classList.add("thinking");
    out.textContent = "Thinking on-device…";

    let body;
    if (key === "research") {
      body = {
        area: document.getElementById("research-area").value,
        message: document.getElementById("research-input").value,
      };
    } else {
      const input = document.getElementById(`${key}-input`);
      body = { message: input.value };
    }

    try {
      const r = await fetch(PERSONA_ENDPOINTS[key], {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await r.json();
      out.classList.remove("thinking");
      if (data.error) {
        out.innerHTML = `<em>${data.error}</em>`;
        return;
      }
      const meta = `
        <div class="meta">
          <span>⚡ ${data.latency_ms} ms</span>
          <span>🔢 ${data.tokens} tokens</span>
          <span>💻 ${data.hardware}</span>
          <span>💰 saved ${data.cloud_cost_saved}</span>
        </div>`;
      out.innerHTML = escapeHtml(data.response || "[empty response]") + meta;
    } catch (e) {
      out.classList.remove("thinking");
      out.innerHTML = `<em>Network error: ${e.message}</em>`;
    }
  }

  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  // ---- Voice: MediaRecorder + WAV encode + POST to /api/transcribe ----
  document.querySelectorAll("[data-voice-for]").forEach((btn) => {
    let mediaStream = null;
    let audioCtx = null;
    let processor = null;
    let chunks = [];
    let recording = false;

    btn.addEventListener("click", async () => {
      const target = document.getElementById(btn.dataset.voiceFor);
      if (recording) {
        await stopAndTranscribe(target);
        return;
      }
      try {
        mediaStream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
      } catch (e) {
        const msg = (e.name === "NotAllowedError")
          ? "Microphone permission denied — allow it in the browser and try again."
          : (e.name === "NotFoundError")
            ? "No microphone found on this device."
            : "Microphone error: " + (e.message || e.name);
        target.placeholder = msg;
        target.value = "";
        return;
      }
      audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
      const src = audioCtx.createMediaStreamSource(mediaStream);
      processor = audioCtx.createScriptProcessor(4096, 1, 1);
      chunks = [];
      processor.onaudioprocess = (ev) => {
        chunks.push(new Float32Array(ev.inputBuffer.getChannelData(0)));
      };
      src.connect(processor);
      processor.connect(audioCtx.destination);
      recording = true;
      btn.classList.add("recording");
      btn.textContent = "● Stop";
    });

    async function stopAndTranscribe(target) {
      recording = false;
      btn.disabled = true;
      btn.textContent = "⏳ Transcribing…";
      try { processor.disconnect(); } catch {}
      try { mediaStream.getTracks().forEach((t) => t.stop()); } catch {}
      const sr = audioCtx.sampleRate;
      try { await audioCtx.close(); } catch {}

      const total = chunks.reduce((n, a) => n + a.length, 0);
      const all = new Float32Array(total);
      let off = 0;
      for (const c of chunks) { all.set(c, off); off += c.length; }
      const audio16k = (sr === 16000) ? all : resample(all, sr, 16000);
      const wav = encodeWav(audio16k, 16000);
      const fd = new FormData();
      fd.append("audio", new Blob([wav], { type: "audio/wav" }), "rec.wav");
      try {
        const r = await fetch("/api/transcribe", { method: "POST", body: fd });
        const data = await r.json();
        if (data.text) {
          target.value = (target.value ? target.value + " " : "") + data.text.trim();
        } else if (data.error) {
          target.placeholder = "Transcribe error: " + data.error;
        }
      } catch (e) {
        target.placeholder = "Transcribe network error: " + e.message;
      }
      btn.classList.remove("recording");
      btn.disabled = false;
      btn.textContent = "🎤 Voice";
    }
  });

  function resample(buffer, srcRate, dstRate) {
    if (srcRate === dstRate) return buffer;
    const ratio = srcRate / dstRate;
    const newLen = Math.round(buffer.length / ratio);
    const out = new Float32Array(newLen);
    for (let i = 0; i < newLen; i++) {
      const idx = i * ratio;
      const i0 = Math.floor(idx);
      const i1 = Math.min(i0 + 1, buffer.length - 1);
      const frac = idx - i0;
      out[i] = buffer[i0] * (1 - frac) + buffer[i1] * frac;
    }
    return out;
  }

  function encodeWav(samples, sampleRate) {
    const buffer = new ArrayBuffer(44 + samples.length * 2);
    const view = new DataView(buffer);
    const writeStr = (off, s) => { for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i)); };
    writeStr(0, "RIFF");
    view.setUint32(4, 36 + samples.length * 2, true);
    writeStr(8, "WAVE"); writeStr(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    writeStr(36, "data");
    view.setUint32(40, samples.length * 2, true);
    let off = 44;
    for (let i = 0; i < samples.length; i++, off += 2) {
      const s = Math.max(-1, Math.min(1, samples[i]));
      view.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
    }
    return buffer;
  }

  // ---- Vision tabs ----
  const visionPicked = { campus: null, "student-vision": null };

  function bindVisionTab(key, endpoint, category) {
    const dropEl = document.getElementById(`${key}-drop`);
    const fileEl = document.getElementById(`${key}-file`);
    const previewEl = document.getElementById(`${key}-preview`);

    dropEl.addEventListener("click", () => fileEl.click());
    dropEl.addEventListener("dragover", (e) => { e.preventDefault(); dropEl.classList.add("dragover"); });
    dropEl.addEventListener("dragleave", () => dropEl.classList.remove("dragover"));
    dropEl.addEventListener("drop", (e) => {
      e.preventDefault();
      dropEl.classList.remove("dragover");
      const f = e.dataTransfer.files[0];
      if (f) setFile(f);
    });
    fileEl.addEventListener("change", () => {
      if (fileEl.files[0]) setFile(fileEl.files[0]);
    });

    function setFile(f) {
      visionPicked[key] = { type: "file", file: f };
      const reader = new FileReader();
      reader.onload = () => { previewEl.src = reader.result; previewEl.style.display = "block"; };
      reader.readAsDataURL(f);
    }
    function setSample(sample) {
      visionPicked[key] = { type: "sample", name: sample.name };
      previewEl.src = sample.url;
      previewEl.style.display = "block";
    }

    document.querySelector(`[data-vision-submit="${key}"]`).addEventListener("click", () =>
      submitVision(key, endpoint));
    document.querySelector(`[data-vision-clear="${key}"]`).addEventListener("click", () => {
      visionPicked[key] = null;
      previewEl.src = "";
      previewEl.style.display = "none";
      fileEl.value = "";
      const outId = key === "campus" ? "campus-output-vision" : `${key}-output`;
      document.getElementById(outId).innerHTML = "";
    });

    fetch("/api/samples").then((r) => r.json()).then((data) => {
      const gallery = document.getElementById(`${key}-samples`);
      const items = (data.samples || []).filter((s) => s.category === category);
      gallery.innerHTML = items.length ? `<div class="gallery-label">Sample images</div>` +
        items.map((s) => `<div class="thumb" data-name="${s.name}" title="${s.label}"><img src="${s.url}" /></div>`).join("") : "";
      gallery.querySelectorAll(".thumb").forEach((t) => {
        t.addEventListener("click", () => {
          const name = t.dataset.name;
          const s = items.find((x) => x.name === name);
          if (s) setSample(s);
          gallery.querySelectorAll(".thumb").forEach((x) => x.classList.toggle("selected", x === t));
        });
      });
    });
  }

  async function submitVision(key, endpoint) {
    const outId = key === "campus" ? "campus-output-vision" : `${key}-output`;
    const out = document.getElementById(outId);
    const picked = visionPicked[key];
    if (!picked) { out.innerHTML = "<em>Pick a photo first.</em>"; return; }
    out.classList.add("thinking");
    out.textContent = "Analyzing on-device…";
    const notes = (document.getElementById(`${key}-notes`).value || "").trim();
    let resp;
    try {
      if (picked.type === "file") {
        const fd = new FormData();
        fd.append("image", picked.file);
        if (notes) fd.append("message", notes);
        resp = await fetch(endpoint, { method: "POST", body: fd });
      } else {
        resp = await fetch(endpoint, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ sample: picked.name, message: notes }),
        });
      }
      const data = await resp.json();
      out.classList.remove("thinking");
      if (data.error) { out.innerHTML = `<em>${data.error}</em>`; return; }
      const meta = `<div class="meta">
        <span>⚡ ${data.latency_ms} ms</span>
        <span>🔢 ${data.tokens} tokens</span>
        <span>💻 ${data.hardware}</span>
        <span>🧠 ${data.model || "phi-3.5-vision"}</span>
      </div>`;
      out.innerHTML = escapeHtml(data.response || "[empty response]") + meta;
    } catch (e) {
      out.classList.remove("thinking");
      out.innerHTML = `<em>Network error: ${e.message}</em>`;
    }
  }

  bindVisionTab("campus", "/api/vision/campus", "campus");
  bindVisionTab("student-vision", "/api/vision/student", "student");

  // ---- Status pill ----
  async function refreshStatus() {
    try {
      const r = await fetch("/api/status");
      const s = await r.json();
      const dot = document.getElementById("statusDot");
      const text = document.getElementById("statusText");
      dot.classList.remove("ready", "preview");
      if (s.ready) {
        dot.classList.add("ready");
        const extras = [];
        if (s.voice_device && s.voice_device !== "not loaded") extras.push(`🎤 ${s.voice_device}`);
        else if (s.voice_model) extras.push("🎤 loading…");
        if (s.vision_device && s.vision_device !== "not loaded") extras.push(`📷 ${s.vision_device}`);
        else if (s.vision_model) extras.push("📷 loading…");
        const extra = extras.length ? " · " + extras.join(" · ") : "";
        text.textContent = `${s.hardware} · ${s.model} · ${s.runtime}${extra}`;
      } else {
        dot.classList.add("preview");
        text.textContent = s.message || "UI preview (no NPU)";
      }
    } catch {
      document.getElementById("statusText").textContent = "Server unreachable";
    }
  }

  // ---- Dashboard ----
  async function refreshDashboard() {
    try {
      const r = await fetch("/api/metrics");
      const m = await r.json();
      document.getElementById("m-hw").textContent = m.hardware;
      document.getElementById("m-runtime").textContent = m.runtime;
      document.getElementById("m-model").textContent = m.model;
      document.getElementById("m-latency").textContent = m.avg_latency_ms;
      document.getElementById("m-tps").textContent = m.tokens_per_sec;
      document.getElementById("m-calls").textContent = m.total_calls;
      document.getElementById("m-tokens").textContent = m.tokens_total;
      document.getElementById("m-cost").textContent = m.cloud_cost_saved;
      const tbody = document.getElementById("log-body");
      tbody.innerHTML = m.recent.map((e) => `
        <tr>
          <td>${new Date(e.timestamp).toLocaleTimeString()}</td>
          <td>${e.persona || "—"}</td>
          <td>${e.tokens}</td>
          <td>${e.latency_ms} ms</td>
          <td>${e.hardware}</td>
        </tr>
      `).join("");
    } catch { /* silent */ }
  }

  // ---- Populate research area dropdown ----
  async function loadResearchAreas() {
    try {
      const r = await fetch("/api/data/research");
      const data = await r.json();
      const sel = document.getElementById("research-area");
      sel.innerHTML = '<option value="">— all areas —</option>' +
        (data.areas || []).map((a) => `<option value="${a.id}">${a.title}</option>`).join("");
    } catch {}
  }

  // ---- Init ----
  refreshStatus();
  refreshDashboard();
  loadResearchAreas();
  setInterval(refreshStatus, 8000);
  setInterval(refreshDashboard, 2000);
})();
