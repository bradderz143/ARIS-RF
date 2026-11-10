// Signals tab: every investigated signal ARIS saved. The list is only downloaded again when it changed.
let sigTag = '';
async function loadSignals(){
  let res; try { res = await apiGet('/api/signals?have=' + encodeURIComponent(sigTag)); } catch (e) { return; }
  if (res.unchanged) return;
  sigTag = res.tag; const rows = res.signals || [];
  document.getElementById('sig-count').textContent = res.count + ' saved signal' + (res.count === 1 ? '' : 's');
  const box = document.getElementById('signals-box'); box.innerHTML = '';
  if (!rows.length) box.appendChild(el('div', 'muted', 'Nothing saved yet. Investigate a frequency and it appears here.'));
  rows.forEach(p => {
    const card = el('div', 'card');
    const head = el('div'); head.style.cssText = 'display:flex;justify-content:space-between;align-items:center;margin-bottom:6px';
    const f = el('strong', '', p.frequency_mhz.toFixed(4) + ' MHz'); f.style.cssText = 'font-size:1.05rem;color:#60a5fa';
    head.appendChild(f); head.appendChild(el('span', 'pill', 'SNR ' + (Number(p.snr_db) >= 0 ? '+' : '') + Number(p.snr_db).toFixed(1) + ' dB')); card.appendChild(head);
    const meta = el('div', '', 'Captured: ' + p.timestamp + (p.notes ? '  |  ' + p.notes : ''));
    meta.style.cssText = 'font-size:.78rem;color:#94a3b8;margin-bottom:8px'; card.appendChild(meta);
    const body = el('div', '', p.urh_analysis || ''); body.style.cssText = 'white-space:pre-wrap;line-height:1.45;font-size:.9rem;color:#e2e8f0';
    card.appendChild(body);
    const row = el('div'); row.style.cssText = 'margin-top:10px;display:flex;gap:6px;flex-wrap:wrap';
    const ask = el('button', 'btn', 'Ask AI about it'); ask.style.cssText = 'padding:6px 11px;font-size:.8rem';
    ask.onclick = () => sendQuick('Tell me in plain English about the signal at ' + p.frequency_mhz.toFixed(4) + ' MHz. What device likely made it, and is it normal?');
    const loc = el('button', 'btn-ghost', 'Locate on map'); loc.style.cssText = 'padding:6px 11px;font-size:.8rem';
    loc.onclick = () => mapLocateFreq(p.frequency_mhz);
    row.appendChild(ask); row.appendChild(loc); card.appendChild(row);
    const det = el('details'); det.appendChild(el('summary', '', 'Show raw technical data (optional)'));
    det.appendChild(el('pre', '', 'Power: ' + p.power_db + ' dB | Bandwidth: ' + p.bw_khz + ' kHz'));
    card.appendChild(det); box.appendChild(card);
  });
}
