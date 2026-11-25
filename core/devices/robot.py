"""Fixed commands sent to the paired Pi."""
import json
import threading
import time
import urllib.error
import urllib.request

from .base import Device
from .. import config


class Robot(Device):
    def __init__(self):
        super().__init__('robot', 'Raspberry Pi', ('body', 'voice'), 'Enter the connection key')
        self.online = False
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.connection_error = ''
        self.audio_monitor = False
        self.expressive = False
        self.performance = False
        self.motion_enabled = False
        self.joint_attention = False

    def start(self):
        threading.Thread(target=self.watch, daemon=True).start()

    def request(self, path, data=None, content_type='application/json', timeout=2):
        host, key = config.get('robot_host'), config.get('robot_key')
        if not host or not key:
            return None
        request = urllib.request.Request(f'http://{host}:8765/{path}', data=data, headers={
            'Authorization': 'Bearer ' + key, 'Content-Type': content_type,
        })
        try:
            with self.opener.open(request, timeout=timeout) as response:
                return json.load(response)
        except (OSError, ValueError) as error:
            if path == 'ping':
                if isinstance(error, urllib.error.HTTPError):
                    self.connection_error = f'HTTP {error.code}'
                else:
                    self.connection_error = str(getattr(error, 'reason', error))
            return None

    def post(self, path, item):
        return self.request(path, json.dumps(item).encode())

    def watch(self):
        while True:
            result = self.request('ping')
            self.online = bool(result and result.get('protocol') == 1)
            self.audio_monitor = bool(result and result.get('playback_monitor'))
            self.expressive = bool(result and result.get('character'))
            self.performance = bool(result and result.get('performance', 0) >= 2)
            self.motion_enabled = bool(result and result.get('motion', True))
            self.joint_attention = bool(result and result.get('joint_attention'))
            if self.online and result.get('audio'):
                self.caps.add('voice')
            else:
                self.caps.discard('voice')
            self.detail = ('Connected' if self.active('voice') else 'Connected · speaker unavailable') if self.online else (
                'Pi unreachable, old software, or key rejected' if config.get('robot_key') else 'Enter the connection key')
            if result and result.get('audio_route'):
                self.detail += ' · ' + result['audio_route']
            if not self.online and self.connection_error:
                self.detail += f' ({self.connection_error})'
            if self.online and not result.get('motion', True):
                self.detail += ' · motion off on Pi'
            time.sleep(2)

    def look(self, mode, base=0, neck=-0.8, energy=0., accent=0.):
        return self.post('look', {'mode': mode, 'base': base, 'neck': neck, 'energy': energy, 'accent': accent})

    def speak(self, wav):
        return bool(self.request('speak', wav, 'audio/wav', timeout=4))

    def playing(self):
        if self.audio_monitor:
            result = self.request('playback', timeout=.45)
            if result is not None:
                return result.get('playing')
        return None

    def hush(self):
        self.post('stop', {})

    def macro(self, request_id, name, prompt):
        item = {'id': request_id, 'name': name, 'prompt': prompt}
        return bool(self.post('macro', item) or self.post('macro', item))
