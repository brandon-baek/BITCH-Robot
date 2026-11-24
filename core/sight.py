"""BITCH's sight: camera frames in, people out.

Finds and recognises faces (faces.py), works out where each person is on the desk, whether they are looking at
BITCH, and tells the robot where to look. It also notices arrivals: when someone shows up after the desk has
been empty for a while (or a person it knows comes back), it raises an 'arrived' event so BITCH can greet them.

Only the desk camera (the Mac's, for now) is used for tracking. A phone camera is used for snapshots
("what is this?"), because it moves around with you.
"""
import json, math, threading, time
from . import config
from .hub import hub

AWAY = 600           # seconds the desk must be empty (or a person gone) before it counts as an arrival


def cam():
    return config.get('camera')


def to_robot(px, py, c=None):
    c = c or cam()
    ang = math.degrees(math.atan2(px, max(1e-3, py)))
    return max(-0.9, min(0.9, (ang / 90) * (-1 if c['flip'] else 1)))


def locate(c, cx, cy, fw, W, H):
    """Face in the picture -> where that person is on the desk (cm, robot at 0,0) and how high above the camera."""
    t = math.tan(math.radians(c['fov'] / 2)); tv = t * H / W
    a = math.atan((cx - 0.5) * 2 * t); d = c['face_cm'] / max(0.02, fw * 2 * t)
    up = d * (0.5 - cy) * 2 * tv
    fwd, rt = d * math.cos(a), d * math.sin(a); th = math.radians(c['yaw'])
    return c['x'] + fwd * math.sin(th) + rt * math.cos(th), c['y'] + fwd * math.cos(th) - rt * math.sin(th), up + c['z'], a


def gaze(c, row, px, py):
    """Is this person looking at BITCH? Head turn from the 5 face landmarks, compared with where BITCH sits."""
    if row[4] == 0 and row[5] == 0: return None, None
    rex, rey, lex, ley, nx, ny, rmx, rmy, lmx, lmy = [float(v) for v in row[4:14]]
    ed = max(1.0, math.hypot(lex - rex, ley - rey)); ex, ey = (rex + lex) / 2, (rey + ley) / 2; mx, my = (rmx + lmx) / 2, (rmy + lmy) / 2
    turn = math.degrees(math.asin(max(-1, min(1, 2.1 * ((nx - ex) / ed)))))
    nod = ((ny - ey) / max(1.0, my - ey)) - 0.58
    to_cam = math.atan2(c['x'] - px, c['y'] - py); face_dir = to_cam - math.radians(turn)
    to_bot = math.atan2(-px, -py)
    off = math.degrees(math.atan2(math.sin(face_dir - to_bot), math.cos(face_dir - to_bot))) - c['gaze_bias']
    return off, nod


class Sight:
    def __init__(s):
        s.latest = {}; s.seq = 0; s.got = threading.Event()
        s.frame_seq = 0; s.jpg = None; s.faces_json = '[]'; s.newframe = threading.Condition()
        s.enroll = {}            # {'name', 'track', 'until', 'n'} while learning a face
        s.eyes = None
        s.last_seen = 0.0        # last time anyone was in view
        s.present = {}           # name -> last time seen
        s.arriving = None        # (since, gap) while deciding who just arrived
        s.target_id = None
        s.viewers = 0            # app screens currently watching the live view

    # ----- input from eyes devices -----
    def frame(s, dev_id, fr):
        if dev_id != 'mac-camera' and 'mac-camera' in hub.devices: return     # the desk camera does the tracking
        s.latest[dev_id] = fr; s.seq += 1; s.got.set()

    def start(s):
        threading.Thread(target=s._run, daemon=True).start()

    # ----- people it knows -----
    def known(s):
        return s.eyes.db.names() if s.eyes else []

    def learn(s, name, track='target', secs=8):
        s.enroll = {'name': name.strip()[:24], 'track': track, 'until': time.time() + secs, 'n': 0}

    def forget(s, name):
        s.enroll = {'forget': name}

    def aim(s, x, y):
        config.set('follow', {'mode': 'off', 'who': None})
        for b in hub.bodies(): b.look(to_robot(x, y), -0.8)

    # ----- the loop -----
    def _run(s):
        import cv2
        from .faces import Eyes
        try: s.eyes = Eyes(lambda m: hub.note(m, problem=False))
        except Exception as e: hub.note(f'eyes failed to load: {e}'); return
        for n, v in s.eyes.db.people.items(): s.present[n] = v.get('last', 0)
        hub.ui({'type': 'known', 'known': s.known()})
        sm = {}; miss = 0; seen = 0; eye_on = False; eye_since = 0
        while True:
            s.got.wait(1); s.got.clear()
            if s.seq == seen or not s.latest: continue
            seen = s.seq; fr = next(iter(s.latest.values()))
            fr = cv2.resize(fr, (640, int(640 * fr.shape[0] / fr.shape[1]))); H, W = fr.shape[:2]
            if s.enroll.get('forget'):
                s.eyes.db.forget(s.enroll.pop('forget')); s.enroll = {}; hub.ui({'type': 'known', 'known': s.known()})
            en = None
            if s.enroll.get('name'):
                tid = s.enroll.get('track')
                if tid == 'target': tid = s.target_id
                en = {'track': tid, 'name': s.enroll['name']}
            tracks, added = s.eyes.step(fr, en)
            if en and added:
                s.enroll['n'] = s.enroll.get('n', 0) + added
                if s.enroll['n'] >= 8:
                    name = s.enroll['name']; s.enroll = {}; s.present[name] = time.time()
                    hub.chat('note', f"(learned {name}'s face)"); hub.ui({'type': 'known', 'known': s.known()})
                    hub.emit('learned_face', name=name)
            if s.enroll.get('until') and time.time() > s.enroll['until']: s.enroll = {}
            now = time.time(); c = cam(); people = []
            for t in tracks:
                x, y, w, h = [float(v) for v in t.box]; px, py, pz, a = locate(c, (x + w / 2) / W, (y + h / 2) / H, w / W, W, H)
                k = sm.get(t.id)
                if k: px, py, pz = k[0] + 0.4 * (px - k[0]), k[1] + 0.4 * (py - k[1]), k[2] + 0.4 * (pz - k[2])
                sm[t.id] = (px, py, pz)
                off, nod = gaze(c, t.row, px, py)
                looking = off is not None and abs(off) < 22 and nod is not None and -0.35 < nod < 0.35
                people.append({'id': t.id, 'name': t.name, 'x': px, 'y': py, 'z': pz, 'd': math.hypot(px, py),
                               'box': [x / W, y / H, w / W, h / H], 'looking': looking, 'gaze': None if off is None else round(off, 1),
                               'new': now - t.born < 1})
            sm = {k: v for k, v in sm.items() if any(p['id'] == k for p in people)}
            # who to watch
            f = config.get('follow'); tgt = None
            if f['mode'] == 'closest' and people: tgt = min(people, key=lambda p: p['d'])
            elif f['mode'] == 'person':
                w_ = f['who']; tgt = next((p for p in people if (p['name'] == w_ if isinstance(w_, str) else p['id'] == w_)), None)
            for p in people: p['target'] = p is tgt
            s.target_id = tgt['id'] if tgt else None
            if tgt:
                miss = 0; el = math.degrees(math.atan2(tgt['z'], max(10, tgt['d'])))
                n = max(-1.0, min(0.3, -0.8 + el / 50))
                if tgt['looking'] and not eye_on and now - eye_since > 4: eye_on = True; eye_since = now
                elif not tgt['looking'] and eye_on: eye_on = False; eye_since = now
                for b in hub.bodies(): b.look(to_robot(tgt['x'], tgt['y'], c), n, eye=tgt['looking'])
            else:
                miss += 1
                if miss == 8:
                    eye_on = False
                    for b in hub.bodies(): b.look(seen=False)
            s._arrivals(people, now)
            if people: s.last_seen = now
            hub.people = people
            if now - getattr(s, '_sent', 0) > (0.1 if s.viewers else 0.5) or len(people) != getattr(s, '_n', 0):
                s._sent = now; s._n = len(people); hub.ui({'type': 'people', 'people': people})
            if s.viewers:
                ok, jp = cv2.imencode('.jpg', fr, [cv2.IMWRITE_JPEG_QUALITY, 72])
                if ok:
                    with s.newframe:
                        s.frame_seq += 1; s.jpg = jp.tobytes(); s.faces_json = json.dumps(people); s.newframe.notify_all()

    def _arrivals(s, people, now):
        """Raise 'arrived' once per arrival, after waiting a moment so it knows who it is."""
        if not config.get('greet') or hub.asleep: s.arriving = None; return
        if people and s.arriving is None and now - s.last_seen > AWAY:
            s.arriving = (now, now - s.last_seen if s.last_seen else None)
        if s.arriving and now - s.arriving[0] > 2.5:
            since, gap = s.arriving; s.arriving = None
            if people:
                names = [p['name'] for p in people if p['name']]
                for n in names: s.present[n] = now
                hub.emit('arrived', name=names[0] if names else None, gone=gap, track=s.target_id)
            return
        for p in people:                         # someone it knows came back while others were here
            n = p['name']
            if not n: continue
            if s.arriving is None and now - s.present.get(n, 0) > AWAY and now - s.last_seen < 5:
                hub.emit('arrived', name=n, gone=now - s.present[n] if s.present.get(n) else None, track=p['id'])
            s.present[n] = now
