"""Opt-in, on-device identity cues. Persist embeddings only."""
import json
import os
import threading
import time

import numpy as np

from . import config

FACE = 'face_recognition_sface_2021dec.onnx'
VOICE = '3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx'
COUNT = {'face': 6, 'voice': 3}
THRESHOLD = {'face': (0.48, 0.075), 'voice': (0.69, 0.10)}


def normalized(values):
    vector = np.asarray(values, dtype=np.float32).ravel()
    length = np.linalg.norm(vector)
    return vector / length if length > 1e-6 and np.isfinite(length) else None


class People:
    def __init__(self):
        self.lock = threading.RLock()
        self.path = config.HOME / 'people-v1.json'
        self.models = {}
        self.session = None
        self.message = ''
        try:
            self.profiles = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.profiles = {}

    def model(self, modality):
        with self.lock:
            if modality not in self.models:
                if modality == 'face':
                    import cv2
                    self.models[modality] = cv2.FaceRecognizerSF.create(str(config.ROOT / 'mac' / FACE), '')
                else:
                    import sherpa_onnx
                    settings = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                        model=str(config.ROOT / 'mac' / VOICE), num_threads=1, debug=False)
                    self.models[modality] = sherpa_onnx.SpeakerEmbeddingExtractor(settings)
            return self.models[modality]

    def save(self, profiles):
        path = self.path.with_suffix('.tmp')
        with os.fdopen(os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600), 'w') as output:
            json.dump(profiles, output, separators=(',', ':'))
        path.replace(self.path)
        self.profiles = profiles

    def active(self):
        with self.lock:
            if self.session and time.monotonic() >= self.session['expires']:
                self.session = None
                self.message = 'Enrollment timed out. Try again when ready.'
            return self.session

    def state(self):
        session = self.active()
        with self.lock:
            return {
                'profiles': [{'name': person, 'face': 'face' in data, 'voice': 'voice' in data}
                             for person, data in sorted(self.profiles.items())],
                'enrollment': ({'name': session['name'], 'kind': session['kind'],
                                'progress': len(session['samples']), 'total': COUNT[session['kind']]} if session else None),
                'message': self.message,
            }

    def names(self, kind):
        with self.lock:
            return {name for name, profile in self.profiles.items() if kind in profile}

    def needed(self, kind):
        session = self.active()
        with self.lock:
            return bool(session and session['kind'] == kind or any(kind in data for data in self.profiles.values()))

    def begin(self, name, kind, source):
        name = ' '.join(name.split())
        if not name or len(name) > 32 or not all(c.isalpha() or c in " -'’" for c in name):
            raise ValueError('Enter a name of up to 32 letters.')
        model = FACE if kind == 'face' else VOICE
        if not (config.ROOT / 'mac' / model).exists():
            raise ValueError('Recognition model missing. Run mac/setup.sh.')
        with self.lock:
            if len(self.profiles) >= 12 and name not in self.profiles:
                raise ValueError('Remove a profile to make room.')
            self.session = {'name': name, 'kind': kind, 'source': source,
                            'samples': [], 'expires': time.monotonic() + 65}
            self.message = ('Face the camera by yourself and slowly turn your head a little.'
                            if kind == 'face' else 'Say three different sentences, with a short pause between them.')

    def cancel(self):
        with self.lock:
            self.session = None
            self.message = ''

    def remove(self, name):
        with self.lock:
            self.save({key: value for key, value in self.profiles.items() if key != name})
            if self.session and self.session['name'] == name:
                self.session = None
            self.message = 'Profile removed.'

    def enroll_sample(self, session, vector, track=None):
        with self.lock:
            if self.session is not session or vector is None:
                return
            if track is not None:
                if 'track' in session and session['track'] != track:
                    session['samples'].clear()
                session['track'] = track
            samples = session['samples']
            if samples and float(np.dot(samples[0], vector)) < (0.5 if track is not None else 0.42):
                self.message = 'That sample sounded or looked different. Try another one by yourself.'
                return
            samples.append(vector)
            if len(samples) >= COUNT[session['kind']]:
                name = session['name']
                updated = {**self.profiles.get(name, {}), session['kind']: [sample.tolist() for sample in samples]}
                self.save({**self.profiles, name: updated})
                self.session = None
                self.message = 'Saved ' + session['kind'] + ' for ' + name + '.'
            else:
                self.message = 'Sample captured. ' + ('Keep looking toward the camera.' if track is not None else 'Say a different sentence.')

    def match(self, kind, vector, *, extra_confidence=0.):
        if vector is None:
            return None
        with self.lock:
            candidates = [(name, max(float(np.dot(vector, sample)) for sample in profile[kind]))
                          for name, profile in self.profiles.items() if kind in profile and profile[kind]
                          and len(profile[kind][0]) == len(vector)]
        candidates.sort(key=lambda item: item[1], reverse=True)
        threshold, gap = THRESHOLD[kind]
        if candidates and candidates[0][1] >= threshold + extra_confidence and (len(candidates) == 1 or candidates[0][1] - candidates[1][1] >= gap):
            return candidates[0][0]
        return None

    def face(self, image, row, source, track, alone):
        session = self.active()
        if row[2] < 45 or not self.needed('face'):
            return None
        model = self.model('face')
        vector = normalized(model.feature(model.alignCrop(image, row)))
        if session and session['kind'] == 'face' and session['source'] == source and alone:
            self.enroll_sample(session, vector, track)
        return self.match('face', vector)

    def speaker(self, source, audio, voiced, addressed):
        session = self.active()
        enrollment = bool(session and session['kind'] == 'voice' and session['source'] == source)
        minimum = 1.4 if enrollment else .75
        if voiced < minimum or not (enrollment or addressed and self.needed('voice')):
            return enrollment, None
        try:
            model = self.model('voice')
            stream = model.create_stream()
            stream.accept_waveform(sample_rate=16000, waveform=audio)
            stream.input_finished()
            vector = normalized(model.compute(stream)) if model.is_ready(stream) else None
            if enrollment:
                self.enroll_sample(session, vector)
            return enrollment, self.match('voice', vector, extra_confidence=.06 if voiced < 1.4 else 0.)
        except (RuntimeError, ImportError, OSError, AttributeError, ValueError):
            if enrollment:
                with self.lock:
                    self.message = 'Voice recognition unavailable. Check the model and microphone.'
            return enrollment, None


people = People()
