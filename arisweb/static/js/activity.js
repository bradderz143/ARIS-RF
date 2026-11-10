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
// Clear logs (v4.8.8): the event logs and result lists go so the data folder never fills the disk. One confirm;
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
async function loadActivity(viewing){
  // Another tab is open: only the badge needs updating, so ask for the short version.
  let a; try { a = await apiGet('/api/activity' + (activeTab() === 'activity' ? '' : '?brief=1')); } catch (e) { return; }
  const seen = actSeen();
  const fresh = (a.flagged_rounds || []).filter(x => x.t > seen && (x.top || []).some(ev => ev.threat_level !== 'LOW')).length
              + (a.captures || []).filter(c => c.t > seen && (c.outcome === 'identified' || c.outcome === 'demodulated')).length;
  const badge = document.getElementById('act-badge'); badge.innerHTML = '';
  if (viewing){ try { localStorage.setItem('aris_act_seen', String(a.now)); } catch (e) {} }
  else if (fresh) badge.appendChild(el('span', 'badge', String(fresh)));
  if (activeTab() !== 'activity') return;
  let d = null; try { d = await apiGet('/api/disk'); } catch (e) {}
  const box = document.getElementById('activity-box'); box.innerHTML = '';
  const disk = el('div', 'act-disk');
  disk.appendChild(el('span', '', d && d.line ? d.line : 'On disk: sizes not available'));
  const cb = el('button', '', 'Clear logs'); cb.id = 'act-clear-logs'; cb.onclick = clearLogs; disk.appendChild(cb);
  box.appendChild(disk);
  if (ACT_NOTE && Date.now() - ACT_NOTE.t < 120000) box.appendChild(el('div', 'infobox', ACT_NOTE.text));
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
}
