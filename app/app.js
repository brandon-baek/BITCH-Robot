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
  const banner = $('error');
  banner.textContent = error.message;
  // Shown in the top layer, re-raised each time so it lands above any sheet that is open.
  if (banner.matches(':popover-open')) banner.hidePopover();
  banner.showPopover();
  clearTimeout(fail.timer);
  fail.timer = setTimeout(() => banner.hidePopover(), 6000);
}

function action(id, event, fn) {
  $(id).addEventListener(event, async e => {
    if (event !== 'change' && event !== 'input') e.preventDefault();
    if ($('error').matches(':popover-open')) $('error').hidePopover();
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
  const pendingCount = state.tasks.filter(task => ['working', 'background', 'queued'].includes(task.state)).length;
  const pendingNote = pendingCount ? ' · ' + (pendingCount === 1 ? '1 lookup active' : pendingCount + ' lookups active') : '';
  if (state.life?.alarm_ringing) return ['speaking', 'Wake up!'];
  if (state.power?.state === 'waking') return ['idle', 'Waking up' + (state.power.eta ? ` · about ${state.power.eta} s` : '')];
  if (state.power?.state === 'sleeping') return ['asleep', 'Deep sleep · say “Hey BITCH”' + pendingNote];
  if (state.show_active) return ['speaking', 'Performing'];
  if (state.status === 'Speaking') return ['speaking', 'Talking'];
  if (state.status === 'Thinking' || state.status === 'Answering') return ['thinking', 'Thinking'];
  if (state.tasks.some(task => task.state === 'working') && !['Listening', 'Understanding', 'Ready'].includes(phase)) return ['thinking', 'Looking into it'];
  if (settings.mic_muted) return ['off', 'Your mic is off' + pendingNote];
  if (settings.mic_source === 'off' || !micDevice) return ['off', 'No microphone' + pendingNote];
  if (!state.ears_ready) return ['idle', 'Waking up'];
  if (phase === 'Listening') return ['hearing', 'Listening'];
  if (phase === 'Understanding') return ['thinking', 'Got it'];
  if (phase === 'Ready') return ['listening', (state.speaker_name ? 'Go ahead, ' + state.speaker_name : 'Go ahead') + pendingNote];
  if (state.life?.napping) return ['asleep', 'Napping with you' + pendingNote];
  if (state.rhythm === 'asleep') return ['asleep', 'Asleep · say “Hey BITCH”' + pendingNote];
  if (state.rhythm === 'resting') return ['resting', 'Resting' + pendingNote];
  return ['idle', 'Say “Hey BITCH”' + pendingNote];
}

const QUIET_AFTER = 45;   // seconds after a conversation ends before its captions fade
const SESSION_GAP = 600;  // seconds of silence separating distinct conversations
let exchangeSentinels = null;

function ensureExchangeObserver(exchange) {
  if (CSS.supports('animation-timeline', 'scroll()') || exchangeSentinels) return;
  const start = document.createElement('div');
  const end = document.createElement('div');
  for (const sentinel of [start, end]) {
    sentinel.setAttribute('aria-hidden', 'true');
    sentinel.style.cssText = 'min-inline-size: 1px; min-block-size: 1px; flex: none';
  }
  const observer = new IntersectionObserver(entries => {
    for (const {target, isIntersecting} of entries) {
      exchange.classList.toggle(target === start ? 'more-start' : 'more-end', !isIntersecting);
    }
  }, {root: exchange});
  exchangeSentinels = {start, end, observer};
}

// The active conversation captions, scrollable up through past turns in this exchange.
function renderNow(state, feeling) {
  const messages = state.messages;
  const newest = messages.at(-1);
  const recent = Boolean(newest) && (state.now - newest.at < QUIET_AFTER);
  const busy = ['thinking', 'speaking'].includes(feeling);
  const listeningFresh = ['listening', 'hearing'].includes(feeling) && Boolean(newest) && (state.now - newest.at < QUIET_AFTER * 2);
  const working = state.tasks.some(task => ['working', 'background', 'queued'].includes(task.state));
  const showing = Boolean(newest) && (recent || busy || listeningFresh || working);
  const exchange = $('exchange');
  const wasShowing = $('captions').dataset.showing === 'true';

  if (showing && messages.length) {
    let startIdx = messages.length - 1;
    while (startIdx > 0 && messages[startIdx].at - messages[startIdx - 1].at <= SESSION_GAP) startIdx--;
    const items = messages.slice(startIdx).map(m => ({role: m.role, text: m.text}));
    // While BITCH talks, show only what it has said aloud so far on the active reply line.
    if (state.speaking) {
      if (items.at(-1)?.role === 'robot') items[items.length - 1].text = state.speaking;
      else items.push({role: 'robot', text: state.speaking});
    }
    const signature = JSON.stringify(items);
    if (exchange.dataset.signature !== signature) {
      const prevCount = Number(exchange.dataset.count || 0);
      const nearBottom = exchange.scrollHeight - exchange.scrollTop - exchange.clientHeight < 36;
      const newTurn = items.length > prevCount && items.at(-1)?.role === 'user';
      ensureExchangeObserver(exchange);
      const nodes = items.map(item => {
        const p = document.createElement('p');
        p.className = item.role === 'robot' ? 'reply' : 'said';
        p.textContent = item.text;
        return p;
      });
      if (exchangeSentinels) {
        exchangeSentinels.observer.disconnect();
        exchange.replaceChildren(exchangeSentinels.start, ...nodes, exchangeSentinels.end);
        exchangeSentinels.observer.observe(exchangeSentinels.start);
        exchangeSentinels.observer.observe(exchangeSentinels.end);
      } else {
        exchange.replaceChildren(...nodes);
      }
      exchange.dataset.signature = signature;
      exchange.dataset.count = String(items.length);
      if (!wasShowing || nearBottom || newTurn) {
        exchange.scrollTop = exchange.scrollHeight;
      }
    }
  }
  $('captions').dataset.showing = String(showing);
  const hint = state.settings.mic_muted ? 'Your mic is off. Type below.' : 'Say “Hey BITCH”, or type below.';
  if ($('hint').textContent !== hint) $('hint').textContent = hint;
  $('showEarlier').hidden = !messages.length;
  if ($('earlier').open) renderHistory(messages);
}

// Who BITCH is with, and how it knows them.
function renderCompany(state, feeling) {
  const talking = ['listening', 'hearing', 'thinking', 'speaking'].includes(feeling);
  const view = state.in_view;
  const learning = state.people.learning || [];
  let text = '', by = '', add = false;
  const meeting = learning.find(entry => entry.name === state.company?.name) || learning[0];
  if (meeting && (talking || view)) {
    text = meeting.voice != null ? 'Listening for ' + meeting.name + '’s voice · ' + meeting.voice + ' of 3'
      : 'Getting to know ' + meeting.name;
    by = 'Learning';
  } else if (talking && state.company) {
    text = state.company.name;
    by = {voice: 'Voice', face: 'Face', body: 'Tracked', introduced: 'Introduced'}[state.company.by] || '';
  } else if (talking && view && !view.names.length && view.others === 1) {
    text = 'Someone new';
    add = true;
  } else if (view && view.names.length) {
    const names = view.names.length > 1 ? view.names.slice(0, -1).join(', ') + ' and ' + view.names.at(-1) : view.names[0];
    text = names + (view.others ? ' and ' + view.others + (view.others === 1 ? ' other' : ' others') : '')
      + (view.names.length + view.others > 1 ? ' are here' : ' is here');
  }
  // The line keeps its space when empty, so nothing below it jumps.
  $('company').classList.toggle('on', Boolean(text));
  if (!text) return;
  if ($('companyText').textContent !== text) $('companyText').textContent = text;
  if ($('companyBy').textContent !== by) $('companyBy').textContent = by;
  $('addFromStage').hidden = !add;
}

// Faces in view, shown while BITCH is meeting someone, so you can see exactly who it means.
let facesAt = 0;
async function renderFaces(state, feeling) {
  const learning = (state.people.learning || []).find(entry => entry.track != null || entry.voice != null);
  const stranger = ['listening', 'hearing', 'thinking', 'speaking'].includes(feeling)
    && state.in_view && state.in_view.others > 0 && !state.company;
  const show = Boolean(learning || stranger) && state.in_view;
  $('faces').hidden = !show;
  if (!show || performance.now() - facesAt < 1000) return;
  facesAt = performance.now();
  const {faces} = await (await api('faces')).json();
  $('faces').replaceChildren(...faces.map(face => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'face' + (learning && face.track === learning.track ? ' learning' : '');
    const picture = document.createElement('img');
    picture.src = face.picture;
    picture.alt = face.name || 'Someone BITCH doesn’t know yet';
    const label = document.createElement('span');
    label.textContent = learning && face.track === learning.track ? 'Learning ' + learning.name
      : face.name || (learning ? 'Is this ' + learning.name + '?' : 'Who’s this?');
    button.append(picture, label);
    button.disabled = Boolean(face.name);
    button.onclick = () => (learning
      ? api('people', {action: 'learn-face', name: learning.name, track: face.track})
      : openPerson('', face.track)).catch(fail);
    return button;
  }));
}

function renderHistory(messages) {
  const signature = JSON.stringify(messages);
  if ($('history').dataset.signature === signature) return;
  $('history').dataset.signature = signature;
  const nodes = [];
  let previous = 0;
  messages.forEach((message, index) => {
    if (message.at - previous > 600) {
      const time = document.createElement('time');
      time.textContent = new Date(message.at * 1000).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
      nodes.push(time);
    }
    const next = messages[index + 1];
    const line = document.createElement('p');
    const opens = message.at - previous > 600, closes = !next || next.at - message.at > 600;
    line.className = 'line ' + (message.role === 'robot' ? 'robot' : 'user') + (opens && closes ? ' only' : closes ? ' last' : '');
    const who = document.createElement('span');
    who.className = 'who';
    who.textContent = message.role === 'robot' ? 'BITCH' : 'You';
    line.append(who, document.createTextNode(message.text));
    nodes.push(line);
    previous = message.at;
  });
  $('history').replaceChildren(...nodes);
}

function renderLookups(state) {
  const container = $('lookups');
  const active = (state.tasks || []).filter(task => ['working', 'background', 'queued'].includes(task.state));
  container.hidden = !active.length;
  if (!active.length) {
    container.dataset.signature = '';
    return;
  }
  const labels = active.map(task => {
    const secs = Math.max(1, Math.round(state.now - (task.started || state.now)));
    const req = (task.request || '').split('\n')[0].trim();
    const stateLabel = task.state === 'queued' ? 'Queued' : 'Searching';
    return {id: task.id, req, stateLabel, secs};
  });
  const signature = JSON.stringify(labels);
  if (container.dataset.signature === signature) return;
  container.dataset.signature = signature;
  container.replaceChildren(...labels.map(item => {
    const pill = document.createElement('div');
    pill.className = 'lookup-pill';
    const dot = document.createElement('span');
    dot.className = 'lookup-dot';
    dot.setAttribute('aria-hidden', 'true');
    const text = document.createElement('span');
    text.className = 'lookup-text';
    text.textContent = item.req;
    const meta = document.createElement('span');
    meta.className = 'lookup-meta';
    meta.textContent = `${item.stateLabel} · ${item.secs}s`;
    pill.append(dot, text, meta);
    return pill;
  }));
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
  const words = {
    working: 'Looking into it…',
    background: 'Working in the background — will notify you',
    queued: 'Queued on the Pi',
    failed: 'Couldn’t reach the Pi browser',
    unread: 'Sent — check the Pi browser tab',
    lost: 'Lost connection to the Pi',
    slow: 'Timed out on the Pi',
    canceled: 'Canceled',
  };
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
  const waking = Boolean(state.life?.alarm_ringing || state.life?.napping || state.rhythm === 'asleep'
    || state.power?.state === 'sleeping');
  renderPower(state.power);
  $('wakeBtn').hidden = !waking;
  $('wakeBtn').textContent = state.life?.alarm_ringing ? 'Stop alarm' : 'Wake up';
  $('wakeBtn').classList.toggle('alarm', Boolean(state.life?.alarm_ringing));
  $('voiceToggle').setAttribute('aria-pressed', String(settings.muted));
  $('voiceToggle').setAttribute('aria-label', settings.muted ? 'Turn BITCH’s voice back on' : 'Mute BITCH’s voice');
  setIcon($('voiceToggle'), settings.muted ? 'i-voice-off' : 'i-voice');
  $('micToggle').setAttribute('aria-pressed', String(settings.mic_muted));
  $('micToggle').setAttribute('aria-label', settings.mic_muted ? 'Turn my microphone back on' : 'Turn off my microphone');
  setIcon($('micToggle'), settings.mic_muted ? 'i-mic-off' : 'i-mic');
  renderNow(state, feeling);
  renderCompany(state, feeling);
  renderLookups(state);
  renderFaces(state, feeling).catch(() => {});
  if ($('person').open) updatePerson(state.people);
  if (previewSource !== settings.camera_source) {
    previewSource = settings.camera_source;
    frameId = 0;
    clearPreview();
  }

  if (!$('settings').open) return;
  renderPeople(state.people);
  renderTasks(state.tasks);
  const entries = capability => devices
    .filter(device => device.online && device.caps.includes(capability))
    .map(device => [device.id, device.name + (device.id === browserDevice ? ' (this device)' : '')]);
  options('mic_source', [['off', 'Off'], ...entries('ears')], settings.mic_source);
  options('camera_source', [['off', 'Off'], ...entries('eyes')], settings.camera_source);
  const cameraOn = devices.some(device => device.id === settings.camera_source && device.online);
  $('cameraView').hidden = $('attentionRow').hidden = !cameraOn;
  options('speaker_source', [['auto', 'Automatic'], ['off', 'Off'], ...entries('voice')], settings.speaker_source);
  if (document.activeElement !== $('brain_source')) $('brain_source').value = settings.brain_source || 'local';
  $('speakerTest').textContent = state.output ? 'Test ' + (state.output === 'Raspberry Pi' ? 'Pi speaker' : state.output) : 'Test speaker';
  const robot = devices.find(device => device.id === 'robot');
  $('robotStatus').textContent = robot ? robot.detail : 'Off';
  $('modelName').textContent = state.model || '';
  $('notifStatus').textContent = state.life?.notifications?.detail || 'Unavailable';
  if ($('picture').hidden) $('drive').textContent = state.body?.mode || 'Waiting for Pi';
  if (document.activeElement !== $('robot_host')) $('robot_host').value = settings.robot_host;
  $('robot_key').placeholder = settings.robot_paired ? 'Saved' : 'Paste key';
  for (const key of ['idle', 'keep_audio', 'check_ins', 'natural_memory', 'sounds', 'sleep_at_night']) $(key).checked = settings[key];
  const hours = Array.from({length: 24}, (_, hour) => [String(hour), new Date(2000, 0, 1, hour).toLocaleTimeString([], {hour: 'numeric'})]);
  options('quiet_start', hours.slice(), String(settings.quiet_start));
  options('quiet_end', hours.slice(), String(settings.quiet_end));
  $('nightRow').hidden = !settings.sleep_at_night;
  $('motors').textContent = state.wear ? state.wear.moving_minutes + ' min moving · ' + Math.round(state.wear.powered_minutes) + ' min on' : 'Needs the Pi update';
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
    const radius = 6.5 + (feeling === 'hearing' ? Math.min(1.4, eyes.hearing * 3) : feeling === 'listening' ? .6 : 0);
    node.setAttribute('r', radius);
    // The glint stays up and to the left, like light from the room catching a plastic eye.
    const glint = $(index ? 'glintR' : 'glintL');
    glint.setAttribute('cx', (index ? 54 : 18) + pupil.x - radius * .34);
    glint.setAttribute('cy', 19 + pupil.y - radius * .36);
  });
  // Blink now and then; close halfway when the mic is off and fully when BITCH is offline.
  const rest = feeling === 'offline' || feeling === 'asleep' ? .86 : feeling === 'off' ? .45 : feeling === 'resting' ? .3 : 0;
  if (now > eyes.blinkAt) eyes.blinkAt = now + 2600 + Math.random() * 4200;
  const blinking = !eyes.still.matches && eyes.blinkAt - now < 140;
  const wanted = blinking ? .9 : rest;
  eyes.lid += (wanted - eyes.lid) * Math.min(1, dt * (blinking ? 40 : 10));
  // A round cardboard lid slides down over each eye.
  for (const id of ['lidL', 'lidR']) $(id).setAttribute('cy', (-12 + eyes.lid * 31).toFixed(2));
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
  $('enrollNoFrame').textContent = previewSource === 'off' ? 'Choose a camera in Settings first.' : 'Waiting for the camera…';
  $('noFrame').hidden = $('enrollNoFrame').hidden = false;
  $('picture').hidden = $('enrollPicture').hidden = true;
  $('attention').textContent = 'No one in view';
}

async function drawFrame(response, source, target) {
  const scene = JSON.parse(response.headers.get('X-Scene'));
  const jpeg = URL.createObjectURL(await response.blob());
  try {
    const image = new Image();
    image.src = jpeg;
    await image.decode();
    if (source !== previewSource) return;
    const canvas = $(target);
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
    $(target === 'picture' ? 'noFrame' : 'enrollNoFrame').hidden = true;
    canvas.hidden = false;
  } finally {
    URL.revokeObjectURL(jpeg);
  }
}

async function preview() {
  let delay = 0;
  const source = previewSource;
  const target = $('person').open && enroll.step === 'face' ? 'enrollPicture' : $('settings').open ? 'picture' : null;
  if (document.hidden || source === 'off' || !target) {
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
          await drawFrame(response, source, target);
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
  const list = $('profiles');
  const signature = JSON.stringify(people.profiles);
  if (list.dataset.signature === signature) return;
  list.dataset.signature = signature;
  list.replaceChildren(...people.profiles.map(person => {
    const row = document.createElement('div');
    row.className = 'row person-row';
    const name = document.createElement('button');
    name.className = 'text-button';
    name.type = 'button';
    name.textContent = person.name;
    name.setAttribute('aria-label', 'Teach BITCH ' + person.name + '’s face and voice again');
    name.onclick = () => openPerson(person.name).catch(fail);
    const known = document.createElement('span');
    known.className = 'value';
    known.textContent = [person.relation, person.face && 'Face', person.voice && 'Voice'].filter(Boolean).join(' · ') || 'Nothing yet';
    const remove = document.createElement('button');
    remove.className = 'text-button';
    remove.type = 'button';
    remove.textContent = 'Forget';
    remove.setAttribute('aria-label', 'Forget ' + person.name);
    remove.onclick = () => api('people', {action: 'remove', name: person.name}).catch(fail);
    row.append(name, known, remove);
    return row;
  }));
}

// Adding someone: name, then face, then voice. Either can be skipped.
const enroll = {step: 'name', name: '', saw: null};

function showStep(step) {
  enroll.step = step;
  for (const [id, name] of [['stepName', 'name'], ['stepFace', 'face'], ['stepVoice', 'voice'], ['stepDone', 'done']]) $(id).hidden = step !== name;
  $('personNext').textContent = step === 'name' ? 'Next' : step === 'done' ? 'Done' : 'Skip';
  $('cancelPerson').style.visibility = step === 'done' ? 'hidden' : '';
  $('personTitle').textContent = step === 'name' ? 'New person' : enroll.name;
  $('enrollState').textContent = '';
  if (step === 'face') frameId = 0;
}

async function openPerson(name = '', track = null) {
  if ($('settings').open) $('settings').close();
  enroll.name = name;
  enroll.track = track;
  $('personName').value = name;
  $('faceProgress').replaceChildren(...Array.from({length: 6}, () => document.createElement('i')));
  for (const line of $('voiceLines').children) line.className = '';
  $('person').showModal();
  if (name) await learn('face');
  else {
    showStep('name');
    $('personName').focus();
  }
}

async function learn(kind) {
  showStep(kind);
  enroll.saw = null;
  try {
    await api('people', {action: kind, name: enroll.name});
  } catch (error) {
    $('enrollState').textContent = error.message + (kind === 'face' ? ' You can skip to voice.' : '');
  }
}

async function nextStep() {
  if (enroll.step === 'name') {
    enroll.name = $('personName').value.trim();
    if (!enroll.name) throw new Error('Type a name first.');
    if (enroll.track != null) {
      // Picked from the faces in view: BITCH learns that face as the conversation goes on.
      await api('people', {action: 'learn-face', name: enroll.name, track: enroll.track});
      showStep('done');
      $('doneText').textContent = 'BITCH is learning ' + enroll.name + '’s face. Have them say hi so it learns their voice too.';
      return;
    }
    return learn('face');
  }
  if (enroll.step === 'done') return $('person').close();
  await api('people', {action: 'cancel'}).catch(() => {});
  if (enroll.step === 'face') return learn('voice');
  showStep('done');
  $('doneText').textContent = 'Nothing new saved for ' + enroll.name + '.';
}

function updatePerson(people) {
  const session = people.enrollment;
  if (session) {
    enroll.saw = session.kind;
    if (session.kind === 'face') [...$('faceProgress').children].forEach((dot, index) => dot.classList.toggle('on', index < session.progress));
    if (session.kind === 'voice') [...$('voiceLines').children].forEach((line, index) => {
      line.className = index < session.progress ? 'done' : index === session.progress ? 'current' : '';
    });
    if (people.message) $('enrollState').textContent = people.message;
    return;
  }
  // The session closed: saved, timed out, or canceled.
  if (enroll.saw && people.message.startsWith('Saved')) {
    const finished = enroll.saw;
    enroll.saw = null;
    if (finished === 'face') return learn('voice');
    showStep('done');
    const profile = people.profiles.find(person => person.name === enroll.name);
    $('doneText').textContent = profile?.face ? 'BITCH knows ' + enroll.name + '’s face and voice.' : 'BITCH knows ' + enroll.name + '’s voice.';
  } else if (enroll.saw && people.message) {
    enroll.saw = null;
    $('enrollState').textContent = people.message;
  }
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
action('wakeBtn', 'click', () => api('wake', {}));
function renderPower(power) {
  if (!power) return;
  const until = power.until ? ' until ' + new Date(power.until * 1000).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'}) : '';
  const words = {
    awake: `Awake · ${power.memory_free}% of the Mac’s memory free`,
    waking: 'Waking up' + (power.eta ? ` · about ${power.eta} s left` : '…'),
    sleeping: power.reason === 'memory' ? 'Deep sleep · the Mac ran low on memory' : `Deep sleep${until}`,
  }[power.state];
  if ($('powerState').textContent !== words) $('powerState').textContent = words;
  for (const button of $('restChoices').querySelectorAll('button')) button.disabled = power.state !== 'awake';
}
$('restChoices').addEventListener('click', async event => {
  const button = event.target.closest('button[data-minutes]');
  if (!button) return;
  button.disabled = true;
  try {
    renderPower(await (await api('power/sleep', {minutes: Number(button.dataset.minutes)})).json());
  } catch (error) {
    fail(error);
  }
});
action('openSettings', 'click', async () => {
  $('settings').showModal();
  await Promise.all([refresh(), loadMemories()]);
});

async function loadMemories(data) {
  const {memories} = data || await (await api('memory')).json();
  if (!memories.length) {
    const p = document.createElement('p');
    p.className = 'row muted';
    p.textContent = 'Nothing yet';
    return $('memories').replaceChildren(p);
  }
  $('memories').replaceChildren(...memories.slice(0, 40).map(item => {
    const row = document.createElement('div');
    row.className = 'row memory';
    const text = document.createElement('p');
    const when = new Date(item.t * 1000).toLocaleDateString([], {month: 'short', day: 'numeric'});
    const meta = document.createElement('span');
    meta.className = 'meta';
    meta.textContent = (item.kind === 'episode' ? 'Conversation' : 'About ' + (item.about || 'you')) + ' · ' + when;
    text.append(meta, document.createTextNode(item.text));
    const forget = document.createElement('button');
    forget.className = 'text-button';
    forget.type = 'button';
    forget.textContent = 'Forget';
    forget.onclick = async () => loadMemories(await (await api('memory', {id: item.id})).json()).catch(fail);
    row.append(text, forget);
    return row;
  }));
}
action('cleanMemory', 'click', async () => {
  const button = $('cleanMemory');
  button.disabled = true;
  button.textContent = 'Cleaning…';
  try {
    const data = await (await api('memory/clean', {})).json();
    await loadMemories(data);
    button.textContent = data.removed.length ? `Removed ${data.removed.length}` : 'Already tidy';
  } finally {
    setTimeout(() => { button.disabled = false; button.textContent = 'Clean up memory'; }, 2500);
  }
});
action('closeSettings', 'click', () => $('settings').close());
$('settings').addEventListener('click', event => { if (event.target === $('settings')) $('settings').close(); });
$('settings').addEventListener('close', () => {
  if (calibrating) calibrate('cancel').catch(fail);
});

// Settings
action('speakerTest', 'click', () => api('test-speaker', {}));
for (const key of ['idle', 'keep_audio', 'check_ins', 'natural_memory', 'sounds', 'sleep_at_night', 'quiet_start', 'quiet_end',
  'mic_source', 'camera_source', 'speaker_source', 'brain_source']) {
  action(key, 'change', e => {
    const value = e.target.type === 'checkbox' ? e.target.checked : key.startsWith('quiet_') ? Number(e.target.value) : e.target.value;
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
  location.replace(location.pathname);   // drop ?pair= from a scanned QR code
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
  $('pairQr').src = '/api/pair-qr?t=' + Date.now();   // same code, scannable
  $('pairQrRow').hidden = false;
});
action('addPerson', 'click', () => openPerson());
action('addFromStage', 'click', () => openPerson());
action('personNext', 'click', nextStep);
$('personName').addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); nextStep().catch(fail); } });
action('cancelPerson', 'click', async () => {
  await api('people', {action: 'cancel'}).catch(() => {});
  $('person').close();
});
$('person').addEventListener('cancel', () => api('people', {action: 'cancel'}).catch(() => {}));
action('showEarlier', 'click', () => {
  renderHistory(latest?.messages || []);
  $('earlier').showModal();
  $('history').lastElementChild?.scrollIntoView({block: 'end'});
});
action('closeEarlier', 'click', () => $('earlier').close());
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
    const scanned = new URLSearchParams(location.search).get('pair');   // from the Mac's QR code
    $('secureLink').href = 'https://' + location.hostname + ':' + hello.https + (scanned ? '/?pair=' + scanned : '');
    if (secure && /^\d{6}$/.test(scanned || '')) {
      $('code').value = scanned;
      $('pairForm').requestSubmit();
    }
    $('statusText').textContent = 'Not connected yet';
    return;
  }
  $('stage').hidden = $('composer').hidden = $('barActions').hidden = false;
  $('robotSetup').hidden = $('addDevice').hidden = !hello.local;
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
