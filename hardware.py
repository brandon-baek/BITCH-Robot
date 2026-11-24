"""What hardware is plugged into the robot itself. brain.py reports this to the Mac in /ping, and the Mac's robot driver
(core/devices/robot.py) turns each part into one of BITCH's senses or outputs automatically:

  OLED screen   -> 'face'  (googly eyes, see face.py)
  USB mic       -> 'ears'  (the Mac pulls the sound from /mic and hears through the robot)
  camera        -> reported for later
Check what it finds:  python3 hardware.py
"""
import os, re, shlex, shutil, subprocess


def mic():
    """The first microphone ALSA knows about, like 'plughw:1,0', or None."""
    if os.environ.get('BITCH_MIC_CMD'): return 'test'
    if not shutil.which('arecord'): return None
    try: out = subprocess.run(['arecord', '-l'], capture_output=True, text=True, timeout=5).stdout
    except Exception: return None
    m = re.search(r'card (\d+):.*?device (\d+):', out)
    return f'plughw:{m.group(1)},{m.group(2)}' if m else None


def camera():
    for tool in ('rpicam-hello', 'libcamera-hello'):
        if shutil.which(tool):
            try:
                out = subprocess.run([tool, '--list-cameras'], capture_output=True, text=True, timeout=8).stdout
                if re.search(r'^\s*0\s*:', out, re.M): return 'pi-camera'
            except Exception: pass
    return 'usb' if os.path.exists('/dev/video0') else None


def speaker():
    return next((p for p in ('pw-play', 'paplay', 'aplay') if shutil.which(p)), None)


def mic_process():
    """A process that writes 16 kHz mono 16-bit sound to stdout (BITCH_MIC_CMD replaces it in tests)."""
    cmd = os.environ.get('BITCH_MIC_CMD')
    args = shlex.split(cmd) if cmd else ['arecord', '-q', '-D', mic() or 'default', '-f', 'S16_LE', '-r', '16000', '-c', '1', '-t', 'raw']
    return subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def detect():
    return {'mic': mic(), 'camera': camera(), 'speaker': speaker()}


if __name__ == '__main__':
    import face
    d = detect(); scr = face.open_screen()
    print(f"  OLED screen: {'yes' if scr else 'no'}\n  microphone:  {d['mic'] or 'no'}\n  camera:      {d['camera'] or 'no'}\n  speaker:     {d['speaker'] or 'no'}")
