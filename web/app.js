const $ = (s) => document.querySelector(s);
const LS_KEY = "vct_api_key";

let apiKey = localStorage.getItem(LS_KEY) || "";
let authRequired = false;

// ---------- fetch wrapper that injects X-API-Key ----------
async function api(path, opts = {}) {
  opts.headers = opts.headers || {};
  if (apiKey) opts.headers["X-API-Key"] = apiKey;
  const res = await fetch(path, opts);
  if (res.status === 401) {
    // key invalid — clear and re-prompt
    apiKey = "";
    localStorage.removeItem(LS_KEY);
    showLogin();
    throw new Error("Unauthorized");
  }
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { const j = await res.json(); msg = j.detail || msg; } catch {}
    throw new Error(msg);
  }
  return res;
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// ---------- login flow ----------
function showLogin() {
  $("#loginModal").style.display = "flex";
  $("#loginKey").value = "";
  $("#loginMsg").textContent = "";
  setTimeout(() => $("#loginKey").focus(), 50);
}
function hideLogin() { $("#loginModal").style.display = "none"; }

$("#loginBtn").addEventListener("click", async () => {
  const val = $("#loginKey").value.trim();
  if (!val) return;
  // test by calling a protected endpoint
  const r = await fetch("/api/voices", { headers: { "X-API-Key": val } });
  if (r.ok) {
    apiKey = val;
    localStorage.setItem(LS_KEY, apiKey);
    hideLogin();
    await init();
  } else {
    $("#loginMsg").textContent = "Invalid key.";
  }
});
$("#loginKey").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("#loginBtn").click();
});

$("#logoutBtn").addEventListener("click", () => {
  apiKey = "";
  localStorage.removeItem(LS_KEY);
  showLogin();
});

// ---------- health + auth probe ----------
async function refreshHealth() {
  try {
    const r = await fetch("/api/health");
    const j = await r.json();
    authRequired = !!j.auth_required;
    const el = $("#status");
    el.textContent = `ready · device: ${j.device} · voices: ${j.voices}`;
    el.className = "status ok";
    $("#logoutBtn").style.display = authRequired && apiKey ? "inline-block" : "none";
  } catch {
    $("#status").textContent = "backend unreachable";
    $("#status").className = "status err";
  }
}

// ---------- voices ----------
async function refreshVoices() {
  const r = await api("/api/voices");
  const { voices } = await r.json();
  const list = $("#voicesList");
  const select = $("#voiceSelect");
  list.innerHTML = "";
  select.innerHTML = "";

  if (!voices.length) {
    list.innerHTML = '<div class="empty">No voices yet. Clone one above.</div>';
    const opt = document.createElement("option");
    opt.textContent = "— no voices —";
    opt.disabled = true;
    opt.selected = true;
    select.appendChild(opt);
    return;
  }

  for (const v of voices) {
    const row = document.createElement("div");
    row.className = "voice";
    const refInfo = (v.reference_seconds || v.segment_count)
      ? ` · ${v.reference_seconds ? v.reference_seconds.toFixed(0) + "s ref" : ""}${v.segment_count ? `, ${v.segment_count} segment${v.segment_count === 1 ? "" : "s"}` : ""}`
      : "";
    row.innerHTML = `
      <div class="info">
        <div class="name">${escapeHtml(v.name)}${v.description ? ` <span style="color:var(--muted);font-weight:400;">— ${escapeHtml(v.description)}</span>` : ""}</div>
        <div class="meta">${v.voice_id}${refInfo}</div>
      </div>
      <button class="del" data-id="${v.voice_id}">Delete</button>
    `;
    list.appendChild(row);

    const opt = document.createElement("option");
    opt.value = v.voice_id;
    opt.textContent = `${v.name}  (${v.voice_id})`;
    select.appendChild(opt);
  }

  list.querySelectorAll(".del").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm("Delete this voice?")) return;
      try {
        await api(`/api/voices/${btn.dataset.id}`, { method: "DELETE" });
        await refreshVoices();
        await refreshHealth();
      } catch (e) { alert("Delete failed: " + e.message); }
    });
  });
}

// ---------- keys ----------
async function refreshKeys() {
  try {
    const r = await api("/api/keys");
    const { keys } = await r.json();
    const list = $("#keysList");
    list.innerHTML = "";
    if (!keys.length) {
      list.innerHTML = '<div class="empty">No API keys yet. Create one above.</div>';
      return;
    }
    for (const k of keys) {
      const row = document.createElement("div");
      row.className = "voice";
      const used = k.last_used_at ? `last used ${new Date(k.last_used_at).toLocaleString()}` : "never used";
      row.innerHTML = `
        <div class="info">
          <div class="name">${escapeHtml(k.name)}</div>
          <div class="meta">${k.key_masked} · ${used}</div>
        </div>
        <button class="del" data-id="${k.id}">Revoke</button>
      `;
      list.appendChild(row);
    }
    list.querySelectorAll(".del").forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (!confirm("Revoke this key? Tools using it will stop working.")) return;
        try {
          await api(`/api/keys/${btn.dataset.id}`, { method: "DELETE" });
          await refreshKeys();
          await refreshHealth();
        } catch (e) { alert("Delete failed: " + e.message); }
      });
    });
  } catch (e) {
    console.error(e);
  }
}

$("#keyForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("#keyBtn");
  const msg = $("#keyMsg");
  btn.disabled = true;
  msg.textContent = "";
  try {
    const fd = new FormData(e.target);
    const r = await api("/api/keys", { method: "POST", body: fd });
    const j = await r.json();
    e.target.reset();
    // show the full key once
    $("#newKeyValue").textContent = j.key;
    $("#newKeyModal").style.display = "flex";
    await refreshKeys();
    await refreshHealth();
  } catch (err) {
    msg.textContent = "Error: " + err.message;
    msg.className = "msg err";
  } finally {
    btn.disabled = false;
  }
});

$("#copyKeyBtn").addEventListener("click", async () => {
  const txt = $("#newKeyValue").textContent;
  try { await navigator.clipboard.writeText(txt); $("#copyKeyBtn").textContent = "Copied!"; }
  catch { $("#copyKeyBtn").textContent = "Copy failed"; }
  setTimeout(() => ($("#copyKeyBtn").textContent = "Copy to clipboard"), 1500);
});
$("#closeKeyBtn").addEventListener("click", () => {
  $("#newKeyModal").style.display = "none";
});

// ---------- clone ----------
$("#cloneForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("#cloneBtn");
  const msg = $("#cloneMsg");
  msg.textContent = "Processing audio…";
  msg.className = "msg";
  btn.disabled = true;
  try {
    const fd = new FormData(e.target);
    const r = await api("/api/voices", { method: "POST", body: fd });
    const j = await r.json();
    msg.textContent = `Cloned: ${j.name} (${j.voice_id})`;
    msg.className = "msg ok";
    e.target.reset();
    await refreshVoices();
    await refreshHealth();
  } catch (err) {
    msg.textContent = "Error: " + err.message;
    msg.className = "msg err";
  } finally {
    btn.disabled = false;
  }
});

// ---------- speed ----------
$("#speed").addEventListener("input", (e) => {
  $("#speedVal").textContent = parseFloat(e.target.value).toFixed(2);
});

// ---------- generate ----------
function selectedFormat() {
  const el = document.querySelector('input[name="format"]:checked');
  return el && el.value === "mp3" ? "mp3" : "wav";
}

async function runGenerate({ batchZip }) {
  const btn = $("#genBtn");
  const btnZip = $("#genBtnZip");
  const msg = $("#genMsg");
  const player = $("#player");
  const form = $("#genForm");
  const endpoint = batchZip ? "/api/generate-batch" : "/api/generate";
  const fmt = selectedFormat();
  msg.textContent = batchZip
    ? `Generating ZIP (one .${fmt} per question). This may take several minutes for many items…`
    : "Generating… (this can take a while for long text)";
  msg.className = "msg";
  btn.disabled = true;
  btnZip.disabled = true;
  player.style.display = "none";
  try {
    const fd = new FormData(form);
    const res = await fetch(endpoint, {
      method: "POST",
      body: fd,
      headers: apiKey ? { "X-API-Key": apiKey } : {},
    });
    if (res.status === 401) {
      apiKey = ""; localStorage.removeItem(LS_KEY); showLogin();
      throw new Error("Unauthorized");
    }
    if (!res.ok) {
      let m = `HTTP ${res.status}`;
      try {
        const j = await res.json();
        m = Array.isArray(j.detail) ? j.detail.map((x) => x.msg || x).join("; ") : (j.detail || m);
      } catch {}
      throw new Error(m);
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    if (batchZip) {
      const a = document.createElement("a");
      a.href = url;
      a.download = `questions_${Date.now()}.zip`;
      document.body.appendChild(a); a.click(); a.remove();
      msg.textContent = "Done. ZIP downloaded.";
    } else {
      player.src = url;
      player.style.display = "block";
      const a = document.createElement("a");
      a.href = url;
      a.download = `voice_${Date.now()}.${fmt}`;
      document.body.appendChild(a); a.click(); a.remove();
      msg.textContent = "Done. Audio downloaded.";
    }
    msg.className = "msg ok";
  } catch (err) {
    msg.textContent = "Error: " + err.message;
    msg.className = "msg err";
  } finally {
    btn.disabled = false;
    btnZip.disabled = false;
  }
}

window.vctSelectedFormat = selectedFormat;

$("#genForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  await runGenerate({ batchZip: false });
});

$("#genBtnZip").addEventListener("click", async () => {
  const form = $("#genForm");
  if (!form.reportValidity()) return;
  await runGenerate({ batchZip: true });
});

// Exposed for batch_generate.js (same-origin script load order)
window.vctApi = api;
window.vctShowLogin = showLogin;

// ---------- API curl snippet ----------
function renderApiCurlSnippet() {
  const el = document.getElementById("apiCurlSnippet");
  if (!el) return;
  const base = window.location.origin;
  el.textContent = `curl -X POST "${base}/api/generate" \\
  -H "X-API-Key: YOUR_KEY" \\
  -F voice_id=YOUR_VOICE_ID \\
  -F text="Hello from my video tool." \\
  -F speed=1.0 \\
  -F format=mp3 \\
  --output narration.mp3`;
}
renderApiCurlSnippet();

// ---------- init ----------
async function init() {
  await refreshHealth();
  // If auth is required and we have no key, prompt
  if (authRequired && !apiKey) { showLogin(); return; }
  try {
    await refreshVoices();
    await refreshKeys();
  } catch (e) { console.error(e); }
}

init();
