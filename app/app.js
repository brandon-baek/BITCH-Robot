/* The BITCH app: one page for your phone and the Mac window. Everything live comes over one WebSocket (/link). */
(() => {
const $ = id => document.getElementById(id), $$ = s => [...document.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const api = async (path, body, method) => {
  const r = await fetch(path, body === undefined && !method ? {} : { method: method || 'POST', headers: { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
};
const ago = t => { const s = Date.now() / 1000 - t; return s < 60 ? 'just now' : s < 3600 ? `${Math.round(s / 60)} min ago` : s < 86400 ? `${Math.round(s / 3600)} h ago` : new Date(t * 1000).toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' }) };
const store = { get: (k, d) => { try { const v = localStorage.getItem('bitch.' + k); return v === null ? d : JSON.parse(v) } catch { return d } },
  set: (k, v) => { try { localStorage.setItem('bitch.' + k, JSON.stringify(v)) } catch { } } };
const WORD = { starting: 'Starting', listening: 'Listening', hearing: 'Hearing you', understanding: 'Understanding', thinking: 'Thinking', speaking: 'Speaking', asleep: 'Asleep' };

const S = { hello: null, id: store.get('id', null), status: 'starting', devices: [], people: [], known: [], settings: {}, chat: [], mood: null,
  notes: [], lib: null, labels: {}, pushKey: null, screen: 'talk', inventing: null, online: false };
let ws = null, rig = null;

// ---------------- start: pair or open ----------------
async function boot() {
  try { S.hello = await api('/api/hello') } catch { setTimeout(boot, 2000); return }
  if (!S.hello.paired) return showPair();
  $('pair').hidden = true; $('app').hidden = false;
  rig = Rig($('rig'), { onBoop: () => { if (S.status === 'listening') rig.feel('happy') } });
  rig.level = () => level;
  setup(); connect();
  if ('serviceWorker' in navigator && isSecureContext) navigator.serviceWorker.register('/sw.js').catch(() => { });
}
function showPair() {
  $('pair').hidden = false; $('app').hidden = true;
  const r = Rig($('pairRig')); setTimeout(() => r.gesture && r.lib, 0);
  r.lib = { feelings: {}, gestures: {} }; setInterval(() => r.kick(0.5), 2600);
  $('pairForm').onsubmit = async e => {
    e.preventDefault(); $('pairErr').textContent = '';
    try { await api('/api/pair', { code: $('pairCode').value.trim(), name: deviceName() }); location.reload() }
    catch (err) { $('pairErr').textContent = err.message }
  };
}
function deviceName() {
  const u = navigator.userAgent;
  return /iPhone/.test(u) ? 'iPhone' : /iPad/.test(u) ? 'iPad' : /Android/.test(u) ? 'Android phone' : /Mac/.test(u) ? 'Mac browser' : 'Browser';
}

// ---------------- the link ----------------
let backoff = 500;
function connect() {
  ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/link');
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => { backoff = 500; S.online = true; ws.send(JSON.stringify({ type: 'hello', id: S.id, name: deviceName() })); sendCaps(); if (S.screen === 'see') send({ type: 'view', on: true }); };
  ws.onclose = e => {
    S.online = false; setStatus(); if (e.code === 4401) { location.reload(); return }
    setTimeout(connect, backoff); backoff = Math.min(8000, backoff * 1.7);
  };
  ws.onmessage = e => { if (typeof e.data !== 'string') return onAudio(e.data); handle(JSON.parse(e.data)) };
}
const send = m => { if (ws && ws.readyState === 1) ws.send(typeof m === 'string' || m instanceof ArrayBuffer || ArrayBuffer.isView(m) ? m : JSON.stringify(m)) };

function handle(m) {
  switch (m.type) {
    case 'welcome': S.id = m.id; store.set('id', m.id); S.lib = m.library; S.labels = m.labels; S.pushKey = m.push_key; rig.lib = m.library; rig.feel('happy'); break;
    case 'hello':
      Object.assign(S, { status: m.status, devices: m.devices, people: m.people, notes: m.notes, settings: m.settings, known: m.known, mood: m.mood, chat: m.chat });
      rig.asleep = m.asleep; renderAll(); break;
    case 'status': S.status = m.status; rig.asleep = m.status === 'asleep'; rig.thinking = ['thinking', 'understanding'].includes(m.status); setStatus(); break;
    case 'chat': addChat(m.msg); break;
    case 'express': rig.feel(m.emotion); if (m.move && m.move !== 'none') rig.gesture(m.move); break;
    case 'talking': rig.talk(m.seconds); break;
    case 'perform': rig.play(m.steps); break;
    case 'show': rig.gesture(m.name === 'dance' ? 'wiggle' : m.name === 'sing' ? 'look_around' : 'nod'); if (m.name === 'sleep') rig.asleep = true; if (m.name === 'wake') rig.asleep = false; break;
    case 'inventing': S.inventing = m.what; rig.thinking = true; setStatus(); setTimeout(() => { S.inventing = null; rig.thinking = false; setStatus() }, 25000); break;
    case 'action': if (S.screen === 'life' && /set_timer|remind|reminders|list|teach/.test(m.tool)) loadLife(); break;
    case 'devices': S.devices = m.devices; renderPills(); if (S.screen === 'more') renderDevices(); break;
    case 'people': S.people = m.people; lookAtWatched(); if (S.screen === 'see') { faces = m.people; paintFollow(); paintPeople() } break;
    case 'known': S.known = m.known; paintFollow(true); paintPeople(true); break;
    case 'notes': S.notes = m.notes; renderNotes(); break;
    case 'level': level = m.v; $('lvl').style.width = Math.round(m.v * 100) + '%'; break;
    case 'mood': S.mood = m.mood; renderMood(); break;
    case 'activity': if (S.screen === 'life') loadLife(); break;
    case 'tasks': if (S.screen === 'life') loadLife(); break;
    case 'memories': if (S.screen === 'memory') loadMemory(); break;
    case 'settings': S.settings = m.settings; renderSettings(); break;
    case 'hush': hush(); break;
    case 'snap': snap(); break;
    case 'pushed': renderPhone(); break;
  }
}

// ---------------- screens ----------------
function go(name) {
  if (S.screen === 'see' && name !== 'see') send({ type: 'view', on: false });
  S.screen = name; store.set('screen', name);
  $$('[data-screen]').forEach(s => s.hidden = s.dataset.screen !== name);
  $$('nav.tabs button').forEach(b => b.setAttribute('aria-current', b.dataset.go === name ? 'page' : 'false'));
  $('main').scrollTop = 0;
  if (name === 'see') { send({ type: 'view', on: true }); startLive() }
  if (name === 'life') loadLife();
  if (name === 'memory') loadMemory();
  if (name === 'more') { renderMore() }
  if (name === 'talk') scrollChat(true);
}

function setup() {
  $$('nav.tabs button').forEach(b => b.onclick = () => go(b.dataset.go));
  // talk
  $('say').onsubmit = e => { e.preventDefault(); const v = $('sayText').value.trim(); if (!v) return; unlockAudio(); send({ type: 'say', text: v, speak: spkOn || isDesk() }); $('sayText').value = '' };
  const Q = [['Wave', 'happy', 'wave'], ['Nod', 'happy', 'nod'], ['Wiggle', 'excited', 'wiggle'], ['Look around', 'curious', 'look_around'], ['Spin', 'excited', 'spin'],
    ['Bow', 'proud', 'bow'], ['Dance', 'show', 'dance'], ['Sing', 'show', 'sing'], ['Sleep', 'show', 'sleep'], ['Wake up', 'show', 'wake'], ['Stop', 'stop', '']];
  Q.unshift(['Mute', 'mute', ''], ['Camera off', 'cam', '']);
  $('quick').innerHTML = Q.map(([l, e, a]) => `<button class="chip" type="button" data-e="${e}" data-a="${a}">${l}</button>`).join('');
  $('quick').onclick = e => {
    const b = e.target.closest('button'); if (!b) return;
    if (b.dataset.e === 'stop') { api('/api/act', { do: 'stop' }); hush(); return }
    if (b.dataset.e === 'cam') { saveSet({ camera_on: !(S.settings.camera_on !== false) }); renderSettings(); return }
    if (b.dataset.e === 'mute') { const m = !S.settings.muted; saveSet({ muted: m }); if (m) { api('/api/act', { do: 'stop' }); hush() } renderSettings(); return }
    if (b.dataset.e === 'show') api('/api/act', { do: 'show', name: b.dataset.a });
    else { api('/api/act', { do: 'gesture', emotion: b.dataset.e, move: b.dataset.a }); rig.feel(b.dataset.e) }
  };
  const TRY = ['Remind me in 10 minutes to stretch', 'Do the chicken dance', "What's the weather tomorrow?", 'Remember that my favourite food is pizza', 'Tell me a joke', 'What do you see?'];
  $('try').innerHTML = TRY.map(t => `<button class="chip" type="button">${esc(t)}</button>`).join('');
  $('try').onclick = e => { const b = e.target.closest('button'); if (b) { unlockAudio(); send({ type: 'say', text: b.textContent, speak: spkOn || isDesk() }) } };
  setupMic(); setupCamera();
  spkOn = store.get('speak', false); $('spkBtn').classList.toggle('on', spkOn); $('spkBtn').setAttribute('aria-pressed', spkOn);
  $('spkBtn').onclick = () => { spkOn = !spkOn; store.set('speak', spkOn); unlockAudio(); $('spkBtn').classList.toggle('on', spkOn); $('spkBtn').setAttribute('aria-pressed', spkOn); sendCaps() };
  if (isDesk()) { $('spkBtn').hidden = true; $('camBtn').hidden = true; $('micBtn').hidden = true; $('micHint').textContent = 'The desk mic is always listening. Say "Hey BITCH", or type.' }
  // see
  setupSee();
  // life
  $('chatty').oninput = () => { $('chatv').textContent = chatWord(+$('chatty').value); saveSet({ chattiness: +$('chatty').value }) };
  const hrs = [...Array(24).keys()].map(h => `<option value="${h}">${h % 12 || 12} ${h < 12 ? 'am' : 'pm'}</option>`).join('');
  $('qs').innerHTML = hrs; $('qe').innerHTML = hrs;
  $('qs').onchange = () => saveSet({ quiet_start: +$('qs').value }); $('qe').onchange = () => saveSet({ quiet_end: +$('qe').value });
  $('greet').onchange = () => saveSet({ greet: $('greet').checked }); $('brief').onchange = () => saveSet({ morning_brief: $('brief').checked });
  // memory
  let mt; $('memq').oninput = () => { clearTimeout(mt); mt = setTimeout(loadMemory, 250) };
  $('memAdd').onsubmit = async e => { e.preventDefault(); const v = $('memText').value.trim(); if (!v) return; await api('/api/memories', { text: v, kind: 'fact', importance: 3 }); $('memText').value = ''; loadMemory() };
  // more
  $('invent').onsubmit = async e => { e.preventDefault(); const v = $('inventText').value.trim(); if (!v) return; await api('/api/skills/invent', { what: v }); $('inventText').value = ''; go('talk') };
  $('owner').onchange = () => saveSet({ owner: $('owner').value.trim() || 'Brandon' });
  $('cityForm').onsubmit = async e => { e.preventDefault(); const c = $('city').value.trim(); if (!c) return; const r = await api('/api/settings', { city: c }); $('cityNow').textContent = r.result || ''; $('city').value = '' };
  $$('#voiceSeg button').forEach(b => b.onclick = () => saveSet({ voice: b.dataset.v }));
  $('rob').oninput = () => { $('robv').textContent = Math.round($('rob').value * 100) + '%'; saveSet({ robotness: +$('rob').value }) };
  $('deskmic').onchange = () => saveSet({ mic_on: $('deskmic').checked });
  $('camsw').onchange = () => { saveSet({ camera_on: $('camsw').checked }); renderSettings() };
  $('mutesw').onchange = () => { saveSet({ muted: $('mutesw').checked }); renderSettings() };
  $('cm').onchange = () => saveSet({ chat_model: $('cm').value.trim() }); $('dm').onchange = () => saveSet({ deep_model: $('dm').value.trim() });
  $('quit').onclick = () => { if ($('quit').textContent !== 'Sure?') { $('quit').textContent = 'Sure?'; setTimeout(() => $('quit').textContent = 'Quit BITCH', 2500); return } api('/api/act', { do: 'quit' }) };
  go(store.get('screen', 'talk'));
}
const isDesk = () => S.hello && S.hello.local;
const chatWord = v => v <= 0 ? 'never' : v < 0.3 ? 'rarely' : v < 0.65 ? 'sometimes' : v < 0.9 ? 'often' : 'a lot';
let saveT = {}; function saveSet(o) { Object.assign(S.settings, o); clearTimeout(saveT); saveT = setTimeout(() => api('/api/settings', o), 200) }

function renderAll() { setStatus(); renderMood(); renderNotes(); renderChat(); renderPills(); renderSettings(); if (S.screen === 'more') renderMore(); if (S.screen === 'see') { paintFollow(true); paintPeople(true) } }

// ---------------- talk ----------------
function setStatus() {
  const w = !S.online ? 'Offline' : S.inventing ? 'Inventing' : (WORD[S.status] || S.status);
  $('status').textContent = w;
}
function renderMood() {
  if (!S.mood) return;
  $('mood').textContent = 'Feeling ' + S.mood.words; $('mood2').textContent = 'Right now: ' + S.mood.words;
  const B = [['Energy', 'energy'], ['Happiness', 'happiness'], ['Bored', 'boredom'], ['Wants company', 'social']];
  $('bars').innerHTML = B.map(([l, k]) => `<div class="bar"><span>${l}</span><i><b style="width:${Math.round(S.mood[k] * 100)}%"></b></i></div>`).join('');
}
function renderNotes() {
  const probs = (S.notes || []).filter(n => n.problem && Date.now() / 1000 - n.t < 600).slice(-2);
  $('notes').innerHTML = probs.map(n => `<p>${esc(n.text)}</p>`).join('');
}
function renderPills() {
  const robot = S.devices.find(d => d.kind === 'robot'), cam = S.devices.find(d => d.id === 'mac-camera'), mic = S.devices.find(d => d.id === 'mac-mic');
  const p = [`<span class="pill"><span class="dot ${robot && robot.online ? 'on' : ''}"></span>Robot</span>`];
  if (cam) p.push(`<span class="pill"><span class="dot ${/Watching|Playing/.test(cam.detail) ? 'on' : ''}"></span>Eyes</span>`);
  if (mic) p.push(`<span class="pill"><span class="dot ${mic.detail === 'Listening' && S.settings.mic_on !== false ? 'on' : ''}"></span>Ears</span>`);
  $('pills').innerHTML = p.join(''); $('sidePills').innerHTML = p.join('');
}
function devName(id) { const d = S.devices.find(x => x.id === id); return d ? d.name : '' }
function bubble(m) {
  if (m.role === 'note') return `<div class="msg note">${esc(m.text)}</div>`;
  if (m.role === 'you') {
    const from = m.device && m.device !== S.id ? devName(m.device) : '';
    const how = m.mode === 'ambient' ? 'heard' : m.mode === 'ptt' ? 'said' : '';
    return `<div class="msg you">${esc(m.text)}${from || how ? `<small>${esc([how, from && 'on ' + from].filter(Boolean).join(' '))}</small>` : ''}</div>`;
  }
  const meta = [m.emotion && m.emotion !== 'neutral' ? m.emotion : '', m.move && m.move !== 'none' ? m.move.replace('_', ' ') : ''].filter(Boolean).join(' · ');
  return `<div class="msg bitch">${m.proactive ? `<span class="tag">ON ITS OWN${m.why ? ' · ' + esc(m.why) : ''}</span><br>` : ''}${m.learned ? `<span class="tag">NEW MOVE · ${esc(m.learned)}</span><br>` : ''}${esc(m.text)}${meta ? `<small>${esc(meta)}</small>` : ''}</div>`;
}
function renderChat() {
  const c = $('chat'), msgs = S.chat.slice(-80);
  c.innerHTML = msgs.length ? msgs.map(bubble).join('') : `<p class="empty">Say "Hey BITCH" at the desk, or type below. Try one of these:</p>`;
  $('try').hidden = msgs.filter(m => m.role === 'you').length > 3;
  scrollChat(true);
}
function addChat(m) {
  S.chat.push(m); if (S.chat.length > 200) S.chat.shift();
  const c = $('chat'); if (c.querySelector('.empty')) c.innerHTML = '';
  const near = nearBottom(); c.insertAdjacentHTML('beforeend', bubble(m));
  if (m.role === 'you' && S.chat.filter(x => x.role === 'you').length > 3) $('try').hidden = true;
  if (near || m.device === S.id) scrollChat();
}
const nearBottom = () => { const el = $('main'); return el.scrollHeight - el.scrollTop - el.clientHeight < 160 };
function scrollChat(now) { const el = $('main'); requestAnimationFrame(() => el.scrollTo({ top: el.scrollHeight, behavior: now ? 'auto' : 'smooth' })) }
function lookAtWatched() {
  const t = S.people.find(p => p.target); const c = S.settings.camera;
  if (!t || !c) return;
  const ang = Math.atan2(t.x, Math.max(1e-3, t.y)) * 180 / Math.PI;
  rig.look = { b: Math.max(-0.9, Math.min(0.9, ang / 90 * (c.flip ? -1 : 1))), n: null, t: rig.T };
  rig.gaze = { x: 0, y: t.looking ? 0 : 0.3 };
}

// ---------------- sound out ----------------
let actx = null, level = 0, playing = [], spkOn = false;
function unlockAudio() {
  try {
    if (!actx) actx = new (window.AudioContext || window.webkitAudioContext)();
    if (actx.state === 'suspended') actx.resume();
    const b = actx.createBuffer(1, 1, 22050), s = actx.createBufferSource(); s.buffer = b; s.connect(actx.destination); s.start(0);
  } catch { }
}
document.addEventListener('pointerdown', () => { if (!actx || actx.state !== 'running') unlockAudio() }, { passive: true });
async function onAudio(buf) {
  const u = new Uint8Array(buf); if (u[0] !== 1) return;
  try {
    unlockAudio();
    const ab = await actx.decodeAudioData(buf.slice(1));
    const src = actx.createBufferSource(), an = actx.createAnalyser(); an.fftSize = 512; src.buffer = ab; src.connect(an); an.connect(actx.destination);
    const startAt = Math.max(actx.currentTime, ...playing.map(p => p.end));
    src.start(startAt); const item = { src, end: startAt + ab.duration }; playing.push(item);
    const data = new Float32Array(an.fftSize);
    const meter = () => { if (!playing.includes(item)) return; an.getFloatTimeDomainData(data); let s = 0; for (const v of data) s += v * v; level = Math.min(1, Math.sqrt(s / data.length) * 5); requestAnimationFrame(meter) };
    setTimeout(() => { rig.talk(ab.duration); meter() }, Math.max(0, (startAt - actx.currentTime) * 1000));
    src.onended = () => { playing = playing.filter(p => p !== item); if (!playing.length) level = 0 };
  } catch (e) { console.warn('audio', e) }
}
function hush() { for (const p of playing) try { p.src.stop() } catch { } playing = []; level = 0; rig && rig.talk(0) }

// ---------------- sound in (the phone mic) ----------------
let mic = null, handsFree = false, holding = false;
const WORKLET = `class Down extends AudioWorkletProcessor{constructor(){super();this.r=sampleRate/16000;this.t=0;this.s=0;this.n=0;this.o=[]}
process(i){const x=i[0]&&i[0][0];if(!x)return true;for(let k=0;k<x.length;k++){this.s+=x[k];this.n++;this.t+=1;if(this.t>=this.r){this.t-=this.r;this.o.push(this.s/this.n);this.s=0;this.n=0}}
while(this.o.length>=512){const c=this.o.splice(0,512),b=new Int16Array(513);b[0]=2;for(let k=0;k<512;k++)b[k+1]=Math.max(-1,Math.min(1,c[k]))*32767;this.port.postMessage(b.buffer,[b.buffer])}return true}}
registerProcessor('down',Down)`;
async function openMic() {
  if (mic) return true;
  if (!navigator.mediaDevices || !isSecureContext) { $('micHint').textContent = 'The mic needs the secure link. Set it up in More › This phone.'; go('more'); return false }
  try {
    unlockAudio();
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
    const src = actx.createMediaStreamSource(stream);
    await actx.audioWorklet.addModule(URL.createObjectURL(new Blob([WORKLET], { type: 'application/javascript' })));
    const node = new AudioWorkletNode(actx, 'down');
    node.port.onmessage = e => {                     // tag byte 2 + 16-bit PCM; Int16 frame starts with the tag in its low byte
      if (!handsFree && !holding) return;
      const v = new Int16Array(e.data), out = new Uint8Array(1 + 1024); out[0] = 2; out.set(new Uint8Array(v.buffer, 2), 1); send(out);
      let s = 0; for (let k = 1; k < v.length; k += 8) s += (v[k] / 32768) ** 2; const l = Math.min(1, Math.sqrt(s / 64) * 12); $('lvl').style.width = Math.round(l * 100) + '%';
    };
    src.connect(node); const z = actx.createGain(); z.gain.value = 0; node.connect(z); z.connect(actx.destination);
    mic = { stream, node, src }; return true;
  } catch (e) { $('micHint').textContent = 'Microphone blocked: ' + e.message; return false }
}
function closeMic() { if (!mic) return; mic.stream.getTracks().forEach(t => t.stop()); try { mic.node.disconnect() } catch { } mic = null }
function setupMic() {
  const b = $('micBtn'); let t = null, down = 0;
  b.addEventListener('pointerdown', e => { e.preventDefault(); down = Date.now(); t = setTimeout(async () => { if (await openMic()) { holding = true; hush(); send({ type: 'ptt', on: true }); sendCaps(); paintMic() } }, 260) });
  const up = async () => {
    clearTimeout(t);
    if (holding) { holding = false; send({ type: 'ptt', on: false }); if (!handsFree) setTimeout(() => { if (!holding && !handsFree) { closeMic(); sendCaps() } }, 4000); paintMic(); return }
    if (Date.now() - down < 400) {
      if (handsFree) { handsFree = false; closeMic() } else if (await openMic()) handsFree = true;
      sendCaps(); paintMic();
    }
  };
  b.addEventListener('pointerup', up); b.addEventListener('pointercancel', up); b.addEventListener('contextmenu', e => e.preventDefault());
}
function paintMic() {
  const b = $('micBtn'); b.classList.toggle('hold', holding); b.classList.toggle('on', handsFree && !holding);
  $('micHint').textContent = holding ? 'Listening while you hold…' : handsFree ? 'Hands-free: just talk. Tap the mic to stop.' : 'Tap the mic for hands-free, or hold it to talk.';
}
function sendCaps() {
  const c = []; if (mic && (handsFree || holding)) c.push('ears'); if (spkOn || (mic && (handsFree || holding))) c.push('voice'); if (cam) c.push('eyes');
  if (!isDesk()) send({ type: 'caps', caps: c });
}

// ---------------- the phone camera ----------------
let cam = null;
function setupCamera() {
  $('camBtn').onclick = async () => {
    if (cam) { cam.getTracks().forEach(t => t.stop()); cam = null; $('camthumb').hidden = true; $('camBtn').classList.remove('on'); sendCaps(); return }
    if (!navigator.mediaDevices || !isSecureContext) { go('more'); return }
    try {
      cam = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: 'environment' }, width: { ideal: 1280 } }, audio: false });
      $('camvid').srcObject = cam; await $('camvid').play(); $('camthumb').hidden = false; $('camBtn').classList.add('on'); sendCaps();
    } catch (e) { $('micHint').textContent = 'Camera blocked: ' + e.message }
  };
}
function snap() {
  const v = $('camvid'); if (!cam || !v.videoWidth) return;
  const c = document.createElement('canvas'), w = Math.min(1024, v.videoWidth); c.width = w; c.height = Math.round(v.videoHeight * w / v.videoWidth);
  c.getContext('2d').drawImage(v, 0, 0, c.width, c.height); rig.flash = 0.9;
  c.toBlob(async b => { const d = new Uint8Array(await b.arrayBuffer()), out = new Uint8Array(d.length + 1); out[0] = 3; out.set(d, 1); send(out) }, 'image/jpeg', 0.85);
}

// ---------------- see: live view, people, desk map ----------------
let faces = [], liveOn = false, bm = null, frames = 0, lastF = '', lastP = '';
const mirror = b => [1 - b[0] - b[2], b[1], b[2], b[3]];
async function startLive() {
  if (liveOn) return; liveOn = true; let seq = 0, t1 = performance.now();
  const fps = setInterval(() => { const n = performance.now(); $('fps').textContent = bm ? Math.round(frames * 1000 / (n - t1)) + ' fps' : ''; frames = 0; t1 = n }, 1000);
  while (S.screen === 'see') {
    try {
      const r = await fetch('/api/frame?after=' + seq);
      if (r.status === 200) {
        seq = +r.headers.get('X-Seq'); faces = JSON.parse(r.headers.get('X-Faces') || '[]');
        const nb = await createImageBitmap(await r.blob()); bm && bm.close && bm.close(); bm = nb; frames++; drawLive(); $('camoff').hidden = true;
      } else { $('camoff').hidden = !!bm; await new Promise(z => setTimeout(z, 800)) }
    } catch { await new Promise(z => setTimeout(z, 1200)) }
  }
  clearInterval(fps); liveOn = false;
}
function drawLive() {
  const cv = $('cv'), g = cv.getContext('2d'), r = cv.getBoundingClientRect(), d = devicePixelRatio || 1;
  if (cv.width !== Math.round(r.width * d)) { cv.width = r.width * d; cv.height = r.height * d }
  const W = cv.width, H = cv.height; g.save(); g.translate(W, 0); g.scale(-1, 1); g.drawImage(bm, 0, 0, W, H); g.restore();
  for (const p of faces) {
    const [x, y, w, h] = mirror(p.box).map((v, i) => v * (i % 2 ? H : W)), col = p.target ? '#f4c430' : '#2d6cdf', L = Math.min(w, h) * .28;
    g.strokeStyle = col; g.lineWidth = (p.target ? 4 : 3) * d; g.lineCap = 'round'; g.beginPath();
    for (const [cx, cy, sx, sy] of [[x, y, 1, 1], [x + w, y, -1, 1], [x, y + h, 1, -1], [x + w, y + h, -1, -1]]) { g.moveTo(cx, cy + sy * L); g.lineTo(cx, cy); g.lineTo(cx + sx * L, cy) }
    g.stroke();
    const lab = `${p.name || 'Someone'} · ${(p.d / 100).toFixed(1)} m${p.looking ? ' · looking at BITCH' : ''}`;
    g.font = `600 ${13 * d}px "Helvetica Neue",Helvetica,Arial`; const tw = g.measureText(lab).width + 16 * d, ty = Math.max(4 * d, y - 30 * d), lx = Math.max(4 * d, Math.min(x, W - tw - 4 * d));
    g.fillStyle = col; g.beginPath(); g.roundRect(lx, ty, tw, 24 * d, 12 * d); g.fill(); g.fillStyle = p.target ? '#1f2a44' : '#fff'; g.fillText(lab, lx + 8 * d, ty + 16.5 * d);
  }
}
function paintFollow(force) {
  const f = S.settings.follow || { mode: 'closest' }, key = JSON.stringify([f, S.known, faces.map(p => p.name)]);
  if (!force && key === lastF) return; lastF = key;
  const here = new Set(faces.map(p => p.name).filter(Boolean));
  let h = `<button class="chip ${f.mode === 'closest' ? 'on' : ''}" data-m="closest">Whoever's closest</button>`;
  for (const n of S.known) h += `<button class="chip ${f.mode === 'person' && f.who === n ? 'on' : ''}" data-m="person" data-w="${esc(n)}"><img src="/api/face/${encodeURIComponent(n)}" alt="" onerror="this.remove()">${esc(n)}${here.has(n) ? '' : ' (away)'}</button>`;
  h += `<button class="chip ${f.mode === 'off' ? 'on' : ''}" data-m="off">Nobody</button>`; $('follow').innerHTML = h;
}
function paintPeople(force) {
  const key = JSON.stringify([S.known, faces.map(p => [p.name, p.target, p.looking])]); if (!force && key === lastP) return; lastP = key;
  if (!S.known.length) { $('people').innerHTML = '<p class="empty" style="grid-column:1/-1">Nobody yet. Tap a face in the live view and type their name, or say "my name is …" to BITCH.</p>'; return }
  $('people').innerHTML = S.known.map(n => { const p = faces.find(q => q.name === n); return `<div class="person ${p ? (p.target ? 'watch' : 'here') : ''}"><img src="/api/face/${encodeURIComponent(n)}" alt="" onerror="this.outerHTML='<div class=ph></div>'"><b>${esc(n)}</b><span>${p ? (p.looking ? 'looking at BITCH' : 'here') : 'away'}</span><br><button type="button" class="btn ghost small" data-forget="${esc(n)}" style="margin-top:6px">Forget</button></div>` }).join('');
}
function follow(mode, who) { S.settings.follow = { mode, who: who ?? null }; api('/api/act', { do: 'follow', mode, who }); paintFollow(true) }
function setupSee() {
  $('follow').onclick = e => { const b = e.target.closest('.chip'); if (b && b.dataset.m) follow(b.dataset.m, b.dataset.w) };
  $('people').onclick = e => { const b = e.target.closest('[data-forget]'); if (!b) return; if (b.textContent !== 'Sure?') { b.textContent = 'Sure?'; setTimeout(() => b.isConnected && (b.textContent = 'Forget'), 2500); return } api('/api/act', { do: 'forget_face', name: b.dataset.forget }) };
  const pop = $('facePop'); let pf = null;
  $('cv').onclick = e => {
    const r = $('cv').getBoundingClientRect(), mx = (e.clientX - r.left) / r.width, my = (e.clientY - r.top) / r.height;
    const p = faces.find(q => { const [x, y, w, h] = mirror(q.box); return mx >= x - .02 && mx <= x + w + .02 && my >= y - .06 && my <= y + h + .02 });
    if (!p) { pop.classList.remove('show'); return } pf = p; $('popT').textContent = p.name || 'Someone new'; $('popName').value = p.name || ''; $('popForget').hidden = !p.name;
    pop.style.left = Math.min(r.width - 250, Math.max(8, e.clientX - r.left - 120)) + 'px'; pop.style.top = Math.min(r.height - 170, e.clientY - r.top + 12) + 'px'; pop.classList.add('show');
  };
  $('popFollow').onclick = () => { follow('person', pf.name || pf.id); pop.classList.remove('show') };
  $('popForm').onsubmit = e => { e.preventDefault(); const n = $('popName').value.trim(); if (n) api('/api/act', { do: 'learn', name: n, id: pf.id }); pop.classList.remove('show') };
  $('popForget').onclick = () => { api('/api/act', { do: 'forget_face', name: pf.name }); pop.classList.remove('show') };
  const cfg = () => S.settings.camera, saveCam = () => saveSet({ camera: cfg() });
  $('camz').oninput = () => { cfg().z = +$('camz').value; paintCam(); saveCam() };
  $('camfov').oninput = () => { cfg().fov = +$('camfov').value; paintCam(); saveCam() };
  $('camflip').onchange = () => { cfg().flip = $('camflip').checked; saveCam() };
  $('calib').onclick = () => { api('/api/act', { do: 'calibrate' }); $('calib').textContent = 'Got it'; setTimeout(() => $('calib').textContent = "I'm looking at BITCH right now", 1500) };
  setupMap();
}
function paintCam() {
  const c = S.settings.camera; if (!c) return;
  $('camz').value = c.z; $('camzv').textContent = c.z > 0 ? c.z + ' cm higher' : c.z < 0 ? (-c.z) + ' cm lower' : 'same height';
  $('camfov').value = c.fov; $('camfovv').textContent = c.fov + '°'; $('camflip').checked = !!c.flip;
}
function setupMap() {
  const c = $('map'), x = c.getContext('2d'); let W = 0, H = 0, Sc = 2, OX = 0, OY = 0, drag = null, aim = null;
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches, t0 = performance.now();
  const K = () => ({ ink: getComputedStyle(document.documentElement).getPropertyValue('--ink').trim(), soft: '#5d6b84', kraft: '#b98b54', kraft2: '#8a6235', cam: '#2d6cdf', you: '#f4c430', line: getComputedStyle(document.documentElement).getPropertyValue('--line').trim() });
  function fit() { const r = c.getBoundingClientRect(), d = devicePixelRatio || 1; W = r.width; H = r.height; c.width = W * d; c.height = H * d; x.setTransform(d, 0, 0, d, 0, 0); Sc = Math.min(W / 340, H / 240); OX = W / 2; OY = H - 36 * Sc }
  new ResizeObserver(fit).observe(c);
  const w2s = (a, b) => [OX + a * Sc, OY - b * Sc], s2w = (a, b) => [(a - OX) / Sc, (OY - b) / Sc];
  const rr = (a, b, w, h, r) => { x.beginPath(); x.roundRect(a - w / 2, b - h / 2, w, h, r) };
  const cm = () => S.settings.camera;
  const handle = () => { const th = cm().yaw * Math.PI / 180, [cx, cy] = w2s(cm().x, cm().y); return [cx + 16.5 * Sc * Math.sin(th), cy - 16.5 * Sc * Math.cos(th)] };
  function draw(now) {
    requestAnimationFrame(draw); if (S.screen !== 'see' || !cm() || !W) return; const k = K(); x.clearRect(0, 0, W, H);
    x.fillStyle = k.line; { const [x0, y1] = s2w(0, 0), [x1, y0] = s2w(W, H); for (let gx = Math.ceil(x0 / 10) * 10; gx <= x1; gx += 10) for (let gy = Math.ceil(y0 / 10) * 10; gy <= y1; gy += 10) { const [a, b] = w2s(gx, gy); x.beginPath(); x.arc(a, b, 1, 0, 7); x.fill() } }
    const [rx, ry] = w2s(0, 0); x.strokeStyle = k.soft; x.lineWidth = 1; x.setLineDash([3, 6]); x.font = '12px "Helvetica Neue",Helvetica,Arial'; x.fillStyle = k.soft;
    for (const [r, l] of [[50, '50 cm'], [100, '1 m'], [150, '1.5 m'], [200, '2 m']]) { x.beginPath(); x.arc(rx, ry, r * Sc, Math.PI * 1.08, Math.PI * 1.92); x.stroke(); x.fillText(l, rx + 4, ry - r * Sc - 5) } x.setLineDash([]);
    const C = cm(), [cx, cy] = w2s(C.x, C.y), th = C.yaw * Math.PI / 180, f = C.fov * Math.PI / 360, L = Math.max(W, H) * 1.5;
    const gr = x.createRadialGradient(cx, cy, 0, cx, cy, L * .5); gr.addColorStop(0, 'rgba(45,108,223,.24)'); gr.addColorStop(1, 'rgba(45,108,223,0)'); x.fillStyle = gr; x.beginPath(); x.moveTo(cx, cy); x.lineTo(cx + L * Math.sin(th - f), cy - L * Math.cos(th - f)); x.lineTo(cx + L * Math.sin(th + f), cy - L * Math.cos(th + f)); x.closePath(); x.fill();
    const tp = faces.find(p => p.target), tg = aim || (tp ? [tp.x, tp.y] : null);
    if (tg) { const [px, py] = w2s(tg[0], tg[1]); x.strokeStyle = 'rgba(244,196,48,.9)'; x.lineWidth = 4; x.lineCap = 'round'; x.beginPath(); x.moveTo(rx, ry); x.lineTo(px, py); x.stroke() }
    const ang = tg ? Math.atan2(tg[0], Math.max(1, tg[1])) : 0; x.fillStyle = '#fff'; x.strokeStyle = '#1f2a44'; x.lineWidth = 2; rr(rx, ry + 6 * Sc, 34 * Sc, 22 * Sc, 4 * Sc); x.fill(); x.stroke();
    x.save(); x.translate(rx, ry); x.rotate(ang); x.fillStyle = k.kraft; x.beginPath(); x.arc(0, 0, 7.5 * Sc, 0, 7); x.fill(); x.fillStyle = k.kraft2; x.fillRect(-1.2 * Sc, -15 * Sc, 2.4 * Sc, 15 * Sc); x.fillStyle = k.kraft; rr(0, -17 * Sc, 13 * Sc, 3 * Sc, Sc); x.fill();
    for (const e of [-3, 3]) { x.fillStyle = '#fff'; x.beginPath(); x.arc(e * Sc, -19.6 * Sc, 2 * Sc, 0, 7); x.fill(); x.strokeStyle = '#1f2a44'; x.lineWidth = 1; x.stroke(); x.fillStyle = '#1f2a44'; x.beginPath(); x.arc(e * Sc, -20.4 * Sc, .9 * Sc, 0, 7); x.fill() } x.restore();
    x.save(); x.translate(cx, cy); x.rotate(th); x.fillStyle = k.cam; rr(0, 0, 12 * Sc, 8 * Sc, 2 * Sc); x.fill(); x.fillStyle = '#fff'; x.beginPath(); x.arc(0, -4 * Sc, 2.2 * Sc, 0, 7); x.fill(); x.fillStyle = k.cam; x.beginPath(); x.arc(0, -4 * Sc, 1.1 * Sc, 0, 7); x.fill();
    x.strokeStyle = k.cam; x.lineWidth = 2; x.beginPath(); x.moveTo(0, -6.5 * Sc); x.lineTo(0, -15 * Sc); x.stroke(); x.fillStyle = '#fff'; x.beginPath(); x.arc(0, -16.5 * Sc, 3 * Sc, 0, 7); x.fill(); x.stroke(); x.restore();
    const ph = reduce ? 0 : ((now - t0) / 1400) % 1; x.textAlign = 'center';
    for (const q of faces) {
      const [px, py] = w2s(q.x, q.y), R = 4.5 * Sc, col = q.target ? k.you : k.cam;
      if (q.target) { x.strokeStyle = `rgba(244,196,48,${.7 * (1 - ph)})`; x.lineWidth = 2; x.beginPath(); x.arc(px, py, R + 7 * Sc * ph, 0, 7); x.stroke() }
      if (q.looking) { x.strokeStyle = k.ink; x.lineWidth = 1.5; x.beginPath(); x.arc(px, py, R + 3.5 * Sc, 0, 7); x.stroke() }
      x.fillStyle = col; x.beginPath(); x.arc(px, py, R, 0, 7); x.fill(); x.strokeStyle = '#1f2a44'; x.lineWidth = 2; x.stroke();
      x.fillStyle = k.ink; x.font = '600 12px "Helvetica Neue",Helvetica,Arial'; x.fillText(`${q.name || 'Someone'} · ${(q.d / 100).toFixed(1)} m`, px, py + R + 16);
    }
    x.textAlign = 'start';
    if (aim) { const [a, b] = w2s(aim[0], aim[1]); x.strokeStyle = k.ink; x.lineWidth = 2; x.beginPath(); x.arc(a, b, 8, 0, 7); x.moveTo(a - 14, b); x.lineTo(a + 14, b); x.moveTo(a, b - 14); x.lineTo(a, b + 14); x.stroke() }
  }
  const pos = e => { const r = c.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top] };
  c.onpointerdown = e => {
    if (!cm()) return; const [sx, sy] = pos(e), [hx, hy] = handle(), [cx, cy] = w2s(cm().x, cm().y);
    if (Math.hypot(sx - hx, sy - hy) < 16) drag = 'aim'; else if (Math.hypot(sx - cx, sy - cy) < 10 * Sc) drag = 'move';
    else { const [wx, wy] = s2w(sx, sy); aim = [wx, wy]; api('/api/act', { do: 'aim', x: wx, y: wy }); S.settings.follow = { mode: 'off' }; paintFollow(true); setTimeout(() => aim = null, 2500); return }
    c.setPointerCapture(e.pointerId);
  };
  c.onpointermove = e => {
    if (!cm()) return; const [sx, sy] = pos(e);
    if (!drag) { const [hx, hy] = handle(), [cx, cy] = w2s(cm().x, cm().y); c.style.cursor = Math.hypot(sx - hx, sy - hy) < 16 ? 'grab' : Math.hypot(sx - cx, sy - cy) < 10 * Sc ? 'move' : 'crosshair'; return }
    if (drag === 'move') { const [wx, wy] = s2w(sx, sy); cm().x = Math.round(wx); cm().y = Math.round(wy) } else { const [cx, cy] = w2s(cm().x, cm().y); cm().yaw = Math.round(Math.atan2(sx - cx, cy - sy) * 180 / Math.PI) }
  };
  c.onpointerup = () => { if (drag) { drag = null; saveSet({ camera: cm() }) } };
  requestAnimationFrame(draw);
}

// ---------------- life ----------------
async function loadLife() {
  let d; try { d = await api('/api/life') } catch { return }
  S.mood = d.mood; renderMood();
  const T = d.tasks.filter(t => ['timer', 'reminder', 'routine'].includes(t.kind));
  $('tasks').innerHTML = T.length ? T.map(t => `<div class="item"><div class="txt"><span class="kind">${t.kind}</span>${esc(t.text)}<span class="meta">${esc(t.rule ? t.rule + ' · next ' + t.nice : t.nice)}</span></div><div class="acts"><button class="btn ghost small" data-cancel="${t.id}">Cancel</button></div></div>`).join('')
    : '<p class="empty">Nothing yet. Try "remind me at 7 to call mum" or "every weekday at 8 tell me the weather".</p>';
  const L = Object.entries(d.lists);
  $('lists').innerHTML = (L.length ? L.map(([n, items]) => `<div class="card"><div class="lab" style="margin-bottom:8px">${esc(n[0].toUpperCase() + n.slice(1))}<button class="btn ghost small" data-clear="${esc(n)}">Clear</button></div>
    <div class="chips">${items.map(i => `<button class="chip" data-list="${esc(n)}" data-item="${esc(i)}" title="Tap to tick off">${esc(i)}</button>`).join('') || '<span class="empty">Empty</span>'}</div>
    <form class="row" data-add="${esc(n)}" style="margin-top:8px;flex-wrap:nowrap;gap:8px"><input type="text" placeholder="Add to ${esc(n)}" autocomplete="off"><button class="btn small">Add</button></form></div>`).join('')
    : '<p class="empty">No lists yet. Try "add milk to my shopping list".</p>');
  $('acts').innerHTML = d.activity.length ? d.activity.slice(0, 30).map(a => `<div class="item"><div class="txt"><span class="kind">${esc(a.kind)}</span>${esc(a.text)}<span class="meta">${esc(a.why || '')}${a.why ? ' · ' : ''}${ago(a.t)}</span></div>
    <div class="acts">${a.feedback ? `<span class="hint">${a.feedback === 'like' ? 'Liked' : 'Noted, won’t'}</span>` : `<button class="btn ghost small" data-fb="like" data-id="${a.id}">Good</button><button class="btn ghost small" data-fb="dislike" data-id="${a.id}">Don’t</button>`}</div></div>`).join('')
    : '<p class="empty">Nothing yet. It will greet you, ring reminders and speak up now and then.</p>';
  const s = S.settings; $('chatty').value = s.chattiness; $('chatv').textContent = chatWord(s.chattiness); $('qs').value = s.quiet_start; $('qe').value = s.quiet_end;
  $('greet').checked = !!s.greet; $('brief').checked = !!s.morning_brief;
}
document.addEventListener('click', async e => {
  const b = e.target.closest('[data-cancel],[data-fb],[data-list],[data-clear]'); if (!b) return;
  if (b.dataset.cancel) await api('/api/tasks/' + b.dataset.cancel, undefined, 'DELETE');
  if (b.dataset.fb) await api('/api/activity/' + b.dataset.id, { feedback: b.dataset.fb });
  if (b.dataset.list) await api('/api/lists/' + encodeURIComponent(b.dataset.list), { remove: b.dataset.item });
  if (b.dataset.clear) await api('/api/lists/' + encodeURIComponent(b.dataset.clear), { clear: true });
  loadLife();
});
document.addEventListener('submit', async e => {
  const f = e.target.closest('[data-add]'); if (!f) return; e.preventDefault();
  const v = f.querySelector('input').value.trim(); if (v) { await api('/api/lists/' + encodeURIComponent(f.dataset.add), { add: v }); loadLife() }
});

// ---------------- memory ----------------
async function loadMemory() {
  let all; try { all = await api('/api/memories' + ($('memq').value.trim() ? '?q=' + encodeURIComponent($('memq').value.trim()) : '')) } catch { return }
  const row = m => `<div class="item" data-mem="${m.id}"><div class="txt"><span contenteditable="true" spellcheck="false" data-edit="${m.id}">${esc(m.text)}</span><span class="meta">${m.kind === 'feedback' ? 'how you want it to behave · ' : ''}${ago(m.t)}${m.importance >= 5 ? ' · core' : ''}</span></div>
    <div class="acts"><button class="btn ghost small" data-del="${m.id}" aria-label="Forget this">Forget</button></div></div>`;
  const facts = all.filter(m => !['diary', 'episode'].includes(m.kind)), diary = all.filter(m => m.kind === 'diary'), eps = all.filter(m => m.kind === 'episode');
  $('memFacts').innerHTML = facts.map(row).join('') || '<p class="empty">Nothing yet. Tell it things, like "my sister is Chloe", and it remembers.</p>';
  $('memDiary').innerHTML = diary.map(row).join('') || '<p class="empty">BITCH writes a little diary entry each night.</p>';
  $('memEps').innerHTML = eps.slice(0, 20).map(row).join('') || '<p class="empty">Short summaries of your chats show up here.</p>';
}
document.addEventListener('focusout', e => {
  const el = e.target.closest('[data-edit]'); if (!el) return;
  const t = el.textContent.trim(); if (t) api('/api/memories/' + el.dataset.edit, { text: t }, 'PATCH');
});
document.addEventListener('keydown', e => { if (e.key === 'Enter' && e.target.closest('[data-edit]')) { e.preventDefault(); e.target.blur() } });
document.addEventListener('click', async e => {
  const b = e.target.closest('[data-del]'); if (!b) return;
  if (b.textContent !== 'Sure?') { b.textContent = 'Sure?'; setTimeout(() => b.isConnected && (b.textContent = 'Forget'), 2500); return }
  await api('/api/memories/' + b.dataset.del, undefined, 'DELETE'); b.closest('.item').remove();
});

// ---------------- more ----------------
function renderMore() { renderPairing(); renderPhone(); loadSkills(); renderDevices(); renderSettings() }
let pairTimer = null;
async function renderPairing() {
  if (!isDesk()) { $('pairPanel').hidden = true; return }
  $('pairPanel').hidden = false; $('quit').hidden = false;
  try {
    const p = await api('/api/pair-code'); $('pairBig').textContent = p.code; $('pairUrl').textContent = `http://${p.host}:${p.http}`;
    $('pairLeft').textContent = 'The code changes every 10 minutes. Phones only need to pair once.';
  } catch { }
  clearTimeout(pairTimer); pairTimer = setTimeout(() => S.screen === 'more' && renderPairing(), 30000);
}
function renderPhone() {
  if (isDesk()) { $('phonePanel').hidden = true; return }
  $('phonePanel').hidden = false;
  const h = S.hello, https = `https://${h.host}:${h.https}`, ios = /iPhone|iPad/.test(navigator.userAgent);
  const standalone = matchMedia('(display-mode: standalone)').matches || navigator.standalone;
  let html = '';
  if (!isSecureContext) {
    html = `<p>To talk out loud, use the camera and get notifications, set up the secure link once (about a minute):</p><ol class="steps">
      <li><a href="/BITCH-Home.crt">Download BITCH's certificate</a> and allow it.</li>
      ${ios ? `<li>Open <b>Settings</b>, tap <b>Profile Downloaded</b> near the top, then <b>Install</b>.</li>
      <li>In <b>Settings › General › About › Certificate Trust Settings</b>, turn on <b>BITCH Home</b>.</li>` : `<li>Open the downloaded file and install it as a CA certificate.</li>`}
      <li>Open <a href="${https}">${esc(https)}</a> and pair once more.</li></ol>
      <p class="hint" style="margin-top:8px">It only works on your home Wi-Fi and never leaves your network.</p>`;
  } else {
    const dev = S.devices.find(d => d.id === S.id) || {}, pushOn = (dev.caps || []).includes('notify');
    html = `<p class="hint" style="margin-bottom:8px">Secure link is on. Mic, camera and voice work here.</p>`;
    if (ios && !standalone) html += `<p>For notifications, add BITCH to your Home Screen: tap <b>Share</b>, then <b>Add to Home Screen</b>, and open it from there.</p>`;
    else if ('PushManager' in window) html += `<label class="sw">Notifications<small>Reminders, timers and BITCH's messages when you're away</small><input type="checkbox" id="pushSw" ${pushOn ? 'checked' : ''}></label>`;
  }
  $('phoneSetup').innerHTML = html;
  const sw = $('pushSw'); if (sw) sw.onchange = () => sw.checked ? enablePush().catch(e => { sw.checked = false; alert(e.message) }) : disablePush();
}
async function enablePush() {
  const perm = await Notification.requestPermission(); if (perm !== 'granted') throw new Error('Notifications are blocked in Settings.');
  const reg = await navigator.serviceWorker.register('/sw.js'); await navigator.serviceWorker.ready;
  const key = Uint8Array.from(atob(S.pushKey.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - S.pushKey.length % 4) % 4)), c => c.charCodeAt(0));
  const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
  send({ type: 'push', sub: sub.toJSON() });
}
async function disablePush() { const reg = await navigator.serviceWorker.getRegistration(); const s = reg && await reg.pushManager.getSubscription(); if (s) await s.unsubscribe(); send({ type: 'unpush' }) }
async function loadSkills() {
  let ks; try { ks = await api('/api/skills') } catch { return }
  const built = Object.keys((S.lib || {}).gestures || {});
  $('skills').innerHTML = (ks.length ? ks.map(k => `<div class="item"><div class="txt"><span class="kind">${k.kind === 'move' ? 'move' : 'routine'}</span>${esc(k.name)}<span class="meta">${esc(k.kind === 'move' ? (k.body.asked ? 'invented for "' + k.body.asked + '"' : k.description) : k.body.steps)}${k.uses ? ' · used ' + k.uses + '×' : ''}</span></div>
    <div class="acts"><button class="btn small" data-play="${esc(k.name)}">${k.kind === 'move' ? 'Play' : 'Run'}</button><button class="btn ghost small" data-delskill="${esc(k.name)}">Delete</button></div></div>`).join('') : '<p class="empty">Nothing invented yet. Ask it for any move, like "do the robot".</p>')
    + `<div><p class="hint" style="margin:6px 0 8px">Built in</p><div class="chips">${built.map(b => `<button class="chip" data-play="${b}">${b.replace('_', ' ')}</button>`).join('')}<button class="chip" data-show="dance">dance</button><button class="chip" data-show="sing">sing</button></div></div>`;
}
document.addEventListener('click', async e => {
  const p = e.target.closest('[data-play]'), d = e.target.closest('[data-delskill]'), sh = e.target.closest('[data-show]');
  if (p) { api('/api/skills/play', { name: p.dataset.play }); go('talk') }
  if (sh) { api('/api/act', { do: 'show', name: sh.dataset.show }); go('talk') }
  if (d) { if (d.textContent !== 'Sure?') { d.textContent = 'Sure?'; setTimeout(() => d.isConnected && (d.textContent = 'Delete'), 2500); return } await api('/api/skills/' + encodeURIComponent(d.dataset.delskill), undefined, 'DELETE'); loadSkills() }
});
function renderDevices() {
  const L = S.labels || {};
  $('devices').innerHTML = S.devices.map(d => `<div class="item"><div class="txt"><b>${esc(d.name)}</b>${d.id === S.id ? ' <span class="hint">(this one)</span>' : ''}
    <span class="meta"><span class="dot ${d.online ? 'on' : ''}" style="display:inline-block;margin-right:6px"></span>${esc(d.detail || (d.online ? 'Connected' : 'Offline'))}</span>
    <div class="chips" style="margin-top:8px">${d.caps.map(c => `<button class="chip ${d.off.includes(c) ? '' : 'on'}" data-dev="${esc(d.id)}" data-cap="${c}" aria-pressed="${!d.off.includes(c)}" title="Tap to switch ${d.off.includes(c) ? 'on' : 'off'}">${esc(L[c] || c)}: ${d.off.includes(c) ? 'off' : 'on'}</button>`).join('')}</div></div>
    ${d.kind === 'phone' && d.id !== S.id ? `<div class="acts"><button class="btn ghost small" data-forgetdev="${esc(d.id)}">Forget</button></div>` : ''}</div>`).join('');
}
document.addEventListener('click', async e => {
  const c = e.target.closest('[data-cap]'), f = e.target.closest('[data-forgetdev]');
  if (c) api('/api/act', { do: 'device', id: c.dataset.dev, cap: c.dataset.cap, on: c.getAttribute('aria-pressed') !== 'true' });
  if (f) api('/api/act', { do: 'forget_device', id: f.dataset.forgetdev });
});
function renderSettings() {
  const s = S.settings; if (!s || !$('owner')) return;
  if (document.activeElement !== $('owner')) $('owner').value = s.owner || '';
  $('cityNow').textContent = s.location ? s.location.name : 'not set';
  $$('#voiceSeg button').forEach(b => b.classList.toggle('on', b.dataset.v === s.voice));
  $('rob').value = s.robotness; $('robv').textContent = Math.round(s.robotness * 100) + '%';
  $('deskmic').checked = s.mic_on !== false; $('mutesw').checked = !!s.muted;
  const cb = document.querySelector('#quick [data-e=cam]'); if (cb) { cb.textContent = s.camera_on === false ? 'Camera on' : 'Camera off'; cb.classList.toggle('on', s.camera_on === false) }
  $('camsw').checked = s.camera_on !== false;
  const mb = document.querySelector('#quick [data-e=mute]'); if (mb) { mb.textContent = s.muted ? 'Unmute' : 'Mute'; mb.classList.toggle('on', !!s.muted) }
  $('mood').textContent = s.muted ? 'Muted: answers here in text only' : (S.mood ? 'Feeling ' + S.mood.words : ''); $('cm').value = s.chat_model || ''; $('dm').value = s.deep_model || '';
  paintCam(); renderPills();
}

boot();
})();
