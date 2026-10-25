// EMAY PM10 web app — frontend logic

const $ = (id) => document.getElementById(id);

const state = {
  records: [],
  current: null,
  currentSamples: null,   // {mv:[], peaks:[], sample_rate_hz, duration_s}
  view: "interactive",    // "interactive" | "clinical"
};

// ── toast ──────────────────────────────────────────────────────────────
function toast(msg, level = "") {
  const el = $("toast");
  el.className = "toast " + level;
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 3500);
}

// ── api ────────────────────────────────────────────────────────────────
async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
  });
  const ct = res.headers.get("content-type") || "";
  const body = ct.includes("json") ? await res.json() : await res.text();
  if (!res.ok) {
    const msg = (typeof body === "object" && body && body.error) || res.statusText;
    throw new Error(msg);
  }
  return body;
}

// ── device status ──────────────────────────────────────────────────────
async function refreshDeviceStatus() {
  try {
    const s = await api("/api/device/status");
    const el = $("device-status");
    if (s.connected) {
      el.textContent = "PM10 connected";
      el.className = "pill pill-good";
    } else {
      el.textContent = "PM10 not detected";
      el.className = "pill pill-bad";
    }
  } catch (e) {
    $("device-status").textContent = "status error";
  }
}

// ── records list ───────────────────────────────────────────────────────
function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

async function loadRecords() {
  state.records = await api("/api/records");
  renderRecords();
}

function renderRecords() {
  const listEl = $("records-list");
  const emptyEl = $("records-empty");
  listEl.innerHTML = "";
  if (!state.records.length) {
    emptyEl.classList.remove("hidden");
    return;
  }
  emptyEl.classList.add("hidden");
  for (const r of state.records) {
    const div = document.createElement("div");
    div.className = "record-card";
    const badges = [];
    if (r.has_analysis) badges.push('<span class="r-badge ok">AI analyzed</span>');
    if (r.has_pdf) badges.push('<span class="r-badge">PDF</span>');
    badges.push(`<span class="r-badge">${r.duration_s}s @ ${r.sample_rate_hz} Hz</span>`);
    div.innerHTML = `
      <div class="r-info">
        <div class="r-date">${fmtDate(r.captured_at)}</div>
        <div class="r-sub">${r.sample_count} samples · ${r.r_peaks} R-peaks · device index ${r.device_index}</div>
      </div>
      <div class="r-badges">${badges.join(" ")}</div>
      <div class="r-bpm">${r.heart_rate_bpm}<div class="unit">bpm</div></div>
    `;
    div.addEventListener("click", () => openRecord(r.id));
    listEl.appendChild(div);
  }
}

// ── detail view ────────────────────────────────────────────────────────
async function openRecord(id) {
  try {
    const data = await api(`/api/records/${id}`);
    state.current = data;
    // Reset to interactive on each open, but keep clinical view content ready.
    setView("interactive");
    $("records-view").classList.add("hidden");
    $("detail-view").classList.remove("hidden");
    $("detail-title").textContent = fmtDate(data.meta.captured_at);
    $("detail-meta").textContent =
      `${data.meta.duration_s} s · ${data.meta.sample_rate_hz} Hz · ` +
      `${data.meta.sample_count} samples · ${data.meta.heart_rate_bpm} bpm ` +
      `(${data.meta.r_peaks} R-peaks) · device index ${data.meta.device_index}`;
    await renderInteractivePlot(id);
    if (data.analysis) {
      renderAnalysis(data.analysis, id);
    } else {
      $("analysis-panel").classList.add("hidden");
    }
  } catch (e) {
    toast(e.message, "error");
  }
}

function isDarkMode() {
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
}

async function renderInteractivePlot(id) {
  const data = await api(`/api/records/${id}/samples`);
  state.currentSamples = data;
  const sr = data.sample_rate_hz;
  const mv = data.mv;
  const t = new Float64Array(mv.length);
  for (let i = 0; i < mv.length; i++) t[i] = i / sr;
  const peakT = data.peaks.map((i) => i / sr);
  const peakV = data.peaks.map((i) => mv[i]);

  const dark = isDarkMode();
  const trace = {
    x: Array.from(t), y: mv,
    type: "scattergl", mode: "lines",
    line: { color: dark ? "#4dd0aa" : "#0a7455", width: 1.2 },
    hovertemplate: "t = %{x:.3f} s<br>%{y:.3f} mV<extra></extra>",
    name: "ECG",
  };
  const peakTrace = {
    x: peakT, y: peakV,
    type: "scatter", mode: "markers",
    marker: { color: dark ? "#f2748a" : "#c1394a", size: 8, symbol: "circle" },
    hovertemplate: "R-peak<br>t = %{x:.3f} s<br>%{y:.3f} mV<extra></extra>",
    name: "R-peak",
  };

  const layout = {
    margin: { l: 55, r: 20, t: 8, b: 60 },
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    font: {
      family: '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
      color: dark ? "#e8e9ec" : "#16171a",
    },
    xaxis: {
      title: "time (s)",
      gridcolor: dark ? "rgba(255,255,255,0.08)" : "rgba(0,0,0,0.08)",
      zerolinecolor: dark ? "rgba(255,255,255,0.15)" : "rgba(0,0,0,0.15)",
      rangeslider: { visible: true, thickness: 0.06 },
    },
    yaxis: {
      title: "mV",
      gridcolor: dark ? "rgba(255,255,255,0.08)" : "rgba(0,0,0,0.08)",
      zeroline: true,
      zerolinecolor: dark ? "rgba(255,255,255,0.25)" : "rgba(0,0,0,0.25)",
    },
    dragmode: "zoom",
    showlegend: false,
    hovermode: "x unified",
  };
  const config = {
    responsive: true,
    displaylogo: false,
    scrollZoom: true,
    modeBarButtonsToRemove: ["lasso2d", "select2d", "toImage"],
    displayModeBar: true,
    doubleClick: "reset",
  };
  await Plotly.newPlot("detail-plot", [trace, peakTrace], layout, config);
}

function setRangePreset(seconds) {
  if (!state.currentSamples) return;
  const dur = state.currentSamples.duration_s;
  let x0, x1;
  if (seconds === "all") {
    x0 = 0; x1 = dur;
  } else {
    const win = Math.min(Number(seconds), dur);
    const center = dur / 2;
    x0 = Math.max(0, center - win / 2);
    x1 = Math.min(dur, x0 + win);
  }
  Plotly.relayout("detail-plot", { "xaxis.range": [x0, x1] });
}

function loadClinicalView(id) {
  const stripImg = $("clinical-strip");
  stripImg.src = `/api/records/${id}/strip.png?t=${Date.now()}`;
  const zoomsEl = $("clinical-zooms");
  zoomsEl.innerHTML = "";
  // Assume up to 3 zoom tiles; skip 404s silently.
  const bust = Date.now();
  [0, 1, 2].forEach((i) => {
    const tile = document.createElement("div");
    tile.className = "clinical-zoom-tile";
    const img = document.createElement("img");
    img.alt = `Zoom ${i}`;
    img.onerror = () => tile.remove();
    img.src = `/api/records/${id}/zoom_${i}.png?t=${bust}`;
    tile.appendChild(img);
    zoomsEl.appendChild(tile);
  });
}

function setView(view) {
  state.view = view;
  document.querySelectorAll(".view-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.view === view);
  });
  const interactive = view === "interactive";
  $("detail-plot").style.display = interactive ? "" : "none";
  $("range-presets").style.display = interactive ? "" : "none";
  $("clinical-view").classList.toggle("hidden", interactive);
  if (interactive && state.currentSamples) {
    requestAnimationFrame(() => Plotly.Plots.resize("detail-plot"));
  }
  if (!interactive && state.current) {
    loadClinicalView(state.current.id);
  }
}

function downloadCsv() {
  if (!state.current) return;
  window.location.href = `/api/records/${state.current.id}/timeseries.csv`;
}

async function toggleFullscreen() {
  const frame = $("plot-frame");
  if (!document.fullscreenElement) {
    try { await frame.requestFullscreen(); }
    catch (e) { toast("Fullscreen not permitted: " + e.message, "error"); }
  } else {
    await document.exitFullscreen();
  }
}

function closeDetail() {
  $("detail-view").classList.add("hidden");
  $("records-view").classList.remove("hidden");
  state.current = null;
}

// ── actions ────────────────────────────────────────────────────────────
async function syncFromDevice() {
  const btn = $("btn-sync");
  const original = btn.textContent;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Syncing…';
  try {
    const result = await api("/api/sync", { method: "POST" });
    const n = result.saved.length;
    if (n) toast(`Downloaded ${n} record${n > 1 ? "s" : ""}.`, "success");
    else toast("No new records on device.", "");
    for (const err of result.errors) {
      toast(`Record ${err.index}: ${err.error}`, "error");
    }
    await loadRecords();
  } catch (e) {
    toast(e.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = original;
    refreshDeviceStatus();
  }
}

function renderMarkdownInto(el, text) {
  if (!text) { el.innerHTML = ""; return; }
  // marked → sanitize → typeset math.
  const html = (window.marked && window.marked.parse)
    ? window.marked.parse(text, { gfm: true, breaks: true })
    : text.replace(/\n/g, "<br>");
  const safe = (window.DOMPurify && window.DOMPurify.sanitize)
    ? window.DOMPurify.sanitize(html, { USE_PROFILES: { html: true } })
    : html;
  el.innerHTML = safe;
  if (window.renderMathInElement) {
    try {
      window.renderMathInElement(el, {
        delimiters: [
          { left: "$$", right: "$$", display: true },
          { left: "\\[", right: "\\]", display: true },
          { left: "$", right: "$", display: false },
          { left: "\\(", right: "\\)", display: false },
        ],
        throwOnError: false,
      });
    } catch (_) { /* katex not ready yet — safe to ignore */ }
  }
}

function renderAnalysis(analysis, recordId) {
  $("analysis-panel").classList.remove("hidden");
  const meta = `${analysis.model} · ${fmtDate(analysis.generated_at)}`;
  const extras = [];
  if (analysis.csv_bytes_sent) extras.push(`CSV ${Math.round(analysis.csv_bytes_sent / 1024)} KB`);
  if (analysis.code_execution_enabled) extras.push("code execution ON");
  $("analysis-model").textContent = meta + (extras.length ? " · " + extras.join(" · ") : "");
  renderMarkdownInto($("analysis-text"), analysis.text);

  const plotsEl = $("analysis-plots");
  plotsEl.innerHTML = "";
  const bust = Date.now();
  (analysis.plots || []).forEach((fname, i) => {
    const fig = document.createElement("figure");
    fig.style.margin = "0";
    const img = document.createElement("img");
    img.alt = `AI-generated plot ${i + 1}`;
    img.src = `/api/records/${recordId}/${fname}?t=${bust}`;
    const cap = document.createElement("figcaption");
    cap.textContent = `AI-generated plot ${i + 1}`;
    fig.appendChild(img);
    fig.appendChild(cap);
    plotsEl.appendChild(fig);
  });

  const codeBlocks = analysis.code_blocks || [];
  const details = $("analysis-code-details");
  const container = $("analysis-code-blocks");
  container.innerHTML = "";
  if (codeBlocks.length) {
    details.classList.remove("hidden");
    $("analysis-code-count").textContent = `(${codeBlocks.length} block${codeBlocks.length > 1 ? "s" : ""})`;
    codeBlocks.forEach((b) => {
      if (b.code) {
        const pre = document.createElement("pre");
        pre.textContent = b.code;
        container.appendChild(pre);
      }
      if (b.output) {
        const pre = document.createElement("pre");
        pre.className = "code-output";
        pre.textContent = b.output;
        container.appendChild(pre);
      }
    });
  } else {
    details.classList.add("hidden");
  }
}

async function analyzeCurrent() {
  if (!state.current) return;
  const btn = $("btn-analyze");
  const original = btn.textContent;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Analyzing…';
  try {
    const result = await api(`/api/records/${state.current.id}/analyze`, { method: "POST" });
    renderAnalysis(result, state.current.id);
    toast("Analysis complete.", "success");
    const idx = state.records.findIndex((r) => r.id === state.current.id);
    if (idx >= 0) state.records[idx].has_analysis = true;
  } catch (e) {
    toast(e.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

function downloadPdf() {
  if (!state.current) return;
  window.location.href = `/api/records/${state.current.id}/pdf`;
}

async function deleteCurrent() {
  if (!state.current) return;
  if (!confirm("Delete this recording permanently? This cannot be undone.")) return;
  try {
    await api(`/api/records/${state.current.id}`, { method: "DELETE" });
    toast("Record deleted.", "success");
    closeDetail();
    await loadRecords();
  } catch (e) {
    toast(e.message, "error");
  }
}

// ── settings ───────────────────────────────────────────────────────────
async function openSettings() {
  try {
    const s = await api("/api/settings");
    $("cfg-api-key").value = "";
    $("cfg-api-status").textContent = s.gemini_api_key_set
      ? "✓ API key currently set (leave blank to keep, enter new to replace)"
      : "No API key set yet.";
    $("cfg-model").value = s.gemini_model;
    $("cfg-hr-low").value = s.hr_low_bpm;
    $("cfg-hr-high").value = s.hr_high_bpm;
    $("cfg-filter-enabled").checked = !!s.filter_enabled;
    $("cfg-filter-hp").value = s.filter_hp_hz;
    $("cfg-filter-lp").value = s.filter_lp_hz;
    $("cfg-notch-enabled").checked = !!s.notch_enabled;
    $("cfg-notch-hz").value = s.notch_hz;
    $("restore-output").classList.add("hidden");
    $("settings-modal").classList.remove("hidden");
  } catch (e) {
    toast(e.message, "error");
  }
}

function closeSettings() {
  $("settings-modal").classList.add("hidden");
}

async function saveSettings() {
  const body = {
    gemini_model: $("cfg-model").value.trim(),
    hr_low_bpm: parseInt($("cfg-hr-low").value, 10),
    hr_high_bpm: parseInt($("cfg-hr-high").value, 10),
    filter_enabled: $("cfg-filter-enabled").checked,
    filter_hp_hz: parseFloat($("cfg-filter-hp").value),
    filter_lp_hz: parseFloat($("cfg-filter-lp").value),
    notch_enabled: $("cfg-notch-enabled").checked,
    notch_hz: parseFloat($("cfg-notch-hz").value),
  };
  const key = $("cfg-api-key").value.trim();
  if (key) body.gemini_api_key = key;
  try {
    await api("/api/settings", { method: "POST", body: JSON.stringify(body) });
    toast("Settings saved. Reopen a record to see filter changes.", "success");
    closeSettings();
    // If a record is open, re-render with new filter.
    if (state.current) await openRecord(state.current.id);
  } catch (e) {
    toast(e.message, "error");
  }
}

async function restoreDevice() {
  if (!confirm(
    "This sends the reverse-engineered restore packet to the PM10:\n\n" +
    " • Language → English\n" +
    " • Filter → ON\n" +
    " • HR alarm thresholds → the values in Settings\n\n" +
    "The device must be plugged in and powered on. Continue?"
  )) return;
  const btn = $("btn-restore-device");
  const orig = btn.textContent;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Sending…';
  try {
    const result = await api("/api/device/restore", { method: "POST" });
    const out = $("restore-output");
    out.textContent =
      `Sent  (65 B): ${result.sent_hex}\n\n` +
      `Received (${result.response_hex.length} pkts):\n` +
      result.response_hex.map((h) => "  " + h).join("\n");
    out.classList.remove("hidden");
    toast("Restore packet sent. Check the device LCD.", "success");
  } catch (e) {
    toast(e.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = orig;
  }
}

// ── wire up ────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", async () => {
  $("btn-sync").addEventListener("click", syncFromDevice);
  $("btn-settings").addEventListener("click", openSettings);
  $("btn-back").addEventListener("click", closeDetail);
  $("btn-analyze").addEventListener("click", analyzeCurrent);
  $("btn-pdf").addEventListener("click", downloadPdf);
  $("btn-csv").addEventListener("click", downloadCsv);
  $("btn-delete").addEventListener("click", deleteCurrent);
  document.querySelectorAll(".view-btn").forEach((b) => {
    b.addEventListener("click", () => setView(b.dataset.view));
  });
  $("btn-save-settings").addEventListener("click", saveSettings);
  $("btn-restore-device").addEventListener("click", restoreDevice);
  $("btn-fullscreen").addEventListener("click", toggleFullscreen);

  document.querySelectorAll(".plot-toolbar-left [data-range]").forEach((btn) => {
    btn.addEventListener("click", () => setRangePreset(btn.dataset.range));
  });

  // When entering/exiting fullscreen the plot container size changes — re-layout Plotly.
  const onFsChange = () => {
    const btn = $("btn-fullscreen");
    btn.textContent = document.fullscreenElement ? "⤢ Exit fullscreen" : "⛶ Fullscreen";
    if (state.currentSamples) {
      requestAnimationFrame(() => Plotly.Plots.resize("detail-plot"));
    }
  };
  document.addEventListener("fullscreenchange", onFsChange);
  document.addEventListener("webkitfullscreenchange", onFsChange);
  window.addEventListener("resize", () => {
    if (state.currentSamples) Plotly.Plots.resize("detail-plot");
  });

  document.querySelectorAll("[data-close]").forEach((el) => {
    el.addEventListener("click", closeSettings);
  });
  $("settings-modal").addEventListener("click", (e) => {
    if (e.target.id === "settings-modal") closeSettings();
  });

  await Promise.all([refreshDeviceStatus(), loadRecords()]);
  setInterval(refreshDeviceStatus, 5000);
});
