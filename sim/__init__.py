"""BITCH in simulation: a measured 3D model plus training environments. See sim/README.md."""
try:
    from gymnasium.envs.registration import register
    register(id='Bitch-LookAt-v0', entry_point='sim.env:BitchEnv', kwargs={'task': 'look_at'})
    register(id='Bitch-Pose-v0', entry_point='sim.env:BitchEnv', kwargs={'task': 'pose'})
except Exception:       # gymnasium not installed: the model files still build
    pass
