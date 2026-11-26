'use strict';

const $ = id => document.getElementById(id);
let socket, browserId, browserDevice, hello, mic, camera, calibrating = false, captureCount = 0;
let starting = new Set();
let previewSource = 'off';
let frameId = 0;
let settingsVersion = 0;
let latest = null;

async function api(path, body) {
  const options = body === undefined ? {cache: 'no-store'} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
  };
  const response = await fetch('/api/' + path, options);
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'BITCH isn’t reachable right now.');
  }
  return response;
}

function fail(error) {
  $('error').textContent = error.message;
  $('error').hidden = false;
  clearTimeout(fail.timer);
  fail.timer = setTimeout(() => { $('error').hidden = true; }, 6000);
}

function action(id, event, fn) {
  $(id).addEventListener(event, async e => {
    if (event !== 'change' && event !== 'input') e.preventDefault();
    $('error').hidden = true;
    try { await fn(e); } catch (error) { fail(error); }
  });
}

function options(id, entries, selection) {
  const node = $(id);
  if (document.activeElement === node && node.options.length) return;
  if (!entries.some(([value]) => value === selection)) entries.push([selection, 'Disconnected']);
  const signature = JSON.stringify(entries);
  if (signature !== node.dataset.entries) {
    node.replaceChildren(...entries.map(([value, label]) => new Option(label, value)));
    node.dataset.entries = signature;
  }
  node.value = selection;
}

function setIcon(button, icon) {
  button.querySelector('use').setAttribute('href', '#' + icon);
}

// The turn, told honestly: what BITCH is doing right now.
function mood(state) {
  const settings = state.settings;
  const phase = state.turn_phase;
  const micDevice = state.devices.find(device => device.id === settings.mic_source);
  if (state.show_active) return ['speaking', 'Performing'];
  if (state.status === 'Speaking') return ['speaking', 'Talking'];
  if (state.status === 'Thinking' || state.status === 'Answering') return ['thinking', 'Thinking'];
  if (settings.mic_muted) return ['off', 'Your mic is off'];
  if (settings.mic_source === 'off' || !micDevice) return ['off', 'No microphone'];
  if (!state.ears_ready) return ['idle', 'Waking up'];
  if (phase === 'Listening') return ['hearing', 'Listening'];
  if (phase === 'Understanding') return ['thinking', 'Got it'];
  if (phase === 'Ready') return ['listening', state.speaker_name ? 'Go ahead, ' + state.speaker_name : 'Go ahead'];
  return ['idle', 'Say “Hey BITCH”'];
}

function renderMessages(state) {
  const working = state.tasks.filter(task => task.state === 'working');
  const signature = JSON.stringify([state.messages, working.map(task => task.id)]);
  if ($('messages').dataset.signature === signature) return;
  $('messages').dataset.signature = signature;
  const nodes = state.messages.map(message => {
    const p = document.createElement('p');
    p.className = 'bubble ' + (message.role === 'robot' ? 'robot' : 'user');
    p.textContent = message.text;
    return p;
  });
  for (const task of working) {
    const p = document.createElement('p');
    p.className = 'bubble working';
    p.textContent = 'Asking ChatGPT';
    nodes.push(p);
  }
  const pinned = scrollY + innerHeight >= document.documentElement.scrollHeight - 80;
  $('messages').replaceChildren(...nodes);
  $('empty').hidden = nodes.length > 0;
  if (pinned) scrollTo({top: document.documentElement.scrollHeight});
}

function renderTasks(tasks) {
  const signature = JSON.stringify(tasks);
  if ($('tasks').dataset.signature === signature) return;
  $('tasks').dataset.signature = signature;
  if (!tasks.length) {
    const p = document.createElement('p');
    p.className = 'row muted';
    p.textContent = 'Nothing asked yet';
    $('tasks').replaceChildren(p);
    return;
  }
  const words = {working: 'Waiting for ChatGPT…', queued: 'In the Pi inbox', slow: 'Still waiting. Check the ChatGPT tab on the Pi.', canceled: 'Canceled'};
  $('tasks').replaceChildren(...tasks.slice().reverse().map(task => {
    const row = document.createElement('div');
    row.className = 'row task';
    const ask = document.createElement('p');
    ask.className = 'ask';
    ask.textContent = task.request.split('\n')[0];
    const answer = document.createElement('p');
    answer.className = 'answer';
    answer.textContent = task.answer || words[task.state] || '';
    row.append(ask, answer);
    return row;
  }));
}

async function refresh() {
  const version = settingsVersion;
  const state = await (await api('state')).json();
  if (version !== settingsVersion) return;
  latest = state;
  const settings = state.settings;
  const {devices} = state;
  const [feeling, words] = mood(state);
  $('app').dataset.mood = feeling;
  if ($('statusText').textContent !== words) $('statusText').textContent = words;
  eyes.energy = state.voice_energy || 0;
  eyes.hearing = state.level || 0;

  $('stop').hidden = !(state.show_active || ['Speaking', 'Thinking', 'Answering', 'Performing'].includes(state.status));
  $('voiceToggle').setAttribute('aria-pressed', String(settings.muted));
  $('voiceToggle').setAttribute('aria-label', settings.muted ? 'Turn BITCH’s voice back on' : 'Mute BITCH’s voice');
  setIcon($('voiceToggle'), settings.muted ? 'i-voice-off' : 'i-voice');
  $('micToggle').setAttribute('aria-pressed', String(settings.mic_muted));
  $('micToggle').setAttribute('aria-label', settings.mic_muted ? 'Turn my microphone back on' : 'Turn off my microphone');
  setIcon($('micToggle'), settings.mic_muted ? 'i-mic-off' : 'i-mic');
  renderMessages(state);

  if (!$('settings').open) return;
  if (state.people) renderPeople(state.people);
  renderTasks(state.tasks);
  if (previewSource !== settings.camera_source) {
    previewSource = settings.camera_source;
    frameId = 0;
    clearPreview();
  }
  const entries = capability => devices
    .filter(device => device.online && device.caps.includes(capability))
    .map(device => [device.id, device.name + (device.id === browserDevice ? ' (this device)' : '')]);
  options('mic_source', [['off', 'Off'], ...entries('ears')], settings.mic_source);
  options('camera_source', [['off', 'Off'], ...entries('eyes')], settings.camera_source);
  const cameraOn = devices.some(device => device.id === settings.camera_source && device.online);
  $('cameraView').hidden = $('attentionRow').hidden = !cameraOn;
  options('speaker_source', [['auto', 'Automatic'], ['off', 'Off'], ...entries('voice')], settings.speaker_source);
  $('speakerTest').textContent = state.output ? 'Test ' + (state.output === 'Raspberry Pi' ? 'Pi speaker' : state.output) : 'Test speaker';
  const robot = devices.find(device => device.id === 'robot');
  $('robotStatus').textContent = robot ? robot.detail : 'Off';
  $('modelName').textContent = state.model || '';
  if ($('picture').hidden) $('drive').textContent = state.body?.mode || 'Waiting for Pi';
  if (document.activeElement !== $('robot_host')) $('robot_host').value = settings.robot_host;
  $('robot_key').placeholder = settings.robot_paired ? 'Saved' : 'Paste key';
  for (const key of ['idle', 'keep_audio', 'check_ins', 'natural_memory']) $(key).checked = settings[key];
  if (document.activeElement !== $('volume')) {
    $('volume').value = settings.volume;
    $('volumeValue').value = settings.volume + '%';
  }
  $('notice').textContent = state.notes.at(-1) || '';
  $('notice').hidden = !state.notes.length;
  if (!calibrating) {
    const points = settings.camera_calibration[settings.camera_source];
    $('calStatus').textContent = points ? (points?.[0]?.length === 4 ? 'Aligned' : 'Turn aligned') : '';
  }
  if (mic && !starting.has('ears') && settings.mic_source !== browserDevice) stopMedia('ears');
  if (camera && !starting.has('eyes') && settings.camera_source !== browserDevice) stopMedia('eyes');
}

// Googly eyes: each pupil is a little weight on a spring, so it overshoots and wobbles.
const eyes = {
  pupils: [{x: 0, y: 0, vx: 0, vy: 0}, {x: 0, y: 0, vx: 0, vy: 0}],
  target: {x: 0, y: 0}, lid: 0, blinkAt: 0, wanderAt: 0, wander: {x: 0, y: 0},
  pointer: null, pointerAt: -1e9, energy: 0, hearing: 0, last: 0,
  still: matchMedia('(prefers-reduced-motion: reduce)'),
};

function lookTarget(now) {
  const feeling = $('app').dataset.mood;
  if (feeling === 'thinking') return {x: .62, y: -.62};
  if (feeling === 'listening' || feeling === 'hearing') return {x: 0, y: .08};
  if (feeling === 'speaking') {
    const shake = eyes.energy * .35;
    return {x: Math.sin(now / 90) * shake, y: .05 + Math.cos(now / 70) * shake};
  }
  if (eyes.pointer && now - eyes.pointerAt < 2500) return eyes.pointer;
  if (now > eyes.wanderAt) {
    eyes.wander = {x: (Math.random() * 2 - 1) * .7, y: (Math.random() * 2 - 1) * .4};
    eyes.wanderAt = now + 1800 + Math.random() * 2600;
  }
  return eyes.wander;
}

function animateEyes(now) {
  const dt = Math.min(.05, (now - (eyes.last || now)) / 1000);
  eyes.last = now;
  const feeling = $('app').dataset.mood;
  const target = lookTarget(now);
  const reach = 6.2;
  eyes.pupils.forEach((pupil, index) => {
    if (eyes.still.matches) {
      pupil.x = target.x * reach;
      pupil.y = target.y * reach;
    } else {
      const ax = (target.x * reach - pupil.x) * 140 - pupil.vx * 9;
      const ay = (target.y * reach - pupil.y) * 140 - pupil.vy * 9 + (feeling === 'speaking' ? (index ? 1 : -1) * eyes.energy * 40 : 0);
      pupil.vx += ax * dt;
      pupil.vy += ay * dt;
      pupil.x += pupil.vx * dt;
      pupil.y += pupil.vy * dt;
      const length = Math.hypot(pupil.x, pupil.y);
      if (length > reach + 1) {
        // The pupil hits the rim and bounces back, like a real googly eye.
        pupil.x *= (reach + 1) / length;
        pupil.y *= (reach + 1) / length;
        pupil.vx *= -.4;
        pupil.vy *= -.4;
      }
    }
    const node = $(index ? 'pupilR' : 'pupilL');
    node.setAttribute('cx', (index ? 54 : 18) + pupil.x);
    node.setAttribute('cy', 19 + pupil.y);
    node.setAttribute('r', 6.5 + (feeling === 'hearing' ? Math.min(1.4, eyes.hearing * 3) : feeling === 'listening' ? .6 : 0));
  });
  // Blink now and then; close halfway when the mic is off and fully when BITCH is offline.
  const rest = feeling === 'offline' ? 1 : feeling === 'off' ? .45 : 0;
  if (now > eyes.blinkAt) eyes.blinkAt = now + 2600 + Math.random() * 4200;
  const blinking = !eyes.still.matches && eyes.blinkAt - now < 140;
  const wanted = blinking ? 1 : rest;
  eyes.lid += (wanted - eyes.lid) * Math.min(1, dt * (blinking ? 40 : 10));
  for (const id of ['lidL', 'lidR']) $(id).setAttribute('height', (eyes.lid * 34).toFixed(2));
  requestAnimationFrame(animateEyes);
}

addEventListener('pointermove', event => {
  const box = $('eyes').getBoundingClientRect();
  const dx = event.clientX - (box.left + box.width / 2);
  const dy = event.clientY - (box.top + box.height / 2);
  const length = Math.max(1, Math.hypot(dx, dy));
  eyes.pointer = {x: dx / length * Math.min(1, length / 140), y: dy / length * Math.min(1, length / 140)};
  eyes.pointerAt = performance.now();
}, {passive: true});

function caps() {
  return [mic ? 'ears' : '', camera ? 'eyes' : ''].filter(Boolean).join(',');
}

function connectSocket() {
  return new Promise((resolve, reject) => {
    const scheme = location.protocol === 'https:' ? 'wss://' : 'ws://';
    socket = new WebSocket(scheme + location.host + '/link/' + browserId);
    socket.onopen = () => socket.send(caps());
    socket.onmessage = event => {
      browserDevice = JSON.parse(event.data).device;
      resolve(browserDevice);
    };
    socket.onclose = () => {
      if (mic) stopMedia('ears');
      if (camera) stopMedia('eyes');
      reject(new Error('This device couldn’t connect to BITCH.'));
    };
  });
}

function announce() {
  if (socket?.readyState !== WebSocket.OPEN) return connectSocket();
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Connecting this device took too long. Try again.')), 2500);
    socket.addEventListener('message', event => {
      clearTimeout(timer);
      resolve(JSON.parse(event.data).device);
    }, {once: true});
    socket.send(caps());
  });
}

function send(kind, bytes) {
  if (socket?.readyState !== WebSocket.OPEN || socket.bufferedAmount > 90000) return;
  socket.send(new Blob([Uint8Array.of(kind), bytes]));
}

function stopMedia(kind) {
  const item = kind === 'ears' ? mic : camera;
  if (!item) return;
  item.stream.getTracks().forEach(track => track.stop());
  if (kind === 'ears') {
    item.context.close().catch(() => {});
    mic = null;
  } else {
    clearInterval(item.timer);
    item.video.srcObject = null;
    camera = null;
  }
  if (socket?.readyState === WebSocket.OPEN) socket.send(caps());
}

async function enumerate() {
  const devices = await navigator.mediaDevices.enumerateDevices();
  for (const [id, kind] of [['browserMic', 'audioinput'], ['browserCamera', 'videoinput']]) {
    const node = $(id);
    const chosen = node.value;
    const choices = devices.filter(device => device.kind === kind && device.deviceId && device.label);
    node.replaceChildren(new Option('Default', ''), ...choices.map(device => new Option(device.label, device.deviceId)));
    node.value = choices.some(device => device.deviceId === chosen) ? chosen : '';
  }
}

async function useMedia(kind) {
  if (starting.size) return;
  if (!navigator.mediaDevices) throw new Error('Open the secure link to use this device’s microphone or camera.');
  const audio = kind === 'ears';
  const setting = audio ? 'mic_source' : 'camera_source';
  const id = $(audio ? 'browserMic' : 'browserCamera').value;
  const device = id ? {deviceId: {exact: id}} : {};
  const Worklet = window.AudioContext || window.webkitAudioContext;
  const context = audio ? new Worklet() : null;
  starting.add(kind);
  let stream;
  try {
    if (context) await context.resume();
    stream = await navigator.mediaDevices.getUserMedia(audio
      ? {audio: {...device, channelCount: 1, echoCancellation: true, noiseSuppression: true}}
      : {video: {...device, width: {ideal: 480}, height: {ideal: 360}}, audio: false});
    stopMedia(kind);
    if (audio) {
      await context.audioWorklet.addModule('/app/mic.js');
      const node = new AudioWorkletNode(context, 'robot-mic');
      const source = context.createMediaStreamSource(stream);
      const silent = context.createGain();
      silent.gain.value = 0;
      source.connect(node).connect(silent).connect(context.destination);
      node.port.onmessage = event => { if (mic?.stream === stream) send(2, event.data); };
      mic = {stream, context, source, node};
    } else {
      const video = document.createElement('video');
      video.playsInline = video.muted = true;
      video.srcObject = stream;
      await video.play();
      const canvas = document.createElement('canvas');
      const draw = canvas.getContext('2d');
      let encoding = false;
      const timer = setInterval(() => {
        if (!video.videoWidth || encoding || socket?.bufferedAmount > 90000) return;
        encoding = true;
        const width = Math.min(video.videoWidth, 640);
        const height = Math.round(video.videoHeight * width / video.videoWidth);
        if (canvas.width !== width || canvas.height !== height) {
          canvas.width = width;
          canvas.height = height;
        }
        draw.drawImage(video, 0, 0, canvas.width, canvas.height);
        canvas.toBlob(blob => {
          encoding = false;
          if (blob && camera?.stream === stream) send(3, blob);
        }, 'image/jpeg', 0.78);
      }, 95);
      camera = {stream, video, timer};
    }
    stream.getTracks().forEach(track => track.addEventListener('ended', () => {
      const item = audio ? mic : camera;
      if (item?.stream === stream) stopMedia(kind);
    }));
    const selected = await announce();
    await api('settings', {[setting]: selected});
    await enumerate();
  } catch (error) {
    stopMedia(kind);
    if (stream) stream.getTracks().forEach(track => track.stop());
    if (context && context.state !== 'closed') await context.close();
    throw error;
  } finally {
    starting.delete(kind);
  }
}

function clearPreview() {
  $('noFrame').textContent = previewSource === 'off' ? 'Camera off' : 'Waiting for the camera…';
  $('noFrame').hidden = false;
  $('picture').hidden = true;
  $('attention').textContent = 'No one in view';
}

async function drawFrame(response, source) {
  const scene = JSON.parse(response.headers.get('X-Scene'));
  const jpeg = URL.createObjectURL(await response.blob());
  try {
    const image = new Image();
    image.src = jpeg;
    await image.decode();
    if (source !== previewSource) return;
    const canvas = $('picture');
    const [width, height] = scene.size;
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
    const ctx = canvas.getContext('2d', {alpha: false});
    ctx.drawImage(image, 0, 0);
    const fontSize = Math.max(12, Math.round(width * 0.026));
    ctx.font = '600 ' + fontSize + 'px -apple-system, system-ui, sans-serif';
    const active = Boolean(scene.drive.aim) && scene.drive.mode !== 'Aligning';
    for (const person of scene.people) {
      const [nx, ny, nw, nh] = person.box;
      const x = Math.round(nx * width), y = Math.round(ny * height);
      const w = Math.min(width - x, nw * width), h = Math.min(height - y, nh * height);
      const focused = person.id === scene.focus && active;
      const label = person.name || 'Person ' + person.id;
      ctx.lineWidth = focused ? 3 : 1.25;
      ctx.strokeStyle = focused ? '#D6B084' : 'rgba(255,255,255,.75)';
      ctx.beginPath();
      ctx.roundRect(x, y, w, h, 10);
      ctx.stroke();
      const top = y > fontSize + 12 ? y - fontSize - 12 : y + 6;
      const labelWidth = ctx.measureText(label).width + 16;
      ctx.fillStyle = focused ? '#D6B084' : 'rgba(18,20,23,.78)';
      ctx.beginPath();
      ctx.roundRect(Math.min(x, width - labelWidth), top, labelWidth, fontSize + 10, 8);
      ctx.fill();
      ctx.fillStyle = focused ? '#18202E' : '#F2EFEA';
      ctx.fillText(label, Math.min(x, width - labelWidth) + 8, top + fontSize + 2);
    }
    if (scene.drive.point) {
      const [x, y] = scene.drive.point;
      ctx.beginPath();
      ctx.arc(x * width, y * height, Math.max(9, width * .025), 0, Math.PI * 2);
      ctx.lineWidth = 3;
      ctx.strokeStyle = '#D6B084';
      ctx.stroke();
    }
    const count = scene.people.length;
    const subject = scene.people.find(person => person.id === scene.focus);
    $('attention').textContent = active && scene.focus != null
      ? (subject ? 'Following ' + (subject.name || 'Person ' + subject.id) : 'Waiting for Person ' + scene.focus)
      : count === 1 ? 'One person' : count ? count + ' people' : 'No one in view';
    const beats = {curious: 'Curious lean', glance: 'Glancing aside', shy: 'Peeking back', peek: 'Peeking back',
      stretch: 'Stretching', proud: 'Chin up', sad: 'Drooping', survey: 'Looking around the room'};
    $('drive').textContent = beats[scene.drive.gesture] || scene.drive.mode;
    $('noFrame').hidden = true;
    canvas.hidden = false;
  } finally {
    URL.revokeObjectURL(jpeg);
  }
}

async function preview() {
  let delay = 0;
  const source = previewSource;
  if (document.hidden || source === 'off' || !$('settings').open) {
    delay = 300;
  } else {
    try {
      const response = await api('frame?after=' + frameId);
      if (source === previewSource) {
        if (response.status === 204) {
          clearPreview();
          delay = 180;
        } else {
          frameId = Number(response.headers.get('X-Frame'));
          await drawFrame(response, source);
        }
      }
    } catch {
      clearPreview();
      delay = 450;
    }
  }
  setTimeout(preview, delay);
}

let aimRequest = Promise.resolve();

async function calibrate(step, track = false) {
  const align = $('alignmentMessage');
  align.textContent = '';
  try {
    const angle = Number($('aim').value);
    const neck = Number($('neckAim').value);
    const result = await (await api('calibrate', {action: step, angle, neck, track})).json();
    captureCount = result.samples.length;
    if (step === 'start') {
      $('aim').value = 0;
      $('angle').value = '0.00';
      $('neckAim').value = -0.8;
      $('pitch').value = '-0.80';
      calibrating = true;
    }
    if (step === 'save' || step === 'cancel') calibrating = false;
    $('calibration').hidden = !calibrating;
    $('startCalibration').hidden = calibrating;
    $('sample').hidden = captureCount >= 3;
    $('save').hidden = $('saveTrack').hidden = captureCount < 3;
    $('aim').disabled = $('neckAim').disabled = captureCount >= 3;
    $('calStep').textContent = captureCount < 3
      ? ['1 of 3 · Left of the robot', '2 of 3 · Straight ahead', '3 of 3 · Right of the robot'][captureCount]
      : 'All three points captured.';
    if (step === 'sample' && captureCount < 3) align.textContent = 'Captured. Move to the next spot.';
  } catch (error) {
    if (!calibrating) throw error;
    align.textContent = error.message;
  }
}

function renderPeople(people) {
  const enrollment = people.enrollment;
  $('cancelEnroll').hidden = !enrollment;
  $('rememberFace').disabled = $('rememberVoice').disabled = Boolean(enrollment);
  if (enrollment || people.message) {
    $('enrollState').textContent = (enrollment ? enrollment.progress + ' of ' + enrollment.total + ' · ' : '') + people.message;
  }
  const list = $('profiles');
  const signature = JSON.stringify(people.profiles);
  if (list.dataset.signature === signature) return;
  list.dataset.signature = signature;
  list.replaceChildren(...people.profiles.map(person => {
    const row = document.createElement('div');
    row.className = 'row';
    const label = document.createElement('span');
    label.textContent = person.name;
    const known = document.createElement('span');
    known.className = 'value';
    known.textContent = [person.face && 'Face', person.voice && 'voice'].filter(Boolean).join(' and ');
    const remove = document.createElement('button');
    remove.className = 'text-button';
    remove.type = 'button';
    remove.textContent = 'Forget';
    remove.setAttribute('aria-label', 'Forget ' + person.name);
    remove.onclick = () => api('people', {action: 'remove', name: person.name}).catch(fail);
    row.append(label, known, remove);
    return row;
  }));
}

// Main screen controls
action('sayForm', 'submit', async () => {
  const text = $('sayText').value.trim();
  if (!text) return;
  $('send').disabled = true;
  await api('say', {text});
  $('sayText').value = '';
});
$('sayText').addEventListener('input', () => { $('send').disabled = !$('sayText').value.trim(); });
action('micToggle', 'click', () => {
  settingsVersion++;
  return api('settings', {mic_muted: !(latest?.settings.mic_muted)});
});
action('voiceToggle', 'click', () => {
  settingsVersion++;
  return api('settings', {muted: !(latest?.settings.muted)});
});
action('stop', 'click', () => api('stop', {}));
action('openSettings', 'click', async () => {
  $('settings').showModal();
  await refresh();
});
action('closeSettings', 'click', () => $('settings').close());
$('settings').addEventListener('click', event => { if (event.target === $('settings')) $('settings').close(); });
$('settings').addEventListener('close', () => {
  if (calibrating) calibrate('cancel').catch(fail);
});

// Settings
action('speakerTest', 'click', () => api('test-speaker', {}));
for (const key of ['idle', 'keep_audio', 'check_ins', 'natural_memory', 'mic_source', 'camera_source', 'speaker_source']) {
  action(key, 'change', e => {
    const value = e.target.type === 'checkbox' ? e.target.checked : e.target.value;
    settingsVersion++;
    if (key === 'camera_source' && value === 'off') {
      previewSource = 'off';
      frameId = 0;
      clearPreview();
    }
    return api('settings', {[key]: value});
  });
}
$('volume').addEventListener('input', () => { $('volumeValue').value = $('volume').value + '%'; });
action('volume', 'change', () => {
  settingsVersion++;
  return api('settings', {volume: Number($('volume').value)});
});
action('pairForm', 'submit', async () => {
  await api('pair', {code: $('code').value});
  location.reload();
});
action('robotForm', 'submit', async () => {
  const settings = {robot_host: $('robot_host').value.trim()};
  if ($('robot_key').value.trim()) settings.robot_key = $('robot_key').value.trim();
  await api('settings', settings);
  $('robot_key').value = '';
  $('robotStatus').textContent = 'Connecting…';
});
action('showCode', 'click', async () => {
  $('pairCode').value = (await (await api('pair-code')).json()).code;
});
action('rememberFace', 'click', () => api('people', {action: 'face', name: $('personName').value}));
action('rememberVoice', 'click', () => api('people', {action: 'voice', name: $('personName').value}));
action('cancelEnroll', 'click', () => api('people', {action: 'cancel'}));
action('useMic', 'click', () => useMedia('ears'));
action('useCamera', 'click', () => useMedia('eyes'));
action('startCalibration', 'click', () => calibrate('start'));
$('aim').addEventListener('input', () => { $('angle').value = Number($('aim').value).toFixed(2); });
$('neckAim').addEventListener('input', () => { $('pitch').value = Number($('neckAim').value).toFixed(2); });
for (const slider of ['aim', 'neckAim']) {
  action(slider, 'change', () => {
    aimRequest = aimRequest.then(() => calibrate('aim'));
    return aimRequest;
  });
}
action('sample', 'click', async () => {
  $('sample').disabled = true;
  try {
    await aimRequest;
    await calibrate('sample');
  } finally {
    $('sample').disabled = false;
  }
});
action('save', 'click', () => calibrate('save'));
action('saveTrack', 'click', () => calibrate('save', true));
action('cancel', 'click', () => calibrate('cancel'));
$('alignment').addEventListener('toggle', () => {
  if (!$('alignment').open && calibrating) calibrate('cancel').catch(fail);
});
window.addEventListener('pagehide', () => {
  stopMedia('ears');
  stopMedia('eyes');
  socket?.close();
});

async function poll() {
  try {
    await refresh();
  } catch {
    $('app').dataset.mood = 'offline';
    $('statusText').textContent = 'Can’t reach BITCH';
  }
  setTimeout(poll, 280);
}

async function start() {
  requestAnimationFrame(animateEyes);
  hello = await (await api('hello')).json();
  if (!hello.paired) {
    $('pair').hidden = false;
    const secure = location.protocol === 'https:';
    $('secureSetup').hidden = secure;
    $('pairForm').hidden = !secure;
    $('secureLink').href = 'https://' + location.hostname + ':' + hello.https;
    $('statusText').textContent = 'Not connected yet';
    return;
  }
  $('conversation').hidden = $('composer').hidden = $('barActions').hidden = false;
  $('robotSetup').hidden = $('addDevice').hidden = $('peopleSetup').hidden = !hello.local;
  const address = 'http://' + hello.host + ':' + hello.http;
  $('pairLink').href = $('pairLink').textContent = address;
  browserId = localStorage.getItem('desk-robot-browser');
  if (!browserId) {
    browserId = crypto.randomUUID().replaceAll('-', '');
    localStorage.setItem('desk-robot-browser', browserId);
  }
  if (navigator.mediaDevices) enumerate().catch(() => {});
  preview();
  await poll();
}

start().catch(error => {
  $('app').dataset.mood = 'offline';
  $('statusText').textContent = 'Can’t reach BITCH';
  fail(error);
});
