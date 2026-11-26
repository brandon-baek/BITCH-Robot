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


VOICE_RATE = {'gentle': .93, 'emphatic': 1.07, 'upbeat': 1.06, 'playful': 1.045}
# Mood also moves the pitch and the loudness, so a sad line sounds lower and softer, not just slower.
VOICE_PITCH = {'gentle': .965, 'warm': .985, 'emphatic': .985, 'upbeat': 1.055, 'playful': 1.04}
VOICE_GAIN = {'gentle': .8, 'warm': .92}


spoken = threading.local()   # word start times from the last synthesis on this thread


def synthesize(text, speed=1.):
    global pipeline
    spoken.words = []
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
    words = [(word, (at - start / rate) / (1.035 * pitch)) for word, at in getattr(spoken, 'words', ())]
    # Retain the clean voice and its prosody. Add a quiet metallic signal and one very short reflection.
    count = max(1, round(len(audio) * SR / (rate * 1.035 * pitch)))
    clean = np.interp(np.linspace(0, len(audio) - 1, count), np.arange(len(audio)), audio)
    signal = clean * (0.93 + 0.07 * np.sin(2 * np.pi * 75 * np.arange(count) / SR))
    delay = round(0.004 * SR)
    signal[delay:] += 0.055 * clean[:-delay]
    signal *= 0.9 * VOICE_GAIN.get(delivery, 1.) / max(float(np.abs(signal).max()), 0.2)
    if ready_cue:
        signal = np.concatenate((signal, np.zeros(round(SR * 0.12)), cue()))
    result = encode(signal)
    remember_gestures(result[0], gestures(words))
    return result


AFFIRM = {'yes', 'yeah', 'yep', 'yup', 'right', 'exactly', 'sure', 'totally', 'absolutely', 'definitely',
          'correct', 'agreed', 'true', 'okay', 'ok', 'uh-huh'}
NEGATE = {'no', 'nope', 'nah', 'not', "don't", "can't", "won't", "isn't", "aren't", 'never', "didn't", "doesn't",
          "wasn't", "shouldn't", "couldn't", "wouldn't", "haven't", 'nothing', 'nobody'}
EMPHASIS = {'really', 'very', 'so', 'super', 'huge', 'tiny', 'always', 'seriously', 'literally', 'actually',
            'way', 'every', 'all', 'best', 'worst', 'love', 'hate', 'amazing', 'terrible'}
YOU = {'you', 'your', "you're", 'yours', 'yourself', "you've", "you'll"}
SELF = {'i', 'me', 'my', "i'm", "i've", "i'll", "i'd", 'myself'}
THINK = {'hmm', 'well', 'um', 'uh', 'maybe', 'perhaps', 'guess', 'probably', 'honestly'}
GESTURES = {}   # rendered clip -> its word gestures, so the motion travels with the audio


def gestures(words):
    """Turn word timings into body language: nod on yes, shake on no, tilt on a question,
    lean in on "you" and back on "I"."""
    events, shook, opening = [], False, True
    for index, (word, at) in enumerate(words):
        plain = word.casefold().strip('.,!?;:"“”‘’\'').replace('’', "'")
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
        if not 0 <= phase < 1:
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
            # One cue when you finish; more only if the answer really stalls (two quick ones sounded like a stutter).
            schedule = (0., 4.5, 9.)
            for index, when in enumerate(schedule):
                if done.wait(max(0., when - previous)) or stop.is_set():
                    break
                started = time.monotonic()
                play(*encode(wait_sound(index if greeting else index+1)), either, interim=True)
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


NOTHING = object()


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

    def offer(self, phrase, delivery, laugh_first=False, expression='none'):
        if self.stop.is_set():
            return
        if not self.workers:
            self.workers = tuple(threading.Thread(target=work, daemon=True)
                                 for work in (self.synthesize, self.play))
            for worker in self.workers:
                worker.start()
        if phrase:
            self.parts.append(phrase)
        self.pending.put((phrase, delivery, laugh_first, expression))

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
    play(*encode(breath), stop, text=True, delivery='playful', interim=True, expression='amused')


def say(text, stop, *, ready_cue=False, delivery='neutral', expression='none'):
    if config.get('muted') or not config.get('volume'):
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
