"""Where BITCH keeps its things, and the settings you can change in the app.

Everything personal lives in ~/.bitch (never in the git repo, which is public):
  settings.json   your settings
  bitch.db        memory: conversations, facts, reminders, skills, what it did on its own
  faces/          the faces it knows
  tls/            the certificate that lets your phone use the mic and notifications
  push.pem        the key that signs notifications to your phone
"""
import json, os, threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = os.environ.get('BITCH_HOME') or os.path.expanduser('~/.bitch')   # BITCH_HOME lets tests use a scratch folder
APP_DIR = os.path.join(ROOT, 'app')
os.makedirs(HOME, exist_ok=True)

DEFAULTS = {
    'owner': 'Brandon',              # the person BITCH belongs to
    'chat_model': 'qwen3.5:4b',      # fast brain: conversation
    'deep_model': 'qwen3.5:9b',      # slower, smarter brain: new moves, memories, deciding to speak up
    'embed_model': 'nomic-embed-text',
    'voice': 'words',                # 'words' or 'babble'
    'kokoro_voice': 'af_heart',
    'mac_voice': 'Samantha',
    'robotness': 0.65,                # robot effect on the voice, 0..1
    'mic_on': True,
    'muted': False,
    'camera_on': True,               # False: the camera is fully off (its light goes out) until you turn it back on                  # muted: BITCH never speaks out loud (it still answers in the app)
    'greet': True,                   # greet people by name when they arrive
    'chattiness': 0.5,               # how often it speaks up on its own, 0 (never) .. 1 (a lot)
    'quiet_start': 22, 'quiet_end': 8,   # quiet hours: no unprompted chatter (reminders still ring)
    'morning_brief': True,           # a short hello + plan the first time it sees you each morning
    'location': None,                # {'name','lat','lon','tz'} for the weather; BITCH asks for it
    'follow': {'mode': 'closest', 'who': None},
    'camera': {'x': 0.0, 'y': 15.0, 'z': 5.0, 'yaw': 0.0, 'fov': 70.0, 'face_cm': 15.0, 'flip': False, 'gaze_bias': 0.0},
}

_F = os.path.join(HOME, 'settings.json')
_lock = threading.Lock()
_s = json.loads(json.dumps(DEFAULTS))
try:
    _s.update(json.load(open(_F)))
except Exception:
    pass
try:   # the old companion kept the camera placement here
    if not os.path.exists(_F):
        _s['camera'].update(json.load(open(os.path.expanduser('~/.desk_camera.json'))))
except Exception:
    pass
_listeners = []
args = None          # filled in by main.py


def get(key, default=None):
    with _lock:
        v = _s.get(key, DEFAULTS.get(key, default))
        return json.loads(json.dumps(v)) if isinstance(v, (dict, list)) else v


def set(key, value):
    with _lock:
        _s[key] = value
        try:
            tmp = _F + '.tmp'
            json.dump(_s, open(tmp, 'w'), indent=1)
            os.replace(tmp, _F)
        except Exception:
            pass
    for fn in _listeners:
        try: fn(key, value)
        except Exception: pass


def update(**kv):
    for k, v in kv.items(): set(k, v)


def all():
    with _lock:
        return json.loads(json.dumps(_s))


def on_change(fn):
    _listeners.append(fn)
