/**
 * Multi-document quiz-question queue.
 * Add one or more .docx files. For each: parse server-side, then for every
 * parsed question, call /api/questions/generate-one (which synthesises the
 * four voices and merges them with a configurable pause server-side) and
 * write "Question N.<ext>" into a fresh per-doc subfolder.
 */
(function () {
  const $ = (s) => document.querySelector(s);

  let qDirHandle = null;
  let qDocQueue = [];
  let qRunning = false;
  let qPaused = false;
  let qCancelRequested = false;
  let qCurrentDocIdx = -1;
  let nextDocId = 1;

  function escapeHtml(s) {
    return (s || "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function fsSafeSegment(name) {
    const t = (name || "").replace(/[<>:"/\\|?*\u0000-\u001f]/g, "_").trim();
    return t || "untitled";
  }

  function suggestFolderName(filename) {
    const stem = (filename || "").replace(/\.[^.]+$/, "");
    const tokens = stem.split(/[_\-\s]+/).filter(Boolean);
    if (!tokens.length) return "Questions";
    return tokens
      .map((t) => (/[A-Z]/.test(t) ? t : (t.charAt(0).toUpperCase() + t.slice(1))))
      .join(" ");
  }

  function currentFormat() {
    if (typeof window.vctSelectedFormat === "function") return window.vctSelectedFormat();
    const el = document.querySelector('input[name="format"]:checked');
    return el && el.value === "mp3" ? "mp3" : "wav";
  }

  function currentPauseSeconds() {
    const el = $("#qPause");
    if (!el) return 1.5;
    const v = parseFloat(el.value);
    return isFinite(v) ? Math.max(0, Math.min(10, v)) : 1.5;
  }

  // ---------- filesystem helpers ----------
  async function fileExists(parent, name) {
    try { await parent.getFileHandle(name, { create: false }); return true; }
    catch (e) { return e && e.name !== "NotFoundError"; }
  }
  async function dirExists(parent, name) {
    try { await parent.getDirectoryHandle(name, { create: false }); return true; }
    catch (e) {
      if (!e) return false;
      if (e.name === "NotFoundError") return false;
      return true;
    }
  }
  async function entryExists(parent, name) {
    return (await dirExists(parent, name)) || (await fileExists(parent, name));
  }

  async function getFreshDirectoryHandle(parent, baseName) {
    const safe = fsSafeSegment(baseName);
    for (let n = 0; n < 999; n++) {
      const name = n === 0 ? safe : `${safe}_${n}`;
      if (!(await entryExists(parent, name))) {
        return await parent.getDirectoryHandle(name, { create: true });
      }
    }
    throw new Error(`Could not allocate a fresh subfolder for ${baseName}`);
  }

  async function uniqueFileNameFn(parent, baseName) {
    if (!(await entryExists(parent, baseName))) return baseName;
    const dot = baseName.lastIndexOf(".");
    const stem = dot >= 0 ? baseName.slice(0, dot) : baseName;
    const ext = dot >= 0 ? baseName.slice(dot) : "";
    for (let i = 1; i < 999; i++) {
      const n = `${stem}_${i}${ext}`;
      if (!(await entryExists(parent, n))) return n;
    }
    throw new Error("Could not find a unique filename");
  }

  async function writeQuestionBlob(docDirHandle, qNumber, ext, blob) {
    const fileSeg = fsSafeSegment(`Question ${qNumber}.${ext}`);
    const unique = await uniqueFileNameFn(docDirHandle, fileSeg);
    const fh = await docDirHandle.getFileHandle(unique, { create: true });
    const w = await fh.createWritable();
    await w.write(blob);
    await w.close();
    return unique;
  }

  // ---------- API ----------
  function decodeError(j, fallback) {
    if (typeof j.detail === "string") return j.detail;
    if (Array.isArray(j.detail)) return j.detail.map((x) => x.msg || JSON.stringify(x)).join("; ");
    if (j.detail) return String(j.detail);
    return fallback;
  }

  async function fetchParseQuestionDocx(file) {
    const fd = new FormData();
    fd.append("file", file);
    const key = localStorage.getItem("vct_api_key") || "";
    const res = await fetch("/api/questions/parse-docx", {
      method: "POST",
      body: fd,
      headers: key ? { "X-API-Key": key } : {},
    });
    if (res.status === 401) {
      if (window.vctShowLogin) window.vctShowLogin();
      throw new Error("Unauthorized");
    }
    if (!res.ok) {
      let msg = `HTTP ${res.status}`;
      try { msg = decodeError(await res.json(), msg); } catch { /* ignore */ }
      throw new Error(msg);
    }
    return res.json();
  }

  async function generateOneQuestion(voiceId, question, speed, fmt, pauseSec) {
    const fd = new FormData();
    fd.append("voice_id", voiceId);
    fd.append("question_json", JSON.stringify(question));
    fd.append("speed", String(speed));
    fd.append("format", fmt || "wav");
    fd.append("pause_seconds", String(pauseSec));
    const key = localStorage.getItem("vct_api_key") || "";
    const res = await fetch("/api/questions/generate-one", {
      method: "POST",
      body: fd,
      headers: key ? { "X-API-Key": key } : {},
    });
    if (res.status === 401) {
      if (window.vctShowLogin) window.vctShowLogin();
      throw new Error("Unauthorized");
    }
    if (!res.ok) {
      let msg = `HTTP ${res.status}`;
      try { msg = decodeError(await res.json(), msg); } catch { /* ignore */ }
      throw new Error(msg);
    }
    return res.blob();
  }

  // ---------- rendering ----------
  function renderQueue() {
    const el = $("#qQueueBody");
    if (!el) return;
    if (!qDocQueue.length) {
      el.innerHTML = '<tr><td colspan="6" class="batch-empty">No question docs yet. Click "Add question doc" to start.</td></tr>';
      return;
    }
    el.innerHTML = qDocQueue.map((d, idx) => {
      const editable = !qRunning && d.status === "pending";
      const folderCell = editable
        ? `<input type="text" data-doc-id="${d.id}" class="q-folder-name" value="${escapeHtml(d.folderName)}" style="width:100%;" />`
        : `<span>${escapeHtml(d.folderName)}${d.actualFolder && d.actualFolder !== d.folderName ? ` <span style="color:var(--muted);">(saved as ${escapeHtml(d.actualFolder)})</span>` : ""}</span>`;
      const total = d.parsed ? d.parsed.count : 0;
      const qInfo = d.parsed
        ? `${d.completed} / ${total}${d.failed ? ` · ${d.failed} failed` : ""}`
        : "—";
      const errHtml = d.error ? `<div class="batch-err">${escapeHtml(d.error)}</div>` : "";
      const removable = !qRunning;
      const rmBtn = removable ? `<button type="button" class="q-doc-remove ghost" data-doc-id="${d.id}">Remove</button>` : "";
      return `<tr data-id="${d.id}">
          <td>${idx + 1}</td>
          <td>${escapeHtml(d.file.name)}</td>
          <td>${folderCell}</td>
          <td>${qInfo}</td>
          <td><span class="batch-status batch-status--${d.status}">${d.status}</span>${errHtml}</td>
          <td>${rmBtn}</td>
        </tr>`;
    }).join("");

    el.querySelectorAll(".q-folder-name").forEach((inp) => {
      inp.addEventListener("input", (e) => {
        const id = parseInt(e.target.dataset.docId, 10);
        const d = qDocQueue.find((x) => x.id === id);
        if (d) d.folderName = e.target.value;
      });
    });
    el.querySelectorAll(".q-doc-remove").forEach((btn) => {
      btn.addEventListener("click", () => {
        const id = parseInt(btn.dataset.docId, 10);
        qDocQueue = qDocQueue.filter((x) => x.id !== id);
        renderQueue();
        updateProgressPanel();
        setControlsEnabled();
      });
    });
  }

  function updateProgressPanel() {
    const total = qDocQueue.length;
    const okDocs = qDocQueue.filter((d) => d.status === "done").length;
    const failedDocs = qDocQueue.filter((d) => d.status === "failed").length;
    const summary = $("#qProgressSummary");
    const cur = $("#qProgressCurrent");
    if (summary) {
      summary.textContent = total
        ? `${okDocs + failedDocs} of ${total} documents processed · ${okDocs} ok · ${failedDocs} failed`
        : "";
    }
    if (cur) {
      const d = qDocQueue[qCurrentDocIdx];
      if (qRunning && d) {
        if (d.status === "parsing") {
          cur.textContent = `Parsing ${d.folderName}…`;
        } else if (d.currentQuestion) {
          cur.textContent = `Doc ${qCurrentDocIdx + 1}/${qDocQueue.length}: ${d.folderName} · Question ${d.currentQuestion.number}`;
        } else {
          cur.textContent = `Doc ${qCurrentDocIdx + 1}/${qDocQueue.length}: ${d.folderName}`;
        }
      } else if (qCancelRequested && qRunning) {
        cur.textContent = "Stopping after current item…";
      } else {
        cur.textContent = "";
      }
    }
  }

  function setControlsEnabled() {
    const hasPending = qDocQueue.some((d) => d.status === "pending");
    const canStart = hasPending && qDirHandle && !qRunning;
    const startBtn = $("#qBtnStart");
    const pauseBtn = $("#qBtnPause");
    const resumeBtn = $("#qBtnResume");
    const cancelBtn = $("#qBtnCancel");
    const clearBtn = $("#qBtnClear");
    const addBtn = $("#qAddDoc");
    if (startBtn) startBtn.disabled = !canStart;
    if (pauseBtn) pauseBtn.disabled = !qRunning || qPaused;
    if (resumeBtn) resumeBtn.disabled = !qRunning || !qPaused;
    if (cancelBtn) cancelBtn.disabled = !qRunning;
    if (clearBtn) clearBtn.disabled = qRunning || qDocQueue.length === 0;
    if (addBtn) addBtn.disabled = qRunning;
  }

  function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }
  async function waitWhilePaused() {
    while (qPaused && !qCancelRequested && qRunning) await sleep(120);
  }

  // ---------- main loop ----------
  async function runQuestionLoop() {
    if (qRunning) return;

    const voiceSelect = $("#voiceSelect");
    const speedEl = $("#speed");
    const voiceId = voiceSelect && voiceSelect.value;
    const speed = speedEl ? parseFloat(speedEl.value) : 1;
    if (!voiceId) { alert("Select a voice in section 3."); return; }
    if (!qDirHandle) { alert("Select an output folder first."); return; }
    if (!qDocQueue.some((d) => d.status === "pending")) {
      alert("No pending documents in queue.");
      return;
    }

    qRunning = true;
    qPaused = false;
    qCancelRequested = false;
    const msgEl = $("#qBatchMsg");
    if (msgEl) { msgEl.textContent = ""; msgEl.className = "msg"; }
    setControlsEnabled();
    renderQueue();

    const fmt = currentFormat();
    const pauseSec = currentPauseSeconds();

    for (let i = 0; i < qDocQueue.length; i++) {
      if (qCancelRequested) break;
      await waitWhilePaused();
      if (qCancelRequested) break;

      const d = qDocQueue[i];
      if (d.status !== "pending") continue;

      qCurrentDocIdx = i;
      d.status = "parsing";
      d.error = null;
      d.completed = 0;
      d.failed = 0;
      d.currentQuestion = null;
      renderQueue();
      updateProgressPanel();

      let parsed;
      try {
        parsed = await fetchParseQuestionDocx(d.file);
        d.parsed = parsed;
      } catch (e) {
        d.status = "failed";
        d.error = "Parse failed: " + (e.message || String(e));
        renderQueue();
        updateProgressPanel();
        continue;
      }

      let docDh;
      try {
        docDh = await getFreshDirectoryHandle(qDirHandle, d.folderName || "Questions");
        d.actualFolder = docDh.name;
      } catch (e) {
        d.status = "failed";
        d.error = "Subfolder failed: " + (e.message || String(e));
        renderQueue();
        updateProgressPanel();
        continue;
      }

      d.status = "generating";
      renderQueue();

      const questions = parsed.questions || [];
      for (let qi = 0; qi < questions.length; qi++) {
        if (qCancelRequested) break;
        await waitWhilePaused();
        if (qCancelRequested) break;

        const q = questions[qi];
        d.currentQuestion = q;
        updateProgressPanel();

        try {
          const blob = await generateOneQuestion(voiceId, q, speed, fmt, pauseSec);
          await writeQuestionBlob(docDh, q.number, fmt, blob);
          d.completed++;
        } catch (e) {
          console.error("Question failed in", d.folderName, "Q" + q.number, e);
          d.failed++;
        }
        renderQueue();
        updateProgressPanel();
      }

      if (qCancelRequested) {
        d.status = "cancelled";
        d.error = `Cancelled after ${d.completed} of ${questions.length} questions.`;
      } else if (d.failed > 0 && d.completed === 0) {
        d.status = "failed";
        d.error = `All ${d.failed} question(s) failed.`;
      } else {
        d.status = "done";
        if (d.failed > 0) d.error = `${d.failed} question(s) failed (rest saved).`;
      }
      d.currentQuestion = null;
      renderQueue();
      updateProgressPanel();
    }

    qCurrentDocIdx = -1;
    qRunning = false;
    qPaused = false;
    const cancelled = qCancelRequested;
    qCancelRequested = false;
    setControlsEnabled();
    renderQueue();
    updateProgressPanel();

    const okDocs = qDocQueue.filter((d) => d.status === "done").length;
    const failedDocs = qDocQueue.filter((d) => d.status === "failed").length;
    const cancelledDocs = qDocQueue.filter((d) => d.status === "cancelled").length;
    const totalQs = qDocQueue.reduce((s, d) => s + (d.completed || 0), 0);
    const failedQs = qDocQueue.reduce((s, d) => s + (d.failed || 0), 0);
    if (msgEl) {
      const head = cancelled ? "Stopped." : "Question batch finished.";
      const cancelledPart = cancelledDocs ? `, ${cancelledDocs} cancelled` : "";
      msgEl.textContent = `${head} ${okDocs} doc(s) complete, ${failedDocs} failed${cancelledPart} · ${totalQs} question(s) saved, ${failedQs} failed.`;
      msgEl.className = (failedDocs || failedQs || cancelled) ? "msg err" : "msg ok";
    }
  }

  // ---------- handlers ----------
  $("#qPickFolder")?.addEventListener("click", async () => {
    if (!window.showDirectoryPicker) {
      const fp = $("#qFolderPath");
      if (fp) {
        fp.textContent = "Folder picker requires Chrome or Edge (HTTPS or localhost).";
        fp.className = "batch-folder err";
      }
      return;
    }
    try {
      qDirHandle = await window.showDirectoryPicker({ mode: "readwrite" });
      const fp = $("#qFolderPath");
      if (fp) {
        fp.textContent = `Selected folder: ${qDirHandle.name} (path not shown by browser for privacy)`;
        fp.className = "batch-folder ok";
      }
      setControlsEnabled();
    } catch (e) {
      if (e.name !== "AbortError") console.error(e);
    }
  });

  $("#qAddDoc")?.addEventListener("click", () => {
    const inp = $("#qDocxInput");
    if (inp) inp.click();
  });

  $("#qDocxInput")?.addEventListener("change", (e) => {
    const input = e.target;
    const f = input.files && input.files[0];
    if (f) {
      qDocQueue.push({
        id: nextDocId++,
        file: f,
        folderName: suggestFolderName(f.name),
        actualFolder: null,
        status: "pending",
        parsed: null,
        completed: 0,
        failed: 0,
        error: null,
        currentQuestion: null,
      });
      renderQueue();
      updateProgressPanel();
      setControlsEnabled();
    }
    input.value = "";
  });

  $("#qPause")?.addEventListener("input", (e) => {
    const v = parseFloat(e.target.value);
    const span = $("#qPauseVal");
    if (span) span.textContent = isFinite(v) ? v.toFixed(2) : "1.50";
  });

  $("#qBtnStart")?.addEventListener("click", () => { runQuestionLoop(); });
  $("#qBtnPause")?.addEventListener("click", () => {
    if (!qRunning) return;
    qPaused = true;
    setControlsEnabled();
    updateProgressPanel();
  });
  $("#qBtnResume")?.addEventListener("click", () => {
    qPaused = false;
    setControlsEnabled();
    updateProgressPanel();
  });
  $("#qBtnCancel")?.addEventListener("click", () => {
    if (!qRunning) return;
    qCancelRequested = true;
    qPaused = false;
    setControlsEnabled();
    updateProgressPanel();
  });
  $("#qBtnClear")?.addEventListener("click", () => {
    if (qRunning) return;
    qDocQueue = [];
    const msgEl = $("#qBatchMsg");
    if (msgEl) { msgEl.textContent = ""; msgEl.className = "msg"; }
    renderQueue();
    updateProgressPanel();
    setControlsEnabled();
  });

  renderQueue();
  updateProgressPanel();
  setControlsEnabled();
})();
