// Map tab: a floorplan, where each antenna (node) sits on it, and a heat map of where a transmitter probably is.
// Positions are metres from the plan's top-left corner (x right, y down).
const MAP = {st: null, plan: null, img: null, imgFor: null, mode: 'locate', selNode: null, result: null, heat: 'source',
             session: null, survey: null, scalePts: [], scaleOn: false, busy: false, freq: null, panel: null, geom: null,
             calib: null, msg: ''};

function mapNodeColor(n){
  const loc = (MAP.st ? MAP.st.nodes : []).filter(x => x.kind === 'local');
  const k = loc.findIndex(x => x.id === n.id);
  if (k >= 0) return DONGLE_COLORS[k % DONGLE_COLORS.length];
  const rem = (MAP.st ? MAP.st.nodes : []).filter(x => x.kind !== 'local');
  return DONGLE_COLORS[(3 + rem.findIndex(x => x.id === n.id)) % DONGLE_COLORS.length];
}
function mapSaved(){ try { return JSON.parse(localStorage.getItem('aris_map') || '{}'); } catch (e) { return {}; } }
function mapSave(){ try { localStorage.setItem('aris_map', JSON.stringify({freq: MAP.freq, bw: mapVal('mp-bw'), dwell: mapVal('mp-dwell'), mode: MAP.mode, heat: MAP.heat})); } catch (e) {} }
function mapVal(id){ const e = document.getElementById(id); return e ? e.value : undefined; }

async function mapInit(){
  const sv = mapSaved();
  if (!MAP.st){ if (sv.mode) MAP.mode = sv.mode; if (sv.heat) MAP.heat = sv.heat; if (!MAP.freq && sv.freq) MAP.freq = sv.freq; }
  await mapLoad();
  mapMode(MAP.mode, true);
}
async function mapLoad(){
  try { MAP.st = await apiGet('/api/map/state'); } catch (e) { document.getElementById('map-help').textContent = e.message; return; }
  MAP.plan = MAP.st.plan;
  const sel = document.getElementById('map-plan'); sel.innerHTML = '';
  MAP.st.plans.forEach(p => { const o = el('option', '', p.name + ' (' + p.width_m + ' x ' + p.height_m + ' m)'); o.value = p.id; sel.appendChild(o); });
  const o = el('option', '', '+ New floorplan...'); o.value = '__new'; sel.appendChild(o);
  sel.value = MAP.st.active;
  if (MAP.plan && MAP.plan.image && MAP.imgFor !== MAP.plan.id + MAP.plan.image){
    MAP.img = null; MAP.imgFor = MAP.plan.id + MAP.plan.image;
    const im = new Image(); im.onload = () => { MAP.img = im; mapDraw(); }; im.src = '/api/map/plan/' + MAP.plan.id + '/image?f=' + encodeURIComponent(MAP.plan.image);
  } else if (!MAP.plan || !MAP.plan.image){ MAP.img = null; MAP.imgFor = null; }
  if (MAP.session && !MAP.st.sessions.some(s => s.id === MAP.session)) MAP.session = null;
  mapDraw();
}
function mapRefreshIfOpen(){ if (activeTab() === 'map') mapLoad().then(() => mapPanel()); }
async function mapPickPlan(){
  const v = document.getElementById('map-plan').value;
  if (v === '__new'){ document.getElementById('map-plan').value = MAP.st.active; mapShowPlanPanel(true); return; }
  try { await post('/api/map/plan/' + v, {activate: true}); } catch (e) { mapMsg(e.message); }
  MAP.result = null; MAP.session = null; await mapLoad(); mapPanel();
}
function mapMode(m, quiet){
  MAP.mode = m; MAP.panel = null; MAP.scaleOn = false; MAP.scalePts = [];
  document.querySelectorAll('#map-modes .chip').forEach(c => c.classList.toggle('on', c.dataset.m === m));
  if (m === 'place' && !MAP.selNode && MAP.st){ const first = MAP.st.nodes.find(n => !n.placed) || MAP.st.nodes[0]; MAP.selNode = first ? first.id : null; }
  mapPanel(); mapDraw(); if (!quiet) mapSave();
}
function mapHelp(t){ document.getElementById('map-help').textContent = t || ''; }
function mapMsg(t){ MAP.msg = t; const e = document.getElementById('mp-msg'); if (e) e.textContent = t; }

// ---- drawing ----------------------------------------------------------------------------------------------
function mapDraw(){
  const c = document.getElementById('map-canvas'); if (!c || !MAP.plan) return;
  const dpr = window.devicePixelRatio || 1, pl = MAP.plan;
  const w = Math.max(200, c.clientWidth || c.parentNode.clientWidth);
  const ratio = pl.height_m / pl.width_m;
  const h = Math.round(Math.min(w * ratio + 24, window.innerHeight * 0.62));
  c.style.height = h + 'px';
  if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)){ c.width = Math.round(w * dpr); c.height = Math.round(h * dpr); }
  const ctx = c.getContext('2d'); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = '#0b1220'; ctx.fillRect(0, 0, w, h);
  const pad = 12, sc = Math.min((w - 2 * pad) / pl.width_m, (h - 2 * pad - 12) / pl.height_m);
  const ox = (w - pl.width_m * sc) / 2, oy = pad;
  const P = (x, y) => [ox + x * sc, oy + y * sc];
  MAP.geom = {ox: ox, oy: oy, sc: sc};
  if (MAP.img) ctx.drawImage(MAP.img, ox, oy, pl.width_m * sc, pl.height_m * sc);
  else { ctx.fillStyle = '#0f172a'; ctx.fillRect(ox, oy, pl.width_m * sc, pl.height_m * sc); }
  const gstep = pl.width_m > 60 ? 10 : (pl.width_m > 25 ? 5 : 1);
  ctx.strokeStyle = MAP.img ? 'rgba(148,163,184,0.18)' : '#1e293b'; ctx.lineWidth = 1;
  for (let x = 0; x <= pl.width_m + 1e-9; x += gstep){ const a = P(x, 0), b = P(x, pl.height_m); ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke(); }
  for (let y = 0; y <= pl.height_m + 1e-9; y += gstep){ const a = P(0, y), b = P(pl.width_m, y); ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke(); }
  ctx.strokeStyle = '#334155'; ctx.strokeRect(ox, oy, pl.width_m * sc, pl.height_m * sc);
  // heat map
  const r = MAP.result, heat = r && r.heat && r.heat.ok ? r.heat : null;
  if (heat && MAP.mode !== 'place'){
    // One pixel per grid cell on a small canvas, then scaled up smoothly (no seams between cells).
    const g = heat.grid, off = document.createElement('canvas'); off.width = g.nx; off.height = g.ny;
    const octx = off.getContext('2d'), im = octx.createImageData(g.nx, g.ny);
    for (let iy = 0; iy < g.ny; iy++) for (let ix = 0; ix < g.nx; ix++){
      const v = g.values[iy][ix], k = (iy * g.nx + ix) * 4, rgb = heatColor(0.35 + 0.65 * v);
      im.data[k] = rgb[0]; im.data[k + 1] = rgb[1]; im.data[k + 2] = rgb[2];
      im.data[k + 3] = v < 0.05 ? 0 : Math.round(255 * (0.1 + 0.5 * v));
    }
    octx.putImageData(im, 0, 0);
    const q0 = P(g.x0, g.y0); ctx.imageSmoothingEnabled = true;
    ctx.drawImage(off, q0[0], q0[1], g.nx * g.cell_m * sc, g.ny * g.cell_m * sc);
    if (heat.kind === 'source'){
      (heat.alternatives || []).forEach(a => { const q = P(a.x, a.y); ctx.strokeStyle = '#fde68a'; ctx.lineWidth = 1.5; ctx.setLineDash([3, 3]);
        ctx.beginPath(); ctx.arc(q[0], q[1], 10, 0, 2 * Math.PI); ctx.stroke(); ctx.setLineDash([]);
        ctx.fillStyle = '#fde68a'; ctx.font = '10px system-ui, sans-serif'; ctx.textAlign = 'center'; ctx.fillText('or here?', q[0], q[1] + 22); });
      const b = P(heat.best.x, heat.best.y);
      ctx.strokeStyle = '#fef08a'; ctx.lineWidth = 2; ctx.setLineDash([5, 4]);
      ctx.beginPath(); ctx.arc(b[0], b[1], Math.max(8, heat.spread_m * sc), 0, 2 * Math.PI); ctx.stroke(); ctx.setLineDash([]);
      ctx.beginPath(); ctx.moveTo(b[0] - 9, b[1]); ctx.lineTo(b[0] + 9, b[1]); ctx.moveTo(b[0], b[1] - 9); ctx.lineTo(b[0], b[1] + 9); ctx.stroke();
    }
  }
  // survey points of the current session
  if (r && MAP.mode !== 'place') (r.points || []).filter(pt => pt.kind === 'survey').forEach(pt => {
    const q = P(pt.x, pt.y); ctx.fillStyle = pt.heard ? '#e2e8f0' : '#475569';
    ctx.beginPath(); ctx.moveTo(q[0], q[1] - 6); ctx.lineTo(q[0] + 6, q[1]); ctx.lineTo(q[0], q[1] + 6); ctx.lineTo(q[0] - 6, q[1]); ctx.closePath(); ctx.fill();
    if (pt.level_db !== null){ ctx.fillStyle = '#cbd5e1'; ctx.font = '10px system-ui, sans-serif'; ctx.textAlign = 'left'; ctx.fillText(pt.heard ? Number(pt.level_db).toFixed(0) : 'none', q[0] + 8, q[1] + 3); }
  });
  // nodes
  const last = {}; ((r && r.this_round) || []).forEach(x => { last[x.node_id] = x; });
  (MAP.st ? MAP.st.nodes : []).filter(n => n.placed && n.floorplan === pl.id).forEach(n => {
    const q = P(n.x, n.y), col = mapNodeColor(n), sel = MAP.mode === 'place' && MAP.selNode === n.id;
    ctx.globalAlpha = n.enabled === false ? 0.4 : 1;
    ctx.fillStyle = col; ctx.beginPath(); ctx.arc(q[0], q[1], sel ? 9 : 7, 0, 2 * Math.PI); ctx.fill();
    ctx.strokeStyle = sel ? '#fff' : '#0b1220'; ctx.lineWidth = 2; ctx.stroke();
    ctx.fillStyle = '#e2e8f0'; ctx.font = '600 11px system-ui, sans-serif'; ctx.textAlign = 'center';
    let lbl = n.name; const lr = last[n.id];
    if (lr && MAP.mode !== 'place') lbl += lr.error ? ' (error)' : (lr.heard ? ' ' + (Number(lr.snr_db) >= 0 ? '+' : '') + Number(lr.snr_db).toFixed(0) + ' dB' : ' (not heard)');
    ctx.fillText(lbl, q[0], q[1] - 12); ctx.globalAlpha = 1;
  });
  if (MAP.mode === 'survey' && MAP.survey){ const q = P(MAP.survey[0], MAP.survey[1]);
    ctx.strokeStyle = '#60a5fa'; ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(q[0], q[1], 10, 0, 2 * Math.PI); ctx.stroke();
    ctx.beginPath(); ctx.arc(q[0], q[1], 2, 0, 2 * Math.PI); ctx.stroke(); }
  if (MAP.scaleOn) MAP.scalePts.forEach((pt, i) => { const q = P(pt[0], pt[1]);
    ctx.fillStyle = '#f472b6'; ctx.beginPath(); ctx.arc(q[0], q[1], 5, 0, 2 * Math.PI); ctx.fill();
    if (i === 1){ const a = P(MAP.scalePts[0][0], MAP.scalePts[0][1]); ctx.strokeStyle = '#f472b6'; ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(q[0], q[1]); ctx.stroke(); } });
  // scale bar
  const bar = niceStep(pl.width_m / 5), bl = bar * sc;
  ctx.fillStyle = '#94a3b8'; ctx.fillRect(ox, h - 12, bl, 3); ctx.font = '10px system-ui, sans-serif'; ctx.textAlign = 'left';
  ctx.fillText(bar + ' m', ox + bl + 6, h - 8);
}
function mapAt(ev){
  const c = document.getElementById('map-canvas'), r = c.getBoundingClientRect(), g = MAP.geom; if (!g) return null;
  const x = (ev.clientX - r.left - g.ox) / g.sc, y = (ev.clientY - r.top - g.oy) / g.sc;
  return [Math.round(x * 100) / 100, Math.round(y * 100) / 100];
}
async function mapClick(ev){
  const at = mapAt(ev); if (!at || !MAP.plan) return;
  const inside = at[0] >= -0.5 && at[1] >= -0.5 && at[0] <= MAP.plan.width_m + 0.5 && at[1] <= MAP.plan.height_m + 0.5;
  if (!inside) return;
  if (MAP.scaleOn){ if (MAP.scalePts.length >= 2) MAP.scalePts = []; MAP.scalePts.push(at); mapDraw(); mapShowPlanPanel(); return; }
  if (MAP.mode === 'place'){
    if (!MAP.selNode){ mapHelp('Pick an antenna in the list first.'); return; }
    try { await post('/api/map/node/' + MAP.selNode, {x: at[0], y: at[1], floorplan: MAP.plan.id}); } catch (e) { mapMsg(e.message); return; }
    const nxt = MAP.st.nodes.find(n => !n.placed && n.id !== MAP.selNode);
    await mapLoad(); if (nxt) MAP.selNode = nxt.id; mapPanel(); mapDraw();
  } else if (MAP.mode === 'survey'){ MAP.survey = at; mapDraw(); mapPanel(); }
}

// ---- panels -----------------------------------------------------------------------------------------------
function field(label, inp){ const f = el('label', 'field', label); f.appendChild(inp); return f; }
function numInput(id, val, step, w){ const i = el('input'); i.type = 'number'; i.id = id; i.step = step || 'any'; i.inputMode = 'decimal'; if (val !== undefined && val !== null) i.value = val; if (w) i.style.width = w; return i; }
function selInput(id, opts, val){ const s = el('select'); s.id = id; opts.forEach(([v, t]) => { const o = el('option', '', t); o.value = v; s.appendChild(o); }); if (val !== undefined) s.value = val; return s; }
function btn(cls, text, fn){ const b = el('button', cls, text); b.onclick = fn; return b; }
function measureFields(sv){
  const row = el('div', 'row');
  row.appendChild(field('Frequency MHz', numInput('mp-freq', MAP.freq || sv.freq || '', '0.0001')));
  row.appendChild(field('Width', selInput('mp-bw', [['', 'Auto'], ['12.5', '12.5 kHz'], ['25', '25 kHz'], ['50', '50 kHz'], ['125', '125 kHz'], ['200', '200 kHz']], sv.bw || '')));
  row.appendChild(field('Listen', selInput('mp-dwell', [['3', '3 s'], ['5', '5 s'], ['10', '10 s'], ['20', '20 s'], ['30', '30 s']], sv.dwell || '5')));
  return row;
}
function measureArgs(){
  const f = parseFloat(mapVal('mp-freq')); MAP.freq = isFinite(f) ? f : null; mapSave();
  const a = {freq_mhz: MAP.freq, dwell_s: parseFloat(mapVal('mp-dwell') || '5')};
  if (mapVal('mp-bw')) a.bw_khz = parseFloat(mapVal('mp-bw'));
  return a;
}
function mapPanel(){
  const box = document.getElementById('map-panel'); box.innerHTML = '';
  if (!MAP.st || !MAP.plan) return;
  if (MAP.panel === 'plan'){ mapShowPlanPanel(); return; }
  const sv = mapSaved();
  const placed = MAP.st.nodes.filter(n => n.placed && n.floorplan === MAP.plan.id && n.enabled !== false);
  if (MAP.mode === 'place'){
    mapHelp('Pick an antenna, then tap where it sits on the plan. Long USB extensions or coax let each dongle\'s antenna sit in a different spot; remote nodes can be anywhere on the network.');
    const list = el('div', 'nodes');
    MAP.st.nodes.forEach(n => {
      const row = el('div', 'node' + (MAP.selNode === n.id ? ' sel' : ''));
      const dot = el('span', 'dot'); dot.style.background = mapNodeColor(n); row.appendChild(dot);
      const g = el('div', 'grow'); g.appendChild(el('div', '', n.name + (n.kind === 'remote' ? ' (remote)' : '')));
      const cal = Object.keys(n.calibrated || {});
      g.appendChild(el('div', 'sub', (n.placed && n.floorplan === MAP.plan.id ? 'at ' + n.x.toFixed(1) + ', ' + n.y.toFixed(1) + ' m' : (n.placed ? 'placed on another plan' : 'not placed')) +
        (n.antenna ? ' | ' + n.antenna : '') + (cal.length ? ' | calibrated: ' + cal.join(', ') : '') + (n.enabled === false ? ' | off' : '')));
      row.appendChild(g);
      row.onclick = () => { MAP.selNode = n.id; mapPanel(); mapDraw(); };
      list.appendChild(row);
    });
    box.appendChild(list);
    const n = MAP.st.nodes.find(x => x.id === MAP.selNode);
    if (n){
      const pnl = el('div', 'panel'); pnl.appendChild(el('h4', '', n.name));
      const row = el('div', 'row');
      const nm = el('input'); nm.id = 'mp-nname'; nm.value = n.name; nm.className = 'wide'; row.appendChild(field('Name', nm));
      const an = el('input'); an.id = 'mp-nant'; an.value = n.antenna || ''; an.placeholder = 'e.g. 433 whip, by the window'; an.className = 'wide'; row.appendChild(field('Antenna note', an));
      pnl.appendChild(row);
      const b = el('div', 'row');
      b.appendChild(btn('btn-ghost', 'Save', async () => { try { await post('/api/map/node/' + n.id, {name: nm.value, antenna: an.value}); await mapLoad(); mapPanel(); } catch (e) { mapMsg(e.message); } }));
      if (n.placed) b.appendChild(btn('btn-ghost', 'Remove from plan', async () => { try { await post('/api/map/node/' + n.id, {unplace: true}); await mapLoad(); mapPanel(); } catch (e) { mapMsg(e.message); } }));
      b.appendChild(btn('btn-ghost', n.enabled === false ? 'Switch on' : 'Switch off', async () => { try { await post('/api/map/node/' + n.id, {enabled: n.enabled === false}); await mapLoad(); mapPanel(); } catch (e) { mapMsg(e.message); } }));
      if (n.kind === 'remote'){
        b.appendChild(btn('btn-ghost', 'Test', async () => { try { const h = await apiGet('/api/map/node/' + n.id + '/health'); mapMsg(h.ok ? 'Node OK: ' + (h.node || '') + ' ' + (h.dongles ? h.dongles.join(', ') : '') : 'Problem: ' + h.error); } catch (e) { mapMsg(e.message); } }));
        b.appendChild(btn('btn-ghost danger', 'Delete', async () => { try { await del('/api/map/node/' + n.id); MAP.selNode = null; await mapLoad(); mapPanel(); } catch (e) { mapMsg(e.message); } }));
      }
      pnl.appendChild(b); box.appendChild(pnl);
    }
    const rp = el('details', 'panel'); rp.appendChild(el('summary', '', 'Add a remote node (another computer running aris_node.py)'));
    const rr = el('div', 'row'); rr.style.marginTop = '8px';
    const rn = el('input'); rn.id = 'mp-rname'; rn.placeholder = 'Kitchen Pi'; rr.appendChild(field('Name', rn));
    const ru = el('input'); ru.id = 'mp-rurl'; ru.placeholder = 'http://192.168.1.50:5055'; ru.className = 'wide'; rr.appendChild(field('Address', ru));
    const rt = el('input'); rt.id = 'mp-rtok'; rt.type = 'password'; rt.placeholder = 'token from aris_node.py'; rt.className = 'wide'; rr.appendChild(field('Token', rt));
    rp.appendChild(rr);
    rp.appendChild(btn('btn-ghost', 'Add node', async () => { try { const nn = await post('/api/map/remote', {name: rn.value, url: ru.value, token: rt.value}); MAP.selNode = nn.id; await mapLoad(); mapPanel(); } catch (e) { mapMsg(e.message); } }));
    rp.appendChild(el('div', 'note', 'On the other computer: copy this project there and run  python3 aris_node.py --init  then  python3 aris_node.py . It prints the address and token to type here. It only measures signal strength when ARIS asks.'));
    box.appendChild(rp);
  } else if (MAP.mode === 'locate'){
    mapHelp(placed.length >= 3 ? 'Enter a frequency and press Measure. Every placed antenna listens at the same time.'
      : (placed.length ? 'Only ' + placed.length + ' antenna(s) placed: you will get a rough direction. Place 3 or more for a position (Place antennas).'
                       : 'Place your antennas on the plan first (Place antennas).'));
    box.appendChild(measureFields(sv));
    const row = el('div', 'row');
    const mb = btn('btn', MAP.busy ? 'Measuring...' : 'Measure', () => mapMeasure(false)); mb.disabled = MAP.busy || !placed.length; row.appendChild(mb);
    row.appendChild(btn('btn-ghost', 'New session', () => { MAP.session = null; MAP.result = null; MAP.forceNew = true; mapPanel(); mapDraw(); mapMsg('Next Measure starts a new session.'); }));
    const seg = el('div', 'seg');
    [['source', 'Where it could be'], ['strength', 'Signal strength']].forEach(([v, t]) => { const b = btn(MAP.heat === v ? 'on' : '', t, () => { MAP.heat = v; mapSave(); mapViewSession(); }); seg.appendChild(b); });
    row.appendChild(seg); box.appendChild(row);
    mapResultBox(box);
    mapSessionsBox(box);
  } else if (MAP.mode === 'survey'){
    mapHelp('Carry one antenna (a laptop dongle, or a long cable) around the space. At each spot: tap where you are, then Measure here. The hot area on the strength map is where it is strongest.');
    box.appendChild(measureFields(sv));
    const row = el('div', 'row');
    row.appendChild(field('Antenna you carry', selInput('mp-snode', MAP.st.nodes.filter(n => n.enabled !== false).map(n => [n.id, n.name]), sv.snode || (MAP.st.nodes[0] || {}).id)));
    const mb = btn('btn', MAP.busy ? 'Measuring...' : 'Measure here', () => mapMeasure(true)); mb.disabled = MAP.busy || !MAP.survey; row.appendChild(mb);
    box.appendChild(row);
    if (!MAP.survey) box.appendChild(el('div', 'muted', 'Tap the plan where the antenna is now.'));
    else box.appendChild(el('div', 'muted', 'Spot: ' + MAP.survey[0].toFixed(1) + ', ' + MAP.survey[1].toFixed(1) + ' m'));
    if (MAP.session) box.appendChild(btn('btn-ghost', 'Remove survey points', async () => { try { MAP.result = await post('/api/map/session/' + MAP.session + '/clear', {kind: 'survey'}); mapPanel(); mapDraw(); } catch (e) { mapMsg(e.message); } }));
    mapResultBox(box);
  } else if (MAP.mode === 'calibrate'){
    mapHelp('Antennas and cables differ, so the same signal reads differently on each. Calibrating once per band fixes that.');
    box.appendChild(el('div', 'infobox', 'Put all antennas side by side (same spot, same direction), 1-3 m from a transmitter you control: press a 433 MHz remote or doorbell over and over during the whole measurement, or for the FM band use a strong radio station. Enter its frequency and press Calibrate. Then put the antennas back and place them on the plan.'));
    box.appendChild(measureFields(Object.assign({}, sv, {dwell: '10'})));
    const row = el('div', 'row');
    const cb = btn('btn', MAP.busy ? 'Measuring...' : 'Calibrate', mapCalibrate); cb.disabled = MAP.busy; row.appendChild(cb);
    row.appendChild(btn('btn-ghost danger', 'Clear this band', async () => { const a = measureArgs(); if (!a.freq_mhz){ mapMsg('Enter the frequency of the band to clear.'); return; }
      try { await post('/api/map/calibrate', {clear: true, freq_mhz: a.freq_mhz}); MAP.calib = null; await mapLoad(); mapPanel(); mapMsg('Calibration cleared for that band.'); } catch (e) { mapMsg(e.message); } }));
    box.appendChild(row);
    if (MAP.calib){
      const pnl = el('div', 'panel'); pnl.appendChild(el('h4', '', 'Calibrated band ' + MAP.calib.band + ' at ' + MAP.calib.freq_mhz + ' MHz'));
      Object.entries(MAP.calib.offsets_db).forEach(([n, v]) => pnl.appendChild(el('div', 'muted', n + ': ' + (v >= 0 ? '+' : '') + v + ' dB correction')));
      if (MAP.calib.not_calibrated.length) pnl.appendChild(el('div', 'muted', 'Did not hear it: ' + MAP.calib.not_calibrated.join(', ')));
      MAP.calib.warnings.forEach(w => pnl.appendChild(el('div', 'warnbox', w)));
      box.appendChild(pnl);
    }
    const tbl = el('table', 'readings'); const hr = el('tr'); ['Antenna', 'Calibrated bands'].forEach(t => hr.appendChild(el('th', '', t))); tbl.appendChild(hr);
    MAP.st.nodes.forEach(n => { const tr = el('tr'); tr.appendChild(el('td', '', n.name));
      tr.appendChild(el('td', '', Object.entries(n.offsets_db || {}).map(([b, v]) => b + ' ' + (v >= 0 ? '+' : '') + v + ' dB').join(', ') || 'none')); tbl.appendChild(tr); });
    box.appendChild(tbl);
  }
  const msg = el('div', 'muted'); msg.id = 'mp-msg'; msg.style.marginTop = '8px'; msg.textContent = MAP.msg || ''; box.appendChild(msg);
}
function mapResultBox(box){
  const r = MAP.result; if (!r) return;
  const pnl = el('div', 'panel');
  pnl.appendChild(el('h4', '', r.session.freq_mhz + ' MHz, session ' + r.session.id + ' (' + r.session.time + ')'));
  pnl.appendChild(el('div', '', r.summary || ''));
  (r.notes || []).forEach(n => pnl.appendChild(el('div', 'note', n)));
  if (r.this_round && r.this_round.length){
    const tbl = el('table', 'readings'); const hr = el('tr'); ['Antenna', 'Heard', 'SNR', 'Level'].forEach(t => hr.appendChild(el('th', '', t))); tbl.appendChild(hr);
    r.this_round.forEach(x => { const tr = el('tr'); tr.appendChild(el('td', '', x.node));
      if (x.error){ const td = el('td', '', x.error); td.colSpan = 3; tr.appendChild(td); }
      else { tr.appendChild(el('td', '', x.heard ? (x.active_fraction > 0.9 ? 'yes, continuous' : 'yes, bursts') : 'no'));
        tr.appendChild(el('td', '', (Number(x.snr_db) >= 0 ? '+' : '') + Number(x.snr_db).toFixed(1) + ' dB')); tr.appendChild(el('td', '', Number(x.level_db).toFixed(1) + (x.overloaded ? ' (overload)' : ''))); }
      tbl.appendChild(tr); });
    pnl.appendChild(tbl);
    pnl.appendChild(el('div', 'note', 'Level is in dB relative to each receiver after its gain is taken off; only differences between antennas matter.'));
  }
  pnl.appendChild(el('div', 'note', (r.points || []).length + ' reading spot(s) in this session. Measuring again adds to it: devices that transmit in bursts give steadier results after a few rounds.'));
  const b = el('div', 'row'); b.style.marginTop = '8px';
  b.appendChild(btn('btn-ghost', 'Investigate this frequency', () => sendQuick('Investigate ' + r.session.freq_mhz + ' MHz and tell me in plain English what it is.')));
  b.appendChild(btn('btn-ghost danger', 'Delete session', async () => { try { await del('/api/map/session/' + r.session.id); MAP.result = null; MAP.session = null; await mapLoad(); mapPanel(); } catch (e) { mapMsg(e.message); } }));
  pnl.appendChild(b); box.appendChild(pnl);
}
function mapSessionsBox(box){
  const ss = (MAP.st.sessions || []).filter(s => s.floorplan === MAP.plan.id); if (!ss.length) return;
  const d = el('details', 'panel'); d.appendChild(el('summary', '', 'Earlier sessions (' + ss.length + ')'));
  ss.forEach(s => { const r = el('div', 'node'); r.appendChild(el('div', 'grow', s.freq_mhz + ' MHz | ' + s.time + ' | ' + s.readings + ' readings'));
    r.onclick = () => { MAP.session = s.id; MAP.freq = s.freq_mhz; mapViewSession(); }; d.appendChild(r); });
  box.appendChild(d);
}
async function mapViewSession(){
  if (!MAP.session){ mapPanel(); mapDraw(); return; }
  try { const r = await apiGet('/api/map/session/' + MAP.session + '?mode=' + MAP.heat); r.this_round = MAP.result && MAP.result.session.id === MAP.session ? MAP.result.this_round : null; MAP.result = r; }
  catch (e) { mapMsg(e.message); }
  mapPanel(); mapDraw();
}
async function mapMeasure(survey){
  const a = measureArgs(); if (!a.freq_mhz){ mapMsg('Enter a frequency.'); return; }
  if (MAP.result && MAP.result.session.freq_mhz !== a.freq_mhz) MAP.session = null;
  const body = Object.assign(a, {mode: MAP.heat});
  if (MAP.session) body.session_id = MAP.session;                   // otherwise the server continues a recent
  if (MAP.forceNew){ body.new_session = true; MAP.forceNew = false; } // session for this frequency (e.g. from chat)
  if (survey){ body.survey = true; body.node_id = mapVal('mp-snode'); body.x = MAP.survey[0]; body.y = MAP.survey[1];
    try { const sv = mapSaved(); sv.snode = body.node_id; localStorage.setItem('aris_map', JSON.stringify(sv)); } catch (e) {} }
  MAP.busy = true; MAP.msg = 'Listening for ' + a.dwell_s + ' s...'; mapPanel();
  try { MAP.result = await post('/api/map/measure', body); MAP.session = MAP.result.session.id; MAP.msg = ''; }
  catch (e) { MAP.msg = e.message; }
  MAP.busy = false; await mapLoad(); mapPanel(); mapDraw();
}
async function mapCalibrate(){
  const a = measureArgs(); if (!a.freq_mhz){ mapMsg('Enter the transmitter frequency.'); return; }
  MAP.busy = true; MAP.msg = 'Listening for ' + a.dwell_s + ' s. Keep the transmitter going...'; mapPanel();
  try { MAP.calib = await post('/api/map/calibrate', a); MAP.msg = 'Saved.'; } catch (e) { MAP.msg = e.message; }
  MAP.busy = false; await mapLoad(); mapPanel();
}
function mapLocateFreq(f){
  MAP.freq = Math.round(Number(f) * 10000) / 10000; MAP.session = null; MAP.result = null; MAP.mode = 'locate'; mapSave();
  setTimeout(() => { const s = (MAP.st && MAP.st.sessions || []).find(x => Math.abs(x.freq_mhz - MAP.freq) < 0.01 && MAP.plan && x.floorplan === MAP.plan.id);
    if (s){ MAP.session = s.id; mapViewSession(); } }, 600);          // show an earlier hunt for it, if any
  switchTab('map');
}

// ---- plan settings ----------------------------------------------------------------------------------------
function mapShowPlanPanel(isNew){
  MAP.panel = 'plan';
  const box = document.getElementById('map-panel'); box.innerHTML = '';
  const pl = MAP.plan; const pnl = el('div', 'panel');
  if (!isNew && pl){
    pnl.appendChild(el('h4', '', 'This plan'));
    const r = el('div', 'row');
    const nm = el('input'); nm.value = pl.name; nm.className = 'wide'; r.appendChild(field('Name', nm));
    const wd = numInput('mp-pw', pl.width_m, '0.01'); r.appendChild(field('Width m', wd));
    const ht = numInput('mp-ph', pl.height_m, '0.01'); ht.disabled = !!pl.image; r.appendChild(field(pl.image ? 'Height m (from picture)' : 'Height m', ht));
    pnl.appendChild(r);
    const b = el('div', 'row');
    b.appendChild(btn('btn', 'Save', async () => { try { await post('/api/map/plan/' + pl.id, Object.assign({name: nm.value, width_m: wd.value}, pl.image ? {} : {height_m: ht.value})); await mapLoad(); mapShowPlanPanel(); mapMsg('Saved.'); } catch (e) { mapMsg(e.message); } }));
    b.appendChild(btn('btn-ghost' + (MAP.scaleOn ? ' on' : ''), MAP.scaleOn ? 'Cancel scale' : 'Set scale from 2 points', () => { MAP.scaleOn = !MAP.scaleOn; MAP.scalePts = []; mapDraw(); mapShowPlanPanel(); }));
    pnl.appendChild(b);
    if (MAP.scaleOn){
      if (MAP.scalePts.length < 2) pnl.appendChild(el('div', 'infobox', 'Tap two points on the plan whose real distance you know (for example both ends of a wall). Tapped: ' + MAP.scalePts.length + ' of 2.'));
      else { const sr = el('div', 'row'); const dist = numInput('mp-dist', '', '0.01'); sr.appendChild(field('Real distance m', dist));
        sr.appendChild(btn('btn', 'Apply scale', async () => { const [a, c] = MAP.scalePts;
          try { await post('/api/map/plan/' + pl.id + '/scale', {ax: a[0], ay: a[1], bx: c[0], by: c[1], distance_m: dist.value}); MAP.scaleOn = false; MAP.scalePts = []; await mapLoad(); mapShowPlanPanel(); mapMsg('Scale set. Antenna positions kept their spot on the picture.'); } catch (e) { mapMsg(e.message); } }));
        pnl.appendChild(sr); }
    }
    pnl.appendChild(btn('btn-ghost danger', 'Delete this plan', async () => { try { await del('/api/map/plan/' + pl.id); await mapLoad(); mapShowPlanPanel(); } catch (e) { mapMsg(e.message); } }));
    box.appendChild(pnl);
  }
  const np = el('div', 'panel'); np.appendChild(el('h4', '', 'New floorplan'));
  const r2 = el('div', 'row');
  const nn = el('input'); nn.placeholder = 'Ground floor'; nn.className = 'wide'; r2.appendChild(field('Name', nn));
  const nw = numInput('mp-nw', 10, '0.01'); r2.appendChild(field('Real width m', nw));
  np.appendChild(r2);
  const fileIn = el('input'); fileIn.type = 'file'; fileIn.accept = 'image/png,image/jpeg,image/webp';
  np.appendChild(field('Picture (PNG, JPEG or WebP; a photo of a drawing works)', fileIn));
  const b2 = el('div', 'row'); b2.style.marginTop = '8px';
  b2.appendChild(btn('btn', 'Upload plan', async () => {
    if (!fileIn.files.length){ mapMsg('Choose a picture first, or use Blank plan.'); return; }
    const fd = new FormData(); fd.append('image', fileIn.files[0]); fd.append('name', nn.value || fileIn.files[0].name); fd.append('width_m', nw.value || '10');
    try { await post('/api/map/plan', fd); MAP.result = null; MAP.session = null; await mapLoad(); MAP.panel = null; mapMode('place'); mapMsg('Plan added. Now place your antennas.'); } catch (e) { mapMsg(e.message); } }));
  b2.appendChild(btn('btn-ghost', 'Blank plan', async () => { try { await post('/api/map/plan', {name: nn.value || 'Blank plan', width_m: nw.value || 12}); await mapLoad(); MAP.panel = null; mapMode('place'); } catch (e) { mapMsg(e.message); } }));
  b2.appendChild(btn('btn-ghost', 'Close', () => { MAP.panel = null; MAP.scaleOn = false; mapPanel(); mapDraw(); }));
  np.appendChild(b2);
  np.appendChild(el('div', 'note', 'Width is the real distance across the whole picture. If you do not know it, upload with a guess and then use "Set scale from 2 points" on a wall you can measure.'));
  box.appendChild(np);
  const msg = el('div', 'muted'); msg.id = 'mp-msg'; msg.style.marginTop = '8px'; msg.textContent = MAP.msg || ''; box.appendChild(msg);
}
(function(){
  document.getElementById('map-canvas').addEventListener('click', mapClick);
  window.addEventListener('resize', () => { if (activeTab() === 'map') mapDraw(); });
})();
