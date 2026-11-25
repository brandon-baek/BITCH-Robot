"""A paired browser can offer its selected camera and microphone."""
import time

import cv2
import numpy as np

from .base import Device

MIC, CAMERA = 2, 3


class WebDevice(Device):
    def __init__(self, device_id, name):
        super().__init__(device_id, name, (), 'Browser connected')
        self.frame_at = 0

    def receive(self, data):
        if not data or len(data) > 160_000:
            return
        kind, payload = data[0], memoryview(data)[1:]
        if kind == MIC and 0 < len(payload) <= 8192 and len(payload) % 2 == 0 and self.hub.accepts(self.id, 'ears'):
            samples = np.frombuffer(payload, '<i2').astype(np.float32) / 32768
            self.hub.hearing.feed(self.id, samples)
        if kind == CAMERA and payload and self.hub.accepts(self.id, 'eyes'):
            now = time.monotonic()
            if now - self.frame_at < 0.07:
                return
            self.frame_at = now
            image = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
            if image is not None and max(image.shape[:2]) <= 1920:
                self.hub.sight.feed(self.id, image)
