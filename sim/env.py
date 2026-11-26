"""Gymnasium environments for training BITCH in simulation.

    import gymnasium as gym, sim
    env = gym.make('Bitch-LookAt-v0')            # turn and look at a moving face
    env = gym.make('Bitch-Pose-v0')              # follow pose targets smoothly (animation tracking)

Everything matches the real robot's control loop so a trained policy can run on the Pi:
  * one step = 20 ms, the same 50 Hz that desk.py and actor.Actor use
  * by default an action nudges each joint's target (action 1 = 0.08 desk.py units per 20 ms step),
    which keeps motion smooth on the real servos and is much easier to learn; action_mode='absolute'
    instead maps the action straight onto the joint range. Targets stay inside Actor.LO/HI.
  * observations use the same units

Sim-to-real: with randomize=True every episode changes the masses, servo strength and speed,
each servo's center (like an unsaved calibration), the true degrees per unit, command latency
and sensor noise, so a policy can't overfit to one exact robot.
"""
from collections import deque
import numpy as np
import mujoco
import gymnasium as gym
from gymnasium import spaces
from . import params as P
from .model import mjcf

DT = 1 / P.CONTROL_HZ


class BitchEnv(gym.Env):
    metadata = {'render_modes': ['rgb_array', 'human'], 'render_fps': P.CONTROL_HZ}

    def __init__(self, task='look_at', render_mode=None, randomize=True, limits='safe', action_mode='delta', max_step=0.08,
                 episode_seconds=8.0, moving_target=True, camera='three_quarter', size=(480, 640)):
        assert task in ('look_at', 'pose')
        self.task, self.render_mode, self.randomize, self.moving = task, render_mode, randomize, moving_target
        self.action_mode, self.max_step = action_mode, max_step
        self.camera, self.size = camera, size
        self.model = mujoco.MjModel.from_xml_string(mjcf())
        self.data = mujoco.MjData(self.model)
        self.skip = max(1, round(DT / self.model.opt.timestep))
        self.horizon = int(episode_seconds * P.CONTROL_HZ)
        self.lo = np.array(P.SAFE_LO if limits == 'safe' else [r[0] for r in P.REACH], float)
        self.hi = np.array(P.SAFE_HI if limits == 'safe' else [r[1] for r in P.REACH], float)
        self.deg = np.array(P.DEG_PER_UNIT, float)
        self.target_site = self.model.site('target').id
        self.eye_site = self.model.site('eyes').id
        self.robot_bodies = {self.model.body(n).id for n in ('turntable', 'arm', 'neck', 'head')}
        m = self.model     # nominal values that randomization scales from
        self._nom = dict(mass=m.body_mass.copy(), inertia=m.body_inertia.copy(), kp=m.actuator_gainprm[:, 0].copy(),
                         force=m.actuator_forcerange.copy(), damping=m.dof_damping.copy(), armature=m.dof_armature.copy())
        self.action_space = spaces.Box(-1, 1, (4,), np.float32)
        n = 16 + (7 if task == 'look_at' else 8)
        self.observation_space = spaces.Box(-np.inf, np.inf, (n,), np.float32)
        self._renderer = self._viewer = None

    # ---------------------------------------------------------------- helpers
    def units(self):
        return self.data.qpos[:4] * 180 / np.pi / self.deg

    def _randomize(self):
        m, n, r = self.model, self._nom, self.np_random
        s = r.uniform(0.8, 1.25, m.nbody) if self.randomize else np.ones(m.nbody)
        m.body_mass[:] = n['mass'] * s; m.body_inertia[:] = n['inertia'] * s[:, None]
        k = r.uniform(0.7, 1.3, 4) if self.randomize else np.ones(4)
        m.actuator_gainprm[:, 0] = n['kp'] * k; m.actuator_biasprm[:, 1] = -n['kp'] * k
        m.actuator_forcerange[:] = n['force'] * (r.uniform(0.8, 1.1, 4)[:, None] if self.randomize else 1)
        m.dof_damping[:] = n['damping'] * (r.uniform(0.7, 1.4, 4) if self.randomize else 1)
        m.dof_armature[:] = n['armature'] * (r.uniform(0.5, 2.0, 4) if self.randomize else 1)
        self.offset = r.uniform(-0.05, 0.05, 4) if self.randomize else np.zeros(4)      # servo center error (units)
        self.scale = r.uniform(0.85, 1.1, 4) if self.randomize else np.ones(4)          # true deg per unit
        self.latency = int(r.integers(0, 3)) if self.randomize else 1                   # steps of Mac->Pi->servo delay
        self.noise = 0.01 if self.randomize else 0.0

    def _command(self, units):
        """What the servo actually does with a desk.py target, including this episode's calibration errors."""
        rad = (units + self.offset) * self.deg * self.scale * np.pi / 180
        lo, hi = self.model.actuator_ctrlrange[:, 0], self.model.actuator_ctrlrange[:, 1]
        self.data.ctrl[:] = np.clip(rad, lo, hi)

    def _new_target(self):
        r = self.np_random
        if self.task == 'look_at':                   # a face somewhere in front of the desk
            dist, az = r.uniform(0.25, 0.75), r.uniform(-1.2, 1.2)
            self.target = np.array([dist * np.cos(az), dist * np.sin(az), r.uniform(0.18, 0.65)])
            self.target_vel = r.normal(0, 0.06, 3) * (1 if self.moving else 0)
        else:                                        # a pose an animation might ask for
            self.target = r.uniform(self.lo, self.hi)
            self.next_switch = self.t + int(r.uniform(0.8, 2.5) * P.CONTROL_HZ)

    def _contacts(self):
        bad = 0
        for c in self.data.contact[:self.data.ncon]:
            b1, b2 = self.model.geom_bodyid[c.geom1], self.model.geom_bodyid[c.geom2]
            if b1 in self.robot_bodies or b2 in self.robot_bodies: bad += 1
        return bad

    def _look(self):
        eye = self.data.site_xpos[self.eye_site]; fwd = self.data.site_xmat[self.eye_site].reshape(3, 3)
        to = self.target - eye; dist = np.linalg.norm(to); to_eye = fwd.T @ (to / dist)
        return to_eye, dist, float(np.clip(to_eye[0], -1, 1))

    def _obs(self):
        q = self.units() + self.np_random.normal(0, self.noise, 4)
        v = self.data.qvel[:4] * 180 / np.pi / self.deg / 10
        base = [q, v, self.goal, self.last_action]
        if self.task == 'look_at':
            to_eye, dist, _ = self._look()
            rel = self.target - np.array([0, 0, P.BOX_HEIGHT / 1000])
            extra = [to_eye, [dist], rel]
        else:
            extra = [self.target, self.target - q]
        return np.concatenate(base + extra).astype(np.float32)

    # ---------------------------------------------------------------- gym API
    def reset(self, seed=None, options=None):
        super().reset(seed=seed)
        self._randomize()
        mujoco.mj_resetDataKeyframe(self.model, self.data, self.model.key('rest').id)
        start = np.clip(np.array(P.REST) + self.np_random.normal(0, 0.08, 4) * (1 if self.randomize else 0), self.lo, self.hi)
        self.data.qpos[:4] = start * self.deg * np.pi / 180
        self.t = 0
        self.goal = start.copy()
        self.last_action = np.zeros(4) if self.action_mode == 'delta' else 2 * (start - self.lo) / (self.hi - self.lo) - 1
        self.queue = deque([start] * (self.latency + 1), maxlen=self.latency + 1)
        self._command(start)
        mujoco.mj_forward(self.model, self.data)
        self._new_target(); self._show_target()
        return self._obs(), {}

    def step(self, action):
        a = np.clip(np.asarray(action, float), -1, 1)
        if self.action_mode == 'delta': self.goal = np.clip(self.goal + a * self.max_step, self.lo, self.hi)
        else: self.goal = self.lo + (a + 1) / 2 * (self.hi - self.lo)
        self.queue.append(self.goal.copy()); self._command(self.queue[0])
        mujoco.mj_step(self.model, self.data, self.skip)
        self.t += 1
        jerk = float(np.sum((a - self.last_action) ** 2)); self.last_action = a
        effort = float(np.sum((self.data.actuator_force / self.model.actuator_forcerange[:, 1]) ** 2))
        hits = self._contacts()
        info = dict(contacts=hits)
        if self.task == 'look_at':
            if self.moving:
                self.target += self.target_vel * DT
                self.target_vel += self.np_random.normal(0, 0.15, 3) * DT
                self._keep_in_reach()
                self._show_target()
            _, _, cos = self._look()
            angle = np.degrees(np.arccos(cos)); info['angle_deg'] = angle
            head_roll = self.units()[3]
            reward = 0.5 * cos + np.exp(-angle / 10) - 0.05 * jerk - 0.02 * effort - 0.05 * head_roll ** 2 - 0.5 * min(hits, 1)
        else:
            if self.t >= self.next_switch: self._new_target()
            err = np.abs(self.target - self.units()); info['error'] = float(err.mean())
            reward = -float(err.mean()) - 0.05 * jerk - 0.02 * effort - 0.5 * min(hits, 1)
        if self.render_mode == 'human': self.render()
        return self._obs(), float(reward), False, self.t >= self.horizon, info

    def _keep_in_reach(self):
        """A wandering face bounces off the edge of where BITCH can look (in front, within arm's reach of the desk)."""
        x, y, z = self.target; d, az = np.hypot(x, y), np.arctan2(y, x)
        d2, az2, z2 = np.clip(d, 0.25, 0.8), np.clip(az, -1.2, 1.2), np.clip(z, 0.18, 0.65)
        if d2 != d or az2 != az: self.target_vel[:2] *= -1
        if z2 != z: self.target_vel[2] *= -1
        self.target = np.array([d2 * np.cos(az2), d2 * np.sin(az2), z2])

    def action_for(self, goal):
        """The action that moves the joint targets toward `goal` (desk.py units) as fast as allowed."""
        goal = np.asarray(goal, float)
        if self.action_mode == 'delta': return np.clip((goal - self.goal) / self.max_step, -1, 1)
        return 2 * (goal - self.lo) / (self.hi - self.lo) - 1

    def _show_target(self):
        if self.task == 'look_at': self.model.site_pos[self.target_site] = self.target
        else: self.model.site_pos[self.target_site] = (0, 0, -1)     # hide it under the desk

    def render(self, camera=None):
        if self.render_mode == 'human':
            if self._viewer is None:
                from mujoco import viewer
                self._viewer = viewer.launch_passive(self.model, self.data)
            self._viewer.sync(); return None
        if self._renderer is None:
            self._renderer = mujoco.Renderer(self.model, *self.size)
        opt = mujoco.MjvOption(); opt.sitegroup[5] = 1
        self._renderer.update_scene(self.data, camera=camera or self.camera, scene_option=opt)
        return self._renderer.render()

    def close(self):
        if self._renderer: self._renderer.close(); self._renderer = None
        if self._viewer: self._viewer.close(); self._viewer = None


def look_at_baseline(env):
    """A hand-written controller for the look-at task, useful as a baseline and a sanity check.
    Turns the base toward the target and tilts the neck to match its height."""
    tgt = env.target; eye = env.data.site_xpos[env.eye_site]
    base = np.degrees(np.arctan2(tgt[1], tgt[0])) / P.DEG_PER_UNIT[0]
    pitch = np.degrees(np.arctan2(tgt[2] - eye[2], np.hypot(tgt[0] - eye[0], tgt[1] - eye[1])))
    arm = -0.8
    neck = (pitch + arm * P.DEG_PER_UNIT[1]) / P.DEG_PER_UNIT[2]   # head pitch = neck - arm, so the neck also undoes the lean
    return env.action_for(np.clip([base, arm, neck, 0], env.lo, env.hi))
