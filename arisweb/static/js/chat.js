// AI Chat tab: the server relays each message to the aris.py console and keeps the conversation.
// Polling is cheap: the server only sends the messages when its sequence number changed, and the page polls
// fast only while ARIS is working or the chat is on screen.
let chatSeq = -1, wasBusy = false, chatTimer = null, chatCount = -1;
async function pollChat(){
  clearTimeout(chatTimer);
  let data = null;
  try { data = await apiGet('/api/chat_state?seq=' + chatSeq); } catch (e) {}
  if (data){
    if (data.messages){
      const n = data.messages.length, badge = document.getElementById('chat-badge');
      if (chatCount >= 0 && n > chatCount && activeTab() !== 'chat' && badge) badge.innerHTML = '<span class="badge">new</span>';
      chatCount = n;
      const box = document.getElementById('chat-box'); box.innerHTML = '';
      data.messages.forEach(m => {
        const div = el('div', 'bubble ' + m.role); renderText(div, m.text);
        div.appendChild(el('span', 'time', m.time)); box.appendChild(div);
      });
      box.scrollTop = box.scrollHeight;
    }
    chatSeq = data.seq;
    const sb = document.getElementById('status-bar');
    if (data.busy){ sb.style.display = 'flex'; document.getElementById('status-text').textContent = data.status; }
    else sb.style.display = 'none';
    if (wasBusy && !data.busy){ loadSignals(); if (typeof mapRefreshIfOpen === 'function') mapRefreshIfOpen(); }
    wasBusy = data.busy;
  }
  const delay = document.hidden ? 15000 : (wasBusy ? 1000 : (activeTab() === 'chat' ? 2000 : 6000));
  clearTimeout(chatTimer);                       // overlapping calls (a send while a poll is out) keep one timer
  chatTimer = setTimeout(pollChat, delay);
}
async function sendMessage(e){
  e.preventDefault();
  const inp = document.getElementById('chat-input'); const txt = inp.value.trim(); if (!txt) return;
  inp.value = ''; try { await post('/api/chat_send', {message: txt}); } catch (err) {} pollChat();
}
// The chat's Stop: ends only what the chat is doing (captures, Decode and Name all keep running).
async function stopChat(){ try { await post('/api/chat_reset', {scope: 'chat'}); } catch (e) {} chatSeq = -1; pollChat(); }
// Clear data: stops everything, then wipes every log and result (asked twice: the data, then the baselines).
async function clearData(){
  if (!confirm('Clear all data?\n\nDeletes the Signals list, capture logs and results, patrol findings, FM station names, the device history (Devices tab), investigation reports and this chat.\n\nKeeps settings (gains, frequency corrections, dongles), logins, and the Map antennas and floorplans.\n\nEverything running is stopped first.')) return;
  const base = confirm('Also delete the baselines?\n\nPatrol and the threat audit compare against them; record new ones afterwards ("record a baseline").\n\nOK deletes them, Cancel keeps them.');
  try { await post('/api/clear_data', {baselines: base}); } catch (e) {}
  if (typeof switchTab === 'function') switchTab('chat');
  chatSeq = -1; pollChat();
}
// Reset Scanner: the server stops every task that uses a dongle and says in the chat what it stopped.
async function resetAI(){
  try { await post('/api/chat_reset'); } catch (e) {}
  if (typeof switchTab === 'function') switchTab('chat');
  chatSeq = -1; pollChat();
}
