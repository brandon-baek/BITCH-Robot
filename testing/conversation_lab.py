"""A simulated room for measuring how a conversation with BITCH feels.

    OLLAMA_URL=http://127.0.0.1:11436 ~/dc-venv/bin/python -m testing.conversation_lab [scenario ...]

Real hearing (Silero VAD + Whisper), the real brain and model, and the real voice run unchanged. Only the devices
are fake: a microphone that streams a room in real time, and a speaker that records what BITCH says and feeds it
back into that microphone with a delay, the way a speaker on a desk does. People in the room are synthesized voices.

For every turn it measures what a person feels:
  - first words: seconds from the end of their speech to BITCH's first word leaving the speaker
  - heard: what Whisper understood, against what was said
  - gaps: silence inside one reply (between sentences), and any beeps
  - self-interruptions: BITCH stopping without anyone talking over it
  - barge-in: how fast BITCH stops when someone does talk over it, and whether it then answers them
Nothing here touches ~/.bitch.
"""
import io
import json
import os
import queue
import re
import sys
import tempfile
import threading
import time
import wave

import numpy as np

SCRATCH = tempfile.mkdtemp(prefix='bitch-lab-')
os.environ['BITCH_HOME'] = SCRATCH
os.environ.setdefault('OLLAMA_URL', 'http://127.0.0.1:11436')

from core import config  # noqa: E402

config.update({'mic_source': 'lab-mic', 'speaker_source': 'lab-speaker', 'camera_source': 'off', 'volume': 60,
               'muted': False, 'mic_muted': False, 'idle': False, 'check_ins': False, 'greet': False,
               'natural_memory': True, 'owner': 'Brandon',
               'home_place': {'name': 'San Jose, California', 'lat': 37.339, 'lon': -121.895, 'imperial': True}})

from core.devices.base import Device  # noqa: E402
from core.hub import hub  # noqa: E402

MIC_SR = 16000
FRAME = 512                      # 32 ms, what the real microphones deliver
ECHO_DELAY = .25                 # speaker to mic, as measured on the desk (Bluetooth + air)
ECHO_GAIN = .35
NOISE = .0025                    # quiet room hiss


def words(text):
    return re.findall(r"[a-z0-9']+", (text or '').lower())


def wer(said, heard):
    """Word error rate, ignoring the wake word and punctuation."""
    ref = [w for w in words(said) if w not in ('hey', 'bitch', 'yo', 'okay')]
    hyp = [w for w in words(heard) if w not in ('hey', 'bitch', 'yo', 'okay')]
    d = np.arange(len(hyp) + 1)
    for i, r in enumerate(ref, 1):
        prev, d[0] = d.copy(), i
        for j, h in enumerate(hyp, 1):
            d[j] = min(prev[j] + 1, d[j - 1] + 1, prev[j - 1] + (r != h))
    return d[len(hyp)] / max(1, len(ref))


class Voices:
    """People in the room, spoken by Kokoro voices at 16 kHz."""
    def __init__(self):
        from kokoro import KPipeline
        from scipy.signal import resample_poly
        self.pipeline = KPipeline(lang_code='a', repo_id='hexgrad/Kokoro-82M')
        self.resample = resample_poly
        self.cache = {}

    def say(self, text, voice='am_michael', speed=1.0):
        key = (text, voice, speed)
        if key not in self.cache:
            audio = np.concatenate([np.asarray(r.audio, dtype=np.float32).reshape(-1)
                                    for r in self.pipeline(text, voice=voice, speed=speed)])
            audio = self.resample(audio, 2, 3).astype(np.float32)   # 24 kHz -> 16 kHz
            loud = np.flatnonzero(np.abs(audio) > .02 * np.abs(audio).max())
            audio = audio[max(0, loud[0] - 800):loud[-1] + 800]      # trim the synthesizer's silent padding
            self.cache[key] = audio * (.32 / max(1e-4, float(np.abs(audio).max())))
        return self.cache[key]


class Room:
    """Everything the microphone hears, on one clock."""
    def __init__(self):
        self.lock = threading.Lock()
        self.sources = []          # (start time, samples at 16 kHz, gain, tag)
        self.started = time.monotonic()

    def add(self, at, samples, gain=1., tag=''):
        with self.lock:
            self.sources.append([at, samples, gain, tag])

    def cut(self, tag, at):
        """Stop a sound (BITCH's echo when its speaker is hushed)."""
        with self.lock:
            for source in self.sources:
                if source[3] == tag:
                    keep = max(0, int((at - source[0]) * MIC_SR))
                    source[1] = source[1][:keep]

    def frame(self, at):
        out = np.random.default_rng().normal(0, NOISE, FRAME).astype(np.float32)
        with self.lock:
            alive = []
            for start, samples, gain, tag in self.sources:
                offset = int((at - start) * MIC_SR)
                if offset + FRAME <= 0:
                    alive.append([start, samples, gain, tag])
                    continue
                if offset >= len(samples):
                    continue
                lo, hi = max(0, offset), min(len(samples), offset + FRAME)
                out[lo - offset:hi - offset] += gain * samples[lo:hi]
                alive.append([start, samples, gain, tag])
            self.sources = alive
        return np.clip(out, -1, 1)


class LabMic(Device):
    def __init__(self, room):
        super().__init__('lab-mic', 'Lab microphone', ('ears',))
        self.room = room

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        next_at = time.monotonic()
        while True:
            frame = self.room.frame(next_at)
            if self.hub.hearing:
                self.hub.hearing.feed(self.id, frame)
            next_at += FRAME / MIC_SR
            time.sleep(max(0., next_at - time.monotonic()))


class LabSpeaker(Device):
    """Records what BITCH says and plays it back into the room's microphone."""
    def __init__(self, room):
        super().__init__('lab-speaker', 'Lab speaker', ('voice',))
        self.room = room
        self.events = []           # dict(start, duration, end, hushed)
        self.lock = threading.Lock()
        self.current = None

    def speak(self, wav):
        from scipy.signal import resample_poly
        with wave.open(io.BytesIO(wav)) as file:
            rate = file.getframerate()
            pcm = np.frombuffer(file.readframes(file.getnframes()), '<i2').astype(np.float32) / 32768
        now = time.monotonic()
        heard = resample_poly(pcm, MIC_SR, rate).astype(np.float32) if rate != MIC_SR else pcm
        with self.lock:
            self.hush_locked(now)
            tag = f'bitch-{len(self.events)}'
            event = {'start': now, 'duration': len(pcm) / rate, 'end': now + len(pcm) / rate, 'hushed': None,
                     'tag': tag, 'peak': float(np.abs(pcm).max()), 'rms': float(np.sqrt(np.mean(pcm ** 2)))}
            self.events.append(event)
            self.current = event
        self.room.add(now + ECHO_DELAY, heard, ECHO_GAIN, tag)
        return True

    def speak_stream(self, chunks):
        """Like a real speaker: plays PCM in real time as it arrives, and echoes it into the room."""
        from scipy.signal import resample_poly
        now = time.monotonic()
        with self.lock:
            self.hush_locked(now)
            tag = f'bitch-{len(self.events)}'
            event = {'start': now, 'duration': 0., 'end': now + 30, 'hushed': None, 'tag': tag, 'live': True}
            self.events.append(event)
            self.current = event

        def run():
            played = 0.
            for data in chunks:
                pcm = np.frombuffer(data, '<i2').astype(np.float32) / 32768
                with self.lock:
                    if event['hushed'] is not None:
                        return
                    at = max(time.monotonic(), event['start'] + played)   # a gap if the voice falls behind
                    if at > event['start'] + played + .02:
                        event.setdefault('stalls', []).append(round(at - event['start'] - played, 2))
                        event['start'] = at - played
                    played += len(pcm) / 24000
                    event['duration'] = played
                    event['end'] = event['start'] + played
                self.room.add(event['start'] + played - len(pcm) / 24000 + ECHO_DELAY,
                              resample_poly(pcm, MIC_SR, 24000).astype(np.float32), ECHO_GAIN, tag)
            with self.lock:
                event['end'] = event['start'] + played
        threading.Thread(target=run, daemon=True).start()
        return True

    def playing(self):
        with self.lock:
            return bool(self.current and self.current['hushed'] is None and time.monotonic() < self.current['end'])

    def hush_locked(self, now):
        if self.current and self.current['hushed'] is None and now < self.current['end']:
            self.current['hushed'] = now
            self.current['end'] = now
            self.room.cut(self.current['tag'], now + ECHO_DELAY)

    def hush(self):
        with self.lock:
            self.hush_locked(time.monotonic())


class Lab:
    def __init__(self):
        from core.brain import Brain
        from core.hearing import Hearing
        self.room = Room()
        self.voices = Voices()
        self.all_marks = []
        hub.brain, hub.hearing, hub.sight = Brain(), Hearing(), None
        self.speaker = LabSpeaker(self.room)
        hub.add(self.speaker)
        hub.add(LabMic(self.room))
        hub.status = 'Waiting for “BITCH”'
        hub.brain.start()
        hub.hearing.start()
        deadline = time.monotonic() + 180
        while not hub.hearing.ready and time.monotonic() < deadline:
            time.sleep(.2)
        from core import llm, voice
        llm.chat([{'role': 'user', 'content': 'Hi.'}], max_tokens=2, timeout=300)
        llm.warm_context(hub.brain.spoken_system())
        voice.render('Warming up.')
        if voice.load_qwen():
            reply = voice.LiveReply()
            reply.add('Warming up the live voice.')
            reply.close()
            reply.done.wait(60)
        self.marks = []
        self.trace()

    def mark(self, name):
        self.marks.append((name, time.monotonic()))
        self.all_marks.append(self.marks[-1])   # the whole run, for tracing what happened between turns

    def trace(self):
        """Timestamp every stage of a turn, so slow stages show up by name."""
        from core import llm, voice
        brain, hearing = hub.brain, hub.hearing
        submit, chat, offer = brain.submit, llm.chat, voice.ReplyStream.offer
        put = hearing.clips.put_nowait
        lab = self

        def traced_put(clip):
            lab.mark('clip-' + clip.kind)
            return put(clip)

        def traced_submit(text, *args, **kwargs):
            lab.mark('transcribed')
            return submit(text, *args, **kwargs)

        def traced_chat(messages, *args, **kwargs):
            lab.mark('model-start')
            on_delta = kwargs.get('on_delta')
            if on_delta:
                first = [True]
                def delta(content):
                    if first[0]:
                        lab.mark('model-first-token')
                        first[0] = False
                    return on_delta(content)
                kwargs['on_delta'] = delta
            result = chat(messages, *args, **kwargs)
            lab.mark('model-done')
            return result

        def traced_offer(stream, phrase, *args, **kwargs):
            lab.mark('first-phrase' if not stream.parts else 'phrase')
            return offer(stream, phrase, *args, **kwargs)

        hearing.clips.put_nowait = traced_put
        listen = hearing.listen

        def traced_listen(whisper, audio, *, need_name):
            started = time.monotonic()
            text = listen(whisper, audio, need_name=need_name)
            lab.mark(f'listen {time.monotonic() - started:.2f}s (waited {voice.Urgent.waited:.2f}s for the GPU)')
            return text
        hearing.listen = traced_listen
        brain.submit = traced_submit
        llm.chat = traced_chat
        voice.ReplyStream.offer = traced_offer
        speak = self.speaker.speak

        def traced_speak(wav):
            lab.mark('speak')
            return speak(wav)
        self.speaker.speak = traced_speak
        speak_stream = self.speaker.speak_stream

        def traced_stream(chunks):
            lab.mark('speak')
            return speak_stream(chunks)
        self.speaker.speak_stream = traced_stream
        emit = voice.LiveReply.emit

        def traced_emit(reply, samples):
            if not reply.pcm:
                lab.mark('voice-first-chunk')
            return emit(reply, samples)
        voice.LiveReply.emit = traced_emit
        interrupt = brain.interrupt

        def traced_interrupt(source, text, *args, **kwargs):
            result = interrupt(source, text, *args, **kwargs)
            lab.mark(f'interrupt({text!r}, strong={kwargs.get("strong")}, sustained={kwargs.get("sustained")}) -> {result}')
            return result
        brain.interrupt = traced_interrupt

    def wait_idle(self, timeout=60):
        """Until BITCH has finished its turn and the speaker is quiet."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not hub.brain.busy.is_set() and not self.speaker.playing():
                return True
            time.sleep(.05)
        return False

    def run(self, name, script):
        """script: list of turns {say, voice?, gap?, barge?} -> list of measured turns."""
        results = []
        hub.sight = None
        previous_end = time.monotonic()
        for turn in script:
            if turn.get('room'):
                from unittest.mock import Mock
                hub.sight = Mock(in_view=Mock(return_value={'names': [], 'others': turn['room']}),
                                 current_scene=Mock(return_value=(None, turn['room'])), solo_identity=Mock(return_value=(None, None)),
                                 face_to_learn=Mock(return_value=None), where_is=Mock(return_value=None))
            audio = self.voices.say(turn['say'], turn.get('voice', 'am_michael'), turn.get('speed', 1.))
            if turn.get('after') is not None:
                at = previous_end + turn['after']
                time.sleep(max(0., at - time.monotonic()))
            elif turn.get('barge') is not None:
                # Talk over BITCH this many seconds after its reply starts.
                start = self.wait_for_speech(timeout=30)
                if start is None:
                    results.append({'say': turn['say'], 'error': 'BITCH never started talking to barge into'})
                    continue
                at = start + turn['barge']
                time.sleep(max(0., at - time.monotonic()))
            else:
                self.wait_idle()
                time.sleep(turn.get('gap', 1.0))
                at = time.monotonic() + .05
            events_before = len(self.speaker.events)
            messages_before = time.time()
            self.room.add(at, audio, turn.get('gain', 1.), tag='person')
            speech_end = at + len(audio) / MIC_SR
            previous_end = speech_end
            if turn.get('continues'):
                # The next part follows right away: don't wait for (or measure) a reply in between.
                continue
            self.marks = []
            following = script[script.index(turn) + 1] if script.index(turn) + 1 < len(script) else {}
            if following.get('barge') is not None:
                results.append({'say': turn['say'], 'note': 'reply left running for the next turn to talk over'})
                continue
            results.append(self.measure(turn, at, speech_end, events_before, messages_before))
            stages = {}
            for name, t in self.marks:
                if t >= at and name not in stages:
                    stages[name] = round(t - speech_end, 2)
            from core import llm
            stages['prompt'] = getattr(llm, 'last_stats', None)
            results[-1]['stages'] = stages
        self.wait_idle()
        return {'scenario': name, 'turns': results}

    def wait_for_speech(self, timeout):
        deadline = time.monotonic() + timeout
        count = len(self.speaker.events)
        while time.monotonic() < deadline:
            with self.speaker.lock:
                fresh = [e for e in self.speaker.events[count:] if e['duration'] > .6]
            if fresh:
                return fresh[0]['start']
            live = [e for e in self.speaker.events[count:] if e.get('live')]
            if live and time.monotonic() - live[0]['start'] > .6:
                return live[0]['start']
            time.sleep(.02)
        return None

    def measure(self, turn, at, speech_end, events_before, messages_before):
        # Wait for the reply to start (end of turn + transcription), then for it to finish.
        time.sleep(max(0., speech_end - time.monotonic()) + .05)
        deadline = time.monotonic() + 6
        while not hub.brain.busy.is_set() and time.monotonic() < deadline:
            time.sleep(.02)
        self.wait_idle(timeout=45)
        time.sleep(.4)
        with self.speaker.lock:
            events = [dict(e) for e in self.speaker.events[events_before:]]
            interrupted = [dict(e) for e in self.speaker.events[:events_before] if e['hushed'] and e['hushed'] >= at]
        new = [m for m in hub.messages if m['at'] >= messages_before]
        heard = ' '.join(m['text'] for m in new if m['role'] == 'user')
        reply = ' '.join(m['text'] for m in new if m['role'] == 'robot')
        spoken = [e for e in events if e['start'] >= speech_end - .2]
        voice_events = [e for e in spoken if e['duration'] >= .45]
        cues = [e for e in spoken if e['duration'] < .45]
        first = voice_events[0]['start'] - speech_end if voice_events else None
        gaps = [round(b['start'] - a['end'], 2) for a, b in zip(voice_events, voice_events[1:])]
        if turn.get('expect') == 'quiet':
            # Said between people: the right answer is silence.
            return {'say': turn['say'], 'heard': heard, 'reply': reply, 'quiet_ok': not voice_events and not reply,
                    'first_words': None, 'gaps': [], 'stalls': [], 'self_interrupted': 0, 'wer': None}
        result = {
            'say': turn['say'], 'heard': heard, 'wer': round(wer(turn['say'], heard), 2) if heard else 1.0,
            'reply': reply, 'first_words': round(first, 2) if first is not None else None,
            'gaps': gaps, 'cues': len(cues),
            'self_interrupted': sum(1 for e in voice_events if e['hushed'] and e['hushed'] < e['start'] + e['duration'] - .1),
            'stalls': [s for e in voice_events for s in e.get('stalls', [])],
        }
        if turn.get('barge') is not None:
            stopped = [e['hushed'] for e in interrupted]
            result['barge_stop'] = round(min(stopped) - at, 2) if stopped else None
        return result


SCENARIOS = {
    'quick': [
        {'say': "Hey BITCH, what's fifteen percent of eighty?"},
        {'say': "Nice. And what's that times three?", 'gap': .8},
        {'say': "Cool, thanks.", 'gap': .8},
    ],
    'chat': [
        {'say': "Hey BITCH, I'm back."},
        {'say': "I failed my chemistry exam today.", 'gap': .7},
        {'say': "Yeah, it was the stoichiometry part. I just blanked.", 'gap': .7},
        {'say': "What should I do to study better next time?", 'gap': .7},
    ],
    'pauses': [
        # Mid-sentence pauses must not end the turn early: one turn, said in three parts.
        {'say': "Hey BITCH, so I was thinking about, um,", 'continues': True},
        {'say': "building a little base for you out of plywood,", 'after': .9, 'continues': True},
        {'say': "Is that a good idea?", 'after': .7},
    ],
    'barge': [
        {'say': "Hey BITCH, tell me a story about a robot who learns to cook. Make it at least six sentences."},
        {'say': "Wait, actually, make it about a robot who learns to paint.", 'barge': 3.0},
    ],
    'family': [
        # Three people in the room. BITCH answers what's meant for it and stays out of what isn't.
        {'say': "Hey BITCH, this is my family. Say hi to everyone.", 'room': 3},
        {'say': "Hi BITCH! It's nice to meet you.", 'voice': 'af_heart', 'gap': .8},
        {'say': "Mom, do you want some tea?", 'gap': .8, 'expect': 'quiet'},
        {'say': "Sure, thanks honey.", 'voice': 'af_heart', 'gap': .6, 'expect': 'quiet'},
        {'say': "Hey BITCH, set a timer for five minutes for the tea.", 'voice': 'af_bella', 'gap': .8},
        {'say': "Hey BITCH, what's the weather going to be like tomorrow?", 'gap': 1.},
        {'say': "Hey BITCH, tell us a really short joke.", 'voice': 'af_bella', 'gap': 1.},
        {'say': "Okay, it's your turn to do the dishes.", 'voice': 'af_heart', 'gap': .8, 'expect': 'quiet'},
        {'say': "Thanks BITCH, that's all for now.", 'gap': 1.},
    ],
    'echo': [
        # A long reply with nobody talking over it: BITCH must not stop itself.
        {'say': "Hey BITCH, explain how a servo motor works, in a few sentences."},
    ],
}


if __name__ == '__main__':
    names = sys.argv[1:] or list(SCENARIOS)
    lab = Lab()
    report = [lab.run(name, SCENARIOS[name]) for name in names]
    print(json.dumps(report, indent=1))
