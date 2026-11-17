// Activity tab: background captures, patrol findings (repeats grouped), the watchdog log, recent investigations.
function actTook(s){ s = Math.round(s || 0); return s < 90 ? s + ' s' : (s < 5400 ? Math.round(s / 60) + ' min' : (s / 3600).toFixed(1) + ' h'); }
function plural(n, w){ return n + ' ' + w + (n === 1 ? '' : 's'); }
function actRow(title, sub, level, buttons){
  const r = el('div', 'act-row'); const top = el('div', 'act-top'); top.appendChild(el('strong', '', title));
  if (level) top.appendChild(el('span', 'lvl ' + level, level)); r.appendChild(top);
  (Array.isArray(sub) ? sub : [sub]).filter(x => x).forEach(x => r.appendChild(el('div', 'act-sub', x)));
  if (buttons && buttons.length){ const b = el('div', 'act-btns');
    buttons.forEach(([label, action]) => { const k = el('button', '', label);
      k.onclick = () => (typeof action === 'function' ? action() : sendQuick(action)); b.appendChild(k); });
    r.appendChild(b); }
  return r;
}
function actSeen(){ try { return parseFloat(localStorage.getItem('aris_act_seen') || '0'); } catch (e) { return 0; } }
// Clear logs: the event logs and result lists go so the data folder never fills the disk. One confirm;
// the reply stays at the top of the tab for two minutes. Captures keep running. (Clear data, on the chat, wipes all.)
let ACT_NOTE = null;
async function clearLogs(){
  if (!confirm('Clear the logs?\n\nDeletes the capture, patrol and audit logs, the list of finished captures and recent investigations, the chat notices and the text log.\n\nKeeps the Signals list, FM station names, the device history, baselines, settings, logins, and the Map antennas and floorplans. A running capture keeps running.')) return;
  let msg;
  try { const r = await post('/api/clear_logs'); msg = r.message || 'Logs cleared.'; }
  catch (e) { msg = 'Could not clear the logs: ' + (e.message || e); }
  ACT_NOTE = {text: msg, t: Date.now()};
  loadActivity(true);
}
// Room watch: a long watch of the bands where small transmitters are found. The card: status, both ways to start
// (carry on with the reference it has, or a new one), Stop, notes for the timeline, the report (to read and to
// download), "seen" for the alerts, Decode / Locate / "this one is mine" on an alert, the optional alert address,
// and a switch that keeps this screen on. The alarm: a red bar on every tab while an alert is not marked as seen,
// with a sound, a vibration and the page title, repeated every minute.
const ROOM = {report: null, pushOpen: false, rung: 0, ringAt: 0, ac: null, wake: null, wantWake: false, busy: false, hours: 72,
              live: false, fails: 0, lostAt: 0, last: null, note: ''};
try { ROOM.wantWake = localStorage.getItem('aris_room_wake') === '1'; } catch (e) {}
function roomAdmin(){ return (document.body.dataset.role || 'admin') !== 'viewer'; }
function roomArm(){ try { const C = window.AudioContext || window.webkitAudioContext; if (C && !ROOM.ac) ROOM.ac = new C(); if (ROOM.ac && ROOM.ac.state === 'suspended') ROOM.ac.resume(); } catch (e) {} }
document.addEventListener('pointerdown', roomArm, {passive: true});     // browsers allow sound only after a touch
function roomRing(){
  ROOM.ringAt = Date.now();
  try { if (navigator.vibrate) navigator.vibrate([400, 150, 400, 150, 400]); } catch (e) {}
  try { roomArm(); const ac = ROOM.ac; if (!ac) return;
    [0, 0.35, 0.7].forEach(dt => { const o = ac.createOscillator(), g = ac.createGain(); o.type = 'square'; o.frequency.value = 880;
      o.connect(g); g.connect(ac.destination); const t = ac.currentTime + dt;
      g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.35, t + 0.02); g.gain.exponentialRampToValueAtTime(0.0001, t + 0.25);
      o.start(t); o.stop(t + 0.27); }); } catch (e) {}
}
// "Seen" counts only when ARIS said so: a press that never arrived must not silence the alarm.
async function roomSeen(){
  try { await post('/api/roomwatch/ack'); } catch (e) { ROOM.note = 'Not marked: no answer from ARIS. '; roomAlarm(ROOM.last); return; }
  ROOM.note = ''; roomAlarm(Object.assign({}, ROOM.last || {}, {unseen: 0, last: null})); loadActivity(activeTab() === 'activity');
}
// No answer from ARIS while a watch runs (the computer is off or asleep, the network or the program is down): the
// page must not go on showing "Watching". After two polls without an answer the alarm bar says so and rings.
function roomLost(){
  ROOM.fails += 1;
  if (!ROOM.live || ROOM.fails < 2) return;
  if (!ROOM.lostAt) ROOM.lostAt = Date.now() - 15000;
  const d = new Date(ROOM.lostAt), hm = String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
  const bar = roomBar();
  bar.querySelector('.ra-text').textContent = 'No answer from ARIS since ' + hm + ': the room watch cannot be seen from here. Check the computer and the connection.';
  document.getElementById('room-alarm-seen').style.display = 'none';
  bar.style.display = ''; document.title = 'NO ANSWER | ARIS';
  if (Date.now() - ROOM.ringAt > 55000) roomRing();
}
function roomBar(){
  let bar = document.getElementById('room-alarm');
  if (!bar){
    bar = el('div', 'room-alarm'); bar.id = 'room-alarm'; bar.setAttribute('role', 'alert');
    bar.appendChild(el('div', 'ra-text', ''));
    const b = el('div', 'ra-btns');
    const open = el('button', '', 'Open'); open.onclick = () => switchTab('activity'); b.appendChild(open);
    const seen = el('button', '', 'Seen'); seen.id = 'room-alarm-seen'; seen.onclick = roomSeen; b.appendChild(seen);
    bar.appendChild(b);
    const tabs = document.querySelector('.tabs'); tabs.parentNode.insertBefore(bar, tabs);
  }
  return bar;
}
function roomAlarm(room){
  ROOM.last = room || null; ROOM.fails = 0; ROOM.lostAt = 0;
  ROOM.live = !!(room && (room.running || room.unseen));
  let bar = document.getElementById('room-alarm');
  const n = (room && room.unseen) || 0, last = room && room.last;
  if (!n || !last){ if (bar) bar.style.display = 'none'; document.title = 'ARIS'; ROOM.note = ''; return; }
  bar = roomBar();
  document.getElementById('room-alarm-seen').style.display = '';
  bar.querySelector('.ra-text').textContent = ROOM.note + (n > 1 ? n + ' alerts not marked as seen. Latest, ' : 'Alert, ') + String(last.time || '').slice(11, 16) + ': ' + String(last.text || '');
  bar.style.display = '';
  document.title = '(' + n + ') ALERT | ARIS';
  if (last.t > ROOM.rung || Date.now() - ROOM.ringAt > 55000){ ROOM.rung = Math.max(ROOM.rung, last.t); roomRing(); }
}
async function roomWake(on){
  ROOM.wantWake = !!on; try { localStorage.setItem('aris_room_wake', on ? '1' : ''); } catch (e) {}
  try { if (on && 'wakeLock' in navigator){ if (!ROOM.wake){ ROOM.wake = await navigator.wakeLock.request('screen'); ROOM.wake.addEventListener('release', () => { ROOM.wake = null; }); } }
        else if (ROOM.wake){ const w = ROOM.wake; ROOM.wake = null; await w.release(); } } catch (e) { ROOM.wake = null; }
}
document.addEventListener('visibilitychange', () => { if (!document.hidden && ROOM.wantWake) roomWake(true); });
function roomBtn(label, fn, cls){ const k = el('button', cls || '', label); k.onclick = fn; return k; }
function actRoom(box, r){
  if (!r){ box.appendChild(el('div', 'h', 'Room watch')); box.appendChild(el('div', 'act-sub', 'The room watch could not be read just now.')); return; }
  const admin = roomAdmin(), running = !!r.running;
  if (ROOM.wantWake && !ROOM.wake && running) roomWake(true);
  box.appendChild(el('div', 'h', 'Room watch'));
  const sub = [r.line || 'No room watch has been started.'];
  if ((r.partly_covered || []).length) sub.push('Only partly covered: ' + r.partly_covered.join('; ') + '.');
  if (running && !(r.push && r.push.set)) sub.push('A phone with its screen off hears nothing: keep this page open with the screen on, or set "Alerts to a locked phone" below.');
  ROOM.pushSet = !!(r.push && r.push.set);
  if ((r.deaf || []).length) sub.push('Not hearing its reference signals: ' + r.deaf.join(', ') + '. Check the antenna.');
  if (r.inventory && r.inventory.listed) sub.push(r.inventory.listed + ' steady signals were already on the air at the reference that no broadcast or phone band explains: the Report lists them. Anything on then never alerts.');
  if ((r.accepted || []).length) sub.push('Accepted as known: ' + r.accepted.map(x => x.mhz + ' MHz (' + x.label + ')').join('; ') + '.');
  const rx = r.receivers || [], down = rx.filter(x => x.state === 'failing' || x.state === 'slow');
  if (running && rx.length){
    const off = rx.filter(x => x.state !== 'working');
    if (!off.length) sub.push('Receivers: all ' + rx.length + ' working.');
    off.forEach(x => sub.push(x.note || (x.name + ': ' + x.state + '.')));
  }
  if (r.gaps_s >= 60) sub.push('Not watched for ' + actTook(r.gaps_s) + ' in total (the Report lists when).');
  if (r.late_refs) sub.push(plural(r.late_refs, 'reference') + ' recorded late (the Report lists them: what was on the air then counts as normal there).');
  (r.errors || []).slice(-2).forEach(x => sub.push('Note: ' + x));
  const bad = running && (r.stalled || (r.unwatched || []).length || (r.deaf || []).length || down.length);
  const card = actRow(running ? (r.phase === 'reference' ? 'Recording the quiet reference' : 'Watching') : r.interrupted ? 'Interrupted' : 'Not running',
                      sub, running ? (bad ? 'CHECK' : 'RUN') : null, []);
  card.id = 'act-roomwatch';
  const b = el('div', 'act-btns');
  if (admin && !running){
    const sel = document.createElement('select'); sel.id = 'rw-hours'; sel.className = 'rw-sel';
    [[8, '8 hours'], [24, '1 day'], [48, '2 days'], [72, '3 days'], [120, '5 days']].forEach(([h, t]) => { const o = el('option', '', t); o.value = h; if (h === ROOM.hours) o.selected = true; sel.appendChild(o); });
    sel.onchange = () => { ROOM.hours = Number(sel.value); };
    b.appendChild(sel);
    if (r.previous) b.appendChild(roomBtn('Carry on', () => actRoomStart(true), 'rw-go'));
    b.appendChild(roomBtn(r.previous ? 'New watch' : 'Start', () => actRoomStart(false), r.previous ? '' : 'rw-go'));
  }
  if (admin && running) b.appendChild(roomBtn('Stop', async () => { if (!confirm('Stop the room watch?\n\n"Carry on" starts it again later with the same reference and everything it has seen.')) return;
    try { await post('/api/roomwatch/stop'); } catch (e) {} loadActivity(true); }));
  if (r.unseen) b.appendChild(roomBtn('Mark ' + plural(r.unseen, 'alert') + ' as seen', roomSeen, 'rw-go'));
  if (running){
    ['Break', 'Door opened', 'People arrive'].forEach(m => b.appendChild(roomBtn(m, async () => { if (ROOM.busy) return; ROOM.busy = true;
      try { await post('/api/roomwatch/mark', {label: m}); } catch (e) {} ROOM.busy = false; loadActivity(true); })));
    b.appendChild(roomBtn('Note...', async () => { const t = prompt('A note for the timeline:'); if (!t) return;
      try { await post('/api/roomwatch/mark', {label: t}); } catch (e) {} loadActivity(true); }));
  }
  if (r.started) b.appendChild(roomBtn(ROOM.report === null ? 'Report' : 'Close the report', async () => {
    if (ROOM.report !== null){ ROOM.report = null; loadActivity(true); return; }
    try { ROOM.report = (await apiGet('/api/roomwatch/report')).text || ''; } catch (e) { ROOM.report = 'Could not read the report.'; }
    loadActivity(true); }));
  if ('wakeLock' in navigator) b.appendChild(roomBtn(ROOM.wantWake ? 'Screen stays on' : 'Keep screen on', async () => { await roomWake(!ROOM.wantWake); loadActivity(true); }, ROOM.wantWake ? 'rw-on' : ''));
  card.appendChild(b);
  if (ROOM.report !== null){
    const pre = el('pre', 'rw-report', ROOM.report); card.appendChild(pre);
    const dl = document.createElement('a'); dl.className = 'rw-dl'; dl.href = '/api/roomwatch/report?download=1'; dl.textContent = 'Download the report as a text file'; card.appendChild(dl);
  }
  if (admin){
    const d = document.createElement('details'); d.className = 'rw-push'; d.open = ROOM.pushOpen;
    d.ontoggle = () => { ROOM.pushOpen = d.open; };
    const p = r.push || {};
    d.appendChild(el('summary', '', 'Alerts to a locked phone (optional): ' + (p.set ? 'on, through ' + (p.host || 'the address set') : 'off')));
    d.appendChild(el('div', 'act-sub', 'A page cannot ring a phone whose screen is off. Give the address of a push service here (for example a topic of the ntfy app, https://ntfy.sh/a-long-random-name) and every alert is also posted there. Only a fixed sentence is sent unless you tick the box.'));
    const inp = document.createElement('input'); inp.type = 'url'; inp.id = 'rw-push-url'; inp.className = 'rw-inp'; inp.placeholder = p.set ? 'set (type a new address to change it)' : 'https://...'; inp.autocomplete = 'off'; d.appendChild(inp);
    const lab = el('label', 'rw-chk', ''); const chk = document.createElement('input'); chk.type = 'checkbox'; chk.checked = !!p.detail; lab.appendChild(chk); lab.appendChild(document.createTextNode(' Send the alert\'s own text too (the frequency leaves this computer)')); d.appendChild(lab);
    const pb = el('div', 'act-btns');
    const say = t => { ACT_NOTE = {text: t, t: Date.now()}; loadActivity(true); };
    pb.appendChild(roomBtn('Save', async () => { const u = inp.value.trim(); if (!u && !p.set){ say('Type the address first.'); return; }
      try { await post('/api/roomwatch/push', u ? {url: u, detail: chk.checked} : {detail: chk.checked}); say('Saved.'); } catch (e) { say('Not saved: ' + (e.message || e)); } }));
    if (p.set){
      pb.appendChild(roomBtn('Send a test', async () => { try { const x = await post('/api/roomwatch/push_test'); say(x.message || 'Test sent.'); } catch (e) { say(e.message || 'The test failed.'); } }));
      pb.appendChild(roomBtn('Turn off', async () => { if (!confirm('Stop posting alerts to that address?')) return; try { await post('/api/roomwatch/push', {url: ''}); say('Turned off.'); } catch (e) { say('Could not turn it off: ' + (e.message || e)); } }));
    }
    d.appendChild(pb);
    if (p.error) d.appendChild(el('div', 'act-sub', 'The last alert could not be posted (' + p.error + ').'));
    if (p.pending) d.appendChild(el('div', 'act-sub', plural(p.pending, 'alert') + ' not delivered to the alert address yet; it keeps trying for half an hour.'));
    if (p.dropped) d.appendChild(el('div', 'act-sub', plural(p.dropped, 'alert') + ' could not be delivered to the alert address at all. They are in the timeline.'));
    card.appendChild(d);
  }
  box.appendChild(card);
  const NAME = {alert: 'Alert', health: 'Check', mark: 'Note', note: 'Seen by the watch', skip: 'Gap', stop: 'End', start: 'Start', ack: 'Marked as seen', mine: 'Accepted', late: 'Reference'};
  const CARRIER = ['new', 'surge', 'stronger', 'held', 'recurring'];     // alerts about one frequency (not a whole sweep)
  (r.rows || []).slice(0, 8).forEach(x => {
    const btns = [];
    if (x.kind === 'alert' && x.mhz && CARRIER.indexOf(x.what) >= 0){
      const f = Number(x.mhz).toFixed(4);
      btns.push(['Decode', 'Investigate ' + f + ' MHz'], ['Locate', () => mapLocateFreq(Number(x.mhz))]);
      if (admin && running) btns.push(['This one is mine', async () => { const t = prompt('What is ' + f + ' MHz? (it stops alerting and the report lists it as accepted)', ''); if (t === null) return;
        try { const a = await post('/api/roomwatch/accept', {frequency_mhz: Number(x.mhz), label: t || 'known'}); ACT_NOTE = {text: a.message || 'Accepted.', t: Date.now()}; }
        catch (e) { ACT_NOTE = {text: 'Not accepted: ' + (e.message || e), t: Date.now()}; } loadActivity(true); }]);
    }
    box.appendChild(actRow(String(x.time || '').slice(5, 16) + '  ' + (NAME[x.kind] || x.kind), [x.text],
                           x.kind === 'alert' || x.kind === 'health' ? 'HIGH' : null, btns)); });
}
async function actRoomStart(carry){
  const h = ROOM.hours, len = h >= 48 ? (h / 24) + ' days' : h + ' hours';
  if (!confirm(carry
      ? 'Carry on the room watch for ' + len + '?\n\nIt keeps the reference it has and everything it has seen. Use this after a stop or a restart during the same watch.'
      : 'Start a new room watch for ' + len + '?\n\nIt first records a quiet reference in each band (several minutes). Keep the room as quiet as it will be: phones off, no new equipment.\n\nAnything already transmitting while the reference is recorded counts as normal and never alerts.\n\nIt covers only what the receivers here tune, and a clear result is not proof that a room is clean.\n\nAlerts to a locked phone: ' + (ROOM.pushSet ? 'on.' : 'off (a page cannot ring a phone whose screen is off; see the card).'))) return;
  if (ROOM.busy) return; ROOM.busy = true;
  try { const r = await post('/api/roomwatch/start', carry ? {hours: h, carry_on: true} : {hours: h, new_reference: true}); ACT_NOTE = {text: r.message || 'Started.', t: Date.now()}; }
  catch (e) { ACT_NOTE = {text: 'Could not start the room watch: ' + (e.message || e), t: Date.now()}; }
  ROOM.busy = false; loadActivity(true);
}
async function loadActivity(viewing, auto){
  // Another tab is open: only the badge needs updating, so ask for the short version.
  let a; try { a = await apiGet('/api/activity' + (activeTab() === 'activity' && !document.hidden ? '' : '?brief=1')); } catch (e) { roomLost(); return; }
  const seen = actSeen();
  const fresh = (a.flagged_rounds || []).filter(x => x.t > seen && (x.top || []).some(ev => ev.threat_level !== 'LOW')).length
              + (a.captures || []).filter(c => c.t > seen && (c.outcome === 'identified' || c.outcome === 'demodulated')).length
              + ((a.room && a.room.unseen) || 0);
  roomAlarm(a.room);
  const badge = document.getElementById('act-badge'); badge.innerHTML = '';
  if (viewing){ try { localStorage.setItem('aris_act_seen', String(a.now)); } catch (e) {} }
  else if (fresh) badge.appendChild(el('span', 'badge', String(fresh)));
  if (activeTab() !== 'activity' || document.hidden) return;
  if (auto && (ROOM.report !== null || ROOM.pushOpen)) return;     // someone is reading or typing: do not redraw
  // everything is fetched before the tab is emptied and drawn in one go, and the scroll position is put back:
  // a redraw that waited half way threw the reader to the top every 15 s
  const [d, rw] = await Promise.all([apiGet('/api/disk').catch(() => null), apiGet('/api/roomwatch').catch(() => null)]);
  if (activeTab() !== 'activity') return;
  const box = document.getElementById('activity-box');
  const view = document.getElementById('view-activity'), keepY = window.scrollY, keepV = view ? view.scrollTop : 0;
  box.innerHTML = '';
  const disk = el('div', 'act-disk');
  disk.appendChild(el('span', '', d && d.line ? d.line : 'On disk: sizes not available'));
  const cb = el('button', '', 'Clear logs'); cb.id = 'act-clear-logs'; cb.onclick = clearLogs; disk.appendChild(cb);
  box.appendChild(disk);
  if (ACT_NOTE && Date.now() - ACT_NOTE.t < 120000){ const n = el('div', 'infobox', ACT_NOTE.text); n.style.whiteSpace = 'pre-wrap'; box.appendChild(n); }
  actRoom(box, rw);
  const w = a.watchdog || {};
  const run = w.watchdogs_running || [];
  box.appendChild(el('div', 'h', 'Captures'));
  if (!run.length && !(a.captures || []).length) box.appendChild(el('div', 'act-sub', 'None yet. Select a signal on the Spectrum tab and press "Capture until decoded", or ask "capture 433.92".'));
  run.forEach(x => box.appendChild(actRow(x.mode === 'band' ? 'Recording the ' + x.band_label + ' on ' + x.dongle
                                                           : 'Listening: ' + Number(x.freq_mhz).toFixed(4) + ' MHz on ' + x.dongle, [
      (x.mode === 'band' ? 'Every burst recorded and decoded' + (x.slices > 1 ? ' (' + x.slices + ' slices in turn)' : '')
       : x.mode === 'continuous' ? 'Logging everything' : 'Until a packet is decoded or demodulated') + ' | ' + (x.mode === 'band' ? '' : (x.decoder || '') + ' | ') + x.minutes + ' min so far' + (x.deadline ? ', stops by ' + new Date(x.deadline * 1000).toTimeString().slice(0, 5) : ''),
      (x.status || '') + (x.mode !== 'band' && (x.decoded_packets || x.bursts_heard) ? ' | ' + x.decoded_packets + ' decoded, ' + x.bursts_heard + ' undecoded bursts' : '')], 'RUN',
    [['Stop', async () => { try { await post('/api/capture/stop', {serial: x.serial}); } catch (e) {} loadActivity(true); }]])));
  const OUT = {identified: 'IDENTIFIED', demodulated: 'CAPTURED', timeout: 'NOTHING', stopped: 'STOPPED', error: 'FAILED'};
  (a.captures || []).slice(0, 8).forEach(c => box.appendChild(c.band_label
    ? actRow('The ' + c.band_label + ' on ' + c.dongle, [c.finished + ' | after ' + actTook(c.seconds), c.verdict],
             c.outcome === 'error' ? 'FAILED' : 'RECORDED',
             [['Record again', async () => { try { await post('/api/capture/start', {band: c.band.join('-')}); } catch (e) {} loadActivity(true); }],
              ['Check the logs', 'check the logs']])
    : actRow(Number(c.freq_mhz).toFixed(4) + ' MHz on ' + c.dongle,
    [c.finished + ' | after ' + actTook(c.seconds) + ' | ' + (c.decoder || ''), c.verdict], OUT[c.outcome] || c.outcome,
    [['Investigate', 'Investigate ' + Number(c.freq_mhz).toFixed(4) + ' MHz'], ['Capture again', async () => { try { await post('/api/capture/start', {freq_mhz: c.freq_mhz}); } catch (e) {} loadActivity(true); }],
     ['Locate', () => mapLocateFreq(Number(c.freq_mhz))]])));
  const p = a.patrol || {};
  box.appendChild(el('div', 'h', 'Patrol'));
  box.appendChild(actRow(p.running ? 'Running' : 'Off',
    p.running ? [(p.bands || []).join(', ') + ' every ' + p.interval_min + ' min on ' + (p.dongles || []).join(', '),
                 (p.audits_done || 0) + ' band checks done, last update ' + (p.updated || '?')]
              : 'Start it with the button, or type /patrol start', null,
    p.running ? [['Stop patrol', '/patrol stop']] : [['Start patrol', '/patrol start']]));
  const groups = new Map();
  (a.flagged_rounds || []).forEach(rd => (rd.top || []).forEach(ev => {
    const key = rd.band + '|' + (ev.pattern === 'frequency hopping' ? 'hop' : Number(ev.peak_mhz).toFixed(2) + ev.pattern);
    const g = groups.get(key);
    if (g){ g.n += 1; g.first = rd.time; } else groups.set(key, {rd: rd, ev: ev, n: 1, first: rd.time});
  }));
  const evs = Array.from(groups.values());
  if (!evs.length) box.appendChild(el('div', 'act-sub', 'Nothing flagged yet.'));
  evs.slice(0, 15).forEach(({rd, ev, n, first}) => {
    const f = Number(ev.peak_mhz).toFixed(3);
    const hop = ev.pattern === 'frequency hopping';
    const sub = [(n > 1 ? 'In ' + n + ' rounds, ' + first + ' to ' + rd.time : rd.time) + ' | ' + rd.band + ' | ' + (hop ? ev.channels + ' channels' : ev.pattern) + ' | +' + ev.surge_delta_db + ' dB | ' + Object.keys(ev.flagged_by || {}).join(', ') + (rd.names_then ? ' (dongle numbers of that day)' : ''),
                 (ev.why || []).join('; ')];
    if (rd.checked && Number(rd.checked.mhz).toFixed(3) === f) sub.push('Checked automatically: ' + (rd.checked.verdict || rd.checked.error));
    box.appendChild(actRow((hop ? 'Hopping around ' : '') + f + ' MHz', sub, ev.threat_level,
      hop ? [] : [['Investigate', 'Investigate ' + f + ' MHz and tell me in plain English what it is and whether it is normal.'],
                  ['Capture', async () => { try { await post('/api/capture/start', {freq_mhz: Number(ev.peak_mhz)}); } catch (e) {} loadActivity(true); }],
                  ['Locate', () => mapLocateFreq(Number(ev.peak_mhz))]]));
  });
  box.appendChild(el('div', 'h', 'Heard by captures and the watchdog'));
  if (!(w.decoded_devices || []).length && !(w.undecoded_signals || []).length) box.appendChild(el('div', 'act-sub', 'Nothing logged yet.'));
  (w.decoded_devices || []).forEach(d => box.appendChild(actRow(d.model + (d.id !== undefined && d.id !== null ? ' ' + (String(d.id).startsWith('#') ? '' : '#') + d.id : ''),
    [plural(d.packets, 'packet') + ', ' + plural(d.transmissions, 'transmission') + (d.repeats_every_s ? ', about every ' + d.repeats_every_s + ' s' : '') + (d.best_snr_db !== null ? ' | SNR ' + d.best_snr_db + ' dB' : '') + ' | ' + (d.heard_on || []).join(', '),
     'Last seen ' + d.last_seen], null)));
  const fr = g => (g.freq_range_mhz && g.freq_range_mhz[1] - g.freq_range_mhz[0] >= 0.0005)
    ? Number(g.freq_range_mhz[0]).toFixed(3) + '-' + Number(g.freq_range_mhz[1]).toFixed(3) : g.freq_mhz;
  (w.undecoded_signals || []).forEach(g => box.appendChild(actRow('Undecoded ' + g.modulation + ' at ' + fr(g) + ' MHz',
    [plural(g.transmissions, 'transmission') + (g.repeats_every_s ? ', about every ' + g.repeats_every_s + ' s' : '') + ' | ' + (g.signal || g.pattern),
     g.pattern_hint ? 'Usually: ' + g.pattern_hint : '', 'Last seen ' + g.last_seen], null,
    [['Find out what it is', 'Find out exactly what ' + g.freq_mhz + ' MHz is'], ['Capture', async () => { try { await post('/api/capture/start', {freq_mhz: g.freq_mhz}); } catch (e) {} loadActivity(true); }],
     ['Locate', () => mapLocateFreq(Number(g.freq_mhz))]])));
  box.appendChild(el('div', 'h', 'Recent investigations'));
  const ch = a.checks || [];
  if (!ch.length) box.appendChild(el('div', 'act-sub', 'None yet.'));
  ch.forEach(c => box.appendChild(actRow(Number(c.frequency_mhz).toFixed(4) + ' MHz',
    [c.time + (c.evidence_text ? ' | ' + c.evidence_text : '') + (c.strongest_dongle ? ' | strongest: ' + c.strongest_dongle : ''), c.verdict], null,
    [['Again', 'Investigate ' + Number(c.frequency_mhz).toFixed(4) + ' MHz'], ['Locate', () => mapLocateFreq(Number(c.measured_carrier_mhz || c.frequency_mhz))]])));
  if (view) view.scrollTop = keepV;
  if (window.scrollY !== keepY) window.scrollTo(0, keepY);
}
