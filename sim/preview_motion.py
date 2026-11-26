"""Simulate brain.Motion + character.py on BITCH's 3D MuJoCo physics model and render visual filmstrips.

Usage:
    ~/dc-venv/bin/python -m sim.preview_motion --out /tmp/bitch_previews
"""
import argparse
import math
import os
from pathlib import Path
import random
import sys
from types import SimpleNamespace

import mujoco
import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / 'testing' / 'fakes') not in sys.path:
    sys.path.insert(0, str(ROOT / 'testing' / 'fakes'))

import brain as pi
from sim import params as P
from sim.model import mjcf


SCENARIOS = [
    # (name, mode, duration_s, base, neck, mood, speech_profile, camera)
    ('curious', 'curious', 2.4, 0.0, -0.8, {'valence': 0.35, 'arousal': 0.55, 'loneliness': 0.0, 'annoyance': 0.0}, None, 'three_quarter'),
    ('curious_side', 'curious', 2.4, 0.0, -0.8, {'valence': 0.35, 'arousal': 0.55, 'loneliness': 0.0, 'annoyance': 0.0}, None, 'side'),
    ('happy', 'happy', 1.8, 0.0, -0.8, {'valence': 0.85, 'arousal': 0.80, 'loneliness': 0.0, 'annoyance': 0.0}, None, 'three_quarter'),
    ('victory', 'victory', 2.5, 0.0, -0.8, {'valence': 0.95, 'arousal': 0.95, 'loneliness': 0.0, 'annoyance': 0.0}, None, 'three_quarter'),
    ('double_take', 'double-take', 1.7, 0.25, -0.75, {'valence': 0.2, 'arousal': 0.75, 'loneliness': 0.0, 'annoyance': 0.0}, None, 'three_quarter'),
    ('scared', 'scared', 2.0, 0.0, -0.8, {'valence': -0.3, 'arousal': 0.9, 'loneliness': 0.0, 'annoyance': 0.0}, None, 'side'),
    ('proud', 'proud', 2.3, 0.0, -0.8, {'valence': 0.7, 'arousal': 0.55, 'loneliness': 0.0, 'annoyance': 0.0}, None, 'side'),
    ('lonely', 'lonely', 3.4, 0.0, -0.8, {'valence': -0.5, 'arousal': 0.15, 'loneliness': 0.9, 'annoyance': 0.0}, None, 'side'),
    ('grumpy', 'grumpy', 2.2, 0.0, -0.8, {'valence': -0.25, 'arousal': 0.65, 'loneliness': 0.0, 'annoyance': 0.85}, None, 'three_quarter'),
    ('stretch', 'stretch', 3.0, 0.0, -0.8, {'valence': 0.3, 'arousal': 0.45, 'loneliness': 0.0, 'annoyance': 0.0}, None, 'side'),
    ('peek', 'peek', 2.7, 0.0, -0.8, {'valence': 0.4, 'arousal': 0.6, 'loneliness': 0.0, 'annoyance': 0.0}, None, 'three_quarter'),
    ('ponder', 'ponder', 2.8, 0.0, -0.8, {'valence': 0.1, 'arousal': 0.4, 'loneliness': 0.0, 'annoyance': 0.0}, None, 'three_quarter'),
    ('survey', 'scan', 3.6, 0.0, -0.8, {'valence': 0.2, 'arousal': 0.5, 'loneliness': 0.0, 'annoyance': 0.0}, None, 'three_quarter'),
    ('alarm', 'alarm', 2.4, 0.0, -0.8, {'valence': 0.3, 'arousal': 1.0, 'loneliness': 0.0, 'annoyance': 0.0}, None, 'three_quarter'),
    ('doze', 'doze', 5.2, 0.0, -0.8, {'valence': 0.0, 'arousal': 0.05, 'loneliness': 0.0, 'annoyance': 0.0}, None, 'side'),
    ('speak_upbeat', 'speak-upbeat', 2.6, 0.0, -0.8, {'valence': 0.75, 'arousal': 0.75, 'loneliness': 0.0, 'annoyance': 0.0}, 'excited', 'three_quarter'),
    ('speak_playful', 'speak-playful', 2.6, 0.0, -0.8, {'valence': 0.65, 'arousal': 0.65, 'loneliness': 0.0, 'annoyance': 0.0}, 'playful', 'three_quarter'),
    ('listen', 'listen', 2.2, 0.0, -0.8, {'valence': 0.35, 'arousal': 0.55, 'loneliness': 0.0, 'annoyance': 0.0}, 'listening', 'side'),
]


def simulate_scenario(model, name, mode, duration, base, neck, mood, speech_profile, camera, seed=7):
    data = mujoco.MjData(model)
    mujoco.mj_resetDataKeyframe(model, data, model.key('rest').id)
    mujoco.mj_forward(model, data)

    sfx_events = []
    desk = SimpleNamespace(
        cur=list(pi.REST), on=[True] * 4, BASE_SPEED=1.0, REST=pi.REST,
        S=[SimpleNamespace(us=lambda _u: None) for _ in range(4)],
        relax=lambda *js: None,
        _load=lambda: list(pi.REST),
        power_all=lambda: None,
        move=lambda *a, **k: None,
    )
    motion = pi.Motion(desk, on_sfx=lambda s, v: sfx_events.append((round(now, 2), s, round(v, 2))))
    motion.rng = random.Random(seed)
    motion.started = True
    if mode == 'scan':
        motion.mode = 'scan'
        motion.next_beat = 0.0

    dt = 0.02
    substeps = max(1, round(dt / model.opt.timestep))
    steps = int(duration / dt)
    capture_at = np.linspace(0, steps - 1, 6, dtype=int)
    capture_set = set(capture_at.tolist())

    renderer = mujoco.Renderer(model, 320, 420)
    # Slightly widen inspection FOV so full upright/jumping poses stay 100% framed in contact sheets.
    cam_id = model.camera(camera).id
    orig_fovy = float(model.cam_fovy[cam_id])
    model.cam_fovy[cam_id] = max(orig_fovy, 46.0)
    frames = []
    q_hist = []
    eye_hist = []
    pitch_hist = []
    max_contacts = 0
    max_torque_pct = np.zeros(4)
    min_eye_z = 999.0

    for step_idx in range(steps):
        now = step_idx * dt
        energy = accent = 0.0
        gesture = (0.0, 0.0)
        if speech_profile == 'excited':
            energy = 0.55 + 0.4 * math.sin(now * 9.0) ** 2
            accent = 0.38 * math.sin(now * 6.5)
            gesture = (0.35 * math.sin(now * 3.8), 0.18 * math.sin(now * 2.6))
        elif speech_profile == 'playful':
            energy = 0.45 + 0.35 * math.sin(now * 7.5) ** 2
            accent = 0.30 * math.sin(now * 5.2)
            gesture = (0.40 * math.sin(now * 3.1), 0.15 * math.cos(now * 2.4))
        elif speech_profile == 'listening':
            energy = 0.4 + 0.35 * math.sin(now * 4.0) ** 2

        motion.step(mode, base, neck, now, dt, energy=energy, accent=accent, gesture=gesture,
                    mood=mood, sfx_volume=0.8)
        # Drive the REAL MuJoCo servos with desk.cur (in radians)!
        rad = np.asarray(P.to_rad(desk.cur), float)
        lo, hi = model.actuator_ctrlrange[:, 0], model.actuator_ctrlrange[:, 1]
        data.ctrl[:] = np.clip(rad, lo, hi)
        mujoco.mj_step(model, data, substeps)

        q_units = np.asarray(P.to_units(data.qpos[:4]), float)
        q_hist.append(q_units)
        eye_mm = data.site('eyes').xpos * 1000.0
        eye_hist.append(eye_mm)
        min_eye_z = min(min_eye_z, float(eye_mm[2] - P.BOX_HEIGHT))
        fwd = data.site('eyes').xmat.reshape(3, 3)[:, 0]
        pitch_hist.append(math.degrees(math.asin(max(-1.0, min(1.0, float(fwd[2]))))))
        max_contacts = max(max_contacts, int(data.ncon))
        trq = np.abs(data.actuator_force[:4]) / P.SERVO_STALL_TORQUE * 100.0
        max_torque_pct = np.maximum(max_torque_pct, trq)

        if step_idx in capture_set:
            renderer.update_scene(data, camera=camera)
            img = Image.fromarray(renderer.render())
            draw = ImageDraw.Draw(img)
            label = f'{name}  t={now:.2f}s  b={q_units[0]:+.2f} a={q_units[1]:+.2f} n={q_units[2]:+.2f} h={q_units[3]:+.2f}'
            draw.rectangle([0, 0, img.width, 22], fill=(20, 22, 26))
            draw.text((6, 5), label, fill=(245, 245, 240))
            frames.append(img)

    renderer.close()
    model.cam_fovy[cam_id] = orig_fovy
    q_arr = np.asarray(q_hist)
    eye_arr = np.asarray(eye_hist)
    span_deg = np.ptp(q_arr, axis=0) * np.asarray(P.DEG_PER_UNIT)
    eye_span = np.ptp(eye_arr, axis=0)
    metrics = {
        'name': name,
        'contacts': max_contacts,
        'max_trq_pct': [round(float(x), 1) for x in max_torque_pct],
        'span_deg': [round(float(x), 1) for x in span_deg],
        'eye_span_mm': [round(float(x), 1) for x in eye_span],
        'min_eye_z_mm': round(min_eye_z, 1),
        'pitch_range_deg': (round(min(pitch_hist), 1), round(max(pitch_hist), 1)),
        'sfx': len(sfx_events),
    }
    return frames, metrics


def build_contact_sheet(strips, out_path):
    """Stack list of (6-frame strip) rows into one high-clarity inspection sheet."""
    if not strips:
        return
    fw, fh = strips[0][0].size
    cols = len(strips[0])
    rows = len(strips)
    sheet = Image.new('RGB', (fw * cols, fh * rows), (18, 20, 24))
    for r, row_frames in enumerate(strips):
        for c, img in enumerate(row_frames):
            sheet.paste(img, (c * fw, r * fh))
    sheet.save(out_path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', default='/tmp/bitch_motion_previews')
    parser.add_argument('--gif', action='store_true', help='Also export an animated GIF showcase')
    args = parser.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    model = mujoco.MjModel.from_xml_string(mjcf())
    all_strips = []
    all_metrics = []
    print(f"{'Scenario':15s} | {'Con':3s} | {'Span (b,a,n,h deg)':28s} | {'Eye Travel (x,y,z mm)':24s} | {'Pitch deg':14s} | {'Max Trq % (b,a,n,h)':24s} | SFX")
    print('-' * 124)
    for sc in SCENARIOS:
        frames, m = simulate_scenario(model, *sc)
        all_strips.append((sc[0], frames))
        all_metrics.append(m)
        print(
            f"{m['name']:15s} | {m['contacts']:3d} | "
            f"{str(m['span_deg']):28s} | {str(m['eye_span_mm']):24s} | "
            f"{str(m['pitch_range_deg']):14s} | {str(m['max_trq_pct']):24s} | {m['sfx']}"
        )

    # Save 3 crisp sheets of 6 motions each so every frame is large & easy to inspect visually!
    for idx in range(0, len(all_strips), 6):
        chunk = [frames for _, frames in all_strips[idx:idx + 6]]
        sheet_path = out_dir / f'sheet_{idx // 6 + 1}.png'
        build_contact_sheet(chunk, sheet_path)
        print(f'Saved {sheet_path}')

    if args.gif:
        # Render a smooth 25fps showcase GIF of signature Pixar motions (curious -> happy -> victory -> grumpy -> lonely -> stretch)
        gif_frames = []
        renderer = mujoco.Renderer(model, 420, 560)
        cam_id = model.camera('three_quarter').id
        orig_fovy = float(model.cam_fovy[cam_id])
        model.cam_fovy[cam_id] = max(orig_fovy, 46.0)
        data = mujoco.MjData(model)
        mujoco.mj_resetDataKeyframe(model, data, model.key('rest').id)
        desk = SimpleNamespace(
            cur=list(pi.REST), on=[True] * 4, BASE_SPEED=1.0, REST=pi.REST,
            S=[SimpleNamespace(us=lambda _u: None) for _ in range(4)],
            relax=lambda *js: None, _load=lambda: list(pi.REST),
            power_all=lambda: None, move=lambda *a, **k: None,
        )
        motion = pi.Motion(desk)
        motion.rng = random.Random(11)
        motion.started = True
        dt = 0.02
        substeps = max(1, round(dt / model.opt.timestep))
        reel = [
            ('curious', 2.2, {'valence': 0.4, 'arousal': 0.55, 'loneliness': 0.0, 'annoyance': 0.0}),
            ('victory', 2.4, {'valence': 0.95, 'arousal': 0.95, 'loneliness': 0.0, 'annoyance': 0.0}),
            ('grumpy', 2.0, {'valence': -0.25, 'arousal': 0.65, 'loneliness': 0.0, 'annoyance': 0.85}),
            ('lonely', 2.8, {'valence': -0.5, 'arousal': 0.15, 'loneliness': 0.9, 'annoyance': 0.0}),
            ('stretch', 2.6, {'valence': 0.35, 'arousal': 0.5, 'loneliness': 0.0, 'annoyance': 0.0}),
        ]
        now = 0.0
        for mode, dur, mood in reel:
            for k in range(int(dur / dt)):
                motion.step(mode, 0.0, -0.8, now, dt, mood=mood)
                rad = np.asarray(P.to_rad(desk.cur), float)
                lo, hi = model.actuator_ctrlrange[:, 0], model.actuator_ctrlrange[:, 1]
                data.ctrl[:] = np.clip(rad, lo, hi)
                mujoco.mj_step(model, data, substeps)
                if k % 2 == 0:
                    renderer.update_scene(data, camera='three_quarter')
                    img = Image.fromarray(renderer.render())
                    draw = ImageDraw.Draw(img)
                    draw.rectangle([0, 0, img.width, 26], fill=(18, 20, 24))
                    draw.text((10, 6), f'BITCH 3D Sim — {mode.upper()}', fill=(250, 250, 245))
                    gif_frames.append(img)
                now += dt
        renderer.close()
        model.cam_fovy[cam_id] = orig_fovy
        gif_path = out_dir / 'showcase.gif'
        gif_frames[0].save(gif_path, save_all=True, append_images=gif_frames[1:], duration=40, loop=0)
        print(f'Saved {gif_path} ({len(gif_frames)} frames)')

    bad_contacts = [m['name'] for m in all_metrics if m['contacts'] > 0]
    if bad_contacts:
        print('WARNING: Collisions detected in:', bad_contacts)
        sys.exit(1)


if __name__ == '__main__':
    main()
