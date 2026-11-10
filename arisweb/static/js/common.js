// Shared helpers. Every change request carries the X-ARIS header (the server refuses changes without it).
function el(tag, cls, text){ const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; }
function renderText(node, text){
  // Shows ```blocks```, **bold** and `code` from the AI as formatting, safely (no HTML from the reply is ever parsed).
  const blocks = String(text).split(/```[a-zA-Z]*\n?([\s\S]*?)```/);
  if (blocks.length > 1){
    blocks.forEach((b, i) => { if (i % 2){ const pre = document.createElement('pre'); pre.className = 'chatpre'; pre.textContent = b.replace(/\n$/, ''); node.appendChild(pre); }
                               else if (b) renderInline(node, b); });
    return;
  }
  renderInline(node, text);
}
function renderInline(node, text){
  const parts = String(text).split(/(\*\*[^*\n]+\*\*|`[^`\n]+`)/);
  for (const p of parts){
    if (p.length > 4 && p.startsWith('**') && p.endsWith('**')){ const b = document.createElement('strong'); b.textContent = p.slice(2, -2); node.appendChild(b); }
    else if (p.length > 2 && p.startsWith('`') && p.endsWith('`')){ const c = document.createElement('code'); c.textContent = p.slice(1, -1); node.appendChild(c); }
    else if (p){ node.appendChild(document.createTextNode(p)); }
  }
}
function toLogin(r){ if (r.status === 401){ location.href = '/login'; return true; } return false; }
async function apiGet(url){
  const r = await fetch(url); if (toLogin(r)) throw new Error('login');
  const j = await r.json().catch(() => ({error: 'bad reply'}));
  if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status)); return j;
}
async function apiSend(method, url, body){
  const isForm = body instanceof FormData;
  const r = await fetch(url, {method: method, headers: isForm ? {'X-ARIS': '1'} : {'Content-Type': 'application/json', 'X-ARIS': '1'},
                              body: isForm ? body : JSON.stringify(body || {})});
  if (toLogin(r)) throw new Error('login');
  const j = await r.json().catch(() => ({error: 'bad reply'}));
  if (!r.ok){ const e = new Error(j.error || ('HTTP ' + r.status)); e.job = j.job; e.status = r.status; throw e; }
  return j;
}
function post(url, body){ return apiSend('POST', url, body); }
function del(url){ return apiSend('DELETE', url); }
const TABS = ['chat', 'signals', 'spectrum', 'map', 'activity', 'devices'];
function activeTab(){ return TABS.find(t => document.getElementById('view-' + t).classList.contains('active')); }
function switchTab(t){
  for (const n of TABS){
    document.getElementById('tab-' + n).classList.toggle('active', t === n);
    document.getElementById('view-' + n).classList.toggle('active', t === n);
  }
  try { localStorage.setItem('aris_tab', t); } catch (e) {}
  if (t === 'chat'){ const b = document.getElementById('chat-badge'); if (b) b.innerHTML = ''; }
  if (t === 'signals') loadSignals();
  if (t === 'spectrum') specInit();
  if (t === 'map') mapInit();
  if (t === 'activity') loadActivity(true);
  if (t === 'devices'){ loadDevices(true); loadWifi(true); loadBt(true); }
}
async function sendQuick(txt){ switchTab('chat'); try { await post('/api/chat_send', {message: txt}); } catch (e) {} pollChat(); }
const DONGLE_COLORS = ['#34d399', '#60a5fa', '#f472b6', '#fbbf24', '#a78bfa', '#fb923c'];
function heatColor(t){
  t = Math.max(0, Math.min(1, t));
  const st = [[0, [5, 7, 30]], [0.25, [20, 40, 160]], [0.5, [0, 170, 210]], [0.75, [250, 220, 60]], [1, [240, 60, 40]]];
  for (let i = 1; i < st.length; i++){ if (t <= st[i][0]){ const a = st[i - 1], b = st[i], u = (t - a[0]) / (b[0] - a[0]);
    return [0, 1, 2].map(k => Math.round(a[1][k] + (b[1][k] - a[1][k]) * u)); } }
  return st[st.length - 1][1];
}
function appStart(){
  pollChat(); loadSignals();
  setInterval(() => { if (!document.hidden) loadActivity(activeTab() === 'activity'); }, 15000); loadActivity(false);
  document.addEventListener('visibilitychange', () => { if (!document.hidden){ pollChat(); loadActivity(activeTab() === 'activity'); } });
  let t = null; try { t = localStorage.getItem('aris_tab'); } catch (e) {}
  if (t && TABS.includes(t) && t !== 'chat') switchTab(t);
}
