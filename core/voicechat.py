"""Optional local Nemotron VoiceChat file-mode adapter for addressed audio."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import wave

import numpy as np

from . import config, streaming, voice

REPLY = re.compile(r'^soniqo  (.+?)\nframes \d+', re.M | re.S)
MAX_INPUT_SAMPLES = 160000
LOCAL_AGENT = re.compile(
    r"\b(?:analy[sz]e|calculat\w*|compare|debug|explain|reason through|research|step.by.step|"
    r"teach me|translate|write|summari[sz]e|give (?:me )?(?:the )?steps|how (?:do|does|can|much|many)|"
    r"what(?:'s| is) (?:the )?(?:difference|best way)|why)\b", re.I)


def settings():
    bundle = Path(os.environ.get('BITCH_VOICECHAT_MODEL') or config.get('voicechat_model')
                  or config.HOME / 'voicechat-int5').expanduser()
    runtime = Path(config.HOME / 'voicechat-runtime' / 'speech')
    command = os.environ.get('BITCH_VOICECHAT_BIN') or config.get('voicechat_bin') or (
        str(runtime) if runtime.is_file() else shutil.which('speech'))
    ready = command and os.access(command, os.X_OK) and all(
        (bundle / piece / 'model.safetensors').is_file() for piece in ('encoder', 'llm', 'tts'))
    return (command, bundle) if ready else None


def respond(samples, prompt, stop):
    runtime = settings()
    if runtime is None or stop.is_set() or not 0 < len(samples) <= MAX_INPUT_SAMPLES:
        return None
    executable, model = runtime
    with tempfile.TemporaryDirectory(prefix='bitch-voicechat-') as folder:
        request, response = (Path(folder) / name for name in ('input.wav', 'output.wav'))
        with wave.open(str(request), 'wb') as wav:
            wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            wav.writeframes((np.clip(samples, -1., 1.) * 32767).astype('<i2').tobytes())
        with subprocess.Popen([executable, 'voice-chat', '--model', str(model), '--plain',
                               '--input', str(request), '--output', str(response), '--tail-seconds', '5',
                               '--speech-iterations', '4',
                               '--system-prompt', prompt],
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True, encoding='utf-8') as process:
            deadline = time.monotonic() + 45.
            while not stop.is_set() and time.monotonic() < deadline:
                try:
                    report, _ = process.communicate(timeout=.15)
                    break
                except subprocess.TimeoutExpired:
                    continue
            else:
                process.kill()
                process.communicate()
                return None
        # The plain renderer can print a preview before the final transcript.
        final = report.rfind('\nsoniqo  ')
        if process.returncode != 0 or final < 0 or not (match := REPLY.match(report[final+1:])):
            return None
        text = streaming.clean(match[1])
        if not text or len(text) > 560 or not response.is_file():
            return None
        import soundfile

        audio, rate = soundfile.read(response, dtype='float32')
        if audio.ndim != 1 or not len(audio) or not np.isfinite(audio).all():
            return None
        audible = np.flatnonzero(np.abs(audio) >= .006)
        if not len(audible) or audible[-1] - audible[0] < .12 * rate:
            return None
        audio = audio[max(0, audible[0] - rate//25):min(len(audio), audible[-1] + rate//14)]
        signal = np.interp(np.arange(round(len(audio)*voice.SR/rate)) * rate / voice.SR,
                           np.arange(len(audio)), audio)
        signal *= .88 / max(float(np.max(np.abs(signal))), .22)
        return text, voice.encode(signal)
