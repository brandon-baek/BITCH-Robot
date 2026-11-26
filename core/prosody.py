"""How something was said: pitch, pitch range, loudness and pace, against the speaker's usual.

Words alone miss tone. "Great." said flat and slow is sarcasm; said high and quick it is
excitement. This measures the voice cheaply from the utterance audio and turns the
difference from that mic's usual voice into a short note for the model."""
import threading

import numpy as np

SR = 16000
FRAME = 640       # 40 ms
lock = threading.Lock()
usual = {}        # source -> running averages of the features below


def pitch_track(audio):
    """Pitch in Hz for each clearly voiced 40 ms frame, by autocorrelation."""
    usable = len(audio) // FRAME * FRAME
    if not usable:
        return np.zeros(0), np.zeros(0)
    frames = audio[:usable].reshape(-1, FRAME).astype(np.float64)
    levels = np.sqrt(np.mean(frames ** 2, axis=1))
    loud = levels >= max(.25 * levels.max(), 1e-4)
    pitches = []
    for frame in frames[loud]:
        frame = frame - frame.mean()
        spectrum = np.fft.rfft(frame * np.hanning(FRAME), 2 * FRAME)
        correlation = np.fft.irfft(np.abs(spectrum) ** 2)[:FRAME]
        low, high = SR // 400, SR // 70
        if correlation[0] <= 0:
            continue
        lag = low + int(np.argmax(correlation[low:high]))
        if correlation[lag] / correlation[0] >= .35:
            pitches.append(SR / lag)
    return np.array(pitches), levels[loud]


def features(audio, words):
    pitches, levels = pitch_track(np.asarray(audio, dtype=np.float32))
    if len(pitches) < 6:
        return None
    semitones = 12 * np.log2(pitches / np.median(pitches))
    spoken = len(levels) * FRAME / SR
    return {
        'pitch': float(np.median(pitches)),
        'range': float(np.percentile(semitones, 90) - np.percentile(semitones, 10)),
        'loudness': float(20 * np.log10(np.median(levels) + 1e-6)),
        'pace': words / max(spoken, .4),
        'rise': float(np.mean(semitones[-max(2, len(semitones) // 5):]) - np.mean(semitones)),
    }


def describe(source, audio, text):
    """A short note on tone, or None while this mic's usual voice is still unknown."""
    if audio is None or len(audio) < SR * .6:
        return None
    measured = features(audio, len(text.split()))
    if measured is None:
        return None
    with lock:
        baseline = usual.get(source)
        count = baseline['count'] if baseline else 0
        notes = []
        if count >= 3:
            loud = measured['loudness'] - baseline['loudness']
            pitch = 12 * np.log2(measured['pitch'] / baseline['pitch'])
            liveliness = measured['range'] / max(baseline['range'], .5)
            pace = measured['pace'] / max(baseline['pace'], .5)
            if loud >= 5:
                notes.append('louder')
            elif loud <= -6:
                notes.append('quieter')
            if pitch >= 2.2:
                notes.append('higher-pitched')
            elif pitch <= -2:
                notes.append('lower-pitched')
            if pace >= 1.25:
                notes.append('faster')
            elif pace <= .78:
                notes.append('slower')
            if liveliness >= 1.45:
                notes.append('with lively, swinging pitch')
            elif liveliness <= .62:
                notes.append('flat, with little pitch movement')
        if measured['rise'] >= 2.5 and not text.rstrip().endswith('?'):
            notes.append('rising at the end like a question')
        # Keep learning this mic's usual voice; a big outlier moves it only a little.
        weight = 1 / (count + 1) if count < 8 else .1
        if notes:
            weight = min(weight, .06)
        if baseline is None:
            usual[source] = {**measured, 'count': 1}
        else:
            for key in ('pitch', 'range', 'loudness', 'pace'):
                baseline[key] += weight * (measured[key] - baseline[key])
            baseline['count'] = count + 1
    if not notes:
        return None
    comparisons = [note for note in notes if not note.startswith(('with', 'flat', 'rising'))]
    rest = [note for note in notes if note not in comparisons]
    phrase = ' and '.join(comparisons) + (' than usual' if comparisons else '')
    return ', '.join(part for part in (phrase, *rest) if part)
