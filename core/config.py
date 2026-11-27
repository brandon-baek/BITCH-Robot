"""Read existing settings and save only changes made by this version."""
import json
import os
from pathlib import Path
import threading

ROOT = Path(__file__).resolve().parent.parent
HOME = Path(os.environ.get('BITCH_HOME', '~/.bitch')).expanduser()
HOME.mkdir(parents=True, exist_ok=True)
APP_DIR = ROOT / 'app'
DEFAULTS = {
    # Chosen by testing/eval_models.py (Nov 2026): best mix of personality, reasoning and reliable tool use.
    'brain_v2': False,
    'chat_model': 'codecraftersllc/ornith-1.5-35b-a3b-abliterated:latest',
    'voicechat_model': '',
    'voicechat_bin': '',
    'voicechat_enabled': False,
    'ollama_url': 'http://127.0.0.1:11434',
    'llm_server': True,
    'voice_engine': 'qwen',   # BITCH's own cloned voice; 'kokoro' is the fast backup
    'voice_model': '0.6B',    # '1.7B' for the bigger voice model (needs more GPU headroom)     # run the chat model in BITCH's own llama-server (fast follow-up turns)
    'kokoro_voice': 'af_heart',
    'mac_voice': 'Samantha',
    'mic_source': 'mac-mic',
    'camera_source': 'mac-camera',
    'speaker_source': 'auto',
    'camera_calibration': {},
    'idle': True,
    'muted': False,
    'mic_muted': False,
    'sounds': True,
    'sleep_at_night': True,
    'quiet_start': 22,
    'quiet_end': 8,
    'keep_audio': False,
    'check_ins': False,
    'natural_memory': False,
    'volume': 100,
    'robot_host': 'raspberrypi.local',
    'robot_key': '',
    'wake_seconds': 15,   # how long waking from deep sleep took lately (learned), for "give me about N seconds"
    'home_place': None,   # {'name', 'lat', 'lon', 'imperial'}, set by saying where you live (for weather)
}
PUBLIC = {'mic_source', 'camera_source', 'speaker_source', 'camera_calibration', 'idle', 'muted', 'mic_muted', 'keep_audio',
          'sounds', 'sleep_at_night', 'quiet_start', 'quiet_end',
          'check_ins', 'natural_memory', 'voicechat_enabled', 'volume', 'robot_host'}
path = HOME / 'settings.json'
lock = threading.Lock()
try:
    stored = json.loads(path.read_text())
    if not isinstance(stored, dict):
        stored = {}
except (OSError, ValueError):
    stored = {}
if stored.get('chat_model') in ('qwen3.5:4b', 'qwen3.5:4b-q4_K_M') and not stored.get('brain_v2'):
    # Once: move from the old small brain to the new default. A model chosen after this stays chosen.
    stored['chat_model'] = DEFAULTS['chat_model']
    stored['brain_v2'] = True
for old, new in [('mic_on', 'mic_source'), ('camera_on', 'camera_source')]:
    if stored.get(old) is False:
        stored.setdefault(new, 'off')


def get(key):
    with lock:
        return stored.get(key, DEFAULTS[key])


def update(changes):
    with lock:
        values = {**stored, **changes}
        temporary = path.with_suffix('.tmp')
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as file:
            json.dump(values, file, indent=2)
        temporary.replace(path)
        stored.update(changes)


def public():
    with lock:
        return {key: stored.get(key, DEFAULTS[key]) for key in PUBLIC} | {'robot_paired': bool(stored.get('robot_key'))}
