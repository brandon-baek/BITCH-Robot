"""Does BITCH's body say what its words say? Play spoken lines through the real gesture code on the 3D model.

Each line is turned into word timings and gestures exactly as the voice does it (voice.gestures and gesture_at),
the eyes look at the listener and glance at anyone named who is in view (as core/sight.py does), and the Pi's motion
engine drives the MuJoCo servos. Then each gesture is checked on the simulated body: a nod really dips the head,
a "no" really shakes it, a question really tilts it, and naming someone really turns BITCH toward them.

    ~/dc-venv/bin/python -m sim.speech_motion --out /tmp/bitch-speech
"""
import argparse
import math
from pathlib import Path
import random
import sys
from types import SimpleNamespace
from unittest.mock import patch

import mujoco
import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
for path in (ROOT, ROOT / 'testing' / 'fakes'):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import brain as pi   # noqa: E402
from core import voice   # noqa: E402
from core.sight import aim_at   # noqa: E402
from sim import params as P   # noqa: E402
from sim.model import mjcf   # noqa: E402

LISTENER = (.5, .42)                  # the person BITCH is talking with, center of the frame
ROOM = {'emma': (.18, .45), 'dad': (.84, .40)}   # others in view
WORDS_PER_SECOND = 2.7
LINES = [
    ('yes', "Yes, exactly. That's the one.", 'playful'),
    ('no', "No, that's not how it works.", 'emphatic'),
    ('question', 'Wait, you did all of that today?', 'upbeat'),
    ('glance', "Ha. Tell Emma she owes me a rematch.", 'playful'),
    ('list', 'First, preheat the oven. Then mix the dry stuff. Finally, bake it.', 'neutral'),
    ('you', "Honestly, you nailed it.", 'warm'),
]


PRE_ROLL = 1.6   # BITCH is already facing the listener, attentive, when it starts to answer


def word_times(text):
    tokens = __import__('re').findall(r"[\w'’-]+|[.,!?;:]", text)
    words, at = [], PRE_ROLL
    for token in tokens:
        words.append((token, at))
        at += .28 if token in '.!?' else .14 if token in ',;:' else 1 / WORDS_PER_SECOND
    return words, at + .6


def simulate(model, name, text, delivery):
    data = mujoco.MjData(model)
    mujoco.mj_resetDataKeyframe(model, data, model.key('rest').id)
    mujoco.mj_forward(model, data)
    desk = SimpleNamespace(cur=list(pi.REST), on=[True] * 4, BASE_SPEED=1.0, REST=pi.REST,
                           S=[SimpleNamespace(us=lambda _u: None) for _ in range(4)], relax=lambda *js: None,
                           _load=lambda: list(pi.REST), power_all=lambda: None, move=lambda *a, **k: None)
    motion = pi.Motion(desk, on_sfx=lambda *a: None)
    motion.rng = random.Random(3)
    motion.started = True
    words, duration = word_times(text)
    with patch.object(voice, 'known_names', return_value=set(ROOM)):
        events = voice.gestures(words)
    dt, substeps = .02, max(1, round(.02 / model.opt.timestep))
    renderer = mujoco.Renderer(model, 300, 380)
    camera = 'three_quarter'
    marks = sorted({round(at + length * .5, 2) for kind, at, length, _ in events} | {PRE_ROLL - .2})[:5] + [duration - .1]
    frames, rows = [], []
    for step in range(int(duration / dt)):
        now = step * dt
        nod, tilt, lean = voice.gesture_at(events, now)
        named = next((who for kind, at, length, who in events if kind == 'glance' and 0 <= now - at < length), None)
        base, neck = aim_at(*(ROOM[named] if named else LISTENER), None)
        energy = .55 + .3 * math.sin(now * 8) ** 2
        speaking = now >= PRE_ROLL - .1
        if not speaking:
            energy = 0.
        motion.step('speaking' if speaking else 'attend', base, neck + nod * voice.NOD_GAIN, now, dt, energy=energy, accent=0.,
                    gesture=(tilt, lean), mood={'valence': .5, 'arousal': .55, 'loneliness': 0, 'annoyance': 0})
        rad = np.asarray(P.to_rad(desk.cur), float)
        data.ctrl[:] = np.clip(rad, model.actuator_ctrlrange[:, 0], model.actuator_ctrlrange[:, 1])
        mujoco.mj_step(model, data, substeps)
        forward = data.site('eyes').xmat.reshape(3, 3)[:, 0]
        rows.append((now, *P.to_units(data.qpos[:4]), math.degrees(math.asin(max(-1, min(1, float(forward[2]))))),
                     math.degrees(math.atan2(float(forward[1]), float(forward[0])))))
        if any(abs(now - mark) < dt / 2 for mark in marks):
            renderer.update_scene(data, camera=camera)
            image = Image.fromarray(renderer.render())
            draw = ImageDraw.Draw(image)
            spoken = ' '.join(w for w, at in words if at <= now)[-34:]
            draw.rectangle([0, 0, image.width, 34], fill=(20, 22, 26))
            draw.text((6, 4), f'{name}  t={now:.2f}s', fill=(245, 245, 240))
            draw.text((6, 18), spoken, fill=(200, 196, 180))
            frames.append(image)
    renderer.close()
    return events, np.asarray(rows), frames


def window(rows, start, end):
    return rows[(rows[:, 0] >= start) & (rows[:, 0] < end)]


def check(name, events, rows):
    """Every gesture the words call for must show on the body, big enough to read across a room."""
    results = []
    before = window(rows, PRE_ROLL - .4, PRE_ROLL)
    rest_pitch, rest_heading = before[:, 5].mean(), before[:, 6].mean()
    for kind, at, length, strength in events:
        part = window(rows, at, at + length + .35)
        if not len(part):
            continue
        if kind == 'nod':
            dip = rest_pitch - part[:, 5].min()
            results.append((kind, at, dip, dip >= 1.5 * (strength if strength < 1 else 1), f'head dips {dip:.1f}°'))
        elif kind == 'shake':
            swing = np.ptp(part[:, 4]) * P.DEG_PER_UNIT[3]
            results.append((kind, at, swing, swing >= 6, f'head swings {swing:.1f}°'))
        elif kind == 'tilt':
            swing = np.abs(part[:, 4] - before[:, 4].mean()).max() * P.DEG_PER_UNIT[3]
            results.append((kind, at, swing, swing >= 4, f'head tilts {swing:.1f}°'))
        elif kind == 'lean':
            reach = np.ptp(part[:, 2]) * P.DEG_PER_UNIT[1]
            results.append((kind, at, reach, reach >= 1.5, f'arm moves {reach:.1f}°'))
        elif kind == 'glance':
            turned = (part[:, 6] - rest_heading)
            toward = turned.min() if ROOM[strength][0] < .5 else turned.max()
            back = window(rows, at + length + .6, at + length + 1.4)
            returned = len(back) == 0 or abs(back[:, 6].mean() - rest_heading) < abs(toward) * .5
            results.append((kind, at, toward, abs(toward) >= 10 and returned,
                            f'turns {toward:+.1f}° toward {strength}, {"comes back" if returned else "stays"}'))
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', default='/tmp/bitch-speech')
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    model = mujoco.MjModel.from_xml_string(mjcf())
    strips, failures = [], 0
    for name, text, delivery in LINES:
        events, rows, frames = simulate(model, name, text, delivery)
        print(f'\n{name}: "{text}"')
        for kind, at, _, ok, detail in check(name, events, rows):
            failures += not ok
            print(f"  {'ok ' if ok else 'BAD'} {kind:6s} at {at:4.2f}s  {detail}")
        strips.append(frames)
    width, height = strips[0][0].size
    columns = max(len(s) for s in strips)
    sheet = Image.new('RGB', (width * columns, height * len(strips)), (18, 20, 24))
    for row, frames in enumerate(strips):
        for column, image in enumerate(frames):
            sheet.paste(image, (column * width, row * height))
    sheet.save(out / 'speech_motion.png')
    print(f'\n{failures} gestures did not read on the body. Sheet: {out / "speech_motion.png"}')
    return failures


if __name__ == '__main__':
    sys.exit(1 if main() else 0)
