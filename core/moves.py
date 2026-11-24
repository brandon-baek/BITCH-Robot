"""BITCH's body language, and how it invents moves it has never done.

A move is a list of simple steps that any body can play (the robot, or the little BITCH drawn in the app):

  {"do": "pose",   "base": 0.5, "arm": -0.6, "neck": -0.4, "head": 0.2, "secs": 0.6, "speed": "snappy"}
  {"do": "wiggle", "joints": ["head"], "size": 0.3, "hz": 3, "secs": 1.2}
  {"do": "sound",  "sound": "excited"}
  {"do": "wait",   "secs": 0.5}
  {"do": "rest",   "secs": 0.8}

Joints (same numbers as desk.py's Actor):
  base  turn          -0.9 (its right) .. 0.9 (its left), 0 faces front
  arm   lean          -0.95 (tall, leaning back) .. -0.15 (leaning far forward)
  neck  look up/down  -1.0 (looking down) .. -0.1 (looking up)
  head  tilt          -0.85 .. 0.85, 0 straight
When you ask for something new ("do the chicken dance"), the deep brain writes the steps, they are checked and
clamped to what the servos can safely do, played, and saved as a skill so next time it's instant.
"""
import difflib, json, re, threading
from . import config, memory

JOINTS = {'base': (-0.9, 0.9), 'arm': (-0.95, -0.15), 'neck': (-1.0, -0.1), 'head': (-0.85, 0.85)}
REST = {'base': 0.0, 'arm': -0.8, 'neck': -0.8, 'head': 0.0}
SPEEDS = {'slow': 0.6, 'normal': 1.2, 'snappy': 2.6}
SOUNDS = ['hello', 'happy', 'excited', 'giggle', 'curious', 'question', 'sad', 'whimper', 'sigh', 'startle', 'scream', 'boo', 'angry',
          'grumble', 'love', 'proud', 'tada', 'yawn', 'snore', 'think', 'scan', 'powerup', 'powerdown', 'beepboop', 'blip', 'doubleblip', 'walle']
SHOWS = {'dance': ('dance', 'disco', 'party', 'boogie', 'groove', 'dance party'), 'sing': ('sing', 'opera', 'song', 'aria', 'serenade'),
         'sleep': ('sleep', 'go to sleep', 'bed', 'nap', 'bedtime'), 'wake': ('wake', 'wake up')}


def P(secs=0.5, speed='normal', **j): return dict(do='pose', secs=secs, speed=speed, **j)
def W(joints, size, hz, secs): return dict(do='wiggle', joints=joints, size=size, hz=hz, secs=secs)


# Small gestures that go with what BITCH says (the robot's brain.py has the same ones built in).
GESTURES = {
    'nod': [P(0.22, 'snappy', neck=-0.45), P(0.22, 'snappy', neck=-0.8), P(0.22, 'snappy', neck=-0.45), P(0.22, 'snappy', neck=-0.8)],
    'shake': [P(0.16, 'snappy', head=0.35), P(0.16, 'snappy', head=-0.35), P(0.16, 'snappy', head=0.35), P(0.16, 'snappy', head=-0.35), P(0.2, head=0)],
    'wiggle': [W(['head', 'base'], 0.3, 3, 1.4)],
    'look_around': [P(1.0, 'normal', base=0.6), P(1.3, base=-0.6), P(0.8, base=0)],
    'lean_in': [P(1.5, 'slow', arm=-0.3, neck=-0.45)],
    'lean_back': [P(1.2, 'normal', arm=-0.95, neck=-0.95)],
    'spin': [{'do': 'sound', 'sound': 'excited'}, P(0.9, 'normal', base=0.85), P(1.2, base=-0.85), P(0.8, base=0)],
    'bow': [P(1.3, 'slow', arm=-0.3, neck=-0.15, head=0), P(1.0, arm=-0.8, neck=-0.8)],
    'wave': [W(['head'], 0.4, 2.2, 1.6)],
}
# How BITCH holds itself for each feeling.
FEELINGS = {
    'happy': dict(arm=-0.6, neck=-0.5, head=0.15), 'excited': dict(arm=-0.5, neck=-0.45, head=0), 'sad': dict(arm=-0.9, neck=-1.0, head=0.3),
    'angry': dict(arm=-0.5, neck=-0.95, head=0), 'scared': dict(arm=-0.95, neck=-1.0, head=-0.25), 'love': dict(arm=-0.6, neck=-0.6, head=0.35),
    'curious': dict(arm=-0.55, neck=-0.55, head=-0.35), 'sleepy': dict(arm=-0.85, neck=-0.95, head=0.3), 'proud': dict(arm=-0.95, neck=-0.45, head=0),
    'confused': dict(arm=-0.75, neck=-0.7, head=0.45), 'neutral': dict(arm=-0.8, neck=-0.8, head=0)}


def library():
    """Everything the app needs to animate BITCH exactly like the robot."""
    return {'gestures': GESTURES, 'feelings': FEELINGS, 'rest': REST, 'joints': JOINTS, 'speeds': SPEEDS}


def clean(steps):
    """Make any list of steps safe: known step types only, joints clamped, sizes and times limited."""
    out, total = [], 0.0
    for st in steps if isinstance(steps, list) else []:
        if not isinstance(st, dict): continue
        d = str(st.get('do', '')).lower()
        if not d:   # be forgiving about shape: {"pose": {...}} or {"wiggle": "head"}
            if isinstance(st.get('pose'), dict): st = dict(st, do='pose', **st['pose']); d = 'pose'
            elif 'wiggle' in st: st = dict(st, do='wiggle', joints=st['wiggle']); d = 'wiggle'
            elif 'sound' in st: d = 'sound'
            elif 'wait' in st: st = dict(do='wait', secs=st['wait']); d = 'wait'
        secs = _num(st.get('secs'), 0.5, 0.05, 4.0)
        if d == 'pose':
            s = {'do': 'pose', 'secs': secs, 'speed': st.get('speed') if st.get('speed') in SPEEDS else 'normal'}
            for j, (lo, hi) in JOINTS.items():
                if j in st and st[j] is not None: s[j] = round(_num(st[j], REST[j], lo, hi), 3)
            if len(s) == 3: continue
        elif d == 'wiggle':
            js = st.get('joints') or ['head']; js = [js] if isinstance(js, str) else js
            js = [j for j in js if j in JOINTS] or ['head']
            s = {'do': 'wiggle', 'joints': js[:3], 'size': round(_num(st.get('size'), 0.3, 0.03, 0.5), 3),
                 'hz': round(_num(st.get('hz'), 3, 0.3, 6), 2), 'secs': secs}
        elif d == 'sound':
            if st.get('sound') not in SOUNDS: continue
            s = {'do': 'sound', 'sound': st['sound']}; secs = 0
        elif d == 'wait': s = {'do': 'wait', 'secs': secs}
        elif d == 'rest': s = {'do': 'rest', 'secs': secs}
        else: continue
        if total + secs > 25: break
        total += secs; out.append(s)
        if len(out) >= 48: break
    return out


def _num(v, default, lo, hi):
    try: v = float(v)
    except (TypeError, ValueError): v = default
    return max(lo, min(hi, v))


def length(steps):
    return sum(s.get('secs', 0) for s in steps if s['do'] != 'sound')


def timeline(steps, fps=10):
    """Sample a move into (time, pose, sound) frames, for bodies that can only be told where to point."""
    import math
    pose = dict(REST); t = 0.0
    for st in steps:
        if st['do'] == 'sound': yield t, dict(pose), st['sound']; continue
        n = max(1, int(st['secs'] * fps)); start = dict(pose)
        target = dict(pose, **{j: st[j] for j in JOINTS if j in st}) if st['do'] == 'pose' else dict(REST) if st['do'] == 'rest' else dict(pose)
        for k in range(1, n + 1):
            f = k / n; e = f * f * (3 - 2 * f)
            cur = {j: start[j] + (target[j] - start[j]) * e for j in JOINTS}
            if st['do'] == 'wiggle':
                for j in st['joints']: cur[j] = max(JOINTS[j][0], min(JOINTS[j][1], cur[j] + st['size'] * math.sin(2 * math.pi * st['hz'] * k / fps)))
            t += 1 / fps; yield t, cur, None
        pose = target


# ---------------- finding or inventing a move ----------------
def _norm(s):
    s = re.sub(r"[^a-z0-9 ]", ' ', str(s).lower())
    s = re.sub(r'\b(the|a|an|do|please|can you|could you|some|little|your|for me|move)\b', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def find(what):
    """Look for a move by name. Returns ('show', name) / ('gesture', name, steps) / ('skill', name, steps) / None."""
    w = _norm(what)
    if not w: return None
    for show, words in SHOWS.items():
        if w in words or any(re.fullmatch(r'(a |the )?' + re.escape(x) + r'( dance| song)?', w) for x in words): return ('show', show)
    g = w.replace(' ', '_')
    if g in GESTURES: return ('gesture', g, GESTURES[g])
    names = {_norm(s['name']): s for s in memory.skills() if s['kind'] == 'move'}
    if w in names: s = names[w]; return ('skill', s['name'], s['body']['steps'])
    close = difflib.get_close_matches(w, list(names), n=1, cutoff=0.82)
    if close: s = names[close[0]]; return ('skill', s['name'], s['body']['steps'])
    return None


FRAME = {'type': 'object', 'properties': {
    'base': {'type': 'number'}, 'arm': {'type': 'number'}, 'neck': {'type': 'number'}, 'head': {'type': 'number'},
    'secs': {'type': 'number'}, 'speed': {'type': 'string', 'enum': list(SPEEDS)},
    'wiggle': {'type': 'string', 'enum': ['none'] + list(JOINTS)}, 'sound': {'type': 'string', 'enum': ['none'] + SOUNDS}},
    'required': ['base', 'arm', 'neck', 'head', 'secs', 'speed', 'wiggle', 'sound']}
SCHEMA = {'type': 'object', 'properties': {'name': {'type': 'string'}, 'frames': {'type': 'array', 'items': FRAME, 'maxItems': 24},
                                           'after': {'type': 'string'}}, 'required': ['name', 'frames', 'after']}


def from_frames(frames):
    """Keyframes (easy for a language model to write) -> steps (what bodies play)."""
    steps = []
    for f in frames if isinstance(frames, list) else []:
        if not isinstance(f, dict): continue
        if f.get('sound') not in (None, 'none'): steps.append({'do': 'sound', 'sound': f['sound']})
        wig = f.get('wiggle') if f.get('wiggle') in JOINTS else None
        pose = {'do': 'pose', 'speed': f.get('speed', 'normal'), 'secs': 0.05 if wig else f.get('secs', 0.5)}
        pose.update({j: f[j] for j in JOINTS if j in f})
        steps.append(pose)
        if wig: steps.append({'do': 'wiggle', 'joints': [wig], 'size': 0.3, 'hz': 3.5, 'secs': f.get('secs', 0.8)})
    return clean(steps)


def _f(base=0, arm=-0.8, neck=-0.8, head=0, secs=0.5, speed='normal', wiggle='none', sound='none'):
    return dict(base=base, arm=arm, neck=neck, head=head, secs=secs, speed=speed, wiggle=wiggle, sound=sound)


DESIGNER = f"""You choreograph moves for BITCH, a small cardboard desk-lamp robot with googly eyes and four servos.
A move is a list of keyframes. In each keyframe BITCH moves all four joints to the positions you give, holds for "secs",
optionally wiggles one joint during that time, and optionally makes a sound as the keyframe starts.
Joints are positions:
- base turns the whole robot: -0.9 = its far right, 0 = facing the person, 0.9 = its far left.
- arm is the lamp arm: -0.95 = tall and leaning back, -0.8 = normal, -0.3 = leaning forward toward the person, -0.15 = bending far down.
- neck tilts the head: -1.0 = looking down at the desk, -0.8 = normal, -0.4 = looking up, -0.1 = looking straight up.
- head tilts sideways like a puzzled dog: -0.85 .. 0.85, 0 = straight.
Rest: base 0, arm -0.8, neck -0.8, head 0. "speed": slow, normal or snappy. "secs": 0.15 to 2.
Sounds: {', '.join(SOUNDS)}.
Make it expressive and funny like a cartoon robot: big clear poses, rhythm, anticipation, a couple of sounds. Use the whole range of every joint.
Use 6 to 16 keyframes. The last keyframe is the rest pose. "name" is a short lowercase name. "after" is one short, cute line BITCH says when done.
Example, a wave: {json.dumps({'name': 'wave', 'frames': [_f(0, -0.6, -0.5, 0.2, 0.4, 'snappy', 'none', 'hello'), _f(0, -0.6, -0.5, 0.2, 1.6, 'normal', 'head'), _f()], 'after': 'Hi hi!'})}
Example, a happy dance: {json.dumps({'name': 'happy dance', 'frames': [_f(0.4, -0.7, -0.6, 0.3, 0.3, 'snappy', 'none', 'excited'), _f(-0.4, -0.7, -0.6, -0.3, 0.3, 'snappy'), _f(0.4, -0.7, -0.6, 0.3, 0.3, 'snappy'), _f(-0.4, -0.7, -0.6, -0.3, 0.3, 'snappy'), _f(0, -0.95, -0.4, 0, 1.2, 'normal', 'base', 'tada'), _f()], 'after': 'Nailed it!'})}"""

_making = threading.Lock()


def invent(what):
    """Write a brand-new move for `what`, check it, save it. Returns (name, steps, after) or None."""
    from . import llm
    with _making:
        hit = find(what)
        if hit and hit[0] != 'show': return hit[1], hit[2], ''
        try:
            out = llm.chat([{'role': 'system', 'content': DESIGNER}, {'role': 'user', 'content': f'Make this move: {what}'}],
                           SCHEMA, deep=True, temperature=0.8, timeout=150, max_tokens=1600)
        except Exception as e:
            print(f'  could not invent a move: {e!r}', flush=True); return None
        steps = from_frames(out.get('frames'))
        if len(steps) < 2: return None
        if steps[-1]['do'] != 'rest': steps.append({'do': 'rest', 'secs': 0.8})
        name = _norm(out.get('name') or what)[:40] or _norm(what)[:40]
        if find(name) and find(name)[0] in ('show', 'gesture'): name = _norm(what)[:40]
        after = str(out.get('after') or '')[:120]
        memory.save_skill(name, 'move', f'A move BITCH invented for "{what}"', {'steps': steps, 'after': after, 'asked': what})
        return name, steps, after
