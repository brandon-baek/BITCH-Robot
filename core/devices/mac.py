"""The Mac's own senses and speaker: its microphone (ears), its camera (eyes) and its speaker (a backup voice).

macOS asks once for camera and microphone access. Until you click Allow, these keep retrying quietly,
so BITCH starts seeing and hearing the moment you allow it.
"""
import os, subprocess, tempfile, threading, time
from .base import Device
from .. import config

os.environ.setdefault('OPENCV_AVFOUNDATION_SKIP_AUTH', '1')   # we ask for permission ourselves (see ask_permission)


def permission(kind):
    """'yes', 'no', 'ask' (not decided yet) or 'unknown' for 'camera' / 'mic'."""
    try:
        import AVFoundation as AV
        st = AV.AVCaptureDevice.authorizationStatusForMediaType_(AV.AVMediaTypeVideo if kind == 'camera' else AV.AVMediaTypeAudio)
        return {0: 'ask', 1: 'no', 2: 'no', 3: 'yes'}.get(int(st), 'unknown')
    except Exception:
        return 'unknown'


def ask_permission(kind):
    """Show macOS's 'BITCH would like to access the camera/microphone' prompt (once)."""
    try:
        import AVFoundation as AV
        if permission(kind) == 'ask':
            AV.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
                AV.AVMediaTypeVideo if kind == 'camera' else AV.AVMediaTypeAudio, lambda ok: None)
    except Exception:
        pass


class MacSpeaker(Device):
    kind = 'mac'
    priority = 1      # the robot's speaker wins when the robot is on

    def __init__(s):
        super().__init__('mac-speaker', "Mac's speaker", caps=('voice',), place='desk')
        s.p = None; s.detail = 'Used when the robot is off'

    def speak(s, wav, seconds, text='', emotion='neutral', move='none'):
        s.hush()
        f = tempfile.NamedTemporaryFile(suffix='.wav', delete=False); f.write(wav); f.close()
        s.p = subprocess.Popen(['afplay', f.name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        threading.Timer(seconds + 5, lambda: os.path.exists(f.name) and os.remove(f.name)).start()

    def hush(s):
        if s.p and s.p.poll() is None:
            try: s.p.terminate()
            except Exception: pass


class MacMic(Device):
    kind = 'mac'

    def __init__(s):
        super().__init__('mac-mic', "Mac's microphone", caps=('ears',), place='desk')

    def start(s):
        threading.Thread(target=s._run, daemon=True).start()

    def _run(s):
        import numpy as np, sounddevice as sd
        while True:
            if permission('mic') == 'no':
                s.detail = 'Not allowed: System Settings > Privacy & Security > Microphone > BITCH'; s._changed(); time.sleep(10); continue
            try:
                quiet = [0]
                def cb(data, frames, t, status):
                    x = data[:, 0].copy()
                    quiet[0] = quiet[0] + 1 if not x.any() else 0
                    if s.active('ears') and s.hub and s.hub.hearing: s.hub.hearing.feed(s.id, x)
                with sd.InputStream(samplerate=16000, channels=1, blocksize=512, dtype='float32', callback=cb):
                    s.detail = 'Listening'; s._changed()
                    while quiet[0] < 16000 // 512 * 20:          # 20 s of perfect silence usually means no permission yet
                        time.sleep(0.5)
                s.detail = 'Waiting for microphone permission'; s._changed()
            except Exception as e:
                s.detail = f'Microphone unavailable ({e})'; s._changed()
            time.sleep(5)

    def _changed(s):
        if s.hub: s.hub.devices_changed()


class MacCamera(Device):
    kind = 'mac'

    def __init__(s, source='0'):
        super().__init__('mac-camera', "Mac's camera", caps=('eyes',), place='desk')
        s.source = source
        s.latest = None            # newest frame (for snapshots)

    def start(s):
        threading.Thread(target=s._run, daemon=True).start()

    def snapshot(s, timeout=4.0):
        import cv2
        if s.latest is None: return None
        ok, jp = cv2.imencode('.jpg', s.latest, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return jp.tobytes() if ok else None

    def _run(s):
        import cv2
        video = not str(s.source).isdigit()
        asked = 0
        while True:
            if not config.get('camera_on'):
                s.detail = 'Camera paused'; s.latest = None; s._changed(); time.sleep(1); continue
            p = 'yes' if video else permission('camera')
            if p == 'no':
                s.detail = 'Not allowed: System Settings > Privacy & Security > Camera > BITCH'; s._changed(); time.sleep(10); continue
            if p == 'ask':                                   # opening it now would only give black frames
                if time.time() - asked > 120: ask_permission('camera'); asked = time.time()
                s.detail = 'Waiting for you to click Allow (camera) on the Mac'; s._changed(); time.sleep(2); continue
            cam = cv2.VideoCapture(s.source if video else int(s.source))
            if not cam.isOpened():
                s.detail = 'Waiting for the camera (click Allow if the Mac asks)'; s._changed(); time.sleep(5); continue
            cam.set(cv2.CAP_PROP_FRAME_WIDTH, 1280); cam.set(cv2.CAP_PROP_FRAME_HEIGHT, 720); cam.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            s.detail = 'Watching' if not video else f'Playing test video {os.path.basename(str(s.source))}'; s._changed()
            fps = cam.get(cv2.CAP_PROP_FPS) or 30; bad = 0
            while True:
                ok, fr = cam.read()
                if not ok:
                    if video: cam.set(cv2.CAP_PROP_POS_FRAMES, 0); continue      # test videos loop
                    bad += 1
                    if bad > 50: break
                    time.sleep(0.05); continue
                if not config.get('camera_on'): break          # paused: let go of the camera right away
                bad = 0; s.latest = fr
                if s.active('eyes') and s.hub and s.hub.sight: s.hub.sight.frame(s.id, fr)
                if video: time.sleep(1 / min(30, fps))                          # play at real speed
            cam.release(); s.detail = 'Camera stopped, retrying'; s._changed(); time.sleep(3)

    def _changed(s):
        if s.hub: s.hub.devices_changed()
