"""VAD and local transcription for the single selected input."""
from collections import deque
from dataclasses import dataclass
import queue
import threading
import time

import numpy as np

from .hub import hub
from . import voice
from .people import people

SR = 16000
FRAME = 512
WHISPER = 'mlx-community/whisper-large-v3-turbo'


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


class Hearing:
    def __init__(self):
        self.audio = queue.Queue(maxsize=80)
        self.clips = queue.Queue(maxsize=3)
        self.whisper_ready = False
        self.detector_ready = False
        self.activity_at = 0
        self.capture = None
        self.decoding = None

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
        if not self.ready or not hub.accepts(source, 'ears') or (
                hub.brain.busy.is_set() and not hub.brain.can_interrupt(source) and not hub.brain.interruption):
            return
        try:
            self.audio.put_nowait((source, hub.brain.revision, time.monotonic(), samples))
        except queue.Full:
            pass

    def _detect(self):
        try:
            import torch
            from silero_vad import load_silero_vad

            torch.set_num_threads(1)
            detector = load_silero_vad()
        except Exception as error:
            hub.note('vad', f'Speech detector unavailable: {error}')
            return
        self.detector_ready = True
        identity = None
        tail = np.empty(0, np.float32)
        pre = deque(maxlen=12)
        clip = []
        voiced = quiet = onset = 0
        last = 0
        talking_over = False
        last_probe = 0
        last_speech = 0.
        previewed = False
        while True:
            source, revision, received, samples = self.audio.get()
            if (source, revision) != identity or received - last > 0.45:
                identity = source, revision
                tail = np.empty(0, np.float32)
                self.capture = None
                pre.clear()
                clip.clear()
                voiced = quiet = onset = 0
                detector.reset_states()
            last = received
            can_interrupt = hub.brain.can_interrupt(source)
            if revision != hub.brain.revision or not hub.accepts(source, 'ears') or (
                    hub.brain.busy.is_set() and not can_interrupt and not hub.brain.interruption):
                continue
            audio = np.concatenate((tail, samples))
            end = len(audio) // FRAME * FRAME
            tail = audio[end:]
            for index in range(0, end, FRAME):
                frame = audio[index:index + FRAME]
                frame_at = received - (len(audio)-index-FRAME) / SR
                hub.level = min(1.0, float(np.sqrt(np.mean(frame ** 2))) * 8)
                speech = detector(torch.from_numpy(frame), SR).item() > (0.32 if clip else 0.5)
                if not clip:
                    pre.append(frame)
                    onset = onset + 1 if speech else 0
                    if onset >= 3:
                        talking_over = can_interrupt
                        last_probe = 0
                        last_speech, previewed = frame_at, False
                        began = frame_at - (onset - 1) * FRAME / SR
                        self.activity_at = received
                        clip = list(pre)
                        voiced, quiet = onset, 0
                    continue
                self.capture = (source, revision, began, received)
                clip.append(frame)
                voiced += int(speech)
                if speech:
                    self.activity_at = received
                    last_speech = frame_at
                    previewed = False
                quiet = 0 if speech else quiet + 1
                probe = voiced - last_probe >= 12 or last_probe == 0 and voiced >= 6 and quiet == 6
                if talking_over and can_interrupt and probe and voiced <= 60:
                    last_probe = voiced
                    candidate = np.concatenate(clip)
                    if not self.clips.full() and not voice.is_echo(candidate, frame_at):
                        self.clips.put_nowait(Clip('barge', source, revision, received, began, frame_at, candidate))
                if (not talking_over and not previewed and voiced >= 10 and quiet >= 22
                        and not hub.brain.busy.is_set() and self.clips.empty()):
                    previewed = True
                    self.clips.put_nowait(Clip('preview', source, revision, time.monotonic(),
                                                began, last_speech, np.concatenate(clip)))
                # Natural mid-sentence pauses are often longer than half a second.
                # Give a longer utterance extra room, including on the initial wake.
                pause = 1.45 if voiced * FRAME / SR < 2.5 else 2.05
                if quiet * FRAME / SR >= pause or len(clip) * FRAME / SR >= 60:
                    self.capture = None
                    admitted = not talking_over or hub.brain.interruption
                    if admitted and voiced >= 7 and not self.clips.full():
                        recording = np.concatenate(clip)
                        speech_end = (len(clip) - max(0, quiet - 2)) * FRAME
                        self.clips.put_nowait(Clip('final', source, revision, time.monotonic(), began, last_speech,
                                                   recording, recording[:speech_end], voiced * FRAME / SR))
                    clip.clear()
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
        self.whisper_ready = True
        preview = None
        while True:
            clip = self.clips.get()
            source, revision, began = clip.source, clip.revision, clip.began
            if clip.kind == 'barge':
                if revision == hub.brain.revision and time.monotonic() - clip.queued < .9 and hub.brain.can_interrupt(source):
                    try:
                        text = self.transcribe(mlx_whisper, clip.audio)
                        _, voice_name = people.speaker(source, clip.audio, len(clip.audio)/SR, addressed=True)
                        hub.brain.interrupt(source, text, voice_name=voice_name)
                    except (ValueError, RuntimeError, OSError):
                        pass
                continue
            valid = lambda: revision == hub.brain.revision and hub.accepts(source, 'ears') and not hub.brain.busy.is_set()
            if time.monotonic() - clip.queued > 2 or not valid():
                continue
            stamp = (source, revision, began, clip.last_speech)
            if clip.kind == 'preview':
                try:
                    text = self.transcribe(mlx_whisper, clip.audio)
                    preview = (stamp, text) if text else None
                except (ValueError, RuntimeError, OSError):
                    preview = None
                continue
            self.decoding = (source, revision, began, time.monotonic())
            try:
                enrolled, _ = people.speaker(source, clip.speech, clip.voiced, addressed=False)
                if enrolled:
                    continue
                text = preview[1] if preview and preview[0] == stamp else self.transcribe(mlx_whisper, clip.audio)
                preview = None
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
                        face = hub.sight.solo_recognized() if hub.sight and not name else None
                        hub.brain.submit(text, source, heard_at=began, name=name, face=face, speech_end=clip.last_speech)
            except Exception as error:
                hub.note('hearing', f'Transcription failed: {error}')
            finally:
                self.decoding = None


    @staticmethod
    def transcribe(engine, audio):
        result = engine.transcribe(audio, path_or_hf_repo=WHISPER, language='en',
                                   condition_on_previous_text=False, temperature=0)
        return ' '.join(segment['text'].strip() for segment in result.get('segments', [])
                        if segment.get('no_speech_prob', 0) < .55 and segment.get('avg_logprob', 0) > -.95)
