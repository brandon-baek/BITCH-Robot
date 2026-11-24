"""BITCH's voice: turns text into sound and plays it on the right device.

Words come from Kokoro (natural, on-device) or the Mac's built-in voice, then get a small-creature pitch and a
touch of robot grit. 'babble' is the creature language from sounds.py. Long replies start playing after the
first sentence while the rest is still being made.
"""
import io, os, random, re, subprocess, sys, tempfile, threading, time, wave
from concurrent.futures import ThreadPoolExecutor
import numpy as np
from . import config
from .hub import hub

sys.path.insert(0, config.ROOT)
import sounds   # noqa: E402  (the robot's creature voice, shared with the Pi)

SR = 22050
_kok = [None]; _klock = threading.Lock()
_pool = ThreadPoolExecutor(1)


def warm():
    """Load the word voice in the background so the first reply isn't slow."""
    def go():
        try: render('Hi!', 'happy')
        except Exception as e: hub.note(f'word voice not ready: {e}')
    threading.Thread(target=go, daemon=True).start()


def tts(text):
    with _klock:
        if _kok[0] is None:
            try:
                from kokoro import KPipeline
                _kok[0] = KPipeline(lang_code='a', repo_id='hexgrad/Kokoro-82M')
            except Exception as e:
                _kok[0] = False; hub.note(f"using the Mac's voice for words (Kokoro: {e})", problem=False)
        if _kok[0]:
            parts = [a for _, _, a in _kok[0](text, voice=config.get('kokoro_voice'), speed=1.0)]
            a = np.concatenate([np.asarray(p, dtype=np.float32).reshape(-1) for p in parts]) if parts else np.zeros(2400, np.float32)
            return a, 24000
    f = tempfile.NamedTemporaryFile(suffix='.wav', delete=False); f.close()
    subprocess.run(['say', '-v', config.get('mac_voice'), '-r', '185', '-o', f.name, '--file-format=WAVE', '--data-format=LEI16@22050', text],
                   check=True, capture_output=True)
    with wave.open(f.name) as w: a = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
    os.remove(f.name)
    return a, 22050


CHIRP = {'happy': 'happy', 'excited': 'excited', 'sad': 'sad', 'love': 'love', 'curious': 'curious', 'confused': 'question',
         'scared': 'whimper', 'proud': 'tada', 'angry': 'grumble'}


def words(text, emotion, chirp=True):
    a, sr = tts(text)
    R = float(config.get('robotness'))
    up = 1 + 0.16 * R * {'excited': 1.3, 'happy': 1.15, 'scared': 1.3, 'sad': 0.7, 'sleepy': 0.6, 'angry': 0.6}.get(emotion, 1.0)
    n = int(len(a) * SR / (sr / up)); y = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a)   # small-creature pitch
    y = y / (np.abs(y).max() + 1e-6)
    if R > 0:
        y = np.tanh((1 + 1.2 * R) * y) / np.tanh(1 + 1.2 * R)                       # warm grit
        t = np.arange(len(y)) / SR; y = y * (1 + 0.05 * R * np.sin(2 * np.pi * 38 * t))  # faint electronic shimmer
        q = int(64 - 40 * R); y = np.round(y * q) / q                               # digital edge
    tail = []
    c = CHIRP.get(emotion)
    if chirp and c and R > 0.15 and random.random() < 0.6: tail = [0.55 * v for v in sounds.V[c]()]
    return list(y) + [0.0] * int(0.07 * SR) + tail


def render(text, emotion='neutral', chirp=True):
    """Text -> (WAV bytes, seconds)."""
    s = None
    if config.get('voice') == 'words':
        try: s = words(text, emotion, chirp)
        except Exception as e: hub.note(f'word voice failed, babbling this time ({e})')
    if s is None: s = sounds._fx(sounds.speak_audio(text, emotion)[0])
    a = np.asarray(s, dtype=np.float32); a = np.clip(a * (0.9 / max(1e-6, float(np.abs(a).max()))), -1, 1)
    b = io.BytesIO()
    with wave.open(b, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes((a * 32000).astype('<i2').tobytes())
    return b.getvalue(), len(a) / SR


def chunks(text):
    """First sentence on its own (so BITCH starts talking quickly), then the rest in bigger pieces."""
    ss = [x.strip() for x in re.split(r'(?<=[.!?])\s+', text.strip()) if x.strip()]
    if len(ss) <= 1: return ss
    out = [ss[0]]; cur = ''
    for x in ss[1:]:
        if cur and len(cur) + len(x) > 220: out.append(cur); cur = x
        else: cur = (cur + ' ' + x).strip()
    if cur: out.append(cur)
    return out


_say_lock = threading.Lock()


def say(text, emotion='neutral', move='none', origin=None, speak=True):
    """Say something: voice on the right device, body language on the robot, BITCH's face in the app.
    Blocks until it has finished talking (or was interrupted)."""
    text = (text or '').strip()
    targets = hub.voices_for(origin, speak) if text and not config.get('muted') else []
    bodies = [b for b in hub.bodies() if b not in targets]
    hub.ui({'type': 'express', 'emotion': emotion, 'move': move, 'text': text})
    for b in bodies: b.gesture(emotion, move)
    if not targets: return True
    with _say_lock:
        hub.interrupted = False
        parts = chunks(text)
        nxt = _pool.submit(render, parts[0], emotion, len(parts) == 1)
        for i in range(len(parts)):
            wav, secs = nxt.result()
            if i + 1 < len(parts): nxt = _pool.submit(render, parts[i + 1], emotion, i + 2 == len(parts))
            if hub.interrupted: break
            hub.set_status('speaking')
            hub.speaking_until = time.time() + secs + 0.35
            last = i + 1 == len(parts)
            for d in targets: d.speak(wav, secs, parts[i], emotion, move if last else 'none')
            hub.ui({'type': 'talking', 'seconds': secs})
            while time.time() < hub.speaking_until and not hub.interrupted: time.sleep(0.03)
        done = not hub.interrupted
        hub.speaking_until = 0
        if hub.status == 'speaking': hub.set_status('asleep' if hub.asleep else 'listening')
        return done
