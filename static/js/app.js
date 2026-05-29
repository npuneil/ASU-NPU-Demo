// ASU — Empower 2026 NPU Showcase frontend logic
(() => {
  const ENDPOINTS = {
    syllabot:    "/api/syllabot",
    practice:    "/api/practice",
    assignment:  "/api/assignment",
    writing:     "/api/writing",
    onboarding:  "/api/onboarding",
    concierge:   "/api/concierge",
    kb:          "/api/kb",
    dev:         "/api/dev",
    hybrid:      "/api/hybrid",
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
    btn.addEventListener("click", () => submit(btn.dataset.submit));
  });

  function buildBody(key) {
    const inputEl = document.getElementById(`${key}-input`);
    const message = inputEl ? inputEl.value.trim() : "";
    switch (key) {
      case "syllabot":
        return { message, course: document.getElementById("syllabot-course").value };
      case "practice":
        return { message, topic: document.getElementById("practice-topic").value };
      case "assignment":
        return { message, assignment: document.getElementById("assignment-id").value };
      case "writing": {
        const pasted = document.getElementById("writing-paste").value.trim();
        return {
          message: message || "Please review this draft and give structured feedback.",
          draft: document.getElementById("writing-draft").value,
          draft_text: pasted,
        };
      }
      case "onboarding":
        return { message, role: document.getElementById("onboarding-role").value };
      case "kb":
        return { message, article: document.getElementById("kb-article").value };
      case "dev":
        return { message, employee: document.getElementById("dev-employee").value };
      case "hybrid": {
        const sel = document.getElementById("hybrid-scenario");
        const example = sel.options[sel.selectedIndex]?.dataset.example || "";
        return { message: message || example, scenario: sel.value };
      }
      default:
        return { message };
    }
  }

  async function submit(key) {
    const out = document.getElementById(`${key}-output`);
    out.classList.add("thinking");
    out.textContent = key === "hybrid"
      ? "Retrieving M365 context… composing on-device…"
      : "Thinking on-device…";

    const body = buildBody(key);

    try {
      const r = await fetch(ENDPOINTS[key], {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await r.json();
      out.classList.remove("thinking");
      if (data.error) { out.innerHTML = `<em>${data.error}</em>`; return; }
      const meta = `
        <div class="meta">
          <span>⚡ ${data.latency_ms} ms on-device</span>
          <span>🔢 ${data.tokens} tokens</span>
          <span>💻 ${data.hardware}</span>
          <span>💰 saved ${data.cloud_cost_saved}</span>
        </div>`;
      out.innerHTML = escapeHtml(data.response || "[empty response]") + meta;

      if (key === "hybrid") {
        const m = document.getElementById("hybrid-meta");
        m.innerHTML = `
          <div class="hybrid-pill cloud">☁️ Mock cloud retrieval: ${data.cloud_latency_ms} ms</div>
          <div class="hybrid-pill local">🔒 On-device generation: ${data.latency_ms} ms</div>
          <div class="hybrid-pill total">⏱ Total: ${data.total_latency_ms} ms</div>
        `;
      }
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

  // ---- Voice: Browser-native Web Speech API ----
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const speechAvailable = !!SpeechRecognition;

  document.querySelectorAll("[data-voice-for]").forEach((btn) => {
    let recognition = null;
    let listening = false;

    if (!speechAvailable) {
      btn.title = "Speech recognition not supported in this browser";
      btn.style.opacity = "0.5";
      return;
    }

    btn.addEventListener("click", () => {
      const target = document.getElementById(btn.dataset.voiceFor);
      if (listening) { recognition.stop(); return; }
      recognition = new SpeechRecognition();
      recognition.lang = "en-US";
      recognition.interimResults = true;
      recognition.continuous = true;
      recognition.maxAlternatives = 1;

      let finalTranscript = "";
      let interimTranscript = "";

      recognition.onstart = () => {
        listening = true;
        btn.classList.add("recording");
        btn.textContent = "● Stop";
      };
      recognition.onresult = (event) => {
        interimTranscript = "";
        for (let i = event.resultIndex; i < event.results.length; i++) {
          if (event.results[i].isFinal) finalTranscript += event.results[i][0].transcript;
          else interimTranscript += event.results[i][0].transcript;
        }
        target.value = (target.value ? target.value + " " : "") +
          finalTranscript + (interimTranscript ? " " + interimTranscript : "");
        finalTranscript = "";
      };
      recognition.onerror = (event) => {
        if (event.error === "not-allowed") target.placeholder = "Microphone permission denied — allow it and try again.";
        else if (event.error === "no-speech") target.placeholder = "No speech detected — try again.";
      };
      recognition.onend = () => {
        listening = false;
        btn.classList.remove("recording");
        btn.textContent = "🎤 Voice";
      };
      recognition.start();
    });
  });

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
        text.textContent = `${s.hardware} · ${s.model} · ${s.runtime}`;
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

  // ---- Populate dropdowns ----
  async function populate(url, selectId, makeOption, opts = {}) {
    try {
      const r = await fetch(url);
      const data = await r.json();
      const sel = document.getElementById(selectId);
      if (!sel) return;
      const items = data[opts.key || Object.keys(data)[0]] || [];
      const current = sel.innerHTML;
      sel.innerHTML = current + items.map(makeOption).join("");
      if (opts.onLoad) opts.onLoad(items);
    } catch {}
  }

  function loadDropdowns() {
    populate("/api/data/syllabi", "syllabot-course",
      (c) => `<option value="${c.code}">${c.code} — ${c.title}</option>`,
      { key: "courses" });

    populate("/api/data/practice", "practice-topic",
      (t) => `<option value="${t.id}">${t.course} — ${t.title}</option>`,
      { key: "topics" });

    populate("/api/data/assignments", "assignment-id",
      (a) => `<option value="${a.id}">${a.course} — ${a.title} (due ${a.due})</option>`,
      { key: "assignments" });

    populate("/api/data/writing", "writing-draft",
      (d) => `<option value="${d.id}">${d.label}</option>`,
      { key: "drafts" });

    populate("/api/data/onboarding", "onboarding-role",
      (r) => `<option value="${r}">${r}</option>`,
      { key: "roles" });

    populate("/api/data/kb", "kb-article",
      (a) => `<option value="${a.id}">${a.id} — ${a.title}</option>`,
      { key: "articles" });

    populate("/api/data/dev", "dev-employee",
      (e) => `<option value="${e}">${e}</option>`,
      { key: "employees" });

    // Hybrid scenarios — render cards too
    fetch("/api/data/hybrid").then((r) => r.json()).then((data) => {
      const sel = document.getElementById("hybrid-scenario");
      const scenarios = data.scenarios || [];
      sel.innerHTML = scenarios.map((s) =>
        `<option value="${s.id}" data-example="${escapeAttr(s.user_prompt_example)}">${s.label}</option>`
      ).join("");
      sel.addEventListener("change", () => renderHybridCards(scenarios));
      const inputEl = document.getElementById("hybrid-input");
      sel.addEventListener("change", () => {
        const ex = sel.options[sel.selectedIndex]?.dataset.example || "";
        inputEl.placeholder = ex ? `e.g., "${ex}"` : "What do you want to do with this context?";
      });
      // initial render
      if (scenarios.length) {
        sel.dispatchEvent(new Event("change"));
        renderHybridCards(scenarios);
      }
    });
  }

  function renderHybridCards(scenarios) {
    const sel = document.getElementById("hybrid-scenario");
    const target = document.getElementById("hybrid-cards");
    const cur = scenarios.find((s) => s.id === sel.value) || scenarios[0];
    if (!cur) { target.innerHTML = ""; return; }
    target.innerHTML = `
      <div class="m365-row-label">📎 Mock context retrieved from your Microsoft 365:</div>
      ${cur.context.map(cardHtml).join("")}
    `;
  }

  function cardHtml(c) {
    const sourceClass = (c.source || "").toLowerCase();
    const sub = c.from ? `from ${escapeHtml(c.from)} · ${escapeHtml(c.received || "")}` :
                c.modified_by ? `modified by ${escapeHtml(c.modified_by)} · ${escapeHtml(c.modified || "")}` : "";
    return `
      <div class="m365-card source-${sourceClass}">
        <div class="m365-head"><span class="m365-icon">${c.icon || "📎"}</span><span class="m365-source">${escapeHtml(c.source)}</span></div>
        <div class="m365-title">${escapeHtml(c.title || "")}</div>
        <div class="m365-sub">${sub}</div>
        <div class="m365-snippet">${escapeHtml(c.snippet || "")}</div>
      </div>
    `;
  }

  function escapeAttr(s) { return (s || "").replace(/"/g, "&quot;"); }

  // ---- Init ----
  refreshStatus();
  refreshDashboard();
  loadDropdowns();
  setInterval(refreshStatus, 8000);
  setInterval(refreshDashboard, 2000);
})();
