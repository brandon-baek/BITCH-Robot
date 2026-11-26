"""VAD and local transcription for the single selected input."""
from collections import deque
from dataclasses import dataclass
import queue
import re
import threading
import time

import numpy as np

from .hub import hub
from . import config, voice
from .people import people

SR = 16000
FRAME = 512
WHISPER = 'mlx-community/whisper-large-v3-turbo'
# Parakeet reads a sentence in about 0.1 s, Whisper in about 0.66 s, at the same accuracy (testing/asr_eval: 8.1% word
# errors each over close, noisy and far-field speech). Whisper with the name hint is better at catching "BITCH" from
# across the room (8/9 against 5/9), so it gets a second listen only when the name is needed and Parakeet missed it.
PARAKEET = 'mlx-community/parakeet-tdt-0.6b-v2'
# Example addresses steer Whisper toward the name. Measured on noisy far-field clips this raised
# the name being heard from 69% to 97% (with the wake rules in brain.py) at zero false wakes.
WAKE_HINT = "Hey BITCH. Yo BITCH. Okay BITCH. BITCH, look at this. What do you think, BITCH?"
WAKE_ONLY_SPEECH = .35  # seconds of real speech behind a bare "BITCH." (the prompt can echo the name on noise)
PRE_ROLL = 20         # frames of lead-in (0.64 s) kept before speech starts
OVERLAP = 2.2         # mic this many times louder than BITCH's predicted echo means someone is talking over it
SUSTAINED = 20        # frames (0.64 s) of talking over BITCH that stop it without waiting for words
PREVIEW_QUIET = 8      # frames of silence (about a quarter second) before a speculative transcript
TRAILING = re.compile(
    r"(?:\.\.\.|…|[,;:—–-])$|\b(?:and|but|or|so|because|cause|the|a|an|to|of|with|my|your|our|their|"
    r"if|then|um|uh|uhm|like|for|from|than|is|are|was|were|i|i'm|also)\.$", re.I)


def wake_only(text):
    from .brain import wake_request

    return wake_request(text) == ''


def sounds_finished(text):
    """True when a transcript reads like a whole turn, not a pause mid-thought."""
    from .brain import wake_request

    text = text.strip()
    return bool(text and text[-1] in '.?!' and not TRAILING.search(text) and wake_request(text) != '')


class EchoGate:
    """Tell someone talking over BITCH apart from BITCH's own voice coming back into the mic.

    While a reply plays, it learns how late (speaker delay) and how loud (room gain) BITCH's
    voice arrives, then predicts the echo for each mic frame. Speech well above that
    prediction is a person. The learned delay also keeps head nods in time with the sound."""
    STEP = .032

    def __init__(self):
        self.delay = .3
        self.gain = None
        self.ratios = deque(maxlen=160)
        self.quiet = deque(maxlen=300)
        self.trace = deque(maxlen=90)
        self.reference = None
        self.learned_at = 0.

    def envelope(self):
        current = voice.echo_reference
        if current is None:
            recent = voice.recent_echo
            current = recent[1] if recent and time.monotonic() < recent[0] else None
        if current is None:
            self.reference = None
            return None
        if self.reference is None or self.reference[0] is not current:
            start, pcm = current
            width = round(self.STEP * 4000)
            usable = len(pcm) // width * width
            levels = np.sqrt(np.mean(((np.asarray(pcm[:usable]) / 32768) ** 2).reshape(-1, width), axis=1)) if usable else np.zeros(0)
            self.reference = current, start, levels
            self.trace.clear()
        return self.reference[1:]

    def predicted(self, start, levels, at, delay=None):
        index = (at - start - (self.delay if delay is None else delay)) / self.STEP
        if index < 0 or index >= len(levels) - 1:
            return 0.
        low = int(index)
        return float(levels[low] + (levels[low+1] - levels[low]) * (index - low))

    def excess(self, frame, at, speech):
        """How many times louder the mic is than BITCH's predicted echo, or None with no playback."""
        level = float(np.sqrt(np.mean(frame ** 2)))
        reference = self.envelope()
        if reference is None:
            if not speech:
                self.quiet.append(level)
            return None
        start, levels = reference
        floor = float(np.percentile(self.quiet, 30)) if self.quiet else 1e-3
        self.trace.append((at, level))
        if at - self.learned_at > .5 and len(self.trace) >= 40:
            self.learn(start, levels)
        echo = self.predicted(start, levels, at)
        if echo > .01:
            self.ratios.append(level / echo)
            if len(self.ratios) >= 12:
                # A low percentile ignores frames where someone was also talking.
                self.gain = float(np.percentile(self.ratios, 35))
        gain = self.gain if self.gain is not None else .25
        return level / (gain * echo + 2.5 * floor + 1e-6)

    def learn(self, start, levels):
        self.learned_at = self.trace[-1][0]
        times = np.array([at for at, _ in self.trace])
        heard = np.log(np.array([level for _, level in self.trace]) + 1e-4)
        best, score = None, .55
        for delay in np.arange(0., .9, .016):
            sent = np.log(np.array([self.predicted(start, levels, at, delay) for at in times]) + 1e-4)
            if np.std(sent) < 1e-3:
                continue
            fit = float(np.corrcoef(heard, sent)[0, 1])
            if fit > score:
                best, score = delay, fit
        if best is not None:
            self.delay = .7 * self.delay + .3 * best
            voice.output_delay = self.delay


@dataclass
class Clip:
    kind: str
    source: str
    revision: int
    queued: float
    began: float
    last_speech: float
    audio: np.ndarray
    speech: np.ndarray | None = None
    voiced: float = 0.
    interrupted: bool = False
    strong: bool = False   # clearly louder than BITCH's echo


class Hearing:
    def __init__(self):
        self.audio = queue.Queue(maxsize=80)
        self.clips = queue.Queue(maxsize=3)
        self.whisper_ready = False
        self.detector_ready = False
        self.activity_at = 0
        self.capture = None
        self.decoding = None
        self.finished = None
        self.echo = EchoGate()

    def phase(self):
        # Associate UI feedback with the microphone and the accepted conversation.
        if hub.brain.busy.is_set():
            return None
        now = time.monotonic()
        for item, phase in ((self.capture, 'Listening'), (self.decoding, 'Understanding')):
            if item:
                source, revision, began, updated = item
                if (now - updated < (1 if phase == 'Listening' else 30)
                        and revision == hub.brain.revision and hub.accepts(source, 'ears')):
                    return phase if hub.brain.addressed('', source, began) else 'Checking for name'
        return None

    @property
    def ready(self):
        return self.whisper_ready and self.detector_ready

    def start(self):
        threading.Thread(target=self._transcribe, daemon=True).start()
        threading.Thread(target=self._detect, daemon=True).start()

    def feed(self, source, samples):
        # Audio is never dropped here: even while BITCH is busy it becomes lead-in for the next turn.
        if not self.ready or not hub.accepts(source, 'ears') or config.get('mic_muted'):
            return
        try:
            self.audio.put_nowait((source, hub.brain.revision, time.monotonic(), samples))
        except queue.Full:
            pass

    def _detect(self):
        try:
            import torch
            from silero_vad import load_silero_vad

            # This setting is process-wide, so it also paces Kokoro: one thread made every
            # phrase about 0.4 s slower to render, while the VAD costs under 0.4 ms a frame either way.
            torch.set_num_threads(4)
            detector = load_silero_vad()
        except Exception as error:
            hub.note('vad', f'Speech detector unavailable: {error}')
            return
        self.detector_ready = True
        source = None
        tail = np.empty(0, np.float32)
        pre = deque(maxlen=PRE_ROLL)
        clip = []
        voiced = quiet = onset = 0
        last = 0
        talking_over = False
        last_probe = 0
        last_speech = 0.
        previewed = False
        overlap = 0          # recent frames where someone is louder than BITCH's own echo
        overlap_start = None
        louder_run = 0       # consecutive frames clearly louder than the echo
        recent_louder = deque(maxlen=16)
        held = False         # this clip already paused BITCH's thinking
        interrupted = False
        marks = []           # which clip frames were speech
        while True:
            heard_from, _, received, samples = self.audio.get()
            if heard_from != source or received - last > 0.45:
                source = heard_from
                tail = np.empty(0, np.float32)
                self.capture = None
                pre.clear()
                clip, marks = [], []
                voiced = quiet = onset = 0
                detector.reset_states()
            last = received
            revision = hub.brain.revision
            can_interrupt = hub.brain.can_interrupt(source)
            can_continue = hub.brain.can_continue(source)
            audio = np.concatenate((tail, samples))
            end = len(audio) // FRAME * FRAME
            tail = audio[end:]
            if not hub.accepts(source, 'ears'):
                continue
            if hub.brain.busy.is_set() and not (can_interrupt or can_continue or hub.brain.interruption):
                # BITCH can't take a turn right now. Keep the audio as lead-in so the
                # first words of the next turn survive.
                pre.extend(audio[index:index + FRAME] for index in range(0, end, FRAME))
                if clip and not talking_over:
                    clip, marks = [], []
                    onset = 0
                    self.capture = None
                continue
            for index in range(0, end, FRAME):
                frame = audio[index:index + FRAME]
                frame_at = received - (len(audio)-index-FRAME) / SR
                hub.level = min(1.0, float(np.sqrt(np.mean(frame ** 2))) * 8)
                speech = detector(torch.from_numpy(frame), SR).item() > (0.32 if clip else 0.5)
                louder = self.echo.excess(frame, frame_at, speech)
                if clip and talking_over and not hub.brain.busy.is_set() and not hub.brain.interruption:
                    # BITCH finished while this clip was open. If someone was talking over it,
                    # keep their part. Otherwise it was BITCH's own voice: keep only the last
                    # moment as lead-in and listen fresh, so a reply right after is heard whole.
                    talking_over = previewed = False
                    if overlap_start is None:
                        pre.clear()
                        pre.extend(clip[-PRE_ROLL:])
                        clip, marks = [], []
                        onset = 0
                        self.capture = None
                    else:
                        cut = max(0, overlap_start - 8)
                        clip, marks = clip[cut:], marks[cut:]
                        began, voiced = frame_at - len(clip) * FRAME / SR, sum(marks)
                if not clip:
                    pre.append(frame)
                    # Just after BITCH spoke, its echo is still in the room: only sound clearly louder than that
                    # echo can open a new turn. (While it speaks, the talk-over path below handles this.)
                    echo_only = louder is not None and not can_interrupt and louder < OVERLAP * .7
                    onset = onset + 1 if speech and not echo_only else 0
                    if onset >= 3:
                        talking_over = interrupted = can_interrupt
                        held = False
                        last_probe = overlap = louder_run = 0
                        overlap_start = None
                        last_speech, previewed = frame_at, False
                        began = frame_at - (len(pre) - 1) * FRAME / SR
                        self.activity_at = received
                        clip = list(pre)
                        marks = [False] * (len(pre) - onset) + [True] * onset
                        voiced, quiet = onset, 0
                    continue
                self.capture = (source, revision, began, received)
                clip.append(frame)
                marks.append(speech)
                voiced += int(speech)
                if speech:
                    self.activity_at = received
                    last_speech = frame_at
                    previewed = False
                quiet = 0 if speech else quiet + 1
                recent_louder.append(louder or 0.)
                if talking_over and can_interrupt:
                    # Someone talking over BITCH is clearly louder than its own echo at the mic.
                    overlap = (min(16, overlap + 1) if speech and louder is not None and louder >= OVERLAP
                               else max(0, overlap - 1))
                    if overlap and overlap_start is None:
                        overlap_start = len(clip) - 1
                    if not overlap:
                        overlap_start = None
                    louder_run = louder_run + 1 if speech and louder is not None and louder >= OVERLAP else 0
                    if louder_run == SUSTAINED:
                        # Someone has kept talking over BITCH: stop, like a person would, even
                        # before the words are clear enough to transcribe.
                        hub.brain.interrupt(source, '', strong=True, sustained=True)
                    probe = voiced - last_probe >= 12 or last_probe == 0 and voiced >= 6 and quiet == 6
                    if (overlap >= 9 and overlap_start is not None and len(clip) - overlap_start - last_probe >= 9
                            and not self.clips.full()):
                        last_probe = len(clip) - overlap_start
                        candidate = np.concatenate(clip[max(0, overlap_start - 8):])
                        self.clips.put_nowait(Clip('barge', source, revision, received, began, frame_at, candidate,
                                                   strong=True))
                    elif (probe and voiced <= 60 and overlap_start is None and not self.clips.full()
                          and (louder is None or max(recent_louder, default=0.) >= OVERLAP * .7)):
                        # Only when something rose above BITCH's own echo; otherwise Whisper
                        # mishearing the echo ("I think it…") could pass for a correction.
                        last_probe = voiced
                        candidate = np.concatenate(clip)
                        if not voice.is_echo(candidate, frame_at):
                            self.clips.put_nowait(Clip('barge', source, revision, received, began, frame_at, candidate))
                if talking_over and hub.brain.interruption and overlap_start is not None:
                    # The person won. From here the clip is their turn, minus BITCH's earlier words.
                    cut = max(0, overlap_start - 8)
                    clip, marks = clip[cut:], marks[cut:]
                    began, voiced = frame_at - len(clip) * FRAME / SR, sum(marks)
                    talking_over = False
                if can_continue and not talking_over and not held and voiced >= 8:
                    # Still talking while BITCH is thinking: let them finish, then answer all of it.
                    held = interrupted = hub.brain.hold_for_more(source)
                if (not talking_over and not previewed and voiced >= 8 and quiet >= PREVIEW_QUIET
                        and not hub.brain.busy.is_set() and self.clips.empty()):
                    previewed = True
                    self.clips.put_nowait(Clip('preview', source, revision, time.monotonic(), began, last_speech,
                                               np.concatenate(clip), voiced=voiced * FRAME / SR))
                # Natural mid-sentence pauses are often longer than half a second.
                # Give a longer utterance extra room, including on the initial wake.
                pause = 1.45 if voiced * FRAME / SR < 2.5 else 2.05
                # The preview transcript reads as a finished turn and nobody spoke since: answer now.
                done = quiet >= PREVIEW_QUIET and self.finished == (source, revision, began, last_speech)
                if done or quiet * FRAME / SR >= pause or len(clip) * FRAME / SR >= 60:
                    self.capture = None
                    admitted = not talking_over or hub.brain.interruption
                    if admitted and voiced >= (7 if talking_over else 5) and not self.clips.full():
                        recording = np.concatenate(clip)
                        speech_end = (len(clip) - max(0, quiet - 2)) * FRAME
                        self.clips.put_nowait(Clip('final', source, hub.brain.revision, time.monotonic(), began,
                                                   last_speech, recording, recording[:speech_end],
                                                   voiced * FRAME / SR, interrupted=interrupted))
                    clip, marks = [], []
                    pre.clear()
                    onset = 0
                    detector.reset_states()

    def _transcribe(self):
        try:
            import mlx_whisper

            mlx_whisper.transcribe(np.zeros(SR, np.float32), path_or_hf_repo=WHISPER)
        except Exception as error:
            hub.note('hearing', f'Whisper unavailable: {error}')
            return
        try:
            from parakeet_mlx import from_pretrained

            self.parakeet = from_pretrained(PARAKEET)
            self.read_fast(np.zeros(SR, np.float32))
        except Exception as error:
            self.parakeet = None
            hub.note('hearing', f'Fast recognizer unavailable, using Whisper only: {error}')
        self.whisper_ready = True
        preview = None
        while True:
            clip = self.clips.get()
            source, revision, began = clip.source, clip.revision, clip.began
            if clip.kind == 'barge':
                if revision == hub.brain.revision and time.monotonic() - clip.queued < .9 and hub.brain.can_interrupt(source):
                    try:
                        text = self.listen(mlx_whisper, clip.audio, need_name=False)
                        _, voice_name = people.speaker(source, clip.audio, len(clip.audio)/SR, addressed=True)
                        hub.brain.interrupt(source, text, voice_name=voice_name, strong=clip.strong)
                    except (ValueError, RuntimeError, OSError):
                        pass
                continue
            valid = lambda: self.valid_clip(clip) and not hub.brain.busy.is_set()
            if clip.interrupted and not self.wait_for_interrupted_reply(clip):
                continue
            if not valid() or not clip.interrupted and time.monotonic() - clip.queued > 2:
                continue
            stamp = (source, revision, began, clip.last_speech)
            if clip.kind == 'preview':
                try:
                    if voice.is_echo(clip.audio, clip.last_speech):
                        continue
                    text = self.listen(mlx_whisper, clip.audio, need_name=not hub.brain.addressed('', source, began))
                    preview = (stamp, text) if text else None
                    if text and wake_only(text) and clip.voiced >= WAKE_ONLY_SPEECH:
                        # Just the name: answer with a chirp right away, keep listening for the rest.
                        hub.brain.acknowledge(source)
                    elif text and sounds_finished(text) and hub.brain.addressed(text, source, began):
                        self.finished = stamp
                except (ValueError, RuntimeError, OSError):
                    preview = None
                continue
            self.decoding = (source, revision, began, time.monotonic())
            try:
                if voice.is_echo(clip.speech, clip.last_speech):
                    continue
                enrolled, _ = people.speaker(source, clip.speech, clip.voiced, addressed=False)
                if enrolled:
                    continue
                text = (preview[1] if preview and preview[0] == stamp
                        else self.listen(mlx_whisper, clip.audio, need_name=not hub.brain.addressed('', source, began)))
                preview = None
                if text and wake_only(text) and clip.voiced < WAKE_ONLY_SPEECH:
                    continue
                if hub.brain.interruption and text:
                    from .brain import INTERRUPT, repeats_playback

                    if repeats_playback(text, hub.brain.spoken_reply):
                        continue
                    marker = INTERRUPT.match(text)
                    if marker and not text.lower().startswith('stop listening'):
                        text = text[marker.end():].strip()
                if text and valid() and hub.brain.addressed(text, source, began):
                    _, name = people.speaker(source, clip.speech, clip.voiced, addressed=True)
                    if valid():
                        if name and hub.sight:
                            hub.sight.note_voice(name)
                        face, face_by = (hub.sight.solo_identity(within=8.)
                                         if hub.sight and not name else (None, 'face'))
                        hub.brain.submit(text, source, heard_at=began, name=name, face=face,
                                         face_by=face_by or 'face', speech_end=clip.last_speech, audio=clip.speech)
            except Exception as error:
                hub.note('hearing', f'Transcription failed: {error}')
            finally:
                self.decoding = None

    @staticmethod
    def valid_clip(clip):
        return clip.revision == hub.brain.revision and hub.accepts(clip.source, 'ears')

    def wait_for_interrupted_reply(self, clip):
        # Keep the full utterance while the previous synthesizer and speaker stop.
        deadline = clip.queued + 4.
        while self.valid_clip(clip) and hub.brain.interruption and time.monotonic() < deadline:
            if not hub.brain.busy.is_set():
                return True
            time.sleep(.025)
        return False


    parakeet = None

    @staticmethod
    def level(audio):
        # Bring distant, quiet speech up to a steady level (at most 8x, so noise stays noise).
        frames = audio[:len(audio) // 320 * 320].reshape(-1, 320)
        if len(frames):
            level = float(np.percentile(np.sqrt(np.mean(frames ** 2, axis=1)), 90))
            audio = np.clip(audio * min(8., .08 / max(level, 1e-5)), -1, 1).astype(np.float32)
        return audio

    def read_fast(self, audio):
        import mlx.core as mx
        from parakeet_mlx.audio import get_logmel

        mel = get_logmel(mx.array(self.level(audio)), self.parakeet.preprocessor_config)
        text = self.parakeet.generate(mel)[0].text.strip()
        return text if re.search(r'[A-Za-z]', text) else ''

    def listen(self, whisper, audio, *, need_name):
        """What was said. Fast first; Whisper's name-hinted listen only when the name matters and was missed."""
        if self.parakeet is None:
            return self.transcribe(whisper, audio)
        try:
            text = self.read_fast(audio)
        except Exception:
            return self.transcribe(whisper, audio)
        if need_name and not (text and wake_only(text)):
            from .brain import wake_request

            if not text or wake_request(text) is None:
                second = self.transcribe(whisper, audio)
                if second and wake_request(second) is not None:
                    return second
        return text

    @staticmethod
    def transcribe(engine, audio, *, prompt=True):
        # Bring distant, quiet speech up to a steady level (at most 8x, so noise stays noise).
        frames = audio[:len(audio) // 320 * 320].reshape(-1, 320)
        if len(frames):
            level = float(np.percentile(np.sqrt(np.mean(frames ** 2, axis=1)), 90))
            audio = np.clip(audio * min(8., .08 / max(level, 1e-5)), -1, 1).astype(np.float32)
        result = engine.transcribe(audio, path_or_hf_repo=WHISPER, language='en',
                                   condition_on_previous_text=False, temperature=0,
                                   initial_prompt=WAKE_HINT if prompt else None)
        return ' '.join(segment['text'].strip() for segment in result.get('segments', [])
                        if segment.get('no_speech_prob', 0) < .55 and segment.get('avg_logprob', 0) > -.95)
