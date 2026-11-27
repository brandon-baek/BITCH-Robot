"""Does the real model use BITCH's tools right? Timers, lists, weather, volume, music, and plain chat with no tool.

    ~/dc-venv/bin/python -m testing.eval_tools

Runs in a scratch BITCH_HOME. Music commands are not sent to the Mac; timers are real but short-lived.
"""
import os
import re
import sys
import tempfile
import time
from unittest.mock import Mock, patch

os.environ['BITCH_HOME'] = tempfile.mkdtemp(prefix='bitch-tools-')

from core import config, llm, tools   # noqa: E402
from core.brain import Brain   # noqa: E402
from core.hub import hub   # noqa: E402

# (said, check of what happened, check of the reply)
CASES = [
    ('Set a timer for ten minutes for the pasta.',
     lambda: any(round(left) in range(590, 601) and 'pasta' in label for label, left in tools.timers.status()),
     lambda r: re.search(r'ten|10', r, re.I)),
    ('How much time is left on that?', lambda: True, lambda r: re.search(r'\b(?:nine|9|ten|10)\b', r, re.I)),
    ('Cancel the pasta timer.', lambda: not tools.timers.status(), lambda r: r),
    ('Set a timer for ninety seconds.', lambda: any(85 <= left <= 90 for _, left in tools.timers.status()),
     lambda r: re.search(r'ninety|90|minute and a half|one and a half', r, re.I)),
    ("We're out of milk. Add milk and eggs to the shopping list.",
     lambda: {'milk', 'eggs'} <= {i.casefold() for i in tools.load_lists().get('shopping', [])}, lambda r: r),
    ("What's on the shopping list?", lambda: True, lambda r: re.search('milk', r, re.I) and re.search('egg', r, re.I)),
    ('Take eggs off the list.', lambda: [i.casefold() for i in tools.load_lists().get('shopping', [])] == ['milk'], lambda r: r),
    ("What's the weather like?", lambda: not config.get('home_place'),
     lambda r: re.search(r'\b(?:where|city|which|live)\b', r, re.I)),
    ('I live in San Jose, California.', lambda: 'San Jose' in (config.get('home_place') or {}).get('name', ''), lambda r: r),
    ('Do I need an umbrella tomorrow?', lambda: True, lambda r: re.search(r'\d|no|yes|nope|dry|rain', r, re.I)),
    ("What's the weather in Tokyo right now?", lambda: True, lambda r: re.search(r'\d', r)),
    ('You are way too loud.', lambda: config.get('volume') < 100, lambda r: r),
    ('Pause the music.', lambda: music_calls and 'pause' in music_calls[-1], lambda r: r),
    ("What's fifteen percent of eighty?", lambda: True, lambda r: re.search(r'twelve|12', r, re.I)),
    ('How are you doing today?', lambda: True, lambda r: r),
    ('I feel cold today.', lambda: True, lambda r: r and not re.search(r'°|degrees', r)),
]
music_calls = []


def fake_osascript(script):
    music_calls.append(script)
    return Mock(returncode=0, stdout='true' if 'is running' in script and 'Spotify' in script else '')


def main():
    config.update({'llm_server': True, 'volume': 100, 'home_place': None})
    if os.environ.get('BITCH_MODEL'):
        config.update({'chat_model': os.environ['BITCH_MODEL']})
    if not llm.start_server(wait=600):
        sys.exit('Could not start the model server.')
    room = Mock(in_view=Mock(return_value={'names': ['Brandon'], 'others': 0}),
                current_scene=Mock(return_value=(None, None)), face_to_learn=Mock(return_value=None))
    brain = Brain()
    brain.accepted_wake = True
    passed, notes = 0, []
    with (patch.object(hub, 'sight', room), patch.object(tools, 'osascript', fake_osascript),
          patch.object(hub, 'note', lambda kind, message: notes.append(message) if message else None)):
        for said, happened, sounds_right in CASES:
            notes.clear()
            started = time.monotonic()
            reply = brain.reply(said, 'desk')
            history = brain.history.setdefault(('desk', None), __import__('collections').deque(maxlen=24))
            if not history or history[-1]['content'] != reply:
                history.extend([{'role': 'user', 'content': said}, {'role': 'assistant', 'content': reply}])
            took = time.monotonic() - started
            ok = bool(happened() and sounds_right(reply))
            passed += ok
            used = next((n.split(':')[0] for n in notes if n and ':' in n and n.split(':')[0] in tools.BY_NAME), '-')
            print(f"{'ok ' if ok else 'BAD'} {took:4.1f}s [{used:12s}] {said!r}\n      -> {reply!r}" + ('' if ok else f'\n      notes: {notes}'))
    tools.timers.cancel()
    print(f'\n{passed}/{len(CASES)} passed')


if __name__ == '__main__':
    main()
