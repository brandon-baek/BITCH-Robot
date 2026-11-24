"""A pretend PCA9685 servo board, so the Pi code (desk.py, brain.py) runs on a Mac for testing.
Every servo position is kept in POSES and written to $FAKE_SERVO_LOG (if set) so a test can see the robot move."""
import json, os, time
POSES = {}
_LOG = os.environ.get('FAKE_SERVO_LOG')
_last = [0.0]


class _Servo:
    def __init__(s, ch): s.ch = ch; s._angle = None; s.actuation_range = 180
    def set_pulse_width_range(s, lo, hi): pass
    @property
    def angle(s): return s._angle
    @angle.setter
    def angle(s, v):
        s._angle = v; POSES[s.ch] = v
        if _LOG and time.time() - _last[0] > 0.05:
            _last[0] = time.time()
            with open(_LOG, 'a') as f: f.write(json.dumps({'t': round(time.time(), 2), **{str(k): (None if a is None else round(a, 1)) for k, a in POSES.items()}}) + '\n')


class ServoKit:
    def __init__(s, channels=16): s.servo = [_Servo(i) for i in range(channels)]
