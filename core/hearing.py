"""BITCH's hearing: turns sound from any 'ears' device into words.

  - Silero VAD tells real speech apart from claps, music and typing. One detector per device.
  - A 0.8 s rolling memory means the first words are never cut off.
  - You can talk over BITCH: strong speech while it is talking stops it (barge-in).
  - Push-to-talk (holding the button on the phone) skips all of that: everything you say while holding is heard.
  - Whisper large-v3-turbo runs on the Mac (MLX). One transcription at a time.
"""
import queue, re, threading, time
import numpy as np
from . import config
from .hub import hub

SR, CH = 16000, 512
WHISPER = 'mlx-community/whisper-large-v3-turbo'
JUNK = re.compile(r"^\W*(you|thank you\.?|thanks for watching\.?|bye\.?|\.+|uh+|um+|hmm+|mm+|okay\.?)\W*$", re.I)


class Stream:
    """Speech detection for one device's microphone."""

    def __init__(s, ears, dev_id):
        s.ears, s.dev = ears, dev_id
        s.vad = ears.new_vad()
        s.left = np.zeros(0, np.float32)
        s.pre = []; s.buf = []; s.talking = False; s.vc = 0; s.on_run = 0; s.off_run = 0; s.barge = 0
        s.echo = 0.004; s.floor = 0.003
        s.ptt = None                 # a list while push-to-talk is held

    def prob(s, x):
        if s.vad is None:
            r = float(np.sqrt(np.mean(x * x))); return 1.0 if r > max(0.008, s.floor * 2.8) else 0.0
        import torch
        return float(s.vad(torch.from_numpy(x), SR).item())

    def push(s, x):
        x = np.concatenate([s.left, x.astype(np.float32)])
        n = len(x) // CH * CH
        for i in range(0, n, CH): s.step(x[i:i + CH])
        s.left = x[n:]

    def step(s, x):
        r = float(np.sqrt(np.mean(x * x)))
        if s.dev in ('mac-mic', 'robot'): s.ears.level(min(1.0, r * 12))
        if s.ptt is not None:
            s.ptt.append(x); return
        if (s.dev == 'mac-mic' and not config.get('mic_on')) or hub.status in ('thinking', 'understanding'):
            s.pre = []; return
        p = s.prob(x)
        speaking = time.time() < hub.speaking_until
        if not s.talking:
            if p < 0.3: s.floor = 0.985 * s.floor + 0.015 * r
            s.pre = (s.pre + [x])[-25:]                                # ~0.8 s of memory before speech starts
            if speaking:
                if p < 0.5: s.echo = 0.97 * s.echo + 0.03 * r          # how loud BITCH's own voice is at this mic
                s.barge = s.barge + 1 if (p > 0.85 and r > s.echo * 2.2) else 0
                if s.barge >= 10:
                    hub.interrupt(); s.talking = True; s.buf = list(s.pre); s.off_run = 0; s.barge = 0
                return
            s.on_run = s.on_run + 1 if p > 0.6 else 0
            if s.on_run >= 2:
                s.talking = True; s.buf = list(s.pre); s.off_run = 0; hub.set_status('hearing')
        else:
            s.buf.append(x); s.off_run = s.off_run + 1 if p < 0.35 else 0; s.vc += p > 0.5
            if s.off_run >= 22 or len(s.buf) > int(15 * SR / CH):         # ~0.7 s of quiet ends the sentence
                s.talking = False; s.on_run = 0; audio = np.concatenate(s.buf); s.buf = []; s.pre = []
                voiced = s.vc * CH / SR if s.vad else 1; s.vc = 0
                if s.vad is not None: s.vad.reset_states()
                if len(audio) > SR * 0.4 and voiced > 0.25: s.ears.transcribe(s.dev, audio, 'ambient')
                elif hub.status == 'hearing': hub.set_status('asleep' if hub.asleep else 'listening')

    def hold(s, on):
        if on:
            hub.interrupt(); s.ptt = []; s.talking = False; s.buf = []; hub.set_status('hearing')
        elif s.ptt is not None:
            audio = np.concatenate(s.ptt + [s.left]) if s.ptt else np.zeros(0, np.float32); s.ptt = None; s.left = np.zeros(0, np.float32)
            if len(audio) > SR * 0.3: s.ears.transcribe(s.dev, audio, 'ptt')
            else: hub.set_status('listening')


class Hearing:
    def __init__(s):
        s.streams = {}
        s.q = queue.Queue()          # audio waiting for Whisper
        s.inq = queue.Queue()        # raw audio and push-to-talk events from devices
        s.ready = False
        s._lvl = 0; s._lvl_t = 0
        s._vad_ok = None

    def start(s):
        threading.Thread(target=s._worker, daemon=True).start()
        threading.Thread(target=s._listen, daemon=True).start()

    def _listen(s):
        """Speech detection runs here, never in a device's audio callback or the web server."""
        while True:
            kind, dev_id, data = s.inq.get()
            try:
                if kind == 'pcm':
                    if s.ready: s.stream(dev_id).push(data)
                else: s.stream(dev_id).hold(data)
            except Exception as e:
                print(f'  hearing: {e!r}', flush=True)

    def new_vad(s):
        try:
            import torch
            from silero_vad import load_silero_vad
            torch.set_num_threads(1); s._vad_ok = True
            return load_silero_vad()
        except Exception as e:
            if s._vad_ok is None: hub.note(f'speech detector unavailable, using loudness only ({e})')
            s._vad_ok = False; return None

    def stream(s, dev_id):
        if dev_id not in s.streams: s.streams[dev_id] = Stream(s, dev_id)
        return s.streams[dev_id]

    def feed(s, dev_id, pcm):
        """Called by ears devices with float32 audio at 16 kHz. Cheap: it only queues the audio."""
        if dev_id == 'mac-mic':
            r = hub.get('robot')
            if r and r.active('ears'): return               # the robot's own ears win at the desk (no double hearing)
        s.inq.put(('pcm', dev_id, pcm))

    def hold(s, dev_id, on):
        """Push-to-talk from a device."""
        s.inq.put(('hold', dev_id, on))

    def level(s, v):
        now = time.time()
        if now - s._lvl_t > 0.12 and abs(v - s._lvl) > 0.02:
            s._lvl, s._lvl_t = v, now; hub.level = v; hub.ui({'type': 'level', 'v': round(v, 2)})

    def transcribe(s, dev_id, audio, mode):
        s.q.put((dev_id, audio, mode))

    def _worker(s):
        import mlx_whisper
        hub.note('loading Whisper (the first time downloads about 1.6 GB)...', problem=False)
        try:
            mlx_whisper.transcribe(np.zeros(SR, np.float32), path_or_hf_repo=WHISPER)
        except Exception as e:
            hub.note(f'hearing failed to load: {e}'); return
        s.ready = True
        if hub.status in ('starting',): hub.set_status('listening')
        hub.note('ears ready', problem=False)
        owner = config.get('owner')
        while True:
            dev_id, audio, mode = s.q.get()
            hub.set_status('understanding')
            try:
                res = mlx_whisper.transcribe(audio, path_or_hf_repo=WHISPER, language='en', condition_on_previous_text=False,
                                             initial_prompt=f'Hey BITCH, how are you? {owner} is chatting with BITCH, a little desk robot.')
            except Exception as e:
                hub.note(f'could not understand that ({e})'); hub.set_status('listening'); continue
            segs = res.get('segments') or []
            good = [g for g in segs if g.get('no_speech_prob', 0) < 0.6 and g.get('avg_logprob', 0) > -1.1]
            txt = ' '.join(g['text'].strip() for g in good).strip() if segs else res.get('text', '').strip()
            if txt and not JUNK.match(txt):
                hub.emit('heard', text=txt, device=dev_id, mode=mode)
            else:
                hub.set_status('asleep' if hub.asleep else 'listening')
