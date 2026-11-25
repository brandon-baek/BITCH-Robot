"""One setup page, with local access and pairing for external sensors."""
import hashlib
import json
import os
import re
import secrets
import threading
import time

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response

from . import config, memory, tls, voicechat
from .devices.web import WebDevice
from .hub import hub
from .people import people

HTTP = int(os.environ.get('BITCH_PORT', '8780'))
HTTPS = HTTP + 1
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
pair_lock = threading.Lock()
pairing = None
attempts = 0
HOST = re.compile(r'(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)*[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?')
DEVICE = re.compile(r'[0-9a-f]{32}')


def valid_host(host):
    return isinstance(host, str) and len(host) <= 253 and bool(HOST.fullmatch(host))


def local(connection):
    return bool(connection.client and connection.client.host in ('127.0.0.1', '::1')
                and connection.url.hostname in ('127.0.0.1', 'localhost', '::1'))


def identity(connection):
    if local(connection):
        return 'local'
    token = connection.cookies.get('bitch', '')
    if connection.url.scheme in ('https', 'wss') and token and token in memory.kv_get('tokens', {}):
        return hashlib.sha256(token.encode()).hexdigest()[:24]
    return None


def same_origin(connection):
    origin = connection.headers.get('origin')
    scheme = 'https' if connection.url.scheme in ('https', 'wss') else 'http'
    return not origin or origin == f'{scheme}://{connection.headers.get("host")}'


def authorize(req):
    owner = identity(req)
    if not owner:
        raise HTTPException(401, 'Pair this device first.')
    if not same_origin(req):
        raise HTTPException(403, 'Open the setup page directly.')
    return owner


async def payload(req):
    try:
        data = await req.body()
        if len(data) > 8000:
            raise ValueError
        body = json.loads(data)
        if not isinstance(body, dict):
            raise ValueError
        return body
    except ValueError:
        raise HTTPException(400, 'Invalid request.')


@app.get('/')
def index():
    return FileResponse(config.APP_DIR / 'index.html', headers={'Cache-Control': 'no-store'})


@app.get('/app/{name}')
def asset(name: str):
    if name not in ('app.js', 'style.css', 'mic.js'):
        raise HTTPException(404)
    return FileResponse(config.APP_DIR / name, headers={'Cache-Control': 'no-store'})


@app.get('/sw.js')
def retire_worker():
    script = """self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.registration.unregister()));"""
    return Response(script, media_type='application/javascript', headers={'Cache-Control': 'no-store'})


@app.get('/BITCH-Home.crt')
def certificate():
    if not os.path.isfile(tls.CA_DER):
        raise HTTPException(503, 'Secure setup is unavailable.')
    return FileResponse(tls.CA_DER, media_type='application/x-x509-ca-cert', filename='BITCH-Home.crt')


@app.get('/api/hello')
def hello(req: Request):
    return {'local': local(req), 'paired': bool(identity(req)), 'host': tls.host(), 'http': HTTP, 'https': HTTPS}


@app.get('/api/pair-code')
def pair_code(req: Request):
    global pairing, attempts
    if not local(req) or not same_origin(req):
        raise HTTPException(403)
    with pair_lock:
        if not pairing or pairing[1] < time.monotonic():
            pairing = f'{secrets.randbelow(10 ** 6):06d}', time.monotonic() + 300
            attempts = 0
        return {'code': pairing[0]}


@app.post('/api/pair')
async def pair(req: Request):
    global pairing, attempts
    if req.url.scheme != 'https' or not same_origin(req):
        raise HTTPException(403, 'Use the secure setup link.')
    data = await payload(req)
    with pair_lock:
        if not pairing or pairing[1] < time.monotonic() or attempts >= 10:
            raise HTTPException(403, 'Get a fresh code from the Mac.')
        attempts += 1
        if not secrets.compare_digest(str(data.get('code', '')).encode(), pairing[0].encode()):
            if attempts >= 10:
                pairing = None
            raise HTTPException(403, 'That code did not match.')
        token = secrets.token_urlsafe(32)
        tokens = memory.kv_get('tokens', {})
        tokens[token] = {'t': time.time()}
        memory.kv_set('tokens', tokens)
        pairing = None
    response = JSONResponse({'ok': True})
    response.set_cookie('bitch', token, max_age=365 * 86400, httponly=True, secure=True, samesite='strict')
    return response


@app.get('/api/state')
def state(req: Request):
    owner = authorize(req)
    data = hub.snapshot()
    remaining = hub.brain.followup_seconds()
    if data['status'] == 'Listening for your turn' and not remaining:
        data['status'] = 'Waiting for “BITCH”'
    phase = hub.hearing.phase() if hasattr(hub.hearing, 'phase') else None
    data['turn_phase'] = phase or ('Ready' if remaining else data['status'])
    data['followup_seconds'] = round(remaining, 1)
    data['hearing_speech'] = bool(remaining and time.monotonic() - hub.hearing.activity_at < 0.3)
    data['settings'] = config.public()
    data['body'] = getattr(hub.sight, 'drive', {})
    data['show_active'] = bool(getattr(hub.get('robot'), 'show_active', False))
    output = hub.selected('voice')
    data['output'] = output.name if output else None
    data['model'] = config.get('chat_model')
    data['voicechat_ready'] = voicechat.settings() is not None
    data['ears_ready'] = hub.hearing.ready
    data['finger_ready'] = bool(getattr(getattr(hub.sight, 'pointing', None), 'hand_ready', False))
    data['pointing_ready'] = bool(getattr(getattr(hub.sight, 'pointing', None), 'ready', False))
    if owner == 'local':
        data['people'] = people.state()
        data['speaker_name'] = hub.brain.speaker if remaining else None
    return data


@app.post('/api/settings')
async def settings(req: Request):
    owner = authorize(req)
    data = await payload(req)
    for key, value in data.items():
        if key in ('idle', 'muted', 'keep_audio', 'check_ins', 'natural_memory') and type(value) is bool:
            continue
        if key == 'voicechat_enabled' and owner == 'local' and type(value) is bool:
            if value and not voicechat.settings():
                raise HTTPException(409, 'Install the VoiceChat runtime and model first.')
            continue
        if key == 'volume' and type(value) is int and 0 <= value <= 100:
            continue
        if key in ('mic_source', 'camera_source', 'speaker_source') and isinstance(value, str):
            device = hub.get(value)
            capability = {'mic_source': 'ears', 'camera_source': 'eyes', 'speaker_source': 'voice'}[key]
            if value == 'off' or key == 'speaker_source' and value in ('auto', 'robot') or device and device.active(capability):
                continue
        if owner == 'local' and key == 'robot_host' and valid_host(value):
            continue
        if owner == 'local' and key == 'robot_key' and isinstance(value, str) and re.fullmatch('[A-Za-z0-9_-]{0,64}', value):
            continue
        raise HTTPException(400, f'Invalid setting: {key}.')
    config.update(data)
    if data.get('muted'):
        hub.brain.stop()
    if 'mic_source' in data:
        hub.level = 0
        if data['mic_source'] == 'mac-mic':
            from .devices.mac import ask_permission
            ask_permission('mic')
    if data.get('camera_source') == 'mac-camera':
        from .devices.mac import ask_permission
        ask_permission('camera')
    return {'ok': True}


@app.post('/api/test-speaker')
def test_speaker(req: Request):
    authorize(req)
    if config.get('muted') or not config.get('volume'):
        raise HTTPException(400, 'Unmute the voice and raise its volume first.')
    if not hub.selected('voice'):
        raise HTTPException(400, 'Choose an available speaker first.')
    if not hub.brain.test_speaker():
        raise HTTPException(409, 'Wait until this turn finishes.')
    return {'ok': True}


@app.post('/api/stop')
def stop(req: Request):
    authorize(req)
    hub.brain.stop()
    return {'ok': True}


@app.post('/api/people')
async def profiles(req: Request):
    if authorize(req) != 'local':
        raise HTTPException(403, 'Manage profiles on the Mac.')
    data = await payload(req)
    action = data.get('action')
    name = data.get('name', '')
    if not isinstance(name, str):
        raise HTTPException(400, 'Enter a name.')
    if action in ('face', 'voice'):
        source = hub.selected('eyes' if action == 'face' else 'ears')
        if not source or action == 'voice' and not hub.hearing.ready:
            raise HTTPException(400, 'Choose an active camera or microphone first.')
        try:
            people.begin(name, action, source.id)
        except ValueError as error:
            raise HTTPException(400, str(error))
        hub.brain.stop()
    elif action == 'cancel':
        people.cancel()
    elif action == 'remove':
        people.remove(name)
        if hub.brain.speaker == name:
            hub.brain.stop()
    else:
        raise HTTPException(400, 'Unknown profile action.')
    return people.state()


@app.get('/api/frame')
def frame(req: Request, after: int = 0):
    owner = authorize(req)
    frame = hub.sight.picture(after)
    if frame is None:
        return Response(status_code=204, headers={'Cache-Control': 'no-store'})
    sequence, image, info = frame
    if owner != 'local':
        info['people'] = [{key: value for key, value in entry.items() if key != 'name'} for entry in info['people']]
    return Response(image, media_type='image/jpeg', headers={
        'Cache-Control': 'no-store', 'X-Frame': str(sequence), 'X-Scene': json.dumps(info, separators=(',', ':')),
    })


@app.post('/api/calibrate')
async def calibrate(req: Request):
    owner = authorize(req)
    data = await payload(req)
    action = data.get('action')
    if action not in ('start', 'aim', 'sample', 'save', 'cancel'):
        raise HTTPException(400, 'Unknown calibration step.')
    try:
        angle = float(data['angle']) if action in ('aim', 'sample') else None
        return {'samples': hub.sight.calibration(action, owner, angle, float(data.get('neck', -0.8)), track=data.get('track') is True)}
    except (TypeError, KeyError, ValueError) as error:
        raise HTTPException(400, str(error))


@app.websocket('/link/{device}')
async def link(ws: WebSocket, device: str):
    owner = identity(ws)
    if not owner or not DEVICE.fullmatch(device) or not same_origin(ws):
        await ws.close(code=4401)
        return
    await ws.accept()
    name = 'This Mac browser' if owner == 'local' else 'Paired device ' + device[:4]
    session = WebDevice(f'web-{owner}-{device}', name)
    hub.add(session)
    try:
        while True:
            message = await ws.receive()
            if message['type'] == 'websocket.disconnect':
                break
            data = message.get('bytes')
            if data is not None:
                if hub.get(session.id) is session:
                    session.receive(data)
            elif message.get('text') in ('ears', 'eyes', 'ears,eyes', ''):
                session.caps = set(message['text'].split(',')) - {''}
                await ws.send_json({'device': session.id})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        hub.remove(session)


def start():
    import uvicorn

    def run(port, **options):
        uvicorn.Server(uvicorn.Config(app, port=port, host='0.0.0.0', log_level='warning',
                                      proxy_headers=False, ws_max_size=180_000, **options)).run()

    threading.Thread(target=run, args=(HTTP,), daemon=True).start()
    try:
        certificate, key = tls.ensure()
    except Exception as error:
        hub.note('tls', f'Secure external connections unavailable: {error}')
        return
    threading.Thread(target=run, args=(HTTPS,), kwargs={'ssl_certfile': certificate, 'ssl_keyfile': key}, daemon=True).start()
