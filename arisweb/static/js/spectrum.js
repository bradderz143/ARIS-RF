// Spectrum tab. One dongle, or all of them at the same time ("compare"). Because each dongle has its own antenna
// and gain, "Above floor" plots every line relative to its own dongle's noise floor.
const SP = {running: false, busy: false, timer: null, data: null, holdOn: false, hold: {}, holdSince: null, holdN: 0,
            key: '', sel: null, yMin: null, yMax: null, meta: null, retry: false, geom: null, scale: 'abs',
            preset: null, fmOpen: false, labelBoxes: []};
const SP_L = 44, SP_R = 10, SP_T = 22, SP_B = 22;

function spColor(serial){
  const ds = SP.meta ? SP.meta.dongles : []; const k = ds.findIndex(d => d.serial === serial);
  return DONGLE_COLORS[k < 0 ? 3 : k];
}
function spSave(){ try { localStorage.setItem('aris_spec', JSON.stringify({p: SP.preset, d: document.getElementById('sp-dongle').value,
  s: document.getElementById('sp-start').value, e: document.getElementById('sp-stop').value, sc: SP.scale})); } catch (e) {} }
function spLoad(){ try { return JSON.parse(localStorage.getItem('aris_spec') || 'null'); } catch (e) { return null; } }

async function specInit(){
  if (SP.meta){ specDraw(); return; }
  let m; try { m = await apiGet('/api/spectrum/meta'); } catch (e) { specStatus(e.message || 'Spectrum not available'); return; }
  SP.meta = m;
  const ds = document.getElementById('sp-dongle'); ds.innerHTML = '';
  const all = el('option', '', 'All dongles (compare)'); all.value = 'all'; ds.appendChild(all);
  m.dongles.forEach(d => { const o = el('option', '', d.name + (d.antenna_note ? ' - ' + d.antenna_note : '') +
      (d.best_for && d.best_for.length ? ' (best for ' + d.best_for.join(', ') + ')' : '')); o.value = d.serial; ds.appendChild(o); });
  const box = document.getElementById('sp-presets'); box.innerHTML = '';
  m.presets.forEach(p => { const c = el('div', 'chip', p.name); c.dataset.id = p.id; c.onclick = () => specPreset(p.id); box.appendChild(c); });
  const saved = spLoad();
  ds.value = saved && saved.d ? saved.d : (m.default || 'all'); if (!ds.value) ds.value = 'all';
  if (saved && saved.sc) specScale(saved.sc, true);
  if (saved && saved.p && m.presets.some(p => p.id === saved.p)) specPreset(saved.p);
  else if (saved && saved.s && saved.e){ document.getElementById('sp-start').value = saved.s; document.getElementById('sp-stop').value = saved.e; specCustom(); }
  else specPreset('fm');
}
function specPreset(id){
  const p = SP.meta && SP.meta.presets.find(x => x.id === id); if (!p) return;
  SP.preset = id;
  document.getElementById('sp-start').value = p.start; document.getElementById('sp-stop').value = p.stop;
  document.querySelectorAll('#sp-presets .chip').forEach(c => c.classList.toggle('on', c.dataset.id === id));
  specClear(); spSave();
}
function specCustom(){ SP.preset = null; document.querySelectorAll('#sp-presets .chip').forEach(c => c.classList.remove('on')); specClear(); spSave(); }
function specScale(v, quiet){
  SP.scale = v; document.querySelectorAll('#sp-scale button').forEach(b => b.classList.toggle('on', b.dataset.v === v));
  SP.yMin = null; SP.yMax = null; SP.hold = {}; SP.holdN = 0; SP.holdSince = SP.holdOn ? new Date() : null;
  if (!quiet){ specDraw(); spSave(); }
}
function specStatus(t){ document.getElementById('sp-status').textContent = t; }
function specParams(){
  const d = document.getElementById('sp-dongle').value;
  const s = parseFloat(document.getElementById('sp-start').value), e = parseFloat(document.getElementById('sp-stop').value);
  return {d: d, s: s, e: e, key: d + '|' + s + '|' + e};
}
function specClear(){
  SP.hold = {}; SP.holdN = 0; SP.holdSince = SP.holdOn ? new Date() : null;
  SP.yMin = null; SP.yMax = null; SP.data = null; SP.sel = null; SP.namesMsg = null;
  const wf = document.getElementById('wf-canvas'); const ctx = wf.getContext('2d');
  ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.fillStyle = '#05070d'; ctx.fillRect(0, 0, wf.width, wf.height);
  document.getElementById('sp-sel').style.display = 'none';
  document.getElementById('sp-peaks').innerHTML = '';
  document.getElementById('sp-warn').innerHTML = '';
  document.getElementById('sp-legend').innerHTML = '';
  spSave(); specDraw();
}
async function specOnce(){
  if (SP.busy) return;
  const p = specParams();
  if (!(p.e > p.s)){ specStatus('Enter a start and stop frequency'); return; }
  if (p.key !== SP.key){ SP.key = p.key; specClear(); }
  SP.busy = true;
  const liveSpan = SP.meta ? SP.meta.live_max_span_mhz : 0.8;
  specStatus((p.e - p.s) <= liveSpan + 1e-6 ? 'Capturing...' : 'Sweeping...');
  try{
    const r = await fetch('/api/spectrum?dongle=' + encodeURIComponent(p.d) + '&start=' + p.s + '&stop=' + p.e);
    if (toLogin(r)) return;
    const j = await r.json();
    if (!r.ok){ SP.retry = !!j.busy; specStatus((j.busy ? 'Dongle busy, retrying: ' : '') + (j.error || 'error')); return; }
    SP.retry = false;
    if (specParams().key !== p.key) return;
    SP.data = j; specUpdateHold(); specDraw(); specWaterfall(); specPeaks(); specLegend(); specWarnings();
    if (SP.sel !== null) specSelect(SP.sel, true);
    let st = (j.mode === 'live' ? 'Live IQ' : 'Sweep') + ' ' + j.time + ' | ' + j.took_ms + ' ms';
    if (j.spikes_removed) st += ' | receiver spikes removed';
    if (SP.holdOn && SP.holdSince) st += ' | peak hold since ' + SP.holdSince.toTimeString().slice(0, 5) + ' (' + SP.holdN + (SP.holdN === 1 ? ' sweep)' : ' sweeps)');
    specStatus(st);
  }catch(e){ specStatus('Connection problem'); }
  finally{ SP.busy = false; }
}
function specToggle(){
  SP.running = !SP.running;
  const b = document.getElementById('sp-live'); b.classList.toggle('on', SP.running); b.textContent = SP.running ? 'Stop' : 'Live';
  clearTimeout(SP.timer);
  if (SP.running) specLoop(); else specStatus('Stopped');
}
async function specLoop(){
  if (!SP.running) return;
  if (document.hidden || activeTab() !== 'spectrum'){ SP.timer = setTimeout(specLoop, 1000); return; }
  await specOnce();
  const live = SP.data && SP.data.mode === 'live';
  SP.timer = setTimeout(specLoop, SP.retry ? 2000 : (live ? 250 : 600));
}
function specHoldToggle(){
  SP.holdOn = !SP.holdOn; document.getElementById('sp-hold').classList.toggle('on', SP.holdOn);
  SP.hold = {}; SP.holdN = 0; SP.holdSince = SP.holdOn ? new Date() : null;
  if (SP.holdOn && SP.data) specUpdateHold();
  specDraw();
}

// What gets plotted: one line per dongle, in dBFS (0 = an 8-bit full-scale sine, so nothing sits above 0; not a
// calibrated strength) or in dB above that dongle's own floor.
function spLines(j){
  j = j || SP.data; if (!j) return [];
  const tr = (j.traces || []).filter(t => t.db);
  const rel = SP.scale === 'rel';
  if (!tr.length) return [{key: 'combined', name: j.dongle_name, color: '#34d399', freqs: j.freqs,
                           vals: rel ? j.db.map(v => v - j.floor_db) : j.db, floor: rel ? 0 : j.floor_db}];
  return tr.map(t => ({key: t.dongle, name: t.name, color: spColor(t.dongle), freqs: t.freqs, floor: rel ? 0 : t.floor_db,
                       vals: rel ? t.db.map(v => Math.round((v - t.floor_db) * 10) / 10) : t.db, health: t.health}));
}
function specUpdateHold(){
  if (!SP.holdOn || !SP.data) return;
  SP.holdN += 1;
  spLines().forEach(l => {
    const h = SP.hold[l.key];
    if (!h || h.length !== l.vals.length){ SP.hold[l.key] = l.vals.slice(); return; }
    for (let i = 0; i < l.vals.length; i++) if (l.vals[i] > h[i]) h[i] = l.vals[i];
  });
}
function specCanvas(id){
  const c = document.getElementById(id); const dpr = window.devicePixelRatio || 1;
  const w = Math.max(200, c.clientWidth), h = c.clientHeight;
  if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)){ c.width = Math.round(w * dpr); c.height = Math.round(h * dpr); }
  const ctx = c.getContext('2d'); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return {c: c, ctx: ctx, w: w, h: h};
}
function niceStep(x){ const p = Math.pow(10, Math.floor(Math.log10(x))); for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= x) return m * p; return 10 * p; }
function fmtMHz(f, st){ const d = st >= 1 ? 0 : (st >= 0.1 ? 1 : (st >= 0.01 ? 2 : 3)); return f.toFixed(d); }
function valAt(freqs, vals, f){ let bi = 0, bd = 1e9; for (let i = 0; i < freqs.length; i++){ const d = Math.abs(freqs[i] - f); if (d < bd){ bd = d; bi = i; } } return vals[bi]; }
function spTopAt(lines, f){ let best = -1e9; lines.forEach(l => { const v = valAt(l.freqs, l.vals, f); if (v > best) best = v; }); return best; }
// The level scale: RTL-SDR sweeps and every live IQ frame in dBFS; a Signal Hound's sweeps in calibrated dBm; other
// receivers' sweeps in their own dB; 'mixed' when receivers on different scales are compared (read above floor).
function unitOf(u){ return u === 'dBFS' ? ' dBFS' : u === 'dBm' ? ' dBm' : ' dB'; }
function axisLabel(u){
  return u === 'dBm' ? 'dBm (calibrated)' : u === 'dB' ? "dB (the receiver's own scale)"
    : u === 'mixed' ? 'dB (receivers on different scales: use Above floor)' : 'dBFS (0 = full scale)';
}
function readoutAt(f){
  const lines = spLines(); let t = f.toFixed(4) + ' MHz'; if (!lines.length) return t;
  const u = SP.scale === 'rel' ? ' dB above floor' : unitOf(SP.data && SP.data.unit);
  if (lines.length > 1) return t + '  |  ' + lines.map(l => l.name.replace('Dongle ', 'D') + ' ' + valAt(l.freqs, l.vals, f).toFixed(1)).join('  ') + u;
  return t + '  |  ' + valAt(lines[0].freqs, lines[0].vals, f).toFixed(1) + u;
}
function specLegend(){
  const box = document.getElementById('sp-legend'); box.innerHTML = '';
  const j = SP.data; if (!j) return;
  (j.traces || []).forEach(t => {
    const row = el('div', 'lg'); const i = el('i'); i.style.background = spColor(t.dongle); row.appendChild(i);
    row.appendChild(el('strong', '', t.name));
    if (t.antenna) row.appendChild(el('span', 'sub', t.antenna));
    if (t.error) row.appendChild(el('span', 'bad', t.health === 'busy' ? 'busy (in use by ARIS)' : 'error: ' + t.error));
    else if (t.health === 'flat') row.appendChild(el('span', 'bad', 'flat line: check antenna'));
    else row.appendChild(el('span', 'sub', t.peaks + ' signal' + (t.peaks === 1 ? '' : 's') + ' · floor ' + Number(t.floor_db).toFixed(0) + unitOf(t.unit || j.unit) + ' · gain ' + t.gain_db));
    box.appendChild(row);
  });
}
function specWarnings(){
  const box = document.getElementById('sp-warn'); box.innerHTML = '';
  ((SP.data && SP.data.warnings) || []).forEach(w => box.appendChild(el('div', 'warnbox', w)));
}
function specDraw(){
  const g = specCanvas('spec-canvas'), ctx = g.ctx, w = g.w, h = g.h;
  ctx.fillStyle = '#05070d'; ctx.fillRect(0, 0, w, h);
  const p = specParams(), j = SP.data;
  const L = SP_L, R = SP_R, T = SP_T, B = SP_B, pw = w - L - R, ph = h - T - B;
  if (!(p.e > p.s)) return;
  const lines = spLines(), rel = SP.scale === 'rel';
  if (lines.length){
    let mx = -1e9, fl = 1e9;
    lines.forEach(l => { l.vals.forEach(v => { if (v > mx) mx = v; }); if (l.floor < fl) fl = l.floor; });
    Object.values(SP.hold).forEach(hv => hv.forEach(v => { if (v > mx) mx = v; }));
    // dBFS: the axis ends at 0 (full scale) unless a level really sits above it; other scales follow the data
    const fsScale = !j || !j.unit || j.unit === 'dBFS';
    const lo = rel ? -10 : Math.floor((fl - 10) / 10) * 10;
    const top = Math.ceil((mx + 6) / 10) * 10;
    const hi = rel ? Math.max(20, top) : fsScale ? Math.min(mx <= 0 ? 0 : 10, top) : top;
    SP.yMin = SP.yMin === null ? lo : Math.min(SP.yMin, lo);
    SP.yMax = SP.yMax === null ? hi : Math.max(SP.yMax, hi);
  }
  const y0 = SP.yMin === null ? (rel ? -10 : -60) : SP.yMin, y1 = SP.yMax === null ? (rel ? 30 : 0) : SP.yMax;
  const X = f => L + (f - p.s) / (p.e - p.s) * pw, Y = v => T + (1 - (v - y0) / (y1 - y0)) * ph;
  ctx.strokeStyle = '#1f2937'; ctx.lineWidth = 1; ctx.fillStyle = '#64748b'; ctx.font = '11px system-ui, sans-serif';
  for (let v = Math.ceil(y0 / 10) * 10; v <= y1; v += 10){
    ctx.beginPath(); ctx.moveTo(L, Y(v)); ctx.lineTo(w - R, Y(v)); ctx.stroke(); ctx.textAlign = 'right'; ctx.fillText(String(v), L - 6, Y(v) + 4);
  }
  const stepF = niceStep((p.e - p.s) / 5);
  for (let f = Math.ceil(p.s / stepF) * stepF; f <= p.e + 1e-9; f += stepF){
    ctx.beginPath(); ctx.moveTo(X(f), T); ctx.lineTo(X(f), T + ph); ctx.stroke();
    ctx.textAlign = 'center'; ctx.fillText(fmtMHz(f, stepF), X(f), h - 6);
  }
  ctx.textAlign = 'left'; ctx.fillText(rel ? 'dB above floor' : axisLabel(j && j.unit), 6, 14); ctx.textAlign = 'right'; ctx.fillText('MHz', w - R, 14);
  SP.geom = {L: L, pw: pw, s: p.s, e: p.e};
  if (!j){ ctx.fillStyle = '#94a3b8'; ctx.textAlign = 'center'; ctx.fillText('Press Live or Sweep once', L + pw / 2, T + ph / 2); return; }
  ctx.save(); ctx.beginPath(); ctx.rect(L, T, pw, ph); ctx.clip();
  // noise floors (dashed): one per dongle in dB mode, a single 0 line in "above floor" mode
  ctx.setLineDash([4, 4]); ctx.lineWidth = 1;
  if (rel){ ctx.strokeStyle = '#475569'; ctx.beginPath(); ctx.moveTo(L, Y(0)); ctx.lineTo(w - R, Y(0)); ctx.stroke(); }
  else lines.forEach(l => { ctx.strokeStyle = l.color; ctx.globalAlpha = 0.45; ctx.beginPath(); ctx.moveTo(L, Y(l.floor)); ctx.lineTo(w - R, Y(l.floor)); ctx.stroke(); });
  ctx.globalAlpha = 1; ctx.setLineDash([]);
  const path = (fr, vs) => { ctx.beginPath(); vs.forEach((v, i) => { const x = X(fr[i]), y = Y(v); if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y); }); };
  if (SP.holdOn) lines.forEach(l => { const hv = SP.hold[l.key]; if (!hv || hv.length !== l.vals.length) return;
    ctx.strokeStyle = lines.length > 1 ? l.color : '#f59e0b'; ctx.globalAlpha = 0.55; ctx.lineWidth = 1; ctx.setLineDash([2, 2]);
    path(l.freqs, hv); ctx.stroke(); ctx.setLineDash([]); ctx.globalAlpha = 1; });
  if (lines.length > 1){
    ctx.globalAlpha = 0.85; lines.forEach(l => { path(l.freqs, l.vals); ctx.strokeStyle = l.color; ctx.lineWidth = 1.3; ctx.stroke(); }); ctx.globalAlpha = 1;
  } else if (lines.length){
    const l = lines[0]; path(l.freqs, l.vals); ctx.strokeStyle = l.color; ctx.lineWidth = 1.25; ctx.stroke();
    ctx.lineTo(X(l.freqs[l.freqs.length - 1]), T + ph); ctx.lineTo(X(l.freqs[0]), T + ph); ctx.closePath();
    ctx.fillStyle = 'rgba(16,185,129,0.12)'; ctx.fill();
  }
  // Markers: every peak gets a small triangle; only a few get a label, and labels never overlap.
  const pk = (j.peaks || []).slice();
  const order = pk.slice().sort((a, b) => (b.flag - a.flag) || (b.snr_db - a.snr_db));
  // Every marker's triangle is reserved first, so a label never covers another signal's marker.
  const pos = order.map(m => ({m: m, x: X(m.mhz), y: Y(spTopAt(lines, m.mhz))}));
  const boxes = pos.map(q => ({x0: q.x - 5, x1: q.x + 5, y0: q.y - 11, y1: q.y - 2}));
  pos.forEach(q => { ctx.fillStyle = q.m.name ? '#34d399' : (q.m.flag ? '#f59e0b' : (q.m.kind === 'fm' ? '#94a3b8' : '#e2e8f0'));
    ctx.beginPath(); ctx.moveTo(q.x, q.y - 3); ctx.lineTo(q.x - 4, q.y - 10); ctx.lineTo(q.x + 4, q.y - 10); ctx.closePath(); ctx.fill(); });
  let labelled = 0; ctx.font = '600 10.5px system-ui, sans-serif'; ctx.textAlign = 'center';
  pos.forEach(q => {
    if (labelled >= 6) return;
    const m = q.m, txt = m.flag ? m.mhz.toFixed(m.checked === 'off_channel' ? 2 : 1) + ' off-plan' : (m.kind === 'fm' ? m.mhz.toFixed(1) : m.mhz.toFixed(3));
    const tw = ctx.measureText(txt).width + 6;
    // An off-plan label matters most: if it would cover another marker, try it beside, above or below its marker.
    const spots = [[0, 0]];
    if (m.flag) [0, -13, -26, -39, 26].forEach(dy => [0, tw / 2 - 6, 6 - tw / 2].forEach(dx => spots.push([dx, dy])));
    for (const [dx, dy] of spots){
      const ty = Math.max(T + 10, q.y - 14 + dy);
      const cx = Math.min(Math.max(q.x + dx, L + tw / 2), w - R - tw / 2);   // keep the label inside the plot
      const bx = {x0: cx - tw / 2, x1: cx + tw / 2, y0: ty - 11, y1: ty + 2};
      if (boxes.some(b => bx.x0 < b.x1 && bx.x1 > b.x0 && bx.y0 < b.y1 && bx.y1 > b.y0)) continue;
      boxes.push(bx); labelled += 1; ctx.fillStyle = m.name ? '#6ee7b7' : (m.flag ? '#f59e0b' : '#e2e8f0'); ctx.fillText(txt, cx, ty);
      return;
    }
  });
  ctx.restore();
  if (SP.sel !== null){ ctx.strokeStyle = '#60a5fa'; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(X(SP.sel), T); ctx.lineTo(X(SP.sel), T + ph); ctx.stroke(); }
}
function specWaterfall(){
  const j = SP.data; if (!j) return;
  const c = document.getElementById('wf-canvas'); const dpr = window.devicePixelRatio || 1;
  const w = Math.round(c.clientWidth * dpr), h = Math.round(c.clientHeight * dpr);
  if (c.width !== w || c.height !== h){ c.width = w; c.height = h; }
  const ctx = c.getContext('2d'); ctx.setTransform(1, 0, 0, 1, 0, 0);
  // Slow sweeps get tall rows so the waterfall fills in about 24 sweeps instead of hundreds.
  const rowCss = j.mode === 'live' ? 2 : Math.max(4, Math.floor(c.clientHeight / 24));
  const rowH = Math.max(1, Math.round(rowCss * dpr));
  ctx.drawImage(c, 0, rowH);
  const p = specParams(); const L = Math.round(SP_L * dpr), R = Math.round(SP_R * dpr), pw = w - L - R;
  const lines = spLines(); const rel = SP.scale === 'rel';
  const fl = rel ? 0 : Math.min.apply(null, lines.map(l => l.floor));
  const lo = fl - 3, hi = fl + 40;
  const col = new Array(pw).fill(null);
  lines.forEach(l => { for (let i = 0; i < l.freqs.length; i++){
    const x = Math.floor((l.freqs[i] - p.s) / (p.e - p.s) * pw);
    if (x >= 0 && x < pw && (col[x] === null || l.vals[i] > col[x])) col[x] = l.vals[i]; } });
  let last = lo; for (let x = 0; x < pw; x++){ if (col[x] === null) col[x] = last; else last = col[x]; }
  const img = ctx.createImageData(w, rowH), d = img.data;
  for (let x = 0; x < w; x++){                                  // one colour per column, copied down the row
    const rgb = (x >= L && x < L + pw) ? heatColor((col[x - L] - lo) / (hi - lo)) : [5, 7, 13];
    d[x * 4] = rgb[0]; d[x * 4 + 1] = rgb[1]; d[x * 4 + 2] = rgb[2]; d[x * 4 + 3] = 255;
  }
  for (let y = 1; y < rowH; y++) d.copyWithin(y * w * 4, 0, w * 4);
  ctx.putImageData(img, 0, 0);
  SP.wfRows = (SP.wfRows || 0) + 1;
  if (rowCss >= 9 || SP.wfRows % 30 === 1){                       // time stamps in the left margin
    ctx.fillStyle = '#94a3b8'; ctx.font = Math.round(9 * dpr) + 'px system-ui, sans-serif'; ctx.textAlign = 'left';
    ctx.fillText((j.time || '').slice(0, 5), Math.round(3 * dpr), Math.min(rowH, Math.round(9 * dpr)));
  }
}
// FM entries are named by their channel (one decimal); a carrier the IQ check measured off the channels, and
// anything outside the FM band, by its own frequency.
function pkMhz(m){ return (m.kind === 'signal' || m.checked === 'off_channel') ? m.mhz.toFixed(3) : m.mhz.toFixed(1); }
function pkRow(m){
  const r = el('div', 'pk' + (m.flag ? ' flag' : '') + (SP.sel !== null && Math.abs(SP.sel - m.mhz) < 1e-6 ? ' sel' : ''));
  const left = el('div'); left.appendChild(el('div', 'f', pkMhz(m) + ' MHz' + (m.name ? '  ' + m.name : (m.flag ? '  off-plan' : ''))));
  if (m.name) r.classList.add('named');
  left.appendChild(el('div', 'b', m.band));
  // dB above the dongle's own noise floor (signal-to-noise), not a signal strength: bigger is clearer
  const hb = Object.entries(m.heard_by || {}).map(([n, s]) => n.replace('Dongle ', 'D') + ' ' + Number(s).toFixed(0)).join(' · ');
  const right = el('div', 's'); right.appendChild(el('div', '', Number(m.snr_db).toFixed(0) + ' dB above noise'));
  if (hb) right.appendChild(el('div', 'b', hb));
  r.appendChild(left); r.appendChild(right); r.onclick = () => specSelect(m.mhz);
  return r;
}
function specPeaks(){
  const box = document.getElementById('sp-peaks'); box.innerHTML = '';
  const j = SP.data; if (!j) return;
  const pk = j.peaks || [];
  if (!pk.length){ box.appendChild(el('div', 'muted', 'No signals above the noise floor in this range.')); return; }
  const fm = pk.filter(m => m.kind === 'fm'), rest = pk.filter(m => m.kind !== 'fm');
  box.appendChild(el('div', 'h', 'Signals (' + pk.length + ')'));
  box.appendChild(el('div', 'muted', 'dB above noise: how far each signal rises over the dongle\'s own noise floor (bigger is clearer; D1, D2 = each dongle). The plot\'s dBFS is relative to the dongle\'s full scale (0 = the strongest level an 8-bit dongle can read). Neither is a calibrated strength (a Signal Hound\'s sweeps are: those show in dBm).'));
  const fmAll = pk.filter(m => m.kind === 'fm' || m.kind === 'fm_offplan'), unnamed = fmAll.filter(m => !m.name);
  // Stations already checked (on their channel with no name, or off the channels) are only read again on request,
  // except a channel where no name was read more than a day ago (one look is not proof that it sends none).
  const day = Date.now() / 1000 - 86400;
  const fresh = unnamed.filter(m => !m.checked || m.checked === 'rds_weak'
                                    || (['no_name', 'not_heard', 'too_weak'].includes(m.checked) && (m.checked_epoch || 0) < day));
  if (unnamed.length){
    const row = el('div', 'name-row');
    const b = el('button', 'btn-ghost', SP.naming ? 'Naming stations...' : (fresh.length ?
      'Name all FM stations (' + fresh.length + ')' : 'Check the unnamed again (' + unnamed.length + ')'));
    b.disabled = !!SP.naming; b.onclick = () => specNameAll(fresh.length ? fresh : unnamed); row.appendChild(b);
    const nn = fmAll.length - unnamed.length, nc = unnamed.length - fresh.length;
    row.appendChild(el('span', 'muted', (nn || nc) ? [nn ? nn + ' named' : '', nc ? nc + ' checked, no name read' : ''].filter(Boolean).join(', ') + '.'
      : 'Reads every station\'s own name (RDS): one 6 s capture per 1 MHz of band, both dongles at once.'));
    box.appendChild(row);
  }
  const res = el('div'); res.id = 'sp-names'; box.appendChild(res);
  if (SP.namesMsg) res.appendChild(el('div', SP.namesMsg[0], SP.namesMsg[1]));
  rest.forEach(m => box.appendChild(pkRow(m)));
  if (fm.length >= 4){
    const g = el('div', 'pk'); const left = el('div');
    const nn = fm.filter(m => m.name).length;
    left.appendChild(el('div', 'f', 'FM band: ' + fm.length + ' signals' + (nn ? ' (' + nn + ' named)' : '')));
    left.appendChild(el('div', 'b', fm.slice().sort((a, b) => a.mhz - b.mhz).map(m => m.mhz.toFixed(1)).join(', ')));
    g.appendChild(left); g.appendChild(el('div', 's', SP.fmOpen ? 'hide' : 'show'));
    g.onclick = () => { SP.fmOpen = !SP.fmOpen; specPeaks(); };
    box.appendChild(g);
    if (SP.fmOpen) fm.forEach(m => box.appendChild(pkRow(m)));
    if (!j.fm_plan_checked) box.appendChild(el('div', 'note', 'This range is too wide to check FM channels (each point covers ' + j.step_khz + ' kHz). Tap the FM 88-108 preset to check which signals are off the station plan.'));
  } else fm.forEach(m => box.appendChild(pkRow(m)));
}
function spNearestPeak(f){
  const j = SP.data; if (!j) return null; const win = (j.stop_mhz - j.start_mhz) * 0.01;
  let best = null; (j.peaks || []).forEach(m => { if (Math.abs(m.mhz - f) <= win && (!best || m.snr_db > best.snr_db)) best = m; });
  return best;
}
function specSelect(f, quiet){
  SP.sel = f;
  document.getElementById('sp-readout').textContent = readoutAt(f);
  const pnl = document.getElementById('sp-sel'); pnl.style.display = 'block';
  const m = spNearestPeak(f);
  document.getElementById('sp-sel-f').textContent = f.toFixed(4) + ' MHz';
  document.getElementById('sp-sel-sub').textContent = m && Math.abs(m.mhz - f) < 1e-6 ? m.band + ' | +' + Number(m.snr_db).toFixed(1) + ' dB' + (m.best ? ' | strongest on ' + m.best : '') : readoutAt(f).split('|').slice(1).join('|');
  if (!quiet){ specDraw(); specPeaks(); }
}
function specFreqAt(ev){
  if (!SP.geom) return null;
  const r = document.getElementById('spec-canvas').getBoundingClientRect();
  const cx = (ev.touches && ev.touches.length ? ev.touches[0].clientX : ev.clientX) - r.left;
  const g = SP.geom, f = g.s + (cx - g.L) / g.pw * (g.e - g.s);
  return (f < g.s || f > g.e) ? null : f;
}
function specHover(ev){ const f = specFreqAt(ev); if (f !== null) document.getElementById('sp-readout').textContent = readoutAt(f); }
function specClick(ev){ let f = specFreqAt(ev); if (f === null) return; const m = spNearestPeak(f); if (m) f = m.mhz; specSelect(f); }
function specAsk(){
  if (SP.sel === null) return; if (SP.running) specToggle();
  sendQuick('Investigate ' + SP.sel.toFixed(4) + ' MHz on all dongles and tell me in plain English what it is and whether it is normal.');
}
// Decode: the same investigation as the chat (every dongle at once, the decoders that suit the band) for the
// chosen time, run by the GUI itself; progress and the result card appear right here.
SP.decT = 30; SP.job = null;
function specDecodeTime(s){ SP.decT = s; document.querySelectorAll('#sp-dec-t button').forEach(b => b.classList.toggle('on', Number(b.dataset.s) === s)); }
function decBox(){ return document.getElementById('sp-result'); }
async function specDecode(){
  if (SP.sel === null) return; if (SP.running) specToggle();
  const f = SP.sel, box = decBox(); box.innerHTML = '';
  let j; try { j = await post('/api/decode', {freq_mhz: f, seconds: SP.decT}); }
  catch (e){ box.appendChild(el('div', 'warnbox', 'Could not start: ' + e.message)); return; }
  SP.job = j.job; specDecodePoll();
}
function decProgress(j){
  const box = decBox(); box.innerHTML = '';
  const c = el('div', 'dec-card');
  c.appendChild(el('div', 'dec-h', 'Decoding ' + Number(j.freq_mhz).toFixed(4) + ' MHz for ' + j.seconds + ' s on every dongle...'));
  const bar = el('div', 'bar'); const fill = el('i'); fill.style.width = Math.min(100, Math.round(100 * j.elapsed / Math.max(1, j.expected))) + '%';
  bar.appendChild(fill); c.appendChild(bar); c.appendChild(el('div', 'act-sub', j.progress || ''));
  box.appendChild(c);
  document.getElementById('sp-dec-go').disabled = true;
}
function decResult(j){
  const box = decBox(); box.innerHTML = ''; document.getElementById('sp-dec-go').disabled = false;
  if (j.state === 'error'){ box.appendChild(el('div', 'warnbox', 'Decode failed: ' + j.error)); return; }
  const k = j.card || {}; const ev = k.evidence || 'none';
  const c = el('div', 'dec-card ' + ev);
  const top = el('div', 'dec-top');
  top.appendChild(el('span', 'tag ' + ev, ev === 'confirmed_decode' ? 'IDENTIFIED' : (ev === 'signal_present' ? 'NOT IDENTIFIED' : 'NOTHING HEARD')));
  top.appendChild(el('span', 'act-sub', Number(k.freq_mhz).toFixed(4) + ' MHz | ' + (k.listened_seconds || j.seconds) + ' s | ' + (k.band || '')));
  c.appendChild(top);
  const v = el('div', 'dec-v'); renderText(v, k.verdict || ''); c.appendChild(v);
  (k.dongles || []).forEach(d => c.appendChild(el('div', 'act-sub', d)));
  if (k.antenna_note) c.appendChild(el('div', 'act-sub', k.antenna_note));
  if (k.saved) c.appendChild(el('div', 'act-sub', 'Saved to the Signals list.'));
  const b = el('div', 'act-btns');
  const add = (label, fn) => { const x = el('button', '', label); x.onclick = fn; b.appendChild(x); };
  if (ev !== 'confirmed_decode') add('Capture until decoded', () => specCapture(k.measured_mhz || k.freq_mhz));
  add('Ask AI about it', () => sendQuick('Explain the last investigation of ' + Number(k.freq_mhz).toFixed(4) + ' MHz in plain English: what is it and is it normal?'));
  add('Locate', () => mapLocateFreq(Number(k.measured_mhz || k.freq_mhz)));
  c.appendChild(b); box.appendChild(c);
  if (k.station_name) specMarkNamed({[Number(k.station_channel).toFixed(1)]: {name: k.station_name}});
  if (typeof loadSignals === 'function') loadSignals();
}
// A station whose RDS name was read: green in the plot and the list, never "off-plan" (the server remembers it
// too, so later sweeps show it the same way).
function specMarkNamed(named){
  const j = SP.data; if (!j || !j.peaks) return;
  let changed = false;
  j.peaks.forEach(m => {
    if (m.kind !== 'fm' && m.kind !== 'fm_offplan') return;
    for (const [ch, v] of Object.entries(named)){
      if (Math.abs(Number(ch) - m.mhz) <= 0.06){
        m.name = v.name; m.flag = false; m.kind = 'fm'; m.band = v.scrolls ? 'FM station, RDS scrolling text "' + v.name + '"' : 'FM station "' + v.name + '" (RDS)'; changed = true; break;
      }
    }
  });
  if (changed){ specDraw(); specPeaks(); }
}
// What the IQ check found on channels that sent no name: on the channel (grey), RDS too weak, or off the channels
// (orange, at the carrier it measured).
function specMarkChecked(list){
  const j = SP.data; if (!j || !j.peaks) return;
  (list || []).forEach(x => {
    if (!x.state || x.state === 'not_checked') return;
    j.peaks.forEach(m => {
      if ((m.kind !== 'fm' && m.kind !== 'fm_offplan') || m.name || Math.abs(Number(x.channel_mhz) - m.mhz) > 0.06) return;
      m.checked = x.state; if (x.band) m.band = x.band; m.checked_epoch = x.epoch || (Date.now() / 1000);   // (now: Name all)
      if (x.state === 'off_channel'){ m.flag = true; m.kind = 'fm_offplan'; if (x.carrier_mhz) m.mhz = Number(x.carrier_mhz); }
      else if (x.state === 'no_name' || x.state === 'rds_weak' || x.state === 'hd_sideband'){ m.flag = false; m.kind = 'fm'; }
      // not heard / too weak (v4.9.3): remembered with the time, offered to Name all again after a day
      else if (x.state === 'not_heard' || x.state === 'too_weak'){ m.kind = 'fm'; if (!x.band) m.band = 'Checked: ' + (x.state === 'too_weak' ? 'too weak here to read' : 'not heard') + ' in the last check'; }
    });
  });
  specDraw(); specPeaks();
}
// Names read since the last sweep (a capture, a Decode, the chat, Name all on another phone) show up in the list
// without sweeping again: the page asks every 10 s while the Spectrum tab shows FM stations.
async function specKnownPoll(){
  const j = SP.data;
  if (document.hidden || typeof activeTab !== 'function' || activeTab() !== 'spectrum' || !j || !j.peaks
      || !j.peaks.some(m => m.kind === 'fm' || m.kind === 'fm_offplan')) return;
  let k; try { k = await apiGet('/api/fm_known?have=' + encodeURIComponent(SP.knownTag || '')); } catch (e){ return; }
  if (k.unchanged) return;
  SP.knownTag = k.tag;
  specMarkNamed(k.named || {}); specMarkChecked(k.checked || []);
}
setInterval(specKnownPoll, 10000);
async function specNameAll(peaks){
  if (SP.naming || !peaks.length) return; if (SP.running) specToggle();
  const freqs = peaks.map(m => m.mhz), best = {};
  peaks.forEach(m => { if (m.best) best[m.mhz.toFixed(1)] = m.best; });
  SP.naming = true; specPeaks();
  const show = (cls, txt) => { SP.namesMsg = [cls, txt]; const b = document.getElementById('sp-names'); if (b){ b.innerHTML = ''; b.appendChild(el('div', cls, txt)); } };
  let job;
  try { job = (await post('/api/fm_names', {freqs: freqs, best: best})).job; }
  catch (e){
    // already naming (started before the page was reloaded): follow that run instead of failing
    if (e.status === 409 && e.job && /naming FM/.test(e.message)) job = e.job;
    else { SP.naming = false; specPeaks(); show('warnbox', 'Could not start: ' + e.message); return; }
  }
  for (;;){
    await new Promise(r => setTimeout(r, 1200));
    let j; try { j = await apiGet('/api/decode/' + job); } catch (e){ continue; }
    if (j.state === 'running'){
      if (j.partial){            // stations turn green (or grey / orange) as each slice is decoded
        const n = Object.keys(j.partial.named || {}).length + (j.partial.not_named || []).length;
        if (n !== SP.partialN){ SP.partialN = n; specMarkNamed(j.partial.named || {}); specMarkChecked(j.partial.not_named); }
      }
      const left = Math.max(0, Math.round((j.expected || 0) - j.elapsed));
      show('infobox', (j.progress || 'Working...') + ' (' + Math.round(j.elapsed) + ' s' + (left ? ', about ' + left + ' s left' : '') + ')');
      continue;
    }
    SP.naming = false; SP.partialN = 0;
    if (j.state === 'error'){ specPeaks(); show('warnbox', 'Naming failed: ' + j.error); return; }
    specMarkNamed(j.names.named || {}); specMarkChecked(j.names.not_named); specPeaks();
    show('infobox', j.names.message);
    return;
  }
}
async function specDecodePoll(){
  if (!SP.job) return;
  let j; try { j = await apiGet('/api/decode/' + SP.job); } catch (e){ setTimeout(specDecodePoll, 2000); return; }
  if (j.state === 'running'){ decProgress(j); setTimeout(specDecodePoll, 1000); return; }
  SP.job = null; decResult(j);
}
async function specCapture(f){
  if (f === undefined || f === null) f = SP.sel; if (f === null) return; if (SP.running) specToggle();
  const box = decBox(); box.innerHTML = '';
  box.appendChild(el('div', 'infobox', 'Starting a capture on ' + Number(f).toFixed(4) + ' MHz (comparing the dongles first)...'));
  let r; try { r = await post('/api/capture/start', {freq_mhz: Number(f)}); }
  catch (e){ box.innerHTML = ''; box.appendChild(el('div', 'warnbox', 'Capture not started: ' + e.message)); return; }
  box.innerHTML = ''; box.appendChild(el('div', 'infobox', r.message || 'Capture started.'));
}
function specWatch(){ specCapture(); }
function specLocate(){ if (SP.sel === null) return; if (SP.running) specToggle(); mapLocateFreq(SP.sel); }
(function(){
  const c = document.getElementById('spec-canvas');
  c.addEventListener('mousemove', specHover); c.addEventListener('click', specClick);
  window.addEventListener('resize', () => { if (activeTab() === 'spectrum') specDraw(); });
})();
