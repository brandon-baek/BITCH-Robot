"""Tests that run on the Mac with no robot, no camera and no real AI models.

  ~/dc-venv/bin/python -m unittest testing.test_bitch -v

The end-to-end test starts a fake Ollama, the Pi's brain.py with a fake servo board, and BITCH itself in a
scratch folder, then chats with it like a phone would.
"""
import datetime as dt, json, os, socket, subprocess, sys, tempfile, time, unittest, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRATCH = tempfile.mkdtemp(prefix='bitch-test-')
os.environ['BITCH_HOME'] = os.path.join(SCRATCH, 'home')
os.environ.setdefault('OLLAMA_URL', 'http://127.0.0.1:1')          # unit tests never reach a model
sys.path.insert(0, ROOT)


class Times(unittest.TestCase):
    def test_when(self):
        from core import timeparse as T
        now = dt.datetime(2026, 11, 23, 17, 30)          # a Monday
        cases = {'in 10 minutes': '2026-11-23 17:40', 'at 7': '2026-11-23 19:00', 'at 9': '2026-11-23 21:00', 'tomorrow at 9': '2026-11-24 09:00',
                 'tomorrow at 3': '2026-11-24 15:00', 'friday at 10am': '2026-11-27 10:00', 'in half an hour': '2026-11-23 18:00', 'tonight': '2026-11-23 20:00'}
        for words, want in cases.items():
            self.assertEqual(T.when(words, now).strftime('%Y-%m-%d %H:%M'), want, words)
        self.assertIsNone(T.when('banana', now))

    def test_repeat(self):
        from core import timeparse as T
        r = T.repeat('every weekday at 7:30'); self.assertEqual(r['days'], [0, 1, 2, 3, 4]); self.assertEqual(r['at'], '07:30')
        self.assertEqual(T.next_due(r, dt.datetime(2026, 11, 28, 12, 0)).strftime('%a %H:%M'), 'Mon 07:30')
        self.assertEqual(T.repeat('every 2 hours')['secs'], 7200)


class Moves(unittest.TestCase):
    def test_clean_keeps_the_servos_safe(self):
        from core import moves
        st = moves.clean([{'do': 'pose', 'base': 5, 'arm': 0.9, 'secs': 99}, {'do': 'wiggle', 'joints': ['head', 'tail'], 'size': 3, 'hz': 50},
                          {'do': 'sound', 'sound': 'not a sound'}, {'do': 'explode'}, 'junk'])
        self.assertEqual(st[0]['base'], 0.9); self.assertEqual(st[0]['arm'], -0.15); self.assertEqual(st[0]['secs'], 4.0)
        self.assertEqual(st[1]['joints'], ['head']); self.assertEqual(st[1]['size'], 0.5); self.assertEqual(st[1]['hz'], 6)
        self.assertEqual(len(st), 2)

    def test_frames(self):
        from core import moves
        st = moves.from_frames([{'base': 0.3, 'arm': -0.5, 'neck': -0.4, 'head': 0.2, 'secs': 1, 'speed': 'snappy', 'wiggle': 'head', 'sound': 'tada'}])
        self.assertEqual([s['do'] for s in st], ['sound', 'pose', 'wiggle'])

    def test_find(self):
        from core import moves
        self.assertEqual(moves.find('dance')[0], 'show'); self.assertEqual(moves.find('the wave')[1], 'wave')
        self.assertIsNone(moves.find('a robot sneeze'))


class Tools(unittest.TestCase):
    def test_calculate(self):
        from core import tools
        self.assertEqual(tools.run('calculate', {'expression': '17*23'}, {})[1], '17*23 = 391')
        self.assertFalse(tools.run('calculate', {'expression': '__import__("os")'}, {})[0])

    def test_guard_blocks_timers_nobody_asked_for(self):
        from core import tools
        self.assertEqual(tools.run('set_timer', {'minutes': 5}, {'text': "I'm so tired", 'mode': 'typed'}), (False, None))

    def test_lists(self):
        from core import tools
        tools.run('list', {'action': 'add', 'list': 'shopping list', 'items': 'eggs and milk'}, {'mode': 'typed'})
        self.assertIn('eggs, milk', tools.run('list', {'action': 'read', 'list': 'shopping'}, {})[1])


class Brain(unittest.TestCase):
    def test_names_and_greetings(self):
        from core.brain import named, GREETING, Brain
        self.assertTrue(named('hey bitch what time is it')); self.assertFalse(named('the bit chart'))
        self.assertTrue(GREETING.match('Hey BITCH!')); self.assertFalse(GREETING.match('hey, set a timer'))
        self.assertEqual(Brain().tidy('*waves* Hi! [happy] 🎉 there'), 'Hi! there')

    def test_overheard_talk_is_ignored(self):
        from core.brain import Brain
        b = Brain()
        self.assertFalse(b.engaged('Ryan, do you want to see my companion app?'))
        self.assertFalse(b.engaged('Now go join.'))
        self.assertTrue(b.engaged('hey bitch, what time is it'))
        self.assertTrue(b.engaged("yes it's me", {'name': 'Brandon'})); self.assertFalse(b.engaged("Actually, what's it called?", {'name': 'Brandon'}))
        b.last_spoke = __import__('time').time(); self.assertTrue(b.engaged('yeah, tell me more'))

    def test_home_city_must_be_said(self):
        from core import tools
        self.assertEqual(tools.run('set_home', {'city': 'unknown'}, {'text': 'Bringo.', 'mode': 'ambient'})[1], 'Not set: ask the person which city they live in.')


def free(port):
    with socket.socket() as s: return s.connect_ex(('127.0.0.1', port)) != 0


@unittest.skipUnless(free(8780) and free(8765) and free(11500), 'BITCH, the robot or the fake brain is already running on this Mac')
class EndToEnd(unittest.TestCase):
    """Fake brain + fake robot + BITCH, then a pretend phone chats with it."""

    @classmethod
    def setUpClass(cls):
        py = sys.executable; env = dict(os.environ, OLLAMA_URL='http://127.0.0.1:11500', HOME=os.path.join(SCRATCH, 'pi'))
        os.makedirs(env['HOME'], exist_ok=True)
        cls.procs = [subprocess.Popen([py, 'testing/fake_ollama.py', '11500'], cwd=ROOT, stdout=subprocess.DEVNULL),
                     subprocess.Popen([py, '-c', 'from brain import *; listen()'], cwd=ROOT, env=dict(env, PYTHONPATH='testing/fakes:.'),
                                      stdout=open(os.path.join(SCRATCH, 'pi.log'), 'w'), stderr=subprocess.STDOUT),
                     subprocess.Popen([py, '-m', 'core', '--quiet', '--no-mic', '--no-camera', '--no-browser', '--pi', '127.0.0.1'], cwd=ROOT,
                                      env=dict(env, HOME=os.path.expanduser('~')), stdout=open(os.path.join(SCRATCH, 'core.log'), 'w'), stderr=subprocess.STDOUT)]
        for _ in range(60):
            try: urllib.request.urlopen('http://127.0.0.1:8780/api/hello', timeout=1); break
            except Exception: time.sleep(0.5)
        time.sleep(4)

    @classmethod
    def tearDownClass(cls):
        for p in cls.procs: p.terminate(); p.wait(5)

    def chat(self, *lines, wait=6):
        out = subprocess.run([sys.executable, 'testing/phone.py', 'say', *lines, '--wait', str(wait)], cwd=ROOT, capture_output=True, text=True, timeout=120)
        return out.stdout

    def test_conversation_tools_and_robot(self):
        self.assertIn("You're home!", self.chat('hey bitch', wait=32))         # the one-time welcome after the upgrade (spoken by the robot)
        log = self.chat('set a 1 minute timer', 'remember that my sister is Chloe', 'do a robot sneeze', wait=4)
        self.assertIn('Timer started', log)
        life = json.loads(urllib.request.urlopen('http://127.0.0.1:8780/api/life').read())
        self.assertTrue(any(t['kind'] == 'timer' for t in life['tasks']))
        mem = json.loads(urllib.request.urlopen('http://127.0.0.1:8780/api/memories').read())
        self.assertTrue(any('Chloe' in m['text'] for m in mem))
        time.sleep(4)
        self.assertIn('move: test', open(os.path.join(SCRATCH, 'pi.log')).read())       # the robot played the invented move

    def test_pairing_is_required_off_the_mac(self):
        ip = subprocess.run(['ipconfig', 'getifaddr', 'en0'], capture_output=True, text=True).stdout.strip()
        if not ip: self.skipTest('no Wi-Fi address')
        with self.assertRaises(urllib.error.HTTPError) as e: urllib.request.urlopen(f'http://{ip}:8780/api/memories')
        self.assertEqual(e.exception.code, 401)


if __name__ == '__main__':
    unittest.main()
