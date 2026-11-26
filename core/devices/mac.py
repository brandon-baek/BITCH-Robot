"""The Mac's audio/video devices, including locally attached USB devices."""
import os
import subprocess
import tempfile
import threading
import time

import numpy as np

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


class Resampler:
    """Turn a mic stream into 16 kHz for Whisper without folding hiss into the speech band.

    Plain interpolation from 48 kHz aliased everything above 8 kHz back into the voice,
    right where the "tch" of the robot's name lives. Measured on noisy far-field clips,
    filtering first cut Whisper's word errors from 8.8% to 5.8%."""
    def __init__(self, rate, out=16000):
        from scipy.signal import firwin

        self.step = rate / out
        self.taps = firwin(63, 7400, fs=rate).astype(np.float32) if rate > out else None
        self.state = np.zeros(0 if self.taps is None else len(self.taps) - 1, np.float32)
        self.last = np.zeros(1, np.float32)
        self.offset = 1.

    def __call__(self, samples):
        from scipy.signal import lfilter

        samples = np.asarray(samples, dtype=np.float32)
        if self.taps is not None:
            samples, self.state = lfilter(self.taps, 1., samples, zi=self.state)
        if self.step == 1:
            return samples.astype(np.float32, copy=True)
        # Carry the previous block's last sample so interpolation is seamless across blocks.
        joined = np.concatenate((self.last, samples))
        positions = np.arange(self.offset, len(joined) - 1, self.step)
        result = np.interp(positions, np.arange(len(joined)), joined)
        self.offset = (positions[-1] if len(positions) else self.offset - self.step) + self.step - (len(joined) - 1)
        self.last = joined[-1:]
        return result.astype(np.float32)


class MacMic(Device):
    def __init__(self, source=None):
        super().__init__('mac-mic', 'Mac microphone', ('ears',), 'Waiting for input')
        self.source = source

    def start(self):
        threading.Thread(target=self.capture, daemon=True).start()

    def capture(self):
        import sounddevice as sd

        def receive(data, frames, t, status):
            self.hub.hearing.feed(self.id, convert(data[:, 0]))

        while True:
            if not self.hub.accepts(self.id, 'ears') or config.get('mic_muted'):
                # Muted means the microphone is released, so macOS shows it as off.
                self.detail = 'Muted' if config.get('mic_muted') else 'Standby'
                time.sleep(0.4)
                continue
            try:
                rate = sd.query_devices(self.source, 'input')['default_samplerate']
                block = round(rate / 50)
                convert = Resampler(rate)
                with sd.InputStream(device=self.source, samplerate=rate, channels=1, blocksize=block, callback=receive):
                    self.detail = 'Open · ' + f'{rate / 1000:g} kHz'
                    while self.hub.accepts(self.id, 'ears') and not config.get('mic_muted'):
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
