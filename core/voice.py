"""Natural speech with a small, deterministic metallic layer."""
from contextlib import contextmanager
import io
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
last_played = {}
STEP = .04
DELIVERY = {
    'neutral': (1.0, .105, 0.),
    'warm': (.9, .09, .018),
    'gentle': (.66, .065, -.032),
    'upbeat': (1.18, .13, .04),
    'emphatic': (1.25, .145, .015),
    'playful': (1.14, .12, .028),
}
pipeline = None
pipeline_lock = threading.Lock()


def synthesize(text):
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
                         for _, _, audio in pipeline(text, voice=config.get('kokoro_voice'), speed=1.0)]
                if parts:
                    return np.concatenate(parts), SR
            except Exception:
                pipeline = False
    with tempfile.TemporaryDirectory() as directory:
        path = directory + '/reply.wav'
        subprocess.run(['say', '-v', config.get('mac_voice'), '-r', '185', '-o', path,
                        '--file-format=WAVE', '--data-format=LEI16@24000', '--', text],
                       timeout=25, check=True, capture_output=True)
        with wave.open(path, 'rb') as audio:
            samples = np.frombuffer(audio.readframes(audio.getnframes()), '<i2').astype(np.float32) / 32768
            return samples, audio.getframerate()


def render(text, ready_cue=False):
    audio, rate = synthesize(text)
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
    try:
        yield
    finally:
        done.set()
        if worker:
            worker.join()


def say(text, stop, *, ready_cue=False, delivery='neutral'):
    if config.get('muted') or not config.get('volume'):
        return
    wav, duration = render(text, ready_cue=ready_cue) if text else encode(cue())
    play(wav, duration, stop, text=bool(text), ready_cue=ready_cue, delivery=delivery)


def play(wav, duration, stop, *, text=False, ready_cue=False, delivery='neutral', interim=False):
    global playback
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
    output = wav
    if speaker.id == 'robot' and time.monotonic() - last_played.get(speaker, -float('inf')) >= 20:
        frames, lead = preroll(pcm)
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as file:
            file.setparams((1, 2, SR, 0, 'NONE', 'not compressed'))
            file.writeframes(frames)
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
    cue_at = max(0., duration-.30) if ready_cue or interim else None
    if interim:
        cue_at = 0.
    speech_end = max(0., duration-.42) if text and ready_cue else duration if text else 0.
    if speech_end > 0:
        envelope, nod = motion_track(pcm[:round(speech_end*SR)*2])
    else:
        envelope = nod = ()
    playback = start, envelope, nod, lead, 0., speech_end, cue_at, delivery
    try:
        while not stop.wait(.06):
            playing = speaker.playing()
            elapsed = time.monotonic() - start
            if playing is False or playing is None and elapsed >= duration + lead + .65:
                break
            if elapsed > duration + lead + 5:
                speaker.hush()
                break
        if stop.is_set():
            speaker.hush()
    finally:
        playback = None
        last_played[speaker] = time.monotonic()
    if not interim:
        time.sleep(.26 if ready_cue else .43)
