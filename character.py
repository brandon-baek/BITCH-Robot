"""Procedural Pixar-style full-body character motion engine for BITCH's 4-joint puppet.

Calibrated directly against BITCH's measured 3D MuJoCo physics model (sim/params.py & sim/model.py):
  - +base turns left (90 deg/unit), +arm leans forward/down (45 deg/unit),
    +neck pitches head up (45 deg/unit), +head rolls/tilts (45 deg/unit).
  - Gaze-stabilized body translation: world head pitch = (neck - arm) * 45 deg.
    Matching d_neck ~= d_arm lets BITCH lunge 100+ mm closer, stand tall upright,
    or recoil far back while keeping its googly eyes locked straight on you.
  - Every gesture is synthesized fresh with Disney/Pixar principles: anticipation,
    follow-through/overshoot, kinematic chain lag (head -> neck -> arm -> base),
    timing/weight contrast (snappy pops vs heavy weighted droops), and continuous
    non-repeating organic breathing + emotional posture.

Runs natively at 50 Hz on a Raspberry Pi 4 (<0.5 ms/step) while keeping MG90S
servos collision-free, under 50% stall torque, and zero-power when calm/asleep.
"""
import math
import random

REST = (0.0, -0.8, -0.8, 0.0)
BASE_LIMIT = 0.55
NECK_MIN, NECK_MAX = -0.88, -0.43

# MuJoCo-verified collision-free & torque-safe (<35% stall) full-body envelope.
LIVE_LO = (-0.86, -1.24, -0.98, -0.84)
LIVE_HI = (0.86, 0.42, 0.58, 0.84)
SLEEP_ARM_MAX = 0.86   # deep curled-down slump for doze/lonely/yawn (still >75 mm above lid, 0 collisions)


def clamp(value, lo, hi):
    return lo if value < lo else hi if value > hi else value


def smoothstep(t):
    t = clamp(t, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def ease_out_back(t, overshoot=1.45):
    t = clamp(t, 0.0, 1.0) - 1.0
    return 1.0 + (overshoot + 1.0) * t * t * t + overshoot * t * t


def ease_in_out_cubic(t):
    t = clamp(t, 0.0, 1.0)
    return 4.0 * t * t * t if t < 0.5 else 1.0 - ((-2.0 * t + 2.0) ** 3) / 2.0


class OrganicNoise:
    """Incommensurate multi-harmonic oscillators that never repeat a cycle."""

    def __init__(self, rng):
        self.phases = [rng.uniform(0.0, math.tau) for _ in range(12)]
        self.freqs = (
            0.23, 0.61, 1.37,   # base subtle drift
            0.31, 0.79, 1.73,   # arm breathing + weight shift
            0.29, 0.83, 1.91,   # neck subtle nod/counter-bob
            0.37, 1.07, 2.29,   # head curiosity cock + micro-saccades
        )

    def sample(self, t):
        s = math.sin
        p, f = self.phases, self.freqs
        return (
            0.55 * s(t * f[0] + p[0]) + 0.30 * s(t * f[1] + p[1]) + 0.15 * s(t * f[2] + p[2]),
            0.65 * s(t * f[3] + p[3]) + 0.25 * s(t * f[4] + p[4]) + 0.10 * s(t * f[5] + p[5]),
            0.55 * s(t * f[6] + p[6]) + 0.30 * s(t * f[7] + p[7]) + 0.15 * s(t * f[8] + p[8]),
            0.50 * s(t * f[9] + p[9]) + 0.35 * s(t * f[10] + p[10]) + 0.15 * s(t * f[11] + p[11]),
        )


class Phrase:
    """One procedurally generated multi-beat full-body motion phrase.

    Each keyframe is (t, d_base, d_arm, d_neck, d_head, stiff_mult, ease_kind).
    Offsets are relative to the live gaze anchor (base, REST[1], neck, 0).
    """

    def __init__(self, name, keyframes, *, side=1, sfx=None):
        self.name = name
        self.keyframes = keyframes
        self.duration = keyframes[-1][0]
        self.side = side
        self.sfx = sfx or []   # list of (t, sound_name, volume)
        self.played_sfx = set()

    def evaluate(self, age):
        frames = self.keyframes
        if age <= 0.0:
            _, b, a, n, h, st, _ = frames[0]
            return (b * self.side, a, n, h * self.side), st
        if age >= self.duration:
            _, b, a, n, h, st, _ = frames[-1]
            return (b * self.side, a, n, h * self.side), st
        for idx in range(len(frames) - 1):
            f0, f1 = frames[idx], frames[idx + 1]
            if f0[0] <= age <= f1[0]:
                span = max(1e-4, f1[0] - f0[0])
                u = (age - f0[0]) / span
                kind = f1[6]
                if kind == 'pop':
                    w = ease_out_back(u, 1.45)
                elif kind == 'snap':
                    w = ease_out_back(u, 1.85)
                elif kind == 'heavy':
                    w = ease_in_out_cubic(u)
                else:
                    w = smoothstep(u)
                b = f0[1] + (f1[1] - f0[1]) * w
                a = f0[2] + (f1[2] - f0[2]) * w
                n = f0[3] + (f1[3] - f0[3]) * w
                h = f0[4] + (f1[4] - f0[4]) * w
                st = f0[5] + (f1[5] - f0[5]) * u
                return (b * self.side, a, n, h * self.side), st
        return (0.0, 0.0, 0.0, 0.0), 1.0

    def due_sfx(self, age):
        due = []
        for idx, (at, sound, vol) in enumerate(self.sfx):
            if idx not in self.played_sfx and age >= at:
                self.played_sfx.add(idx)
                due.append((sound, vol))
        return due


def synthesize_phrase(name, rng, side=1, mood=None):
    """Build a fresh, never-identical full-body Pixar gesture phrase on demand.

    Uses gaze-stabilized body translation (d_neck ~= d_arm + pitch_delta) so BITCH
    physically leans in, cranes upright, recoils back, and bounces using its whole
    4-joint body while keeping natural eye contact.
    """
    mood = mood or {}
    valence = float(mood.get('valence', 0.1))
    arousal = float(mood.get('arousal', 0.4))
    loneliness = float(mood.get('loneliness', 0.0))

    tempo = clamp(1.0 - 0.18 * arousal + 0.22 * loneliness, 0.76, 1.32)
    amp = clamp(rng.uniform(0.88, 1.14) * (1.0 + 0.12 * arousal - 0.12 * loneliness), 0.72, 1.24)
    s = lambda v: v * amp
    t = lambda sec: sec * tempo * rng.uniform(0.92, 1.08)

    if name == 'curious':
        # Iconic WALL-E / Luxo lean-in: brief recoil anticipation, then lunge ~85 mm forward
        # with dead-level gaze-stabilized neck + deep 32 deg head cock + curious second-look shift.
        t1 = t(0.20)
        t2 = t1 + t(0.38)
        t3 = t2 + t(0.50)
        t4 = t3 + t(0.66)
        t5 = t4 + t(0.52)
        lunge = clamp(s(0.76), 0.58, 0.90)
        tilt = clamp(s(0.72), 0.54, 0.80)
        return Phrase(name, (
            (0.0, -s(0.04), -s(0.20), -s(0.16), -s(0.20), 1.2, 'smooth'),
            (t1, s(0.06), lunge * 0.68, lunge * 0.66, tilt * 0.92, 1.65, 'pop'),
            (t2, s(0.10), lunge, lunge - 0.02, tilt, 1.35, 'smooth'),
            (t3, s(0.12), lunge * 0.96, lunge + 0.02, tilt * rng.uniform(0.76, 1.05), 1.05, 'smooth'),
            (t4, s(0.05), lunge * 0.44, lunge * 0.42, -tilt * 0.34, 0.95, 'smooth'),
            (t5, 0.0, 0.0, 0.0, 0.0, 0.95, 'smooth'),
        ), side=side, sfx=[(t1 * 0.7, 'curious', 0.38)] if rng.random() < 0.55 else [])

    if name == 'double-take':
        # Explosive Pixar double-take: whip back + head recoil, snap forward tall with eye lock!
        t1, t2, t3, t4 = t(0.16), t(0.46), t(0.96), t(1.46)
        return Phrase(name, (
            (0.0, -s(0.08), -s(0.34), -s(0.22), -s(0.46), 2.1, 'smooth'),
            (t1, s(0.06), s(0.56), s(0.56), s(0.68), 2.4, 'snap'),
            (t2, s(0.10), s(0.78), s(0.78), s(0.58), 1.9, 'pop'),
            (t3, s(0.04), s(0.46), s(0.46), s(0.22), 1.2, 'smooth'),
            (t4, 0.0, 0.06, 0.06, 0.0, 1.0, 'smooth'),
        ), side=side, sfx=[(t1 * 0.45, 'blip', 0.38)] if rng.random() < 0.65 else [])

    if name in ('happy', 'recognize'):
        # Joyful crouch-and-spring perk-up with wide rhythmic WALL-E head waggles & eye lock!
        t1, t2, t3, t4, t5 = t(0.20), t(0.54), t(0.90), t(1.28), t(1.72)
        return Phrase(name, (
            (0.0, 0.0, s(0.36), s(0.18), -s(0.22), 1.7, 'smooth'),
            (t1, s(0.28), s(0.78), s(0.82), s(0.72), 2.2, 'snap'),
            (t2, -s(0.28), s(0.36), s(0.34), -s(0.72), 2.15, 'snap'),
            (t3, s(0.22), s(0.80), s(0.84), s(0.60), 2.0, 'pop'),
            (t4, -s(0.08), s(0.40), s(0.40), -s(0.26), 1.4, 'smooth'),
            (t5, 0.0, 0.06, 0.06, 0.0, 1.0, 'smooth'),
        ), side=side, sfx=[(t1 * 0.55, rng.choice(('happy', 'walle', 'doubleblip')), 0.42)])

    if name == 'victory':
        # Full-body Luxo Jr. celebration dance! Deep squat -> huge tall bounce + wide sweeping base twists!
        t1, t2, t3, t4, t5, t6, t7 = t(0.24), t(0.64), t(1.04), t(1.44), t(1.84), t(2.22), t(2.60)
        return Phrase(name, (
            (0.0, 0.0, s(0.95), s(0.45), -s(0.26), 1.85, 'smooth'),
            (t1, s(0.62), s(0.76), s(0.86), s(0.80), 2.45, 'snap'),
            (t2, -s(0.62), s(0.20), s(0.16), -s(0.80), 2.45, 'snap'),
            (t3, s(0.56), s(0.84), s(0.92), s(0.78), 2.4, 'snap'),
            (t4, -s(0.46), s(0.28), s(0.24), -s(0.72), 2.15, 'pop'),
            (t5, s(0.20), s(0.78), s(0.86), s(0.48), 1.85, 'pop'),
            (t6, 0.0, s(0.44), s(0.48), -s(0.16), 1.3, 'smooth'),
            (t7, 0.0, 0.0, 0.0, 0.0, 1.0, 'smooth'),
        ), side=side, sfx=[(t1 * 0.5, 'tada', 0.52), (t3, 'giggle', 0.40)])

    if name == 'alarm':
        # Energetic, impossible-to-ignore full-body wake-up jolt!
        t1, t2, t3, t4, t5 = t(0.20), t(0.46), t(0.74), t(1.04), t(1.38)
        return Phrase(name, (
            (0.0, -s(0.18), -s(0.32), -s(0.12), -s(0.45), 2.4, 'smooth'),
            (t1, s(0.58), s(0.90), s(1.08), s(0.80), 2.7, 'snap'),
            (t2, -s(0.58), s(0.32), s(0.46), -s(0.80), 2.7, 'snap'),
            (t3, s(0.52), s(0.94), s(1.12), s(0.76), 2.6, 'snap'),
            (t4, -s(0.44), s(0.42), s(0.56), -s(0.68), 2.4, 'snap'),
            (t5, 0.0, s(0.24), s(0.28), 0.0, 1.5, 'smooth'),
        ), side=side, sfx=[(0.05, 'excited', 0.68)])

    if name in ('sad', 'lonely'):
        # Heartbreaking full-body WALL-E collapse: brief tiny hopeful lift, then heavy slow slump
        # forward & down with eyes drooping toward the shoebox lid.
        t1, t2, t3, t4 = t(0.48), t(1.45), t(2.55), t(3.35)
        slump = 1.24 if name == 'lonely' else 1.02
        return Phrase(name, (
            (0.0, 0.0, s(0.16), s(0.26), -s(0.14), 0.68, 'smooth'),
            (t1, -s(0.14), slump * 0.62, -0.10, s(0.48), 0.52, 'heavy'),
            (t2, -s(0.22), slump, -0.18, s(0.66), 0.44, 'heavy'),
            (t3, -s(0.16), slump * 0.92, -0.16, s(0.56), 0.46, 'heavy'),
            (t4, -s(0.06), slump * 0.45, -0.08, s(0.22), 0.58, 'smooth'),
        ), side=side, sfx=[(t1 * 0.85, rng.choice(('sigh', 'whimper', 'sad')), 0.38)])

    if name == 'grumpy':
        # Sassy Pixar huff: sharp indignant base turn away, stiff tall spine, rapid stubborn head shakes,
        # sulky hold, then reluctant side-eye peek back!
        t1, t2, t3, t4, t5, t6 = t(0.18), t(0.42), t(0.68), t(1.15), t(1.62), t(2.12)
        return Phrase(name, (
            (0.0, -s(0.10), -s(0.22), s(0.08), -s(0.20), 1.9, 'smooth'),
            (t1, s(0.60), s(0.46), s(0.24), -s(0.66), 2.35, 'snap'),
            (t2, s(0.56), s(0.42), s(0.18), s(0.52), 2.25, 'snap'),
            (t3, s(0.64), s(0.48), s(0.22), -s(0.58), 1.9, 'pop'),
            (t4, s(0.58), s(0.36), s(0.16), -s(0.34), 1.1, 'smooth'),
            (t5, s(0.22), s(0.54), s(0.52), s(0.38), 1.35, 'pop'),
            (t6, 0.0, 0.0, 0.0, 0.0, 1.0, 'smooth'),
        ), side=side, sfx=[(t1 * 0.65, rng.choice(('grumble', 'angry')), 0.42)])

    if name == 'glance':
        # Crisp peripheral look-aside: head darts first, base follows wide, leans slightly to check, returns.
        look = rng.uniform(0.38, 0.62)
        t1, t2, t3, t4 = t(0.20), t(0.80), t(1.32), t(1.75)
        return Phrase(name, (
            (0.0, -0.05, -0.08, -0.04, s(0.44), 1.35, 'smooth'),
            (t1, look, s(0.42), s(0.48) + rng.uniform(-0.08, 0.14), s(0.52), 1.25, 'pop'),
            (t2, look * 0.92, s(0.48), s(0.50), s(0.16), 0.95, 'smooth'),
            (t3, look * 0.42, s(0.22), s(0.24), -s(0.28), 1.0, 'smooth'),
            (t4, 0.0, 0.0, 0.0, 0.0, 0.95, 'smooth'),
        ), side=side)

    if name == 'peek':
        # Playful hide-and-peek: turns far aside and ducks low, then SPRINGS straight back & lunges in!
        t1, t2, t3, t4, t5 = t(0.28), t(0.95), t(1.30), t(2.05), t(2.60)
        return Phrase(name, (
            (0.0, -s(0.08), -0.10, 0.06, -s(0.24), 1.15, 'smooth'),
            (t1, s(0.58), s(0.86), s(0.22), s(0.58), 1.05, 'smooth'),
            (t2, s(0.56), s(0.92), s(0.28), s(0.54), 0.90, 'smooth'),
            (t3, s(0.04), s(0.94), s(1.04), -s(0.68), 1.85, 'snap'),
            (t4, 0.0, s(0.72), s(0.78), -s(0.44), 1.1, 'smooth'),
            (t5, 0.0, 0.0, 0.0, 0.0, 0.95, 'smooth'),
        ), side=side, sfx=[(t3 * 0.85, 'blip', 0.32)] if rng.random() < 0.55 else [])

    if name == 'shy':
        # Sweet shy tuck: turns away with downward glance, then shy sideways peek back up.
        t1, t2, t3, t4 = t(0.28), t(1.25), t(1.78), t(2.50)
        return Phrase(name, (
            (0.0, -0.04, -0.12, -0.04, -0.16, 0.95, 'smooth'),
            (t1, s(0.52), s(0.56), -0.08, s(0.68), 0.88, 'smooth'),
            (t2, s(0.48), s(0.48), s(0.44), -s(0.48), 0.92, 'smooth'),
            (t3, s(0.18), s(0.58), s(0.64), -s(0.36), 1.0, 'smooth'),
            (t4, 0.0, 0.0, 0.0, 0.0, 0.9, 'smooth'),
        ), side=side)

    if name == 'stretch':
        # Luxurious full-body morning stretch: pull way back looking at the ceiling, then arch way forward!
        t1, t2, t3, t4 = t(0.52), t(1.48), t(2.35), t(2.95)
        return Phrase(name, (
            (0.0, 0.0, -0.12, 0.02, -0.18, 0.85, 'smooth'),
            (t1, -s(0.22), -s(0.38), s(0.58), -s(0.54), 0.72, 'smooth'),
            (t2, s(0.20), s(1.06), s(1.02), s(0.66), 0.85, 'smooth'),
            (t3, s(0.08), s(0.68), s(0.76), s(0.32), 0.9, 'smooth'),
            (t4, 0.0, 0.0, 0.0, 0.0, 0.9, 'smooth'),
        ), side=side, sfx=[(t1 * 0.55, 'powerup', 0.34)] if valence >= 0 else [])

    if name == 'yawn':
        # Expressive Wall-E sleepy yawn: crane chin high toward ceiling, then heavy melt down!
        t1, t2, t3 = t(0.62), t(1.58), t(2.48)
        return Phrase(name, (
            (0.0, 0.0, -0.10, 0.12, -0.12, 0.62, 'smooth'),
            (t1, -s(0.10), -s(0.24), s(0.68), s(0.44), 0.55, 'smooth'),
            (t2, s(0.06), s(0.92), -0.06, -s(0.28), 0.46, 'heavy'),
            (t3, 0.0, s(0.78), -0.14, s(0.18), 0.44, 'heavy'),
        ), side=side, sfx=[(0.18, 'yawn', 0.44)])

    if name == 'doze':
        # Authentic nodding off: slumps gradually low over the box, catches itself once with a startled
        # sleepy jerk back upright, then melts deeply down into a cozy curled-up rest!
        return Phrase(name, (
            (0.0, 0.0, 0.0, 0.0, 0.0, 0.55, 'smooth'),
            (1.05, s(0.06), 0.52, -0.08, s(0.20), 0.44, 'heavy'),
            (2.20, s(0.12), 1.15, -0.16, s(0.36), 0.38, 'heavy'),
            (2.58, s(0.04), 0.38, 0.24, -s(0.26), 1.15, 'pop'),
            (3.95, s(0.14), 1.38, -0.16, s(0.40), 0.38, 'heavy'),
            (5.10, s(0.16), 1.56, -0.18, s(0.34), 0.35, 'heavy'),
        ), side=side, sfx=[(0.35, 'yawn', 0.38)])

    if name == 'survey':
        # Genuine wide curious room scan: cranes tall & sweeps far left -> leans in -> sweeps far right!
        a1 = rng.uniform(0.55, 0.74)
        a2 = -rng.uniform(0.50, 0.70)
        t1, t2, t3, t4, t5 = t(0.42), t(1.32), t(2.15), t(2.95), t(3.55)
        return Phrase(name, (
            (0.0, -0.06, s(0.20), s(0.26), s(0.36), 1.1, 'smooth'),
            (t1, a1, s(0.76), s(0.84), s(0.54), 1.15, 'smooth'),
            (t2, a1 * 0.92, s(0.92), s(0.96), -s(0.28), 0.95, 'smooth'),
            (t3, a2, s(0.68), s(0.86), -s(0.56), 1.15, 'smooth'),
            (t4, a2 * 0.75, s(0.44), s(0.52), s(0.24), 0.95, 'smooth'),
            (t5, 0.0, 0.0, 0.0, 0.0, 0.95, 'smooth'),
        ), side=side)

    if name == 'ponder':
        # Real thinking character: swivels aside, leans back & glances thoughtfully up (+24 deg),
        # pauses as if turning an idea over, then snaps forward with a "eureka!" return to eye contact!
        t1, t2, t3, t4 = t(0.32), t(1.08), t(1.92), t(2.68)
        return Phrase(name, (
            (0.0, -s(0.08), -0.06, s(0.08), s(0.18), 1.05, 'smooth'),
            (t1, -s(0.46), -s(0.22), s(0.30), -s(0.58), 0.95, 'smooth'),
            (t2, -s(0.40), -s(0.16), s(0.36), -s(0.50), 0.85, 'smooth'),
            (t3, -s(0.04), s(0.62), s(0.62), s(0.34), 1.45, 'pop'),
            (t4, 0.0, s(0.30), s(0.30), s(0.12), 1.0, 'smooth'),
        ), side=side)

    if name == 'scared':
        # Unmistakable full-body startle! Whip arm all the way back, drop neck to stare wide-eyed,
        # rapid terrified head rattle, then cautious forward peek!
        t1, t2, t3, t4, t5 = t(0.16), t(0.42), t(0.72), t(1.22), t(1.88)
        return Phrase(name, (
            (0.0, -s(0.10), -s(0.42), -s(0.18), -s(0.66), 2.5, 'snap'),
            (t1, -s(0.16), -s(0.44), -s(0.18), s(0.60), 2.4, 'snap'),
            (t2, -s(0.14), -s(0.38), -s(0.16), -s(0.48), 2.1, 'pop'),
            (t3, -s(0.08), -s(0.24), -s(0.10), s(0.32), 1.5, 'smooth'),
            (t4, 0.0, s(0.42), s(0.42), -s(0.26), 1.0, 'smooth'),
            (t5, 0.0, 0.0, 0.0, 0.0, 0.95, 'smooth'),
        ), side=side, sfx=[(0.02, 'startle', 0.48)])

    if name == 'proud':
        # Stand TALL & vertical, chin lifted a regal +16 deg looking down its nose, noble swivel!
        t1, t2, t3, t4 = t(0.32), t(1.08), t(1.78), t(2.28)
        return Phrase(name, (
            (0.0, 0.0, s(0.18), 0.04, -0.14, 1.3, 'smooth'),
            (t1, s(0.22), s(0.74), s(1.02), s(0.30), 1.65, 'pop'),
            (t2, -s(0.18), s(0.72), s(0.98), -s(0.24), 1.15, 'smooth'),
            (t3, s(0.06), s(0.56), s(0.74), s(0.16), 1.05, 'smooth'),
            (t4, 0.0, 0.0, 0.0, 0.0, 0.95, 'smooth'),
        ), side=side, sfx=[(t1 * 0.65, 'proud', 0.40)])

    if name == 'seek':
        # Actively searching where someone just vanished: leans far forward & sweeps wide!
        t1, t2, t3, t4 = t(0.34), t(0.95), t(1.62), t(2.15)
        return Phrase(name, (
            (0.0, 0.0, s(0.42), s(0.44), s(0.38), 1.35, 'smooth'),
            (t1, s(0.44), s(0.78), s(0.72), s(0.60), 1.3, 'pop'),
            (t2, -s(0.36), s(0.64), s(0.74), -s(0.54), 1.25, 'smooth'),
            (t3, s(0.12), s(0.26), s(0.32), -s(0.24), 1.0, 'smooth'),
            (t4, 0.0, 0.0, 0.0, 0.0, 0.95, 'smooth'),
        ), side=side)

    if name == 'notice':
        t1, t2 = t(0.14), t(0.56)
        return Phrase(name, (
            (0.0, 0.0, -s(0.18), -s(0.08), -s(0.24), 1.95, 'smooth'),
            (t1, 0.0, s(0.54), s(0.62), s(0.46), 2.15, 'snap'),
            (t2, 0.0, s(0.34), s(0.38), s(0.22), 1.25, 'smooth'),
        ), side=side)

    if name == 'attend':
        # Engaged forward lean-in when the floor opens for you!
        t1, t2 = t(0.20), t(0.72)
        return Phrase(name, (
            (0.0, 0.0, s(0.18), s(0.22), s(0.28), 1.3, 'smooth'),
            (t1, 0.0, s(0.62), s(0.68), s(0.46), 1.35, 'pop'),
            (t2, 0.0, s(0.52), s(0.56), s(0.34), 1.05, 'smooth'),
        ), side=side)

    if name == 'listen':
        # Actively leaning in toward the speaker with steady eye lock & attentive head cock!
        t1, t2, t3 = t(0.24), t(0.85), t(1.55)
        return Phrase(name, (
            (0.0, 0.0, s(0.24), s(0.28), s(0.28), 1.25, 'smooth'),
            (t1, 0.0, s(0.66), s(0.72), s(0.48), 1.3, 'pop'),
            (t2, 0.0, s(0.58), s(0.64), s(0.34), 1.0, 'smooth'),
            (t3, 0.0, s(0.62), s(0.68), s(0.40), 0.95, 'smooth'),
        ), side=side)

    if name == 'look-target':
        t1, t2, t3, t4 = t(0.20), t(0.64), t(1.30), t(2.10)
        return Phrase(name, (
            (0.0, 0.0, -0.12, -0.06, s(0.24), 1.4, 'smooth'),
            (t1, 0.0, s(0.48), s(0.54), s(0.52), 1.5, 'pop'),
            (t2, 0.0, s(0.74), s(0.78), -s(0.34), 1.15, 'smooth'),
            (t3, 0.0, s(0.54), s(0.56), -s(0.20), 1.0, 'smooth'),
            (t4, 0.0, s(0.34), s(0.36), -s(0.10), 0.95, 'smooth'),
        ), side=side)

    if name == 'mirror':
        t1, t2, t3, t4 = t(0.20), t(0.58), t(1.50), t(1.96)
        return Phrase(name, (
            (0.0, 0.0, -0.12, -0.04, -0.18, 1.3, 'smooth'),
            (t1, 0.0, s(0.36), s(0.42), s(0.66), 1.5, 'pop'),
            (t2, 0.0, s(0.58), s(0.64), s(0.74), 1.15, 'smooth'),
            (t3, 0.0, s(0.38), s(0.42), s(0.44), 1.0, 'smooth'),
            (t4, 0.0, 0.0, 0.0, 0.0, 0.95, 'smooth'),
        ), side=side)

    # ---------------------------------------------------------------- Expressive speech choreography!
    # Every speaking style uses full-body posture + gaze-stabilized lean-in so talking looks alive.
    if name == 'speaking':
        t1, t2, t3 = t(0.22), t(0.68), t(1.32)
        return Phrase(name, (
            (0.0, 0.0, s(0.12), s(0.14), s(0.18), 1.5, 'smooth'),
            (t1, 0.0, s(0.54), s(0.60), -s(0.34), 1.6, 'pop'),
            (t2, 0.0, s(0.38), s(0.42), s(0.26), 1.35, 'smooth'),
            (t3, 0.0, s(0.44), s(0.48), -s(0.14), 1.25, 'smooth'),
        ), side=side)

    if name == 'speak-warm':
        t1, t2, t3 = t(0.26), t(0.84), t(1.48)
        return Phrase(name, (
            (0.0, 0.0, s(0.18), s(0.20), -s(0.18), 1.2, 'smooth'),
            (t1, s(0.06), s(0.64), s(0.68), s(0.44), 1.35, 'smooth'),
            (t2, -s(0.04), s(0.52), s(0.56), s(0.30), 1.15, 'smooth'),
            (t3, 0.0, s(0.48), s(0.52), s(0.22), 1.1, 'smooth'),
        ), side=side)

    if name == 'speak-gentle':
        # Base stays 0 so gentle empathetic speech holds steady, tender eye contact.
        t1, t2, t3 = t(0.44), t(1.12), t(1.75)
        return Phrase(name, (
            (0.0, 0.0, s(0.10), s(0.08), -s(0.20), 0.85, 'smooth'),
            (t1, 0.0, s(0.54), s(0.48), s(0.46), 0.82, 'smooth'),
            (t2, 0.0, s(0.44), s(0.40), s(0.36), 0.78, 'smooth'),
            (t3, 0.0, s(0.38), s(0.36), s(0.28), 0.80, 'smooth'),
        ), side=side)

    if name == 'speak-upbeat':
        t1, t2, t3, t4 = t(0.22), t(0.58), t(0.98), t(1.44)
        return Phrase(name, (
            (0.0, 0.0, -s(0.12), -s(0.10), -s(0.26), 1.9, 'smooth'),
            (t1, s(0.20), s(0.66), s(0.68), s(0.58), 2.15, 'snap'),
            (t2, -s(0.18), s(0.34), s(0.36), -s(0.54), 2.05, 'pop'),
            (t3, s(0.16), s(0.70), s(0.72), s(0.48), 1.95, 'pop'),
            (t4, 0.0, s(0.44), s(0.46), -s(0.18), 1.5, 'smooth'),
        ), side=side)

    if name == 'speak-emphatic':
        t1, t2, t3 = t(0.20), t(0.62), t(1.22)
        return Phrase(name, (
            (0.0, -s(0.08), -s(0.18), -s(0.04), s(0.34), 1.85, 'smooth'),
            (t1, s(0.12), s(0.76), s(0.72), -s(0.38), 2.1, 'snap'),
            (t2, s(0.06), s(0.48), s(0.50), s(0.20), 1.65, 'pop'),
            (t3, 0.0, s(0.42), s(0.44), -s(0.10), 1.35, 'smooth'),
        ), side=side)

    if name == 'speak-playful':
        t1, t2, t3, t4 = t(0.26), t(0.68), t(1.14), t(1.60)
        return Phrase(name, (
            (0.0, s(0.06), s(0.10), s(0.12), s(0.44), 1.65, 'smooth'),
            (t1, s(0.26), s(0.60), s(0.60), -s(0.62), 1.85, 'pop'),
            (t2, -s(0.24), s(0.72), s(0.74), s(0.54), 1.85, 'pop'),
            (t3, s(0.14), s(0.44), s(0.46), -s(0.38), 1.5, 'smooth'),
            (t4, 0.0, s(0.38), s(0.40), s(0.28), 1.3, 'smooth'),
        ), side=side)

    if name == 'speak-annoyed':
        t1, t2, t3, t4 = t(0.18), t(0.46), t(0.92), t(1.46)
        return Phrase(name, (
            (0.0, 0.0, -s(0.20), s(0.02), -s(0.20), 2.0, 'smooth'),
            (t1, s(0.24), s(0.56), s(0.44), s(0.52), 2.2, 'snap'),
            (t2, s(0.16), s(0.42), s(0.34), -s(0.46), 2.0, 'pop'),
            (t3, -s(0.18), s(0.36), s(0.44), s(0.34), 1.6, 'smooth'),
            (t4, 0.0, s(0.24), s(0.26), -s(0.16), 1.25, 'smooth'),
        ), side=side)

    if name == 'speak-amused':
        # Laughing torso bounce!
        t1, t2, t3, t4 = t(0.18), t(0.42), t(0.70), t(1.14)
        return Phrase(name, (
            (0.0, 0.0, -s(0.16), s(0.04), s(0.26), 2.0, 'smooth'),
            (t1, s(0.12), s(0.68), s(0.58), -s(0.48), 2.25, 'snap'),
            (t2, -s(0.10), s(0.16), s(0.38), s(0.42), 2.15, 'snap'),
            (t3, s(0.10), s(0.72), s(0.66), -s(0.42), 2.0, 'pop'),
            (t4, 0.0, s(0.44), s(0.50), s(0.18), 1.4, 'smooth'),
        ), side=side)

    if name == 'speak-surprised':
        t1, t2, t3 = t(0.18), t(0.54), t(1.22)
        return Phrase(name, (
            (0.0, -s(0.06), -s(0.34), -s(0.12), -s(0.46), 2.25, 'snap'),
            (t1, s(0.08), s(0.34), s(0.56), s(0.54), 2.1, 'pop'),
            (t2, 0.0, s(0.70), s(0.76), -s(0.30), 1.5, 'smooth'),
            (t3, 0.0, s(0.40), s(0.44), s(0.12), 1.2, 'smooth'),
        ), side=side)

    if name == 'speak-farewell':
        t1, t2, t3, t4 = t(0.28), t(0.68), t(1.16), t(1.72)
        return Phrase(name, (
            (0.0, 0.0, s(0.44), s(0.50), s(0.32), 1.25, 'smooth'),
            (t1, s(0.14), s(0.62), s(0.64), -s(0.44), 1.35, 'pop'),
            (t2, -s(0.14), s(0.48), s(0.56), s(0.42), 1.3, 'pop'),
            (t3, s(0.12), s(0.18), s(0.22), -s(0.22), 1.05, 'smooth'),
            (t4, 0.0, 0.0, 0.0, 0.0, 0.95, 'smooth'),
        ), side=side)

    return None


def mood_baseline(mood, t, noise_vec):
    """Continuous full-body emotional posture + gaze-stabilized organic breathing.

    Returns (d_base, d_arm, d_neck, d_head, stiff_mult).
    """
    valence = clamp(float(mood.get('valence', 0.1)), -1.0, 1.0)
    arousal = clamp(float(mood.get('arousal', 0.35)), 0.0, 1.0)
    loneliness = clamp(float(mood.get('loneliness', 0.0)), 0.0, 1.0)
    annoyance = clamp(float(mood.get('annoyance', 0.0)), 0.0, 1.0)

    # Full-body emotional silhouette:
    # - Happy/engaged rides taller and leaned slightly closer with eyes level (d_arm & d_neck move together).
    # - Lonely/sad slumps arm forward & drops chin low with heavy sideways head droop.
    # - Annoyed pulls upright & tense with edgy head cock.
    arm_posture = (0.30 * max(0.0, valence) + 0.42 * loneliness
                   + 0.24 * max(0.0, -valence) + 0.12 * annoyance)
    neck_posture = (0.34 * max(0.0, valence) - 0.16 * loneliness
                    - 0.10 * max(0.0, -valence) + 0.04 * annoyance)
    head_posture = 0.24 * loneliness - 0.18 * annoyance + 0.08 * max(0.0, valence)

    # Breathing: whole-body chest heave on arm with counter-stabilized neck so gaze stays calm.
    breath_rate = 0.26 + 0.16 * arousal - 0.08 * loneliness
    breath = math.sin(math.tau * breath_rate * t)
    breath_amp = 0.055 + 0.025 * arousal + 0.022 * loneliness
    d_arm = arm_posture + breath * breath_amp + 0.045 * noise_vec[1]
    d_neck = neck_posture + breath * breath_amp * 0.72 + 0.036 * noise_vec[2]
    d_base = 0.032 * noise_vec[0]
    d_head = head_posture + 0.078 * noise_vec[3]

    stiff_mult = clamp(1.0 + 0.32 * arousal + 0.16 * annoyance - 0.32 * loneliness - 0.18 * max(0.0, -valence), 0.52, 1.55)
    return d_base, d_arm, d_neck, d_head, stiff_mult


def idle_choice_for_mood(mode, mood, recent, rng):
    """Choose the next non-repeating procedural idle beat weighted by internal mood."""
    loneliness = float(mood.get('loneliness', 0.0))
    valence = float(mood.get('valence', 0.1))
    annoyance = float(mood.get('annoyance', 0.0))

    if mode == 'scan':
        pool = ['survey', 'stretch', 'glance', 'peek', 'curious']
        if loneliness > 0.45 or valence < -0.25:
            pool += ['lonely', 'sad']
    else:
        pool = ['curious', 'glance', 'shy', 'peek', 'stretch']
        if loneliness > 0.45:
            pool += ['lonely', 'lonely', 'sad']
        elif valence > 0.45:
            pool += ['happy', 'curious', 'peek']
        if annoyance > 0.45:
            pool += ['grumpy', 'glance']

    fresh = [item for item in pool if item not in recent] or pool
    return rng.choice(fresh)
