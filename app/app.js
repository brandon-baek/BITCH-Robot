'use strict';

const $ = id => document.getElementById(id);
let socket, browserId, browserDevice, hello, mic, camera, calibrating = false, captureCount = 0;
let starting = new Set();
let previewSource = 'off';
let frameId = 0;
let settingsVersion = 0;

async function api(path, body) {
  const options = body === undefined ? {cache: 'no-store'} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
  };
  const response = await fetch('/api/' + path, options);
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'The setup server is unavailable.');
  }
  return response;
}

function fail(error) {
  $('error').textContent = error.message;
  $('error').hidden = false;
}

function action(id, event, fn) {
  $(id).addEventListener(event, async e => {
    e.preventDefault();
    $('error').hidden = true;
    try { await fn(e); } catch (error) { fail(error); }
  });
}

function options(id, entries, selection) {
  const node = $(id);
  if (document.activeElement === node && node.options.length) return;
  if (!entries.some(([value]) => value === selection)) entries.push([selection, 'Disconnected source']);
  const signature = JSON.stringify(entries);
  if (signature !== node.dataset.entries) {
    node.replaceChildren(...entries.map(([value, label]) => new Option(label, value)));
    node.dataset.entries = signature;
  }
  node.value = selection;
}

async function refresh() {
  const version = settingsVersion;
  const state = await (await api('state')).json();
  if (version !== settingsVersion) return;
  if (state.people) renderPeople(state.people);
  const settings = state.settings;
  const {devices} = state;
  if (previewSource !== settings.camera_source) {
    previewSource = settings.camera_source;
    frameId = 0;
    clearPreview();
  }
  const entries = capability => devices
    .filter(device => device.online && device.caps.includes(capability))
    .map(device => [device.id, device.name + (device.id === browserDevice ? ' · this browser' : '')]);
  options('mic_source', [['off', 'Off'], ...entries('ears')], settings.mic_source);
  options('camera_source', [['off', 'Off'], ...entries('eyes')], settings.camera_source);
  options('speaker_source', [['auto', 'Auto · Pi, then Mac'], ['off', 'Off'], ...entries('voice')], settings.speaker_source);
  const phase = state.turn_phase;
  const listening = phase === 'Ready' || phase === 'Listening';
  $('status').lastElementChild.textContent = state.show_active ? 'Performing' : phase === 'Listening' ? 'Hearing you' : phase === 'Understanding' ? 'Understanding your words' : phase === 'Checking for name' ? 'Checking for the name' : state.status;
  $('wake').dataset.listening = String(listening);
  $('turnHeading').textContent = state.show_active ? 'Performing' : phase === 'Listening' ? 'I’m listening' : phase === 'Understanding' ? 'Got it. One moment…' : listening ? (state.speaker_name ? 'Your turn, ' + state.speaker_name : 'Your turn') : ['Thinking', 'Speaking', 'Answering'].includes(state.status)
    ? state.status + '…' : 'Say “Hey BITCH”';
  const micDevice = devices.find(device => device.id === settings.mic_source);
  $('listenHint').textContent = state.show_active ? 'Say “Stop the show” or tap Stop speech.'
    : settings.mic_source === 'off' ? 'Microphone off.'
    : !micDevice ? 'Choose a connected microphone.'
    : !state.ears_ready ? 'Loading on-device speech recognition…'
    : micDevice.id === 'mac-mic' && !micDevice.detail.startsWith('Open') ? micDevice.detail
    : phase === 'Listening' ? 'Go on. A pause is fine.'
    : phase === 'Understanding' ? 'Finishing your sentence on the Mac…'
    : listening ? (state.hearing_speech ? 'Hearing you…' : 'Keep talking without repeating the name · ' + Math.ceil(state.followup_seconds) + 's')
    : state.status === 'Speaking' ? 'You can cut in while it’s talking.'
    : settings.muted || settings.volume === 0 ? 'Voice muted. Replies appear here.'
    : 'Say the name to open a conversation.';
  $('meter').value = state.status === 'Speaking' || !micDevice ? 0 : Math.min(1, state.level);
  $('speakerTest').textContent = state.output ? 'Test ' + (state.output === 'Raspberry Pi' ? 'Pi speaker' : state.output) : 'Test speaker';
  const robot = devices.find(device => device.id === 'robot');
  $('robotStatus').textContent = robot ? robot.detail : 'Pi disabled';
  $('modelName').textContent = state.model
    ? (state.voicechat_ready && settings.voicechat_enabled ? 'Nemotron VoiceChat 11B · Local agent ' : 'Brain · ') + state.model
    : '';
  $('voicechatControl').hidden = !(state.voicechat_ready && state.people);
  if ($('picture').hidden) $('drive').textContent = state.body?.mode || 'Waiting for Pi';
  if (document.activeElement !== $('robot_host')) $('robot_host').value = settings.robot_host;
  $('robot_key').placeholder = settings.robot_paired ? 'Key saved' : '';
  for (const key of ['idle', 'muted', 'keep_audio', 'check_ins', 'natural_memory', 'voicechat_enabled']) $(key).checked = settings[key];
  if (document.activeElement !== $('volume')) {
    $('volume').value = settings.volume;
    $('volumeValue').value = settings.volume + '%';
  }
  const last = state.messages.slice(-4);
  const contents = JSON.stringify(last);
  if ($('messages').dataset.contents !== contents) {
    $('messages').dataset.contents = contents;
    $('messages').replaceChildren(...last.map(message => {
      const p = document.createElement('p');
      p.className = message.role;
      const name = document.createElement('strong');
      name.textContent = message.role === 'robot' ? 'BITCH' : 'You';
      p.append(name, document.createTextNode(message.text));
      return p;
    }));
  }
  $('notice').textContent = state.notes.at(-1) || '';
  $('notice').hidden = !state.notes.length;
  if (!calibrating) {
    const points = settings.camera_calibration[settings.camera_source];
    const label = points?.[0]?.length === 4 ? 'Aligned' : 'Turn aligned';
    $('calStatus').textContent = points ? label + (settings.idle ? '' : ' · idle motion off') : '';
  }
  if (mic && !starting.has('ears') && settings.mic_source !== browserDevice) stopMedia('ears');
  if (camera && !starting.has('eyes') && settings.camera_source !== browserDevice) stopMedia('eyes');
}

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
      reject(new Error('This browser could not connect.'));
    };
  });
}

function announce() {
  if (socket?.readyState !== WebSocket.OPEN) return connectSocket();
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Device registration timed out.')), 2500);
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
    node.replaceChildren(new Option('System default', ''), ...choices.map(device => new Option(device.label, device.deviceId)));
    node.value = choices.some(device => device.deviceId === chosen) ? chosen : '';
  }
}

async function useMedia(kind) {
  if (starting.size) return;
  if (!navigator.mediaDevices) throw new Error('Open the secure setup link to use browser devices.');
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
  const off = previewSource === 'off';
  $('noFrame').textContent = off ? 'Choose a camera in Connections.' : 'Waiting for a camera frame…';
  $('noFrame').hidden = false;
  $('picture').hidden = true;
  $('feedState').textContent = off ? 'Camera off' : 'Camera waiting';
  $('feedState').classList.remove('live');
  $('attention').textContent = 'No one in view';
  $('aimReadout').textContent = '';
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
    const fontSize = Math.max(12, Math.round(width * 0.024));
    ctx.font = '600 ' + fontSize + 'px system-ui';
    const active = Boolean(scene.drive.aim) && scene.drive.mode !== 'Aligning';
    for (const person of scene.people) {
      const [nx, ny, nw, nh] = person.box;
      const x = Math.round(nx * width), y = Math.round(ny * height);
      const w = Math.min(width - x, nw * width), h = Math.min(height - y, nh * height);
      const focused = person.id === scene.focus;
      const label = (person.name || 'Person ' + person.id) + (focused && active ? ' · Tracking' : '');
      const color = focused && active ? '#ffc18d' : '#e1ebe3';
      ctx.lineWidth = focused && active ? 2.5 : 1.1;
      ctx.strokeStyle = color;
      ctx.strokeRect(x, y, w, h);
      const top = y > fontSize + 8 ? y - fontSize - 8 : y;
      const labelWidth = ctx.measureText(label).width + 12;
      const left = Math.min(x, width - labelWidth);
      ctx.fillStyle = focused && active ? color : '#18201eea';
      ctx.fillRect(left, top, labelWidth, fontSize + 8);
      ctx.fillStyle = focused && active ? '#221810' : color;
      ctx.fillText(label, left + 6, top + fontSize + 1);
    }
    if (scene.drive.point) {
      const [x, y] = scene.drive.point;
      ctx.beginPath();
      ctx.arc(x * width, y * height, Math.max(9, width * .025), 0, Math.PI * 2);
      ctx.lineWidth = 3;
      ctx.strokeStyle = '#18201e';
      ctx.stroke();
      ctx.lineWidth = 1.5;
      ctx.strokeStyle = '#ffc18d';
      ctx.stroke();
    }
    const count = scene.people.length;
    const subject = scene.people.find(person => person.id === scene.focus);
    $('attention').textContent = active && scene.focus != null
      ? (subject ? 'Tracking ' + (subject.name || 'Person ' + subject.id) : 'Holding for Person ' + scene.focus)
      : count === 1 ? '1 person in view' : count ? count + ' people in view' : 'No one in view';
    const beats = {curious:'Curious lean', glance:'Glancing aside', shy:'Peeking back', peek:'Peeking back', stretch:'Stretching', proud:'Chin up', sad:'Drooping', survey:'Surveying the room'};
    $('drive').textContent = beats[scene.drive.gesture] || scene.drive.mode;
    $('aimReadout').textContent = scene.drive.aim && Number.isFinite(scene.drive.base)
      ? 'Turn ' + scene.drive.base.toFixed(2) + ' → ' + scene.drive.aim[0].toFixed(2) : '';
    $('noFrame').hidden = true;
    canvas.hidden = false;
    $('feedState').textContent = 'Live';
    $('feedState').classList.add('live');
  } finally {
    URL.revokeObjectURL(jpeg);
  }
}

async function preview() {
  let delay = 0;
  const source = previewSource;
  if (document.hidden || source === 'off') {
    delay = 250;
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
    if (step === 'sample' && captureCount < 3) align.textContent = 'Captured. Move to the next position.';
  } catch (error) {
    if (!calibrating) throw error;
    align.textContent = error.message;
  }
}

action('speakerTest', 'click', () => api('test-speaker', {}));
for (const key of ['idle', 'muted', 'keep_audio', 'check_ins', 'natural_memory', 'voicechat_enabled',
  'mic_source', 'camera_source', 'speaker_source']) {
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
function renderPeople(people) {
  const enrollment = people.enrollment;
  $('cancelEnroll').hidden = !enrollment;
  $('rememberFace').disabled = $('rememberVoice').disabled = Boolean(enrollment);
  $('enrollState').textContent = (enrollment ? enrollment.progress + ' / ' + enrollment.total + ' · ' : '') + people.message;
  const list = $('profiles');
  const signature = JSON.stringify(people.profiles);
  if (list.dataset.signature === signature) return;
  list.dataset.signature = signature;
  list.replaceChildren(...people.profiles.map(person => {
    const row = document.createElement('li');
    const label = document.createElement('span');
    label.textContent = person.name + ' · ' + [person.face && 'face', person.voice && 'voice'].filter(Boolean).join(' and ');
    const remove = document.createElement('button');
    remove.textContent = 'Forget';
    remove.setAttribute('aria-label', 'Forget ' + person.name);
    remove.onclick = () => api('people', {action: 'remove', name: person.name}).catch(fail);
    row.append(label, remove);
    return row;
  }));
}

action('rememberFace', 'click', () => api('people', {action: 'face', name: $('personName').value}));
action('rememberVoice', 'click', () => api('people', {action: 'voice', name: $('personName').value}));
action('cancelEnroll', 'click', () => api('people', {action: 'cancel'}));
action('hush', 'click', () => api('stop', {}));
$('volume').addEventListener('input', () => { $('volumeValue').value = $('volume').value + '%'; });
action('volume', 'change', () => {
  settingsVersion++;
  return api('settings', {volume: Number($('volume').value)});
});
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
  try { await refresh(); }
  catch (error) { $('status').lastElementChild.textContent = 'Disconnected'; }
  setTimeout(poll, 280);
}

async function start() {
  hello = await (await api('hello')).json();
  if (!hello.paired) {
    $('pair').hidden = false;
    const secure = location.protocol === 'https:';
    $('secureSetup').hidden = secure;
    $('pairForm').hidden = !secure;
    $('secureLink').href = 'https://' + location.hostname + ':' + hello.https;
    $('status').lastElementChild.textContent = 'Pairing';
    return;
  }
  $('controls').hidden = false;
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

start().catch(fail);
