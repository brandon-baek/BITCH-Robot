"""Train BITCH with reinforcement learning (PPO from stable-baselines3).

    pip install stable-baselines3            # pulls in PyTorch
    python -m sim.train                      # look-at task, 300k steps (a few minutes on the M1)
    python -m sim.train --task pose --steps 1000000
    python -m sim.train --eval sim/out/policies/look_at.zip     # score a saved policy

Each run saves  sim/out/policies/<task>.zip  (for more training) and  <task>.npz  (just the
network weights; numpy only, so it can run on the Pi with no PyTorch: see sim/policy.py).
"""
import argparse, os
import numpy as np
import gymnasium as gym
from . import env as _env  # noqa: F401  (registers the environments)
from .policy import export

OUT = os.path.join(os.path.dirname(__file__), 'out', 'policies')


def evaluate(model, task, episodes=10):
    """Average return, and the median tracking error over every step (degrees for look_at, units for pose)."""
    e = _env.BitchEnv(task=task, randomize=True)
    scores, errs = [], []
    for ep in range(episodes):
        o, _ = e.reset(seed=1000 + ep); done = False; total = 0
        while not done:
            a, _ = model.predict(o, deterministic=True)
            o, r, term, trunc, info = e.step(a); total += r; done = term or trunc
            errs.append(info.get('angle_deg', info.get('error')))
        scores.append(total)
    return float(np.mean(scores)), float(np.median(errs))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--task', default='look_at', choices=['look_at', 'pose'])
    ap.add_argument('--steps', type=int, default=300_000)
    ap.add_argument('--envs', type=int, default=8)
    ap.add_argument('--eval', help='score a saved .zip instead of training')
    a = ap.parse_args()
    from stable_baselines3 import PPO
    from stable_baselines3.common.env_util import make_vec_env
    if a.eval:
        model = PPO.load(a.eval); print('return %.1f, median tracking error %.2f' % evaluate(model, a.task)); return
    os.makedirs(OUT, exist_ok=True)
    venv = make_vec_env(lambda: _env.BitchEnv(task=a.task), n_envs=a.envs, seed=0)
    model = PPO('MlpPolicy', venv, n_steps=512, batch_size=1024, learning_rate=3e-4, gamma=0.98, gae_lambda=0.95,
                ent_coef=0.0, clip_range=0.2, policy_kwargs=dict(net_arch=[128, 128], log_std_init=-1.0), verbose=0, seed=0)
    chunk = max(a.steps // 6, 1)
    for done in range(chunk, a.steps + chunk, chunk):
        model.learn(chunk, reset_num_timesteps=False)
        score, err = evaluate(model, a.task, 5)
        print(f'{min(done, a.steps):>9,} steps   return {score:7.1f}   median tracking error {err:.2f}', flush=True)
    path = os.path.join(OUT, a.task)
    model.save(path + '.zip'); export(model, path + '.npz')
    print('saved', path + '.zip', 'and', path + '.npz')


if __name__ == '__main__':
    main()
