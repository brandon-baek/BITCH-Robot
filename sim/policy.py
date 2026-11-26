"""Run a trained policy with numpy only (works on the Pi, no PyTorch).

    from sim.policy import Policy
    pi = Policy('sim/out/policies/look_at.npz')
    action = pi(observation)          # 4 numbers, -1..1, same meaning as in sim/env.py

The observation must be built exactly like BitchEnv._obs(), and the action mapped
into Actor's safe range the same way BitchEnv.step() does.
"""
import numpy as np


def export(sb3_model, path):
    """Save a stable-baselines3 PPO MLP policy as plain numpy weights."""
    sd = {k: v.detach().cpu().numpy() for k, v in sb3_model.policy.state_dict().items()}
    keep = {k.replace('.', '_'): v for k, v in sd.items() if k.startswith(('mlp_extractor.policy_net', 'action_net'))}
    np.savez(path, **keep)


class Policy:
    def __init__(self, path):
        w = np.load(path)
        hidden = sorted({k.split('_')[4] for k in w.files if k.startswith('mlp_extractor_policy_net')}, key=int)
        self.layers = [(w[f'mlp_extractor_policy_net_{i}_weight'], w[f'mlp_extractor_policy_net_{i}_bias']) for i in hidden]
        self.out = (w['action_net_weight'], w['action_net_bias'])

    def __call__(self, obs):
        x = np.asarray(obs, np.float32)
        for W, b in self.layers: x = np.tanh(W @ x + b)
        return np.clip(self.out[0] @ x + self.out[1], -1, 1)
