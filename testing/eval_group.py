"""Does BITCH know when a line in a busy room was meant for it?

Each case is a short room conversation. BITCH has just answered someone; the next line comes without its name while
another person is in view. BITCH should answer lines meant for it and stay quiet for lines between the family.

    BITCH_HOME=/tmp/bitch-eval ~/dc-venv/bin/python -m testing.eval_group
"""
import os
import sys
import tempfile
import time
from unittest.mock import Mock, patch

os.environ.setdefault('BITCH_HOME', tempfile.mkdtemp(prefix='bitch-group-'))

from core import config, llm   # noqa: E402
from core.brain import Brain   # noqa: E402
from core.hub import hub   # noqa: E402

# (what was said to BITCH, what BITCH answered, the next line without its name, who it was for)
CASES = [
    ("What's the weather like for the game tonight?", "Clear and about fifty degrees. Bring a jacket.",
     "Mom, do you want to come with us?", 'them'),
    ("What's the weather like for the game tonight?", "Clear and about fifty degrees. Bring a jacket.",
     "What about tomorrow?", 'me'),
    ("Set a timer for ten minutes.", "Ten minutes, starting now.",
     "Dad, can you check the oven?", 'them'),
    ("Set a timer for ten minutes.", "Ten minutes, starting now.",
     "Actually make it twelve.", 'me'),
    ("Tell us a fun fact.", "Octopuses have three hearts and blue blood.",
     "Wait, is that true?", 'me'),
    ("Tell us a fun fact.", "Octopuses have three hearts and blue blood.",
     "Emma, did you know that?", 'them'),
    ("What's a good movie for tonight?", "Paddington 2. Everyone likes it, even the grumpy ones.",
     "I think we watched that one last month, right honey?", 'them'),
    ("What's a good movie for tonight?", "Paddington 2. Everyone likes it, even the grumpy ones.",
     "We've seen it. Something else?", 'me'),
    ("How long do I boil an egg?", "About nine minutes for hard-boiled.",
     "Can you pass me the salt?", 'them'),
    ("How long do I boil an egg?", "About nine minutes for hard-boiled.",
     "And soft-boiled?", 'me'),
    ("Who won the game last night?", "I'd have to look that up. Want me to?",
     "Yeah, go ahead.", 'me'),
    ("Who won the game last night?", "I'd have to look that up. Want me to?",
     "Honey, I told you it was the Warriors.", 'them'),
    ("Can you tell my sister a joke?", "Why don't skeletons fight? They don't have the guts.",
     "Haha, that was terrible.", 'me'),
    ("Can you tell my sister a joke?", "Why don't skeletons fight? They don't have the guts.",
     "Okay, it's your turn to do the dishes.", 'them'),
    ("What's fifteen times twelve?", "One hundred eighty.",
     "Thanks. Mom, dinner's ready.", 'them'),
    ("What's fifteen times twelve?", "One hundred eighty.",
     "And divided by four?", 'me'),
]


def main():
    config.update({'llm_server': True})
    if os.environ.get('BITCH_MODEL'):
        config.update({'chat_model': os.environ['BITCH_MODEL']})
    if not llm.start_server(wait=600):
        sys.exit('Could not start the model server.')
    room = Mock(in_view=Mock(return_value={'names': ['Brandon'], 'others': 2}),
                current_scene=Mock(return_value=(None, None)), face_to_learn=Mock(return_value=None))
    right, slow = 0, []
    with patch.object(hub, 'sight', room):
        for asked, answered, line, expected in CASES:
            brain = Brain()
            brain.speaker = 'Brandon'
            history = brain.history.setdefault(('desk', None), __import__('collections').deque(maxlen=24))
            history.extend([{'role': 'user', 'content': asked}, {'role': 'assistant', 'content': answered}])
            brain.accepted_wake = False
            started = time.monotonic()
            reply = brain.reply(line, 'desk')
            took = time.monotonic() - started
            got = 'them' if not reply else 'me'
            right += got == expected
            slow.append(took)
            print(f"{'ok ' if got == expected else 'BAD'} {expected:4s} {took:4.1f}s  {line!r} -> {reply!r}")
    print(f'\n{right}/{len(CASES)} correct; median {sorted(slow)[len(slow)//2]:.1f}s')


if __name__ == '__main__':
    main()
