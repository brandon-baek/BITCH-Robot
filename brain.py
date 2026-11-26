"""Pi runtime for quiet motion, speaker playback and desktop handoff."""
import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
import os
import random
from pathlib import Path
import secrets
import struct
import signal
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from urllib.parse import parse_qs, urlsplit

import character
import macros
import stage
from actor import Actor

PORT = 8765
BASE = 0.55
NECK = (-0.88, -0.43)
REST = (0, -0.8, -0.8, 0)
# Each entry sets a target at a beat onset. Offsets are relative to calibrated
# gaze. Actor carries momentum across beats and across interruptions.
BEATS = {
    'curious': ((0, 0, -.08, -.06, -.12), (.22, 0, -.02, .09, .44),
                (.53, .018, .29, .14, .44), (1.55, 0, .22, .1, -.21), (2.28, 0, 0, 0, 0)),
    'glance': ((0, -.025, -.045, -.04, .33), (.18, .17, .03, .07, .30),
               (.74, .14, .06, .04, .08), (1.24, .13, .04, .04, -.19), (1.48, 0, 0, 0, 0)),
    'shy': ((0, -.02, -.05, -.035, -.12), (.2, .24, -.13, -.1, .49),
            (1.25, .23, -.1, .075, -.27), (1.65, .09, .045, .1, -.25), (2.45, 0, 0, 0, 0)),
    'happy': ((0, 0, -.09, -.07, -.12), (.18, 0, .22, .17, .32),
              (.48, 0, .17, .09, -.32), (.76, 0, .21, .15, .19), (1.17, 0, .055, .03, 0)),
    'proud': ((0, 0, .07, -.05, -.09), (.29, 0, -.13, .2, .09),
              (1.48, .04, -.10, .15, .17), (2.06, 0, 0, 0, 0)),
    'sad': ((0, 0, -.04, .035, -.08), (.48, -.06, .13, -.14, .39),
            (1.8, -.04, .1, -.09, .27), (2.64, 0, 0, 0, 0)),
    'scared': ((0, 0, -.16, -.10, -.37), (.28, -.06, -.18, -.1, .20),
               (.47, -.06, -.17, -.08, -.18), (.69, -.05, -.13, -.04, .12), (1.35, 0, 0, 0, 0)),
    'survey': ((0, -.04, -.08, -.05, .38), (.21, .29, .09, .13, .33),
               (1.13, .26, .12, .07, -.19), (1.72, .11, .06, .01, -.28), (2.32, 0, 0, 0, 0)),
    'notice': ((0, 0, -.075, -.06, -.14), (.12, 0, .16, .15, .24), (.55, 0, .12, .09, .14)),
    'attend': ((0, 0, .06, .055, .26), (.17, 0, .23, .11, .37), (.63, 0, .19, .08, .29)),
    'ponder': ((0, 0, .10, .03, .16), (.18, 0, -.03, -.09, .28),
               (.55, -.14, -.16, .08, -.41), (1.52, -.12, -.14, .08, -.37),
               (2.14, -.05, .05, .15, .17), (2.63, 0, .11, .06, .09)),
    'double-take': ((0, 0, -.11, -.08, -.28), (.14, 0, .03, .10, .39),
                    (.45, 0, .28, .19, .35), (.94, 0, .24, .12, .15),
                    (1.38, 0, .09, .05, 0)),
    'seek': ((0, 0, .16, .03, .26), (.34, .08, .25, -.04, .42),
             (.88, .12, .16, .09, -.30), (1.52, .08, -.02, .07, -.17),
             (2.03, 0, 0, 0, 0)),
    'stretch': ((0, 0, -.07, -.04, -.14), (.38, -.07, -.20, -.06, -.24),
                (1.27, -.05, .29, .20, .33), (2.15, 0, .23, .15, .21),
                (2.75, 0, 0, 0, 0)),
    'peek': ((0, -.04, .05, .04, -.21), (.18, .21, -.16, -.09, .39),
             (.98, .21, -.08, -.045, .33), (1.17, .13, .29, .17, -.43),
             (1.95, .06, .23, .13, -.24), (2.43, 0, 0, 0, 0)),
    'look-target': ((0, 0, -.045, -.04, .12), (.19, 0, .085, .06, .31),
                    (.63, 0, .20, .085, -.19), (1.25, 0, .16, .025, -.12),
                    (2.1, 0, .105, .01, -.06)),
    'listen': ((0, 0, .09, .055, .22), (.22, 0, .24, .14, .42),
               (.73, 0, .19, .087, .25), (1.32, 0, .16, .065, .20)),
    'speaking': ((0, 0, -.055, -.045, .15), (.19, 0, .22, .11, -.29),
                 (.59, .03, .13, .055, -.18), (1.25, 0, .12, .065, .09)),
    'speak-warm': ((0, 0, -.04, 0, -.12), (.23, 0, .25, .085, .28),
                   (.77, 0, .20, .055, .22)),
    'speak-gentle': ((0, 0, -.09, .035, -.24), (.46, 0, .24, -.025, .36),
                     (1.24, 0, .15, .025, .28)),
    'speak-upbeat': ((0, 0, -.16, -.10, -.21), (.16, 0, .29, .21, .43),
                     (.46, .055, .23, .14, -.38), (.77, -.04, .29, .15, .28),
                     (1.25, 0, .18, .09, -.11)),
    'speak-emphatic': ((0, -.04, -.12, .09, .28), (.20, .045, .30, -.08, -.23),
                       (.56, .03, .20, .13, -.08), (1.12, 0, .17, .06, .06)),
    'speak-playful': ((0, .025, -.09, .045, .33), (.28, .10, .17, -.065, -.43),
                      (.65, .04, .27, .14, .13), (1.24, 0, .12, .065, .24)),
    'speak-annoyed': ((0, 0, -.095, .04, -.13), (.16, .04, .27, -.12, .33),
                      (.41, .015, .19, -.03, .24), (.85, -.07, .16, .11, -.25), (1.4, 0, .1, .02, -.12)),
    'speak-amused': ((0, 0, -.04, .045, .16), (.21, 0, .23, -.11, -.31),
                     (.43, .03, -.04, .12, .18), (.69, .015, .25, -.08, -.24),
                     (1.03, 0, .15, .055, -.12)),
    'speak-surprised': ((0, 0, -.20, -.075, -.31), (.19, 0, .05, .17, .33),
                        (.52, 0, .24, .10, -.18), (1.18, 0, .12, .055, .08)),
    'speak-farewell': ((0, 0, .13, .08, .2), (.27, 0, .24, -.09, -.27),
                       (.66, 0, .14, .12, .23), (1.12, .055, -.08, .015, .15),
                       (1.65, .11, -.10, -.085, -.11)),
    'mirror': ((0, 0, -.06, -.02, -.10), (.19, 0, .055, .045, .44),
               (.55, 0, .19, .075, .48), (1.48, 0, .11, .05, .30), (1.9, 0, 0, 0, 0)),
}
# Nodding off: the head sinks, catches itself once, then settles. The Mac parks the body after it.
BEATS['doze'] = ((0, 0, 0, 0, 0), (.9, 0, .04, -.09, .06), (2.1, 0, .07, -.2, .13),
                 (2.45, 0, .05, -.07, .07), (3.9, 0, .09, -.23, .15), (5.2, 0, .09, -.24, .15))
BEATS['recognize'] = BEATS['happy']
BEATS['lonely'] = BEATS['sad']
BEATS['grumpy'] = BEATS['speak-annoyed']
BEATS['victory'] = BEATS['happy']
BEATS['alarm'] = BEATS['double-take']
BEATS['yawn'] = BEATS['doze']
SPEECH = {name for name in BEATS if name == 'speaking' or name.startswith('speak-')}
SOCIAL = {'notice', 'attend', 'listen', 'ponder', 'speaking', 'recognize', 'mirror-left', 'mirror-right',
          'curious', 'peek', 'shy', 'glance', 'survey',
          'happy', 'sad', 'lonely', 'grumpy', 'victory', 'alarm', 'yawn',
          'scared', 'proud', 'double-take', 'seek', 'look-target', 'doze', 'stretch'} | SPEECH
WEIGHTED_MODES = {'sad', 'lonely', 'doze', 'yawn', 'speak-gentle'}
DEEP_ARM_MODES = {'doze', 'lonely', 'sad', 'yawn'}
IDLE_BEATS = ('curious', 'glance', 'shy', 'stretch', 'peek')
ROOM_BEATS = ('survey', 'stretch', 'glance', 'sad', 'peek')
# How springy each state feels. Gestures carry their own timing, so these stay close to 1.
SOFTNESS = {'doze': .45, 'stretch': .8, 'sad': .6, 'lonely': .6, 'yawn': .6, 'shy': .9, 'speak-gentle': .85,
            'scared': 1.5, 'alarm': 1.4, 'victory': 1.3, 'happy': 1.25, 'recognize': 1.25, 'notice': 1.25,
            'double-take': 1.25}
# These can cut a gesture short. Anything else waits until the current gesture has made its point.
URGENT = {'listen', 'attend', 'notice', 'alarm', 'scared', 'park', 'aim', 'show'}
LOUD = {'victory', 'alarm', 'scared'}   # the only states allowed to move fast
# Separate quick head accents from the heavier arm's slower weight transfer.
SPEECH_WEIGHT = {'speak-gentle': .68, 'speak-upbeat': 1.2, 'speak-emphatic': 1.16,
                 'speak-annoyed': 1.15, 'speak-amused': 1.25, 'speak-surprised': 1.18, 'speak-playful': 1.12, 'speak-farewell': .8}


def pose_at(frames, elapsed):
    return next(row[1:] for row in reversed(frames) if row[0] <= max(0, elapsed))


KEY = macros.HOME / 'robot.key'


def connection_key():
    macros.HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Publish the complete key atomically, even if two terminals ask for it at once.
    with tempfile.NamedTemporaryFile(dir=macros.HOME) as temporary:
        temporary.write(secrets.token_urlsafe(24).encode())
        temporary.flush()
        try:
            os.link(temporary.name, KEY)
        except FileExistsError:
            pass
    return KEY.read_text().strip()


def command(item):
    mode = item.get('mode')
    if mode not in SOCIAL | {'scan', 'watch', 'park', 'aim'}:
        raise ValueError('unknown motion mode')
    base, neck = float(item.get('base', 0)), float(item.get('neck', -0.8))
    if not math.isfinite(base) or not math.isfinite(neck):
        raise ValueError('invalid motion')
    return mode, max(-BASE, min(BASE, base)), max(NECK[0], min(NECK[1], neck))


def clamp(value, limit):
    return max(-limit, min(limit, value))


class Motion:
    def __init__(self, desk, on_sfx=None):
        self.desk = desk
        self.on_sfx = on_sfx
        self.started = False
        self.rng = random.Random()
        self.noise = character.OrganicNoise(self.rng)
        self.idle_beat = None
        self.idle_phrase = None
        self.social_phrase = None
        self.idle_anchor = None
        self.last_beat = None
        self.next_beat = 0
        self.scan_base = 0
        self.social_at = 0
        self.restoring = False
        self.actor = None
        self.mode = 'park'
        self.cue_at = None
        self.gaze_anchor = None
        self.beat = 'Resting'
        self.lean = 0.
        self.gesture_side = 1
        self.recent_idle = []
        self.base_still = 0.
        self.head_still = 0.
        self.last_sfx_at = -100.
        self.wear = {'powered': 0., 'moving': 0., 'travel': [0.] * 4}

    def start(self):
        desk = self.desk
        known = desk._load()
        if known and all(isinstance(value, (int, float)) and math.isfinite(value) and -2 <= value <= 2 for value in known):
            desk.cur[:] = known
        for index in (1, 2, 3, 0):
            scale = 1000 if index == 0 else 500
            desk.S[index].us(1500 + desk.cur[index] * scale)
            desk.on[index] = True
            time.sleep(0.08)
        self.actor = None
        self.restoring = abs(REST[1] - desk.cur[1]) > 0.02
        self.started = True

    def step(self, mode, base, neck, now, dt, energy=0.0, accent=0.0, score_frame=None, gesture=(0., 0.),
             calm=False, mood=None, sfx_volume=0.0):
        if not self.started:
            if mode == 'park' and score_frame is None:
                return
            self.start()
        desk = self.desk
        if self.actor is None:
            self.actor = Actor(wake=False, desk=desk, powered=True)
        actor = self.actor
        # Unlock MuJoCo-verified collision-free & torque-safe full-body range!
        arm_max = character.SLEEP_ARM_MAX if mode in DEEP_ARM_MODES else character.LIVE_HI[1]
        actor.low = character.LIVE_LO
        actor.high = (character.LIVE_HI[0], arm_max, character.LIVE_HI[2], character.LIVE_HI[3])
        # Mode-adaptive Pixar dynamics: crisp snappy pops for energy/speech, weighted slowness for sad/sleepy,
        # and gentle smooth base tracking when calmly following a face.
        base_scale = min(1., max(0., desk.BASE_SPEED))
        if mode in WEIGHTED_MODES:
            actor.speed = (.5 * base_scale, .8, 1.1, 1.6)
            actor.accel = (1.6, 2.4, 3.8, 6.0)
        elif mode in LOUD:
            actor.speed = (1.4 * base_scale, 2.0, 3.0, 4.4)
            actor.accel = (4.5, 7.0, 11.0, 18.0)
        elif mode in SOCIAL or mode == 'scan':
            actor.speed = (.9 * base_scale, 1.3, 2.2, 3.2)
            actor.accel = (2.6, 4.2, 7.5, 12.0)
        else:
            actor.speed = (.58 * base_scale, 1.0, 1.8, 3.0)
            actor.accel = (1.55, 3.0, 6.5, 12.0)
        if score_frame is not None:
            pose, stiffness, idle = score_frame
            actor.idle_amt = idle * .35
            actor.to(*pose, stiff=stiffness)
            actor.step(min(.02, dt))
            self.mode, self.beat = 'show', 'Performing'
            self.restoring = False
            return
        if (mode != self.mode and mode not in URGENT and self.social_phrase is not None
                and now - self.social_at < self.social_phrase.commit):
            # Let the current gesture make its point before the next one starts, so it reads as one idea.
            mode = self.mode
        following = mode == 'watch' or mode in SOCIAL
        changed = mode != self.mode
        if changed and (mode == 'look-target' or self.mode == 'look-target'):
            self.gaze_anchor = None
        if changed:
            if mode in SPEECH or mode in ('ponder', 'seek', 'double-take', 'look-target', 'victory', 'alarm', 'grumpy'):
                self.gesture_side = (-1 if base > .36 else 1 if base < -.36 else -self.gesture_side)
            self.idle_beat = None
            self.idle_phrase = None
            self.social_phrase = None
            self.social_at = now
            self.next_beat = now + (self.rng.uniform(16., 42.) if calm else self.rng.uniform(5., 9.))
            if mode == 'scan':
                self.scan_base = desk.cur[0]
            self.mode = mode
            if mode in SOCIAL:
                beat_name = 'mirror' if mode.startswith('mirror-') else mode
                side = (-1 if mode == 'mirror-left' else 1 if mode == 'mirror-right'
                        else self.gesture_side if mode in SPEECH or mode in ('ponder', 'seek', 'double-take', 'look-target', 'victory', 'alarm', 'grumpy')
                        else (-1 if base > .30 else 1 if base < -.30 else self.rng.choice((-1, 1))))
                self.social_phrase = character.synthesize_phrase(beat_name, self.rng, side=side, mood=mood)
        if mode == 'scan':
            base, neck = self.scan_base, REST[2]
        elif mode == 'park':
            base, neck = REST[0], REST[2]
        if following:
            if self.gaze_anchor is None or abs(base - self.gaze_anchor) > .15:
                if abs(base - desk.cur[0]) > .13:
                    self.cue_at, self.held_base = now, desk.cur[0]
                    self.throw = base - self.held_base
                self.gaze_anchor = base
        else:
            self.gaze_anchor = self.cue_at = None

        self.beat = mode
        frames = None
        active_phrase = None
        phrase_age = 0.0
        direction = 1
        if mode in SOCIAL:
            self.beat = 'mirror' if mode.startswith('mirror-') else mode
            direction = -1 if mode == 'mirror-left' else 1
            if mode in SPEECH or mode in ('ponder', 'seek', 'double-take', 'look-target', 'victory', 'alarm', 'grumpy'):
                direction = self.gesture_side
            age = now - self.social_at
            if self.social_phrase is not None:
                # The alarm repeats while it rings. Everything else plays once and holds its last pose.
                if mode == 'alarm' and age >= self.social_phrase.duration + 0.15:
                    self.social_at = now
                    age = 0.0
                    self.gesture_side = -self.gesture_side
                    self.social_phrase = character.synthesize_phrase(mode, self.rng, side=self.gesture_side, mood=mood)
                active_phrase, phrase_age = self.social_phrase, age
            else:
                frames = BEATS[self.beat]
        elif mode in ('watch', 'scan'):
            if (self.idle_beat or self.idle_phrase) and self.idle_anchor and (
                    abs(base - self.idle_anchor[0]) > .09 or abs(neck - self.idle_anchor[1]) > .10):
                self.idle_beat = self.idle_phrase = None
                self.next_beat = now + 1.35
            if mode == 'watch' and not (self.idle_beat or self.idle_phrase) and abs(base - desk.cur[0]) > .08:
                self.next_beat = max(self.next_beat, now + 1.65)
            if calm and self.next_beat - now > 65:
                self.next_beat = now + self.rng.uniform(22, 62)
            if now >= self.next_beat and not self.idle_beat:
                self.last_beat = character.idle_choice_for_mood(mode, mood or {}, self.recent_idle, self.rng)
                self.recent_idle = (self.recent_idle + [self.last_beat])[-3:]
                side = -1 if base > .30 else 1 if base < -.30 else self.rng.choice((-1, 1))
                phrase = character.synthesize_phrase(self.last_beat, self.rng, side=side, mood=mood)
                if phrase is not None:
                    self.idle_phrase = (phrase, now)
                    self.idle_anchor = base, neck
                    loneliness = float((mood or {}).get('loneliness', 0.0))
                    # Mostly still, with an occasional gesture: that's what reads as alive rather than busy.
                    gap = (self.rng.uniform(22, 58) if calm
                           else self.rng.uniform(9, 16) if loneliness > 0.5
                           else self.rng.uniform(6, 13))
                    self.next_beat = now + phrase.duration + gap
            if self.idle_beat:
                frames, start, direction = self.idle_beat
                age = now - start
                self.beat = self.last_beat or mode
                if age >= frames[-1][0] + .35:
                    self.idle_beat = frames = None
            elif self.idle_phrase:
                phrase, start = self.idle_phrase
                phrase_age = now - start
                active_phrase = phrase
                self.beat = phrase.name
                if phrase_age >= phrase.duration + .25:
                    self.idle_phrase = active_phrase = None

        stiff_mult = 1.0
        if active_phrase is not None:
            (turn, lean, pitch, tilt), stiff_mult = active_phrase.evaluate(phrase_age)
            arm = REST[1] + lean
            base, neck = base + turn, neck + pitch
            if self.on_sfx and sfx_volume > 0.01 and mode not in SPEECH:
                urgent = mode in ('alarm', 'victory', 'yawn', 'doze', 'lonely', 'grumpy')
                for sound_name, vol in active_phrase.due_sfx(phrase_age):
                    if urgent or now - self.last_sfx_at >= 7.5:
                        self.last_sfx_at = now
                        self.on_sfx(sound_name, min(1.0, vol * sfx_volume))
        else:
            turn, lean, pitch, tilt = pose_at(frames, age) if frames else (0.,) * 4
            arm = REST[1] + lean
            base, neck, tilt = base + direction * turn, neck + pitch, direction * tilt

        # Layer continuous Pixar emotional baseline + organic non-repeating breathing when live.
        # Blend baseline down during active phrases so crafted silhouettes never double-stack or saturate.
        if mood and mode not in ('park', 'aim', 'doze', 'yawn') and not (calm and active_phrase is None and frames is None):
            mb, ma, mn, mh, mst = character.mood_baseline(mood, now, self.noise.sample(now))
            blend = 0.35 if active_phrase is not None else 1.0
            if mode not in SPEECH:
                base += mb * blend
            arm += ma * blend
            neck += mn * blend
            tilt += mh * blend
            stiff_mult *= (1.0 + (mst - 1.0) * (0.5 if active_phrase is not None else 1.0))
            # Velocity-coupled arc: turning the base subtly leads with a curious head tilt & upward lift.
            if following and mode not in SPEECH and self.cue_at is None:
                tilt += clamp(actor.v[0] * 0.18, 0.12)
                neck += min(0.05, abs(actor.v[0]) * 0.05)

        if mode in SPEECH:
            weight = SPEECH_WEIGHT.get(mode, 1.)
            # Talking: the syllable nods (accent) and word gestures from the Mac are the only moving layer
            # on top of the held delivery posture. One clear beat per stressed word, nothing else.
            lean_response = 1 - math.exp(-dt / (.30 if energy > self.lean else .5))
            self.lean += (energy - self.lean) * lean_response
            neck += accent * weight * .9 + gesture[1] * .5
            arm += weight * (.08 * self.lean - .17 * accent) + gesture[1] * .8   # the body rocks a little with each nod
            tilt += direction * weight * .7 * accent + gesture[0] * .9
        elif mode == 'listen':
            # Listening is still: a slow lean that follows how much they're saying, not each syllable.
            self.lean += (energy - self.lean) * (1 - math.exp(-dt / .9))
            arm += .05 * self.lean
            neck += .05 * self.lean
            tilt += .05 * self.lean
        else:
            self.lean = 0.
            if energy > 0 and following:
                neck += .10 * energy
                arm += .06 * energy
                tilt -= .10 * energy
        if self.cue_at is not None:
            elapsed = now - self.cue_at
            side = math.copysign(1, self.throw)
            # Aim the head, then nod, shift weight, and finally commit the base.
            if elapsed < .75:
                tilt += side * (.34 if elapsed < .38 else .11)
                if elapsed > .08:
                    neck += .085
                if elapsed > .14:
                    arm += .12
            if elapsed < .12:
                base = self.held_base
            elif elapsed < .22:
                base = self.held_base - clamp(self.throw * .18, .055)
            elif abs(base - desk.cur[0]) < .11 and elapsed < 1.25:
                base += side * min(.055, abs(self.throw) * .15)
            if elapsed >= 1.25:
                self.cue_at = None
        actor.idle_scale = (.18, .28, .85, .72) if following else (.75, .35, .9, 1.)
        # When mood baseline is active, disable Actor's built-in sine idle completely so two oscillators
        # never fight each other and bounce the arm while trying to stay still.
        base_idle = 0.0 if mood else (.52 if following else .85)
        actor.idle_amt = base_idle if mode not in ('aim', 'park') else 0
        if calm and not (self.idle_beat or self.idle_phrase):
            actor.idle_amt = 0
        if self.cue_at is not None and now - self.cue_at < .14:
            actor.idle_amt = 0
        stiff = max(.35, min(2.2, SOFTNESS.get(self.beat, 1.0) * stiff_mult))
        if self.restoring:
            if abs(REST[1] - desk.cur[1]) > .02:
                base, arm, neck, tilt = desk.cur[0], REST[1], desk.cur[2], desk.cur[3]
                actor.idle_amt = 0
            else:
                self.restoring = False
        actor.to(base, arm, neck, tilt, stiff=stiff)
        quiet_watch = calm and mode == 'watch' and not (self.idle_beat or self.idle_phrase) and self.cue_at is None
        self.rest_base(actor, base, now, quiet_watch)
        self.rest_head(actor, tilt, now, quiet_watch)
        actor.step(min(.02, dt))
        self.count(actor, dt)
        if mode == 'park' and max(abs(a - b) for a, b in zip(REST, desk.cur)) < .008 and max(map(abs, actor.v)) < .025:
            desk.relax()
            self.started = False

    def rest_base(self, actor, base, now, quiet):
        """The base turns on a bearing with no load to hold, so it can go unpowered while it waits."""
        if not quiet or abs(base - self.desk.cur[0]) > .03 or abs(actor.v[0]) > .02:
            self.base_still = now
            if 0 in actor.limp:
                actor.limp.discard(0)
                actor.written[0] = None
            return
        if 0 not in actor.limp and now - self.base_still > 2.5 and hasattr(self.desk, 'relax'):
            self.desk.relax(0)
            actor.limp.add(0)

    def rest_head(self, actor, tilt, now, quiet):
        """Detach balanced head tilt after extended calm stillness to reduce servo hum and wear."""
        if not quiet or abs(tilt - self.desk.cur[3]) > .03 or abs(actor.v[3]) > .02:
            self.head_still = now
            if 3 in actor.limp:
                actor.limp.discard(3)
                actor.written[3] = None
            return
        if 3 not in actor.limp and now - self.head_still > 3.5 and hasattr(self.desk, 'relax'):
            self.desk.relax(3)
            actor.limp.add(3)

    def count(self, actor, dt):
        wear = self.wear
        wear['powered'] += dt
        wear['moving'] += actor.moving
        wear['travel'] = [round(total + moved, 3) for total, moved in zip(wear['travel'], actor.travel)]
        actor.moving, actor.travel = 0., [0.] * 4


class Wear:
    """Minutes the motors were powered and moving, per day, kept in ~/.bitch-pi/wear.json."""
    def __init__(self):
        self.path = macros.HOME / 'wear.json'
        try:
            self.days = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.days = {}
        self.saved = time.monotonic()

    def add(self, counts):
        day = self.days.setdefault(time.strftime('%Y-%m-%d'), {'powered': 0., 'moving': 0., 'travel': [0.] * 4})
        day['powered'] += counts['powered']
        day['moving'] += counts['moving']
        day['travel'] = [round(a + b, 3) for a, b in zip(day['travel'], counts['travel'])]
        counts.update(powered=0., moving=0., travel=[0.] * 4)
        if time.monotonic() - self.saved > 300:
            self.save()

    def save(self):
        self.saved = time.monotonic()
        self.days = dict(sorted(self.days.items())[-60:])
        try:
            macros.HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps(self.days))
            temporary.replace(self.path)
        except OSError:
            pass

    def today(self):
        day = self.days.get(time.strftime('%Y-%m-%d'), {'powered': 0., 'moving': 0., 'travel': [0.] * 4})
        return {'powered_minutes': round(day['powered'] / 60, 1), 'moving_minutes': round(day['moving'] / 60, 1),
                'travel': day['travel']}


class Body:
    def __init__(self, motion_enabled=True):
        self.motion_enabled = motion_enabled
        self.lock = threading.Lock()
        self.look = ('park', 0, -0.8)
        self.look_at = 0
        self.audio = shutil.which('pw-play') or shutil.which('paplay') or shutil.which('aplay')
        self.position = (0.0, REST[2])
        self.player = None
        self.ambient_until = 0.
        self.ambient_volume = 0.
        self.ambient_process = None
        self.audio_file = None
        self.sink = None
        self.audio_label = 'Checking speaker'
        self.beat = 'park'
        self.energy = self.accent = 0.
        self.gesture = (0., 0.)
        self.calm = False
        self.mood = None
        self.sfx_volume = 0.
        self.sfx_player = None
        self.wear = Wear()
        self.pose = list(REST)
        self.show = None
        self.show_result = None
        self.refresh_output()

    def refresh_output(self):
        if not self.audio:
            self.audio_label = 'No playback program'
            return
        target = os.environ.get('BITCH_PI_SPEAKER', 'JBL Flip 3')
        if target == 'default':
            self.sink, self.audio_label = '', 'Pi default output'
            return
        try:
            graph = subprocess.run(['pw-dump'], capture_output=True, timeout=1.5, check=True)
            for node in json.loads(graph.stdout):
                props = (node.get('info') or {}).get('props') or {}
                if props.get('media.class') == 'Audio/Sink' and target.casefold() in (
                        props.get('node.description', '') + ' ' + props.get('node.name', '')).casefold():
                    self.sink = props['node.name']
                    self.audio_label = props.get('node.description', target)
                    return
        except (OSError, subprocess.SubprocessError, ValueError, KeyError):
            pass
        self.sink, self.audio_label = None, target + ' disconnected'

    def keepalive(self, volume):
        with self.lock:
            self.ambient_until = time.monotonic() + 8 if volume else 0.
            self.ambient_volume = volume

    def ambient_audio(self):
        command = shutil.which('pw-cat')
        if not command:
            return
        frequency, block = 200, 1200
        phases = [math.sin(math.tau * frequency * i / 24000) for i in range(block)]
        pulse_at = time.monotonic()+90
        pulse_left = 0
        previous_sink = None
        while True:
            now = time.monotonic()
            with self.lock:
                sink, process = self.sink, self.ambient_process
                volume = self.ambient_volume if now < self.ambient_until else 0.
                speaking = self.player and self.player.poll() is None
            if process and (not volume or sink is None or sink != previous_sink or process.poll() is not None):
                self.end_ambient(process)
                process = None
            if volume and sink is not None and not process:
                try:
                    args = [command, '--playback', '--rate=24000', '--channels=1', '--format=s16', '--latency=100ms']
                    if sink:
                        args.extend(['--target', sink])
                    process = subprocess.Popen([*args, '-'], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                               stderr=subprocess.DEVNULL, bufsize=0)
                    os.set_blocking(process.stdin.fileno(), False)
                    with self.lock:
                        self.ambient_process = process
                    previous_sink = sink
                    pulse_at = now+90
                except OSError:
                    time.sleep(1)
                    continue
            if process:
                if now >= pulse_at and not speaking:
                    pulse_left = 5
                    pulse_at = now + random.uniform(90, 180)
                if speaking:
                    pulse_left = 0
                gain = volume ** 1.65
                data = struct.pack('<1200h', *(round(13*gain*sample) for sample in phases))
                if pulse_left:
                    elapsed = (5-pulse_left)*block
                    pitch = 415 if pulse_left >= 3 else 580
                    data = struct.pack('<1200h', *(round(800*gain*math.sin(math.pi*(elapsed+i)/(block*5))**2
                        * math.sin(math.tau*pitch*i/24000)) for i in range(block)))
                    pulse_left -= 1
                try:
                    os.write(process.stdin.fileno(), data)
                except BlockingIOError:
                    pass
                except (OSError, ValueError):
                    self.end_ambient(process)
            time.sleep(max(.005, .05 - (time.monotonic()-now)))

    def end_ambient(self, process):
        with self.lock:
            if self.ambient_process is process:
                self.ambient_process = None
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        process.stdin.close()

    def set_look(self, look, energy=0., accent=0., tilt=0., lean=0., calm=False, mood=None, sfx_volume=0.):
        with self.lock:
            self.energy, self.accent = energy, accent
            self.gesture = (tilt, lean)
            self.calm = calm
            self.mood = mood
            self.sfx_volume = sfx_volume
            self.look = look
            self.look_at = time.monotonic()
            return self.position

    def play_sfx(self, name, volume):
        """Play a short WALL-E creature vocalization on the Pi speaker if idle."""
        if not self.audio or self.sink is None or volume <= 0.01:
            return
        with self.lock:
            if self.show or (self.player and self.player.poll() is None):
                return
            if self.sfx_player and self.sfx_player.poll() is None:
                return
            sink = self.sink
        def run():
            try:
                import sounds
                if name not in sounds.V:
                    return
                path = sounds._render(name, random.randrange(sounds.VARIANTS))
                args = [self.audio]
                if sink:
                    args += ['--target', sink]
                if self.audio.endswith('pw-play'):
                    args += ['--volume', f'{max(0.05, min(1.0, volume)):.2f}']
                args.append(path)
                proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                with self.lock:
                    self.sfx_player = proc
            except Exception:
                pass
        threading.Thread(target=run, daemon=True).start()

    def stop(self):
        with self.lock:
            if self.show:
                self.show_result = {'id': self.show['id'], 'state': 'stopped'}
            self.show = None
            self._stop()

    def begin_show(self, item):
        name, token = item.get('name'), item.get('id')
        volume = item.get('volume', 0)
        if not isinstance(token, str) or len(token) != 32 or any(c not in '0123456789abcdef' for c in token):
            raise ValueError('invalid performance id')
        if type(volume) not in (float, int) or not math.isfinite(volume) or not 0 <= volume <= 1:
            raise ValueError('invalid performance volume')
        if name == 'routine':
            score = stage.routine(item.get('steps'))
        elif isinstance(name, str) and name in stage.SHOWS:
            score = None
        else:
            raise ValueError('unknown performance')
        with self.lock:
            if not self.motion_enabled:
                return {'state': 'unavailable', 'reason': 'Motion is off on the Pi.'}
            if self.show_result and self.show_result['id'] == token:
                return {'state': 'queued', 'id': token}
            if self.show:
                return {'state': 'unavailable', 'reason': 'A performance is already running.'}
            if volume and name in stage.SHOWS and self.sink is None:
                return {'state': 'unavailable', 'reason': 'The Pi speaker is disconnected.'}
            active = {'id': token, 'name': name, 'start': None}
            self.show = active
            self.show_result = {'id': token, 'state': 'preparing'}
        threading.Thread(target=self.prepare_show, args=(active, score, volume), daemon=True).start()
        return {'state': 'queued', 'id': token}

    def prepare_show(self, active, score, volume):
        track = None
        try:
            score = score or stage.legacy_show(active['name'])
            lead, duration = .43, score.duration
            with self.lock:
                sink = self.sink
                if not self.ambient_process or self.ambient_process.poll() is not None:
                    lead = 1.65
            if volume and sink is not None and (score.song or score.cues):
                track = tempfile.NamedTemporaryFile(suffix='.wav')
                with stage.render_lock:
                    duration = stage.write_soundtrack(score, track, volume, lead)
                track.flush()
            with self.lock:
                if self.show is not active:
                    return
                self._stop()
                if track:
                    args = [self.audio, '--target', sink] if sink else [self.audio]
                    self.player = subprocess.Popen([*args, track.name], stdout=subprocess.DEVNULL,
                                                   stderr=subprocess.DEVNULL)
                    self.audio_file, track = track, None
                now = time.monotonic()
                active.update(score=score, start=now+lead if self.player else now, duration=duration)
                self.show_result = {'id': active['id'], 'state': 'playing'}
        except (OSError, ValueError, TypeError, wave.Error) as error:
            print(f'Unable to prepare performance: {error}', flush=True)
            with self.lock:
                if self.show is active:
                    self.show_result = {'id': active['id'], 'state': 'failed'}
                    self.show = None
        finally:
            if track:
                track.close()

    def _stop(self):
        if self.player and self.player.poll() is None:
            self.player.terminate()
            try:
                self.player.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                self.player.kill()
                self.player.wait()
        if self.audio_file:
            self.audio_file.close()
        self.player = self.audio_file = None

    def speak(self, data):
        with wave.open(io.BytesIO(data), 'rb') as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 24000):
                raise ValueError('expected a 24 kHz mono PCM WAV')
            if not 0 < audio.getnframes() <= 70 * 24000 or len(audio.readframes(audio.getnframes())) != 2 * audio.getnframes():
                raise ValueError('invalid WAV length')
        if not self.audio or self.sink is None:
            return False
        with self.lock:
            if self.show:
                return False
            self._stop()
            file = tempfile.NamedTemporaryFile(suffix='.wav')
            try:
                file.write(data)
                file.flush()
                args = [self.audio, '--target', self.sink] if self.sink else [self.audio]
                self.player = subprocess.Popen([*args, file.name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.audio_file = file
                try:
                    status = self.player.wait(timeout=0.09)
                except subprocess.TimeoutExpired:
                    return True
                self._stop()
                return status == 0
            except OSError:
                file.close()
                return False

    def speak_stream(self, chunks):
        """Play 24 kHz mono 16-bit PCM as it arrives from the Mac, so the first words start before the reply is
        finished. Uses the same pw-cat pipe as the speaker keepalive."""
        command = shutil.which('pw-cat')
        if not command or self.sink is None:
            for _ in chunks:   # drain, so the connection ends cleanly
                pass
            return False
        with self.lock:
            if self.show:
                for _ in chunks:
                    pass
                return False
            self._stop()
            args = [command, '--playback', '--rate=24000', '--channels=1', '--format=s16', '--latency=60ms']
            if self.sink:
                args.extend(['--target', self.sink])
            player = self.player = subprocess.Popen([*args, '-'], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                                    stderr=subprocess.DEVNULL)
        alive = True
        for data in chunks:
            if not alive:
                continue   # hushed: keep reading so the Mac's request finishes
            try:
                player.stdin.write(data)
                player.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                alive = False
        try:
            player.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass
        return True

    def run(self):
        motion = None
        last = time.monotonic()
        try:
            while True:
                now = time.monotonic()
                with self.lock:
                    energy, accent = self.energy, self.accent
                    gesture = self.gesture if now - self.look_at < .3 else (0., 0.)
                    calm = self.calm
                    mood = self.mood
                    sfx_volume = self.sfx_volume
                    look = self.look if now - self.look_at < 3 else ('park', 0, -0.8)
                    if self.player and self.player.poll() is not None:
                        if self.show and self.player.returncode:
                            self.show_result = {'id': self.show['id'], 'state': 'failed'}
                            self.show = None
                        self._stop()
                    current = self.show
                    frame = None
                    if current and current['start'] is not None:
                        elapsed = now - current['start']
                        score = current['score']
                        if elapsed >= current['duration']:
                            self.show_result = {'id': current['id'], 'state': 'finished'}
                            self.show = None
                            self._stop()
                        else:
                            frame = score.at(min(elapsed, score.duration)) if elapsed >= 0 else (REST, .65, .25)
                if self.motion_enabled and motion is None and (frame is not None or look[0] != 'park'):
                    import desk

                    motion = Motion(desk, on_sfx=self.play_sfx)
                if motion:
                    motion.step(*look, now, min(0.05, now - last), energy, accent, frame, gesture, calm, mood, sfx_volume)
                    self.wear.add(motion.wear)
                    with self.lock:
                        self.position = (motion.desk.cur[0], motion.desk.cur[2])
                        self.beat = motion.beat
                        self.pose = motion.desk.cur[:]
                last = now
                time.sleep(max(0., .02 - (time.monotonic() - now)))
        finally:
            self.wear.save()
            self.stop()
            self.keepalive(0)
            if motion and motion.started:
                motion.desk.relax()


def listen(motion_enabled=True):
    body, key = Body(motion_enabled), connection_key()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(5)
            super().setup()

        def log_message(self, *_):
            pass

        def respond(self, status, result):
            data = json.dumps(result).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def chunks(self, limit):
            """The pieces of a chunked request body as they arrive."""
            total = 0
            while True:
                size = int(self.rfile.readline(64).split(b';')[0].strip() or b'0', 16)
                if size == 0:
                    self.rfile.readline(64)
                    return
                total += size
                if total > limit:
                    raise ValueError('stream too long')
                data = self.rfile.read(size)
                self.rfile.readline(8)
                yield data

        def authorized(self):
            if hmac.compare_digest(self.headers.get('Authorization', '').encode(), ('Bearer ' + key).encode()):
                return True
            self.respond(401, {'error': 'pair the Pi first'})
            return False

        def local_inbox(self):
            return self.client_address[0] == '127.0.0.1' and self.headers.get('Host') in (
                f'localhost:{PORT}', f'127.0.0.1:{PORT}')

        def inbox(self):
            data = macros.inbox_html().encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == '/inbox':
                if self.local_inbox():
                    self.inbox()
                else:
                    self.send_error(403)
                return
            if self.authorized():
                url = urlsplit(self.path)
                if url.path == '/macro/status':
                    try:
                        request_id = parse_qs(url.query).get('id', [''])[0]
                        self.respond(200, macros.handoff_status(request_id))
                    except (ValueError, FileNotFoundError):
                        self.respond(404, {'error': 'no such request'})
                    return
                if self.path == '/ping':
                    body.refresh_output()
                    ambient = body.ambient_process
                    self.respond(200, {'audio': body.sink is not None, 'audio_route': body.audio_label,
                                       'playback_monitor': True, 'speak_stream': bool(shutil.which('pw-cat')), 'character': True, 'performance': 3, 'joint_attention': True, 'audio_keepalive': bool(shutil.which('pw-cat')),
                                       'audio_warm': bool(ambient and ambient.poll() is None),
                                       'handoff': 3, 'shows': 1, 'show_active': bool(body.show),
                                       'motion': body.motion_enabled, 'gestures': 1, 'rhythm': 1, 'pixar': 1,
                                       'wear': body.wear.today(), 'protocol': 1})
                elif self.path == '/show':
                    with body.lock:
                        self.respond(200, body.show_result or {'state': 'idle'})
                elif self.path == '/playback':
                    with body.lock:
                        active = bool(body.player and body.player.poll() is None)
                    self.respond(200, {'playing': active})
                else:
                    self.respond(404, {'error': 'unknown command'})

        def do_POST(self):
            if self.path == '/inbox':
                origin = 'http://' + self.headers.get('Host', '')
                if not self.local_inbox() or self.headers.get('Origin') != origin:
                    self.send_error(403)
                    return
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 150000:
                        raise ValueError('invalid form length')
                    fields = parse_qs(self.rfile.read(length).decode(), max_num_fields=5)
                    macros.update_handoff(fields['id'][0], fields['action'][0], fields.get('answer', [''])[0])
                except (ValueError, KeyError, OSError) as error:
                    self.send_error(400, str(error))
                    return
                self.send_response(303)
                self.send_header('Location', '/inbox')
                self.end_headers()
                return
            if not self.authorized():
                return
            if self.path == '/speak_stream':
                if self.headers.get('Transfer-Encoding', '').casefold() != 'chunked':
                    self.respond(400, {'error': 'expected a chunked stream'})
                    return
                try:
                    played = body.speak_stream(self.chunks(limit=80 * 24000 * 2))
                except (ValueError, OSError) as error:
                    self.respond(400, {'error': str(error)})
                    return
                self.respond(200 if played else 503, {'ok': played})
                return
            if self.path not in ('/look', '/speak', '/stop', '/macro', '/keepalive', '/show'):
                self.respond(404, {'error': 'unknown command'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                limit = 3_400_000 if self.path == '/speak' else 6000
                if not 0 < length <= limit:
                    raise ValueError('invalid body length')
                data = self.rfile.read(length)
                if self.path == '/speak':
                    if not body.speak(data):
                        self.respond(503, {'error': 'playback unavailable'})
                        return
                else:
                    item = json.loads(data)
                    if not isinstance(item, dict):
                        raise ValueError('expected an object')
                    if self.path == '/look':
                        look = command(item)
                        if not body.motion_enabled and look[0] != 'park':
                            self.respond(409, {'error': 'Motion disabled on the Pi'})
                        else:
                            energy = float(item.get('energy', 0))
                            if not math.isfinite(energy) or not 0 <= energy <= 1:
                                raise ValueError('invalid speech level')
                            accent = float(item.get('accent', 0))
                            if not math.isfinite(accent) or not -.85 <= accent <= .85:
                                raise ValueError('invalid speech accent')
                            # Word-timed gestures from the Mac: a head tilt or shake and a lean in or back.
                            tilt, lean = float(item.get('tilt', 0)), float(item.get('lean', 0))
                            if not (math.isfinite(tilt) and math.isfinite(lean)) or abs(tilt) > .85 or abs(lean) > .50:
                                raise ValueError('invalid gesture')
                            raw_mood = item.get('mood')
                            mood = None
                            if isinstance(raw_mood, dict):
                                mood = {}
                                for key in ('valence', 'arousal', 'loneliness', 'annoyance'):
                                    if key in raw_mood:
                                        val = float(raw_mood[key])
                                        if math.isfinite(val):
                                            mood[key] = max(-1.0, min(1.0, val))
                            sfx_volume = float(item.get('sfx_volume', 0))
                            if not math.isfinite(sfx_volume) or not 0 <= sfx_volume <= 1:
                                sfx_volume = 0.
                            base, neck = body.set_look(look, energy, accent, tilt, lean, item.get('calm') is True, mood, sfx_volume)
                            if isinstance(item.get('sfx'), str) and sfx_volume > 0:
                                body.play_sfx(item['sfx'], sfx_volume)
                            self.respond(200, {'ok': True, 'base': base, 'neck': neck,
                                               'beat': body.beat, 'pose': body.pose})
                        return
                    elif self.path == '/keepalive':
                        volume = float(item.get('volume', 0))
                        if not math.isfinite(volume) or not 0 <= volume <= 1:
                            raise ValueError('invalid keepalive volume')
                        body.keepalive(volume)
                    elif self.path == '/stop':
                        body.stop()
                    elif self.path == '/show':
                        self.respond(200, body.begin_show(item))
                        return
                    else:
                        macros.enqueue(item)
                self.respond(200, {'ok': True})
            except (ValueError, TypeError, EOFError, wave.Error) as error:
                self.respond(400, {'error': str(error)})
            except OSError:
                self.respond(503, {'error': 'Pi action unavailable'})

    with ThreadingHTTPServer(('0.0.0.0', PORT), Handler) as server:
        print(f'BITCH Pi ready on port {PORT}. Run python3 brain.py --key to pair it.', flush=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        threading.Thread(target=body.ambient_audio, daemon=True).start()
        threading.Thread(target=macros.send_worker, daemon=True).start()
        try:
            body.run()
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--key', action='store_true', help='print the Pi connection key')
    parser.add_argument('--no-motion', action='store_true', help='run audio and desktop handoff with motion disabled')
    args = parser.parse_args()
    if args.key:
        print(connection_key())
    else:
        def terminate(*_):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, terminate)
        listen(motion_enabled=not args.no_motion)
