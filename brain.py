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

import macros
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
    'speak-farewell': ((0, 0, .13, .08, .2), (.27, 0, .24, -.09, -.27),
                       (.66, 0, .14, .12, .23), (1.12, .055, -.08, .015, .15),
                       (1.65, .11, -.10, -.085, -.11)),
    'mirror': ((0, 0, -.06, -.02, -.10), (.19, 0, .055, .045, .44),
               (.55, 0, .19, .075, .48), (1.48, 0, .11, .05, .30), (1.9, 0, 0, 0, 0)),
}
BEATS['recognize'] = BEATS['happy']
SPEECH = {name for name in BEATS if name == 'speaking' or name.startswith('speak-')}
SOCIAL = {'notice', 'attend', 'listen', 'ponder', 'speaking', 'recognize', 'mirror-left', 'mirror-right',
          'happy', 'sad', 'scared', 'proud', 'double-take', 'seek', 'look-target'} | SPEECH
IDLE_BEATS = ('curious', 'glance', 'shy', 'stretch', 'peek')
ROOM_BEATS = ('survey', 'stretch', 'glance', 'sad', 'peek')
SOFTNESS = {'sad': .44, 'shy': .82, 'curious': .82, 'ponder': .82, 'scared': 2.1,
            'happy': 1.85, 'recognize': 1.85, 'notice': 1.8, 'double-take': 1.88,
            'speak-gentle': .82, 'speak-farewell': 1.15, 'speak-upbeat': 2.1,
            'speak-emphatic': 1.85, 'speak-playful': 1.78, 'speaking': 1.58}
# Separate quick head accents from the heavier arm's slower weight transfer.
SPEECH_WEIGHT = {'speak-gentle': .68, 'speak-upbeat': 1.2, 'speak-emphatic': 1.16,
                 'speak-playful': 1.12, 'speak-farewell': .8}


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
    def __init__(self, desk):
        self.desk = desk
        self.started = False
        self.rng = random.Random()
        self.idle_beat = None
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

    def step(self, mode, base, neck, now, dt, energy=0.0, accent=0.0):
        if not self.started:
            if mode == 'park':
                return
            self.start()
        desk = self.desk
        if self.actor is None:
            self.actor = Actor(wake=False, desk=desk, powered=True)
        actor = self.actor
        actor.low = (-BASE, -1.06, NECK[0], -.64)
        actor.high = (BASE, -.42, NECK[1], .64)
        actor.speed = (.58 * min(1., max(0., desk.BASE_SPEED)), .68, .85, 1.65)
        actor.accel = (1.55, 2.1, 3.5, 7.4)
        following = mode == 'watch' or mode in SOCIAL
        changed = mode != self.mode
        if changed and (mode == 'look-target' or self.mode == 'look-target'):
            self.gaze_anchor = None
        if changed:
            if mode in SPEECH or mode in ('ponder', 'seek', 'double-take', 'look-target'):
                self.gesture_side = (-1 if base > .36 else 1 if base < -.36 else -self.gesture_side)
            self.idle_beat = None
            self.social_at = now
            self.next_beat = now + self.rng.uniform(.85, 2.1)
            if mode == 'scan':
                self.scan_base = desk.cur[0]
            self.mode = mode
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
        direction = 1
        if mode in SOCIAL:
            self.beat = 'mirror' if mode.startswith('mirror-') else mode
            direction = -1 if mode == 'mirror-left' else 1
            if mode in SPEECH or mode in ('ponder', 'seek', 'double-take', 'look-target'):
                direction = self.gesture_side
            frames, age = BEATS[self.beat], now - self.social_at
        elif mode in ('watch', 'scan'):
            if self.idle_beat and self.idle_anchor and (abs(base-self.idle_anchor[0]) > .09
                    or abs(neck-self.idle_anchor[1]) > .10):
                self.idle_beat = None
                self.next_beat = now + 1.35
            if mode == 'watch' and not self.idle_beat and abs(base-desk.cur[0]) > .08:
                self.next_beat = max(self.next_beat, now+1.65)
            if now >= self.next_beat:
                choices = IDLE_BEATS if mode == 'watch' else ROOM_BEATS
                self.last_beat = self.rng.choice([name for name in choices if name not in self.recent_idle])
                self.recent_idle = (self.recent_idle + [self.last_beat])[-2:]
                side = -1 if base > .30 else 1 if base < -.30 else self.rng.choice((-1, 1))
                self.idle_beat = (BEATS[self.last_beat], now, side)
                self.idle_anchor = base, neck
                self.next_beat = now + BEATS[self.last_beat][-1][0] + self.rng.uniform(.70, 2.25)
            if self.idle_beat:
                frames, start, direction = self.idle_beat
                age = now - start
                self.beat = self.last_beat or mode
                if age >= frames[-1][0] + .35:
                    self.idle_beat = frames = None

        turn, lean, pitch, tilt = pose_at(frames, age) if frames else (0.,)*4
        arm = REST[1] + lean
        base, neck, tilt = base + direction*turn, neck + pitch, direction*tilt
        if mode in SPEECH:
            weight = SPEECH_WEIGHT.get(mode, 1.)
            lean_response = 1 - math.exp(-dt / (.24 if energy > self.lean else .38))
            self.lean += (energy - self.lean) * lean_response
            neck += accent * weight + .035 * energy
            arm += weight * (.15 * self.lean - .17 * accent)
            tilt += direction * weight * (.11 * energy + .92 * accent)
            base -= direction * .038 * self.lean
        elif mode == 'listen':
            # Stay inclined through a pause. The head responds more than the arm.
            self.lean += (energy - self.lean) * (1 - math.exp(-dt / .35))
            neck += .035 * self.lean
            tilt += .08 * self.lean
        else:
            self.lean = 0.
            if energy > 0 and following:
                neck += .13 * energy
                arm += .085 * energy
                tilt -= .16 * energy
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
                base += side * min(.055, abs(self.throw)*.15)
            if elapsed >= 1.25:
                self.cue_at = None
        actor.idle_scale = (.18, 1., .9, .72) if following else (1.,)*4
        actor.idle_amt = (.52 if following else .85) if mode not in ('aim', 'park') else 0
        if self.cue_at is not None and now-self.cue_at < .14:
            actor.idle_amt = 0
        stiff = SOFTNESS.get(self.beat, 1.12)
        if self.restoring:
            if abs(REST[1] - desk.cur[1]) > .02:
                base, arm, neck, tilt = desk.cur[0], REST[1], desk.cur[2], desk.cur[3]
                actor.idle_amt = 0
            else:
                self.restoring = False
        actor.to(base, arm, neck, tilt, stiff=stiff)
        actor.step(min(.02, dt))
        if mode == 'park' and max(abs(a-b) for a,b in zip(REST, desk.cur)) < .008 and max(map(abs, actor.v)) < .025:
            desk.relax()
            self.started = False


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
        self.pose = list(REST)
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

    def set_look(self, look, energy=0., accent=0.):
        with self.lock:
            self.energy, self.accent = energy, accent
            self.look = look
            self.look_at = time.monotonic()
            return self.position

    def stop(self):
        with self.lock:
            self._stop()

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

    def run(self):
        motion = None
        last = time.monotonic()
        try:
            while True:
                now = time.monotonic()
                with self.lock:
                    energy, accent = self.energy, self.accent
                    look = self.look if now - self.look_at < 3 else ('park', 0, -0.8)
                    if self.player and self.player.poll() is not None:
                        self._stop()
                if self.motion_enabled and motion is None and look[0] != 'park':
                    import desk

                    motion = Motion(desk)
                if motion:
                    motion.step(*look, now, min(0.05, now - last), energy, accent)
                    with self.lock:
                        self.position = (motion.desk.cur[0], motion.desk.cur[2])
                        self.beat = motion.beat
                        self.pose = motion.desk.cur[:]
                last = now
                time.sleep(max(0., .02 - (time.monotonic() - now)))
        finally:
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

        def authorized(self):
            if hmac.compare_digest(self.headers.get('Authorization', '').encode(), ('Bearer ' + key).encode()):
                return True
            self.respond(401, {'error': 'pair the Pi first'})
            return False

        def do_GET(self):
            if self.authorized():
                if self.path == '/ping':
                    body.refresh_output()
                    ambient = body.ambient_process
                    self.respond(200, {'audio': body.sink is not None, 'audio_route': body.audio_label,
                                       'playback_monitor': True, 'character': True, 'performance': 2, 'joint_attention': True, 'audio_keepalive': bool(shutil.which('pw-cat')),
                                       'audio_warm': bool(ambient and ambient.poll() is None),
                                       'motion': body.motion_enabled, 'protocol': 1})
                elif self.path == '/playback':
                    with body.lock:
                        active = bool(body.player and body.player.poll() is None)
                    self.respond(200, {'playing': active})
                else:
                    self.respond(404, {'error': 'unknown command'})

        def do_POST(self):
            if not self.authorized():
                return
            if self.path not in ('/look', '/speak', '/stop', '/macro', '/keepalive'):
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
                            if not math.isfinite(accent) or not -.35 <= accent <= .35:
                                raise ValueError('invalid speech accent')
                            base, neck = body.set_look(look, energy, accent)
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
