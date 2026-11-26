"""BITCH's body language: short, readable gestures built the way animators block a shot.

Joints, in desk.py units (see sim/params.py):
  base  +turns left   (90 deg per unit)
  arm   +leans forward and down (45 deg per unit)
  neck  +pitches the head up    (45 deg per unit)
  head  +rolls / tilts           (45 deg per unit)

Leaning the arm while moving the neck by the same amount keeps the eyes level ("gaze-stabilized"), so
`pose(lean=...)` moves the whole body closer without breaking eye contact, and `look=` is the head pitch on top.

The rules every gesture follows (classic animation principles, applied to four servos):
  1. One idea at a time. Each gesture has a single lead action (a tilt, a lean, a turn) and the other
     joints only support it. Nothing waggles back and forth unless the meaning is a shake or a laugh.
  2. Eyes lead, body follows. The head starts first, then the neck, the arm, and the heavy base last
     (`lag`), so motion flows through the body instead of every joint starting on the same frame.
  3. Hold the pose. A key pose is held long enough to read before the next move.
  4. Conversation poses are held states (listen, attend, speak-*); the small word-timed nods from the Mac
     ride on top. Idle gestures go out and come back home.
  5. Small variation (about 10%) keeps it alive without changing what a gesture means.

Runs at 50 Hz on the Pi 4. Ranges are checked against the MuJoCo model in sim/ (no collisions, low torque).
"""
import math

REST = (0.0, -0.8, -0.8, 0.0)
BASE_LIMIT = 0.55
NECK_MIN, NECK_MAX = -0.88, -0.43

# MuJoCo-verified collision-free and torque-safe envelope.
LIVE_LO = (-0.86, -1.24, -0.98, -0.84)
LIVE_HI = (0.86, 0.42, 0.58, 0.84)
SLEEP_ARM_MAX = 0.86   # deep slump for dozing and loneliness (still >75 mm above the box lid)

# Who moves first. Seconds of delay per joint (base, arm, neck, head).
HEAD_FIRST = (0.16, 0.09, 0.04, 0.0)    # look with the eyes, then the body follows
BODY_FIRST = (0.0, 0.0, 0.10, 0.16)     # big body moves (stretch, yawn): the head drags behind
TOGETHER = (0.0, 0.0, 0.0, 0.0)         # startle: everything at once


def clamp(value, lo, hi):
    return lo if value < lo else hi if value > hi else value


def smoothstep(t):
    t = clamp(t, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def ease_out_cubic(t):
    t = clamp(t, 0.0, 1.0)
    return 1.0 - (1.0 - t) ** 3


def ease_out_back(t, overshoot=1.45):
    t = clamp(t, 0.0, 1.0) - 1.0
    return 1.0 + (overshoot + 1.0) * t * t * t + overshoot * t * t


def ease_in_out_cubic(t):
    t = clamp(t, 0.0, 1.0)
    return 4.0 * t * t * t if t < 0.5 else 1.0 - ((-2.0 * t + 2.0) ** 3) / 2.0


EASES = {
    'smooth': smoothstep,                          # slow in, slow out: the default
    'out': ease_out_cubic,                         # quick start, soft landing: reactions
    'pop': lambda u: ease_out_back(u, 0.9),        # a small overshoot that settles: happy accents
    'snap': lambda u: ease_out_back(u, 1.3),       # bigger overshoot, used sparingly
    'heavy': ease_in_out_cubic,                    # slow and weighted: sad, sleepy
}


class OrganicNoise:
    """Slow incommensurate oscillators that never repeat, for breathing and tiny drift."""

    def __init__(self, rng):
        self.phases = [rng.uniform(0.0, math.tau) for _ in range(12)]
        self.freqs = (
            0.23, 0.61, 1.37,   # base drift
            0.31, 0.79, 1.73,   # arm weight shift
            0.29, 0.83, 1.91,   # neck
            0.37, 1.07, 2.29,   # head
        )

    def sample(self, t):
        s = math.sin
        p, f = self.phases, self.freqs
        return (
            0.55 * s(t * f[0] + p[0]) + 0.30 * s(t * f[1] + p[1]) + 0.15 * s(t * f[2] + p[2]),
            # Arm noise stays low-frequency so the heavy arm never jitters.
            0.80 * s(t * f[3] + p[3]) + 0.20 * s(t * f[4] + p[4]),
            0.55 * s(t * f[6] + p[6]) + 0.30 * s(t * f[7] + p[7]) + 0.15 * s(t * f[8] + p[8]),
            0.60 * s(t * f[9] + p[9]) + 0.30 * s(t * f[10] + p[10]) + 0.10 * s(t * f[11] + p[11]),
        )


class Phrase:
    """One gesture: keyframes (t, d_base, d_arm, d_neck, d_head, stiffness, ease) relative to the gaze anchor.

    Each joint plays the same keyframes delayed by its `lag`, which gives overlapping action for free.
    `commit` is when the gesture has made its point; until then a calmer request won't cut it off.
    """

    def __init__(self, name, keyframes, *, side=1, sfx=None, lag=HEAD_FIRST, commit=None):
        self.name = name
        self.keyframes = keyframes
        self.side = side
        self.sfx = sfx or []   # (t, sound_name, volume)
        self.lag = lag
        self.duration = keyframes[-1][0] + max(lag)
        self.commit = min(self.duration, commit if commit is not None else 1.6)
        self.played_sfx = set()

    def _joint(self, j, age):
        frames = self.keyframes
        if age <= frames[0][0]:
            return frames[0][1 + j]
        if age >= frames[-1][0]:
            return frames[-1][1 + j]
        for idx in range(len(frames) - 1):
            f0, f1 = frames[idx], frames[idx + 1]
            if f0[0] <= age <= f1[0]:
                u = (age - f0[0]) / max(1e-4, f1[0] - f0[0])
                w = EASES.get(f1[6], smoothstep)(u)
                return f0[1 + j] + (f1[1 + j] - f0[1 + j]) * w
        return frames[-1][1 + j]

    def _stiff(self, age):
        frames = self.keyframes
        if age <= frames[0][0]:
            return frames[0][5]
        for idx in range(len(frames) - 1):
            f0, f1 = frames[idx], frames[idx + 1]
            if f0[0] <= age <= f1[0]:
                u = (age - f0[0]) / max(1e-4, f1[0] - f0[0])
                return f0[5] + (f1[5] - f0[5]) * u
        return frames[-1][5]

    def evaluate(self, age):
        b, a, n, h = (self._joint(j, age - self.lag[j]) for j in range(4))
        return (b * self.side, a, n, h * self.side), self._stiff(age)

    def due_sfx(self, age):
        due = []
        for idx, (at, sound, vol) in enumerate(self.sfx):
            if idx not in self.played_sfx and age >= at:
                self.played_sfx.add(idx)
                due.append((sound, vol))
        return due


def pose(turn=0.0, lean=0.0, look=0.0, tilt=0.0):
    """Body-language pose -> joint offsets. `lean` moves closer with the eyes kept level; `look` pitches up."""
    return (turn, lean, lean + look, tilt)


HOME = pose()

# Each gesture: (lag, stiffness, commit, [(t, pose, ease), ...]). Times are at normal tempo.
# Poses that end away from HOME are held states: they stay until the mode changes.
GESTURES = {
    # ------------------------------------------------------------------ attention
    'curious': (HEAD_FIRST, 1.1, 1.3, [
        (0.00, HOME, 'smooth'),
        (0.22, pose(lean=-0.04, look=0.03, tilt=-0.05), 'smooth'),        # tiny anticipation
        (0.70, pose(lean=0.26, look=0.02, tilt=0.38), 'out'),              # lean in, head cocked
        (1.70, pose(lean=0.28, tilt=0.40), 'smooth'),                       # hold: "huh?"
        (2.40, HOME, 'smooth'),
    ]),
    'glance': (HEAD_FIRST, 1.1, 1.1, [
        (0.00, HOME, 'smooth'),
        (0.35, pose(turn=0.30, look=0.06, tilt=0.12), 'out'),              # eyes go, body follows
        (1.15, pose(turn=0.30, look=0.06, tilt=0.10), 'smooth'),
        (1.75, HOME, 'smooth'),
    ]),
    'shy': (HEAD_FIRST, 0.9, 1.4, [
        (0.00, HOME, 'smooth'),
        (0.50, pose(turn=0.20, look=-0.16, tilt=0.30), 'smooth'),          # turn away, look down
        (1.40, pose(turn=0.20, look=-0.16, tilt=0.30), 'smooth'),
        (1.90, pose(turn=0.08, look=-0.06, tilt=0.12), 'smooth'),          # peek back
        (2.40, pose(turn=0.08, look=-0.06, tilt=0.12), 'smooth'),
        (3.00, HOME, 'smooth'),
    ]),
    'peek': (HEAD_FIRST, 1.1, 1.8, [
        (0.00, HOME, 'smooth'),
        (0.45, pose(turn=0.34, lean=-0.04, look=-0.10, tilt=0.18), 'smooth'),   # look away and down
        (1.25, pose(turn=0.34, lean=-0.04, look=-0.10, tilt=0.18), 'smooth'),
        (1.65, pose(lean=0.22, look=0.06, tilt=-0.26), 'pop'),                   # pop back: "boo"
        (2.40, pose(lean=0.20, look=0.04, tilt=-0.22), 'smooth'),
        (3.00, HOME, 'smooth'),
    ]),
    'survey': (HEAD_FIRST, 1.0, 2.2, [
        (0.00, HOME, 'smooth'),
        (0.65, pose(turn=0.50, lean=0.06, look=0.10, tilt=0.14), 'smooth'),
        (1.45, pose(turn=0.50, lean=0.06, look=0.10, tilt=0.10), 'smooth'),
        (2.35, pose(turn=-0.45, lean=0.06, look=0.10, tilt=-0.14), 'smooth'),
        (3.15, pose(turn=-0.45, lean=0.06, look=0.10, tilt=-0.10), 'smooth'),
        (3.85, HOME, 'smooth'),
    ]),
    'seek': (HEAD_FIRST, 1.1, 1.6, [
        (0.00, HOME, 'smooth'),
        (0.40, pose(turn=0.30, lean=0.20, look=0.08, tilt=0.18), 'out'),
        (1.10, pose(turn=0.30, lean=0.20, look=0.08, tilt=0.16), 'smooth'),
        (1.75, pose(turn=-0.22, lean=0.20, look=0.08, tilt=-0.16), 'smooth'),
        (2.35, pose(turn=-0.22, lean=0.18, look=0.06, tilt=-0.12), 'smooth'),
        (2.95, HOME, 'smooth'),
    ]),
    'notice': (HEAD_FIRST, 1.4, 0.6, [                                      # held: perked up
        (0.00, HOME, 'smooth'),
        (0.12, pose(lean=-0.05, look=-0.02), 'smooth'),
        (0.40, pose(lean=0.06, look=0.15, tilt=0.18), 'out'),
        (1.20, pose(lean=0.06, look=0.14, tilt=0.16), 'smooth'),
    ]),
    'attend': (HEAD_FIRST, 1.1, 0.5, [                                      # held: "go on"
        (0.00, HOME, 'smooth'),
        (0.45, pose(lean=0.18, look=0.03, tilt=0.16), 'smooth'),
        (1.00, pose(lean=0.18, look=0.03, tilt=0.16), 'smooth'),
    ]),
    'listen': (HEAD_FIRST, 1.0, 0.4, [                                      # held: leaning in
        (0.00, HOME, 'smooth'),
        (0.60, pose(lean=0.22, look=0.02, tilt=0.26), 'smooth'),
        (1.20, pose(lean=0.22, look=0.02, tilt=0.26), 'smooth'),
    ]),
    'ponder': (HEAD_FIRST, 0.95, 0.8, [                                     # held: looking up and away
        (0.00, HOME, 'smooth'),
        (0.55, pose(turn=-0.22, lean=-0.06, look=0.26, tilt=-0.28), 'smooth'),
        (1.60, pose(turn=-0.22, lean=-0.06, look=0.28, tilt=-0.30), 'smooth'),
    ]),
    'look-target': (HEAD_FIRST, 1.2, 0.6, [                                 # held: leaning toward it
        (0.00, HOME, 'smooth'),
        (0.35, pose(lean=0.22, look=0.04, tilt=0.20), 'out'),
        (1.00, pose(lean=0.24, look=0.02, tilt=0.12), 'smooth'),
    ]),
    'mirror': (HEAD_FIRST, 1.1, 1.3, [
        (0.00, HOME, 'smooth'),
        (0.40, pose(lean=0.08, look=0.02, tilt=0.42), 'smooth'),
        (1.50, pose(lean=0.08, look=0.02, tilt=0.44), 'smooth'),
        (2.10, HOME, 'smooth'),
    ]),
    'double-take': (HEAD_FIRST, 1.3, 1.2, [                                 # held: locked on
        (0.00, HOME, 'smooth'),
        (0.25, pose(look=0.02, tilt=0.10), 'smooth'),                        # half-notices...
        (0.45, pose(lean=-0.08, look=0.10, tilt=-0.06), 'out'),              # ...wait, what?
        (0.75, pose(lean=0.24, look=0.12, tilt=0.24), 'pop'),                # leans in
        (1.60, pose(lean=0.22, look=0.10, tilt=0.20), 'smooth'),
        (2.10, pose(lean=0.12, look=0.04, tilt=0.10), 'smooth'),
    ]),

    # ------------------------------------------------------------------ feelings
    'happy': ((0.08, 0.05, 0.02, 0.0), 1.3, 1.2, [
        (0.00, HOME, 'smooth'),
        (0.18, pose(lean=-0.06, look=-0.04), 'smooth'),                      # little crouch
        (0.45, pose(lean=0.14, look=0.18, tilt=0.30), 'pop'),                # perk up
        (0.85, pose(lean=0.14, look=0.16, tilt=-0.24), 'smooth'),            # one happy wiggle
        (1.25, pose(lean=0.12, look=0.12, tilt=0.16), 'smooth'),
        (1.90, pose(lean=0.06, look=0.06, tilt=0.06), 'smooth'),
    ]),
    'victory': ((0.06, 0.04, 0.02, 0.0), 1.5, 2.4, [                        # a little dance on the beat
        (0.00, HOME, 'smooth'),
        (0.25, pose(lean=-0.10, look=-0.06), 'smooth'),
        (0.55, pose(turn=0.32, lean=0.10, look=0.26, tilt=0.45), 'pop'),
        (1.00, pose(turn=-0.32, lean=0.10, look=0.26, tilt=-0.45), 'smooth'),
        (1.45, pose(turn=0.28, lean=0.10, look=0.24, tilt=0.40), 'smooth'),
        (1.90, pose(turn=-0.20, lean=0.08, look=0.20, tilt=-0.28), 'smooth'),
        (2.45, pose(lean=0.04, look=0.08), 'smooth'),
        (2.90, HOME, 'smooth'),
    ]),
    'alarm': ((0.04, 0.02, 0.0, 0.0), 1.7, 1.5, [                           # loops while ringing
        (0.00, HOME, 'smooth'),
        (0.22, pose(turn=0.30, lean=0.12, look=0.20, tilt=0.36), 'out'),
        (0.52, pose(turn=-0.30, lean=0.12, look=0.20, tilt=-0.36), 'out'),
        (0.82, pose(turn=0.30, lean=0.12, look=0.20, tilt=0.36), 'out'),
        (1.12, pose(turn=-0.30, lean=0.12, look=0.20, tilt=-0.36), 'out'),
        (1.45, pose(lean=0.08, look=0.12), 'smooth'),
    ]),
    'sad': (HEAD_FIRST, 0.6, 1.6, [                                         # held: drooping
        (0.00, HOME, 'smooth'),
        (1.00, pose(turn=-0.06, lean=0.30, look=-0.22, tilt=0.22), 'heavy'),
        (2.60, pose(turn=-0.08, lean=0.36, look=-0.26, tilt=0.26), 'heavy'),
    ]),
    'grumpy': (HEAD_FIRST, 1.2, 1.6, [
        (0.00, HOME, 'smooth'),
        (0.30, pose(turn=0.36, lean=-0.06, look=0.14, tilt=-0.22), 'out'),   # turns away, nose up
        (1.50, pose(turn=0.36, lean=-0.06, look=0.14, tilt=-0.20), 'smooth'),
        (2.00, pose(turn=0.18, look=0.08, tilt=-0.10), 'smooth'),            # side-eye back
        (2.60, pose(turn=0.18, look=0.08, tilt=-0.10), 'smooth'),
        (3.10, HOME, 'smooth'),
    ]),
    'scared': (TOGETHER, 1.6, 1.0, [
        (0.00, HOME, 'smooth'),
        (0.15, pose(turn=-0.04, lean=-0.30, look=0.06, tilt=-0.16), 'out'),  # recoil
        (0.95, pose(turn=-0.04, lean=-0.30, look=0.06, tilt=-0.16), 'smooth'),   # freeze
        (1.70, pose(lean=-0.08, look=0.02, tilt=-0.04), 'smooth'),
        (2.30, HOME, 'smooth'),
    ]),
    'proud': (BODY_FIRST, 1.1, 1.6, [
        (0.00, HOME, 'smooth'),
        (0.50, pose(lean=-0.14, look=0.20), 'smooth'),                       # stand tall, chin up
        (1.70, pose(lean=-0.14, look=0.22, tilt=0.04), 'smooth'),
        (2.30, HOME, 'smooth'),
    ]),
    'stretch': (BODY_FIRST, 0.8, 2.0, [
        (0.00, HOME, 'smooth'),
        (0.90, pose(lean=-0.24, look=0.42), 'heavy'),                        # reach up
        (1.90, pose(lean=-0.24, look=0.44, tilt=0.04), 'smooth'),
        (2.60, pose(lean=0.06, look=-0.04), 'heavy'),
        (3.10, HOME, 'smooth'),
    ]),
    'yawn': (BODY_FIRST, 0.6, 2.0, [                                        # ends drooped; sleep follows
        (0.00, HOME, 'smooth'),
        (0.70, pose(lean=-0.10, look=0.42, tilt=0.10), 'heavy'),
        (1.50, pose(lean=-0.10, look=0.44, tilt=0.12), 'smooth'),
        (2.50, pose(lean=0.30, look=-0.46, tilt=0.06), 'heavy'),
    ]),

    # ------------------------------------------------------------------ speaking: held postures
    # The Mac adds word-timed nods, tilts and leans on top, so these stay still once they arrive.
    'speaking': (HEAD_FIRST, 1.0, 0.4, [
        (0.00, HOME, 'smooth'), (0.45, pose(lean=0.14, look=0.02, tilt=0.10), 'smooth'),
    ]),
    'speak-warm': (HEAD_FIRST, 1.0, 0.4, [
        (0.00, HOME, 'smooth'), (0.50, pose(lean=0.18, look=0.04, tilt=0.20), 'smooth'),
    ]),
    'speak-gentle': (HEAD_FIRST, 0.75, 0.6, [
        (0.00, HOME, 'smooth'), (0.80, pose(lean=0.22, look=-0.06, tilt=0.28), 'heavy'),
    ]),
    'speak-upbeat': (HEAD_FIRST, 1.2, 0.4, [
        (0.00, HOME, 'smooth'), (0.35, pose(lean=0.10, look=0.12, tilt=-0.10), 'out'),
    ]),
    'speak-emphatic': (HEAD_FIRST, 1.2, 0.4, [
        (0.00, HOME, 'smooth'), (0.35, pose(lean=0.24, look=-0.04), 'out'),
    ]),
    'speak-playful': (HEAD_FIRST, 1.1, 0.4, [
        (0.00, HOME, 'smooth'), (0.45, pose(turn=0.06, lean=0.10, look=0.06, tilt=0.30), 'smooth'),
    ]),
    'speak-annoyed': (HEAD_FIRST, 1.2, 0.4, [                               # pulls back, half turned away
        (0.00, HOME, 'smooth'), (0.35, pose(turn=0.12, lean=-0.08, look=0.10, tilt=-0.16), 'out'),
    ]),
    'speak-amused': (HEAD_FIRST, 1.2, 0.8, [                                # one laugh bob, then held
        (0.00, HOME, 'smooth'),
        (0.20, pose(lean=-0.06, look=0.10, tilt=0.10), 'out'),
        (0.45, pose(lean=0.06, look=0.04, tilt=0.22), 'smooth'),
        (0.70, pose(lean=-0.02, look=0.08, tilt=0.18), 'smooth'),
        (1.00, pose(lean=0.06, look=0.04, tilt=0.20), 'smooth'),
    ]),
    'speak-surprised': (TOGETHER, 1.3, 0.6, [
        (0.00, HOME, 'smooth'),
        (0.15, pose(lean=-0.14, look=0.14, tilt=-0.10), 'out'),
        (0.90, pose(lean=-0.10, look=0.12, tilt=-0.08), 'smooth'),
        (1.50, pose(lean=0.06, look=0.04), 'smooth'),
    ]),
    'speak-farewell': (HEAD_FIRST, 1.0, 1.0, [
        (0.00, HOME, 'smooth'),
        (0.40, pose(lean=0.16, look=0.04, tilt=0.22), 'smooth'),
        (1.00, pose(lean=0.16, look=-0.06, tilt=0.22), 'smooth'),           # a small nod goodbye
        (1.40, pose(lean=0.12, look=0.04, tilt=0.18), 'smooth'),
        (2.20, HOME, 'smooth'),
    ]),
}
GESTURES['recognize'] = GESTURES['happy']
# Feelings and idle gestures play bigger; conversation states stay calm so the face stays readable.
EXPRESSIVE = 1.25
CALM_STATES = {'listen', 'attend', 'notice', 'look-target', 'victory', 'alarm'} | {
    name for name in GESTURES if name == 'speaking' or name.startswith('speak-')}

SFX = {
    'curious': lambda rng, t: [(t(0.5), 'curious', 0.36)] if rng.random() < 0.5 else [],
    'peek': lambda rng, t: [(t(1.6), 'blip', 0.32)] if rng.random() < 0.5 else [],
    'double-take': lambda rng, t: [(t(0.5), 'blip', 0.36)] if rng.random() < 0.6 else [],
    'happy': lambda rng, t: [(t(0.3), rng.choice(('happy', 'walle', 'doubleblip')), 0.40)],
    'recognize': lambda rng, t: [(t(0.3), rng.choice(('happy', 'walle', 'doubleblip')), 0.40)],
    'victory': lambda rng, t: [(t(0.3), 'tada', 0.50), (t(1.4), 'giggle', 0.38)],
    'alarm': lambda rng, t: [(0.05, 'excited', 0.66)],
    'sad': lambda rng, t: [(t(0.8), rng.choice(('sigh', 'whimper', 'sad')), 0.36)],
    'lonely': lambda rng, t: [(t(0.8), rng.choice(('sigh', 'whimper', 'sad')), 0.36)],
    'grumpy': lambda rng, t: [(t(0.25), rng.choice(('grumble', 'angry')), 0.40)],
    'scared': lambda rng, t: [(0.02, 'startle', 0.46)],
    'proud': lambda rng, t: [(t(0.4), 'proud', 0.38)],
    'yawn': lambda rng, t: [(0.18, 'yawn', 0.42)],
    'doze': lambda rng, t: [(0.35, 'yawn', 0.36)],
}


def synthesize_phrase(name, rng, side=1, mood=None):
    """Build one gesture with a little natural variation, shaped by mood. Returns None if unknown."""
    mood = mood or {}
    valence = float(mood.get('valence', 0.1))
    arousal = float(mood.get('arousal', 0.4))
    loneliness = float(mood.get('loneliness', 0.0))
    # Excited is a touch quicker and bigger, lonely a touch slower and smaller. Never enough to change meaning.
    tempo = clamp(1.0 - 0.14 * (arousal - 0.4) + 0.16 * loneliness, 0.84, 1.2) * rng.uniform(0.95, 1.05)
    amp = clamp(1.0 + 0.10 * (arousal - 0.4) - 0.08 * loneliness, 0.86, 1.1) * rng.uniform(0.92, 1.08)

    def t(seconds):
        return seconds * tempo

    if name == 'doze':
        # Nodding off: the head sinks, catches itself once, then the whole body melts down. Joint offsets, not
        # gaze-stabilized, because the point is that the eyes drop.
        frames = (
            (0.0, 0.0, 0.0, 0.0, 0.0, 0.55, 'smooth'),
            (1.10, 0.04 * amp, 0.50 * amp, -0.10, 0.16 * amp, 0.45, 'heavy'),
            (2.30, 0.08 * amp, 1.05 * amp, -0.18, 0.30 * amp, 0.40, 'heavy'),
            (2.70, 0.03 * amp, 0.42 * amp, 0.20, -0.10 * amp, 1.0, 'out'),
            (4.00, 0.10 * amp, 1.30 * amp, -0.18, 0.34 * amp, 0.40, 'heavy'),
            (5.20, 0.12 * amp, 1.50, -0.20, 0.32 * amp, 0.36, 'heavy'),
        )
        return Phrase(name, frames, side=side, sfx=SFX['doze'](rng, t), lag=TOGETHER, commit=5.0)

    if name == 'lonely':
        # One long, heavy slump toward the box lid. The biggest pose BITCH has, and still one idea.
        frames = (
            (0.0, 0.0, 0.0, 0.0, 0.0, 0.6, 'smooth'),
            (t(1.2), -0.08 * amp, 0.70 * amp, -0.10, 0.28 * amp, 0.5, 'heavy'),
            (t(2.6), -0.12 * amp, 0.90 * amp, -0.14, 0.34 * amp, 0.45, 'heavy'),
            (t(3.6), -0.10 * amp, 0.86 * amp, -0.12, 0.30 * amp, 0.45, 'smooth'),
        )
        return Phrase(name, frames, side=side, sfx=SFX['lonely'](rng, t), lag=HEAD_FIRST, commit=2.6)

    spec = GESTURES.get(name)
    if spec is None:
        return None
    lag, stiff, commit, beats = spec
    if name not in CALM_STATES:
        amp *= EXPRESSIVE
    frames = []
    for at, (b, a, n, h), ease in beats:
        frames.append((t(at), b * amp, a * amp, n * amp, h * amp, stiff, ease))
    if name in ('glance', 'survey', 'peek'):
        # Where it looks varies a little each time; how it moves does not.
        reach = rng.uniform(0.8, 1.15)
        frames = [(f[0], f[1] * reach, *f[2:]) for f in frames]
    sfx = SFX[name](rng, t) if name in SFX else []
    return Phrase(name, tuple(frames), side=side, sfx=sfx, lag=lag, commit=t(commit))


def mood_baseline(mood, t, noise_vec):
    """The posture a mood gives the whole body, plus slow breathing. Returns (d_base, d_arm, d_neck, d_head, stiff)."""
    valence = clamp(float(mood.get('valence', 0.1)), -1.0, 1.0)
    arousal = clamp(float(mood.get('arousal', 0.35)), 0.0, 1.0)
    loneliness = clamp(float(mood.get('loneliness', 0.0)), 0.0, 1.0)
    annoyance = clamp(float(mood.get('annoyance', 0.0)), 0.0, 1.0)

    # The arm stays behind top-dead-center (-0.80 + at most 0.20) so gravity always loads its gears one way
    # and it can't rock in the gear backlash while standing still.
    arm_posture = clamp(0.12 * max(0.0, valence) + 0.20 * loneliness + 0.10 * max(0.0, -valence) + 0.05 * annoyance,
                        -0.10, 0.20)
    neck_posture = 0.16 * max(0.0, valence) - 0.14 * loneliness - 0.08 * max(0.0, -valence) + 0.03 * annoyance
    head_posture = 0.18 * loneliness - 0.12 * annoyance + 0.05 * max(0.0, valence)

    # Breathing: a slow arm heave, with the light neck and head carrying a little life.
    breath = math.sin(math.tau * (0.20 + 0.08 * arousal - 0.05 * loneliness) * t)
    d_arm = arm_posture + breath * 0.012 + 0.008 * noise_vec[1]
    d_neck = neck_posture + breath * 0.020 + 0.020 * noise_vec[2]
    d_base = 0.018 * noise_vec[0]
    d_head = head_posture + 0.040 * noise_vec[3]

    stiff = clamp(1.0 + 0.22 * arousal + 0.10 * annoyance - 0.25 * loneliness - 0.12 * max(0.0, -valence), 0.6, 1.35)
    return d_base, d_arm, d_neck, d_head, stiff


def idle_choice_for_mood(mode, mood, recent, rng):
    """Pick the next idle gesture, weighted by mood, never repeating the last few."""
    loneliness = float(mood.get('loneliness', 0.0))
    valence = float(mood.get('valence', 0.1))
    annoyance = float(mood.get('annoyance', 0.0))

    if mode == 'scan':
        pool = ['survey', 'survey', 'glance', 'stretch', 'curious']
        if loneliness > 0.55 or valence < -0.3:
            pool += ['lonely']
    else:
        pool = ['glance', 'glance', 'curious', 'curious', 'shy', 'peek', 'stretch']
        if loneliness > 0.55:
            pool += ['lonely', 'sad']
        elif valence > 0.45:
            pool += ['happy', 'peek']
        if annoyance > 0.5:
            pool += ['grumpy']

    fresh = [item for item in pool if item not in recent] or pool
    return rng.choice(fresh)
