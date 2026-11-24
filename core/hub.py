"""The hub: BITCH's nervous system.

Every device (phone, Mac, robot, future hardware) is registered here. Senses report in, the brain decides,
and the hub routes what BITCH says and does to the right outputs. It also keeps the app's screens up to date.

Routing rules (where a reply goes):
  - You talked into your phone        -> the phone speaks it (and the robot still moves at the desk)
  - You talked or typed at the desk   -> the robot's speaker, or the Mac's if the robot is off
  - BITCH speaks up on its own        -> the desk if someone is there, otherwise a notification on your phone
"""
import threading, time
from . import memory

STATUS_WORDS = ('starting', 'listening', 'hearing', 'understanding', 'thinking', 'speaking', 'asleep')


class Hub:
    def __init__(s):
        s.devices = {}
        s.lock = threading.RLock()
        s.handlers = {}
        s.status = 'starting'
        s.people = []            # who the cameras see right now (see sight.py)
        s.level = 0.0            # mic loudness, for the meter
        s.asleep = False
        s.notes = []             # problems worth showing (camera off, brain missing...)
        s.interrupted = False
        s.speaking_until = 0.0
        s.last_talk = 0.0        # last time anyone talked with BITCH
        s.brain = s.sight = s.hearing = s.life = None   # filled in by main.py

    # ---------- events ----------
    def on(s, event, fn):
        s.handlers.setdefault(event, []).append(fn)

    def emit(s, event, **kw):
        for fn in list(s.handlers.get(event, [])):
            try: fn(**kw)
            except Exception as e: print(f'  [{event}] handler failed: {e!r}', flush=True)

    # ---------- devices ----------
    def add(s, dev):
        dev.hub = s
        with s.lock: s.devices[dev.id] = dev
        try: dev.start()
        except Exception as e: s.note(f'{dev.name} failed to start: {e}')
        s.devices_changed()
        return dev

    def remove(s, dev_id):
        with s.lock: d = s.devices.pop(dev_id, None)
        if d:
            try: d.stop()
            except Exception: pass
        s.devices_changed()

    def get(s, dev_id):
        return s.devices.get(dev_id)

    def with_cap(s, cap):
        with s.lock: return [d for d in s.devices.values() if d.active(cap)]

    def devices_changed(s):
        s.ui({'type': 'devices', 'devices': [d.info() for d in list(s.devices.values())]})

    # ---------- the app's screens ----------
    def ui(s, msg):
        for d in list(s.devices.values()):
            try: d.ui(msg)
            except Exception: pass

    def set_status(s, st):
        if st != s.status:
            s.status = st; s.ui({'type': 'status', 'status': st})

    def note(s, msg, problem=True):
        print('  ' + msg, flush=True)
        s.notes = (s.notes + [{'t': time.time(), 'text': msg, 'problem': problem}])[-8:]
        s.ui({'type': 'notes', 'notes': s.notes})

    def chat(s, role, text, device=None, **meta):
        m = memory.add_message(role, text, device, meta.pop('emotion', None), meta.pop('move', None), **meta)
        s.ui({'type': 'chat', 'msg': m})
        return m

    # ---------- routing ----------
    def place_of(s, dev_id):
        d = s.devices.get(dev_id)
        return d.place if d else 'desk'

    def desk_voice(s):
        v = [d for d in s.with_cap('voice') if d.place == 'desk']
        return max(v, key=lambda d: d.priority) if v else None

    def voices_for(s, origin=None, speak=True):
        """Which voice outputs should say a reply to something that came from device `origin`."""
        if not speak: return []
        o = s.devices.get(origin)
        if o and o.place != 'desk':
            return [o] if o.active('voice') else []
        d = s.desk_voice()
        return [d] if d else []

    def bodies(s):
        return s.with_cap('body')

    def someone_at_desk(s, within=90):
        """True / False, or None when there's no working desk camera to tell."""
        cam = s.devices.get('mac-camera')
        if not (cam and cam.active('eyes') and s.sight and s.sight.eyes and s.sight.seq): return None
        return bool(s.people) or time.time() - s.sight.last_seen < within

    def interrupt(s):
        if time.time() < s.speaking_until:
            s.interrupted = True
            for d in s.with_cap('voice'):
                try: d.hush()
                except Exception: pass
            s.speaking_until = 0
            s.chat('note', '(interrupted)')

    def snapshot(s):
        from . import config
        return {'type': 'hello', 'status': s.status, 'devices': [d.info() for d in list(s.devices.values())],
                'people': s.people, 'notes': s.notes, 'asleep': s.asleep, 'settings': config.all(),
                'known': s.sight.known() if s.sight else [],
                'mood': s.life.mood.public() if s.life else None,
                'chat': memory.recent_messages(60)}


hub = Hub()
