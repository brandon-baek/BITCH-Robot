"""The Mac's audio/video devices, including locally attached USB devices."""
import os
import subprocess
import tempfile
import threading
import time

from .base import Device
from .. import config


def ask_permission(kind):
    try:
        import AVFoundation as av

        media = av.AVMediaTypeVideo if kind == 'camera' else av.AVMediaTypeAudio
        if av.AVCaptureDevice.authorizationStatusForMediaType_(media) == 0:
            av.AVCaptureDevice.requestAccessForMediaType_completionHandler_(media, lambda _: None)
    except ImportError:
        pass


class MacSpeaker(Device):
    def __init__(self):
        super().__init__('mac-speaker', 'Mac speaker', ('voice',))
        self.lock = threading.Lock()
        self.player = None

    def speak(self, wav):
        with self.lock:
            try:
                with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as file:
                    path = file.name
                    file.write(wav)
                if self.player and self.player.poll() is None:
                    self.player.terminate()
                self.player = player = subprocess.Popen(['afplay', path])
            except OSError as error:
                if 'path' in locals():
                    os.unlink(path)
                self.hub.note('speaker', f'Mac playback failed: {error}')
                return False
        def cleanup():
            player.wait()
            os.unlink(path)
        threading.Thread(target=cleanup, daemon=True).start()
        return True

    def playing(self):
        with self.lock:
            return bool(self.player and self.player.poll() is None)

    def hush(self):
        with self.lock:
            if self.player and self.player.poll() is None:
                self.player.terminate()


class MacMic(Device):
    def __init__(self, source=None):
        super().__init__('mac-mic', 'Mac microphone', ('ears',), 'Waiting for input')
        self.source = source

    def start(self):
        threading.Thread(target=self.capture, daemon=True).start()

    def capture(self):
        import numpy as np
        import sounddevice as sd

        def receive(data, frames, t, status):
            samples = data[:, 0]
            if rate != 16000:
                samples = np.interp(points[:round(frames * 16000 / rate)], np.arange(frames), samples)
            self.hub.hearing.feed(self.id, samples.astype(np.float32, copy=True))

        while True:
            if not self.hub.accepts(self.id, 'ears'):
                self.detail = 'Standby'
                time.sleep(0.4)
                continue
            try:
                rate = sd.query_devices(self.source, 'input')['default_samplerate']
                block = round(rate / 50)
                points = np.arange(round(block * 16000 / rate)) * rate / 16000
                with sd.InputStream(device=self.source, samplerate=rate, channels=1, blocksize=block, callback=receive):
                    self.detail = 'Open · ' + f'{rate / 1000:g} kHz'
                    while self.hub.accepts(self.id, 'ears'):
                        time.sleep(0.3)
            except Exception as error:
                self.detail = f'Microphone unavailable: {error}'
                time.sleep(2)


class MacCamera(Device):
    def __init__(self, source):
        super().__init__('mac-camera', 'Mac camera', ('eyes',), 'Waiting for input')
        self.source = source

    def start(self):
        threading.Thread(target=self.capture, daemon=True).start()

    def capture(self):
        import cv2

        source = int(self.source) if self.source.isdigit() else self.source
        while True:
            if not self.hub.accepts(self.id, 'eyes'):
                self.detail = 'Standby'
                time.sleep(0.4)
                continue
            camera = cv2.VideoCapture(source)
            try:
                camera.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                while self.hub.accepts(self.id, 'eyes'):
                    ok, frame = camera.read()
                    if not ok:
                        if isinstance(source, str) and camera.set(cv2.CAP_PROP_POS_FRAMES, 0):
                            time.sleep(0.1)
                            ok, frame = camera.read()
                        if not ok:
                            self.detail = 'No picture · check camera and permission'
                            break
                    self.detail = 'Sending picture'
                    self.hub.sight.feed(self.id, frame)
                    time.sleep(0.06)
            finally:
                camera.release()
            time.sleep(1)
