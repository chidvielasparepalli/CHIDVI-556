const $ = id => document.getElementById(id);
const feed = $('feed'), input = $('command'), send = $('send'), mic = $('mic');
const terminal = new Set(['succeeded', 'failed', 'cancelled']);
const tasks = new Map();
let csrf = '', deviceId = '', online = false, sending = false, socket = null, retry = 1000;
let pending = null, activeTask = null, voice = null, audio = null, stream = null, processor = null;
let seen = new Set(), reconnectTimer = null, microphoneStarting = false;
try { pending = JSON.parse(sessionStorage.getItem('chidvi_pending') || 'null'); } catch (_) {}
if (pending?.text) input.value = pending.text;

function notice(text = '') { $('notice').textContent = text; $('notice').hidden = !text; }
function connection(connected, text) {
  online = connected;
  $('connection').textContent = text;
  $('connection').classList.toggle('online', connected);
  send.disabled = !online || sending;
}
function status(value = {}) {
  const personality = typeof value.personality === 'object' ? value.personality.name : value.personality;
  $('personality').textContent = 'Personality: ' + (personality || 'Current desktop selection');
  $('control').textContent = value.control_enabled ? 'Laptop control enabled' : 'Laptop: ' + (value.state || 'ready');
}
async function api(path, body) {
  const response = await fetch(path, {method:body === undefined ? 'GET' : 'POST',
    headers:body === undefined ? {} : {'Content-Type':'application/json', 'X-CSRF-Token':csrf},
    body:body === undefined ? undefined : JSON.stringify(body), signal:AbortSignal.timeout(12000)});
  if (response.status === 401) { location.replace('/login'); throw new Error('Session expired. Pair again.'); }
  const result = await response.json();
  if (!response.ok) { const failure = new Error(result.detail || 'Request failed.'); failure.status = response.status; throw failure; }
  return result;
}
function clearPending() {
  if (input.value === pending?.text) input.value = '';
  pending = null;
  sessionStorage.removeItem('chidvi_pending');
  send.textContent = 'Send';
}
function renderTasks() {
  const values = [...tasks.values()].sort((a,b) => (b.created_at || 0) - (a.created_at || 0));
  activeTask = values.find(task => !terminal.has(task.state)) || null;
  $('stop').disabled = !activeTask;
  const list = $('tasks'); list.replaceChildren();
  const visible = values.filter(task => !terminal.has(task.state));
  visible.push(...values.filter(task => terminal.has(task.state)).slice(0, 3));
  if (!visible.length) { const item = document.createElement('li'); item.textContent = 'No tasks yet.'; list.append(item); }
  for (const task of visible) {
    const item = document.createElement('li'), content = document.createElement('span'), detail = document.createElement('small');
    content.textContent = String(task.state || 'queued').replaceAll('_',' ') + ': ' + (task.text || task.id);
    detail.textContent = task.detail || ''; content.append(detail); item.append(content);
    if (!terminal.has(task.state)) {
      const cancel = document.createElement('button'); cancel.type = 'button'; cancel.className = 'quiet cancel';
      cancel.textContent = 'Cancel'; cancel.setAttribute('aria-label', 'Cancel task: ' + (task.text || task.id));
      cancel.addEventListener('click', () => cancelTask(task.id)); item.append(cancel);
    }
    list.append(item);
  }
}
function taskUpdate(task) {
  if (!task?.id) return;
  tasks.set(task.id, task);
  if (pending && task.request_id === pending.request_id) clearPending();
  renderTasks();
}
function append(message) {
  if (message.sequence && seen.has(message.sequence)) return;
  if (message.sequence) seen.add(message.sequence);
  feed.querySelector('.empty')?.remove();
  const element = document.createElement('article');
  const role = message.type === 'log' ? (message.speaker === 'user' ? 'user' : 'assistant') : 'system';
  element.className = 'message ' + role;
  if (message.type === 'file_received') {
    const link = document.createElement('a'); link.href = '/uploads/' + encodeURIComponent(message.name);
    link.download = message.name; link.textContent = 'Download ' + message.name; element.append(link);
  } else {
    const label = document.createElement('span'); label.className = 'speaker';
    label.textContent = role === 'user' ? 'You' : role === 'assistant' ? 'CHIDVI' : 'Laptop';
    const text = document.createElement('span'); text.textContent = message.text || '';
    element.append(label, text);
  }
  feed.append(element);
  while (feed.children.length > 300) feed.firstElementChild.remove();
}
async function connect() {
  clearTimeout(reconnectTimer);
  try {
    const session = await api('/api/session');
    csrf = session.csrf; deviceId = session.device_id; status(session.status);
    for (const task of session.tasks || []) taskUpdate(task);
    socket = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
    socket.onopen = () => socket.send(JSON.stringify({type:'hello', csrf}));
    socket.onmessage = event => {
      let message; try { message = JSON.parse(event.data); } catch (_) { return; }
      if (message.type === 'snapshot') {
        retry = 1000; connection(true, 'Connected');
        seen = new Set(); feed.replaceChildren(); tasks.clear();
        for (const item of message.history || []) append(item);
        for (const task of message.tasks || []) taskUpdate(task);
        status(message.status); renderTasks();
      } else if (message.type === 'task') taskUpdate(message.task);
      else if (message.type === 'connection') status(message.status);
      else if (message.type === 'status') status(message.status || message);
      else if (['log','sys','file_received'].includes(message.type)) append(message);
    };
    socket.onclose = event => {
      stopVoice(); connection(false, 'Reconnecting');
      if (event.code === 4001 || event.code === 4003) { location.replace('/login'); return; }
      if (event.code === 4008) retry = 30000;
      reconnectTimer = setTimeout(connect, retry); retry = Math.min(retry * 2, 30000);
    };
    socket.onerror = () => connection(false, 'Connection error');
  } catch (failure) {
    connection(false, 'Laptop unreachable'); notice(failure.message);
    reconnectTimer = setTimeout(connect, retry); retry = Math.min(retry * 2, 30000);
  }
}
$('composer').addEventListener('submit', async event => {
  event.preventDefault(); const text = input.value.trim(); if (!text || sending) return;
  if (pending && pending.text !== text) { notice('The previous command has an unconfirmed delivery. Retry that command first; its task will not run twice.'); input.value = pending.text; return; }
  pending ||= {text, request_id:crypto.randomUUID()};
  sessionStorage.setItem('chidvi_pending', JSON.stringify(pending));
  sending = true; send.disabled = true; send.textContent = 'Sending...'; notice();
  try { const result = await api('/api/command', pending); taskUpdate(result.task); clearPending(); }
  catch (failure) {
    if (failure.status && failure.status < 500) clearPending();
    notice(failure.status ? failure.message : 'Delivery is unconfirmed. Check task progress or retry this same command safely.');
  } finally { sending = false; send.disabled = !online; send.textContent = pending ? 'Retry send' : 'Send'; }
});
async function cancelTask(id) {
  stopVoice();
  try { const result = await api('/api/cancel', {task_id:id}); taskUpdate(result.task); notice(); }
  catch (failure) { notice(failure.message); }
}
$('stop').addEventListener('click', () => { if(activeTask) cancelTask(activeTask.id); });
$('disconnect').addEventListener('click', async () => {
  stopVoice();
  try { await api('/api/revoke-devices', {device_id:deviceId}); location.replace('/login'); }
  catch (failure) { notice(failure.message); }
});
$('file').addEventListener('change', async event => {
  const file = event.target.files[0]; event.target.value = ''; if (!file) return;
  if (file.size > 20 * 1024 * 1024) { notice('Choose a file smaller than 20 MB.'); return; }
  const data = new FormData(); data.append('file', file); notice('Sending file...');
  try {
    const response = await fetch('/api/upload', {method:'POST', headers:{'X-CSRF-Token':csrf}, body:data});
    const result = await response.json(); if(!response.ok) throw new Error(result.detail || 'Upload failed.');
    notice('File received on the laptop.');
  } catch (failure) { notice(failure.message); }
});

// Reuse the existing phone PCM conversion and the existing Gemini audio relay.
function pcm16(samples, rate) {
  let converted = samples;
  if (rate !== 16000) {
    const ratio = rate / 16000; converted = new Float32Array(Math.round(samples.length / ratio));
    for(let i=0;i<converted.length;i++) converted[i] = samples[Math.min(Math.round(i * ratio), samples.length-1)];
  }
  const output = new Int16Array(converted.length);
  for(let i=0;i<converted.length;i++) output[i] = Math.max(-32768, Math.min(32767, Math.round(converted[i]*32768)));
  return output.buffer;
}
async function startVoice() {
  if(voice || microphoneStarting) { stopVoice(); return; }
  if(!online) { notice('Reconnect to the laptop before starting the microphone.'); return; }
  if(!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    notice('Microphone access needs trusted HTTPS and browser permission. Text commands still work.'); return;
  }
  microphoneStarting = true;
  try {
    const requested = await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true}});
    if(!microphoneStarting) { requested.getTracks().forEach(track=>track.stop()); return; }
    stream = requested;
    audio = new AudioContext(); await audio.resume();
    voice = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/phone-audio');
    voice.onopen = () => voice.send(JSON.stringify({type:'hello', csrf}));
    voice.onmessage = event => {
      if(JSON.parse(event.data).type !== 'ready') return;
      const source = audio.createMediaStreamSource(stream);
      // shortcut: ScriptProcessor reuses supported browser capture; migrate if a target browser removes it.
      processor = audio.createScriptProcessor(4096, 1, 1);
      processor.onaudioprocess = event => {
        if(voice?.readyState === WebSocket.OPEN && voice.bufferedAmount < 32768) {
          const chunk = new Uint8Array(pcm16(event.inputBuffer.getChannelData(0), audio.sampleRate));
          for(let offset=0;offset<chunk.length;offset+=8192) voice.send(chunk.slice(offset,offset+8192));
        }
      };
      source.connect(processor); processor.connect(audio.destination);
      mic.textContent = 'Stop microphone'; mic.classList.add('recording'); mic.setAttribute('aria-pressed','true');
      $('mic-state').textContent = 'Listening on this phone. Tap Stop microphone to finish.';
      microphoneStarting = false;
    };
    voice.onclose = () => stopVoice();
    voice.onerror = () => { notice('Phone audio disconnected. Tap Microphone to reconnect.'); stopVoice(); };
  } catch(failure) { notice(failure.name === 'NotAllowedError' ? 'Microphone permission denied. Allow it in browser site settings.' : 'Microphone unavailable: ' + failure.message); stopVoice(); }
}
function stopVoice() {
  microphoneStarting = false;
  processor?.disconnect(); processor = null;
  if(audio) audio.close().catch(()=>{}); audio = null;
  stream?.getTracks().forEach(track=>track.stop()); stream = null;
  if(voice) { const previous = voice; voice = null; previous.onclose = null; previous.close(); }
  mic.textContent = 'Microphone'; mic.classList.remove('recording'); mic.setAttribute('aria-pressed','false');
  $('mic-state').textContent = 'Microphone is off. Text commands are always available.';
}
mic.addEventListener('click', startVoice);
window.addEventListener('pagehide', stopVoice);
document.addEventListener('visibilitychange', () => {if(document.hidden) stopVoice();});
connect();
