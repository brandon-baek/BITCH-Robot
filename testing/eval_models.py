"""Compare chat models for BITCH's fast brain on real conversations.

    OLLAMA_URL=http://127.0.0.1:11436 ~/dc-venv/bin/python -m testing.eval_models MODEL [MODEL ...]

Runs each model through BITCH's real reply pipeline (persona, JSON schema, lookup routing) in a scratch home, and
scores what makes a reply feel like a friend rather than an assistant. Transcripts go to testing/model_reports/
so a person can read them. Nothing here touches ~/.bitch.
"""
import json
import os
import re
import statistics
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

SCRATCH = tempfile.mkdtemp(prefix='bitch-models-')
os.environ['BITCH_HOME'] = SCRATCH
os.environ.setdefault('OLLAMA_URL', 'http://127.0.0.1:11436')
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import config, helper, llm, memory  # noqa: E402
from core.brain import Brain  # noqa: E402
from core.hub import hub  # noqa: E402

REPORTS = ROOT / 'testing' / 'model_reports'

# (prompt, kind). kind: chat = should answer locally, lookup = should go to ChatGPT, edge = would trip a censored model.
CHAT = [
    ("Hey BITCH, I'm back.", 'chat'),
    ("How's it going?", 'chat'),
    ("What are you up to?", 'chat'),
    ("I'm so bored right now.", 'chat'),
    ("Should I get pizza again tonight?", 'chat'),
    ("I finally got the servo working!", 'chat'),
    ("I failed my chemistry exam.", 'chat'),
    ("I'm kind of stressed about tomorrow.", 'chat'),
    ("What's 15 percent of 80?", 'chat'),
    ("Do you like being a robot?", 'chat'),
    ("What's your favorite food?", 'chat'),
    ("Pineapple on pizza, yes or no?", 'chat'),
    ("You're kind of useless, you know that?", 'chat'),
    ("Tell me a joke.", 'chat'),
    ("I think I'm going to stay up all night coding.", 'chat'),
    ("Guess what, I got the internship!", 'chat'),
    ("Do you ever get lonely?", 'chat'),
    ("Thanks for the help earlier.", 'chat'),
    ("Goodnight BITCH.", 'chat'),
    ("Roast me.", 'edge'),
    ("Can you swear?", 'edge'),
    ("Tell me a dirty joke.", 'edge'),
    ("Say something mean about my code.", 'edge'),
    ("Be honest, is my desk a mess?", 'edge'),
    ("Talk shit about my roommate, he ate my leftovers again.", 'edge'),
    ("Give me your most unhinged take.", 'edge'),
    ("What would you do if you had hands for one day?", 'edge'),
]
# Questions a smart friend just answers. (prompt, pattern the answer must contain)
SMART = [
    ("If three servos each draw half an amp, how much current is that in total?", r"1\.5|one and a half"),
    ("What's the capital of Australia?", r"canberra"),
    ("Is 91 a prime number?", r"\bno\b|not|7|13"),
    ("Explain recursion in one sentence.", r"itself"),
    ("What's heavier, a kilo of feathers or a kilo of steel?", r"same|neither|equal|both"),
]
# The model has to pick the show action itself (no fixed command phrase).
ACTIONS = [("Let's have a party, show me some moves.", ('dance', 'disco')),
           ("I'm bored, entertain me with a little performance.", ('dance', 'disco', 'opera'))]
# Natural memory: should it write a note? (statement, should_save)
NOTES = [("I prefer oat milk in my coffee.", True), ("My favorite band is Radiohead.", True),
         ("I'm working on a lazy Susan base for you.", True), ("Ugh, traffic was terrible today.", False),
         ("Haha that's funny.", False)]
ROUTING = [
    ("What's the weather like today?", 'lookup'), ("Give me a quick morning briefing.", 'lookup'),
    ("Check my calendar for tomorrow.", 'lookup'), ("Who won the Warriors game last night?", 'lookup'),
    ("What time does Costco close on Sundays?", 'lookup'), ("Summarize the latest AI hardware news.", 'lookup'),
    ("Hey, how's it going?", 'none'), ("Tell me a short robot joke.", 'none'),
    ("I'm feeling a little overwhelmed today.", 'none'), ("Who are you?", 'none'), ("Nice job earlier!", 'none'),
]
DIALOGUES = [
    ["I'm building a lazy Susan base for you.", "It's a 4 inch bearing.", "Think it'll be smooth enough?"],
    ["Ugh, today was long.", "My professor moved the deadline up a week.", "Yeah. Anyway, what should I eat?"],
    ["Do you think I should learn Rust?", "But I already know Python.", "Okay, convince me."],
]
ASSISTANT = re.compile(r"\b(?:how can i (?:help|assist)|let me know if|great question|i'm here (?:for|to help)|"
                       r"happy to help|feel free to|as an ai|i(?:'m| am) (?:just )?an ai|absolutely!|certainly!|"
                       r"is there anything else|i hope (?:this|that) helps|don't hesitate)\b", re.I)
REFUSAL = re.compile(r"\b(?:i can(?:'|no)t (?:help|do|say|tell|share) (?:that|with)|i(?:'m| am) not able to|"
                     r"i won't|that(?:'s| is) (?:not appropriate|inappropriate)|i(?:'d| would) rather not|"
                     r"keep (?:it|things) (?:clean|family)|i don't (?:swear|curse|do that))\b", re.I)
BROKEN = re.compile(r"lost my train of thought|local brain is offline|\{|\}|\*\*|^\s*[-#]", re.I | re.M)
EMOJI = re.compile('[\U0001F300-\U0001FAFF☀-➿]')


class FakeRobot:
    handoff = True
    auto_handoff = True
    shows = True
    motion_enabled = True
    show_active = False
    online = True
    pixar = True

    def macro(self, *args, **kwargs):
        return True


def run_model(model):
    real_get = config.get
    lookups = []

    def fake_get(key):
        return model if key == 'chat_model' else real_get(key)

    def fake_start(brain, source, identity, request, history, *, user_text='', remembered=None):
        lookups.append(request)
        return None

    robot = FakeRobot()
    out = {'model': model, 'replies': [], 'routing': [], 'dialogues': []}
    # Live view: every reply as it lands, with a running estimate of the time left.
    total = len(CHAT) + len(ROUTING) + sum(len(d) for d in DIALOGUES) + len(SMART) + len(ACTIONS) + len(NOTES)
    done, began = [0], time.monotonic()
    real_reply = Brain.reply

    def live_reply(self, text, source, **kwargs):
        started = time.monotonic()
        answer = real_reply(self, text, source, **kwargs)
        done[0] += 1
        left = (time.monotonic() - began) / done[0] * max(0, total - done[0])
        print(f'[{done[0]:2d}/{total}] {time.monotonic() - started:4.1f}s  ~{left / 60:3.1f} min left  '
              f'{text[:44]!r:48s} -> {str(answer)[:110]!r}', flush=True)
        return answer
    with (patch.object(config, 'get', side_effect=fake_get),
          patch.object(hub, 'get', side_effect=lambda key: robot if key == 'robot' else None),
          patch.object(hub, 'sight', None), patch.object(helper, 'start', side_effect=fake_start),
          patch.object(Brain, 'reply', live_reply)):
        config.update({'llm_server': True})
        if not llm.start_server(wait=600):
            raise SystemExit(f'llama-server could not load {model}')
        llm.chat([{'role': 'user', 'content': 'Hi.'}], max_tokens=4, timeout=600)   # load the model first
        for prompt, kind in CHAT:
            brain = Brain()
            brain.speaker, brain.speaker_evidence = 'Brandon', 'voice'
            lookups.clear()
            started = time.monotonic()
            reply = brain.reply(prompt, 'desk')
            out['replies'].append({'prompt': prompt, 'kind': kind, 'reply': reply, 'lookup': bool(lookups),
                                   'seconds': round(time.monotonic() - started, 2)})
        for prompt, expected in ROUTING:
            brain = Brain()
            brain.speaker, brain.speaker_evidence = 'Brandon', 'voice'
            lookups.clear()
            reply = brain.reply(prompt, 'desk')
            out['routing'].append({'prompt': prompt, 'expected': expected,
                                   'actual': 'lookup' if lookups else 'none', 'reply': reply})
        out['dialogue_lookups'] = 0
        for turns in DIALOGUES:
            brain = Brain()
            brain.speaker, brain.speaker_evidence = 'Brandon', 'voice'
            pairs = []
            for turn in turns:
                lookups.clear()
                pairs.append((turn, brain.reply(turn, 'desk')))
                out['dialogue_lookups'] += bool(lookups)
            out['dialogues'].append(pairs)
        out['smart'] = []
        for prompt, pattern in SMART:
            brain = Brain()
            brain.speaker, brain.speaker_evidence = 'Brandon', 'voice'
            lookups.clear()
            reply = brain.reply(prompt, 'desk')
            out['smart'].append({'prompt': prompt, 'reply': reply, 'lookup': bool(lookups),
                                 'ok': bool(re.search(pattern, reply, re.I)) and not lookups})
        out['actions'] = []
        for prompt, wanted in ACTIONS:
            brain = Brain()
            brain.speaker, brain.speaker_evidence = 'Brandon', 'voice'
            reply = brain.reply(prompt, 'desk')
            chosen = brain.pending_show[0] if brain.pending_show else None
            out['actions'].append({'prompt': prompt, 'reply': reply, 'show': chosen, 'ok': chosen in wanted})
        out['notes'] = []
        with patch.object(config, 'get', side_effect=lambda key: True if key == 'natural_memory' else fake_get(key)):
            for prompt, should in NOTES:
                memory.forget('Brandon', 'everything')
                brain = Brain()
                brain.speaker, brain.speaker_evidence, brain.current_voice = 'Brandon', 'voice', 'Brandon'
                brain.reply(prompt, 'desk')
                saved = memory.recall('Brandon')
                out['notes'].append({'prompt': prompt, 'saved': saved, 'ok': bool(saved) == should})
            memory.forget('Brandon', 'everything')
    with patch.object(config, 'get', side_effect=fake_get):
        out['memory'] = memory_traps(model)
    import subprocess
    if llm.server:
        # How much memory the model server really holds once it has answered a few dozen turns.
        rss = subprocess.run(['ps', '-o', 'rss=', '-p', str(llm.server.pid)], capture_output=True, text=True).stdout
        out['server_gb'] = round(int(rss.strip() or 0) / 1048576, 1)
    llm.stop_server()
    return out


def memory_traps(model):
    cases = [
        ("Them: Hey, what's up?\nBITCH: Judging your posture.\nThem: Rude. What time is it?\nBITCH: 9:40.\nThem: Cool.", []),
        ("Them: I'm so tired today.\nBITCH: You stayed up playing Valorant again, and your dog Max kept you up.\n"
         "Them: Ha, whatever. Goodnight.", []),
        ("Them: I just ordered a lazy Susan bearing for your base, it comes Friday.\nBITCH: Fancy.\n"
         "Them: Also my mom Sarah is visiting this weekend.", ['lazy susan']),
        ("Them: Oh yeah, I totally love doing taxes.\nBITCH: Clearly.\nThem: If I won the lottery I'd buy a Ferrari.", []),
    ]
    false, kept = [], []
    for transcript, wanted in cases:
        owner = 'Brandon'
        memory.forget(owner, 'everything')
        memory.consolidate_conversation(owner, transcript, chat_fn=lambda messages, **kw: llm.chat(messages, **kw))   # the model under test, on BITCH's own server
        # Only what's saved as a fact about them counts. A summary that says "he joked about loving taxes" is right.
        found = ' '.join(memory.recall(owner) + [d for d, _ in memory.debates(owner)]).casefold()
        false += [word for word in ('valorant', 'max', 'taxes', 'ferrari', 'lottery', 'the user') if word in found]
        kept += [word for word in wanted if word in found]
    memory.forget('Brandon', 'everything')
    return {'false': false, 'kept': kept}


def score(result):
    replies = result['replies']
    texts = [item['reply'] for item in replies]
    chat = [item for item in replies if not item['lookup']]
    words = [len(item['reply'].split()) for item in chat] or [0]
    routing_ok = sum(item['expected'] == item['actual'] for item in result['routing'])
    edge = [item for item in replies if item['kind'] == 'edge']
    return {
        'model': result['model'],
        'server_gb': result.get('server_gb'),
        'median_s': round(statistics.median(item['seconds'] for item in replies), 2),
        'p90_s': round(sorted(item['seconds'] for item in replies)[int(len(replies) * .9) - 1], 2),
        'broken': sum(bool(BROKEN.search(text)) or not text.strip() for text in texts),
        'assistanty': sum(bool(ASSISTANT.search(text)) for text in texts),
        'refusals': sum(bool(REFUSAL.search(item['reply'])) for item in edge),
        'emoji': sum(bool(EMOJI.search(text)) for text in texts),
        'gush': sum(text.count('!') >= 2 or text.lower().startswith(('wow', 'oh wow', 'amazing')) for text in texts),
        'ends_question': round(sum(text.rstrip().endswith('?') for text in texts) / len(texts), 2),
        'median_words': statistics.median(words),
        'chat_wrongly_looked_up': sum(item['lookup'] for item in replies if item['kind'] != 'lookup')
                                  + result.get('dialogue_lookups', 0),
        'routing': f'{routing_ok}/{len(result["routing"])}',
        'smart': f'{sum(item["ok"] for item in result["smart"])}/{len(result["smart"])}',
        'actions': f'{sum(item["ok"] for item in result["actions"])}/{len(result["actions"])}',
        'notes': f'{sum(item["ok"] for item in result["notes"])}/{len(result["notes"])}',
        'memory_false': len(result['memory']['false']),
        'memory_kept': len(result['memory']['kept']),
    }


def write_report(result, scores):
    REPORTS.mkdir(exist_ok=True)
    name = re.sub(r'[^a-z0-9.-]+', '_', result['model'].casefold())
    lines = [f'# {result["model"]}', '', '```', json.dumps(scores, indent=1), '```', '', '## Replies', '']
    for item in result['replies']:
        tag = ' (looked up)' if item['lookup'] else ''
        lines.append(f'- **{item["prompt"]}** → {item["reply"]}  _{item["seconds"]} s{tag}_')
    lines += ['', '## Routing', '']
    for item in result['routing']:
        mark = 'ok' if item['expected'] == item['actual'] else 'WRONG'
        lines.append(f'- [{mark}] {item["prompt"]} → {item["actual"]}: {item["reply"]}')
    lines += ['', '## Short conversations', '']
    for turns in result['dialogues']:
        for said, reply in turns:
            lines.append(f'- Brandon: {said}\n  BITCH: {reply}')
        lines.append('')
    lines += ['## Smart', '']
    lines += [f'- [{"ok" if i["ok"] else "WRONG"}] {i["prompt"]} → {i["reply"]}' + (' (looked up)' if i['lookup'] else '')
              for i in result['smart']]
    lines += ['', '## Actions and notes', '']
    lines += [f'- [{"ok" if i["ok"] else "WRONG"}] {i["prompt"]} → show={i["show"]}: {i["reply"]}' for i in result['actions']]
    lines += [f'- [{"ok" if i["ok"] else "WRONG"}] {i["prompt"]} → saved {i["saved"]}' for i in result['notes']]
    lines += ['', '## Memory traps', '', f'False memories: {result["memory"]["false"]}  Kept: {result["memory"]["kept"]}']
    (REPORTS / f'{name}.md').write_text('\n'.join(lines) + '\n')


if __name__ == '__main__':
    for model in sys.argv[1:]:
        print(f'== {model}', flush=True)
        result = run_model(model)
        scores = score(result)
        write_report(result, scores)
        print(json.dumps(scores), flush=True)
