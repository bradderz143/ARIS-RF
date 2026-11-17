// Spectrum tab: a spectrum analyzer over any range from 0 to 6000 MHz, on one receiver or on all of them at the same
// time ("compare"). The server's plan says which parts of the view the receivers here tune and the pieces to sweep in
// turn; a part nothing tunes is hatched and never drawn as a level. Because each receiver has its own antenna and
// gain, "Above floor" plots every line relative to its own receiver's noise floor.
const SP = {running: false, busy: false, timer: null, data: null, holdOn: false, hold: {}, holdSince: null, holdN: 0,
            holdMax: -1e9, key: '', sel: null, yMin: null, yMax: null, meta: null, retry: false, geom: null,
            scale: 'abs', preset: null, fmOpen: false, helpOpen: false, labelBoxes: [],
            view: {s: 88, e: 108}, hist: [], histAt: 0,          // the view in MHz, and the views before it (Back)
            gain: 'auto', lvl: 'input',                          // receiver gain (auto = the best for each dongle); levels with the gain taken off
            rbw: 'auto', top: null, range: 'auto',               // step between points; level axis (null / auto = automatic)
            avgN: 0, avg: {},                                    // average over N sweeps, kept as power per line
            ref: null,                                           // the Delta reference frequency
            plan: null, plans: {}, planT: null,                  // the server's plan for the view, and the ones asked before
            run: 0, loopId: 0, prog: null,                       // sweep in progress: its number, and where it is
            autoZoom: true, zoomT: null,                         // a zoomed view is swept by itself (the setting), and its timer
            pinch: null,                                         // two fingers on the plot: where the gesture started
            region: null, regionRO: false,                       // the band labels' region (GET /api/region), and whether it is read-only here
            lines: null, drag: null, dragEnd: 0, raf: 0, stale: false};
const SP_L = 44, SP_R = 10, SP_T = 22, SP_B = 22;
const SP_EDGE_LABEL_PX = 10;                    // gain lines closer than this on the plot get no label (the lines alone)
const SP_GAP_TEXT = 'no receiver here tunes this';
const SP_HINT = 'Tap the spectrum or a signal below to select a frequency.';

function spColor(serial){
  const ds = SP.meta ? SP.meta.dongles : []; const k = ds.findIndex(d => d.serial === serial);
  return DONGLE_COLORS[k < 0 ? 3 : k % DONGLE_COLORS.length];
}
function spSave(){ try { localStorage.setItem('aris_spec', JSON.stringify({p: SP.preset, d: document.getElementById('sp-dongle').value,
  s: String(SP.view.s), e: String(SP.view.e), sc: SP.scale, rbw: SP.rbw, top: SP.top, rng: SP.range, avg: SP.avgN, gain: SP.gain, lvl: SP.lvl,
  help: SP.helpOpen, az: SP.autoZoom})); } catch (e) {} }
function spLoad(){ try { return JSON.parse(localStorage.getItem('aris_spec') || 'null'); } catch (e) { return null; } }

// ---- the view: start, stop, centre and span, always inside 0-6000 MHz ----
function spLim(){
  const m = SP.meta || {};
  return {lo: m.view_min_mhz === undefined ? 0 : m.view_min_mhz, hi: m.view_max_mhz || 6000, min: m.min_span_mhz || 0.01};
}
function spR4(x){ return Math.round(x * 10000) / 10000; }
function spMHz(x){ return String(Math.round(x * 1000) / 1000); }
function spRangeText(a, b){ return spMHz(a) + '-' + spMHz(b) + ' MHz'; }
function spFitView(s, e){                       // keeps the span where it can and moves the view inside the limits
  const L = spLim(), span = Math.min(Math.max(e - s, L.min), L.hi - L.lo);
  let a = (s + e) / 2 - span / 2;
  a = Math.min(Math.max(a, L.lo), L.hi - span);
  return [spR4(a), spR4(a + span)];
}
function spFields(){
  // a field that already shows the value is left alone (rewriting one that is being typed into loses the caret)
  const v = SP.view, set = (id, x) => { const f = document.getElementById(id); if (parseFloat(f.value) !== spR4(x)) f.value = spR4(x); };
  set('sp-start', v.s); set('sp-stop', v.e); set('sp-centre', (v.s + v.e) / 2); set('sp-span', v.e - v.s);
  // a chip is on only while the view is its range: after a region change a preset of the same name can cover
  // another range (FM 88-108 here, 76-99 there), and a pinch moves the view before the gesture ends
  if (SP.preset && SP.meta && !spOnPreset(SP.meta.presets.find(x => x.id === SP.preset), v)) SP.preset = null;
  if (!SP.preset && SP.meta){                   // a view typed or zoomed onto a preset's range is that preset
    const m = SP.meta.presets.find(x => spOnPreset(x, v));
    if (m) SP.preset = m.id;
  }
  document.querySelectorAll('#sp-presets .chip').forEach(c => c.classList.toggle('on', c.dataset.id === SP.preset));
  document.getElementById('sp-back').disabled = !SP.hist.length;
}
function spOnPreset(p, v){                      // is the view this preset's range (as the view would hold it)?
  if (!p || !isFinite(p.start) || !isFinite(p.stop)) return false;
  const f = spFitView(p.start, p.stop);
  return Math.abs(f[0] - v.s) < 1e-6 && Math.abs(f[1] - v.e) < 1e-6;
}
function specSetView(s, e, opt){
  opt = opt || {};
  const v = spFitView(s, e), old = SP.view;
  const same = Math.abs(old.s - v[0]) < 1e-7 && Math.abs(old.e - v[1]) < 1e-7;
  if (!same && !opt.noHist){                    // one entry per burst of changes (a turn of the wheel is many steps)
    const now = Date.now();
    if (now - SP.histAt > 700){ SP.hist.push([old.s, old.e]); if (SP.hist.length > 40) SP.hist.shift(); }
    SP.histAt = now;
  }
  SP.view = {s: v[0], e: v[1]}; SP.preset = opt.preset || null;
  spFields();
  if (same && !opt.force){ spSave(); return; }
  specViewChanged();
}
// A new view: the sweep in progress ends here, peak hold and the average start again. What was measured before stays
// on screen where it is still in view, until that part is swept again.
function specViewChanged(){
  specCancel(); spResetTraces();
  const d = SP.data, v = SP.view;
  if (d && !(d.stop_mhz > v.s && d.start_mhz < v.e)) spForget();
  SP.stale = !!SP.data;
  // the marker's panel goes when the marker leaves the view, but not while a Decode starts or runs: its progress card
  // and its Stop are in that panel (the next view change after it ends applies the rule again)
  if (SP.sel !== null && (SP.sel < v.s || SP.sel > v.e) && !SP.job && !SP.decBusy){ SP.sel = null; document.getElementById('sp-sel').style.display = 'none'; }
  document.getElementById('sp-readout').textContent = SP.sel !== null ? readoutAt(SP.sel) : SP_HINT;
  if (SP.ref !== null && (SP.ref < v.s || SP.ref > v.e)) spDeltaOff();
  spSave(); spPlanSoon(); specDraw();
  if (SP.running) spRestartLoop(250); else specStatus(SP.stale ? 'View changed: sweep to measure it' : 'Stopped');
  spZoomSoon();
}
// A zoomed view swept by itself: with the plot stopped, once the view has settled (no change for SP_ZOOM_WAIT_MS),
// a view narrower than what is on screen is swept once when its plan has a finer step than what was measured and
// the sweep is quick (a live capture, or at most SP_QUICK_MHZ tuned). Never while a Decode, Name all or a capture
// start is under way; another view change cancels it (the sweep's run counter); zooming out never starts one.
const SP_ZOOM_WAIT_MS = 800, SP_QUICK_MHZ = 60;
function spZoomSoon(){
  clearTimeout(SP.zoomT); SP.zoomT = null;
  const d = SP.data, v = SP.view;
  if (!SP.autoZoom || SP.running || !d || d.mode === 'none' || !(d.step_khz > 0)) return;
  if (!(v.e - v.s < d.stop_mhz - d.start_mhz - 1e-9)) return;
  SP.zoomT = setTimeout(spZoomSweep, SP_ZOOM_WAIT_MS);
}
function spIdle(){ return !SP.running && !SP.busy && !SP.job && !SP.decBusy && !SP.naming && !SP.capBusy; }
function spQuick(pl){ return pl.mode === 'live' || (pl.mode === 'sweep' && pl.covered_mhz <= SP_QUICK_MHZ); }
function spStepOf(pl){ return pl.mode === 'live' ? pl.span_mhz * 1000 / Math.max(1, pl.points) : pl.step_khz; }
function spKhz(x){ return String(Math.round(x * 1000) / 1000); }
function spCoveredText(pl){ const c = pl.covered || []; return c.length ? spRangeText(c[0][0], c[c.length - 1][1]) : spRangeText(pl.start_mhz, pl.stop_mhz); }
async function spZoomSweep(){
  SP.zoomT = null;
  const d = SP.data, p = specParams();
  if (!SP.autoZoom || !spIdle() || !d || !(p.e > p.s)) return;
  let pl; try { pl = await spPlan(p); } catch (e){ return; }
  if (specParams().key !== p.key || !spIdle() || !SP.autoZoom) return;      // the view moved on, or other work began
  const st = spStepOf(pl);
  if (!spQuick(pl) || !(st > 0) || !(d.step_khz > st * 1.05)) return;       // slow, or nothing finer to see
  specOnce({auto: true, label: pl.mode === 'live' ? 'Zoomed in: capturing ' + spRangeText(p.s, p.e) + ' live'
                                                  : 'Zoomed in: sweeping ' + spCoveredText(pl) + ' at ' + spKhz(st) + ' kHz steps'});
}
function specAutoZoom(){
  SP.autoZoom = !!document.getElementById('sp-autozoom').checked;
  if (!SP.autoZoom){ clearTimeout(SP.zoomT); SP.zoomT = null; }
  spSave();
}
function specPreset(id){
  const p = SP.meta && SP.meta.presets.find(x => x.id === id); if (!p) return;
  specSetView(p.start, p.stop, {preset: id, force: true});
}
function specCustom(which){                     // Start ('s') or Stop ('e') typed
  let s = parseFloat(document.getElementById('sp-start').value), e = parseFloat(document.getElementById('sp-stop').value);
  if (!isFinite(s) || !isFinite(e)){ specStatus('Enter a start and stop frequency'); return; }
  const L = spLim(), span = SP.view.e - SP.view.s;
  s = Math.min(Math.max(s, L.lo), L.hi); e = Math.min(Math.max(e, L.lo), L.hi);
  // a start typed above the stop (or a stop below the start) takes the span along, so the two can be typed in turn
  if (e <= s){ if (which === 's') e = s + span; else if (which === 'e') s = e - span; else { const t = s; s = e; e = t; } }
  if (which === 's' && e > L.hi) e = L.hi; if (which === 'e' && s < L.lo) s = L.lo;
  if (e - s < L.min){ if (which === 's') s = e - L.min; else e = s + L.min; }
  specSetView(s, e);
}
function specCentreSpan(which){                 // Centre ('c') or Span ('w') typed
  const c = parseFloat(document.getElementById('sp-centre').value), w = parseFloat(document.getElementById('sp-span').value);
  if (!isFinite(c) || !isFinite(w) || w <= 0){ specStatus('Enter a centre frequency and a span'); spFields(); return; }
  if (which !== 'c'){ specSetView(c - w / 2, c + w / 2); return; }      // a typed span keeps its width: the view moves inside the limits
  // a typed centre is kept: where the span does not fit round it, the span is made narrower
  const L = spLim(), cc = Math.min(Math.max(c, L.lo), L.hi), half = Math.max(L.min / 2, Math.min(w / 2, cc - L.lo, L.hi - cc));
  specSetView(cc - half, cc + half);
}
function specZoom(k){ const v = SP.view, c = (v.s + v.e) / 2, h = (v.e - v.s) * k / 2; specSetView(c - h, c + h); }
function specPan(dir){ const v = SP.view, d = (v.e - v.s) / 2 * dir; specSetView(v.s + d, v.e + d); }
function specFull(){ const L = spLim(); specSetView(L.lo, L.hi, {preset: 'full'}); }
function specBack(){ const h = SP.hist.pop(); if (h) specSetView(h[0], h[1], {noHist: true}); else spFields(); }
function specDongle(){ SP.plan = null; specClear(); spPlanSoon(); if (SP.running) spRestartLoop(100); }
function specGainLvl(){                         // another gain or level reading: what is on screen is not comparable
  SP.gain = document.getElementById('sp-gain').value || 'auto';
  SP.lvl = document.getElementById('sp-lvl').value === 'raw' ? 'raw' : 'input';
  specCancel(); spResetTraces(); spForget(); spSave(); specDraw();
  if (SP.running) spRestartLoop(100); else specStatus('Gain or levels changed: sweep to measure again');
}
function specRbw(){                             // a new step is a new sweep: hold and average start again
  SP.rbw = document.getElementById('sp-rbw').value || 'auto';
  specCancel(); spResetTraces(); SP.stale = !!SP.data; spSave(); spPlanSoon(); specDraw();
  if (SP.running) spRestartLoop(100); else specStatus(SP.stale ? 'Step changed: sweep to measure with it' : 'Stopped');
}
const SP_TOP_MIN = -200, SP_TOP_MAX = 100;    // the Top field's limits (a far-off value made every redraw take seconds)
function spTopLimit(t){ return Math.min(Math.max(t, SP_TOP_MIN), SP_TOP_MAX); }
function specLevel(){
  const f = document.getElementById('sp-top'), t = parseFloat(f.value);
  SP.top = isFinite(t) ? spTopLimit(t) : null; if (SP.top !== null && SP.top !== t) f.value = SP.top;
  SP.range = document.getElementById('sp-range').value || 'auto';
  spSave(); specDraw();
}
function specAvg(){
  SP.avgN = Number(document.getElementById('sp-avg').value) || 0; SP.avg = {}; SP.lines = null;
  spSave(); specDraw();
}

async function specInit(){
  if (SP.meta){ specDraw(); return; }
  let m; try { m = await apiGet('/api/spectrum/meta'); } catch (e) { specStatus(e.message || 'Spectrum not available'); return; }
  SP.meta = m;
  const ds = document.getElementById('sp-dongle'); ds.innerHTML = '';
  const all = el('option', '', 'All dongles (compare)'); all.value = 'all'; ds.appendChild(all);
  m.dongles.forEach(d => { const o = el('option', '', d.name + (d.antenna_note ? ' - ' + d.antenna_note : '') +
      (d.best_for && d.best_for.length ? ' (best for ' + d.best_for.join(', ') + ')' : '')); o.value = d.serial; ds.appendChild(o); });
  spPresetChips(m);
  const rb = document.getElementById('sp-rbw');
  (m.steps_khz || []).forEach(k => { const o = el('option', '', k + ' kHz'); o.value = String(k); rb.appendChild(o); });
  const saved = spLoad() || {};
  SP.helpOpen = !!saved.help;
  ds.value = saved.d ? saved.d : (m.default || 'all'); if (!ds.value) ds.value = 'all';
  if (saved.sc) specScale(saved.sc, true);
  if (saved.rbw && [...rb.options].some(o => o.value === String(saved.rbw))){ rb.value = String(saved.rbw); SP.rbw = rb.value; }
  if (typeof saved.top === 'number' && isFinite(saved.top)){ SP.top = spTopLimit(saved.top); document.getElementById('sp-top').value = SP.top; }
  const rg = document.getElementById('sp-range');
  if (saved.rng && [...rg.options].some(o => o.value === String(saved.rng))){ rg.value = String(saved.rng); SP.range = rg.value; }
  const gn = document.getElementById('sp-gain'), lv = document.getElementById('sp-lvl');
  if (saved.gain && [...gn.options].some(o => o.value === String(saved.gain))){ gn.value = String(saved.gain); SP.gain = gn.value; }
  if (saved.lvl === 'raw'){ lv.value = 'raw'; SP.lvl = 'raw'; }
  const av = document.getElementById('sp-avg');
  if (saved.avg && [...av.options].some(o => o.value === String(saved.avg))){ av.value = String(saved.avg); SP.avgN = Number(saved.avg); }
  SP.autoZoom = saved.az !== false; document.getElementById('sp-autozoom').checked = SP.autoZoom;
  // the view the user left, whatever the presets now cover (a preset of that name may have another range since:
  // the region changed); spFields marks the chip whose range it is
  const s = parseFloat(saved.s), e = parseFloat(saved.e);
  if (isFinite(s) && isFinite(e) && e > s) specSetView(s, e, {force: true, noHist: true});
  else if (saved.p && m.presets.some(p => p.id === saved.p)) specPreset(saved.p);
  else specPreset('fm');
  SP.hist = []; spFields();
  spLoadRegion();
}
// The preset chips (the server's list follows the region: some bands differ from one region to another).
function spPresetChips(m){
  const box = document.getElementById('sp-presets'); box.innerHTML = '';
  (m.presets || []).forEach(p => {
    const c = el('div', 'chip', p.name); c.dataset.id = p.id; c.onclick = () => specPreset(p.id);
    const part = p.tuned_part === undefined ? p.tuned : p.tuned_part;
    if (part === false){ c.classList.add('untuned'); c.title = 'No receiver here tunes this range. It can still be shown: the plot will say so.'; }
    else if (p.tuned === false) c.title = 'Only part of this range is tuned by a receiver here.';
    box.appendChild(c);
  });
}
// ---- band labels: the region (and country) whose band plan names the signals in the list ----
// GET /api/region; a server without it leaves the line hidden. The admin picks another region here; an extra
// account reads it (the server refuses a change from it with 403, which also turns the selects into text).
async function spLoadRegion(){
  let r; try { r = await apiGet('/api/region'); } catch (e){ return; }
  if (!r || typeof r !== 'object' || !r.id) return;
  SP.region = r; spRegionDraw();
}
function spRegionName(r){ return String(r.name || r.id) + (r.country_name ? ', ' + r.country_name : ''); }
function spRegionDraw(msg){
  const box = document.getElementById('sp-region'), r = SP.region; if (!box) return;
  box.innerHTML = ''; box.hidden = !r; if (!r) return;
  const how = r.guessed ? ' (guessed from the time zone)' : (r.source === 'default' ? ' (the default: none chosen)' : '');
  if (SP.regionRO || document.body.dataset.role === 'viewer'){       // one run of text (the line is a flex row)
    const t = el('span', '', 'Band labels: '); t.appendChild(el('b', '', spRegionName(r)));
    t.appendChild(document.createTextNode(how + '. Only the admin account can change it.')); box.appendChild(t);
  } else {
    box.appendChild(el('span', 'lab', 'Band labels: '));
    const sel = el('select', 'sel'); sel.id = 'sp-region-sel'; sel.setAttribute('aria-label', 'Region for the band labels');
    (Array.isArray(r.choices) ? r.choices : []).forEach(c => { const o = el('option', '', String(c.name)); o.value = String(c.id); sel.appendChild(o); });
    if (![...sel.options].some(o => o.value === r.id)){ const o = el('option', '', String(r.name || r.id)); o.value = r.id; sel.appendChild(o); }
    sel.value = r.id; sel.onchange = () => spRegionSet(sel.value, null);    // another region: its own countries
    box.appendChild(sel);
    const cs = (Array.isArray(r.countries) ? r.countries : []).filter(c => c && c.region === r.id);
    if (cs.length){
      const cy = el('select', 'sel'); cy.id = 'sp-country-sel'; cy.setAttribute('aria-label', 'Country for the band labels');
      const all = el('option', '', 'Whole region'); all.value = ''; cy.appendChild(all);
      cs.forEach(c => { const o = el('option', '', String(c.name)); o.value = String(c.id); cy.appendChild(o); });
      cy.value = r.country || ''; cy.onchange = () => spRegionSet(r.id, cy.value || null);
      box.appendChild(cy);
    }
    if (how) box.appendChild(el('span', 'sub', how));
  }
  box.appendChild(el('span', 'sub', ' A label is the band plan there, never an identification.'));
  if (msg) box.appendChild(el('div', 'sp-region-msg', msg));
}
async function spRegionSet(id, country){
  let r; try { r = await post('/api/region', {id: id, country: country}); }
  catch (e){
    if (e.status === 403) SP.regionRO = true;
    spRegionDraw(e.status === 403 ? '' : 'Not changed: ' + e.message);       // (the text line says who can change it)
    return;
  }
  if (!r || !r.id) return;
  SP.region = r; spRegionDraw('Band labels now follow ' + spRegionName(r) + '.');
  await spReloadMeta();
  // the list's labels came from the old band plan: a quick view is swept again for them, a slow one is left for later
  if (!SP.data || SP.running) return;
  const p = specParams(); let pl; try { pl = await spPlan(p); } catch (e){ return; }
  if (specParams().key === p.key && spIdle() && spQuick(pl)) specOnce({label: 'Band labels changed: sweeping again for the new labels'});
  else if (!SP.busy) specStatus('Band labels changed: sweep again to see them in the list');
}
// The presets again (after a region change): the view stays where it is.
async function spReloadMeta(){
  let m; try { m = await apiGet('/api/spectrum/meta'); } catch (e){ return; }
  if (!m || !Array.isArray(m.presets)) return;
  SP.meta = m; spPresetChips(m);
  spFields(); spSave();                         // (a preset id whose range moved with the region is let go, also in what is saved)
}
function specScale(v, quiet){
  SP.scale = v; document.querySelectorAll('#sp-scale button').forEach(b => b.classList.toggle('on', b.dataset.v === v));
  SP.yMin = null; SP.yMax = null; SP.hold = {}; SP.holdN = 0; SP.holdMax = -1e9; SP.holdSince = SP.holdOn ? new Date() : null;
  SP.avg = {}; SP.lines = null;
  if (!quiet){ if (SP.sel !== null) specSelect(SP.sel, true); specDraw(); spSave(); }
}
function specStatus(t){ document.getElementById('sp-status').textContent = t; }
function specParams(){
  const d = document.getElementById('sp-dongle').value, s = SP.view.s, e = SP.view.e;
  return {d: d, s: s, e: e, key: d + '|' + s + '|' + e + '|' + SP.rbw + '|' + SP.gain + '|' + SP.lvl};
}
// Peak hold, the average, the level scale and the waterfall start again (a new view, receiver or step).
function spResetTraces(){
  SP.hold = {}; SP.holdN = 0; SP.holdMax = -1e9; SP.holdSince = SP.holdOn ? new Date() : null;
  SP.avg = {}; SP.yMin = null; SP.yMax = null; SP.lines = null; SP.wfRows = 0;
  const wf = document.getElementById('wf-canvas'); const ctx = wf.getContext('2d');
  ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.fillStyle = '#05070d'; ctx.fillRect(0, 0, wf.width, wf.height);
  spWfHatch();
}
function spForget(){                            // nothing measured is on the page any more
  SP.data = null; SP.stale = false; SP.lines = null; SP.namesMsg = null;
  document.getElementById('sp-peaks').innerHTML = '';
  document.getElementById('sp-warn').innerHTML = '';
  document.getElementById('sp-legend').innerHTML = '';
}
function spDeltaOff(){ SP.ref = null; document.getElementById('sp-delta').classList.remove('on'); }
function specCancel(){ SP.run += 1; SP.busy = false; SP.prog = null; }      // late answers of that sweep are ignored
function specClear(){
  specCancel(); spResetTraces(); spForget();
  SP.sel = null; spDeltaOff();
  document.getElementById('sp-sel').style.display = 'none'; document.getElementById('sp-readout').textContent = SP_HINT;
  spSave(); specDraw();
  if (SP.running) spRestartLoop(100); else specStatus('Stopped');
}

// ---- the plan, and sweeping piece by piece ----
function spPlan(p){                             // asked once per receiver + range + step, then remembered for a minute
  const hit = SP.plans[p.key];
  if (hit && Date.now() - hit.at < 60000) return hit.pr;
  const keys = Object.keys(SP.plans); if (keys.length > 60) keys.slice(0, 30).forEach(k => delete SP.plans[k]);
  const pr = apiGet('/api/spectrum/plan?dongle=' + encodeURIComponent(p.d) + '&start=' + p.s + '&stop=' + p.e +
                    (SP.rbw !== 'auto' ? '&rbw=' + encodeURIComponent(SP.rbw) : ''));
  SP.plans[p.key] = {at: Date.now(), pr: pr};
  pr.catch(() => { delete SP.plans[p.key]; });
  return pr;
}
function spUsePlan(pl){
  SP.plan = pl; document.getElementById('sp-gap').textContent = pl.gap_note || '';
  spWfHatch(); spDrawSoon();
}
function spPlanSoon(){
  clearTimeout(SP.planT);
  SP.planT = setTimeout(async () => {
    const p = specParams(); if (!(p.e > p.s)) return;
    let pl; try { pl = await spPlan(p); } catch (e){ return; }
    if (specParams().key === p.key) spUsePlan(pl);
  }, 120);
}
async function spFetchPiece(p, pl, c){
  let url = '/api/spectrum?dongle=' + encodeURIComponent(p.d) + '&start=' + c.start + '&stop=' + c.stop;
  if (pl.mode === 'sweep') url += '&step_khz=' + pl.step_khz + '&points=' + c.points;
  url += '&gain=' + encodeURIComponent(SP.gain) + '&level=' + SP.lvl;
  const r = await fetch(url); if (toLogin(r)) return null;
  const j = await r.json().catch(() => ({error: 'bad reply'}));
  j.failed = !r.ok; return j;
}
const spWait = ms => new Promise(r => setTimeout(r, ms));
// One whole sweep of the view. opt.label: what the status line says while it runs (a sweep the page started by
// itself says why); opt.auto: the page started it (a zoomed view).
async function specOnce(opt){
  if (SP.busy) return;
  const label = opt && typeof opt.label === 'string' ? opt.label : '', auto = !!(opt && opt.auto === true);
  const p = specParams();
  if (!(p.e > p.s)){ specStatus('Enter a start and stop frequency'); return; }
  SP.key = p.key;
  const run = ++SP.run; SP.busy = true;
  try{
    let pl;
    try { pl = await spPlan(p); } catch (e){ if (run === SP.run) specStatus(e.message || 'Connection problem'); return; }
    if (run !== SP.run) return;
    spUsePlan(pl);
    if (pl.mode === 'none'){                    // nothing here tunes any of it: no radio is asked, nothing is drawn
      SP.data = {mode: 'none', traces: [], peaks: [], warnings: [], unit: null, start_mhz: pl.start_mhz, stop_mhz: pl.stop_mhz,
                 left_out: pl.left_out || [], gap_note: pl.gap_note, time: new Date().toTimeString().slice(0, 8)};
      SP.stale = false; SP.lines = null; SP.retry = false;
      specDraw(); specPeaks(); specLegend(); specWarnings();
      specStatus('Nothing to measure in this range');
      return;
    }
    const n = pl.chunks.length, parts = [], t0 = Date.now();
    for (let i = 0; i < n; i++){
      const c = pl.chunks[i];
      SP.prog = n > 1 ? {i: i, n: n, s: c.start, e: c.stop} : null;
      specStatus(n > 1 ? (label || 'Sweeping ' + spRangeText(pl.covered[0][0], pl.covered[pl.covered.length - 1][1])) + ': ' + (i + 1) + ' of ' + n
                       : (label || (pl.mode === 'live' ? 'Capturing...' : 'Sweeping...')));
      if (n > 1) specDraw();
      let j = null;
      for (let tries = 0; ; tries++){
        j = await spFetchPiece(p, pl, c);
        if (j === null || run !== SP.run) return;
        if (!j.failed || !j.busy || tries >= 2 || n === 1) break;
        specStatus('Receiver busy, trying this piece again: ' + (i + 1) + ' of ' + n); await spWait(1500);
        if (run !== SP.run) return;
      }
      if (j.failed){
        SP.retry = !!j.busy;
        // with Live on the whole sweep starts again in a moment; a single sweep ends here
        const again = j.busy && SP.running;
        specStatus((j.busy ? (again ? 'Dongle busy, trying again in a moment: ' : 'Dongle busy: ') : '') + (j.error || 'error') +
                   (n > 1 && !again ? ' (stopped at piece ' + (i + 1) + ' of ' + n + ')' : ''));
        return;
      }
      parts.push(j);
      if (n > 1){ spSplice(j); specDraw(); }
    }
    SP.retry = false; SP.prog = null;
    SP.data = spMerge(parts, pl); SP.stale = false; SP.lines = null;
    specUpdateAvg(); specUpdateHold(); specDraw(); specWaterfall(); specPeaks(); specLegend(); specWarnings();
    if (SP.sel !== null) specSelect(SP.sel, true);
    const rg = SP.data.region;                  // the region was changed elsewhere (another page, the chat): follow it
    if (rg && rg.id && SP.region && rg.id !== SP.region.id){ spLoadRegion(); spReloadMeta(); }
    const j = SP.data, took = n > 1 ? ((Date.now() - t0) / 1000).toFixed(1) + ' s' : j.took_ms + ' ms';
    let st = (j.mode === 'live' ? 'Live IQ' : 'Sweep') + ' ' + j.time + ' | ' + took + ' | step ' + j.step_khz + ' kHz';
    if (pl.step_raised) st += ' (raised from ' + pl.step_asked_khz + ')';
    else if (pl.step_lowered) st += ' (lowered from ' + pl.step_asked_khz + ')';
    if (j.spikes_removed) st += ' | receiver spikes removed';
    else if (j.spikes_kept) st += ' | receiver spikes not removed at this step';
    if (SP.holdOn && SP.holdSince) st += ' | peak hold since ' + SP.holdSince.toTimeString().slice(0, 5) + ' (' + SP.holdN + (SP.holdN === 1 ? ' sweep)' : ' sweeps)');
    const an = spAvgCount(); if (an) st += ' | average of ' + an;
    if (auto) st += ' | swept by itself after the zoom';
    specStatus(st);
    document.getElementById('sp-status').title = pl.step_note || '';
  }catch(e){ if (run === SP.run) specStatus('Connection problem'); }
  finally{ if (run === SP.run){ SP.busy = false; if (SP.prog){ SP.prog = null; specDraw(); } } }
}
// One piece of a sweep in progress goes into what is on screen: the earlier sweep stays where it has not been
// swept again yet.
function spSplice(j){
  let d = SP.data;
  if (!d || d.mode !== 'sweep')
    d = SP.data = {mode: 'sweep', unit: j.unit, start_mhz: SP.view.s, stop_mhz: SP.view.e, traces: [], peaks: [], warnings: [],
                   step_khz: j.step_khz, left_out: [], fm_plan_checked: j.fm_plan_checked};
  const lo = j.start_mhz - 1e-7, hi = j.stop_mhz + 1e-7;
  (j.traces || []).forEach(t => {
    if (!t.db || !t.db.length) return;
    let o = d.traces.find(x => x.dongle === t.dongle);
    if (!o){ o = {dongle: t.dongle, name: t.name, antenna: t.antenna, freqs: [], db: [], fl: [], segs: [], floor_db: t.floor_db,
                  unit: t.unit, gain_db: t.gain_db, peaks: 0, health: 'ok', brk: 0}; d.traces.push(o); }
    if (!o.db){ o.freqs = []; o.db = []; o.fl = []; o.segs = []; o.brk = 0; delete o.error; o.health = 'ok'; }
    if (!o.fl) o.fl = o.freqs.map(() => o.floor_db);          // an earlier sweep in one piece had one floor for all
    if (!o.segs) o.segs = o.freqs.length ? [{s: o.freqs[0], e: o.freqs[o.freqs.length - 1], floor: o.floor_db}] : [];
    const a = spLower(o.freqs, lo), b = spLower(o.freqs, hi), n = t.freqs.length;
    o.freqs = o.freqs.slice(0, a).concat(t.freqs, o.freqs.slice(b));
    o.db = o.db.slice(0, a).concat(t.db, o.db.slice(b));
    o.fl = o.fl.slice(0, a).concat(new Array(n).fill(t.floor_db), o.fl.slice(b));
    const segs = [], r = t.range_mhz || [j.start_mhz, j.stop_mhz];
    o.segs.forEach(x => {
      if (x.e <= lo || x.s >= hi){ segs.push(x); return; }
      if (x.s < lo) segs.push({s: x.s, e: j.start_mhz, floor: x.floor});
      if (x.e > hi) segs.push({s: j.stop_mhz, e: x.e, floor: x.floor});
    });
    segs.push({s: r[0], e: r[1], floor: t.floor_db});
    o.segs = segs; o.floor_db = Math.min.apply(null, segs.map(x => x.floor));
    o.brk = Math.max(o.brk || 0, spBreak(r[0], r[1], n));
    o.gp = spGpSplice(o.gp, j.start_mhz, j.stop_mhz, spGainPieces(t));
  });
  d.peaks = (d.peaks || []).filter(m => m.mhz < lo || m.mhz > hi).concat(j.peaks || []);
  // the picture's scale follows the lines on it, not the latest piece: a piece only a calibrated receiver measured
  // must not label the other lines' levels with its unit
  const us = new Set(d.traces.filter(t => t.db && t.db.length).map(t => t.unit));
  d.unit = us.size === 1 ? [...us][0] : (us.size ? 'mixed' : j.unit); SP.lines = null;
}
function spBreak(lo, hi, n){ return 3 * (hi - lo) / Math.max(1, n - 1); }     // points further apart than this are not joined
// The gain each part of a trace was measured at (the server's gain_pieces, [[lo MHz, hi MHz, gain dB], ...]): Auto
// gain cuts a sweep where a band with its own best gain begins. Anything else in that place is ignored: no lines.
function spGainPieces(t){
  const q = t && Array.isArray(t.gain_pieces) ? t.gain_pieces : [];
  const ok = q.filter(x => Array.isArray(x) && x.length >= 3 && [0, 1, 2].every(i => typeof x[i] === 'number' && isFinite(x[i])) && x[0] < x[1]);
  return ok.length ? ok.map(x => [x[0], x[1], x[2]]).sort((a, b) => a[0] - b[0]) : null;
}
// One piece swept again: what lay between lo and hi goes, the new pieces come in.
function spGpSplice(old, lo, hi, add){
  const out = [];
  (old || []).forEach(x => {
    if (x[1] <= lo + 1e-7 || x[0] >= hi - 1e-7){ out.push(x); return; }
    if (x[0] < lo) out.push([x[0], lo, x[2]]);
    if (x[1] > hi) out.push([hi, x[1], x[2]]);
  });
  return out.concat(add || []).sort((a, b) => a[0] - b[0]);
}
// Where the gain changed along one line: between two pieces that touch and were measured at different gains.
function spGainEdges(gp){
  const out = [];
  for (let i = 1; i < (gp || []).length; i++){
    const a = gp[i - 1], b = gp[i];
    if (Math.abs(a[1] - b[0]) <= 1e-4 && Math.abs(a[2] - b[2]) >= 0.05) out.push({f: b[0], a: a[2], b: b[2]});
  }
  return out;
}
function spGainText(g){ return g.toFixed(1); }
// The legend's gains in frequency order, as the dotted lines show them ("37.2 to 8.7 to 37.2 to 29.7 to 37.2"): one
// entry per run of pieces at the same gain. Without pieces, the server's own words for the gain.
function spGainList(t){
  const gp = t.gp || spGainPieces(t) || [], out = [];
  gp.forEach(x => { if (!out.length || Math.abs(out[out.length - 1] - x[2]) >= 0.05) out.push(x[2]); });
  return out.length ? out.map(spGainText).join(' to ') : String(t.gain_db);
}
// The pieces of a whole sweep as one set of lines, signals and warnings.
function spMerge(parts, pl){
  const first = parts[0], last = parts[parts.length - 1], one = parts.length === 1;
  const d = {mode: first.mode, unit: parts.every(x => x.unit === first.unit) ? first.unit : 'mixed',
             start_mhz: pl.start_mhz, stop_mhz: pl.stop_mhz, time: last.time, took_ms: parts.reduce((a, x) => a + (x.took_ms || 0), 0),
             step_khz: first.step_khz, traces: [], peaks: [], warnings: [], gap_note: pl.gap_note, left_out: pl.left_out || [],
             spikes_removed: parts.reduce((a, x) => a + (x.spikes_removed || 0), 0), spikes_kept: parts.some(x => x.spikes_kept),
             fm_plan_checked: parts.every(x => x.fm_plan_checked), dongle_name: first.dongle_name,
             region: last.region && typeof last.region === 'object' ? last.region : null};
  const by = {};
  parts.forEach(j => (j.traces || []).forEach(t => {
    let o = by[t.dongle];
    if (!o){ o = by[t.dongle] = {dongle: t.dongle, name: t.name, antenna: t.antenna, unit: t.unit, peaks: 0, brk: 0,
                                 gains: [], n: 0, flat: 0, quiet: 0, failed: 0}; d.traces.push(o); }
    if (!t.db || !t.db.length){ o.failed += 1; o.lastError = t.error || 'no answer'; o.errHealth = t.health; return; }
    const r = t.range_mhz || [j.start_mhz, j.stop_mhz], n = t.freqs.length;
    if (one){ o.freqs = t.freqs; o.db = t.db; o.fl = null; o.segs = null; }
    else {
      if (!o.freqs){ o.freqs = []; o.db = []; o.fl = []; o.segs = []; }
      let k = 0; const lastF = o.freqs.length ? o.freqs[o.freqs.length - 1] : -1;
      while (k < n && t.freqs[k] <= lastF) k++;               // the point two pieces share is kept once
      for (let i = k; i < n; i++){ o.freqs.push(t.freqs[i]); o.db.push(t.db[i]); o.fl.push(t.floor_db); }
      o.segs.push({s: r[0], e: r[1], floor: t.floor_db});
    }
    o.floor_db = o.n ? Math.min(o.floor_db, t.floor_db) : t.floor_db;
    o.range_mhz = o.n ? [Math.min(o.range_mhz[0], r[0]), Math.max(o.range_mhz[1], r[1])] : [r[0], r[1]];
    o.brk = Math.max(o.brk, spBreak(r[0], r[1], n));
    o.peaks += t.peaks || 0; o.n += 1; o.unit = t.unit;
    const gp = spGainPieces(t); if (gp) o.gp = (o.gp || []).concat(gp).sort((a, b) => a[0] - b[0]);
    if (o.gains.indexOf(t.gain_db) < 0) o.gains.push(t.gain_db);
    if (t.health === 'flat') o.flat += 1; else if (t.health === 'quiet') o.quiet += 1;
  }));
  d.traces.forEach(o => {
    if (!o.n){ o.error = o.lastError; o.health = o.errHealth || 'error'; return; }
    o.gain_db = o.gains.join(' to ');
    o.health = o.flat === o.n ? 'flat' : (o.quiet === o.n ? 'quiet' : 'ok');   // flat only when flat in every piece
  });
  if (one) d.warnings = first.warnings || [];
  else {                                        // a warning stands when it names exactly the receivers flat throughout
    const flat = d.traces.filter(o => o.health === 'flat').map(o => o.dongle).sort().join();
    const m = flat && parts.find(j => (j.warnings || []).length &&
      (j.traces || []).filter(t => t.health === 'flat').map(t => t.dongle).sort().join() === flat);
    d.warnings = m ? m.warnings : [];
  }
  d.peaks = spJoinPeaks(parts, first.step_khz);
  return d;
}
// The signals of all pieces. A signal cut by the border of two pieces was found once in each: it reaches the end of
// one piece and the start of the next, and is kept once (the clearer find, with the whole extent). The signals of
// one piece were already told apart by the server and are never joined here. The list stays a readable length.
function spJoinPeaks(parts, stepKhz){
  if (parts.length === 1) return parts[0].peaks || [];
  const st = (stepKhz || 0) / 1000, pk = [];
  const ext = m => typeof m.lo_mhz === 'number' && typeof m.hi_mhz === 'number';
  parts.forEach((j, pi) => (j.peaks || []).forEach(m => {
    const k = pk.findIndex(x => x.pi === pi - 1 && (ext(x.m) && ext(m)
      ? x.m.hi_mhz >= j.start_mhz - 1.5 * st && m.lo_mhz <= j.start_mhz + 1.5 * st
      : Math.abs(x.m.mhz - m.mhz) <= 2 * st));
    if (k < 0){ pk.push({m: m, pi: pi}); return; }
    const o = pk[k].m, keep = m.snr_db > o.snr_db ? m : o;
    if (ext(o) && ext(m)){ const a = Math.min(o.lo_mhz, m.lo_mhz), b = Math.max(o.hi_mhz, m.hi_mhz); keep.lo_mhz = a; keep.hi_mhz = b; }
    pk[k] = {m: keep, pi: pi};
  }));
  return pk.map(x => x.m).sort((a, b) => ((b.decoded === 'check') - (a.decoded === 'check')) || (b.flag - a.flag) || (b.snr_db - a.snr_db)).slice(0, 60);
}
function spRestartLoop(delay){ clearTimeout(SP.timer); const id = ++SP.loopId; SP.timer = setTimeout(() => specLoop(id), delay || 0); }
function specToggle(){
  SP.running = !SP.running;
  const b = document.getElementById('sp-live'); b.classList.toggle('on', SP.running); b.textContent = SP.running ? 'Stop' : 'Live';
  clearTimeout(SP.timer);
  if (SP.running) spRestartLoop(0);
  else { SP.loopId += 1; const was = SP.busy; specCancel(); specStatus('Stopped'); if (was) specDraw(); }
}
async function specLoop(id){
  if (!SP.running || id !== SP.loopId) return;
  if (document.hidden || activeTab() !== 'spectrum'){ SP.timer = setTimeout(() => specLoop(id), 1000); return; }
  await specOnce();
  if (!SP.running || id !== SP.loopId) return;
  const d = SP.data, wait = SP.retry ? 2000 : (d && d.mode === 'none' ? 3000 : (d && d.mode === 'live' ? 250 : 600));
  SP.timer = setTimeout(() => specLoop(id), wait);
}
function specHoldToggle(){
  SP.holdOn = !SP.holdOn; document.getElementById('sp-hold').classList.toggle('on', SP.holdOn);
  SP.hold = {}; SP.holdN = 0; SP.holdMax = -1e9; SP.holdSince = SP.holdOn ? new Date() : null;
  if (SP.holdOn && SP.data && !SP.stale) specUpdateHold();
  specDraw();
}

// ---- what gets plotted ----
// One line per receiver, in dBFS (0 = an 8-bit full-scale sine, so nothing sits above 0; not a calibrated strength)
// or in dB above that receiver's own floor. Built once per sweep (and per scale or average change), not per frame:
// raw = this sweep's values, vals = what is drawn and read out (the average when Average is on).
function spLower(a, x){ let lo = 0, hi = a.length; while (lo < hi){ const m = (lo + hi) >> 1; if (a[m] < x) lo = m + 1; else hi = m; } return lo; }
function spNear(a, x){                          // index of the value nearest x in an ascending list
  const i = spLower(a, x); if (i <= 0) return a.length ? 0 : -1; if (i >= a.length) return a.length - 1;
  return (x - a[i - 1] <= a[i] - x) ? i - 1 : i;
}
function spLines(){
  if (SP.lines) return SP.lines;
  const j = SP.data, rel = SP.scale === 'rel';
  SP.lines = !j ? [] : (j.traces || []).filter(t => t.db && t.db.length).map(t => {
    const n = t.db.length, fl = t.fl; let raw = t.db, mx = -1e9;
    if (rel){ raw = new Array(n); for (let i = 0; i < n; i++) raw[i] = Math.round((t.db[i] - (fl ? fl[i] : t.floor_db)) * 10) / 10; }
    for (let i = 0; i < n; i++) if (raw[i] > mx) mx = raw[i];
    let vals = raw; const a = SP.avgN ? SP.avg[t.dongle] : null;
    if (a && a.p.length === n){ vals = new Array(n); for (let i = 0; i < n; i++) vals[i] = Math.round(100 * Math.log10(a.p[i])) / 10; }
    // spans: the parts of the view this receiver measured (its pieces, joined where they touch)
    const spans = [];
    (t.segs && t.segs.length ? t.segs.map(q => [q.s, q.e]) : [t.range_mhz || [t.freqs[0], t.freqs[n - 1]]])
      .sort((a, b) => a[0] - b[0]).forEach(q => { const k = spans[spans.length - 1]; if (k && q[0] <= k[1] + 1e-6) k[1] = Math.max(k[1], q[1]); else spans.push([q[0], q[1]]); });
    return {key: t.dongle, name: t.name, color: spColor(t.dongle), freqs: t.freqs, raw: raw, vals: vals, max: mx,
            floor: rel ? 0 : t.floor_db, segs: rel ? null : t.segs, fl: rel ? null : (fl || null), health: t.health, unit: t.unit,
            brk: t.brk || spBreak(t.freqs[0], t.freqs[n - 1], n), spans: spans, edges: spGainEdges(t.gp || spGainPieces(t))};
  });
  return SP.lines;
}
function specUpdateHold(){
  if (!SP.holdOn || !SP.data) return;
  SP.holdN += 1;
  spLines().forEach(l => {
    const h = SP.hold[l.key], v = l.raw;
    if (!h || h.length !== v.length) SP.hold[l.key] = v.slice();
    else for (let i = 0; i < v.length; i++) if (v[i] > h[i]) h[i] = v[i];
    if (l.max > SP.holdMax) SP.holdMax = l.max;
  });
}
// Average: a running mean over about N sweeps, taken as power (not as dB: the mean of dB values would read low).
function specUpdateAvg(){
  if (!SP.avgN || !SP.data) return;
  spLines().forEach(l => {
    const n = l.raw.length; let a = SP.avg[l.key];
    if (!a || a.p.length !== n) a = SP.avg[l.key] = {p: new Float64Array(n), n: 0};
    a.n = Math.min(a.n + 1, SP.avgN); const w = 1 / a.n, p = a.p, v = l.raw;
    for (let i = 0; i < n; i++) p[i] += (Math.pow(10, v[i] / 10) - p[i]) * w;
  });
  SP.lines = null;
}
function spAvgCount(){ let n = 0; if (SP.avgN) Object.values(SP.avg).forEach(a => { if (a.n > n) n = a.n; }); return n; }
function specCanvas(id){
  const c = document.getElementById(id); const dpr = window.devicePixelRatio || 1;
  const w = Math.max(200, c.clientWidth), h = c.clientHeight;
  if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)){ c.width = Math.round(w * dpr); c.height = Math.round(h * dpr); }
  const ctx = c.getContext('2d'); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return {c: c, ctx: ctx, w: w, h: h};
}
function niceStep(x){ const p = Math.pow(10, Math.floor(Math.log10(x))); for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= x) return m * p; return 10 * p; }
function fmtMHz(f, st){ const d = st >= 1 ? 0 : (st >= 0.1 ? 1 : (st >= 0.01 ? 2 : 3)); return f.toFixed(d); }
// The level of a line at a frequency: its nearest point (binary search), or null outside what that receiver measured
// (its own range ends there, even when another receiver's line goes on) and where it has no point near enough.
// Nothing is read out where nothing was measured.
function valAt(l, f){
  if (!l.spans.some(q => f >= q[0] - 1e-6 && f <= q[1] + 1e-6)) return null;
  const i = spNear(l.freqs, f); return (i < 0 || Math.abs(l.freqs[i] - f) > l.brk) ? null : l.vals[i];
}
function spTopAt(lines, f){ let best = null; lines.forEach(l => { const v = valAt(l, f); if (v !== null && (best === null || v > best)) best = v; }); return best; }
function spGaps(){ return (SP.plan && SP.plan.gaps) || []; }
function spInGap(f){ return spGaps().some(g => f >= g[0] && f <= g[1]); }
// The level scale: RTL-SDR sweeps and every live IQ frame in dBFS; a Signal Hound's sweeps in calibrated dBm; other
// receivers' sweeps in their own dB; 'mixed' when receivers on different scales are compared (read above floor).
function unitOf(u){ return u === 'dBFS' ? ' dBFS' : u === 'dBm' ? ' dBm' : ' dB'; }
function axisLabel(u){
  return u === 'dBm' ? 'dBm (calibrated)' : u === 'dB' ? "dB (the receiver's own scale)"
    : u === 'mixed' ? 'dB (receivers on different scales: use Above floor)'
    : SP.lvl === 'input' ? 'dBFS at the input (receiver gain taken off)' : 'dBFS (0 = full scale, as read)';
}
function spSigned(x, d){ return (x >= 0 ? '+' : '') + x.toFixed(d); }
function readoutAt(f){
  const lines = spLines(); let t = f.toFixed(4) + ' MHz';
  if (spInGap(f)) return t + '  |  ' + SP_GAP_TEXT;
  if (!lines.length) return t;
  const u = SP.scale === 'rel' ? ' dB above floor' : unitOf(SP.data && SP.data.unit);
  const got = lines.map(l => [l, valAt(l, f)]).filter(x => x[1] !== null);
  if (!got.length) return t + '  |  not measured here';
  const mixed = SP.scale !== 'rel' && SP.data && SP.data.unit === 'mixed';       // then every level carries its own unit
  t += '  |  ' + (lines.length > 1 ? got.map(x => x[0].name.replace('Dongle ', 'D') + ' ' + x[1].toFixed(1) + (mixed ? unitOf(x[0].unit) : '')).join('  ') : got[0][1].toFixed(1)) + (mixed && lines.length > 1 ? '' : u);
  if (SP.ref !== null && Math.abs(f - SP.ref) > 1e-9) t += '  |  ' + spDeltaText(lines, f);
  return t;
}
// Delta: how far the marker is from the reference. The level difference is taken on one receiver at a time, and only
// a receiver that measured both frequencies gives one: the strongest line at each marker can be two receivers, with
// their own antennas, gains and even scales, and their difference is a number no receiver measured.
function spDeltaText(lines, f){
  const both = lines.map(l => [l, valAt(l, f), valAt(l, SP.ref)]).filter(x => x[1] !== null && x[2] !== null);
  const t = 'delta ' + spSigned(f - SP.ref, 4) + ' MHz';
  if (!both.length) return t + ' from ' + SP.ref.toFixed(4) + ' (no receiver measured both)';
  return t + ', ' + (lines.length > 1 ? both.map(x => x[0].name.replace('Dongle ', 'D') + ' ' + spSigned(x[1] - x[2], 1)).join('  ')
                                      : spSigned(both[0][1] - both[0][2], 1)) + ' dB from ' + SP.ref.toFixed(4);
}
function specLegend(){
  const box = document.getElementById('sp-legend'); box.innerHTML = '';
  const j = SP.data; if (!j) return;
  const whole = r => !r || (r[0] <= j.start_mhz + 1e-6 && r[1] >= j.stop_mhz - 1e-6);
  // when every line covers the same part of the view, that is said once, under the lines
  const got = (j.traces || []).filter(t => !t.error && t.range_mhz), parts = new Set(got.map(t => t.range_mhz.join('-')));
  const shared = got.length > 1 && parts.size === 1 && !whole(got[0].range_mhz) ? got[0].range_mhz : null;
  (j.traces || []).forEach(t => {
    const row = el('div', 'lg'); const i = el('i'); i.style.background = spColor(t.dongle); row.appendChild(i);
    row.appendChild(el('strong', '', t.name));
    if (t.antenna) row.appendChild(el('span', 'sub', t.antenna));
    if (t.error) row.appendChild(el('span', 'bad', t.health === 'busy' ? 'busy (in use by ARIS)' : 'error: ' + t.error));
    else if (t.health === 'flat') row.appendChild(el('span', 'bad', 'flat line: check antenna'));
    else row.appendChild(el('span', 'sub', t.peaks + ' signal' + (t.peaks === 1 ? '' : 's') + ' · floor ' + Number(t.floor_db).toFixed(0) + unitOf(t.unit || j.unit) + ' · gain ' + spGainList(t)));
    if (!t.error && !whole(t.range_mhz) && !shared) row.appendChild(el('span', 'sub', '· measured ' + spRangeText(t.range_mhz[0], t.range_mhz[1])));
    if (!t.error && t.failed) row.appendChild(el('span', 'bad', t.failed + (t.failed === 1 ? ' piece' : ' pieces') + ' not measured: ' + t.lastError));
    box.appendChild(row);
  });
  if (shared) box.appendChild(el('div', 'lg sub', 'Each measured ' + spRangeText(shared[0], shared[1]) + ', all they tune of this view.'));
  (j.left_out || []).forEach(x => {
    const row = el('div', 'lg out'); row.appendChild(el('i')); row.appendChild(el('strong', '', x.name));
    row.appendChild(el('span', 'sub', x.why === 'live' ? 'not in this fast narrow view: choose it on its own to sweep this range'
      : 'tunes ' + spRangeText(x.range_mhz[0], x.range_mhz[1]) + ': none of this view'));
    box.appendChild(row);
  });
}
function specWarnings(){
  const box = document.getElementById('sp-warn'); box.innerHTML = '';
  ((SP.data && SP.data.warnings) || []).forEach(w => box.appendChild(el('div', 'warnbox', w)));
}
function spHatch(ctx){                          // thin diagonal lines: "nothing was measured here"
  if (!SP.hatchTile){
    const t = document.createElement('canvas'); t.width = t.height = 8; const x = t.getContext('2d');
    x.strokeStyle = '#334155'; x.lineWidth = 1; x.beginPath();
    x.moveTo(-1, 9); x.lineTo(9, -1); x.moveTo(-1, 1); x.lineTo(1, -1); x.moveTo(7, 9); x.lineTo(9, 7); x.stroke();
    SP.hatchTile = t;
  }
  return ctx.createPattern(SP.hatchTile, 'repeat');
}
function spDrawSoon(){ if (SP.raf) return; SP.raf = requestAnimationFrame(() => { SP.raf = 0; specDraw(); }); }
function specDraw(){
  const g = specCanvas('spec-canvas'), ctx = g.ctx, w = g.w, h = g.h;
  ctx.fillStyle = '#05070d'; ctx.fillRect(0, 0, w, h); SP.gainEdges = [];
  const p = specParams(), j = SP.data;
  const L = SP_L, R = SP_R, T = SP_T, B = SP_B, pw = w - L - R, ph = h - T - B;
  if (!(p.e > p.s)) return;
  const lines = spLines(), rel = SP.scale === 'rel';
  if (lines.length){
    let mx = -1e9, fl = 1e9;
    lines.forEach(l => { if (l.max > mx) mx = l.max; if (l.floor < fl) fl = l.floor; });
    if (SP.holdOn && SP.holdMax > mx) mx = SP.holdMax;
    // dBFS: the axis ends at 0 (full scale) unless a level really sits above it; other scales follow the data
    const fsScale = !j || !j.unit || j.unit === 'dBFS';
    const lo = rel ? -10 : Math.floor((fl - 10) / 10) * 10;
    const top = Math.ceil((mx + 6) / 10) * 10;
    const hi = rel ? Math.max(20, top) : fsScale ? Math.min(mx <= 0 ? 0 : 10, top) : top;
    SP.yMin = SP.yMin === null ? lo : Math.min(SP.yMin, lo);
    SP.yMax = SP.yMax === null ? hi : Math.max(SP.yMax, hi);
  }
  let y0 = SP.yMin === null ? (rel ? -10 : -60) : SP.yMin, y1 = SP.yMax === null ? (rel ? 30 : 0) : SP.yMax;
  if (SP.top !== null) y1 = SP.top;             // the user's own level axis: Top and Range
  if (SP.range !== 'auto') y0 = y1 - Number(SP.range); else if (y0 > y1 - 20) y0 = y1 - 20;
  const X = f => L + (f - p.s) / (p.e - p.s) * pw, Y = v => T + (1 - (v - y0) / (y1 - y0)) * ph;
  const none = !!(SP.plan && SP.plan.mode === 'none' && SP.plan.start_mhz === p.s && SP.plan.stop_mhz === p.e);
  ctx.strokeStyle = '#1f2937'; ctx.lineWidth = 1; ctx.fillStyle = '#64748b'; ctx.font = '11px system-ui, sans-serif';
  if (!none) for (let v = Math.ceil(y0 / 10) * 10; v <= y1 + 1e-9; v += 10){
    ctx.beginPath(); ctx.moveTo(L, Y(v)); ctx.lineTo(w - R, Y(v)); ctx.stroke(); ctx.textAlign = 'right'; ctx.fillText(String(v), L - 6, Y(v) + 4);
  }
  const stepF = niceStep((p.e - p.s) / (pw < 420 ? 4 : 6));
  for (let f = Math.ceil(p.s / stepF - 1e-9) * stepF; f <= p.e + 1e-9; f += stepF){
    ctx.beginPath(); ctx.moveTo(X(f), T); ctx.lineTo(X(f), T + ph); ctx.stroke();
    const lab = fmtMHz(f, stepF), half = ctx.measureText(lab).width / 2;      // the last label stays on the canvas
    ctx.textAlign = 'center'; ctx.fillText(lab, Math.min(X(f), w - half - 2), h - 6);
  }
  ctx.textAlign = 'left'; if (!none) ctx.fillText(rel ? 'dB above floor' : axisLabel(j && j.unit), 6, 14);
  ctx.textAlign = 'right'; ctx.fillText('MHz', w - R, 14);
  SP.geom = {L: L, pw: pw, s: p.s, e: p.e};
  // what no receiver here tunes: hatched, and labelled where the words fit
  const gaps = spGaps().map(q => [Math.max(L, X(q[0])), Math.min(L + pw, X(q[1]))]).filter(q => q[1] - q[0] >= 1);
  if (gaps.length){
    const pat = spHatch(ctx); ctx.font = '11px system-ui, sans-serif'; ctx.textAlign = 'center';
    const tw = ctx.measureText(SP_GAP_TEXT).width;
    gaps.forEach(q => {
      ctx.fillStyle = '#080b12'; ctx.fillRect(q[0], T, q[1] - q[0], ph); ctx.fillStyle = pat; ctx.fillRect(q[0], T, q[1] - q[0], ph);
      if (q[1] - q[0] >= tw + 14){
        const cx = (q[0] + q[1]) / 2, cy = T + ph / 2;
        ctx.fillStyle = '#05070d'; ctx.fillRect(cx - tw / 2 - 5, cy - 10, tw + 10, 16);
        ctx.fillStyle = '#94a3b8'; ctx.fillText(SP_GAP_TEXT, cx, cy + 2);
      }
    });
  }
  if (!j || j.mode === 'none' || !lines.length){
    if (!none && (!j || j.mode !== 'none')){
      // the hint goes on the widest part that can be measured
      let a = L, b = L + pw; if (gaps.length){ let best = 0, at = L; gaps.concat([[L + pw, L + pw]]).forEach(q => { if (q[0] - at > best){ best = q[0] - at; a = at; b = q[0]; } at = q[1]; }); }
      const hint = 'Press Live or Sweep once'; ctx.textAlign = 'center';
      if (b - a >= ctx.measureText(hint).width + 10){ ctx.fillStyle = '#94a3b8'; ctx.fillText(hint, (a + b) / 2, T + ph / 2 - 22); }
    }
    spMarkers(ctx, X, T, ph); return;
  }
  ctx.save(); ctx.beginPath(); ctx.rect(L, T, pw, ph); ctx.clip();
  // noise floors (dashed): one per receiver in dB mode over the part it measured, a single 0 line in "above floor" mode
  ctx.setLineDash([4, 4]); ctx.lineWidth = 1;
  if (rel){ ctx.strokeStyle = '#475569'; ctx.beginPath(); ctx.moveTo(L, Y(0)); ctx.lineTo(w - R, Y(0)); ctx.stroke(); }
  else lines.forEach(l => {
    ctx.strokeStyle = l.color; ctx.globalAlpha = 0.45; ctx.beginPath();
    (l.segs || [{s: l.freqs[0], e: l.freqs[l.freqs.length - 1], floor: l.floor}]).forEach(q => { ctx.moveTo(X(q.s), Y(q.floor)); ctx.lineTo(X(q.e), Y(q.floor)); });
    ctx.stroke(); });
  ctx.globalAlpha = 1; ctx.setLineDash([]);
  // only the points in view are walked, and points too far apart (a part not measured) are never joined
  const path = (l, vs, fillTo) => {
    const fr = l.freqs, a = Math.max(0, spLower(fr, p.s) - 1), b = Math.min(fr.length, spLower(fr, p.e) + 1);
    let i0 = a;
    for (let i = a; i <= b; i++){
      if (i < b && (i === i0 || fr[i] - fr[i - 1] <= l.brk)) continue;
      if (i > i0){                              // one run of joined points: i0 .. i-1
        ctx.beginPath(); ctx.moveTo(X(fr[i0]), Y(vs[i0]));
        for (let k = i0 + 1; k < i; k++) ctx.lineTo(X(fr[k]), Y(vs[k]));
        ctx.stroke();
        if (fillTo !== undefined){ ctx.lineTo(X(fr[i - 1]), fillTo); ctx.lineTo(X(fr[i0]), fillTo); ctx.closePath(); ctx.fill(); }
      }
      i0 = i;
    }
  };
  if (SP.holdOn) lines.forEach(l => { const hv = SP.hold[l.key]; if (!hv || hv.length !== l.vals.length) return;
    ctx.strokeStyle = lines.length > 1 ? l.color : '#f59e0b'; ctx.globalAlpha = 0.55; ctx.lineWidth = 1; ctx.setLineDash([2, 2]);
    path(l, hv); ctx.setLineDash([]); ctx.globalAlpha = 1; });
  if (SP.stale) ctx.globalAlpha = 0.5;          // measured before the view changed: dimmed until it is swept again
  if (lines.length > 1){
    if (!SP.stale) ctx.globalAlpha = 0.85;
    lines.forEach(l => { ctx.strokeStyle = l.color; ctx.lineWidth = 1.3; path(l, l.vals); });
  } else {
    const l = lines[0]; ctx.strokeStyle = l.color; ctx.lineWidth = 1.25; ctx.fillStyle = 'rgba(16,185,129,0.12)'; path(l, l.vals, T + ph);
  }
  ctx.globalAlpha = 1;
  // Where Auto gain changed (the sweep was cut there and each side measured at its own best gain): a thin dotted
  // line, one per frequency, with the gains either side as the label (one row per dongle when several changed there)
  const gEdges = [];
  lines.forEach(l => (l.edges || []).forEach(e => {
    if (e.f <= p.s || e.f >= p.e || spInGap(e.f)) return;
    let g = gEdges.find(x => Math.abs(x.f - e.f) <= 1e-4); if (!g){ g = {f: e.f, rows: []}; gEdges.push(g); }
    g.rows.push({a: e.a, b: e.b, color: lines.length > 1 ? l.color : '#cbd5e1'});
  }));
  if (gEdges.length){
    ctx.strokeStyle = '#cbd5e1'; ctx.globalAlpha = 0.75; ctx.lineWidth = 1; ctx.setLineDash([1, 3]);
    gEdges.forEach(g => { const x = Math.round(X(g.f)) + 0.5; ctx.beginPath(); ctx.moveTo(x, T); ctx.lineTo(x, T + ph); ctx.stroke(); });
    ctx.setLineDash([]); ctx.globalAlpha = 1;
  }
  // Markers: every peak gets a small triangle; only a few get a label, and labels never overlap.
  const order = (j.peaks || []).filter(m => m.mhz >= p.s && m.mhz <= p.e).sort((a, b) => (b.flag - a.flag) || (b.snr_db - a.snr_db));
  // Every marker's triangle is reserved first, so a label never covers another signal's marker.
  const pos = []; order.forEach(m => { const v = spTopAt(lines, m.mhz); if (v !== null) pos.push({m: m, x: X(m.mhz), y: Y(v)}); });
  const boxes = pos.map(q => ({x0: q.x - 5, x1: q.x + 5, y0: q.y - 11, y1: q.y - 2}));
  pos.forEach(q => { const dq = pkDec(q.m); ctx.fillStyle = dq === 'check' ? '#ef4444' : (dq === 'ok' || q.m.name ? '#22c55e' : (q.m.flag ? '#f59e0b' : (q.m.kind === 'fm' ? '#94a3b8' : '#e2e8f0')));
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
      boxes.push(bx); labelled += 1; const dl = pkDec(m); ctx.fillStyle = dl === 'check' ? '#f87171' : (dl === 'ok' || m.name ? '#6ee7b7' : (m.flag ? '#f59e0b' : '#e2e8f0')); ctx.fillText(txt, cx, ty);
      return;
    }
  });
  // the gain labels: at the top of the plot, else at its foot, never over a signal's label or marker, over a line
  // that was measured (a trace's drawn points) or over another gain line, so each label is plainly its own line's;
  // lines closer than SP_EDGE_LABEL_PX (a wide view: 87.5 and 108 MHz a few pixels apart) get none. The dotted line
  // alone then marks the place, and the legend lists the gains in order.
  ctx.font = '10px system-ui, sans-serif'; ctx.textAlign = 'left';
  gEdges.sort((a, b) => a.f - b.f).forEach(g => { g.x = X(g.f); });
  const fAt = x => p.s + (x - L) / pw * (p.e - p.s);
  const overTrace = bx => lines.some(l => {     // a drawn point inside the box, or a joined stretch through it
    const fr = l.freqs, vs = l.vals; if (!fr || !fr.length) return false;
    const a = Math.max(0, spLower(fr, fAt(bx.x0)) - 1), b = Math.min(fr.length - 1, spLower(fr, fAt(bx.x1)));
    for (let i = a; i <= b; i++){
      const y = Y(vs[i]), x = X(fr[i]);
      if (x >= bx.x0 && x <= bx.x1 && y >= bx.y0 - 1 && y <= bx.y1 + 1) return true;
      if (i > a && fr[i] - fr[i - 1] <= l.brk){
        const yp = Y(vs[i - 1]);
        if (Math.min(y, yp) <= bx.y1 + 1 && Math.max(y, yp) >= bx.y0 - 1) return true;
      }
    }
    return false;
  });
  gEdges.forEach((g, k) => {
    if ((k > 0 && g.x - gEdges[k - 1].x < SP_EDGE_LABEL_PX) || (k + 1 < gEdges.length && gEdges[k + 1].x - g.x < SP_EDGE_LABEL_PX)) return;
    const rows = g.rows.map((r, i) => ({t: (i ? '' : 'gain ') + spGainText(r.a) + ' | ' + spGainText(r.b), c: r.color}));
    const tw = Math.max.apply(null, rows.map(r => ctx.measureText(r.t).width)) + 6, th = rows.length * 12 + 2;
    const x0 = Math.min(Math.max(g.x - tw / 2, L + 1), w - R - tw - 1);
    if (g.x < x0 || g.x > x0 + tw) return;      // (no room for it round its own line)
    if (gEdges.some(o => o !== g && o.x >= x0 - 2 && o.x <= x0 + tw + 2)) return;
    for (const y0 of [T + 2, T + ph - th - 2]){
      const bx = {x0: x0, x1: x0 + tw, y0: y0, y1: y0 + th};
      if (boxes.some(b => bx.x0 < b.x1 && bx.x1 > b.x0 && bx.y0 < b.y1 && bx.y1 > b.y0) || overTrace(bx)) continue;
      boxes.push(bx);
      ctx.fillStyle = 'rgba(5,7,13,0.85)'; ctx.fillRect(bx.x0, bx.y0, tw, th);
      rows.forEach((r, i) => { ctx.fillStyle = r.c; ctx.fillText(r.t, x0 + 3, y0 + 11 + i * 12); });
      g.lab = rows.map(r => r.t); g.box = [Math.round(bx.x0), Math.round(bx.y0), Math.round(bx.x1), Math.round(bx.y1)];
      break;
    }
  });
  // (what is drawn, for the page's checks: each place, its gains, its label and the label's box)
  SP.gainEdges = gEdges.map(g => ({f: g.f, rows: g.rows.map(r => [r.a, r.b]), lab: g.lab || null, box: g.box || null}));
  ctx.restore();
  spMarkers(ctx, X, T, ph);
}
// The marker, the Delta reference, where a sweep in progress is, and the range being dragged out with the mouse.
function spMarkers(ctx, X, T, ph){
  const g = SP.geom, inView = f => f !== null && f >= g.s && f <= g.e;
  ctx.font = '600 10px system-ui, sans-serif'; ctx.textAlign = 'center'; ctx.lineWidth = 1;
  if (SP.prog && inView(SP.prog.s)){
    const x0 = X(SP.prog.s), x1 = X(Math.min(SP.prog.e, g.e));
    ctx.strokeStyle = '#e2e8f0'; ctx.globalAlpha = 0.7; ctx.beginPath(); ctx.moveTo(x0, T); ctx.lineTo(x0, T + ph); ctx.stroke();
    ctx.fillStyle = '#e2e8f0'; ctx.globalAlpha = 0.35; ctx.fillRect(x0, T, Math.max(1, x1 - x0), 2); ctx.globalAlpha = 1;
  }
  if (inView(SP.ref)){
    const x = X(SP.ref); ctx.strokeStyle = '#c084fc'; ctx.setLineDash([5, 3]); ctx.beginPath(); ctx.moveTo(x, T); ctx.lineTo(x, T + ph); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = '#c084fc'; ctx.fillText('R', x, T - 3);
  }
  if (inView(SP.sel)){
    const x = X(SP.sel); ctx.strokeStyle = '#60a5fa'; ctx.beginPath(); ctx.moveTo(x, T); ctx.lineTo(x, T + ph); ctx.stroke();
    if (SP.ref !== null){ ctx.fillStyle = '#60a5fa'; ctx.fillText('M', x, T - 3); }
  }
  const d = SP.drag;
  if (d && d.on){
    const r = document.getElementById('spec-canvas').getBoundingClientRect();
    const a = Math.max(g.L, Math.min(d.x0, d.x1) - r.left), b = Math.min(g.L + g.pw, Math.max(d.x0, d.x1) - r.left);
    if (b > a){ ctx.fillStyle = 'rgba(96,165,250,0.18)'; ctx.fillRect(a, T, b - a, ph); ctx.strokeStyle = '#60a5fa'; ctx.strokeRect(a + 0.5, T + 0.5, b - a - 1, ph - 1); }
  }
}
function spHeat(){                              // the waterfall's colours, worked out once
  if (!SP.heat){ SP.heat = new Uint8Array(256 * 3); for (let i = 0; i < 256; i++){ const c = heatColor(i / 255); SP.heat[i * 3] = c[0]; SP.heat[i * 3 + 1] = c[1]; SP.heat[i * 3 + 2] = c[2]; } }
  return SP.heat;
}
function spWfSize(){
  const c = document.getElementById('wf-canvas'); const dpr = window.devicePixelRatio || 1;
  const w = Math.round(c.clientWidth * dpr), h = Math.round(c.clientHeight * dpr);
  if (w > 0 && h > 0 && (c.width !== w || c.height !== h)){ c.width = w; c.height = h; }
  return {c: c, dpr: dpr, w: c.width, h: c.height, L: Math.round(SP_L * dpr), pw: c.width - Math.round(SP_L * dpr) - Math.round(SP_R * dpr)};
}
// The parts no receiver tunes are hatched down the whole waterfall: no row ever has a colour there.
function spWfHatch(){
  const g = spWfSize(), v = SP.view; if (g.pw <= 0 || !(v.e > v.s)) return;
  const ctx = g.c.getContext('2d'); ctx.setTransform(1, 0, 0, 1, 0, 0);
  spGaps().forEach(q => {
    const a = Math.max(0, Math.round((q[0] - v.s) / (v.e - v.s) * g.pw)), b = Math.min(g.pw, Math.round((q[1] - v.s) / (v.e - v.s) * g.pw));
    if (b - a < 1) return;
    ctx.fillStyle = '#080b12'; ctx.fillRect(g.L + a, 0, b - a, g.h); ctx.fillStyle = spHatch(ctx); ctx.fillRect(g.L + a, 0, b - a, g.h);
  });
}
function specWaterfall(){
  const j = SP.data; if (!j) return;
  const lines = spLines(); if (j.mode === 'none' || !lines.length){ spWfHatch(); return; }
  const g = spWfSize(), c = g.c, dpr = g.dpr, w = g.w, pw = g.pw, L = g.L; if (pw <= 0) return;
  const ctx = c.getContext('2d'); ctx.setTransform(1, 0, 0, 1, 0, 0);
  // Slow sweeps get tall rows so the waterfall fills in about 24 sweeps instead of hundreds.
  const rowCss = j.mode === 'live' ? 2 : Math.max(4, Math.floor(c.clientHeight / 24));
  const rowH = Math.max(1, Math.round(rowCss * dpr));
  ctx.drawImage(c, 0, rowH);
  const p = specParams();
  // colours run from 3 dB under each line's own floor to 40 dB over it (the floor of the piece a point came from):
  // receivers with different floors, or on different scales, then share one picture
  const lo = -3, hi = 40;
  // one level per column: the strongest point that falls in it, or the nearer of the two points around it; a column
  // with no measured point near it stays empty
  if (!SP.wfCol || SP.wfCol.length !== pw) SP.wfCol = new Float32Array(pw);
  const col = SP.wfCol; col.fill(NaN);
  lines.forEach(l => {
    const fr = l.freqs, vs = l.vals, fl = l.fl, a = Math.max(0, spLower(fr, p.s) - 1), b = Math.min(fr.length, spLower(fr, p.e) + 1);
    let px = 0, pv = 0;
    for (let i = a; i < b; i++){
      const x = Math.floor((fr[i] - p.s) / (p.e - p.s) * pw), v = vs[i] - (fl ? fl[i] : l.floor);
      if (i > a && fr[i] - fr[i - 1] <= l.brk)
        for (let k = Math.max(0, px + 1), end = Math.min(pw, x); k < end; k++){ const u = (k - px) * 2 < (x - px) ? pv : v; if (!(col[k] >= u)) col[k] = u; }
      if (x >= 0 && x < pw && !(col[x] >= v)) col[x] = v;
      px = x; pv = v;
    }
  });
  const img = ctx.createImageData(w, rowH), d = img.data, heat = spHeat();
  for (let x = 0; x < w; x++){                                  // one colour per column, copied down the row
    const v = (x >= L && x < L + pw) ? col[x - L] : NaN, o = x * 4;
    if (v !== v){ d[o] = 5; d[o + 1] = 7; d[o + 2] = 13; }
    else { const k = Math.max(0, Math.min(255, Math.round((v - lo) / (hi - lo) * 255))) * 3; d[o] = heat[k]; d[o + 1] = heat[k + 1]; d[o + 2] = heat[k + 2]; }
    d[o + 3] = 255;
  }
  for (let y = 1; y < rowH; y++) d.copyWithin(y * w * 4, 0, w * 4);
  ctx.putImageData(img, 0, 0);
  spWfHatch();
  SP.wfRows = (SP.wfRows || 0) + 1;
  if (rowCss >= 9 || SP.wfRows % 30 === 1){                       // time stamps in the left margin
    ctx.fillStyle = '#94a3b8'; ctx.font = Math.round(9 * dpr) + 'px system-ui, sans-serif'; ctx.textAlign = 'left';
    ctx.fillText((j.time || '').slice(0, 5), Math.round(3 * dpr), Math.min(rowH, Math.round(9 * dpr)));
  }
}
// FM entries are named by their channel (one decimal); a carrier the IQ check measured off the channels, and
// anything outside the FM band, by its own frequency.
function pkMhz(m){ return (m.kind === 'signal' || m.checked === 'off_channel') ? m.mhz.toFixed(3) : m.mhz.toFixed(1); }
// What a Decode or the Saved Signals list says about a signal: 'ok' (a decoder identified it: green), 'check' (an
// identification that looks like noise: red, investigate it first) or null.
function pkDec(m){
  const d = (SP.decNow || []).find(x => Math.abs(x.mhz - m.mhz) <= 0.03);
  return d ? d.state : (m.decoded || null);
}
function pkRow(m){
  const dec = pkDec(m);
  const r = el('div', 'pk' + (m.flag ? ' flag' : '') + (dec ? ' dec-' + dec : '') + (SP.sel !== null && Math.abs(SP.sel - m.mhz) < 1e-6 ? ' sel' : ''));
  const left = el('div'); left.appendChild(el('div', 'f', pkMhz(m) + ' MHz' + (m.name ? '  ' + m.name : (m.flag ? '  off-plan' : ''))));
  if (m.name) r.classList.add('named');
  left.appendChild(el('div', 'b', m.band));
  if (dec) left.appendChild(el('div', 'dec-note', dec === 'ok' ? 'Decoded: identified by a decoder'
                                                             : 'Doubtful decode, probably noise: investigate first'));
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
  if (j.mode === 'none'){ box.appendChild(el('div', 'muted', 'Nothing was measured in this range. ' + (j.gap_note || ''))); return; }
  const pk = j.peaks || [];
  if (!pk.length){ box.appendChild(el('div', 'muted', 'No signals above the noise floor in this range.')); return; }
  const fm = pk.filter(m => m.kind === 'fm'), rest = pk.filter(m => m.kind !== 'fm');
  // the heading, and what the list's numbers mean: closed until asked for (the list is rebuilt every sweep, so
  // whether it is open is kept in SP and saved)
  const head = el('div', 'sp-head'); head.appendChild(el('div', 'h', 'Signals (' + pk.length + ')'));
  const help = el('button', 'sp-help', 'What do the numbers mean?'); help.setAttribute('aria-expanded', SP.helpOpen ? 'true' : 'false');
  help.onclick = () => { SP.helpOpen = !SP.helpOpen; spSave(); specPeaks(); };
  head.appendChild(help); box.appendChild(head);
  if (SP.helpOpen){
    const t = el('div', 'sp-help-text');
    ['dB above noise: how far a signal rises over that dongle\'s own noise. Bigger is clearer, not closer.',
     'D1 8 \u00b7 D2 16: the same, per dongle.',
     'The plot\'s dBFS: 0 is the strongest level an 8-bit dongle can read. Neither is a calibrated strength (a Signal Hound\'s sweeps are: those show in dBm).',
     'A dotted line marked "gain 37.2 | 29.7": Auto gain measured the two sides at different gains (in dB). The noise floor can step there: at a lower gain a dongle\'s own noise reads higher at the input. With Levels "At input" the signals either side can still be compared.'
    ].forEach(x => t.appendChild(el('div', '', x)));
    box.appendChild(t);
  }
  const fmAll = pk.filter(m => m.kind === 'fm' || m.kind === 'fm_offplan'), unnamed = fmAll.filter(m => !m.name);
  // Stations already checked (on their channel with no name, or off the channels) are only read again on request,
  // except a channel where no name was read more than a day ago (one look is not proof that it sends none).
  const day = Date.now() / 1000 - 86400;
  const fresh = unnamed.filter(m => !m.checked || m.checked === 'rds_weak'
                                    || (['no_name', 'not_heard', 'too_weak'].includes(m.checked) && (m.checked_epoch || 0) < day));
  // Name all reads the channels the list shows, so it is offered only where the sweep can place a station on its
  // channel (a fine step): a coarse sweep lists 99.1 as 99.2, and reading that would call a station "not FM"
  if (unnamed.length && j.fm_plan_checked){
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
  } else fm.forEach(m => box.appendChild(pkRow(m)));
  if (!j.fm_plan_checked && fmAll.length) box.appendChild(el('div', 'note', 'The points of this sweep are too far apart to place FM stations on their channels (each point covers ' + j.step_khz + ' kHz). Use the FM 88-108 preset with Step on Auto to name the stations and to check which signals are off the station plan.'));
  if (j.spikes_kept) box.appendChild(el('div', 'note', 'At this step a hop has too few points to tell a receiver\'s own spike in its middle from a signal, so nothing was removed: entries of about the same strength at even distances are most likely those spikes.'));
}
function spNearestPeak(f){
  const j = SP.data; if (!j) return null; const win = (SP.view.e - SP.view.s) * 0.01;
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
// ---- markers ----
function specMkPeak(){                          // the strongest point in view, on any line
  const v = SP.view; let bf = null, bv = -1e9;
  spLines().forEach(l => { for (let i = spLower(l.freqs, v.s), b = spLower(l.freqs, v.e + 1e-9); i < b; i++) if (l.vals[i] > bv){ bv = l.vals[i]; bf = l.freqs[i]; } });
  if (bf === null){ specStatus('Nothing measured in this view yet'); return; }
  const m = spNearestPeak(bf); specSelect(m ? m.mhz : bf);
}
function specMkNext(dir){                       // the next signal below (-1) or above (+1) the marker
  const j = SP.data, v = SP.view;
  if (!j || !(j.peaks || []).length){ specStatus('No signals found in this view yet'); return; }
  const from = SP.sel === null ? (dir > 0 ? v.s - 1 : v.e + 1) : SP.sel, eps = (v.e - v.s) * 1e-4; let best = null;
  j.peaks.forEach(m => { const d = (m.mhz - from) * dir; if (m.mhz >= v.s && m.mhz <= v.e && d > eps && (best === null || d < (best.mhz - from) * dir)) best = m; });
  if (!best){ specStatus('No other signal ' + (dir > 0 ? 'above' : 'below') + ' the marker in this view'); return; }
  specSelect(best.mhz);
}
function specMkCentre(){
  if (SP.sel === null){ specStatus('Set a marker first: tap the plot or press Peak'); return; }
  const h = (SP.view.e - SP.view.s) / 2; specSetView(SP.sel - h, SP.sel + h);
}
function specMkDelta(){                         // the marker becomes the reference; the readout shows the difference
  if (SP.ref !== null) spDeltaOff();
  else if (SP.sel === null){ specStatus('Set a marker first: tap the plot or press Peak'); return; }
  else { SP.ref = SP.sel; document.getElementById('sp-delta').classList.add('on'); }
  if (SP.sel !== null) document.getElementById('sp-readout').textContent = readoutAt(SP.sel);
  specDraw();
}
function spFreqAtX(clientX, clamp){
  if (!SP.geom) return null;
  const r = document.getElementById('spec-canvas').getBoundingClientRect();
  const g = SP.geom, f = g.s + (clientX - r.left - g.L) / g.pw * (g.e - g.s);
  if (clamp) return Math.min(Math.max(f, g.s), g.e);
  return (f < g.s || f > g.e) ? null : f;
}
function specFreqAt(ev){ return spFreqAtX(ev.touches && ev.touches.length ? ev.touches[0].clientX : ev.clientX); }
function specHover(ev){ if (SP.drag && SP.drag.on) return; const f = specFreqAt(ev); if (f !== null) document.getElementById('sp-readout').textContent = readoutAt(f); }
function specClick(ev){
  if (Date.now() - SP.dragEnd < 300) return;    // the end of a drag is not a click
  let f = specFreqAt(ev); if (f === null) return; const m = spNearestPeak(f); if (m) f = m.mhz; specSelect(f);
}
function specAsk(){
  if (SP.sel === null) return; if (SP.running) specToggle();
  sendQuick('Investigate ' + SP.sel.toFixed(4) + ' MHz on all dongles and tell me in plain English what it is and whether it is normal.');
}
// Decode: the same investigation as the chat (every dongle at once, the decoders that suit the band) for the
// chosen time, run by the GUI itself; progress and the result card appear right here.
SP.decT = 30; SP.job = null;
function specDecodeTime(s){ SP.decT = s; document.querySelectorAll('#sp-dec-t button').forEach(b => b.classList.toggle('on', Number(b.dataset.s) === s)); }
function decBox(){ return document.getElementById('sp-result'); }
// A frequency a card can show or act on: a real number above 0, else null (a missing one is never "0.0000 MHz").
function decMhz(v){ const f = (v === null || v === undefined || v === '') ? NaN : Number(v); return isFinite(f) && f > 0 ? f : null; }
// While a Decode is being started or runs, the panel's buttons and the card's are off: a capture or a second start
// would wipe its progress or be refused for the dongles it holds.
function decButtons(off){ document.querySelectorAll('#sp-sel .btns button, #sp-result button:not(.dec-stop)').forEach(b => { b.disabled = off; }); }
// Is any of this on the screen now (the tab open, not scrolled away)?
function decSeen(node){
  const r = node.getBoundingClientRect(), v = document.getElementById('view-spectrum').getBoundingClientRect();
  return r.width > 0 && r.top < Math.min(innerHeight, v.bottom) && r.bottom > Math.max(0, v.top);
}
// One press is one start. What is on screen stays until the server has taken the job (a refusal goes under it), and
// the result box keeps its height for the whole run, so nothing below slides up under the finger. secs: a time for
// this one press (a card's step); the "Decode for" setting stays as it was left.
async function specDecode(secs){
  if (SP.sel === null || SP.decBusy || SP.job) return; if (SP.running) specToggle();
  const f = SP.sel, box = decBox(), t = Number(secs) > 0 ? Number(secs) : SP.decT;
  SP.decBusy = true; decButtons(true); box.style.minHeight = box.offsetHeight + 'px';
  let j; try { j = await post('/api/decode', {freq_mhz: f, seconds: t}); }
  catch (e){
    box.style.minHeight = ''; decButtons(false);
    box.querySelectorAll('.infobox, .warnbox').forEach(x => x.remove());
    const m = el('div', 'warnbox', 'Could not start: ' + e.message); box.appendChild(m); m.scrollIntoView({block: 'nearest'});
    return;
  } finally { SP.decBusy = false; }
  SP.job = j.job; SP.jobF = f; SP.stopErr = null;
  decProgress({freq_mhz: f, seconds: t, elapsed: 0, expected: t, progress: 'Starting...'});
  box.firstChild.scrollIntoView({block: 'nearest'});       // the press may have been made with the box below the screen
  specDecodePoll();
}
// The progress card of the running Decode, with its own Stop. It is built once per job and then only updated: a
// card rebuilt every second would swallow a press that spans a rebuild. Stop sits on the card's first line, where no
// result card has a button, so a second tap on a card's step that started this Decode cannot land on it.
function decProgress(j){
  const box = decBox(), job = String(SP.job || '');
  let c = box.querySelector('.dec-card.dec-prog');
  if (!c || c.dataset.job !== job){
    box.innerHTML = '';
    c = el('div', 'dec-card dec-prog'); c.dataset.job = job;
    const top = el('div', 'dec-ptop'); top.appendChild(el('div', 'dec-h'));
    const stop = el('button', 'dec-stop', 'Stop'); stop.title = 'Stop this Decode only: nothing is saved, captures go on.';
    stop.onclick = decStop; top.appendChild(stop); c.appendChild(top);
    const bar = el('div', 'bar'); bar.appendChild(el('i')); c.appendChild(bar);
    c.appendChild(el('div', 'act-sub dec-pmsg'));
    box.appendChild(c);
  }
  const stopping = !!(j.stopping || (SP.stopping && SP.stopping === SP.job));
  // why a Stop did not work stays readable for a while: the next polls would write the progress over it at once
  const se = SP.stopErr && SP.stopErr.job === SP.job && Date.now() - SP.stopErr.at < SP_STOP_ERR_MS ? SP.stopErr.msg : '';
  c.querySelector('.dec-h').textContent = 'Decoding ' + Number(j.freq_mhz).toFixed(4) + ' MHz for ' + j.seconds + ' s on every dongle...';
  c.querySelector('.bar i').style.width = Math.min(100, Math.round(100 * j.elapsed / Math.max(1, j.expected))) + '%';
  c.querySelector('.dec-pmsg').textContent = stopping ? 'Stopping: the dongles are let go in a moment.' : (se || j.progress || '');
  decButtons(true);
  const b = c.querySelector('.dec-stop'); b.disabled = stopping || !SP.job; b.textContent = stopping ? 'Stopping...' : 'Stop';
}
// Stop: POST /api/decode/<job>/stop for this one job (Reset Scanner still stops everything). The poll then brings
// the end: "stopped", or the result when the job had just finished.
const SP_STOP_ERR_MS = 8000;
async function decStop(){
  const job = SP.job; if (!job || SP.stopping === job) return;
  SP.stopping = job; SP.stopErr = null;
  const b = document.querySelector('#sp-result .dec-stop'); if (b){ b.disabled = true; b.textContent = 'Stopping...'; }
  try { await post('/api/decode/' + encodeURIComponent(job) + '/stop', {}); }
  catch (e){
    if (SP.job !== job || e.status === 409) return;          // it ended meanwhile: its own end is on the way
    SP.stopping = null;
    SP.stopErr = {job: job, msg: 'Could not stop it: ' + e.message, at: Date.now()};
    const m = document.querySelector('#sp-result .dec-pmsg'); if (m) m.textContent = SP.stopErr.msg;
    if (b && b.isConnected){ b.disabled = false; b.textContent = 'Stop'; }
    if (e.status === 404) specDecodePoll();                  // no such job? the poll asks at once, and ends the card if so
  }
}
// A Decode the server no longer knows (404: the GUI restarted, and its jobs with it) ends here: its card would
// otherwise wait for an answer that never comes, with the panel's buttons off.
function decGone(job){
  if (SP.job !== job) return;
  const f = SP.jobF; SP.job = null;
  decResult({state: 'gone', freq_mhz: f});
}
function decResult(j){
  const box = decBox(), at = decSeen(document.getElementById('sp-sel'));     // still at the panel: the result is brought into view
  box.innerHTML = ''; box.style.minHeight = ''; decButtons(false); SP.stopping = null; SP.stopErr = null;
  const show = node => { box.appendChild(node); if (at) node.scrollIntoView({block: 'nearest'}); };
  if (j.state === 'error'){ show(el('div', 'warnbox', 'Decode failed: ' + j.error)); return; }
  if (j.state === 'gone'){
    const f = decMhz(j.freq_mhz);
    show(el('div', 'warnbox', 'The Decode' + (f ? ' of ' + f.toFixed(4) + ' MHz' : '') + ' is no longer running: the server does not know it' +
                              ' any more (the GUI was restarted). If it had finished first, its result is in the Signals list.'));
    return;
  }
  if (j.state === 'stopped'){
    const f = decMhz(j.freq_mhz);
    show(el('div', 'infobox', 'Decode' + (f ? ' of ' + f.toFixed(4) + ' MHz' : '') + ' stopped. Nothing was saved, and the dongles are free.'));
    return;
  }
  const k = j.card && typeof j.card === 'object' ? j.card : {}; const ev = k.evidence || 'none';
  const doubt = (k.doubtful || []).length > 0;
  // a card in a form this page does not expect is drawn as the plain card, never as an empty panel
  let card; try { card = k.report_v ? decReport(k, j) : decPlain(k, j); }
  catch (e){
    try { card = decPlain(k, j); }
    catch (e2){ card = el('div', 'warnbox', 'The result arrived, but this page could not draw it. If it was saved, it is in the Signals list.'); }
  }
  show(card);
  const state = doubt ? 'check' : (ev === 'confirmed_decode' ? 'ok' : null);        // green / red on the plot and in the list now
  const mhz = decMhz(k.measured_mhz) || decMhz(k.freq_mhz);
  if (mhz){
    SP.decNow = (SP.decNow || []).filter(x => Math.abs(x.mhz - mhz) > 0.03);
    if (state) SP.decNow.unshift({mhz: mhz, state: state});
  }
  specDraw(); specPeaks();
  if (k.station_name) specMarkNamed({[Number(k.station_channel).toFixed(1)]: {name: k.station_name}});
  if (typeof loadSignals === 'function') loadSignals();
}
// The card of a result that has no signal report: the verdict, a line per dongle, three equal buttons.
function decPlain(k, j){
  const ev = k.evidence || 'none', doubt = (k.doubtful || []).length > 0;
  const c = el('div', 'dec-card ' + (doubt ? 'doubtful' : ev));
  const top = el('div', 'dec-top');
  top.appendChild(el('span', 'tag ' + (doubt ? 'doubtful' : ev), doubt ? 'CHECK FIRST' : (ev === 'confirmed_decode' ? 'IDENTIFIED' : (ev === 'signal_present' ? 'NOT IDENTIFIED' : 'NOTHING HEARD'))));
  const fm = decMhz(k.freq_mhz), f = decMhz(k.measured_mhz) || fm;
  top.appendChild(el('span', 'act-sub', (fm ? fm.toFixed(4) + ' MHz' : 'frequency not given') + ' | ' + (k.listened_seconds || j.seconds) + ' s | ' + (k.band || '')));
  c.appendChild(top);
  const v = el('div', 'dec-v'); renderText(v, String(k.verdict || '')); c.appendChild(v);
  (Array.isArray(k.dongles) ? k.dongles : []).forEach(d => c.appendChild(el('div', 'act-sub', d)));
  if (k.antenna_note) c.appendChild(el('div', 'act-sub', k.antenna_note));
  if (k.saved) c.appendChild(el('div', 'act-sub', 'Saved to the Signals list.'));
  const b = el('div', 'act-btns');
  const add = (label, fn) => { const x = el('button', '', label); x.onclick = fn; b.appendChild(x); };
  if (ev !== 'confirmed_decode') add('Capture until decoded', () => specCapture(f));
  add('Ask AI about it', () => decAsk(k));
  if (f) add('Locate', () => mapLocateFreq(f));
  c.appendChild(b);
  return c;
}
// Asks the AI about the result on screen (the panel's own "Ask AI" starts a new investigation instead).
function decAsk(k){
  const f = decMhz(k.freq_mhz);
  sendQuick('Explain the last investigation' + (f ? ' of ' + f.toFixed(4) + ' MHz' : '') + ' in plain English: what is it and is it normal?');
}
// The card of a result with a signal report (card.report_v), top to bottom in the order a phone is read: what it is
// and how sure, the record of this frequency, a bar per dongle, the ONE next step as the only highlighted button,
// and everything else closed under Details. Every key of the report may be missing: the card then says less, never
// something else. All of it is set as text (names heard over the air reach these fields).
const DEC_TAG = {doubtful: 'CHECK FIRST', confirmed_decode: 'IDENTIFIED', signal_present: 'NOT IDENTIFIED', none: 'NOTHING HEARD'};
const DEC_SURE = {high: 'High confidence', medium: 'Medium confidence', low: 'Low confidence', confirmed: 'Confirmed by a decoder'};
// A number stays with its unit and a dongle with its number where a line breaks ("33 s", "0.2 kHz", "Dongle 2").
function decNb(t){ return String(t).replace(/(\d) (?=(?:[kMG]?Hz|dB|ms|s|min|ppm|%)(?![A-Za-z]))|(Dongle) (?=\d)/g, '$1$2\u00a0'); }
function decReport(k, j){
  const ev = k.evidence || 'none', was = (k.doubtful || []).length ? 'doubtful' : ev, kind = k.tag_kind || was;
  const obj = x => x && typeof x === 'object' && !Array.isArray(x) ? x : {};
  const car = obj(k.carrier), step = obj(k.step), bars = (Array.isArray(k.bars) ? k.bars : []).filter(b => b && typeof b === 'object');
  const c = el('div', 'dec-card rep ' + kind);
  const line = t => { if (t) c.appendChild(el('div', 'dec-sub', decNb(t))); };
  const top = el('div', 'dec-top');
  top.appendChild(el('span', 'tag ' + kind, k.tag || DEC_TAG[was] || DEC_TAG.signal_present));
  if (k.history_tag) top.appendChild(el('span', 'tag hist ' + (k.history_kind || 'seen'), k.history_tag));
  const secs = k.listened_seconds || j.seconds, fm = decMhz(k.freq_mhz);
  top.appendChild(el('span', 'dec-meta', [car.text || (fm ? fm.toFixed(4) + ' MHz' : ''),
                                           secs ? secs + ' s' : ''].filter(Boolean).join(' · ')));
  c.appendChild(top);
  c.appendChild(el('div', 'dec-head', decNb(k.headline || k.verdict || '')));
  // how sure: the confidence in its own words first, then what it rests on
  const sure = decNb(k.sure || ''), word = DEC_SURE[k.confidence], graded = word && k.confidence !== 'confirmed';
  if (word && sure.indexOf(word) === 0){
    const s = el('div', 'dec-sub'); s.appendChild(el('b', '', word)); s.appendChild(document.createTextNode(sure.slice(word.length))); c.appendChild(s);
  } else if (graded){
    const s = el('div', 'dec-sub'); s.appendChild(el('b', '', word + '.')); s.appendChild(document.createTextNode(sure ? ' ' + sure : '')); c.appendChild(s);
  } else line(sure);
  line(car.note); line(k.history_line);
  if (bars.length){ c.appendChild(decBars(bars)); if (k.bars_note) c.appendChild(el('div', 'dec-cap', decNb(k.bars_note))); }
  const run = decRun(step, k);
  const next = el('div', 'dec-next');
  if (step.reason){
    const r = el('div', 'dec-why'); if (run) r.appendChild(el('b', '', 'Next: '));
    r.appendChild(document.createTextNode(decNb(step.reason))); next.appendChild(r);
  }
  // The step's button, and beside it (below it when they do not fit) the card's one quiet link: the AI's words on
  // this result. Capturing and locating are the panel's buttons above, or the step when the engine picks them: a
  // card that says there is nothing to do does not offer them again.
  const acts = el('div', 'dec-acts');
  if (run){ const b = el('button', 'btn', step.label); b.onclick = run; acts.appendChild(b); }
  const ask = el('button', 'dec-link', 'Explain this result'); ask.onclick = () => decAsk(k); acts.appendChild(ask);
  next.appendChild(acts); c.appendChild(next);
  const more = decDetails(k.details); if (more) c.appendChild(more);
  return c;
}
// One row per dongle: its name, a bar in that dongle's colour on the plot, the level over its own noise, and the
// word "best" on the strongest. The bars share one scale (the engine gives the width); a dongle that did not hear
// it has an empty track and one that was not used has none. Where no row has a level (aircraft are counted, not
// measured) the rows are text only.
function decBars(bars){
  const g = el('div', 'dec-bars'), ds = SP.meta && Array.isArray(SP.meta.dongles) ? SP.meta.dongles : [];
  const levels = bars.some(b => b.state === 'heard' && b.pct);
  bars.forEach(b => {
    const d = ds.find(x => x.serial === b.serial) || ds.find(x => x.name === b.dongle);
    const heard = b.state === 'heard' || b.state === 'count';
    g.appendChild(el('span', 'nm', b.dongle || ''));
    if (b.state === 'count' || !levels) g.appendChild(el('span', 'wide' + (heard ? '' : ' off'), b.text || ''));
    else {
      const t = el('span', b.state === 'not_used' ? '' : 'trk');
      if (b.state === 'heard' && b.pct){
        const i = el('i'); i.style.width = Math.min(100, Math.max(0, Number(b.pct))) + '%'; i.style.background = d ? spColor(d.serial) : '#94a3b8';
        t.appendChild(i);
      }
      g.appendChild(t); g.appendChild(el('span', 'val' + (heard ? '' : ' off'), decNb(b.text || '')));
    }
    g.appendChild(el('span', 'best', b.best ? 'best' : ''));
  });
  return g;
}
// What the card's one button does, or null for no button (nothing to do, an action this page does not know, or a
// radio step on a card that names no frequency). A decode step's time is for that one press.
function decRun(step, k){
  const f = decMhz(step.mhz) || decMhz(k.measured_mhz) || decMhz(k.freq_mhz);
  const runs = {
    decode: f && (() => { specSelect(f); specDecode(Number(step.seconds)); }),
    capture: f && (() => specCapture(f, true, step.minutes)),
    locate: f && (() => mapLocateFreq(f)),
    check: () => sendQuick('/check'),
    activity: () => switchTab('activity'),
    devices: () => switchTab('devices')};
  return (step.label && Object.prototype.hasOwnProperty.call(runs, step.action) && runs[step.action]) || null;
}
// The rest of the report, closed until asked for: a small title per section, its lines, and where the engine gives
// one the on / off record of the listen.
function decDetails(list){
  const secs = (Array.isArray(list) ? list : []).filter(s => s && Array.isArray(s.lines) && s.lines.length);
  if (!secs.length) return null;
  const d = el('details', 'dec-more'); d.appendChild(el('summary', '', 'Details: what it did, checks, history'));
  secs.forEach(s => {
    d.appendChild(el('div', 'dec-sec', s.title || ''));
    s.lines.forEach(t => d.appendChild(el('div', 'dec-ln', decNb(t))));
    const st = decStrip(s.strip); if (st) d.appendChild(st);
  });
  return d;
}
// The on / off record as a row of marks, one per slot of the listen: tall = on, half = on for part of it, a low grey
// mark = off, a gap = nobody looked then. Never finer than the slots the engine gives.
function decStrip(st){
  const h = st && Array.isArray(st.slots) ? st.slots : [], seen = v => v !== null && v !== undefined;
  if (!h.some(seen)) return null;
  const NS = 'http://www.w3.org/2000/svg', W = 10, H = 14;
  const box = el('div', 'dec-strip'), svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 ' + (h.length * W) + ' ' + H); svg.setAttribute('preserveAspectRatio', 'none');
  h.forEach((v, i) => {
    if (!seen(v)) return;
    const rc = document.createElementNS(NS, 'rect'), ht = v >= 1 ? H : (v > 0 ? H / 2 : 3);
    rc.setAttribute('x', i * W + 0.5); rc.setAttribute('width', W - 1); rc.setAttribute('y', H - ht); rc.setAttribute('height', ht);
    rc.setAttribute('class', v > 0 ? 'on' : 'off'); svg.appendChild(rc);
  });
  box.appendChild(svg);
  box.appendChild(el('div', 'dec-cap', decNb((st.label ? st.label + ' ' : '') + 'Tall: on. Half: on part of the time. Low: off.')));
  return box;
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
      // not heard / too weak: remembered with the time, offered to Name all again after a day
      else if (x.state === 'not_heard' || x.state === 'too_weak'){ m.kind = 'fm'; if (!x.band) m.band = 'Checked: ' + (x.state === 'too_weak' ? 'too weak here to read' : 'not heard') + ' in the last check'; }
    });
  });
  specDraw(); specPeaks();
}
// Names read since the last sweep (a capture, a Decode, the chat, Name all in another browser) show up in the list
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
// One poll chain per Decode: a poll asked for out of turn (after a Stop the server did not know) takes the place of
// the one waiting. A connection problem is asked again in 2 s; a JSON 404 means the server no longer knows the job.
async function specDecodePoll(){
  clearTimeout(SP.pollT); SP.pollT = null;
  const job = SP.job; if (!job) return;
  const n = SP.pollN = (SP.pollN || 0) + 1, again = ms => { if (SP.job === job && n === SP.pollN) SP.pollT = setTimeout(specDecodePoll, ms); };
  let r; try { r = await fetch('/api/decode/' + encodeURIComponent(job)); } catch (e){ again(2000); return; }
  if (toLogin(r)) return;
  const j = await r.json().catch(() => null);
  if (SP.job !== job || n !== SP.pollN) return;
  if (r.status === 404 && j && typeof j === 'object'){ decGone(job); return; }
  if (!r.ok || !j || typeof j !== 'object'){ again(2000); return; }
  if (j.state === 'running'){ decProgress(j); again(1000); return; }
  SP.job = null; decResult(j);
}
// A capture's message takes the place of an old result, or (keep) goes below the card whose button started it.
async function specCapture(f, keep, minutes){
  if (f === undefined || f === null) f = SP.sel; if (f === null || SP.capBusy) return; if (SP.running) specToggle();
  SP.capBusy = true;                // one press is one start: the answer can take seconds (the dongles are compared first)
  const box = decBox();
  const say = (cls, text) => {
    if (keep) box.querySelectorAll('.infobox, .warnbox').forEach(x => x.remove()); else box.innerHTML = '';
    const m = el('div', cls, text); box.appendChild(m);
    if (keep && m.scrollIntoView) m.scrollIntoView({block: 'nearest'});     // below a tall card it would be off the screen
  };
  say('infobox', 'Starting a capture on ' + Number(f).toFixed(4) + ' MHz (comparing the dongles first)...');
  const ask = {freq_mhz: Number(f)}; if (Number(minutes) > 0) ask.minutes = Number(minutes);
  let r; try { r = await post('/api/capture/start', ask); }
  catch (e){ say('warnbox', 'Capture not started: ' + e.message); return; }
  finally { SP.capBusy = false; }
  say('infobox', r.message || 'Capture started.');
}
// ---- pinch: two fingers on the plot zoom about their midpoint, within the wheel's limits (the smallest span, the
// whole 0-6000 MHz). The frequency under the fingers' midpoint stays under it, so moving both fingers also pans.
// While the fingers move only the picture follows; lifting them is ONE view change (one Back entry, one sweep).
// The canvas is touch-action: pan-y, so one finger still scrolls the page and taps a marker.
const SP_PINCH_MIN_PX = 30;                     // fingers closer than this count as this far apart (no jump)
// Only the fingers on the plot count (targetTouches: the canvas has no children): a thumb resting elsewhere on the
// screen must not turn a tap into a pinch or a one-finger slide into a zoom.
function spOnPlot(ev){ return ev.targetTouches || ev.touches || []; }
function spPinchStart(ev){
  const t = spOnPlot(ev);
  if (t.length !== 2 || !SP.geom) return;
  if (ev.cancelable) ev.preventDefault();
  const a = t[0].clientX, b = t[1].clientX, g = SP.geom;
  const left = document.getElementById('spec-canvas').getBoundingClientRect().left;
  const k = Math.min(Math.max(((a + b) / 2 - left - g.L) / g.pw, 0), 1);
  SP.pinch = {v0: {s: SP.view.s, e: SP.view.e}, d0: Math.max(SP_PINCH_MIN_PX, Math.abs(a - b)),
              f0: SP.view.s + k * (SP.view.e - SP.view.s), left: left, last: null};
}
function spPinchMove(ev){
  const P = SP.pinch, t = spOnPlot(ev); if (!P || t.length !== 2 || !SP.geom) return;
  if (ev.cancelable) ev.preventDefault();
  const a = t[0].clientX, b = t[1].clientX, g = SP.geom, L = spLim();
  const span = Math.min(Math.max((P.v0.e - P.v0.s) * P.d0 / Math.max(SP_PINCH_MIN_PX, Math.abs(a - b)), L.min), L.hi - L.lo);
  const k = Math.min(Math.max(((a + b) / 2 - P.left - g.L) / g.pw, 0), 1);
  const v = spFitView(P.f0 - k * span, P.f0 - k * span + span);
  P.last = v; SP.view = {s: v[0], e: v[1]};
  spFields(); spDrawSoon();
}
function spPinchEnd(ev){
  const P = SP.pinch; if (!P || spOnPlot(ev).length >= 2) return;
  SP.pinch = null; SP.dragEnd = Date.now();     // (no tap-to-mark from the finger lifted last)
  if (ev.cancelable) ev.preventDefault();
  SP.view = {s: P.v0.s, e: P.v0.e};             // Back returns to where the gesture began
  if (P.last) specSetView(P.last[0], P.last[1]); else { spFields(); specDraw(); }
}
function specWatch(){ specCapture(); }
function specLocate(){ if (SP.sel === null) return; if (SP.running) specToggle(); mapLocateFreq(SP.sel); }
(function(){
  const c = document.getElementById('spec-canvas');
  c.addEventListener('mousemove', specHover); c.addEventListener('click', specClick);
  c.addEventListener('mouseleave', () => { document.getElementById('sp-readout').textContent = SP.sel !== null ? readoutAt(SP.sel) : SP_HINT; });
  // With a mouse: the wheel zooms about the pointer, and dragging sideways zooms into the dragged range. A finger
  // keeps tap-to-mark and scrolls the page (touch sends no wheel, and its taps do not move far enough to be a drag);
  // two fingers zoom about their midpoint (spPinch*), and only they are taken from the browser.
  c.addEventListener('touchstart', spPinchStart, {passive: false});
  c.addEventListener('touchmove', spPinchMove, {passive: false});
  c.addEventListener('touchend', spPinchEnd, {passive: false});
  c.addEventListener('touchcancel', spPinchEnd, {passive: false});
  c.addEventListener('wheel', ev => {
    const f = spFreqAtX(ev.clientX, true); if (f === null) return;
    ev.preventDefault();
    const v = SP.view, k = ev.deltaY < 0 ? 0.8 : 1.25, L = spLim(), span = v.e - v.s;
    // at the smallest span there is no zooming in, at the full span none out (and the view must not slide instead)
    if ((ev.deltaY < 0 && span <= L.min + 1e-9) || (ev.deltaY >= 0 && span >= L.hi - L.lo - 1e-9)) return;
    specSetView(f - (f - v.s) * k, f + (v.e - f) * k);
  }, {passive: false});
  c.addEventListener('mousedown', ev => { if (ev.button !== 0 || !SP.geom) return; SP.drag = {x0: ev.clientX, x1: ev.clientX, on: false}; ev.preventDefault(); });
  window.addEventListener('mousemove', ev => {
    const d = SP.drag; if (!d) return;
    d.x1 = ev.clientX; if (!d.on && Math.abs(d.x1 - d.x0) >= 6){ d.on = true; c.classList.add('drag'); }
    if (d.on) spDrawSoon();
  });
  window.addEventListener('mouseup', () => {
    const d = SP.drag; if (!d) return;
    SP.drag = null; c.classList.remove('drag');
    if (!d.on) return;
    SP.dragEnd = Date.now();
    const a = spFreqAtX(Math.min(d.x0, d.x1), true), b = spFreqAtX(Math.max(d.x0, d.x1), true);
    if (a !== null && b !== null && b - a >= spLim().min) specSetView(a, b); else specDraw();
  });
  window.addEventListener('resize', () => { if (activeTab() === 'spectrum'){ specDraw(); spWfHatch(); } });
})();
