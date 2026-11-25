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
        self.handoff = False
        self.audio_warm = False
        self.warming_since = None
        self.expressive = False
        self.performance = False
        self.emotive_performance = False
        self.motion_enabled = False
        self.joint_attention = False
        self.shows = False
        self.show_active = False

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
            self.handoff = bool(result and result.get('handoff') == 2)
            self.audio_monitor = bool(result and result.get('playback_monitor'))
            self.expressive = bool(result and result.get('character'))
            self.performance = bool(result and result.get('performance', 0) >= 2)
            self.emotive_performance = bool(result and result.get('performance', 0) >= 3)
            self.motion_enabled = bool(result and result.get('motion', True))
            self.joint_attention = bool(result and result.get('joint_attention'))
            self.shows = bool(result and result.get('shows') == 1)
            self.show_active = bool(result and result.get('show_active'))
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
            keep = (self.online and result.get('audio_keepalive') and self.active('voice')
                    and self.hub.selected('voice') is self and config.get('keep_audio') and not config.get('muted')
                    and config.get('volume'))
            if keep:
                if self.post('keepalive', {'volume': config.get('volume') / 100}):
                    now = time.monotonic()
                    if self.warming_since is None:
                        self.warming_since = now
                    self.audio_warm = bool(result.get('audio_warm') and now - self.warming_since >= 5)
                else:
                    self.audio_warm = False
                    self.warming_since = None
            else:
                if self.warming_since is not None and self.online:
                    self.post('keepalive', {'volume': 0})
                self.audio_warm = False
                self.warming_since = None
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
        result = self.post('stop', {})
        if result:
            self.show_active = False
        return bool(result)

    def show(self, token, name, volume, steps=None):
        item = {'id': token, 'name': name, 'volume': volume}
        if steps is not None:
            item['steps'] = steps
        result = self.post('show', item)
        if result and result.get('state') == 'queued':
            self.show_active = True
        return result

    def macro(self, request_id, name, prompt):
        item = {'id': request_id, 'name': name, 'prompt': prompt}
        return bool(self.post('macro', item) or self.post('macro', item))

    def macro_status(self, request_id):
        return self.request('macro/status?id=' + request_id) if self.handoff else None
