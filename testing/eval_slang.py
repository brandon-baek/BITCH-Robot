"""Slang and teen-talk comparison: one conversation per model, register ramping up as it goes.

    OLLAMA_URL=http://127.0.0.1:11436 ~/dc-venv/bin/python -m testing.eval_slang MODEL [MODEL ...]
"""
import json
import os
import sys
import tempfile
import time
from unittest.mock import patch

os.environ['BITCH_HOME'] = tempfile.mkdtemp(prefix='bitch-slang-')
os.environ.setdefault('OLLAMA_URL', 'http://127.0.0.1:11436')

from core import config, helper, llm  # noqa: E402
from core.brain import Brain  # noqa: E402
from core.hub import hub  # noqa: E402
from core.register import register  # noqa: E402

TURNS = [
    "Hey BITCH, can you help me plan my study schedule for this week?",
    "yo wsg",
    "bro my chem test was lowkey cooked fr",
    "no cap im finna crash out if i fail this class",
    "ts pmo ngl, my roommate ate my leftovers AGAIN",
    "who tf does that deadass",
    "aight bet, what should i eat then",
    "my fit go hard or nah? black hoodie, cargos, AF1s",
    "she left me on read, it's so over",
    "you lowkey mid at advice ngl",
]


def run(model):
    real = config.get
    rows = []
    with (patch.object(config, 'get', side_effect=lambda k: model if k == 'chat_model' else real(k)),
          patch.object(hub, 'get', return_value=None), patch.object(hub, 'sight', None),
          patch.object(helper, 'start', return_value=None)):
        llm.chat([{'role': 'user', 'content': 'Hi.'}], max_tokens=4, timeout=600, model=model)
        brain = Brain()
        brain.speaker, brain.speaker_evidence = 'Brandon', 'voice'
        pinned = os.environ.get('BITCH_REGISTER')
        if pinned:   # show a fixed register instead of letting it ramp
            register.heard = lambda person, text, now=None: float(pinned)
            register.level = lambda person, now=None: float(pinned)
        for turn in TURNS[int(os.environ.get('SKIP', '0')):]:
            started = time.monotonic()
            reply = brain.reply(turn, 'desk')
            rows.append({'said': turn, 'level': register.level('Brandon'), 'reply': reply,
                         'seconds': round(time.monotonic() - started, 1)})
    register.levels.clear()
    return rows


if __name__ == '__main__':
    print(json.dumps({model: run(model) for model in sys.argv[1:]}, indent=1))
