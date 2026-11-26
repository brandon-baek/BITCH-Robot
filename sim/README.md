# BITCH in simulation

A measured 3D model of BITCH for physics simulation, reinforcement learning and 3D work.
Every size comes from `params.py`, and each number there is tagged MEASURED, SPEC, CODE, FITTED or ESTIMATE.

## Quick start (on the Mac)

```bash
~/dc-venv/bin/pip install -r ~/BITCH-Robot/sim/requirements.txt
cd ~/BITCH-Robot
~/dc-venv/bin/python -m sim.check                     # model vs tape measure
~/dc-venv/bin/python -m mujoco.viewer --mjcf sim/out/bitch.xml   # drag the joints around in 3D
```

In the viewer, open the Control panel on the right and slide `base`, `arm`, `neck` and `head`.
The Key frames panel has `rest`, `upright`, `curious` and `bedtime`.

## Files

| File | What it is |
|---|---|
| `params.py` | Every dimension, mass and servo spec. Edit here only. |
| `model.py` | Turns the parameters into the MuJoCo model |
| `build.py` | `python -m sim.build` rebuilds everything in `out/` |
| `out/bitch.xml` | MuJoCo model (the master copy): servos, cameras, sensors, keyframes |
| `out/bitch.urdf` | Same robot for PyBullet, ROS, Isaac Sim, Gazebo |
| `out/bitch.json` | Body tree and shapes as plain data (used by the web viewer) |
| `out/meshes/` | Whole robot as `.glb` (Blender, web), `.obj` and `.stl` (CAD, 3D printing), at rest and upright |
| `out/previews/` | Renders from the front, side, three-quarter view and BITCH's eye camera |
| `env.py` | Gymnasium environments `Bitch-LookAt-v0` and `Bitch-Pose-v0` |
| `train.py` | PPO training (`pip install stable-baselines3`) |
| `policy.py` | Runs a trained policy with numpy only, so it fits on the Pi |
| `check.py` | Compares the model with the measured robot |

## Conventions

- Metres, kilograms, radians. x is forward (the way the eyes face), y is BITCH's left, z is up. The origin is on the desk, under the base servo.
- Joint values in the environments and `policy.py` are **desk.py units** (−1…1): `+base` turns left, `+arm` leans forward and down, `+neck` looks up.
- The environment steps every 20 ms, like `desk.py` and `actor.Actor`. By default an action nudges each joint's target by up to 0.08 units per step (`action_mode='delta'`), which keeps motion smooth and is easy to learn. `action_mode='absolute'` maps actions straight onto the joint range. Targets always stay inside `Actor.LO`/`Actor.HI` unless you pass `limits='reach'`.

## What the model includes

- **Every part is a measured primitive:** shoebox and lid, popsicle-stick rails, the lazy Susan (plates and 16 balls), the turning base, S2's upright tab, the arm, the L-shaped neck (side and front panels), the head panel, and googly eyes with loose pupils.
- **Servos where they really are:** S2 glued to its tab on the right; S3 glued to the top of the stick on the right (so it moves with the arm) with its shaft in the neck's side panel; S4 on its side in the neck's front panel, with the head centered on its shaft.
- **Four MG90S servos with real dimensions:** case, tabs, gear dome, spline, horn, circuit board and label. The Pi 4 and PCA9685 are inside the box, in hidden geom group 4.
- **Servo physics from the datasheet:** 0.2 N·m stall torque and 600°/s no-load speed. The motor's back-EMF is modelled as joint damping, and full torque comes at about 3° of error.
- **Collisions on the structural parts.** BITCH can hit itself or the box, like the real one.
- **Cameras:** front, side, three-quarter, top, and `eye`, which sees what BITCH would see from between its eyes.
- **Sensors:** joint position, velocity and torque, plus where the eyes are and which way they point.

## Training

A look-at policy is already trained and included: `out/policies/look_at.npz` (numpy weights, runs on the Pi)
and `look_at.zip` (for more training). Tracking a wandering face with full randomization, over 20 test episodes:

| Controller | Median error | 90% of frames under |
|---|---|---|
| Trained policy (`policy.py`, numpy only) | 0.9° | 1.6° |
| Hand-written baseline (`env.look_at_baseline`) | 3.5° | 9.2° |
| Holding the rest pose | 44° | 65° |

`out/previews/look_at_policy.gif` shows it, with BITCH's own eye camera on the right.

```bash
~/dc-venv/bin/pip install stable-baselines3
~/dc-venv/bin/python -m sim.train                  # look at a moving face: 1.2M steps took ~30 min on 2 CPU cores
~/dc-venv/bin/python -m sim.train --task pose      # follow animation targets smoothly
~/dc-venv/bin/python -m sim.train --eval sim/out/policies/look_at.zip
```

```python
import gymnasium as gym, sim
env = gym.make('Bitch-LookAt-v0', render_mode='rgb_array')
obs, _ = env.reset(seed=0)
obs, reward, done, truncated, info = env.step(env.action_space.sample())
frame = env.unwrapped.render(camera='eye')      # what BITCH sees
```

With `randomize=True` (the default), every episode changes:

- part masses (±20%)
- servo strength, speed and stiffness
- each servo's center (±0.05, like an uncalibrated `TRIM`)
- the true degrees per unit (−15% to +10%)
- Mac → Pi → servo latency (0–40 ms)
- sensor noise

That spread is what lets a policy work on the real robot, which never matches the model exactly.

## Making it more accurate

1. **Angle scale (the biggest error).** At rest the model is 12 mm lower than the real robot. That suggests the real servos turn less per unit than the code's math says. To measure it on the Pi:
   ```bash
   python3 -c "from desk import *; wakeup(); go(P(a=0),2)"     # then hold a protractor against the arm
   python3 -c "from desk import *; go(P(a=-1),2)"               # read the angle again, then relax()
   ```
   Put degrees ÷ 1.0 into `DEG_PER_UNIT` in `params.py`. Do the same for the neck and head.
   The arm length is also fitted (148 mm, so the upright height matches 235 mm); a careful shaft-to-shaft measurement of the arm would pin that down.
2. **Head tilt direction:** check which way `+head` tilts and note it in `params.py`.
3. **Weigh the parts.** Kitchen-scale masses beat estimates. The head and neck matter most.
4. After any change: `python -m sim.build`, then `python -m unittest testing.test_sim`.

Not modelled: cable stiffness and the drag of the wires, the servos' 5 µs deadband, gear backlash, and the friction of a rough lazy Susan. For that last one, raise `frictionloss` on the base joint if the real base sticks.
