/* ELPCam browser GUI - talks to the FastAPI engine in elpcam/server.py */
"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const h = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const fmtBytes = (n) => n > 1e9 ? (n / 1e9).toFixed(2) + " GB" : n > 1e6 ? (n / 1e6).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1e3)) + " kB";
const fmtTime = (s) => { s = Math.floor(s); const m = Math.floor(s / 60); return `${String(m).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`; };

const api = {
  async req(method, url, body) {
    const opt = { method, headers: {} };
    if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
    const r = await fetch(url, opt);
    let data = null;
    try { data = await r.json(); } catch (_) { /* empty */ }
    if (!r.ok) throw new Error((data && data.detail) || `${r.status} ${r.statusText}`);
    return data;
  },
  get(u) { return this.req("GET", u); },
  post(u, b) { return this.req("POST", u, b === undefined ? {} : b); },
  del(u) { return this.req("DELETE", u); },
};

const S = {
  status: null, controls: [], proc: null, modes: [], currentMode: null, devices: [],
  lastEvent: 0, backend: null, activeSlider: null, histOn: false, info: null,
};

/* ------------------------------------------------------------------ toasts */
function toast(msg, level = "info", ms = 3800) {
  const t = h("div", `toast ${level}`, msg);
  $("#toasts").append(t);
  setTimeout(() => t.remove(), level === "error" ? ms * 2 : ms);
  while ($("#toasts").children.length > 5) $("#toasts").firstChild.remove();
}
async function guarded(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(okMsg); return r; }
  catch (e) { toast(e.message, "error"); return null; }
}

/* ------------------------------------------------------------------ live stream */
function startStream() {
  const img = $("#live");
  img.onerror = () => setTimeout(startStream, 2000);
  img.src = `/stream.mjpg?t=${Date.now()}`;
}

/* ------------------------------------------------------------------ devices & modes */
async function loadDevices() {
  const sel = $("#deviceSel");
  const devs = await guarded(() => api.get("/api/devices")) || [];
  S.devices = devs;
  sel.innerHTML = "";
  if (!devs.length) { sel.append(new Option("No cameras found", "")); return; }
  for (const d of devs) {
    const o = new Option(`${d.is_elp ? "★ " : ""}${d.name}${d.vid ? `  (${d.vid}:${d.pid})` : ""}`, String(d.index));
    o.dataset.path = d.path || "";
    sel.append(o);
  }
  const cur = S.status && S.status.device;
  if (cur) sel.value = String(cur.index);
}

async function loadModes() {
  const r = await guarded(() => api.get("/api/modes"));
  if (!r) return;
  S.modes = r.modes; S.currentMode = r.current;
  const sel = $("#modeSel");
  sel.innerHTML = "";
  if (!r.modes.length) { sel.append(new Option(S.status && S.status.device ? "Driver default" : "—", "")); }
  const groups = {};
  for (const m of r.modes) {
    if (!groups[m.fourcc]) { groups[m.fourcc] = document.createElement("optgroup"); groups[m.fourcc].label = m.fourcc === "MJPG" ? "MJPEG (compressed)" : m.fourcc === "YUY2" ? "YUY2 (uncompressed)" : m.fourcc; sel.append(groups[m.fourcc]); }
    const fps = m.fps.length ? ` · ${Math.max(...m.fps)} fps` : "";
    groups[m.fourcc].append(new Option(`${m.width}×${m.height}  ${m.megapixels} MP${fps}`, m.key));
  }
  if (r.current) sel.value = r.current;
  fillFps(r.fps);
  renderModeTable();
}

function fillFps(cur) {
  const sel = $("#fpsSel");
  sel.innerHTML = "";
  const m = S.modes.find((x) => x.key === $("#modeSel").value);
  const list = m && m.fps.length ? m.fps : (cur ? [cur] : []);
  if (!list.length) sel.append(new Option("auto", ""));
  for (const f of list) sel.append(new Option(String(+f.toFixed(2)), String(f)));
  if (cur) sel.value = String(list.reduce((a, b) => Math.abs(b - cur) < Math.abs(a - cur) ? b : a, list[0]));
}

async function setMode(key, fps) {
  toast(`Switching to ${key.replace(":", " ")}…`);
  const st = await guarded(() => api.post("/api/mode", { mode: key, fps: fps ? +fps : null }));
  if (st) applyStatus(st);
  await loadModes();
}

async function openSelected() {
  const sel = $("#deviceSel");
  const o = sel.selectedOptions[0];
  if (!o || o.value === "") { toast("No camera selected", "warning"); return; }
  $("#openBtn").disabled = true;
  const st = await guarded(() => api.post("/api/open", { index: +o.value, path: o.dataset.path || null }));
  $("#openBtn").disabled = false;
  if (st) { applyStatus(st); await afterOpen(); }
}
async function afterOpen() { await Promise.all([loadModes(), loadControls(), loadProcessing()]); renderDevTable(); }

/* ------------------------------------------------------------------ status */
function applyStatus(st) {
  const prev = S.status;
  S.status = st;
  S.backend = st.backend;
  const pill = $("#statePill");
  pill.className = "pill " + ({ live: "live", reconnecting: "warn", opening: "warn", error: "error" }[st.state] || "");
  pill.querySelector("b").textContent = st.state;
  $("#fpsPill").textContent = st.state === "live" ? `${st.fps.toFixed(1)} fps` : "– fps";
  $("#sizePill").textContent = st.frame_size ? `${st.frame_size[0]}×${st.frame_size[1]}${st.mode ? " " + st.mode.fourcc : ""}` : "–";
  $("#sizePill").title = st.output_size ? `Sensor frame; output ${st.output_size[0]}×${st.output_size[1]}, processing ${st.processing_ms} ms` : "";
  const isOpen = ["live", "reconnecting", "opening"].includes(st.state);
  $("#openBtn").textContent = isOpen ? "Close" : "Open";
  $("#openBtn").classList.toggle("primary", !isOpen);

  const rec = st.recording;
  $("#recBadge").hidden = !rec;
  if (rec) $("#recBadge span").textContent = `REC ${fmtTime(rec.elapsed)}`;
  $("#recBtn").classList.toggle("on", !!rec);
  $("#recBtn span").textContent = rec ? "Stop" : "Record";
  $("#recBtn2").textContent = rec ? "Stop recording" : "Start recording";
  $("#recInfo").textContent = rec ? `Recording ${rec.file} · ${rec.size[0]}×${rec.size[1]} @ ${rec.fps} fps (${rec.codec}) · ${rec.frames} frames${rec.dropped ? ` · ${rec.dropped} dropped` : ""}`
    : "Recorded at the processed output size. Frames are paced to a constant rate so playback runs at real speed.";
  const tl = st.timelapse;
  $("#tlBadge").hidden = !tl;
  if (tl) $("#tlBadge span").textContent = tl.frames;
  $("#tlBtn").classList.toggle("on", !!tl);
  $("#tlBtn2").textContent = tl ? "Stop time-lapse" : "Start time-lapse";
  $("#tlInfo").textContent = tl ? `${tl.frames} frames every ${tl.interval}s → ${tl.folder}` : "";

  for (const ev of st.events || []) {
    S.lastEvent = Math.max(S.lastEvent, ev.id);
    if (prev && ev.level !== "error") toast(ev.msg, ev.level); // errors already surface from the failing API call
  }
  const note = $("#camNote");
  if (!st.device) { note.hidden = false; note.textContent = st.error ? `Camera error: ${st.error}` : "No camera open — choose one at the top and press Open. If the list is empty, check the USB cable and the operating system's camera privacy settings."; }
  else if (st.backend === "opencv") { note.hidden = false; note.textContent = "Native control access is unavailable, so OpenCV properties are used: ranges are approximate."; }
  else if (st.modes_documented) { note.hidden = false; note.textContent = "The OS did not report video modes; the ELP spec-sheet mode list is shown instead."; }
  else note.hidden = true;

  if (prev && prev.state !== st.state && st.state === "live") afterOpen();
  else if (prev && st.mode && st.mode.key !== S.currentMode && st.state === "live") { S.currentMode = st.mode.key; loadModes(); }
  if (prev && prev.state !== st.state && st.state === "closed") { loadModes(); renderControls([]); renderDevTable(); }
  if (prev && (!!prev.recording !== !!rec || !!prev.timelapse !== !!tl) && $("#tab-media").classList.contains("active")) loadMedia();
}

async function poll() {
  try { applyStatus(await api.get(`/api/status?since=${S.lastEvent}`)); }
  catch (_) { $("#statePill b").textContent = "offline"; $("#statePill").className = "pill error"; }
  setTimeout(poll, 1000);
}

/* ------------------------------------------------------------------ camera controls */
function fmtCtl(c, v) {
  if (c.unit === "log2 s") {
    const s = Math.pow(2, v);
    return s >= 1 ? `${s} s` : `1/${Math.round(1 / s)} s`;
  }
  if (c.id === "exposure" && S.backend === "v4l2") return `${(v / 10).toFixed(1)} ms`;
  if (c.type === "menu" && c.menu) return c.menu[String(v)] || String(v);
  if (c.unit === "K") return `${v} K`;
  return String(v);
}

const AUTO_DEFAULT = ["exposure", "white_balance", "focus", "iris"];
const ctlQueue = {};
function sendControl(id, payload) {
  const q = ctlQueue[id] || (ctlQueue[id] = { busy: false, next: null });
  q.next = Object.assign(q.next || {}, payload);
  if (!q.busy) pumpControl(id);
}
async function pumpControl(id) {
  const q = ctlQueue[id];
  if (!q.next) { q.busy = false; return; }
  q.busy = true;
  const p = q.next; q.next = null;
  try { updateControl(await api.post(`/api/controls/${encodeURIComponent(id)}`, p)); }
  catch (e) { toast(e.message, "error"); }
  pumpControl(id);
}

function buildControl(c) {
  const wrap = h("div", "ctl");
  wrap.dataset.id = c.id;
  const head = h("div", "ctl-head");
  const name = h("label", "name", c.name);
  name.title = `${c.id}: ${c.min} … ${c.max}, step ${c.step}, default ${c.default}${c.unit ? " (" + c.unit + ")" : ""}`;
  const val = h("span", "val", fmtCtl(c, c.value));
  head.append(name, val);
  if (c.auto_supported && c.type === "int") {
    const lab = h("label", "auto");
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = c.auto;
    cb.addEventListener("change", () => sendControl(c.id, { auto: cb.checked }));
    lab.append(cb, document.createTextNode("Auto"));
    head.append(lab);
  }
  const rst = h("button", "reset", "↺");
  rst.title = `Reset to default (${fmtCtl(c, c.default)})`;
  rst.addEventListener("click", () => sendControl(c.id, { value: c.default, ...(c.auto_supported ? { auto: AUTO_DEFAULT.includes(c.id) } : {}) }));
  head.append(rst);
  wrap.append(head);

  if (c.type === "menu") {
    const sel = document.createElement("select");
    for (const [k, v] of Object.entries(c.menu || {})) sel.append(new Option(v, k));
    sel.value = String(c.value);
    sel.addEventListener("change", () => sendControl(c.id, { value: +sel.value }));
    wrap.append(sel);
    head.querySelector(".val").hidden = true;
  } else if (c.type === "bool") {
    const lab = h("label", "switch");
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = !!c.value;
    cb.addEventListener("change", () => sendControl(c.id, { value: cb.checked ? 1 : 0 }));
    lab.append(cb, h("i"), document.createTextNode(cb.checked ? "On" : "Off"));
    cb.addEventListener("change", () => { lab.lastChild.textContent = cb.checked ? "On" : "Off"; });
    wrap.append(lab);
    head.querySelector(".val").hidden = true;
  } else {
    const r = document.createElement("input");
    r.type = "range"; r.className = "ctl-range";
    r.min = c.min; r.max = c.max; r.step = c.step || 1; r.value = c.value;
    r.addEventListener("pointerdown", () => { S.activeSlider = c.id; });
    r.addEventListener("pointerup", () => { S.activeSlider = null; });
    r.addEventListener("input", () => {
      const cur = S.controls.find((x) => x.id === c.id) || c;
      val.textContent = fmtCtl(cur, +r.value);
      // dragging a slider that is in auto mode switches it to manual (like AMCap / guvcview)
      sendControl(c.id, { value: +r.value, ...(cur.auto ? { auto: false } : {}) });
    });
    wrap.append(r);
  }
  wrap.classList.toggle("disabled", c.auto && c.type === "int");
  return wrap;
}

function updateControl(c) {
  const i = S.controls.findIndex((x) => x.id === c.id);
  if (i >= 0) S.controls[i] = c; else S.controls.push(c);
  const wrap = $(`.ctl[data-id="${CSS.escape(c.id)}"]`);
  if (!wrap) return;
  const val = wrap.querySelector(".val");
  if (val) val.textContent = fmtCtl(c, c.value);
  const r = wrap.querySelector("input[type=range]");
  if (r && S.activeSlider !== c.id) r.value = c.value;
  const autoCb = wrap.querySelector(".auto input");
  if (autoCb) autoCb.checked = c.auto;
  const sel = wrap.querySelector("select");
  if (sel) sel.value = String(c.value);
  const sw = wrap.querySelector(".switch input");
  if (sw) { sw.checked = !!c.value; sw.parentElement.lastChild.textContent = c.value ? "On" : "Off"; }
  wrap.classList.toggle("disabled", c.auto && c.type === "int");
}

function renderControls(list) {
  S.controls = list;
  const targets = { image: $("#ctlImage"), camera: $("#ctlCamera"), other: $("#ctlOther") };
  Object.values(targets).forEach((t) => (t.innerHTML = ""));
  for (const c of list) (targets[c.group] || targets.other).append(buildControl(c));
  for (const [k, t] of Object.entries(targets)) if (!t.children.length) t.append(h("div", "muted small", k === "other" ? "" : "No controls reported."));
  $("#ctlOtherCard").hidden = !list.some((c) => !["image", "camera"].includes(c.group));
  $("#driverDlg").hidden = S.backend !== "directshow";
}

async function loadControls() {
  const list = await guarded(() => api.get("/api/controls"));
  if (list) renderControls(list);
}

async function refreshAutoControls() {
  if (S.status && S.status.state === "live" && $("#tab-camera").classList.contains("active") && S.controls.some((c) => c.auto) && !document.hidden) {
    try { (await api.get("/api/controls")).forEach(updateControl); } catch (_) { /* ignore */ }
  }
  setTimeout(refreshAutoControls, 1500);
}

/* ------------------------------------------------------------------ processing */
const PROC_SLIDERS = {
  dewarpCtl: [
    { k: "lens_fov", n: "Lens field of view", min: 60, max: 240, step: 1, u: "°", tip: "Diagonal FOV of the lens model. Adjust until straight lines look straight (uncalibrated only)." },
    { k: "out_fov", n: "View angle", min: 20, max: 175, step: 1, u: "°" },
    { k: "yaw", n: "Pan", min: -180, max: 180, step: 0.5, u: "°" },
    { k: "pitch", n: "Tilt", min: -90, max: 90, step: 0.5, u: "°" },
    { k: "roll", n: "Roll", min: -180, max: 180, step: 0.5, u: "°" },
  ],
  zoomCtl: [
    { k: "zoom", n: "Zoom", min: 1, max: 8, step: 0.05, u: "×" },
    { k: "zoom_x", n: "Horizontal position", min: -1, max: 1, step: 0.01, u: "" },
    { k: "zoom_y", n: "Vertical position", min: -1, max: 1, step: 0.01, u: "" },
  ],
  previewCtl: [{ k: "jpeg_quality", n: "JPEG quality", min: 30, max: 100, step: 1, u: "" }],
};

let procNext = null, procBusy = false;
function sendProc(patch) {
  procNext = Object.assign(procNext || {}, patch);
  Object.assign(S.proc || {}, patch);
  if (!procBusy) pumpProc();
}
async function pumpProc() {
  if (!procNext) { procBusy = false; return; }
  procBusy = true;
  const p = procNext; procNext = null;
  try { const r = await api.post("/api/processing", p); if (!procNext) applyProc(r); }
  catch (e) { toast(e.message, "error"); }
  pumpProc();
}

function buildProcSliders() {
  for (const [target, list] of Object.entries(PROC_SLIDERS)) {
    const box = $("#" + target);
    box.innerHTML = "";
    for (const s of list) {
      const wrap = h("div", "ctl"); wrap.dataset.p = s.k;
      const head = h("div", "ctl-head");
      const name = h("label", "name", s.n); if (s.tip) name.title = s.tip;
      const val = h("span", "val", "");
      const rst = h("button", "reset", "↺"); rst.title = "Reset";
      head.append(name, val, rst);
      const r = document.createElement("input");
      r.type = "range"; r.min = s.min; r.max = s.max; r.step = s.step;
      const show = (v) => (val.textContent = (s.step < 1 ? (+v).toFixed(2) : String(+v)) + s.u);
      r.addEventListener("pointerdown", () => (S.activeSlider = "p:" + s.k));
      r.addEventListener("pointerup", () => (S.activeSlider = null));
      r.addEventListener("input", () => { show(r.value); sendProc({ [s.k]: +r.value }); });
      rst.addEventListener("click", () => sendProc({ [s.k]: DEFAULTS[s.k] }));
      wrap.append(head, r);
      wrap._set = (v) => { if (S.activeSlider !== "p:" + s.k) r.value = v; show(v); };
      wrap._range = r;
      box.append(wrap);
    }
  }
}
const DEFAULTS = { lens_fov: 170, out_fov: 100, yaw: 0, pitch: 0, roll: 0, zoom: 1, zoom_x: 0, zoom_y: 0, jpeg_quality: 80 };

function applyProc(p) {
  S.proc = p;
  $$("input[data-p]").forEach((cb) => (cb.checked = !!p[cb.dataset.p]));
  for (const seg of ["#dewarpSeg", "#dewarpSeg2"]) $$(seg + " button").forEach((b) => b.classList.toggle("on", b.dataset.v === p.dewarp));
  $$("#rotSeg button").forEach((b) => b.classList.toggle("on", +b.dataset.v === p.rotate));
  $$(".ctl[data-p]").forEach((w) => { if (w._set && p[w.dataset.p] !== undefined) w._set(p[w.dataset.p]); });
  const outFov = $('.ctl[data-p="out_fov"]');
  if (outFov) outFov._range.max = p.dewarp === "panorama" ? 360 : 175;
  $$("#dewarpCtl .ctl").forEach((w) => w.classList.toggle("disabled", p.dewarp === "off"));
  const lens = $('.ctl[data-p="lens_fov"]');
  if (lens) lens.classList.toggle("disabled", p.dewarp === "off" || (p.calibration && p.use_calibration));
  if (document.activeElement !== $("#labelText")) $("#labelText").value = p.label || "";
  $("#previewWidth").value = String(p.preview_width);
  $("#mirrorBtn").classList.toggle("on", p.mirror);
  $("#flipBtn").classList.toggle("on", p.flip);
  $("#calStatus").textContent = p.calibration ? `(${p.calibration.size}, RMS ${p.calibration.rms}px)` : "(none for this resolution)";
  $("#stage").classList.toggle("draggable", p.dewarp !== "off" || p.zoom > 1.001);
}

async function loadProcessing() {
  const p = await guarded(() => api.get("/api/processing"));
  if (p) applyProc(p);
}

/* ------------------------------------------------------------------ stage interaction (virtual PTZ) */
function displayedSize() {
  const img = $("#live"), r = img.getBoundingClientRect();
  if (!img.naturalWidth) return { w: r.width, h: r.height };
  const sc = Math.min(r.width / img.naturalWidth, r.height / img.naturalHeight);
  return { w: img.naturalWidth * sc, h: img.naturalHeight * sc };
}
let hintTimer = null;
function hint(text) {
  const el = $("#stageHint");
  el.textContent = text; el.hidden = false;
  clearTimeout(hintTimer); hintTimer = setTimeout(() => (el.hidden = true), 1200);
}
function ptzHint(p) {
  if (p.dewarp !== "off") hint(`Pan ${p.yaw.toFixed(1)}° · Tilt ${p.pitch.toFixed(1)}° · View ${p.out_fov.toFixed(0)}°`);
  else hint(`Zoom ${p.zoom.toFixed(2)}×`);
}
function setupStage() {
  const stage = $("#stage");
  let drag = null;
  stage.addEventListener("pointerdown", (e) => {
    const p = S.proc;
    if (!p || (p.dewarp === "off" && p.zoom <= 1.001) || e.button !== 0) return;
    drag = { x: e.clientX, y: e.clientY, yaw: p.yaw, pitch: p.pitch, zx: p.zoom_x, zy: p.zoom_y, size: displayedSize() };
    stage.setPointerCapture(e.pointerId);
    stage.classList.add("dragging");
  });
  stage.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const p = S.proc, dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (p.dewarp !== "off") {
      const dpp = p.out_fov / Math.max(1, drag.size.w);
      const sign = p.mirror ? -1 : 1;
      const patch = { yaw: clamp(drag.yaw - sign * dx * dpp, -180, 180), pitch: clamp(drag.pitch + (p.flip ? -1 : 1) * dy * dpp, -90, 90) };
      sendProc(patch); Object.assign(p, patch);
    } else {
      const z = p.zoom;
      const patch = { zoom_x: clamp(drag.zx - dx * 2 / (drag.size.w * (z - 1)), -1, 1), zoom_y: clamp(drag.zy - dy * 2 / (drag.size.h * (z - 1)), -1, 1) };
      sendProc(patch); Object.assign(p, patch);
    }
    ptzHint(p);
  });
  const end = () => { drag = null; stage.classList.remove("dragging"); if (S.proc) applyProc(S.proc); };
  stage.addEventListener("pointerup", end);
  stage.addEventListener("pointercancel", end);
  stage.addEventListener("wheel", (e) => {
    const p = S.proc;
    if (!p || !S.status || S.status.state !== "live") return;
    e.preventDefault();
    const f = Math.exp(e.deltaY * 0.0012);
    if (p.dewarp !== "off") sendProc({ out_fov: clamp(p.out_fov * f, 20, p.dewarp === "panorama" ? 360 : 175) });
    else sendProc({ zoom: clamp(p.zoom / f, 1, 8) });
    ptzHint(p);
    $("#stage").classList.toggle("draggable", p.dewarp !== "off" || p.zoom > 1.001);
  }, { passive: false });
  stage.addEventListener("dblclick", () => { if (S.proc) sendProc(S.proc.dewarp !== "off" ? { yaw: 0, pitch: 0, roll: 0 } : { zoom: 1, zoom_x: 0, zoom_y: 0 }); });
}

/* ------------------------------------------------------------------ histogram */
const histCanvas = document.createElement("canvas");
histCanvas.width = 160; histCanvas.height = 120;
function drawHistogram() {
  if (S.histOn) {
    const img = $("#live");
    try {
      const ctx = histCanvas.getContext("2d", { willReadFrequently: true });
      ctx.drawImage(img, 0, 0, histCanvas.width, histCanvas.height);
      const d = ctx.getImageData(0, 0, histCanvas.width, histCanvas.height).data;
      const bins = 64, R = new Float32Array(bins), G = new Float32Array(bins), B = new Float32Array(bins), L = new Float32Array(bins);
      for (let i = 0; i < d.length; i += 4) {
        R[d[i] >> 2]++; G[d[i + 1] >> 2]++; B[d[i + 2] >> 2]++;
        L[((d[i] * 77 + d[i + 1] * 150 + d[i + 2] * 29) >> 8) >> 2]++;
      }
      const c = $("#histo"), g = c.getContext("2d"), W = c.width, H = c.height;
      const max = Math.max(...L, ...R, ...G, ...B) || 1;
      g.clearRect(0, 0, W, H);
      g.fillStyle = "rgba(255,255,255,.22)";
      g.beginPath(); g.moveTo(0, H);
      for (let i = 0; i < bins; i++) g.lineTo((i + 0.5) * W / bins, H - (L[i] / max) * (H - 4));
      g.lineTo(W, H); g.closePath(); g.fill();
      for (const [arr, col] of [[R, "#ff5a5f"], [G, "#38d39f"], [B, "#4c9dff"]]) {
        g.strokeStyle = col; g.lineWidth = 1.4; g.beginPath();
        for (let i = 0; i < bins; i++) { const x = (i + 0.5) * W / bins, y = H - (arr[i] / max) * (H - 4); i ? g.lineTo(x, y) : g.moveTo(x, y); }
        g.stroke();
      }
    } catch (_) { /* frame not ready */ }
  }
  setTimeout(drawHistogram, 350);
}

/* ------------------------------------------------------------------ capture */
function flash() { const f = $("#flash"); f.classList.add("on"); requestAnimationFrame(() => requestAnimationFrame(() => f.classList.remove("on"))); }
async function snapshot(fullRes) {
  const btns = [$("#stillBtn"), $("#stillBtn2")];
  if (fullRes) { btns.forEach((b) => (b.disabled = true)); toast("Capturing full-resolution still…"); }
  const r = await guarded(() => api.post("/api/snapshot", {
    format: $("#stillFmt").value, quality: +$("#stillQ").value || 95, raw: $("#stillRaw").checked, full_res: !!fullRes,
  }));
  btns.forEach((b) => (b.disabled = false));
  if (r) { flash(); if ($("#tab-media").classList.contains("active")) loadMedia(); }
}
async function toggleRecord() {
  if (S.status && S.status.recording) { await guarded(() => api.post("/api/record/stop")); }
  else {
    const fps = +$("#recFps").value || null;
    await guarded(() => api.post("/api/record/start", { container: $("#recContainer").value, fps }));
  }
  applyStatus(await api.get(`/api/status?since=${S.lastEvent}`));
}
async function toggleTimelapse() {
  if (S.status && S.status.timelapse) await guarded(() => api.post("/api/timelapse/stop"));
  else await guarded(() => api.post("/api/timelapse/start", { interval: +$("#tlInterval").value || 5, video_fps: +$("#tlFps").value || 30, make_video: $("#tlVideo").checked }));
  applyStatus(await api.get(`/api/status?since=${S.lastEvent}`));
}
function calBody() { return { cols: +$("#calCols").value, rows: +$("#calRows").value, square_mm: +$("#calSq").value }; }
async function calStatus() {
  const r = await guarded(() => api.get("/api/calibration"));
  if (!r) return;
  $("#calCount").textContent = r.session.frames;
  const saved = Object.entries(r.saved).map(([k, v]) => `${k}: RMS ${v.rms.toFixed(3)}px (${v.frames} views, ${v.created})`);
  $("#calResult").textContent = saved.length ? "Saved: " + saved.join(" · ") : "No calibration saved yet.";
}

/* ------------------------------------------------------------------ media */
async function loadMedia() {
  const list = await guarded(() => api.get("/api/media"));
  const grid = $("#mediaGrid");
  grid.innerHTML = "";
  if (!list) return;
  if (!list.length) { grid.append(h("div", "empty", "No captures yet — press Snapshot or Record.")); return; }
  for (const m of list) {
    const card = h("div", "media-card");
    const th = h("div", "thumb");
    const thumbSrc = m.kind === "folder" ? m.cover : m.name;
    if (thumbSrc) th.style.backgroundImage = `url("/api/thumb/${encodeURI(thumbSrc)}")`;
    th.append(h("span", "kind", m.kind === "folder" ? `TIME-LAPSE · ${m.count}` : m.kind.toUpperCase()));
    th.addEventListener("click", () => openLightbox(m));
    const meta = h("div", "meta");
    meta.append(h("b", null, m.name), h("span", "muted", `${fmtBytes(m.size)} · ${new Date(m.mtime * 1000).toLocaleString()}`));
    const acts = h("div", "acts");
    if (m.kind !== "folder") {
      const dl = h("a", null, "Download"); dl.href = `/media/${encodeURI(m.name)}?download=true`;
      acts.append(dl);
    }
    const del = h("button", "del", "Delete");
    del.addEventListener("click", async () => {
      if (!confirm(`Delete ${m.name}${m.kind === "folder" ? ` and its ${m.count} frames` : ""}? This cannot be undone.`)) return;
      if (await guarded(() => api.del(`/api/media/${encodeURI(m.name)}`))) loadMedia();
    });
    acts.append(del);
    card.append(th, meta, acts);
    grid.append(card);
  }
}
function openLightbox(m) {
  const body = $("#lbBody");
  body.innerHTML = "";
  let src = m.kind === "folder" ? m.cover : m.name;
  if (!src) return;
  if (m.kind === "video") {
    const v = document.createElement("video");
    v.src = `/media/${encodeURI(src)}`; v.controls = true; v.autoplay = true;
    v.addEventListener("error", () => { body.innerHTML = ""; const p = h("p", "muted", "This video codec can't be played inside the browser. "); const a = h("a", null, "Download it"); a.href = `/media/${encodeURI(src)}?download=true`; p.append(a, document.createTextNode(" to watch in VLC, Windows Media Player or similar.")); body.append(p); });
    body.append(v);
  } else {
    const i = document.createElement("img"); i.src = `/media/${encodeURI(src)}`; body.append(i);
  }
  $("#lbCap").textContent = m.kind === "folder" ? `${m.name} — last frame of ${m.count}` : m.name;
  $("#lightbox").hidden = false;
}
function closeLightbox() { $("#lightbox").hidden = true; $("#lbBody").innerHTML = ""; }

/* ------------------------------------------------------------------ presets */
async function loadPresets() {
  const list = await guarded(() => api.get("/api/presets")) || [];
  const box = $("#presetList");
  box.innerHTML = "";
  if (!list.length) { box.append(h("div", "empty", "No presets saved yet.")); return; }
  for (const p of list) {
    const row = h("div", "preset");
    const d = h("div");
    d.append(h("b", null, p.name), h("span", null, `${p.saved || ""}${p.mode ? " · " + p.mode.replace(":", " ") : ""} · ${p.controls} controls`));
    const ap = h("button", "btn primary", "Apply");
    ap.addEventListener("click", async () => { const st = await guarded(() => api.post(`/api/presets/${encodeURIComponent(p.file)}/apply`)); if (st) { applyStatus(st); afterOpen(); } });
    const del = h("button", "btn", "Delete");
    del.addEventListener("click", async () => { if (confirm(`Delete preset "${p.name}"?`) && await guarded(() => api.del(`/api/presets/${encodeURIComponent(p.file)}`))) loadPresets(); });
    row.append(d, ap, del);
    box.append(row);
  }
}

/* ------------------------------------------------------------------ info */
function kvRows(table, rows) {
  table.innerHTML = "";
  for (const [k, v] of rows) { const tr = h("tr"); tr.append(h("td", null, k), h("td", null, v)); table.append(tr); }
}
function renderDevTable() {
  const st = S.status;
  const d = st && st.device;
  kvRows($("#devTable"), d ? [
    ["Name", d.name], ["Index", String(d.index)], ["USB VID:PID", d.vid ? `${d.vid}:${d.pid}` : "—"],
    ["Recognised as ELP", d.is_elp ? "yes" : "no"], ["Control backend", st.backend || "—"],
    ["Current format", st.mode ? `${st.mode.fourcc} ${st.mode.width}×${st.mode.height} @ ${st.fps_requested || "?"} fps` : "driver default"],
    ["Device path", d.path || "—"],
  ] : [["Status", "No camera open"]]);
}
function renderModeTable() {
  const t = $("#modeTable");
  t.innerHTML = "<tr><th>Format</th><th>Size</th><th>MP</th><th>Frame rates</th></tr>";
  for (const m of S.modes) {
    const tr = h("tr");
    if (m.key === S.currentMode) tr.className = "cur";
    tr.append(h("td", null, m.fourcc), h("td", null, `${m.width}×${m.height}`), h("td", null, String(m.megapixels)), h("td", null, m.fps.map((f) => +f.toFixed(2)).join(", ")));
    tr.title = "Click to switch to this mode";
    tr.addEventListener("click", () => setMode(m.key, m.max_fps || null));
    t.append(tr);
  }
  if (!S.modes.length) { const tr = h("tr"); const td = h("td", "muted", "No camera open."); td.colSpan = 4; tr.append(td); t.append(tr); }
}
async function loadInfo() {
  const info = await guarded(() => api.get("/api/info"));
  if (!info) return;
  S.info = info;
  $("#specTitle").textContent = `${info.model} specifications`;
  kvRows($("#specTable"), info.specs);
  const ul = $("#linkList"); ul.innerHTML = "";
  for (const [label, url] of info.links) { const li = h("li"); const a = h("a", null, label); a.href = url; a.target = "_blank"; a.rel = "noopener"; li.append(a); ul.append(li); }
  $("#appVer").textContent = `ELPCam ${info.version} · OpenCV ${info.opencv} · ${info.platform}${info.simulate ? " · simulator enabled" : ""}`;
  const rc = $("#recContainer");
  rc.innerHTML = "";
  for (const c of info.containers) rc.append(new Option(c.label, c.key));
  try { const saved = localStorage.getItem("elpcam.container"); if (saved && info.containers.some((c) => c.key === saved)) rc.value = saved; } catch (_) { /* ignore */ }
  rc.onchange = () => { try { localStorage.setItem("elpcam.container", rc.value); } catch (_) { /* ignore */ } };
  $("#mediaDir").textContent = `Saved to ${info.data_dir}`;
}

/* ------------------------------------------------------------------ wiring */
function setupUI() {
  $$(".tabs button").forEach((b) => b.addEventListener("click", () => {
    $$(".tabs button").forEach((x) => x.classList.toggle("active", x === b));
    $$(".tab-body").forEach((x) => x.classList.toggle("active", x.id === "tab-" + b.dataset.tab));
    if (b.dataset.tab === "media") loadMedia();
    if (b.dataset.tab === "presets") loadPresets();
    if (b.dataset.tab === "info") { renderDevTable(); renderModeTable(); }
    if (b.dataset.tab === "capture") calStatus();
    try { localStorage.setItem("elpcam.tab", b.dataset.tab); } catch (_) { /* private mode */ }
  }));
  try { const t = localStorage.getItem("elpcam.tab"); if (t) { const b = $(`.tabs button[data-tab="${t}"]`); if (b) b.click(); } } catch (_) { /* ignore */ }

  $("#refreshDevices").addEventListener("click", loadDevices);
  $("#openBtn").addEventListener("click", async () => {
    const st = S.status;
    if (st && ["live", "reconnecting", "opening"].includes(st.state)) { applyStatus(await api.post("/api/close")); renderControls([]); loadModes(); }
    else openSelected();
  });
  $("#deviceSel").addEventListener("change", () => { if (S.status && S.status.state === "live") openSelected(); });
  $("#modeSel").addEventListener("change", () => { const m = S.modes.find((x) => x.key === $("#modeSel").value); fillFps(m ? m.max_fps : null); if (m) setMode(m.key, m.max_fps || null); });
  $("#fpsSel").addEventListener("change", () => { const k = $("#modeSel").value; if (k) setMode(k, +$("#fpsSel").value); });

  $("#resetCtl").addEventListener("click", async () => { const r = await guarded(() => api.post("/api/controls-reset")); if (r) loadControls(); });
  $("#reloadCtl").addEventListener("click", loadControls);
  $("#driverDlg").addEventListener("click", () => guarded(() => api.post("/api/driver-dialog"), "Driver dialog opened on the computer running ELPCam"));

  buildProcSliders();
  $$("input[data-p]").forEach((cb) => cb.addEventListener("change", () => sendProc({ [cb.dataset.p]: cb.checked })));
  for (const seg of ["#dewarpSeg", "#dewarpSeg2"]) $$(seg + " button").forEach((b) => b.addEventListener("click", () => sendProc({ dewarp: b.dataset.v })));
  $$("#rotSeg button").forEach((b) => b.addEventListener("click", () => sendProc({ rotate: +b.dataset.v })));
  let labelT = null;
  $("#labelText").addEventListener("input", () => { clearTimeout(labelT); labelT = setTimeout(() => sendProc({ label: $("#labelText").value }), 300); });
  $("#previewWidth").addEventListener("change", () => sendProc({ preview_width: +$("#previewWidth").value }));
  $("#resetView").addEventListener("click", () => sendProc({ yaw: 0, pitch: 0, roll: 0, out_fov: 100, lens_fov: 170 }));
  $("#resetZoom").addEventListener("click", () => sendProc({ zoom: 1, zoom_x: 0, zoom_y: 0 }));

  $("#mirrorBtn").addEventListener("click", () => sendProc({ mirror: !S.proc.mirror }));
  $("#flipBtn").addEventListener("click", () => sendProc({ flip: !S.proc.flip }));
  $("#rotBtn").addEventListener("click", () => sendProc({ rotate: (S.proc.rotate + 90) % 360 }));
  $("#histBtn").addEventListener("click", () => { S.histOn = !S.histOn; $("#histo").hidden = !S.histOn; $("#histBtn").classList.toggle("on", S.histOn); });
  $("#fsBtn").addEventListener("click", () => { const st = $("#stage"); document.fullscreenElement ? document.exitFullscreen() : st.requestFullscreen && st.requestFullscreen(); });

  $("#snapBtn").addEventListener("click", () => snapshot(false));
  $("#snapBtn2").addEventListener("click", () => snapshot(false));
  $("#stillBtn").addEventListener("click", () => snapshot(true));
  $("#stillBtn2").addEventListener("click", () => snapshot(true));
  $("#recBtn").addEventListener("click", toggleRecord);
  $("#recBtn2").addEventListener("click", toggleRecord);
  $("#tlBtn").addEventListener("click", toggleTimelapse);
  $("#tlBtn2").addEventListener("click", toggleTimelapse);

  $("#calCap").addEventListener("click", async () => {
    const r = await guarded(() => api.post("/api/calibration/capture", calBody()));
    if (!r) return;
    $("#calCount").textContent = r.frames;
    toast(r.found ? `Checkerboard found — ${r.frames} view(s) captured` : "Checkerboard not found — make sure the whole board is visible and the corner counts are right", r.found ? "info" : "warning");
  });
  $("#calCompute").addEventListener("click", async () => {
    toast("Computing calibration…");
    const r = await guarded(() => api.post("/api/calibration/compute"));
    if (r) { toast(`Calibration ${r.size}: RMS ${r.rms.toFixed(3)} px`); calStatus(); loadProcessing(); }
  });
  $("#calClear").addEventListener("click", async () => {
    if (!confirm("Clear captured views and the saved calibration for the current resolution?")) return;
    await guarded(() => api.post("/api/calibration/clear")); calStatus(); loadProcessing();
  });

  $("#mediaRefresh").addEventListener("click", loadMedia);
  $("#lightbox").addEventListener("click", (e) => { if (e.target.id === "lightbox" || e.target.closest(".lb-close")) closeLightbox(); });
  $("#presetSave").addEventListener("click", async () => {
    const name = $("#presetName").value.trim();
    if (!name) { toast("Enter a preset name", "warning"); return; }
    if (await guarded(() => api.post("/api/presets", { name, include_mode: $("#presetMode").checked }))) { $("#presetName").value = ""; loadPresets(); }
  });

  document.addEventListener("keydown", (e) => {
    if (e.target.closest("input, select, textarea") || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === "Escape") { closeLightbox(); return; }
    const k = e.key.toLowerCase();
    const acts = {
      s: () => snapshot(e.shiftKey), r: toggleRecord, m: () => $("#mirrorBtn").click(), v: () => $("#flipBtn").click(),
      t: () => $("#rotBtn").click(), f: () => $("#fsBtn").click(), h: () => $("#histBtn").click(),
      d: () => { const o = ["off", "perspective", "panorama"]; sendProc({ dewarp: o[(o.indexOf(S.proc.dewarp) + 1) % 3] }); },
    };
    if (acts[k] && S.proc) { e.preventDefault(); acts[k](); }
  });
  setupStage();
}

(async function init() {
  setupUI();
  startStream();
  await loadInfo();
  try { const st = await api.get("/api/status"); S.lastEvent = Math.max(0, ...st.events.map((e) => e.id)); applyStatus(st); } catch (_) { /* offline */ }
  await loadDevices();
  await Promise.all([loadModes(), loadControls(), loadProcessing()]);
  renderDevTable();
  poll();
  refreshAutoControls();
  drawHistogram();
})();
