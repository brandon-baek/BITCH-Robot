"""Natural speech with a small, deterministic metallic layer."""
from contextlib import contextmanager
import io
import queue
import subprocess
import time
import tempfile
import threading
import wave

import re

import numpy as np

from . import config
from .hub import hub

SR = 24000
playback = None
echo_reference = None
recent_echo = None
last_played = {}
output_delay = .25   # seconds from sending audio to hearing it in the room; learned by hearing.EchoGate
MOTION_LEAD = .12    # network and servo spring lag between a motion command and visible movement
NOD_GAIN = 1.55      # how big the syllable nods are (was 1.12; asked for more nodding)
gesture_track = []
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
# MLX (BITCH's voice, Parakeet, Whisper) is not safe to run from two threads at once. Everything that uses it takes this
# lock, a step at a time: the voice holds it per chunk (about 0.2 s), so a transcription can slip in between.
MLX = threading.RLock()


def limit_mlx_cache():
    """MLX keeps freed GPU buffers for reuse, and with three models (hearing, voice) that cache grows to gigabytes.
    Memory is what makes words stutter on this Mac, so keep the cache small."""
    try:
        import mlx.core as mx
        mx.set_cache_limit(384 * 1024 * 1024)
    except Exception:
        pass
_urgent = [0]          # how many transcriptions are waiting for the GPU right now


class Urgent:
    """Hearing goes first: while a transcription waits, BITCH's voice steps aside between chunks. Without this the
    voice re-took the lock right after each chunk and a barge-in transcript waited until the whole reply was done."""
    waited = 0.   # how long the last urgent caller waited for the GPU (for latency checks)

    def __enter__(self):
        _urgent[0] += 1
        started = time.monotonic()
        MLX.acquire()
        Urgent.waited = time.monotonic() - started
        _urgent[0] -= 1
        return self

    def __exit__(self, *exc):
        MLX.release()


def mlx_steps(generator):
    """Run an MLX generator one step at a time under the MLX lock, yielding to any waiting transcription."""
    while True:
        while _urgent[0]:
            time.sleep(.003)
        with MLX:
            try:
                item = next(generator)
            except StopIteration:
                return
        yield item


VOICE_RATE = {'gentle': .93, 'emphatic': 1.07, 'upbeat': 1.06, 'playful': 1.045}
# Mood also moves the pitch and the loudness, so a sad line sounds lower and softer, not just slower.
VOICE_PITCH = {'gentle': .965, 'warm': .985, 'emphatic': .985, 'upbeat': 1.055, 'playful': 1.04}
VOICE_GAIN = {'gentle': .8, 'warm': .92}


spoken = threading.local()   # word start times from the last synthesis on this thread

# BITCH's own voice: Qwen3-TTS cloned from one locked reference take (core/voices/bitch.wav), with a warm EQ.
# Chosen by ear over five rounds of auditions; the reference keeps it the same character on every line.
# The voice is cloned from the reference clip, so model size changes speed more than sound: against the reference,
# 0.6B and 1.7B (8-bit) both match at 0.989. While the brain is also generating on the GPU, 1.7B makes speech at
# 0.88x real time (too close to stutter-free) and 0.6B at 0.70x, so 0.6B is the default. 'voice_model' picks.
QWEN_MODELS = {'0.6B': 'mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit', '1.7B': 'mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit'}
REFERENCE = config.ROOT / 'core' / 'voices' / 'bitch.wav'
qwen = None          # the loaded model, or False if it can't run (then Kokoro speaks)


def warm_eq(audio, rate=SR):
    """A little more body in the lower voice (+3 dB around 180-450 Hz) and a softer top. The "Warmer" voice."""
    from scipy.signal import butter, sosfilt

    body = sosfilt(butter(2, [180, 450], 'bandpass', fs=rate, output='sos'), audio)
    top = sosfilt(butter(1, 7000, 'lowpass', fs=rate, output='sos'), audio)
    return (audio + 0.41 * body - 0.2 * (audio - top)).astype(np.float32)


def estimate_words(text, audio, rate):
    """When each word starts, for word-timed gestures, from where the speech actually sounds.

    Qwen doesn't report timings like Kokoro does, so spread the words over the voiced part of the audio by
    syllable count, with a little room for pauses at commas and full stops."""
    tokens = re.findall(r"[\w'’-]+|[.,!?;:]", text)
    if not tokens or not len(audio):
        return []
    step = max(1, round(rate * .02))
    frames = audio[:len(audio) // step * step].reshape(-1, step)
    level = np.sqrt(np.mean(frames ** 2, axis=1)) if len(frames) else np.zeros(1)
    loud = np.flatnonzero(level > .12 * float(level.max() or 1))
    if not len(loud):
        return []
    start, end = loud[0] * step / rate, (loud[-1] + 1) * step / rate
    weight = lambda token: (.6 if token in '.!?' else .3 if token in ',;:' else
                            max(1, len(re.findall(r'[aeiouy]+', token.lower()))))
    total = sum(weight(token) for token in tokens)
    words, at = [], start
    for token in tokens:
        words.append((token, at))
        at += (end - start) * weight(token) / total
    return words


def load_qwen():
    global qwen
    if qwen is None:
        try:
            from mlx_audio.tts.utils import load_model

            limit_mlx_cache()
            with MLX:
                qwen = load_model(QWEN_MODELS.get(config.get('voice_model'), QWEN_MODELS['0.6B']))
            qwen.reference_text = (REFERENCE.with_suffix('.txt')).read_text().strip()
            # The reference voice never changes, so its speaker fingerprint is worked out once.
            extract, fingerprints = qwen.extract_speaker_embedding, {}

            def remembered(audio, *args, **kwargs):
                key = (audio.size, float(audio.sum()))
                if key not in fingerprints:
                    fingerprints[key] = extract(audio, *args, **kwargs)
                return fingerprints[key]
            qwen.extract_speaker_embedding = remembered
        except Exception as error:
            qwen = False
            hub.note('voice', f'BITCH’s voice model is unavailable, using the backup voice: {error}')
    return qwen


def unload():
    """Let go of the voice model (deep sleep). It loads again on the next line BITCH says."""
    global qwen
    with MLX:
        qwen = None
        warmed.clear()
        try:
            import mlx.core as mx
            mx.clear_cache()
        except Exception:
            pass


def synthesize_qwen(text, speed):
    model = load_qwen()
    if not model:
        return None
    # Streaming mode even for a whole line: the non-streaming decoder re-decodes the 8 s reference every time.
    chunks = [np.asarray(chunk.audio, dtype=np.float32).reshape(-1) for chunk in mlx_steps(model.generate(
        text=text, ref_audio=str(REFERENCE), ref_text=model.reference_text, lang_code='english', speed=speed,
        stream=True, streaming_interval=.32))]
    if not chunks:
        return None
    audio = warm_eq(np.concatenate(chunks))
    spoken.words = estimate_words(text, audio, SR)
    return audio, SR


def lift():
    """The metallic layer used to raise the pitch 3.5%. BITCH's own voice skips that: it made it sound younger."""
    return 1. if config.get('voice_engine') == 'qwen' and qwen else 1.035


def synthesize(text, speed=1.):
    global pipeline, qwen
    spoken.words = []
    if config.get('voice_engine') == 'qwen' and qwen is not False:
        with pipeline_lock:
            try:
                result = synthesize_qwen(text, speed)
                if result is not None:
                    return result
            except Exception as error:
                hub.note('voice', f'BITCH’s voice failed on a line, using the backup voice: {error}')
    with pipeline_lock:
        if pipeline is None:
            try:
                from kokoro import KPipeline
                pipeline = KPipeline(lang_code='a', repo_id='hexgrad/Kokoro-82M')
            except Exception:
                pipeline = False
        if pipeline:
            try:
                parts, words, offset = [], [], 0.
                for result in pipeline(text, voice=config.get('kokoro_voice'), speed=speed):
                    audio = np.asarray(result.audio, dtype=np.float32).reshape(-1)
                    # Kokoro reports when each word starts, which lets gestures land on it.
                    words += [(token.text, offset + token.start_ts) for token in (result.tokens or ())
                              if token.start_ts is not None]
                    parts.append(audio)
                    offset += len(audio) / SR
                if parts:
                    spoken.words = words
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


def trim(audio, rate):
    """Cut the silence the synthesizer pads around every phrase.

    Kokoro adds about 0.35 s before and 0.5 s after each phrase. Untrimmed, every reply
    starts late and every sentence break turns into a second of dead air."""
    start, end = trim_bounds(audio, rate)
    return np.asarray(audio, dtype=np.float32)[start:end]


def trim_bounds(audio, rate):
    audio = np.asarray(audio, dtype=np.float32)
    loud = np.flatnonzero(np.abs(audio) >= max(.004, .02 * float(np.abs(audio).max()))) if len(audio) else ()
    if not len(loud):
        return 0, len(audio)
    return max(0, loud[0] - round(.04 * rate)), loud[-1] + round(.1 * rate)


def render(text, ready_cue=False, delivery='neutral'):
    pitch = VOICE_PITCH.get(delivery, 1.)
    # Speeding playback raises the pitch, so ask the synthesizer for the rest of the pace.
    spoken.words = []
    audio, rate = synthesize(text, VOICE_RATE.get(delivery, 1.) / pitch)
    start, end = trim_bounds(audio, rate)
    audio = np.asarray(audio, dtype=np.float32)[start:end]
    if not len(audio):
        raise ValueError('speech synthesis returned no audio')
    words = [(word, (at - start / rate) / (lift() * pitch)) for word, at in getattr(spoken, 'words', ())]
    # Retain the clean voice and its prosody. Add a quiet metallic signal and one very short reflection.
    count = max(1, round(len(audio) * SR / (rate * lift() * pitch)))
    clean = np.interp(np.linspace(0, len(audio) - 1, count), np.arange(len(audio)), audio)
    signal = clean * (0.93 + 0.07 * np.sin(2 * np.pi * 75 * np.arange(count) / SR))
    delay = round(0.004 * SR)
    signal[delay:] += 0.055 * clean[:-delay]
    signal *= 0.9 * VOICE_GAIN.get(delivery, 1.) / max(float(np.abs(signal).max()), 0.2)
    if ready_cue:
        signal = np.concatenate((signal, np.zeros(round(SR * 0.12)), cue()))
    render.signal = signal   # kept so a line can be saved before the volume is applied
    result = encode(signal)
    remember_gestures(result[0], gestures(words))
    return result


# Lines BITCH says while its voice model is out of memory (deep sleep, waking up). Recorded in its own voice the
# first time the voice is loaded, then played from disk, so they need no model, no GPU and no internet.
RECORDED = {
    'waking_short': 'One sec, I’m waking up.',
    'waking': 'Give me about {} seconds, I’m still waking up.',
    'waking_long': 'Hang on, waking up. This one takes about half a minute.',
    'almost': 'Almost there.',
    'cant_wake': 'My memory’s too full to wake up right now. Close something heavy and call me again.',
    'memory_nap': 'Your Mac’s running low on memory, so I’m taking a nap. Say my name if you need me.',
}
WAKING_SECONDS = (10, 15, 20)


def recorded_path(key):
    return config.HOME / 'lines' / f'{key}.wav'


def record_lines():
    """Save the sleep and waking lines in BITCH's own voice (once; cheap to check afterwards)."""
    import soundfile

    folder = config.HOME / 'lines'
    folder.mkdir(parents=True, exist_ok=True)
    wanted = {key: text for key, text in RECORDED.items() if '{}' not in text}
    wanted.update({f'waking_{n}': RECORDED['waking'].format(n) for n in WAKING_SECONDS})
    for key, text in wanted.items():
        path = recorded_path(key)
        if path.exists():
            continue
        try:
            render(text, delivery='warm')
            soundfile.write(str(path), np.asarray(render.signal, dtype=np.float32), SR)
        except Exception as error:
            hub.note('voice', f'Could not record "{text}": {error}')
            return


def say_recorded(key, stop=None, wait=True):
    """Play a saved line at the current volume (or a soft chirp if it hasn't been recorded yet)."""
    path = recorded_path(key)
    if not path.exists():
        earcon('waking', force=True)
        return False
    try:
        import soundfile

        signal, _ = soundfile.read(str(path), dtype='float32')
        wav, duration = encode(signal)
        text = next((line for name, line in RECORDED.items() if key.startswith(name)), '')
        if text:
            hub.chat('robot', text.format(key.rsplit('_', 1)[-1]) if '{}' in text else text)
        if wait:
            play(wav, duration, stop or threading.Event(), text=True, delivery='warm')
        else:
            threading.Thread(target=play, args=(wav, duration, stop or threading.Event()),
                             kwargs={'text': True, 'delivery': 'warm'}, daemon=True).start()
        return True
    except Exception:
        return False


AFFIRM = {'yes', 'yeah', 'yep', 'yup', 'right', 'exactly', 'sure', 'totally', 'absolutely', 'definitely',
          'correct', 'agreed', 'true', 'okay', 'ok', 'uh-huh'}
NEGATE = {'no', 'nope', 'nah', 'not', "don't", "can't", "won't", "isn't", "aren't", 'never', "didn't", "doesn't",
          "wasn't", "shouldn't", "couldn't", "wouldn't", "haven't", 'nothing', 'nobody'}
EMPHASIS = {'really', 'very', 'so', 'super', 'huge', 'tiny', 'always', 'seriously', 'literally', 'actually',
            'way', 'every', 'all', 'best', 'worst', 'love', 'hate', 'amazing', 'terrible'}
YOU = {'you', 'your', "you're", 'yours', 'yourself', "you've", "you'll"}
SELF = {'i', 'me', 'my', "i'm", "i've", "i'll", "i'd", 'myself'}
THINK = {'hmm', 'well', 'um', 'uh', 'maybe', 'perhaps', 'guess', 'probably', 'honestly'}
STEPS = {'first', 'second', 'third', 'then', 'next', 'finally', 'also', 'plus', 'last'}   # a small beat per list item


def known_names():
    try:
        from .people import people
        return {name.casefold() for name in people.names('face') | people.names('voice')}
    except Exception:
        return set()
GESTURES = {}   # rendered clip -> its word gestures, so the motion travels with the audio


def gestures(words):
    """Turn word timings into body language: nod on yes, shake on no, tilt on a question,
    lean in on "you" and back on "I"."""
    events, shook, opening = [], False, True
    names = known_names()
    for index, (word, at) in enumerate(words):
        plain = word.casefold().strip('.,!?;:"“”‘’\'').replace('’', "'")
        possessive = plain[:-2] if plain.endswith("'s") else plain
        if possessive in names:
            # Naming someone in the room: glance at them (the eyes find them in the camera).
            events.append(('glance', at - .15, 1.1, possessive))
        if word.strip() == '?':
            start = next((t for w, t in reversed(words[:index]) if w.strip() in '.!'), words[0][1])
            events.append(('tilt', max(start, at - .8), at - max(start, at - .8) + .45, 1.))
        elif plain in AFFIRM:
            events.append(('nod', at, .42, 1.))
        elif plain in NEGATE and not shook:
            events.append(('shake', at, .6, 1.))
            shook = True
        elif plain in EMPHASIS:
            events.append(('nod', at, .32, .65))
        elif plain in YOU:
            events.append(('lean', at, .75, 1.))
        elif plain in SELF and opening:
            events.append(('lean', at, .75, -.7))
        elif plain in THINK and opening:
            events.append(('tilt', at, .9, -.8))
        elif plain in STEPS and opening:
            events.append(('nod', at, .36, .55))
        if word.strip() in ('.', '!', '?'):
            shook, opening = False, True
        elif plain:
            opening = False
    return events


def remember_gestures(wav, events):
    GESTURES[wav] = events
    while len(GESTURES) > 24:
        GESTURES.pop(next(iter(GESTURES)))


def gesture_at(events, elapsed):
    """Extra (nod, tilt, lean) for the word gestures active at this point in the clip."""
    nod = tilt = lean = 0.
    for kind, at, length, strength in events:
        phase = (elapsed - at) / length
        if not 0 <= phase < 1 or kind == 'glance':
            continue
        swell = np.sin(np.pi * phase)
        if kind == 'nod':
            nod -= .15 * strength * swell
        elif kind == 'shake':
            tilt += .2 * strength * np.sin(4 * np.pi * phase) * (1 - phase)
        elif kind == 'tilt':
            tilt += .19 * strength * swell
        elif kind == 'lean':
            lean += .09 * strength * swell
    return nod, tilt, lean


def note(hz, seconds, volume=.27):
    t = np.arange(round(SR * seconds)) / SR
    return volume * np.sin(np.pi * t / seconds) ** 2 * np.sin(2 * np.pi * hz * t)


def cue():
    return np.concatenate((note(620, .105), np.zeros(round(SR * .035)), note(870, .16)))


def glide(start, end, seconds, volume=.22):
    """A pitch sweep with a little second harmonic, the family sound of BITCH's chirps."""
    t = np.arange(round(SR * seconds)) / SR
    phase = 2 * np.pi * np.cumsum(start * (end / start) ** (t / seconds)) / SR
    return volume * np.sin(np.pi * t / seconds) ** 1.5 * (np.sin(phase) + .18 * np.sin(2 * phase))


def bell(hz, seconds, volume=.18):
    t = np.arange(round(SR * seconds)) / SR
    return volume * np.exp(-t * 7) * np.minimum(1, t * 400) * (np.sin(2 * np.pi * hz * t) + .3 * np.sin(4 * np.pi * hz * t))


def pause(seconds):
    return np.zeros(round(SR * seconds))


# Sound effects for things you do, played on BITCH's speaker so you hear that it happened.
EARCONS = {
    'mic_off': lambda: np.concatenate((glide(700, 430, .17), pause(.03), note(330, .07, .15))),
    'mic_on': lambda: np.concatenate((glide(430, 700, .15), pause(.03), note(930, .07, .13))),
    'voice_off': lambda: np.concatenate((note(587, .09, .2), pause(.02), note(392, .14, .2))),
    'voice_on': lambda: np.concatenate((note(392, .09, .2), pause(.02), note(587, .14, .2))),
    'stop': lambda: glide(560, 270, .15, .22),
    'learned': lambda: np.concatenate([note(hz, .075, .16) for hz in (523, 659, 784)] + [note(1046, .18, .14)]),
    'answer': lambda: np.concatenate((bell(784, .28), bell(1175, .5))),
    'waking': lambda: np.concatenate((glide(300, 520, .35, .12), pause(.05), glide(520, 880, .25, .12))),
    'sleep': lambda: glide(620, 280, .85, .12) * (.8 + .2 * np.sin(2 * np.pi * 7 * np.arange(round(SR * .85)) / SR)),
}


def earcon(name, *, force=False, wait=False):
    """Play a short sound effect on the selected speaker. Skipped while BITCH is talking."""
    if not config.get('sounds') or not config.get('volume') or config.get('muted') and not force:
        return
    if hub.status in ('Speaking', 'Performing'):
        return
    sound = encode(EARCONS[name]())

    def run():
        try:
            play(*sound, threading.Event(), interim=True)
        except (OSError, ValueError, wave.Error):
            pass

    if wait:
        run()
    else:
        threading.Thread(target=run, daemon=True).start()


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


def join(first, second, pause=.16):
    """Play two rendered sentences as one clip with a natural breath between them."""
    frames = []
    for wav in (first, second):
        with wave.open(io.BytesIO(wav)) as file:
            frames.append(file.readframes(file.getnframes()))
    result = io.BytesIO()
    with wave.open(result, 'wb') as file:
        file.setparams((1, 2, SR, 0, 'NONE', 'not compressed'))
        file.writeframes(frames[0] + bytes(round(SR * pause) * 2) + frames[1])
    joined = result.getvalue()
    shift = len(frames[0]) / 2 / SR + pause
    remember_gestures(joined, GESTURES.get(first, []) + [(kind, at + shift, length, strength)
                                                         for kind, at, length, strength in GESTURES.get(second, [])])
    return joined


# Said when someone calls BITCH's name and nothing else. Rendered ahead so the answer is instant.
ACKNOWLEDGE = ('Yeah?', 'What’s up?', 'Mm-hm?', 'Yes?', 'I’m here.', 'Hey.', 'Yeah, what’s up?')
spoken_cache = {}
last_acknowledgment = None


def cached(text, delivery='warm'):
    if text not in spoken_cache:
        spoken_cache[text] = render(text, delivery=delivery)
        while len(spoken_cache) > 24:
            spoken_cache.pop(next(iter(spoken_cache)))
    return spoken_cache[text]


def acknowledge(name=None):
    """Answer a bare "Hey BITCH" out loud, with a little variety, instead of just a beep."""
    global last_acknowledgment
    import random

    choices = [text for text in ACKNOWLEDGE if text != last_acknowledgment]
    text = random.choice(choices)
    if name and random.random() < .35:
        text = random.choice(('Yeah, {}?', 'What’s up, {}?', 'Hey, {}.')).format(name)
    last_acknowledgment = text
    try:
        wav = cached(text)
    except (OSError, RuntimeError, ValueError):
        wav = encode(cue())
        text = ''
    return text, wav


warmed = threading.Event()   # the voice is loaded and has spoken once; hearing waits for it before saying it's ready
warming = False


def warm():
    global warming
    warming = True

    def load():
        try:
            if config.get('voice_engine') == 'qwen' and load_qwen():
                # Run the streaming path once so its first real use isn't slow.
                reply = LiveReply()
                reply.add('Hello there.')
                reply.close()
                reply.done.wait(60)
            render('Hello there.')
            for text in ACKNOWLEDGE:
                cached(text)
            record_lines()
        except Exception as error:
            hub.note('voice', f'Speech synthesis unavailable: {error}')
        finally:
            warmed.set()

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
        if level[i] >= .33 and level[i-1] < level[i] >= level[i+1]:
            if peaks and i - peaks[-1] < 6:
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
    if len(audio) < 1600:
        return False
    previous = recent_echo
    references = [reference for reference in (echo_reference, previous[1] if previous and received < previous[0] else None)
                  if reference is not None]
    samples = audio[-12800:].astype(float)[::4]
    for start, pcm in references:
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
        if matches and (matches[0] > .72 or sum(score > .55 for score in matches[:4]) >= 2):
            return True
    return False


def clear_playback():
    global playback, echo_reference, recent_echo, gesture_track
    gesture_track = []
    if echo_reference is not None:
        recent_echo = time.monotonic() + 1.4, echo_reference
    playback = echo_reference = None


def _preroll_bytes():
    try:
        frames, _ = preroll(b'')
        return frames
    except Exception:
        return b''


def articulation():
    current = playback
    if current is None:
        return 0., False, 0.
    started, level, nod, lead, motion_start, motion_end, cue_at, delivery, _ = current
    # Follow the sound as it leaves the speaker (the hearing echo gate measures that delay),
    # starting each move a little early so the servos land on the syllable.
    elapsed = time.monotonic() - started - lead - max(0., output_delay - MOTION_LEAD)
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
    extra, _, _ = gesture_at(gesture_track, elapsed)
    return min(1., float(energy)*strength), chirping, posture + amplitude*NOD_GAIN*float(pitch) + extra


def gesture():
    """Head tilt and lean from the words being spoken right now."""
    current = playback
    if current is None or not gesture_track:
        return 0., 0.
    started, lead = current[0], current[3]
    elapsed = time.monotonic() - started - lead - max(0., output_delay - MOTION_LEAD)
    _, tilt, lean = gesture_at(gesture_track, elapsed)
    return float(tilt), float(lean)


def glance_now():
    """The name of the person BITCH is saying right now, if any, so the eyes can find them."""
    current = playback
    if current is None or not gesture_track:
        return None
    elapsed = time.monotonic() - current[0] - current[3] - max(0., output_delay - MOTION_LEAD)
    return next((name for kind, at, length, name in gesture_track
                 if kind == 'glance' and 0 <= elapsed - at < length), None)


def delivery_now():
    current = playback
    return current[-2:] if current is not None else None


class Either:
    """Stops when either event is set, so a thinking cue ends the moment speech is ready."""
    def __init__(self, first, second):
        self.first, self.second = first, second

    def is_set(self):
        return self.first.is_set() or self.second.is_set()

    def wait(self, timeout):
        return self.first.wait(timeout) or self.second.is_set()


@contextmanager
def thinking(stop, *, greeting):
    """Play sparse cues during generation; the first spoken words cut off a cue still playing."""
    done = threading.Event()
    either = Either(done, stop)

    def sound():
        try:
            previous = 0.
            # No beep when you finish talking: answers start in about a second. One soft cue only if an answer
            # really stalls, so you know BITCH is still on it.
            schedule = (4.5,)
            for index, when in enumerate(schedule):
                if done.wait(max(0., when - previous)) or stop.is_set():
                    break
                started = time.monotonic()
                play(*encode(wait_sound(2)), either, interim=True)
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


class Shaper:
    """BITCH's voice layer (warm EQ, metallic shimmer, a 4 ms reflection), one chunk at a time.

    The same sound as render(), but streamable: filter state, the shimmer's phase and the reflection's tail carry over
    from chunk to chunk, so the joins are seamless."""
    PEAK = .8                      # typical peak of the voice after the EQ (measured median .75)
    DELAY = round(.004 * SR)

    def __init__(self, gain=1.):
        from scipy.signal import butter, sosfilt_zi

        self.body = butter(2, [180, 450], 'bandpass', fs=SR, output='sos')
        self.top = butter(1, 7000, 'lowpass', fs=SR, output='sos')
        self.body_state = sosfilt_zi(self.body) * 0
        self.top_state = sosfilt_zi(self.top) * 0
        self.offset = 0
        self.tail = np.zeros(self.DELAY, np.float32)
        self.gain = .9 * gain / self.PEAK

    def process(self, audio):
        from scipy.signal import sosfilt

        audio = np.asarray(audio, dtype=np.float32)
        body, self.body_state = sosfilt(self.body, audio, zi=self.body_state)
        top, self.top_state = sosfilt(self.top, audio, zi=self.top_state)
        clean = audio + 0.41 * body - 0.2 * (audio - top)
        n = np.arange(self.offset, self.offset + len(clean))
        signal = clean * (0.93 + 0.07 * np.sin(2 * np.pi * 75 * n / SR))
        echo = np.concatenate((self.tail, clean))[:len(clean)]
        signal = signal + 0.055 * echo
        self.tail = np.concatenate((self.tail, clean))[-self.DELAY:]
        self.offset += len(clean)
        return np.tanh(signal * self.gain * 1.1) / 1.1   # a soft ceiling instead of normalizing the whole line


BUFFER = .2    # seconds of a live reply generated before it starts playing (no stalls measured at this size)
BREATH = .16   # silence between sentences, like a breath


class LiveReply:
    """A whole reply in BITCH's voice as one continuous stream: each sentence is generated right after the one before
    it, with a breath between, while the earlier ones play. First words start in about half a second, and there are
    no gaps from rendering whole sentences one at a time. Delivery (gentle, playful...) can change per sentence."""
    def __init__(self, stop=None):
        self.stop = stop or threading.Event()
        self.phrases = queue.Queue()
        self.chunks = queue.Queue()
        self.pcm = bytearray()
        self.done = threading.Event()
        self.error = None
        self.segments = []        # (start second, phrase, delivery, expression) in the stream's own time
        self.gestures = []
        self.lock = threading.Lock()
        threading.Thread(target=self.generate, daemon=True).start()

    def add(self, phrase, delivery='neutral', laugh_first=False, expression='none'):
        self.phrases.put((phrase, delivery, laugh_first, expression))

    def close(self):
        self.phrases.put(None)

    def seconds(self):
        return len(self.pcm) / 2 / SR

    def emit(self, samples):
        data = (np.clip(samples, -1, 1) * 32767).astype('<i2').tobytes()
        with self.lock:
            self.pcm += data
        self.chunks.put(data)

    def generate(self):
        try:
            model = load_qwen()
            if not model:
                raise RuntimeError('voice model unavailable')
            volume = (config.get('volume') / 100) ** 1.65
            first = True
            while (item := self.phrases.get()) is not None:
                if self.stop.is_set():
                    break
                phrase, delivery, laugh_first, expression = item
                if not first:
                    self.emit(np.zeros(round(BREATH * SR), np.float32))
                first = False
                if laugh_first:
                    self.emit(laugh_sound() * volume)
                shaper = Shaper(VOICE_GAIN.get(delivery, 1.))
                begin = self.seconds()
                self.segments.append((begin, phrase, delivery, expression))
                clean = []
                for chunk in mlx_steps(model.generate(text=phrase, ref_audio=str(REFERENCE),
                                                      ref_text=model.reference_text, lang_code='english',
                                                      stream=True, streaming_interval=.24)):
                    if self.stop.is_set():
                        break
                    audio = np.asarray(chunk.audio, dtype=np.float32).reshape(-1)
                    clean.append(audio)
                    self.emit(shaper.process(audio) * volume)
                if clean:
                    words = estimate_words(phrase, np.concatenate(clean), SR)
                    self.gestures += [(kind, at + begin, length, strength)
                                      for kind, at, length, strength in gestures(words)]
        except Exception as error:
            self.error = error
        finally:
            self.chunks.put(None)
            self.done.set()

    def stream(self, lead=b''):
        if lead:
            yield lead
        while (data := self.chunks.get()) is not None:
            yield data

    def wav(self):
        self.done.wait()
        result = io.BytesIO()
        with wave.open(result, 'wb') as file:
            file.setparams((1, 2, SR, 0, 'NONE', 'not compressed'))
            file.writeframes(bytes(self.pcm))
        return result.getvalue(), len(self.pcm) / 2 / SR

    def segment_at(self, second):
        current = self.segments[0] if self.segments else (0., '', 'neutral', 'none')
        for segment in self.segments:
            if segment[0] <= second:
                current = segment
        return current


def laugh_sound():
    bursts = [note(hz, .14, .18) + note(hz*2, .14, .065) for hz in (310, 277, 238)]
    return np.concatenate([np.r_[burst, np.zeros(round(SR*.065))] for burst in bursts]).astype(np.float32)


def can_stream():
    speaker = hub.selected('voice')
    return (config.get('voice_engine') == 'qwen' and bool(load_qwen()) and speaker is not None
            and hasattr(speaker, 'speak_stream') and not config.get('muted') and bool(config.get('volume')))


def play_live(reply, stop, on_speech=None):
    """Play a LiveReply as it is generated. Falls back to playing it whole on a speaker that can't stream."""
    global playback, echo_reference, gesture_track
    speaker = hub.selected('voice')
    if stop.is_set() or not speaker:
        reply.stop.set()
        return
    spoken = 0
    def announce(upto):
        nonlocal spoken
        while on_speech and spoken < len(reply.segments) and reply.segments[spoken][0] <= upto:
            on_speech(reply.segments[spoken][1])
            spoken += 1
    if not hasattr(speaker, 'speak_stream'):
        wav, duration = reply.wav()
        if reply.error and not reply.pcm:
            raise RuntimeError(f'voice failed: {reply.error}')
        announce(float('inf'))
        play(wav, duration, stop, text=True, segment=True)
        return
    # A small jitter buffer: start once a third of a second is ready (or the reply is done), so a moment of GPU
    # contention can't open a gap mid-word, and BITCH's echo stays where the echo gate expects it.
    while not stop.is_set() and not reply.done.is_set() and reply.seconds() < BUFFER:
        reply.done.wait(.02)
    if stop.is_set():
        reply.stop.set()
        return
    lead_audio = b''
    if speaker.id == 'robot' and time.monotonic() - last_played.get(speaker, -float('inf')) >= 20 \
            and not getattr(speaker, 'audio_warm', False):
        lead_audio = _preroll_bytes()
    lead = len(lead_audio) / 2 / SR
    hub.status = 'Speaking'
    start = time.monotonic()
    if not speaker.speak_stream(reply.stream(lead_audio)):
        wav, duration = reply.wav()
        announce(float('inf'))
        play(wav, duration, stop, text=True, segment=True)
        return
    seen = 0
    try:
        while not stop.wait(.05):
            heard = time.monotonic() - start - lead
            announce(heard + .3)
            _, _, delivery, expression = reply.segment_at(max(0., heard))
            if len(reply.pcm) != seen or (playback and playback[-2:] != (delivery, expression)):
                seen = len(reply.pcm)
                with reply.lock:
                    pcm = bytes(reply.pcm)
                envelope, nod = motion_track(pcm)
                playback = start, envelope, nod, lead, 0., len(pcm) / 2 / SR, None, delivery, expression
                echo_reference = (start + lead, np.frombuffer(pcm, '<i2')[::6].astype(float))
            gesture_track = reply.gestures
            if reply.done.is_set() and speaker.playing() is False:
                break
            if heard > reply.seconds() + 5:
                speaker.hush()
                break
        if stop.is_set():
            reply.stop.set()
            speaker.hush()
    finally:
        announce(float('inf') if not stop.is_set() else time.monotonic() - start - lead)
        clear_playback()
        last_played[speaker] = time.monotonic()
    if reply.error and not reply.pcm:
        raise RuntimeError(f'voice failed: {reply.error}')


NOTHING = object()


class ReplyStream:
    """Speak a reply while the model is still writing it.

    With BITCH's own voice and a speaker that can stream, the whole reply is one LiveReply. Otherwise each phrase is
    rendered while the previous one plays."""
    def __init__(self, stop, finish_thinking, on_speech):
        self.stop = stop
        self.finish_thinking = finish_thinking
        self.on_speech = on_speech
        self.pending = queue.Queue()
        self.audio = queue.Queue()
        self.parts = []
        self.workers = ()
        self.closed = False
        self.live = None

    def offer(self, phrase, delivery, laugh_first=False, expression='none'):
        if self.stop.is_set():
            return
        if not self.workers:
            if phrase and can_stream():
                self.live = LiveReply(self.stop)
                self.workers = (threading.Thread(target=self.play_live, daemon=True),)
            else:
                self.workers = tuple(threading.Thread(target=work, daemon=True)
                                     for work in (self.synthesize, self.play))
            for worker in self.workers:
                worker.start()
        if phrase:
            self.parts.append(phrase)
        if self.live:
            if phrase:
                self.live.add(phrase, delivery, laugh_first, expression)
            return
        self.pending.put((phrase, delivery, laugh_first, expression))

    def play_live(self):
        try:
            # Thinking ends when the first audio is about to play, not before.
            while not self.stop.is_set() and not self.live.done.is_set() and self.live.seconds() < BUFFER:
                self.live.done.wait(.02)
            self.finish_thinking()
            play_live(self.live, self.stop, self.on_speech)
        except (OSError, RuntimeError, ValueError) as error:
            hub.note('voice', f'Speech failed: {error}')

    def synthesize(self):
        try:
            while (part := self.pending.get()) is not None and not self.stop.is_set():
                phrase, delivery, laugh_first, expression = part
                wav = render(phrase, delivery=delivery) if phrase else encode(cue())
                self.audio.put((wav, phrase, delivery, laugh_first, expression))
        except (OSError, RuntimeError, ValueError) as error:
            hub.note('voice', f'Speech synthesis unavailable: {error}')
        finally:
            self.audio.put(None)

    def play(self):
        first = True
        held = NOTHING
        while not self.stop.is_set():
            part, held = (self.audio.get() if held is NOTHING else held), NOTHING
            if part is None:
                break
            (wav, duration), phrase, delivery, laugh_first, expression = part
            phrases = [phrase] if phrase else []
            # Sentences that finished rendering while the last one played go out as one clip,
            # so the speaker doesn't stop and restart between them.
            while phrase:
                try:
                    following = self.audio.get_nowait()
                except queue.Empty:
                    break
                if following is None or not following[1] or following[2:] != (delivery, False, expression):
                    held = following
                    break
                wav = join(wav, following[0][0])
                duration += .16 + following[0][1]
                phrases.append(following[1])
            if first:
                self.finish_thinking()
                first = False
            if laugh_first:
                laugh(self.stop)
            if self.stop.is_set():
                break
            for spoken in phrases:
                self.on_speech(spoken)
            play(wav, duration, self.stop, text=bool(phrase), delivery=delivery, segment=True, expression=expression)

    def finish(self, reply, delivery, ready_cue):
        spoken = ' '.join(self.parts)
        if reply.startswith(spoken):
            remaining = reply[len(spoken):].strip()
            if remaining:
                self.offer(remaining, delivery)
        if ready_cue and not self.live:
            self.offer('', delivery)
        self.close()

    def close(self):
        if self.workers and not self.closed:
            self.closed = True
            if self.live:
                self.live.close()
            else:
                self.pending.put(None)
            for worker in self.workers:
                worker.join()


def laugh(stop):
    if stop.is_set() or config.get('muted') or not config.get('volume'):
        return
    # These soft pulses and the animated shoulder bounce share the same audio clock.
    bursts = [note(hz, .14, .18) + note(hz*2, .14, .065) for hz in (310, 277, 238)]
    breath = np.concatenate([np.r_[burst, np.zeros(round(SR*.065))] for burst in bursts])
    play(*encode(breath), stop, text=True, delivery='playful', interim=True, expression='amused')


def say(text, stop, *, ready_cue=False, delivery='neutral', expression='none'):
    if config.get('muted') or not config.get('volume'):
        return
    if text and not ready_cue and can_stream():
        reply = LiveReply(stop)
        reply.add(text, delivery, expression=expression)
        reply.close()
        play_live(reply, stop)
        return
    wav, duration = render(text, ready_cue=ready_cue, delivery=delivery) if text else encode(cue())
    play(wav, duration, stop, text=bool(text), ready_cue=ready_cue, delivery=delivery, expression=expression)


def play(wav, duration, stop, *, text=False, ready_cue=False, delivery='neutral', interim=False, segment=False,
         expression='none'):
    global playback, echo_reference, gesture_track
    speaker = hub.selected('voice')
    if stop.is_set() or config.get('muted') or not config.get('volume'):
        return
    if not speaker:
        if not interim:
            hub.note('speaker', 'No speaker available. Choose a speaker in Settings.')
        return
    with wave.open(io.BytesIO(wav)) as file:
        pcm = file.readframes(file.getnframes())
    lead = 0.
    # A short output tail lets the Bluetooth transport drain the final syllable.
    # Mid-reply sentences need less while the keep-awake stream holds the speaker open.
    tail = .12
    if speaker.id == 'robot':
        tail = .16 if segment and text and getattr(speaker, 'audio_warm', False) else .44
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
    cue_at = max(0., duration-.30) if ready_cue else 0. if (interim or segment) and not text else None
    speech_end = max(0., duration-.42) if text and ready_cue else duration if text else 0.
    if speech_end > 0:
        envelope, nod = motion_track(pcm[:round(speech_end*SR)*2])
    else:
        envelope = nod = ()
    gesture_track = GESTURES.get(wav, []) if text else []
    playback = start, envelope, nod, lead, 0., speech_end, cue_at, delivery, expression
    echo_reference = (start+lead, np.frombuffer(pcm, '<i2')[::6].astype(float)) if text else None
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
        clear_playback()
        if not early:
            last_played[speaker] = time.monotonic()
    if early and not stop.is_set() and not interim:
        backup = hub.get('mac-speaker') if speaker.id == 'robot' and config.get('speaker_source') == 'auto' else None
        if backup and backup.speak(wav):
            hub.note('speaker', 'Pi playback stopped early. Replayed the reply on the Mac speaker.')
            playback = time.monotonic(), envelope, nod, 0., 0., speech_end, cue_at, delivery, expression
            echo_reference = (time.monotonic(), np.frombuffer(pcm, '<i2')[::6].astype(float)) if text else None
            if stop.wait(duration + .30):
                backup.hush()
            clear_playback()
        else:
            hub.note('speaker', 'Playback ended early. Check the speaker connection.')
    if not interim and not segment and not stop.is_set():
        time.sleep(.26 if ready_cue else .43)
