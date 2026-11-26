"""Route one microphone, one camera and one speaker at a time."""
import threading
import time

from . import config


class Hub:
    def __init__(self):
        self.devices = {}
        self.lock = threading.RLock()
        self.status = 'Starting'
        self.messages = []
        self.notes = {}
        self.level = 0
        self.brain = self.hearing = self.sight = None

    def add(self, device):
        device.hub = self
        with self.lock:
            self.devices[device.id] = device
        device.start()

    def remove(self, device):
        with self.lock:
            if self.devices.get(device.id) is device:
                del self.devices[device.id]

    def get(self, device_id):
        with self.lock:
            return self.devices.get(device_id)

    def selected(self, capability):
        key = {'ears': 'mic_source', 'eyes': 'camera_source', 'voice': 'speaker_source'}[capability]
        source = config.get(key)
        with self.lock:
            if source == 'auto':
                for device_id in ('robot', 'mac-speaker'):
                    device = self.devices.get(device_id)
                    if device_id == 'robot' and capability == 'voice' and not getattr(device, 'audio_monitor', False):
                        continue
                    if device and device.active(capability):
                        return device
                return None
            device = self.devices.get(source)
            return device if device and device.active(capability) else None

    def accepts(self, device_id, capability):
        selected = self.selected(capability)
        return selected is not None and selected.id == device_id

    def note(self, key, message):
        with self.lock:
            self.notes[key] = message
        if message:
            print(f'  {message}', flush=True)

    def chat(self, role, text):
        with self.lock:
            self.messages = (self.messages + [{'role': role, 'text': text, 'at': time.time()}])[-40:]

    def snapshot(self):
        with self.lock:
            return {
                'status': self.status, 'messages': self.messages, 'level': self.level,
                'notes': [message for message in self.notes.values() if message],
                'devices': [device.info() for device in self.devices.values()],
            }


hub = Hub()
