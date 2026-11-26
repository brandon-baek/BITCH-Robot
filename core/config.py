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
    'chat_model': 'qwen3.5:4b-q4_K_M',
    'voicechat_model': '',
    'voicechat_bin': '',
    'voicechat_enabled': False,
    'ollama_url': 'http://127.0.0.1:11434',
    'kokoro_voice': 'af_heart',
    'mac_voice': 'Samantha',
    'mic_source': 'mac-mic',
    'camera_source': 'mac-camera',
    'speaker_source': 'auto',
    'camera_calibration': {},
    'idle': True,
    'muted': False,
    'mic_muted': False,
    'keep_audio': False,
    'check_ins': False,
    'natural_memory': False,
    'volume': 100,
    'robot_host': 'raspberrypi.local',
    'robot_key': '',
}
PUBLIC = {'mic_source', 'camera_source', 'speaker_source', 'camera_calibration', 'idle', 'muted', 'mic_muted', 'keep_audio',
          'check_ins', 'natural_memory', 'voicechat_enabled', 'volume', 'robot_host'}
path = HOME / 'settings.json'
lock = threading.Lock()
try:
    stored = json.loads(path.read_text())
    if not isinstance(stored, dict):
        stored = {}
except (OSError, ValueError):
    stored = {}
if stored.get('chat_model') == 'qwen3.5:4b':
    stored['chat_model'] = DEFAULTS['chat_model']
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
