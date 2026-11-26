"""Tests for the simulation model.  python -m unittest testing.test_sim   (skips if MuJoCo isn't installed)"""
import os, sys, tempfile, unittest, xml.etree.ElementTree as ET
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    import mujoco
except ImportError:
    mujoco = None


@unittest.skipIf(mujoco is None, 'pip install mujoco to test the simulation')
class SimModel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sim import model, params
        cls.P = params
        cls.m = mujoco.MjModel.from_xml_string(model.mjcf())
        cls.d = mujoco.MjData(cls.m)

    def pose(self, units):
        self.d.qpos[:] = self.P.to_rad(units); self.d.ctrl[:] = self.d.qpos; self.d.qvel[:] = 0
        mujoco.mj_forward(self.m, self.d)

    def head_top(self):
        return self.d.site('head_top').xpos[2] * 1000 - self.P.BOX_HEIGHT

    def test_chain(self):
        self.assertEqual([self.m.joint(i).name for i in range(self.m.njnt)], ['base', 'arm', 'neck', 'head'])

    def test_upright_height_matches_tape_measure(self):
        self.pose((0, 0, 0, 0)); self.assertAlmostEqual(self.head_top(), self.P.MEASURED_HEIGHT_UPRIGHT, delta=3)

    def test_signs_match_desk_py(self):
        self.pose((0.5, 0, 0, 0)); self.assertGreater(self.d.site('eyes').xpos[1], 0.01)       # + base turns left
        self.pose((0, 0, 0.5, 0)); self.assertGreater(self.d.site('eyes').xmat[6], 0.3)        # + neck looks up (eye's forward axis tips upward)
        self.pose((0, 0, 0, 0)); z0 = self.head_top()
        self.pose((0, 0.5, 0, 0)); self.assertLess(self.head_top(), z0)                        # + arm goes lower

    def test_holds_every_keyframe_without_collisions(self):
        for k in range(self.m.nkey):
            mujoco.mj_resetDataKeyframe(self.m, self.d, k)
            for _ in range(500): mujoco.mj_step(self.m, self.d)
            np.testing.assert_allclose(self.d.qpos, self.m.key_qpos[k], atol=0.03)
            self.assertEqual(self.d.ncon, 0, self.m.key(k).name)

    def test_servos_not_overloaded_at_rest(self):
        mujoco.mj_resetDataKeyframe(self.m, self.d, self.m.key('rest').id)
        for _ in range(500): mujoco.mj_step(self.m, self.d)
        self.assertTrue(np.all(np.abs(self.d.actuator_force) < 0.5 * self.P.SERVO_STALL_TORQUE))

    def test_exports(self):
        from sim import build
        u = build.urdf(self.m); root = ET.fromstring(u.split('\n', 1)[1])
        self.assertEqual(len(root.findall('joint')), 4)
        j = build.as_json(self.m); self.assertEqual(len(j['bodies']), 5)
        self.assertTrue(all(b['geoms'] for b in j['bodies']))


@unittest.skipIf(mujoco is None, 'pip install mujoco gymnasium')
class SimEnv(unittest.TestCase):
    def test_look_at_baseline_beats_standing_still(self):
        from sim.env import BitchEnv, look_at_baseline
        e = BitchEnv(randomize=False, moving_target=False)
        e.reset(seed=3)
        for _ in range(100): _, r, _, _, info = e.step(look_at_baseline(e))
        self.assertLess(info['angle_deg'], 5)

    def test_pose_task_runs(self):
        from sim.env import BitchEnv
        e = BitchEnv(task='pose'); o, _ = e.reset(seed=0)
        for _ in range(50): o, r, term, trunc, _ = e.step(e.action_space.sample())
        self.assertTrue(np.all(np.isfinite(o)))

    def test_trained_policy_runs_with_numpy(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'sim', 'out', 'policies', 'look_at.npz')
        if not os.path.exists(path): self.skipTest('no trained policy yet')
        from sim.env import BitchEnv
        from sim.policy import Policy
        pi, e = Policy(path), BitchEnv(randomize=False, moving_target=False)
        o, _ = e.reset(seed=1)
        for _ in range(150): o, _, _, _, info = e.step(pi(o))
        self.assertLess(info['angle_deg'], 5)

    def test_pixar_character_motions_are_collision_free_expressive_and_torque_safe(self):
        from sim.preview_motion import simulate_scenario
        from sim.model import mjcf
        import mujoco

        model = mujoco.MjModel.from_xml_string(mjcf())
        checks = [
            ('curious', 'curious', 2.2, 0.0, -0.8, {'valence': 0.35, 'arousal': 0.55}, None, 'three_quarter'),
            ('victory', 'victory', 2.4, 0.0, -0.8, {'valence': 0.95, 'arousal': 0.95}, None, 'three_quarter'),
            ('lonely', 'lonely', 3.0, 0.0, -0.8, {'valence': -0.5, 'arousal': 0.15, 'loneliness': 0.9}, None, 'side'),
            ('doze', 'doze', 5.0, 0.0, -0.8, {'valence': 0.0, 'arousal': 0.05}, None, 'side'),
            ('scared', 'scared', 1.8, 0.0, -0.8, {'valence': -0.3, 'arousal': 0.9}, None, 'side'),
            ('speak_playful', 'speak-playful', 2.2, 0.0, -0.8, {'valence': 0.7, 'arousal': 0.7}, 'playful', 'three_quarter'),
        ]
        results = {}
        for item in checks:
            _, metrics = simulate_scenario(model, *item)
            results[item[0]] = metrics
            self.assertEqual(metrics['contacts'], 0, f'Collision in {item[0]}')
            self.assertLess(max(metrics['max_trq_pct']), 45.0, f'Torque too high in {item[0]}: {metrics["max_trq_pct"]}')
            self.assertGreater(metrics['min_eye_z_mm'], 50.0, f'Head too low in {item[0]}')
        # Expressive enough to read across a room...
        self.assertGreater(results['curious']['eye_span_mm'][0], 25.0)
        self.assertGreater(results['victory']['span_deg'][3], 40.0)
        self.assertGreater(results['victory']['span_deg'][0], 25.0)
        self.assertGreater(results['lonely']['span_deg'][1], 30.0)
        self.assertGreater(results['doze']['span_deg'][1], 50.0)
        # ...but coherent: one idea per gesture, no jittery back-and-forth, and the base holds still while
        # talking so BITCH keeps eye contact. (Reversals are direction changes per joint.)
        for name in ('curious', 'lonely', 'scared', 'doze'):
            self.assertLessEqual(max(results[name]['reversals']), 3, f'{name} is busy: {results[name]["reversals"]}')
        self.assertLessEqual(results['speak_playful']['reversals'][0], 1)
        self.assertLess(results['speak_playful']['span_deg'][0], 10.0)

if __name__ == '__main__':
    unittest.main()


class SpeechBody(unittest.TestCase):
    def test_every_spoken_gesture_reads_on_the_body(self):
        import mujoco
        from sim import speech_motion
        from sim.model import mjcf
        model = mujoco.MjModel.from_xml_string(mjcf())
        for name, text, delivery in speech_motion.LINES:
            events, rows, _ = speech_motion.simulate(model, name, text, delivery)
            results = speech_motion.check(name, events, rows)
            self.assertTrue(results, name)
            for kind, at, _, ok, detail in results:
                self.assertTrue(ok, f'{name}: {kind} at {at:.2f}s, {detail}')
