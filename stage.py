"""Finite choreography for the Pi's existing spring actor."""
from array import array
from bisect import bisect_right
from dataclasses import dataclass
from functools import lru_cache
import math
import sys
import threading
import wave

REST = (0, -.8, -.8, 0)
SHOWS = {'dance': 'disco', 'disco': 'disco_pro', 'opera': 'opera'}
POSES = {
    'bow': ((0, 0, -.53, -.49, 0, 1.25), (.35, 0, -.93, -.86, .06, .85),
            (.86, *REST, .9)),
    'nod': ((0, 0, -.75, -.6, .06, 1.8), (.22, 0, -.72, -.86, .04, 2.2),
            (.50, 0, -.79, -.68, 0, 1.5), (.77, *REST, .8)),
    'curious': ((0, 0, -.83, -.84, -.14, 1.1), (.17, 0, -.82, -.7, .45, 1.8),
                (.38, .06, -.52, -.58, .43, .9), (.82, *REST, .7)),
    'happy': ((0, 0, -.96, -.86, -.12, 1.7), (.18, .06, -.49, -.49, .35, 2.6),
              (.47, -.08, -.71, -.68, -.32, 2.4), (.76, .03, -.55, -.53, .19, 1.5)),
    'sad': ((0, 0, -.74, -.68, .1, .75), (.22, -.1, -.95, -.88, -.31, .65)),
    'surprised': ((0, 0, -.93, -.49, .36, 3), (.25, -.08, -1.02, -.59, -.12, 2.4),
                  (.56, 0, -.91, -.7, .12, 1.3), (.86, *REST, .85)),
    'sway': ((0, 0, -.76, -.66, .26, 1.7), (.24, .28, -.66, -.55, .42, 1.2),
             (.62, -.25, -.71, -.67, -.41, 1.55), (.92, *REST, .65)),
    'left': ((0, 0, -.81, -.74, .24, 1.6), (.18, -.29, -.75, -.65, .23, 1.2)),
    'right': ((0, 0, -.81, -.74, -.24, 1.6), (.18, .29, -.75, -.65, -.23, 1.2)),
    'rest': ((0, *REST, .65),),
}
CHIRPS = ('blip', 'doubleblip', 'tada', 'process')
OPERATIONS = {'gesture': tuple(POSES), 'chirp': CHIRPS, 'pause': ('hold',)}
MAX_STEPS = 12
render_lock = threading.Lock()


def validate_steps(steps):
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise ValueError('routine needs 1–12 steps')
    total = 0.
    for step in steps:
        if not isinstance(step, dict) or set(step) != {'op', 'style', 'seconds'}:
            raise ValueError('expected a routine step')
        op, style, seconds = step['op'], step['style'], step['seconds']
        if not isinstance(op, str) or not isinstance(style, str) or style not in OPERATIONS.get(op, ()):
            raise ValueError('unknown routine action')
        if type(seconds) not in (float, int) or not math.isfinite(seconds) or not .3 <= seconds <= 4:
            raise ValueError('each step needs .3–4 seconds')
        total += seconds
    if total > 30 or all(step['op'] == 'pause' for step in steps):
        raise ValueError('routine needs an action and may run for at most 30 seconds')
    return steps


@dataclass
class Score:
    clock: tuple
    poses: tuple
    shakes: tuple
    cues: tuple
    song: str | None
    duration: float

    def at(self, elapsed):
        index = bisect_right(self.clock, elapsed) - 1
        pose, stiff, idle = self.poses[index]
        offsets = [0.] * 4
        for joint, amp, hz, start, end in self.shakes:
            if start <= elapsed < end:
                fade = min(1., (elapsed-start)/.1, (end-elapsed)/.1)
                offsets[joint] += amp * fade * math.sin(math.tau * hz * (elapsed-start))
        return tuple(x+d for x,d in zip(pose, offsets)), stiff, idle


class ScoreBuilder:
    """The same beat methods as Actor, without hardware or sleeping."""
    def __init__(self):
        self.T = 0.
        self.idle_amt = .45
        self.pose = list(REST)
        self.stiff = 1.
        self.song = None
        self.beats, self.shakes, self.cues = {}, [], []

    def actor(self):
        return self

    def say(self, _):
        # The original stage directions were console captions.
        pass

    def music(self, song):
        self.song = song

    def to(self, b=None, a=None, n=None, h=None, stiff=None):
        for joint, value in enumerate((b, a, n, h)):
            if value is not None:
                self.pose[joint] = value
        if stiff is not None:
            self.stiff = max(.35, min(3.6, stiff))
        return self

    def wait(self, seconds):
        if seconds > 0:
            self.beats[self.T] = (tuple(self.pose), self.stiff, self.idle_amt)
            self.T += seconds
        return self

    def shake(self, joint, amp, hz, duration):
        self.shakes.append(('banh'.index(joint), min(.13, amp), hz, self.T, self.T+duration))
        return self

    def sfx(self, name):
        self.cues.append((self.T, name))
        return self

    def end(self):
        self.to(*REST, stiff=.75).wait(1.35)

    def build(self):
        return Score(tuple(self.beats), tuple(self.beats.values()), tuple(self.shakes), tuple(self.cues),
                     self.song, self.T)


@lru_cache(maxsize=3)
def legacy_show(name):
    import shows

    builder = ScoreBuilder()
    getattr(shows, SHOWS[name])(stage=builder)
    return builder.build()


def routine(steps):
    builder = ScoreBuilder()
    for step in validate_steps(steps):
        op, style, seconds = step['op'], step['style'], step['seconds']
        if op == 'gesture':
            timeline = POSES[style]
            for index, (fraction, b, a, n, h, stiff) in enumerate(timeline):
                until = timeline[index+1][0] if index+1 < len(timeline) else 1
                builder.to(b, a, n, h, stiff).wait((until-fraction)*seconds)
        elif op == 'chirp':
            builder.sfx(style).wait(seconds)
        else:
            builder.wait(seconds)
    builder.end()
    return builder.build()


def write_soundtrack(score, output, volume, lead=0.):
    import music
    import sounds

    def read(path):
        with wave.open(path, 'rb') as source:
            if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, music.SR):
                raise ValueError('invalid show audio')
            samples = array('h')
            samples.frombytes(source.readframes(source.getnframes()))
            if sys.byteorder == 'big':
                samples.byteswap()
            return samples

    song = read(music.render(score.song)) if score.song else array('h')
    gain = .7 * volume ** 1.65
    mixed = array('h', (round(x * gain) for x in song))
    for timestamp, name in score.cues:
        if name not in sounds.V:
            continue
        cue = read(sounds._render(name, 0))
        index = round(timestamp * music.SR)
        missing = index + len(cue) - len(mixed)
        if missing > 0:
            mixed.extend(array('h', [0]) * missing)
        for offset, value in enumerate(cue):
            i = index + offset
            mixed[i] = max(-32768, min(32767, mixed[i] + round(value * gain * .48)))
    with wave.open(output, 'wb') as result:
        result.setparams((1, 2, music.SR, 0, 'NONE', 'not compressed'))
        if lead:
            n = round(lead * music.SR)
            wake = array('h', (round(36 * gain * math.sin(math.tau * 200 * i / music.SR)) for i in range(n)))
            if sys.byteorder == 'big':
                wake.byteswap()
            result.writeframesraw(wake.tobytes())
        if sys.byteorder == 'big':
            mixed.byteswap()
        result.writeframes(mixed.tobytes())
    return max(score.duration, len(mixed)/music.SR)
