"""Natural speech with a small, deterministic metallic layer."""
from contextlib import contextmanager
import io
import queue
import subprocess
import time
import tempfile
import threading
import wave

import numpy as np

from . import config
from .hub import hub

SR = 24000
playback = None
echo_reference = None
last_played = {}
STEP = .04
DELIVERY = {
    'neutral': (1.02, .183, 0.),
    'warm': (.96, .151, .018),
    'gentle': (.73, .116, -.032),
    'upbeat': (1.18, .205, .04),
    'emphatic': (1.25, .218, .015),
    'playful': (1.14, .19, .028),
}
pipeline = None
pipeline_lock = threading.Lock()


VOICE_RATE = {'gentle': .93, 'emphatic': 1.07, 'upbeat': 1.06, 'playful': 1.045}


def synthesize(text, speed=1.):
    global pipeline
    with pipeline_lock:
        if pipeline is None:
            try:
                from kokoro import KPipeline
                pipeline = KPipeline(lang_code='a', repo_id='hexgrad/Kokoro-82M')
            except Exception:
                pipeline = False
        if pipeline:
            try:
                parts = [np.asarray(audio, dtype=np.float32).reshape(-1)
                         for _, _, audio in pipeline(text, voice=config.get('kokoro_voice'), speed=speed)]
                if parts:
                    return np.concatenate(parts), SR
            except Exception:
                pipeline = False
    with tempfile.TemporaryDirectory() as directory:
        path = directory + '/reply.wav'
        subprocess.run(['say', '-v', config.get('mac_voice'), '-r', str(round(185*speed)), '-o', path,
                        '--file-format=WAVE', '--data-format=LEI16@24000', '--', text],
                       timeout=25, check=True, capture_output=True)
        with wave.open(path, 'rb') as audio:
            samples = np.frombuffer(audio.readframes(audio.getnframes()), '<i2').astype(np.float32) / 32768
            return samples, audio.getframerate()


def render(text, ready_cue=False, delivery='neutral'):
    audio, rate = synthesize(text, VOICE_RATE.get(delivery, 1.))
    if not len(audio):
        raise ValueError('speech synthesis returned no audio')
    # Retain the clean voice and its prosody. Add a quiet metallic signal and one very short reflection.
    count = max(1, round(len(audio) * SR / (rate * 1.035)))
    clean = np.interp(np.linspace(0, len(audio) - 1, count), np.arange(len(audio)), audio)
    signal = clean * (0.93 + 0.07 * np.sin(2 * np.pi * 75 * np.arange(count) / SR))
    delay = round(0.004 * SR)
    signal[delay:] += 0.055 * clean[:-delay]
    signal *= 0.9 / max(float(np.abs(signal).max()), 0.2)
    if ready_cue:
        signal = np.concatenate((signal, np.zeros(round(SR * 0.12)), cue()))
    return encode(signal)


def note(hz, seconds, volume=.27):
    t = np.arange(round(SR * seconds)) / SR
    return volume * np.sin(np.pi * t / seconds) ** 2 * np.sin(2 * np.pi * hz * t)


def cue():
    return np.concatenate((note(620, .105), np.zeros(round(SR * .035)), note(870, .16)))


def wait_sound(index):
    if index == 0:
        return np.concatenate((note(485, .10, .19), note(690, .11, .18)))
    if index == 1:
        return np.concatenate((note(360, .14, .17), note(500, .15, .16)))
    return note(430, .22, .16)


def encode(signal):
    # An amplitude taper gives the lower half of the slider useful room-volume control.
    gain = (config.get('volume') / 100) ** 1.65
    result = io.BytesIO()
    with wave.open(result, 'wb') as file:
        file.setparams((1, 2, SR, 0, 'NONE', 'not compressed'))
        file.writeframes((signal * gain * 32767).astype('<i2').tobytes())
    return result.getvalue(), len(signal) / SR


def warm():
    def load():
        try:
            render('Hello there.')
        except Exception as error:
            hub.note('voice', f'Speech synthesis unavailable: {error}')

    threading.Thread(target=load, daemon=True).start()


def preroll(pcm):
    # The speaker's Bluetooth amp wakes more reliably on a signal than on digital silence.
    t = np.arange(round(SR * 2.45)) / SR
    pulse = .68 + .32 * np.sin(2 * np.pi * 2.3 * t)
    fade = np.minimum(1., np.minimum(t, t[-1] - t) * 25)
    level = .06 * (config.get('volume') / 100) ** 1.65
    lead = np.asarray(32767 * level * fade * pulse * np.sin(2 * np.pi * 195 * t), dtype='<i2')
    return lead.tobytes() + pcm, len(t) / SR


def motion_track(pcm):
    """Return the syllable envelope and spaced pitch accents from the rendered speech."""
    signal = np.frombuffer(pcm, '<i2').astype(np.float32)
    width = round(STEP * SR)
    chunks = np.pad(signal, (0, (-len(signal)) % width)).reshape(-1, width)
    rms = np.sqrt(np.mean(chunks**2, axis=1))
    level = np.clip((rms / max(float(np.percentile(rms, 90)), float(rms.max()) * .35, 1.) - .10) / .9, 0, 1)
    nod = np.zeros(len(level))
    peaks = []
    for i in range(1, len(level) - 1):
        if level[i] >= .38 and level[i-1] < level[i] >= level[i+1]:
            if peaks and i - peaks[-1] < 7:
                # Make one readable nod for a group of closely spaced syllables.
                if level[i] > level[peaks[-1]] + .11 and i + 1 < len(level):
                    peaks[-1] = i
            else:
                peaks.append(i)
    for i in peaks:
        begins_phrase = i < 7 or max(level[max(0, i-10):max(1, i-3)], default=0.) < .25
        strength = min(1.12, .55 + .43 * level[i] + .14 * begins_phrase)
        for offset, value in enumerate((.26, .65, .25, -.45, -1., -.85, -.42, .08), -2):
            if 0 <= i + offset < len(nod):
                nod[i + offset] += value * strength
    return level, nod


def is_echo(audio, received):
    """Look for delayed playback in several voiced blocks of microphone audio."""
    current = echo_reference
    if not current or len(audio) < 1600:
        return False
    start, pcm = current
    samples = audio[-12800:].astype(float)[::4]
    index = round((received-start)*4000)
    matches = []
    for end in range(len(samples), 479, -480):
        sample = samples[end-480:end].copy()
        sample -= np.mean(sample)
        norm = np.linalg.norm(sample)
        if norm < .02:
            continue
        center = index - len(samples) + end
        x = pcm[max(0, center-9200):max(0, center+960)]
        if len(x) < len(sample):
            continue
        corr = np.correlate(x, sample, 'valid')
        power = np.cumsum(np.r_[0, x*x])
        n = len(sample)
        denom = norm * np.sqrt(np.maximum(power[n:]-power[:-n], 1e-9))
        matches.append(float(np.max(np.abs(corr)/denom)))
    return bool(matches and (matches[0] > .72 or sum(score > .55 for score in matches[:4]) >= 2))


def articulation():
    current = playback
    if current is None:
        return 0., False, 0.
    started, level, nod, lead, motion_start, motion_end, cue_at, delivery = current
    elapsed = time.monotonic() - started - lead
    chirping = cue_at is not None and cue_at <= elapsed < cue_at + .31
    if not motion_start <= elapsed < motion_end:
        return 0., chirping, 0.
    sample = (elapsed - motion_start) / STEP
    index = min(int(sample), len(level)-1)
    following = min(index+1, len(level)-1)
    fraction = sample-int(sample)
    strength, amplitude, posture = DELIVERY[delivery]
    energy = level[index]*(1-fraction) + level[following]*fraction
    pitch = nod[index]*(1-fraction) + nod[following]*fraction
    return min(1., float(energy)*strength), chirping, posture + amplitude*float(pitch)


@contextmanager
def thinking(stop, *, greeting):
    """Play sparse cues during generation; finish the active cue before speech starts."""
    done = threading.Event()

    def sound():
        try:
            previous = 0.
            schedule = (0., 3.4, 7.6) if greeting else (.85, 3.9, 8.1)
            for index, when in enumerate(schedule):
                if done.wait(max(0., when - previous)) or stop.is_set():
                    break
                started = time.monotonic()
                play(*encode(wait_sound(index if greeting else index+1)), stop, interim=True)
                previous = when + time.monotonic() - started
        except (OSError, ValueError, wave.Error) as error:
            hub.note('speaker', f'Cue failed: {error}')

    speaker = hub.selected('voice')
    # Fast turns can skip the feedback worker when muted or running without output.
    worker = None
    if speaker and not config.get('muted') and config.get('volume'):
        worker = threading.Thread(target=sound, daemon=True)
        worker.start()
    def finish():
        done.set()
        if worker:
            worker.join()

    try:
        yield finish
    finally:
        finish()


class ReplyStream:
    """Prepare the next phrase while the previous phrase is playing."""
    def __init__(self, stop, finish_thinking, on_speech):
        self.stop = stop
        self.finish_thinking = finish_thinking
        self.on_speech = on_speech
        self.pending = queue.Queue()
        self.audio = queue.Queue()
        self.parts = []
        self.workers = ()
        self.closed = False

    def offer(self, phrase, delivery, laugh_first=False):
        if self.stop.is_set():
            return
        if not self.workers:
            self.workers = tuple(threading.Thread(target=work, daemon=True)
                                 for work in (self.synthesize, self.play))
            for worker in self.workers:
                worker.start()
        if phrase:
            self.parts.append(phrase)
        self.pending.put((phrase, delivery, laugh_first))

    def synthesize(self):
        try:
            while (part := self.pending.get()) is not None and not self.stop.is_set():
                phrase, delivery, laugh_first = part
                wav = render(phrase, delivery=delivery) if phrase else encode(cue())
                self.audio.put((wav, phrase, delivery, laugh_first))
        except (OSError, RuntimeError, ValueError) as error:
            hub.note('voice', f'Speech synthesis unavailable: {error}')
        finally:
            self.audio.put(None)

    def play(self):
        first = True
        while (part := self.audio.get()) is not None and not self.stop.is_set():
            wav, phrase, delivery, laugh_first = part
            if first:
                self.finish_thinking()
                if laugh_first:
                    laugh(self.stop)
                first = False
            if self.stop.is_set():
                break
            if phrase:
                self.on_speech(phrase)
            play(*wav, self.stop, text=bool(phrase), delivery=delivery, segment=True)

    def finish(self, reply, delivery, ready_cue):
        spoken = ' '.join(self.parts)
        if reply.startswith(spoken):
            remaining = reply[len(spoken):].strip()
            if remaining:
                self.offer(remaining, delivery)
        if ready_cue:
            self.offer('', delivery)
        self.close()

    def close(self):
        if self.workers and not self.closed:
            self.closed = True
            self.pending.put(None)
            for worker in self.workers:
                worker.join()


def laugh(stop):
    if stop.is_set() or config.get('muted') or not config.get('volume'):
        return
    # These soft pulses and the animated shoulder bounce share the same audio clock.
    bursts = [note(hz, .14, .18) + note(hz*2, .14, .065) for hz in (310, 277, 238)]
    breath = np.concatenate([np.r_[burst, np.zeros(round(SR*.065))] for burst in bursts])
    play(*encode(breath), stop, text=True, delivery='playful', interim=True)


def say(text, stop, *, ready_cue=False, delivery='neutral'):
    if config.get('muted') or not config.get('volume'):
        return
    wav, duration = render(text, ready_cue=ready_cue, delivery=delivery) if text else encode(cue())
    play(wav, duration, stop, text=bool(text), ready_cue=ready_cue, delivery=delivery)


def play(wav, duration, stop, *, text=False, ready_cue=False, delivery='neutral', interim=False, segment=False):
    global playback, echo_reference
    speaker = hub.selected('voice')
    if stop.is_set() or config.get('muted') or not config.get('volume'):
        return
    if not speaker:
        if not interim:
            hub.note('speaker', 'No speaker available. Choose Auto or Mac speaker in Connections.')
        return
    with wave.open(io.BytesIO(wav)) as file:
        pcm = file.readframes(file.getnframes())
    lead = 0.
    # A short output tail lets the Bluetooth transport drain the final syllable.
    tail = (.22 if segment else .44) if speaker.id == 'robot' else .12
    frames = pcm
    if speaker.id == 'robot' and time.monotonic() - last_played.get(speaker, -float('inf')) >= 20:
        if not getattr(speaker, 'audio_warm', False):
            frames, lead = preroll(pcm)
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as file:
        file.setparams((1, 2, SR, 0, 'NONE', 'not compressed'))
        file.writeframes(frames + bytes(round(SR*tail)*2))
    output = buffer.getvalue()
    if text:
        hub.status = 'Speaking'
    start = time.monotonic()
    if not speaker.speak(output):
        if stop.is_set():
            return
        if config.get('speaker_source') != 'auto' or speaker.id != 'robot':
            hub.note('speaker', 'The selected speaker could not play the reply.')
            return
        speaker = hub.get('mac-speaker')
        lead = 0.
        start = time.monotonic()
        if not speaker or not speaker.speak(wav):
            hub.note('speaker', 'The Pi could not play the reply.')
            return
    if stop.is_set():
        speaker.hush()
        return
    hub.note('speaker', None)
    cue_at = max(0., duration-.30) if ready_cue else 0. if interim and not text else None
    speech_end = max(0., duration-.42) if text and ready_cue else duration if text else 0.
    if speech_end > 0:
        envelope, nod = motion_track(pcm[:round(speech_end*SR)*2])
    else:
        envelope = nod = ()
    playback = start, envelope, nod, lead, 0., speech_end, cue_at, delivery
    echo_reference = (start+lead, np.frombuffer(pcm, '<i2')[::6].astype(float))
    early = False
    try:
        while not stop.wait(.06):
            playing = speaker.playing()
            elapsed = time.monotonic() - start
            if playing is False:
                early = elapsed < duration + lead - .30
                break
            if playing is None and elapsed >= duration + lead + tail + .65:
                break
            if elapsed > duration + lead + 5:
                speaker.hush()
                break
        if stop.is_set():
            speaker.hush()
    finally:
        playback = echo_reference = None
        if not early:
            last_played[speaker] = time.monotonic()
    if early and not stop.is_set() and not interim:
        backup = hub.get('mac-speaker') if speaker.id == 'robot' and config.get('speaker_source') == 'auto' else None
        if backup and backup.speak(wav):
            hub.note('speaker', 'Pi playback stopped early. Replayed the reply on the Mac speaker.')
            playback = time.monotonic(), envelope, nod, 0., 0., speech_end, cue_at, delivery
            echo_reference = (time.monotonic(), np.frombuffer(pcm, '<i2')[::6].astype(float)) if text else None
            if stop.wait(duration + .30):
                backup.hush()
            playback = echo_reference = None
        else:
            hub.note('speaker', 'Playback ended early. Check the speaker connection.')
    if not interim and not segment and not stop.is_set():
        time.sleep(.26 if ready_cue else .43)
