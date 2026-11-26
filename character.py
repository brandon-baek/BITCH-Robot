"""Procedural Pixar-style character motion engine for BITCH's 4-joint puppet.

Instead of replaying identical lookup tables, every gesture is synthesized fresh with
anticipation, kinematic chain lag (head leads -> neck -> arm -> base), coupled arcs,
continuous non-repeating organic breathing/micro-saccades, and an emotional baseline
(valence, arousal, loneliness, annoyance) paired with subtle WALL-E creature sounds.

Designed to run at 50 Hz natively on a Raspberry Pi 4 (<0.5 ms/step) while protecting
servo lifespan on a 24/7 desk via automatic bearing/head detach and zero-power parking.
"""
import math
import random

REST = (0.0, -0.8, -0.8, 0.0)
BASE_LIMIT = 0.55
NECK_MIN, NECK_MAX = -0.88, -0.43


def clamp(value, lo, hi):
    return lo if value < lo else hi if value > hi else value


def smoothstep(t):
    t = clamp(t, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def ease_out_back(t, overshoot=1.35):
    t = clamp(t, 0.0, 1.0) - 1.0
    return 1.0 + (overshoot + 1.0) * t * t * t + overshoot * t * t


class OrganicNoise:
    """Incommensurate multi-harmonic oscillators that never repeat a cycle."""

    def __init__(self, rng):
        self.phases = [rng.uniform(0.0, math.tau) for _ in range(12)]
        # Irrational-ratio frequencies keep breathing and curiosity shifts perpetually non-periodic.
        self.freqs = (
            0.23, 0.61, 1.37,   # base micro-drift
            0.31, 0.79, 1.73,   # arm breathing + weight shift
            0.29, 0.83, 1.91,   # neck subtle nod/bob
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
    """One procedurally generated multi-beat motion phrase with smooth keyframe interpolation.

    Each keyframe is (t, d_base, d_arm, d_neck, d_head, stiff_mult, ease_kind).
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
                w = ease_out_back(u, 1.25) if kind == 'pop' else smoothstep(u)
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
    """Build a fresh, never-identical Pixar-principled gesture phrase on demand.

    Every gesture opens with brief anticipation, cascades head -> neck -> arm -> base,
    and settles with randomized amplitude, pacing, and secondary micro-motion.
    """
    mood = mood or {}
    valence = float(mood.get('valence', 0.1))
    arousal = float(mood.get('arousal', 0.4))
    loneliness = float(mood.get('loneliness', 0.0))

    # Tempo & amplitude subtly reflect internal mood so even the same beat feels alive.
    tempo = clamp(1.0 - 0.18 * arousal + 0.22 * loneliness, 0.78, 1.35)
    amp = clamp(rng.uniform(0.84, 1.18) * (1.0 + 0.15 * arousal - 0.15 * loneliness), 0.65, 1.28)
    s = lambda v: v * amp
    t = lambda sec: sec * tempo * rng.uniform(0.92, 1.08)

    if name == 'curious':
        # Wall-E head cock: quick recoil, big curious tilt + forward lean, tiny second-look adjust.
        t1 = t(0.18)
        t2 = t1 + t(0.32)
        t3 = t2 + t(0.42)
        t4 = t3 + t(0.65)
        t5 = t4 + t(0.55)
        tilt = clamp(s(0.42), 0.24, 0.54)
        lean = clamp(s(0.24), 0.12, 0.30)
        return Phrase(name, (
            (0.0, 0.0, -0.05, -0.04, -0.10, 1.0, 'smooth'),
            (t1, 0.0, -0.02, 0.06, tilt * 0.85, 1.35, 'pop'),
            (t2, s(0.04), lean, 0.11, tilt, 1.1, 'smooth'),
            (t3, s(0.05), lean * 0.92, 0.14, tilt * rng.uniform(0.75, 1.08), 0.95, 'smooth'),
            (t4, s(0.02), lean * 0.55, 0.06, -tilt * 0.32, 0.85, 'smooth'),
            (t5, 0.0, 0.0, 0.0, 0.0, 0.9, 'smooth'),
        ), side=side, sfx=[(t1 * 0.7, 'curious', 0.34)] if rng.random() < 0.45 else [])

    if name == 'double-take':
        # Sharp anticipation recoil, snap toward target, tiny settling bob.
        t1, t2, t3, t4 = t(0.14), t(0.40), t(0.88), t(1.36)
        return Phrase(name, (
            (0.0, 0.0, -0.11, -0.08, -0.26, 1.85, 'smooth'),
            (t1, 0.0, 0.03, 0.12, s(0.38), 2.1, 'pop'),
            (t2, 0.0, s(0.26), s(0.18), s(0.32), 1.65, 'pop'),
            (t3, 0.0, s(0.19), s(0.10), s(0.12), 1.1, 'smooth'),
            (t4, 0.0, 0.04, 0.02, 0.0, 0.95, 'smooth'),
        ), side=side, sfx=[(t1 * 0.5, 'blip', 0.32)] if rng.random() < 0.55 else [])

    if name in ('happy', 'recognize'):
        # Joyful perk-up with playful asymmetric head waggle (Wall-E seeing Eva).
        t1, t2, t3, t4 = t(0.16), t(0.44), t(0.72), t(1.14)
        return Phrase(name, (
            (0.0, 0.0, -0.09, -0.06, -0.12, 1.6, 'smooth'),
            (t1, s(0.03), s(0.22), s(0.17), s(0.32), 2.0, 'pop'),
            (t2, -s(0.03), s(0.16), s(0.10), -s(0.30), 1.85, 'pop'),
            (t3, s(0.02), s(0.20), s(0.14), s(0.20), 1.5, 'smooth'),
            (t4, 0.0, 0.04, 0.02, 0.0, 1.0, 'smooth'),
        ), side=side, sfx=[(t1 * 0.6, rng.choice(('happy', 'walle', 'doubleblip')), 0.38)])

    if name == 'victory':
        # Excited wake-up / celebration wiggle with alternating head pops & joyful bounce!
        t1, t2, t3, t4, t5, t6 = t(0.18), t(0.46), t(0.78), t(1.10), t(1.48), t(1.95)
        return Phrase(name, (
            (0.0, 0.0, -0.12, -0.08, -0.16, 1.8, 'smooth'),
            (t1, s(0.16), s(0.28), s(0.22), s(0.42), 2.25, 'pop'),
            (t2, -s(0.16), s(0.14), s(0.08), -s(0.44), 2.25, 'pop'),
            (t3, s(0.14), s(0.30), s(0.24), s(0.38), 2.15, 'pop'),
            (t4, -s(0.10), s(0.22), s(0.16), -s(0.30), 1.85, 'pop'),
            (t5, 0.0, s(0.24), s(0.18), s(0.14), 1.4, 'smooth'),
            (t6, 0.0, 0.0, 0.0, 0.0, 1.0, 'smooth'),
        ), side=side, sfx=[(t1 * 0.5, 'tada', 0.48), (t3, 'giggle', 0.36)])

    if name == 'alarm':
        # Energetic, urgent wake-up jolt sequence!
        t1, t2, t3, t4, t5 = t(0.18), t(0.42), t(0.70), t(1.02), t(1.42)
        return Phrase(name, (
            (0.0, 0.0, -0.14, -0.08, -0.25, 2.3, 'smooth'),
            (t1, s(0.18), s(0.28), s(0.22), s(0.45), 2.5, 'pop'),
            (t2, -s(0.18), s(0.26), s(0.20), -s(0.45), 2.5, 'pop'),
            (t3, s(0.16), s(0.28), s(0.22), s(0.40), 2.4, 'pop'),
            (t4, -s(0.12), s(0.20), s(0.15), -s(0.28), 2.0, 'pop'),
            (t5, 0.0, 0.08, 0.06, 0.0, 1.4, 'smooth'),
        ), side=side, sfx=[(0.05, 'excited', 0.65)])

    if name in ('sad', 'lonely'):
        # Heavy Wall-E droop: brief hopeful glance up, then slow weighted slump & tilt away.
        t1, t2, t3, t4 = t(0.42), t(1.25), t(2.35), t(3.15)
        droop = 0.16 if name == 'lonely' else 0.13
        return Phrase(name, (
            (0.0, 0.0, -0.03, 0.05, -0.08, 0.65, 'smooth'),
            (t1, -s(0.05), droop, -0.12, s(0.32), 0.52, 'smooth'),
            (t2, -s(0.07), droop + 0.04, -0.18, s(0.40), 0.44, 'smooth'),
            (t3, -s(0.04), droop * 0.8, -0.12, s(0.24), 0.50, 'smooth'),
            (t4, 0.0, 0.04, -0.05, s(0.08), 0.58, 'smooth'),
        ), side=side, sfx=[(t1 * 0.9, rng.choice(('sigh', 'whimper', 'sad')), 0.34)])

    if name == 'grumpy':
        # Playful indignation: sharp huff-turn slightly off-axis, chin dip, little stubborn shake.
        t1, t2, t3, t4, t5 = t(0.16), t(0.40), t(0.68), t(1.35), t(1.85)
        return Phrase(name, (
            (0.0, 0.0, -0.06, 0.04, -0.12, 1.7, 'smooth'),
            (t1, s(0.22), -0.06, -0.10, -s(0.34), 2.1, 'pop'),
            (t2, s(0.20), -0.04, -0.12, s(0.22), 2.0, 'pop'),
            (t3, s(0.24), -0.05, -0.11, -s(0.26), 1.6, 'smooth'),
            (t4, s(0.18), -0.03, -0.08, -s(0.14), 1.0, 'smooth'),
            (t5, 0.0, 0.0, 0.0, 0.0, 0.95, 'smooth'),
        ), side=side, sfx=[(t1 * 0.7, rng.choice(('grumble', 'angry')), 0.38)])

    if name == 'glance':
        # Natural peripheral look-aside: head darts first, base follows slightly, returns with settle.
        look = rng.uniform(0.14, 0.28)
        t1, t2, t3, t4 = t(0.18), t(0.75), t(1.25), t(1.62)
        return Phrase(name, (
            (0.0, -0.02, -0.04, -0.03, s(0.28), 1.15, 'smooth'),
            (t1, look, 0.03, rng.uniform(-0.04, 0.09), s(0.26), 1.05, 'smooth'),
            (t2, look * 0.88, 0.05, rng.uniform(-0.02, 0.06), s(0.08), 0.9, 'smooth'),
            (t3, look * 0.45, 0.03, 0.02, -s(0.16), 0.95, 'smooth'),
            (t4, 0.0, 0.0, 0.0, 0.0, 0.9, 'smooth'),
        ), side=side)

    if name == 'peek':
        # Shy tuck-and-peek: dips slightly aside, then leans forward curiously to peek back!
        t1, t2, t3, t4, t5 = t(0.22), t(0.95), t(1.22), t(2.00), t(2.50)
        return Phrase(name, (
            (0.0, -0.04, 0.04, 0.03, -0.18, 0.95, 'smooth'),
            (t1, s(0.22), -0.14, -0.08, s(0.36), 0.85, 'smooth'),
            (t2, s(0.20), -0.08, -0.04, s(0.30), 0.85, 'smooth'),
            (t3, s(0.10), s(0.26), s(0.15), -s(0.38), 1.3, 'pop'),
            (t4, s(0.05), s(0.20), s(0.11), -s(0.20), 0.95, 'smooth'),
            (t5, 0.0, 0.0, 0.0, 0.0, 0.9, 'smooth'),
        ), side=side, sfx=[(t3 * 0.8, 'blip', 0.25)] if rng.random() < 0.35 else [])

    if name == 'shy':
        t1, t2, t3, t4 = t(0.22), t(1.25), t(1.68), t(2.45)
        return Phrase(name, (
            (0.0, -0.02, -0.04, -0.03, -0.12, 0.85, 'smooth'),
            (t1, s(0.22), -0.12, -0.09, s(0.42), 0.78, 'smooth'),
            (t2, s(0.20), -0.09, 0.06, -s(0.24), 0.82, 'smooth'),
            (t3, s(0.08), 0.04, 0.08, -s(0.20), 0.9, 'smooth'),
            (t4, 0.0, 0.0, 0.0, 0.0, 0.85, 'smooth'),
        ), side=side)

    if name == 'stretch':
        # Luxurious waking-up stretch: pull back tall, yawn lean forward, settle back.
        t1, t2, t3, t4 = t(0.45), t(1.38), t(2.22), t(2.85)
        return Phrase(name, (
            (0.0, 0.0, -0.06, -0.03, -0.12, 0.75, 'smooth'),
            (t1, -s(0.06), -0.20, 0.08, -s(0.24), 0.65, 'smooth'),
            (t2, -s(0.04), s(0.26), s(0.18), s(0.32), 0.78, 'smooth'),
            (t3, 0.0, s(0.18), s(0.12), s(0.16), 0.8, 'smooth'),
            (t4, 0.0, 0.0, 0.0, 0.0, 0.85, 'smooth'),
        ), side=side, sfx=[(t1 * 0.6, 'powerup', 0.28)] if valence >= 0 else [])

    if name == 'yawn':
        # Cute Wall-E sleepy yawn before nap/sleep.
        t1, t2, t3 = t(0.55), t(1.45), t(2.35)
        return Phrase(name, (
            (0.0, 0.0, -0.04, 0.02, -0.06, 0.55, 'smooth'),
            (t1, 0.0, -0.12, 0.14, s(0.18), 0.50, 'smooth'),
            (t2, 0.0, 0.08, -0.14, -s(0.10), 0.45, 'smooth'),
            (t3, 0.0, 0.04, -0.18, 0.06, 0.42, 'smooth'),
        ), side=side, sfx=[(0.18, 'yawn', 0.42)])

    if name == 'doze':
        # Nodding off: sinks gradually, catches itself with a sleepy jerk once, then settles.
        return Phrase(name, (
            (0.0, 0.0, 0.0, 0.0, 0.0, 0.5, 'smooth'),
            (0.95, 0.0, 0.04, -0.10, 0.07, 0.42, 'smooth'),
            (2.10, 0.0, 0.08, -0.21, 0.14, 0.38, 'smooth'),
            (2.45, 0.0, 0.04, -0.07, 0.06, 0.72, 'pop'),
            (3.90, 0.0, 0.09, -0.23, 0.15, 0.36, 'smooth'),
            (5.10, 0.0, 0.10, -0.24, 0.15, 0.35, 'smooth'),
        ), side=side, sfx=[(0.35, 'yawn', 0.34)])

    if name == 'survey':
        # Scanning the room with genuine curiosity (pauses at two distinct angles, not a sine sweep).
        a1 = rng.uniform(0.24, 0.40)
        a2 = -rng.uniform(0.16, 0.32)
        t1, t2, t3, t4, t5 = t(0.28), t(1.18), t(1.92), t(2.68), t(3.20)
        return Phrase(name, (
            (0.0, -0.04, -0.06, -0.04, s(0.32), 1.0, 'smooth'),
            (t1, a1, 0.07, 0.11, s(0.26), 0.9, 'smooth'),
            (t2, a1 * 0.9, 0.09, 0.06, -s(0.14), 0.85, 'smooth'),
            (t3, a2, 0.04, 0.09, -s(0.26), 0.9, 'smooth'),
            (t4, a2 * 0.7, 0.02, 0.02, s(0.12), 0.85, 'smooth'),
            (t5, 0.0, 0.0, 0.0, 0.0, 0.9, 'smooth'),
        ), side=side)

    if name == 'ponder':
        # Looking up-and-away like a character genuinely thinking, with subtle chin-lift & return.
        t1, t2, t3, t4 = t(0.24), t(0.75), t(1.85), t(2.60)
        return Phrase(name, (
            (0.0, 0.0, 0.08, 0.03, 0.14, 0.9, 'smooth'),
            (t1, -s(0.14), -0.14, 0.10, -s(0.38), 0.82, 'smooth'),
            (t2, -s(0.12), -0.12, 0.12, -s(0.34), 0.78, 'smooth'),
            (t3, -s(0.04), 0.04, 0.14, s(0.15), 0.88, 'smooth'),
            (t4, 0.0, 0.10, 0.05, 0.08, 0.9, 'smooth'),
        ), side=side)

    if name == 'scared':
        t1, t2, t3, t4 = t(0.18), t(0.45), t(0.75), t(1.35)
        return Phrase(name, (
            (0.0, 0.0, -0.16, -0.10, -0.35, 2.2, 'pop'),
            (t1, -s(0.06), -0.18, -0.10, s(0.22), 2.1, 'pop'),
            (t2, -s(0.06), -0.16, -0.07, -s(0.18), 1.8, 'smooth'),
            (t3, -s(0.04), -0.11, -0.04, s(0.12), 1.3, 'smooth'),
            (t4, 0.0, 0.0, 0.0, 0.0, 1.0, 'smooth'),
        ), side=side, sfx=[(0.02, 'startle', 0.45)])

    if name == 'proud':
        t1, t2, t3 = t(0.28), t(1.45), t(2.05)
        return Phrase(name, (
            (0.0, 0.0, 0.06, -0.04, -0.08, 1.2, 'smooth'),
            (t1, 0.0, -0.14, 0.20, s(0.12), 1.35, 'pop'),
            (t2, s(0.04), -0.10, 0.15, s(0.16), 1.05, 'smooth'),
            (t3, 0.0, 0.0, 0.0, 0.0, 0.95, 'smooth'),
        ), side=side, sfx=[(t1 * 0.7, 'proud', 0.36)])

    if name == 'seek':
        # Looking around where someone just vanished.
        t1, t2, t3, t4 = t(0.32), t(0.90), t(1.55), t(2.05)
        return Phrase(name, (
            (0.0, 0.0, 0.14, 0.03, s(0.24), 1.15, 'smooth'),
            (t1, s(0.09), 0.22, -0.03, s(0.38), 1.05, 'smooth'),
            (t2, s(0.12), 0.15, 0.08, -s(0.28), 1.0, 'smooth'),
            (t3, s(0.07), -0.02, 0.06, -s(0.16), 0.95, 'smooth'),
            (t4, 0.0, 0.0, 0.0, 0.0, 0.9, 'smooth'),
        ), side=side)

    if name == 'notice':
        t1, t2 = t(0.13), t(0.55)
        return Phrase(name, (
            (0.0, 0.0, -0.07, -0.05, -0.12, 1.75, 'smooth'),
            (t1, 0.0, s(0.16), s(0.14), s(0.22), 1.85, 'pop'),
            (t2, 0.0, s(0.11), s(0.08), s(0.12), 1.2, 'smooth'),
        ), side=side)

    if name == 'attend':
        t1, t2 = t(0.18), t(0.65)
        return Phrase(name, (
            (0.0, 0.0, 0.05, 0.05, s(0.22), 1.15, 'smooth'),
            (t1, 0.0, s(0.20), s(0.10), s(0.32), 1.15, 'smooth'),
            (t2, 0.0, s(0.16), s(0.07), s(0.24), 1.0, 'smooth'),
        ), side=side)

    if name == 'listen':
        t1, t2, t3 = t(0.22), t(0.75), t(1.35)
        return Phrase(name, (
            (0.0, 0.0, 0.08, 0.05, s(0.20), 1.1, 'smooth'),
            (t1, 0.0, s(0.22), s(0.13), s(0.36), 1.1, 'smooth'),
            (t2, 0.0, s(0.18), s(0.08), s(0.22), 0.95, 'smooth'),
            (t3, 0.0, s(0.15), s(0.06), s(0.18), 0.9, 'smooth'),
        ), side=side)

    if name == 'look-target':
        t1, t2, t3, t4 = t(0.18), t(0.60), t(1.25), t(2.10)
        return Phrase(name, (
            (0.0, 0.0, -0.04, -0.03, s(0.12), 1.25, 'smooth'),
            (t1, 0.0, 0.08, 0.06, s(0.28), 1.3, 'pop'),
            (t2, 0.0, 0.18, 0.08, -s(0.16), 1.05, 'smooth'),
            (t3, 0.0, 0.14, 0.03, -s(0.10), 0.95, 'smooth'),
            (t4, 0.0, 0.09, 0.01, -s(0.05), 0.9, 'smooth'),
        ), side=side)

    if name == 'mirror':
        t1, t2, t3, t4 = t(0.20), t(0.55), t(1.48), t(1.92)
        return Phrase(name, (
            (0.0, 0.0, -0.06, -0.02, -0.10, 1.15, 'smooth'),
            (t1, 0.0, 0.05, 0.04, s(0.42), 1.25, 'pop'),
            (t2, 0.0, 0.18, 0.07, s(0.46), 1.05, 'smooth'),
            (t3, 0.0, 0.10, 0.04, s(0.28), 0.95, 'smooth'),
            (t4, 0.0, 0.0, 0.0, 0.0, 0.9, 'smooth'),
        ), side=side)

    return None


def mood_baseline(mood, t, noise_vec):
    """Continuous emotional posture + organic breathing + kinematic counter-coupling.

    Returns (d_base, d_arm, d_neck, d_head, stiff_mult).
    """
    valence = clamp(float(mood.get('valence', 0.1)), -1.0, 1.0)
    arousal = clamp(float(mood.get('arousal', 0.35)), 0.0, 1.0)
    loneliness = clamp(float(mood.get('loneliness', 0.0)), 0.0, 1.0)
    annoyance = clamp(float(mood.get('annoyance', 0.0)), 0.0, 1.0)

    # Posture offsets: happy perks up, lonely/sad droops arm & chin, grumpy pitches slightly forward.
    arm_posture = -0.08 * max(0.0, valence) + 0.14 * loneliness + 0.10 * max(0.0, -valence) - 0.04 * annoyance
    neck_posture = 0.07 * valence - 0.11 * loneliness - 0.04 * annoyance
    head_posture = 0.12 * loneliness - 0.10 * annoyance

    # Breathing: slow chest-like heave on arm with subtle counter-nod on neck so gaze stays steady.
    breath_rate = 0.24 + 0.14 * arousal - 0.08 * loneliness
    breath = math.sin(math.tau * breath_rate * t)
    breath_amp = 0.028 + 0.014 * arousal + 0.015 * loneliness
    d_arm = arm_posture + breath * breath_amp + 0.022 * noise_vec[1]
    d_neck = neck_posture - breath * breath_amp * 0.45 + 0.020 * noise_vec[2]
    d_base = 0.016 * noise_vec[0]
    d_head = head_posture + 0.042 * noise_vec[3]

    stiff_mult = clamp(1.0 + 0.28 * arousal + 0.12 * annoyance - 0.32 * loneliness - 0.18 * max(0.0, -valence), 0.55, 1.45)
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
            pool += ['happy', 'curious']
        if annoyance > 0.45:
            pool += ['grumpy', 'glance']

    fresh = [item for item in pool if item not in recent] or pool
    return rng.choice(fresh)
