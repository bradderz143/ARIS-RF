// Devices tab (v4.8.8): every device a decoder named, one row each, a vehicle's tyre sensors grouped as one row
// (the engine's device history, kept past the log's cap). Polled every 6 s while the tab is open; the list is only
// downloaded again when it changed (?have=).
// v4.9.2: a device heard on 3 or more days, or marked by the user, is "known"; the All / New only / Known only
// buttons and the Known / Watch buttons on each row (POST /api/devices/mark).
// v4.9.7: the All / Vehicles / Sensors / Remotes buttons split the list (a row's `group`); Vehicles has three sections:
// v4.11.0: a Bluetooth button shows what an Ubertooth heard (api/bluetooth): devices with a stable address, the number
// with rotating addresses (phones: counted, not listed), tracker-type advertisements; a baseline like the Wi-Fi one.
// v4.10.0: a Wi-Fi button shows the networks a passive scan heard (api/wifi), with a baseline: new ones that are strong,
// hidden or a weaker copy of a known name come first. Only what access points announce; no client devices.
// v4.9.11: in the Sensors view a sensor heard once and not again (`noise`) goes in its own section, 'likely noise'.
// v4.9.10: tyre sensors of one make heard within 5 s are one row even on one occasion ('probably one car').
// cars (a set of sensors heard together), cars that came back (one sensor on 2+ days), single sensors (most likely cars passing).
let DEV = {tag: '', rows: [], totals: null, rule: '', note: null, show: 'all', group: 'all', more: {}};
const DEV_DRAW = 60;   // v4.9.9: rows drawn at once per list or section (the history holds up to 1500 devices; a phone draws 60 and asks)
function devDraw(box, list, id, rowFn){
  const n = DEV.more[id] || DEV_DRAW;
  list.slice(0, n).forEach(r => box.appendChild((rowFn || devRow)(r)));
  if (list.length > n){
    const b = el('button', 'btn-ghost', 'Show ' + Math.min(DEV_DRAW, list.length - n) + ' more (' + (list.length - n) + ' not drawn)');
    b.onclick = () => { DEV.more[id] = n + DEV_DRAW; renderDevices(); }; box.appendChild(b);
  }
}
async function loadDevices(force){
  let r; try { r = await apiGet('/api/devices?have=' + encodeURIComponent(force ? '' : DEV.tag)); } catch (e) { return; }
  if (r.unchanged) return;
  DEV.tag = r.tag || ''; DEV.rows = r.rows || []; DEV.totals = r; DEV.rule = r.rule || '';
  renderDevices();
}
function devText(r){
  return [r.title, r.model, r.id, r.type, r.freq_text, (r.dongles || []).join(' '), (r.ids || []).join(' '), r.reading_words]
    .filter(x => x !== undefined && x !== null).join(' ').toLowerCase();
}
function devRow(r){
  const d = el('div', 'act-row' + (r.kind === 'set' ? ' dev-set' : ''));
  const top = el('div', 'act-top'); top.appendChild(el('strong', '', r.title));
  if (r.kind === 'set') top.appendChild(el('span', 'lvl VEHICLE', r.once ? 'PROBABLY ONE CAR' : r.short === 'tyre sensor' ? 'VEHICLE' : 'SET'));
  else if (r.vehicle === 'again') top.appendChild(el('span', 'lvl VEHICLE', 'BACK AGAIN'));
  else if (r.vehicle === 'passing') top.appendChild(el('span', 'lvl KNOWN', 'PASSING?'));
  if (r.known) top.appendChild(el('span', 'lvl KNOWN', r.known_by === 'you' ? 'KNOWN (you)' : 'KNOWN'));
  else if (r.mark === 'no') top.appendChild(el('span', 'lvl KNOWN', 'WATCHED'));
  d.appendChild(top);
  d.appendChild(el('div', 'act-sub', r.seen_line));
  const kb = el('div', 'dev-known');
  const b1 = el('button', 'btn-ghost', r.known ? 'Not known' : 'Mark known'); b1.onclick = () => devMark(r.key, !r.known); kb.appendChild(b1);
  if (r.mark) { const b2 = el('button', 'btn-ghost', 'Back to the rule'); b2.onclick = () => devMark(r.key, null); kb.appendChild(b2); }
  d.appendChild(kb);
  if (r.reading_words) d.appendChild(el('div', 'dev-reading', (r.kind === 'set' ? 'Last reading (' + r.members[0].id + '): ' : 'Last reading: ') + r.reading_words));
  if (r.kind === 'set'){
    const m = el('div', 'dev-members');
    r.members.forEach(x => m.appendChild(el('div', 'act-sub', x.id + ': ' + x.seen_line.replace(/ \| .*$/, '') + (x.reading_words ? ' | ' + x.reading_words : ''))));
    d.appendChild(m);
  }
  return d;
}
let WIFI = {tag: '', t: null};
async function loadWifi(force){
  let r; try { r = await apiGet('/api/wifi?have=' + encodeURIComponent(force ? '' : WIFI.tag)); } catch (e) { return; }
  if (r.unchanged){ if (WIFI.t){ WIFI.t.helper = r.helper; WIFI.t.scan_age_s = r.scan_age_s; } }
  else { WIFI.tag = r.tag || ''; WIFI.t = r; }
  if (DEV.group === 'wifi') renderDevices(); else devCounts();
}
function wifiText(r){
  const w = r.wps || {};
  return [r.ssid, (r.bssids || []).join(' '), r.chan, (r.bands || []).join(' '), r.security, r.hidden ? 'hidden' : '', r.status, r.maker,
          r.private_address ? 'private address hotspot' : '', w.manufacturer, w.model, w.device_name, r.country].join(' ').toLowerCase();
}
function wifiRow(r){
  const d = el('div', 'act-row' + (r.status === 'look' ? ' dev-set' : ''));
  const top = el('div', 'act-top'); top.appendChild(el('strong', '', r.ssid || '(hidden name)'));
  if (r.status === 'look') top.appendChild(el('span', 'lvl MEDIUM', 'WORTH A LOOK'));
  else if (r.status === 'new') top.appendChild(el('span', 'lvl VEHICLE', 'NEW'));
  else if (r.status === 'baseline') top.appendChild(el('span', 'lvl KNOWN', 'NORMAL HERE'));
  else if (r.status === 'mine') top.appendChild(el('span', 'lvl KNOWN', 'MINE'));
  else if (r.status === 'known') top.appendChild(el('span', 'lvl KNOWN', 'KNOWN'));
  if (r.mark === 'no') top.appendChild(el('span', 'lvl KNOWN', 'WATCHED'));
  d.appendChild(top);
  const w = r.wps || {}, who = [];
  if (r.maker) who.push(r.maker);
  else if (r.private_address) who.push('private address (no maker): an extra network, or a phone hotspot');
  const dev = [w.manufacturer, w.model].filter(x => x).join(' ');
  if (dev && !(r.maker && dev.toLowerCase().includes(r.maker.toLowerCase()) && !w.model)) who.push(dev + (w.device_name && w.device_name !== w.model ? ' "' + w.device_name + '"' : ''));
  if (who.length) d.appendChild(el('div', 'act-sub', who.join(' | ')));
  const ms = r.members || [];
  const radios = ms.map(m => (m.band || '').replace(' GHz', '') + ' GHz ch ' + (m.chan || '?') + (m.width ? ' (' + m.width + ' MHz)' : '') +
                           (m.signal_dbm === null || m.signal_dbm === undefined ? '' : ' ' + Math.round(m.signal_dbm) + ' dBm')).join(' + ');
  d.appendChild(el('div', 'act-sub', (ms.length > 1 ? (r.bands || []).join(' + ').replace(/ GHz/g, '') + ' GHz: ' : '') + radios + ' | ' + (r.security || '') +
                   (r.up_text ? ' | ' + r.up_text : '') + (r.country ? ' | country ' + r.country : '') + ' | ' + (r.heard_now ? 'heard now' : 'not heard for a while')));
  d.appendChild(el('div', 'act-sub', (r.bssids || []).join(' ')));
  const sp = wifiSpark(r); if (sp) d.appendChild(sp);
  (r.reasons || []).forEach(x => d.appendChild(el('div', 'dev-reading', x)));
  (r.notes || []).forEach(x => d.appendChild(el('div', 'act-sub', x)));
  const kb = el('div', 'dev-known');
  [['mine', 'Mine'], ['known', 'Known'], ['no', 'Not known']].forEach(p => {
    if (r.mark === p[0]) return;
    const b = el('button', 'btn-ghost', p[1]); b.onclick = () => wifiMark(r.bssids, p[0]); kb.appendChild(b);
  });
  if (r.mark){ const b = el('button', 'btn-ghost', 'Back to the rule'); b.onclick = () => wifiMark(r.bssids, null); kb.appendChild(b); }
  d.appendChild(kb);
  return d;
}
// v4.13.0: the row's last 24 hours, one bar per quarter hour: its best signal (taller = stronger), a low red
// mark where a scan did not hear it, nothing where there was no scan (the helper was not running).
function wifiSpark(r){
  const h = r.hist || [];
  if (!h.some(v => v !== null)) return null;
  const NS = 'http://www.w3.org/2000/svg', W = 3, H = 22;
  const box = el('div', 'wifi-hist');
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 ' + (h.length * W) + ' ' + H); svg.setAttribute('preserveAspectRatio', 'none');
  svg.setAttribute('class', 'wifi-spark');
  h.forEach((v, i) => {
    if (v === null) return;
    const rc = document.createElementNS(NS, 'rect');
    const ht = v === 0 ? 2 : Math.max(2, Math.min(H, (v + 95) / 65 * H));
    rc.setAttribute('x', i * W); rc.setAttribute('width', W - 0.6); rc.setAttribute('y', H - ht); rc.setAttribute('height', ht);
    rc.setAttribute('class', v === 0 ? 'off' : 'on'); svg.appendChild(rc);
  });
  box.appendChild(svg);
  box.appendChild(el('div', 'act-sub', 'Last 24 h: heard in ' + (r.heard_slots || 0) + ' of ' + (r.scanned_slots || 0) + ' scanned quarter hours'));
  return box;
}
async function wifiMark(bssids, mark){
  let msg; try { const r = await post('/api/wifi/mark', {bssids: bssids, mark: mark}); msg = r.message || 'Done.'; }
  catch (e) { msg = 'Could not mark it: ' + (e.message || e); }
  DEV.note = {text: msg, t: Date.now()}; loadWifi(true);
}
function renderWifi(){
  const box = document.getElementById('devices-box'); box.innerHTML = '';
  const t = WIFI.t || {};
  const q = (document.getElementById('dev-filter').value || '').trim().toLowerCase();
  const rows = (t.rows || []).filter(r => !q || wifiText(r).includes(q));
  const c = t.counts || {};
  document.getElementById('dev-totals').textContent = !WIFI.t ? '' : plural(t.networks || 0, 'network') + (t.bssids > t.networks ? ' (' + t.bssids + ' addresses)' : '') + ' heard' +
    (t.baseline_n === null || t.baseline_n === undefined ? ', no baseline yet' : ', ' + (c.look || 0) + ' worth a look, ' + (c.new || 0) + ' new since the baseline') +
    (q ? ' | ' + rows.length + ' shown' : '');
  if (DEV.note && Date.now() - DEV.note.t < 120000) box.appendChild(el('div', 'infobox', DEV.note.text));
  if (t.helper === 'missing') box.appendChild(el('div', 'infobox', 'No Wi-Fi scan yet: the scan helper is not installed or not running on the station (it is set up with deploy/aris-wifi.sh). ARIS never scans or transmits by itself.'));
  else if (t.helper === 'stale') box.appendChild(el('div', 'infobox', 'The last Wi-Fi scan was ' + Math.round((t.scan_age_s || 0) / 60) + ' minutes ago: the scan helper seems to have stopped.'));
  const bar = el('div', 'dev-known');
  const b1 = el('button', 'btn-ghost', t.baseline_n === null || t.baseline_n === undefined ? 'Record Wi-Fi baseline' : 'Record the baseline again'); b1.onclick = wifiBaseline; bar.appendChild(b1);
  const b2 = el('button', 'btn-ghost danger', 'Forget the Wi-Fi list'); b2.onclick = wifiForget; bar.appendChild(b2);
  box.appendChild(bar);
  box.appendChild(el('div', 'act-sub', 'Only what each network announces (name, channel, signal, security). Devices connected to them are not listed or recorded. A baseline is "what is here normally": record it when you know your space is as it should be.'));
  if (!rows.length) box.appendChild(el('div', 'act-sub', q ? 'Nothing matches "' + q + '".' : 'No networks heard yet.'));
  [['look', 'Worth a look: new, and strong, just came up, hidden or a weaker copy of a known name'], ['new', 'New since the baseline'],
   ['unrated', 'No baseline yet'], ['baseline', 'Normal here (in the baseline)'], ['known', 'Known (marked by you)'],
   ['mine', 'Mine']].forEach(p => {
    const sub = rows.filter(r => r.status === p[0]); if (!sub.length) return;
    box.appendChild(el('div', 'dev-head', p[1] + ' (' + sub.length + ')'));
    devDraw(box, sub, 'wifi-' + p[0], wifiRow);
  });
  document.getElementById('dev-rule').textContent = '';
  devCounts();
}
async function wifiBaseline(){
  if (!confirm('Record the Wi-Fi baseline?\n\nEvery network heard so far becomes "normal here". Do it when you know your space is as it should be.')) return;
  let msg; try { const r = await post('/api/wifi/baseline'); msg = r.message || 'Baseline recorded.'; }
  catch (e) { msg = 'Could not record the baseline: ' + (e.message || e); }
  DEV.note = {text: msg, t: Date.now()}; loadWifi(true);
}
async function wifiForget(){
  if (!confirm('Forget the Wi-Fi list and its baseline?')) return;
  let msg; try { const r = await post('/api/wifi/forget'); msg = r.message || 'Forgotten.'; }
  catch (e) { msg = 'Could not forget the Wi-Fi list: ' + (e.message || e); }
  DEV.note = {text: msg, t: Date.now()}; loadWifi(true);
}
let BT = {tag: '', t: null};
async function loadBt(force){
  let r; try { r = await apiGet('/api/bluetooth?have=' + encodeURIComponent(force ? '' : BT.tag)); } catch (e) { return; }
  if (r.unchanged){ if (BT.t){ BT.t.helper = r.helper; BT.t.scan_age_s = r.scan_age_s; } }
  else { BT.tag = r.tag || ''; BT.t = r; }
  if (DEV.group === 'bt') renderDevices(); else devCounts();
}
const BT_TRACKERS = {find_my: 'Apple Find My network', tile: 'Tile', smarttag: 'Samsung SmartTag'};
function btText(r){ return [r.label, r.company, r.oui, r.atype, r.status, (r.trackers || []).map(k => BT_TRACKERS[k] || k).join(' ')].join(' ').toLowerCase(); }
function btRow(r){
  const d = el('div', 'act-row' + (r.status === 'look' ? ' dev-set' : ''));
  const top = el('div', 'act-top'); top.appendChild(el('strong', '', (r.company && r.company !== 'not assigned' ? r.company : 'Unknown maker') + ' ' + r.label));
  if (r.status === 'look') top.appendChild(el('span', 'lvl MEDIUM', 'WORTH A LOOK'));
  else if (r.status === 'new') top.appendChild(el('span', 'lvl VEHICLE', 'NEW'));
  else if (r.status === 'baseline') top.appendChild(el('span', 'lvl KNOWN', 'NORMAL HERE'));
  d.appendChild(top);
  const sig = r.last_dbm === null || r.last_dbm === undefined ? '' : ' | signal ' + Math.round(r.last_dbm) + ' dBm' +
              (r.max_dbm !== null && r.max_dbm !== undefined && r.max_dbm > r.last_dbm + 3 ? ' (best ' + Math.round(r.max_dbm) + ')' : '');
  d.appendChild(el('div', 'act-sub', (r.atype === 'public' ? 'fixed address' + (r.oui ? ' (maker code ' + r.oui + ')' : '') : 'fixed random address') +
                   ' | heard ' + plural(r.seen || 0, 'time') + sig + ' | ' + (r.heard_now ? 'heard now' : 'not heard for a while')));
  (r.reasons || []).forEach(x => d.appendChild(el('div', 'dev-reading', x)));
  return d;
}
function renderBt(){
  const box = document.getElementById('devices-box'); box.innerHTML = '';
  const t = BT.t || {};
  const q = (document.getElementById('dev-filter').value || '').trim().toLowerCase();
  const rows = (t.rows || []).filter(r => !q || btText(r).includes(q));
  const c = t.counts || {}, rot = t.rotating || {};
  document.getElementById('dev-totals').textContent = !BT.t ? '' : plural(t.devices || 0, 'device') + ' with a fixed address' +
    (t.baseline_n === null || t.baseline_n === undefined ? ', no baseline yet' : ', ' + (c.look || 0) + ' worth a look, ' + (c.new || 0) + ' new since the baseline') +
    (q ? ' | ' + rows.length + ' shown' : '');
  if (DEV.note && Date.now() - DEV.note.t < 120000) box.appendChild(el('div', 'infobox', DEV.note.text));
  if (t.helper === 'missing') box.appendChild(el('div', 'infobox', 'No Bluetooth data yet: the listener is not installed or not running on the station (it is set up with deploy/aris-bt.sh). ARIS never listens or transmits by itself.'));
  else if (t.helper === 'stale') box.appendChild(el('div', 'infobox', 'The last Bluetooth data was ' + Math.round((t.scan_age_s || 0) / 60) + ' minutes ago: the listener seems to have stopped.'));
  const bar = el('div', 'dev-known');
  const b1 = el('button', 'btn-ghost', t.baseline_n === null || t.baseline_n === undefined ? 'Record Bluetooth baseline' : 'Record the baseline again'); b1.onclick = btBaseline; bar.appendChild(b1);
  const b2 = el('button', 'btn-ghost danger', 'Forget the Bluetooth list'); b2.onclick = btForget; bar.appendChild(b2);
  box.appendChild(bar);
  if (BT.t){
    const tr = t.trackers || {}, names = Object.keys(tr).map(k => (BT_TRACKERS[k] || k) + ' ' + tr[k].distinct + ' (strongest ' + Math.round(tr[k].rssi_max) + ' dBm)');
    const comp = Object.keys(rot.by_company || {}).slice(0, 4).map(k => k + ' ' + rot.by_company[k]).join(', ');
    box.appendChild(el('div', 'infobox', 'Heard in the last minute: ' + plural(rot.distinct || 0, 'device') + ' with a rotating address (phones, watches and earbuds change theirs every few minutes, so they are only counted' +
      (comp ? ': ' + comp : '') + ')' + (names.length ? '. Tracker-type advertisements: ' + names.join(', ') + '. Phones and earbuds on those networks advertise the same way: this is not proof of a hidden tag.' : '.')));
  }
  box.appendChild(el('div', 'act-sub', 'Only what devices announce: the maker, the kind of address and the signal. Addresses are stored as a short code, names and contents are never read, and devices that connect or scan are ignored. A baseline is "what is here normally": record it when you know your space is as it should be.'));
  if (!rows.length) box.appendChild(el('div', 'act-sub', q ? 'Nothing matches "' + q + '".' : 'No devices heard yet.'));
  [['look', 'Worth a look: new, and very close or advertising like a tracker'], ['new', 'New since the baseline'],
   ['unrated', 'No baseline yet'], ['baseline', 'Normal here (in the baseline)']].forEach(p => {
    const sub = rows.filter(r => r.status === p[0]); if (!sub.length) return;
    box.appendChild(el('div', 'dev-head', p[1] + ' (' + sub.length + ')'));
    devDraw(box, sub, 'bt-' + p[0], btRow);
  });
  document.getElementById('dev-rule').textContent = '';
  devCounts();
}
async function btBaseline(){
  if (!confirm('Record the Bluetooth baseline?\n\nEvery device with a fixed address heard so far becomes "normal here". Do it when you know your space is as it should be.')) return;
  let msg; try { const r = await post('/api/bluetooth/baseline'); msg = r.message || 'Baseline recorded.'; }
  catch (e) { msg = 'Could not record the baseline: ' + (e.message || e); }
  DEV.note = {text: msg, t: Date.now()}; loadBt(true);
}
async function btForget(){
  if (!confirm('Forget the Bluetooth list and its baseline?')) return;
  let msg; try { const r = await post('/api/bluetooth/forget'); msg = r.message || 'Forgotten.'; }
  catch (e) { msg = 'Could not forget the Bluetooth list: ' + (e.message || e); }
  DEV.note = {text: msg, t: Date.now()}; loadBt(true);
}
function devCounts(){
  const gc = (DEV.totals && DEV.totals.groups) || {}, gl = {all: 'All', vehicle: 'Vehicles', sensor: 'Sensors', remote: 'Remotes'};
  document.querySelectorAll('#dev-group button').forEach(b => { const n = b.dataset.v === 'all' ? DEV.rows.length : (gc[b.dataset.v] || 0);
                                                                b.textContent = gl[b.dataset.v] + ' (' + n + ')'; });
  const w = document.querySelector('#dev-wifi-seg button[data-v="wifi"]');
  if (w){ const t = WIFI.t || {}; w.textContent = 'Wi-Fi (' + (t.networks || 0) + ')' + (t.counts && t.counts.look ? ' !' : ''); }
  const b = document.querySelector('#dev-wifi-seg button[data-v="bt"]');
  if (b){ const t = BT.t || {}; b.textContent = 'Bluetooth (' + (t.devices || 0) + ')' + (t.counts && t.counts.look ? ' !' : ''); }
  const wifi = DEV.group === 'wifi' || DEV.group === 'bt';
  document.getElementById('dev-show').parentNode.style.display = wifi ? 'none' : '';
  document.getElementById('dev-forget').style.display = wifi ? 'none' : '';
}
function renderDevices(){
  if (DEV.group === 'wifi'){ renderWifi(); return; }
  if (DEV.group === 'bt'){ renderBt(); return; }
  const box = document.getElementById('devices-box'); box.innerHTML = '';
  const q = (document.getElementById('dev-filter').value || '').trim().toLowerCase();
  const rows = DEV.rows.filter(r => (!q || devText(r).includes(q)) &&
                                    (DEV.show === 'all' || (DEV.show === 'known') === !!r.known) &&
                                    (DEV.group === 'all' || r.group === DEV.group));
  const t = DEV.totals || {};
  const totals = document.getElementById('dev-totals');
  const sets = t.sets === 1 ? '1 vehicle or set' : t.sets + ' vehicles or sets';   // (the chat says it the same way)
  totals.textContent = DEV.totals ? (plural(t.devices || 0, 'device') + (t.sets ? ' (' + sets + ' among them)' : '')
                                     + (t.known ? ', ' + t.known + ' known' : '')
                                     + ', ' + plural(t.packets || 0, 'packet') + (q || DEV.show !== 'all' || DEV.group !== 'all' ? ' | ' + rows.length + ' shown' : '')) : '';
  if (DEV.note && Date.now() - DEV.note.t < 120000) box.appendChild(el('div', 'infobox', DEV.note.text));
  if (!DEV.rows.length) box.appendChild(el('div', 'act-sub', 'No devices decoded yet. Captures, band recordings ("Record 433 band" on the chat) and investigations add every device a decoder names here.'));
  else if (!rows.length) box.appendChild(el('div', 'act-sub', q ? 'Nothing matches "' + q + '".' : DEV.show === 'known' ? 'No known devices yet: a device is known once it was heard on 3 different days, or when you mark it.' : 'Every device here is known.'));
  if (DEV.group === 'vehicle' && rows.length){
    const parts = [['car', 'Cars: several tyre sensors heard together on more than one occasion'],
                   ['together', 'Probably one car: tyre sensors of one make heard within 5 seconds, seen once'], ['again', 'Cars that came back: heard on 2 or more days'],
                   ['passing', 'Single tyre sensors, one day: most likely cars passing']];
    box.appendChild(el('div', 'act-sub', 'A car is told by its tyre sensors only. The page never says whose car it is: passing cars have new IDs every time.'));
    parts.forEach(p => {
      const sub = rows.filter(r => r.vehicle === p[0]); if (!sub.length) return;
      box.appendChild(el('div', 'dev-head', p[1] + ' (' + sub.length + ')'));
      devDraw(box, sub, p[0]);
    });
  } else if (DEV.group === 'sensor' && rows.some(r => r.noise)){
    const real = rows.filter(r => !r.noise), noise = rows.filter(r => r.noise);
    if (real.length){ box.appendChild(el('div', 'dev-head', 'Sensors heard more than once (' + real.length + ')')); devDraw(box, real, 'real'); }
    box.appendChild(el('div', 'dev-head', 'Likely noise: heard once and never again (' + noise.length + ')'));
    box.appendChild(el('div', 'act-sub', 'A real sensor repeats every minute or so. One packet that never came back is most likely a decoder reading noise or another device (a wrong name or a reading like -200 degrees). Nothing is hidden: a sensor that comes back moves up.'));
    devDraw(box, noise, 'noise');
  } else devDraw(box, rows, 'all');
  devCounts();
  document.getElementById('dev-rule').textContent = DEV.rule ? DEV.rule + ' ' + (DEV.totals && DEV.totals.known_rule ? DEV.totals.known_rule + ' ' : '') + 'Up to 1500 devices are kept: passing tyre sensors go first (after 3 days, or the oldest beyond 1000), known devices last. Clear logs leaves it alone, Clear data empties it.' : '';
}
function devShow(v){
  DEV.show = v;
  document.querySelectorAll('#dev-show button').forEach(b => b.classList.toggle('on', b.dataset.v === v));
  renderDevices();
}
function devGroup(v){
  DEV.group = v;
  document.querySelectorAll('#dev-group button, #dev-wifi-seg button').forEach(b => b.classList.toggle('on', b.dataset.v === v));
  renderDevices();
}
async function devMark(key, known){
  let msg;
  try { const r = await post('/api/devices/mark', {key: key, known: known}); msg = r.message || 'Marked.'; }
  catch (e) { msg = 'Could not mark the device: ' + (e.message || e); }
  DEV.note = {text: msg, t: Date.now()};
  loadDevices(true);
}
async function devForget(){
  if (!confirm('Forget all devices?\n\nEmpties the device history on this tab. The logs are not touched; new decodes start the list again.')) return;
  let msg;
  try { const r = await post('/api/devices/forget'); msg = r.message || 'Devices forgotten.'; }
  catch (e) { msg = 'Could not forget the devices: ' + (e.message || e); }
  DEV.note = {text: msg, t: Date.now()};
  loadDevices(true);
}
setInterval(() => { if (!document.hidden && activeTab() === 'devices'){ loadDevices(false); loadWifi(false); loadBt(false); } }, 6000);
