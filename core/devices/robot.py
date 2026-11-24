"""The robot today: the Raspberry Pi running listen mode (brain.py), reached over Wi-Fi on port 8765.

Its body moves and its speaker (the JBL) is BITCH's desk voice. The Pi does the lip-sync and the springy motion;
this driver just tells it what to do. If the Pi has the newer brain.py it can also play invented moves
(/move); with an older one, invented moves fall back to turning and nodding only.
"""
import json, threading, time, urllib.parse, urllib.request
from .base import Device


class Robot(Device):
    kind = 'robot'
    priority = 10

    def __init__(s, host='raspberrypi.local'):
        super().__init__('robot', 'BITCH robot', caps=('body', 'voice'), place='desk')
        s.base = f'http://{host}:8765'
        s.host = host
        s.online = False
        s.can_move = False
        s.busy = False
        s._look = None; s._looked = None
        s._fallback = None

    # ----- connection -----
    def _get(s, path, timeout=3, **q):
        try:
            r = json.loads(urllib.request.urlopen(s.base + path + ('?' + urllib.parse.urlencode(q) if q else ''), timeout=timeout).read())
            s._alive(True, r); return r
        except Exception:
            s._alive(False); return None

    def _post(s, path, data, ctype, timeout=15, **q):
        try:
            req = urllib.request.Request(s.base + path + ('?' + urllib.parse.urlencode(q) if q else ''), data=data, method='POST',
                                         headers={'Content-Type': ctype})
            r = json.loads(urllib.request.urlopen(req, timeout=timeout).read()); s._alive(True, r); return r
        except Exception:
            s._alive(False); return None

    def _alive(s, ok, r=None):
        if r:
            s.busy = bool(r.get('busy'))
            s.can_move = 'move' in (r.get('caps') or [])
        if ok != s.online:
            s.online = ok
            s.detail = (f'Connected at {s.host}' + ('' if s.can_move else ' (update the Pi for invented moves)')) if ok else \
                       f'Not found at {s.host}. On the Pi run: python3 -c "from brain import *; listen()"'
            if s.hub: s.hub.devices_changed()

    def start(s):
        s.detail = f'Looking for the robot at {s.host}...'
        def ping():
            while True:
                s._get('/ping'); time.sleep(3)
        def tell():                                   # where to look: newest wins, never blocks the camera
            while True:
                q = s._look
                if q is not None and q != s._looked and s.online:
                    s._looked = q
                    try: urllib.request.urlopen(s.base + '/look?' + urllib.parse.urlencode(q), timeout=0.8).read()
                    except Exception: pass
                time.sleep(0.08)
        threading.Thread(target=ping, daemon=True).start()
        threading.Thread(target=tell, daemon=True).start()

    # ----- outputs -----
    def speak(s, wav, seconds, text='', emotion='neutral', move='none'):
        s._post('/speak', wav, 'audio/wav', emotion=emotion, action=move or 'none', text=text)

    def hush(s):
        s._get('/stop', timeout=1.5)

    def gesture(s, emotion='neutral', move='none'):
        threading.Thread(target=s._get, args=('/emote',), kwargs=dict(emotion=emotion, action=move or 'none'), daemon=True).start()

    def show(s, name):
        emo = {'dance': 'excited', 'sing': 'happy', 'sleep': 'sleepy', 'wake': 'happy'}.get(name, 'happy')
        s._get('/emote', emotion=emo, action=name)

    def look(s, b=None, n=None, eye=False, seen=True):
        if not seen: s._look = {'seen': 0}; return
        q = {'seen': 1, 'greet': 0, 'eye': int(bool(eye))}
        if b is not None: q['b'] = f'{b:.2f}'
        if n is not None: q['n'] = f'{n:.2f}'
        s._look = q

    def perform(s, steps, name=''):
        if s.can_move and s._post('/move', json.dumps({'name': name, 'steps': steps}).encode(), 'application/json'):
            return
        # older Pi: act it out with what /look and /emote can do (turning and nodding, plus chirps)
        def act():
            from ..moves import timeline
            for t, pose, sound in timeline(steps, fps=10):
                if sound: s._get('/emote', emotion={'happy': 'happy', 'excited': 'excited', 'sad': 'sad'}.get(sound, 'curious'), action='none')
                s._look = {'seen': 1, 'greet': 0, 'b': f"{pose['base']:.2f}", 'n': f"{pose['neck']:.2f}"}
                time.sleep(0.1)
            s._look = {'seen': 0}
        threading.Thread(target=act, daemon=True).start()
