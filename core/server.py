"""The web side of BITCH: serves the app to your phone and the Mac window, and the /link every device talks over.

  http://localhost:8780            the Mac window (trusted, no pairing)
  http://<your Mac>.local:8780     a phone, first time: pair with the code shown on the Mac, get the certificate
  https://<your Mac>.local:8781    a phone, after installing the certificate: mic, camera and notifications work

Phones pair once with a 6-digit code shown in the Mac's BITCH window (More > Devices). After that they keep a
secret cookie. Nothing here is reachable from outside your home network.
"""
import asyncio, json, os, secrets, threading, time
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from . import config, memory, moves, push, tls
from .devices.base import CAP_LABELS
from .devices.web import WebDevice, saved, save, forget
from .hub import hub

HTTP = int(os.environ.get('BITCH_PORT', 8780)); HTTPS = HTTP + 1      # BITCH_PORT lets tests run next to the real BITCH
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
PAIR = {'code': None, 'until': 0}
TRIES = {}


def local(req):
    return (req.client.host if req.client else '') in ('127.0.0.1', '::1')


def paired(req_or_ws):
    tok = req_or_ws.cookies.get('bitch')
    return bool(tok) and tok in memory.kv_get('tokens', {})


def allowed(req):
    return local(req) or paired(req)


def pair_code():
    if not PAIR['code'] or time.time() > PAIR['until']:
        PAIR['code'] = f'{secrets.randbelow(10 ** 6):06d}'; PAIR['until'] = time.time() + 600
    return PAIR['code']


def need(req):
    if not allowed(req): raise HTTPException(401, 'pair this device first')


NOCACHE = {'Cache-Control': 'no-cache'}


# ---------------- the app itself (no pairing needed to load it) ----------------
@app.get('/')
def index(): return FileResponse(os.path.join(config.APP_DIR, 'index.html'), headers=NOCACHE)


@app.get('/sw.js')
def sw(): return FileResponse(os.path.join(config.APP_DIR, 'sw.js'), media_type='application/javascript', headers=NOCACHE | {'Service-Worker-Allowed': '/'})


@app.get('/manifest.webmanifest')
def manifest(): return FileResponse(os.path.join(config.APP_DIR, 'manifest.webmanifest'), media_type='application/manifest+json')


@app.get('/icon.png')
def icon(): return FileResponse(os.path.join(config.ROOT, 'mac', 'icon.png'), media_type='image/png')


@app.get('/app/{name}')
def asset(name: str):
    p = os.path.join(config.APP_DIR, os.path.basename(name))
    if not os.path.isfile(p): raise HTTPException(404)
    return FileResponse(p, headers=NOCACHE)


@app.get('/BITCH-Home.crt')
def ca():
    tls.ensure()
    return FileResponse(tls.CA_DER, media_type='application/x-x509-ca-cert', filename='BITCH-Home.crt')


@app.get('/api/hello')
def hello(req: Request):
    return {'paired': allowed(req), 'local': local(req), 'host': tls.host(), 'https': HTTPS, 'http': HTTP,
            'secure': req.url.scheme == 'https', 'owner': config.get('owner')}


@app.post('/api/pair')
async def pair(req: Request):
    ip = req.client.host if req.client else '?'
    TRIES[ip] = [t for t in TRIES.get(ip, []) if time.time() - t < 300]
    if len(TRIES[ip]) >= 6: raise HTTPException(429, 'Too many tries. Wait five minutes.')
    TRIES[ip].append(time.time())
    body = await req.json()
    if str(body.get('code', '')).strip() != pair_code(): raise HTTPException(403, 'That code is not right.')
    tok = secrets.token_urlsafe(32); toks = memory.kv_get('tokens', {})
    toks[tok] = {'name': str(body.get('name') or 'Phone')[:40], 't': time.time()}; memory.kv_set('tokens', toks)
    PAIR['code'] = None
    hub.chat('note', f'(paired a new device: {toks[tok]["name"]})')
    r = JSONResponse({'ok': True})
    r.set_cookie('bitch', tok, max_age=10 * 365 * 86400, httponly=True, samesite='lax')
    return r


@app.get('/api/pair-code')
def get_pair_code(req: Request):
    if not local(req): raise HTTPException(403)
    return {'code': pair_code(), 'until': PAIR['until'], 'host': tls.host(), 'http': HTTP, 'https': HTTPS, 'ips': tls.ips()}


# ---------------- the link ----------------
@app.websocket('/link')
async def link(ws: WebSocket):
    if not (ws.client and ws.client.host in ('127.0.0.1', '::1')) and not paired(ws):
        await ws.close(code=4401); return
    await ws.accept()
    loop = asyncio.get_running_loop(); q = asyncio.Queue(); dev = None; viewing = False
    is_local = ws.client.host in ('127.0.0.1', '::1')

    async def sender():
        while True:
            m = await q.get()
            if isinstance(m, (bytes, bytearray)): await ws.send_bytes(m)
            else: await ws.send_text(json.dumps(m, default=str))
    task = asyncio.create_task(sender())
    try:
        while True:
            msg = await ws.receive()
            if msg.get('type') == 'websocket.disconnect': break
            if msg.get('bytes') is not None:
                if dev: dev.got_binary(msg['bytes'])
                continue
            try: m = json.loads(msg.get('text') or '{}')
            except ValueError: continue
            t = m.get('type')
            if t == 'hello':
                did = str(m.get('id') or '')[:40] or ('win-' if is_local else 'phone-') + secrets.token_hex(4)
                kind = 'window' if is_local else 'phone'
                name = 'BITCH window on the Mac' if is_local else (str(m.get('name') or 'Phone')[:40])
                dev = hub.get(did) if isinstance(hub.get(did), WebDevice) else WebDevice(did, name, kind, 'desk' if is_local else 'mobile')
                dev.name = name; dev.attach(loop, q)
                if not is_local: save(did, name=name)
                if hub.get(did) is not dev: hub.add(dev)
                else: hub.devices_changed()
                await q.put({'type': 'welcome', 'id': did, 'push_key': push.public_key(), 'library': moves.library(), 'labels': CAP_LABELS})
                await q.put(hub.snapshot())
            elif not dev: continue
            elif t == 'say':
                text = str(m.get('text') or '').strip()[:1000]
                if text: hub.brain.typed(text, dev.id, speak=bool(m.get('speak', dev.place == 'desk')))
            elif t == 'caps': dev.set_caps(m.get('caps') or [])
            elif t == 'ptt': hub.hearing.hold(dev.id, bool(m.get('on')))
            elif t == 'stop': hub.interrupt()
            elif t == 'view':
                on = bool(m.get('on'))
                if on != viewing: viewing = on; hub.sight.viewers = max(0, hub.sight.viewers + (1 if on else -1))
            elif t == 'push':
                dev.push = m.get('sub'); dev.caps.add('notify'); save(dev.id, push=dev.push); hub.devices_changed()
                await q.put({'type': 'pushed', 'ok': True})
            elif t == 'unpush':
                dev.push = None; dev.caps.discard('notify'); save(dev.id, push=None); hub.devices_changed()
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        task.cancel()
        if viewing: hub.sight.viewers = max(0, hub.sight.viewers - 1)
        if dev and dev.q is q:
            dev.detach()
            if dev.kind == 'window' or not dev.push: hub.remove(dev.id)
            else: hub.devices_changed()


# ---------------- the live view ----------------
@app.get('/api/frame')
def frame(req: Request, after: int = 0):
    need(req); s = hub.sight
    with s.newframe:
        if s.frame_seq <= after: s.newframe.wait(1.0)
        seq, jpg, faces = s.frame_seq, s.jpg, s.faces_json
    if not jpg or seq <= after: return Response(status_code=204)
    return Response(jpg, media_type='image/jpeg', headers={'X-Seq': str(seq), 'X-Faces': faces, 'Cache-Control': 'no-store'})


@app.get('/api/face/{name}')
def face(req: Request, name: str):
    need(req)
    from .faces import FaceDB
    p = FaceDB().photo(name)
    if not os.path.exists(p): raise HTTPException(404)
    return FileResponse(p, media_type='image/jpeg')


# ---------------- the rest of the app ----------------
async def body(req):
    try: return await req.json()
    except Exception: return {}


@app.get('/api/memories')
def get_memories(req: Request, kind: str = None, q: str = None):
    need(req); return memory.memories(kind or None, q or None)


@app.post('/api/memories')
async def add_memory(req: Request):
    need(req); b = await body(req)
    r = memory.remember(str(b.get('text', '')), b.get('kind') or 'fact', importance=int(b.get('importance') or 3))
    hub.ui({'type': 'memories'}); return r or {}


@app.patch('/api/memories/{i}')
async def edit_memory(req: Request, i: int):
    need(req); b = await body(req)
    memory.edit_memory(i, b.get('text'), b.get('importance'), b.get('about'), b.get('kind')); hub.ui({'type': 'memories'}); return {'ok': True}


@app.delete('/api/memories/{i}')
def del_memory(req: Request, i: int):
    need(req); memory.forget(i); hub.ui({'type': 'memories'}); return {'ok': True}


@app.get('/api/life')
def life(req: Request):
    need(req)
    from . import timeparse
    import datetime as dt
    ts = memory.tasks()
    for t in ts:
        t['nice'] = timeparse.nice(dt.datetime.fromtimestamp(t['due']))
        if t['repeat']: t['rule'] = timeparse.describe_rule(json.loads(t['repeat']))
    return {'tasks': ts, 'activity': memory.activity(60), 'mood': hub.life.mood.public(),
            'lists': {n: memory.list_get(n) for n in memory.list_names()}}


@app.delete('/api/tasks/{i}')
def del_task(req: Request, i: int):
    need(req); memory.task_done(i, 'cancelled'); hub.ui({'type': 'tasks'}); return {'ok': True}


@app.post('/api/lists/{name}')
async def edit_list(req: Request, name: str):
    need(req); b = await body(req)
    if b.get('remove') is not None: memory.list_remove(name, [b['remove']])
    elif b.get('clear'): memory.list_remove(name)
    elif b.get('add'): memory.list_add(name, [str(b['add'])])
    hub.ui({'type': 'tasks'}); return {'ok': True}


@app.post('/api/activity/{i}')
async def activity_feedback(req: Request, i: int):
    need(req); b = await body(req); fb = b.get('feedback')
    memory.activity_feedback(i, fb)
    a = next((x for x in memory.activity(200) if x['id'] == i), None)
    if a and fb == 'dislike':
        memory.remember(f'{config.get("owner")} does not want BITCH to do this on its own: {a["text"]}', 'feedback', importance=4)
    return {'ok': True}


@app.get('/api/skills')
def get_skills(req: Request):
    need(req); return memory.skills()


@app.post('/api/skills/play')
async def play_skill(req: Request):
    need(req); b = await body(req); name = str(b.get('name', ''))
    s = memory.skill(name)
    if s and s['kind'] == 'move': threading.Thread(target=hub.brain.play, args=(s['name'], s['body']['steps']), daemon=True).start()
    elif s: hub.brain.run_routine(s['name'])
    elif name in moves.GESTURES: hub.brain.play(name, moves.GESTURES[name])
    return {'ok': True}


@app.post('/api/skills/invent')
async def invent_skill(req: Request):
    need(req); b = await body(req); what = str(b.get('what', '')).strip()[:120]
    if what: threading.Thread(target=hub.brain.invent_and_play, args=(what, None), daemon=True).start()
    return {'ok': True}


@app.delete('/api/skills/{name}')
def del_skill(req: Request, name: str):
    need(req); memory.delete_skill(name)
    for t in memory.tasks():
        if t['kind'] == 'routine' and t['text'] == name: memory.task_done(t['id'], 'cancelled')
    return {'ok': True}


@app.get('/api/settings')
def get_settings(req: Request):
    need(req); return config.all()


SETTABLE = {'camera_on', 'muted', 'owner', 'voice', 'kokoro_voice', 'mac_voice', 'robotness', 'mic_on', 'greet', 'chattiness', 'quiet_start', 'quiet_end',
            'morning_brief', 'chat_model', 'deep_model', 'camera', 'follow'}


@app.post('/api/settings')
async def set_settings(req: Request):
    need(req); b = await body(req)
    for k, v in b.items():
        if k in SETTABLE: config.set(k, v)
    if 'city' in b and b['city']:
        from . import tools
        ok, res = tools.run('set_home', {'city': b['city']}, {'mode': 'event'})
        hub.ui({'type': 'settings', 'settings': config.all()}); return {'ok': ok, 'result': res}
    hub.ui({'type': 'settings', 'settings': config.all()})
    return {'ok': True}


@app.post('/api/act')
async def act(req: Request):
    """Quick actions from the app: gestures, shows, stop, follow, learn a face, aim, calibrate."""
    need(req); b = await body(req); a = b.get('do')
    if a == 'gesture':
        for d in hub.bodies(): d.gesture(b.get('emotion', 'happy'), b.get('move', 'none'))
        if b.get('move') in moves.GESTURES: hub.ui({'type': 'perform', 'name': b['move'], 'steps': moves.GESTURES[b['move']]})
    elif a == 'show':
        name = b.get('name')
        if name == 'sleep': threading.Thread(target=hub.brain.sleep, daemon=True).start()
        elif name == 'wake': threading.Thread(target=hub.brain.wake, daemon=True).start()
        else: hub.brain.show(name)
    elif a == 'stop': hub.interrupt()
    elif a == 'follow':
        w = b.get('who'); config.set('follow', {'mode': b.get('mode', 'closest'), 'who': int(w) if isinstance(w, str) and w.isdigit() else w})
        if b.get('mode') == 'off':
            for d in hub.bodies(): d.look(seen=False)
    elif a == 'learn':
        tid = b.get('id'); hub.sight.learn(str(b.get('name', '')), int(tid) if str(tid).isdigit() else 'target')
    elif a == 'forget_face': hub.sight.forget(str(b.get('name', '')))
    elif a == 'aim': hub.sight.aim(float(b['x']), float(b['y']))
    elif a == 'calibrate':
        t = next((p for p in hub.people if p.get('target')), None)
        if t and t.get('gaze') is not None:
            c = config.get('camera'); c['gaze_bias'] += t['gaze']; config.set('camera', c)
    elif a == 'device':
        d = hub.get(str(b.get('id')))
        if d:
            cap = b.get('cap')
            if b.get('on'): d.off.discard(cap)
            else: d.off.add(cap)
            if isinstance(d, WebDevice): save(d.id, off=sorted(d.off))
            else: memory.kv_set('off_' + d.id, sorted(d.off))
            hub.devices_changed()
    elif a == 'forget_device':
        did = str(b.get('id')); forget(did)
        if isinstance(hub.get(did), WebDevice): hub.remove(did)
    elif a == 'quit' and local(req):
        threading.Timer(0.3, lambda: os._exit(0)).start()
    return {'ok': True}


def start():
    """Run the web side: plain http on 8780 and https on 8781, each in its own thread."""
    import socket, uvicorn
    for port in (HTTP, HTTPS):
        try:
            t = socket.socket(); t.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); t.bind(('0.0.0.0', port)); t.close()
        except OSError:
            hub.note(f'port {port} is busy: is BITCH already running? (quit the other one first)')
    try: cert, key = tls.ensure()
    except Exception as e: cert = key = None; hub.note(f'https is off ({e})')
    for d_id, rec in saved().items():                           # phones with notifications keep working when closed
        if rec.get('push') and not hub.get(d_id):
            d = WebDevice(d_id, rec.get('name', 'Phone')); d.detail = 'App closed (notifications still on)'; hub.add(d)

    def run(**kw):
        cfg = uvicorn.Config(app, host='0.0.0.0', log_level='warning', ws_max_size=8 * 1024 * 1024, **kw)
        srv = uvicorn.Server(cfg); srv.install_signal_handlers = lambda: None
        srv.run()
    threading.Thread(target=run, kwargs={'port': HTTP}, daemon=True).start()
    if cert:
        threading.Thread(target=run, kwargs={'port': HTTPS, 'ssl_certfile': cert, 'ssl_keyfile': key}, daemon=True).start()
