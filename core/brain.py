"""BITCH's mind: hears or reads something, thinks, acts, answers.

One thought at a time, in order:
  1. Build what the model sees: who BITCH is (persona.md), its tools, examples, recent conversation, and a note
     from its senses: the time, who is in view, its mood, what's coming up, and the memories that matter now.
  2. The fast brain answers in JSON: respond?, feeling, actions, move, say.
  3. Actions run (tools.py). If any fetched information, or one couldn't be done, the brain gets the results
     and answers again, so it never claims something that didn't happen.
  4. It speaks on the right device, the robot moves, and performances (dances, invented moves) play last.
"""
import datetime as dt, json, queue, random, re, threading, time
from . import config, llm, memory, moves, timeparse, tools, voice
from .hub import hub

FEELINGS = list(moves.FEELINGS)
GESTURES = ['none'] + list(moves.GESTURES)
NAME = re.compile(r"\bb[.\-* ]?[i!*1]?[.\-* ]?[t*]?[.\-* ]?[c*][.\-* ]?h\b|\bb\*{2,}", re.I)
INTRO = re.compile(r"\b(?:my name is|my name's|call me|i am called|this is)\s+([A-Za-z][A-Za-z\-']{1,20})", re.I)
YES = re.compile(r"^\W*(yes|yeah|yep|yup|ya|yah|that'?s me|it'?s me|i am|correct|of course|sure|uh[- ]?huh|mhm|indeed|you got it)\b", re.I)
CAM_OFF = re.compile(r"\b(camera off|turn off (the|your) camera|stop (watching|looking)|close your eyes|privacy)\b", re.I)
CAM_ON = re.compile(r"\b(camera on|turn on (the|your) camera|open your eyes|watch again|you can look)\b", re.I)
MUTE = re.compile(r"\b(mute|be quiet|shut up|stop talking|quiet mode|hush)\b", re.I)
UNMUTE = re.compile(r"\b(unmute|you can talk|talk again|speak again)\b", re.I)
GREETING = re.compile(r"^\W*((hey|hi|hello|yo|sup|hiya|howdy|good (morning|afternoon|evening))\W*(there|bitch|buddy|bud|you)?\W*)+$", re.I)
WAKE = re.compile(r'\b(wake up|good morning|rise and shine|morning)\b', re.I)
NOT_NAMES = {'not', 'just', 'here', 'back', 'home', 'so', 'very', 'really', 'tired', 'fine', 'good', 'ok', 'okay', 'sorry', 'going', 'the'}


CLAIMS = {'set_timer': r'timer|minutes? (?:is|are) (?:set|running)|counting', 'remind': r'remind', 'remember': r'remember|saved|noted|memory',
          'forget': r'forgot|forget|erased', 'teach': r'routine|learned|every', 'list': r'list'}


def named(t): return bool(NAME.search(t or ''))


def schema():
    return {'type': 'object', 'properties': {
        'respond': {'type': 'boolean'},
        'feeling': {'type': 'string', 'enum': FEELINGS},
        'actions': {'type': 'array', 'maxItems': 4, 'items': {'type': 'object', 'properties': {
            'tool': {'type': 'string', 'enum': list(tools.TOOLS)}, 'args': {'type': 'object'}}, 'required': ['tool', 'args']}},
        'move': {'type': 'string', 'enum': GESTURES},
        'say': {'type': 'string'}}, 'required': ['respond', 'feeling', 'actions', 'move', 'say']}


def J(feeling, say, move='none', actions=None, respond=True):
    return json.dumps({'respond': respond, 'feeling': feeling, 'actions': actions or [], 'move': move, 'say': say})


def A(tool, **args): return {'tool': tool, 'args': args}


def examples(o):
    return [
        ('set a 5 minute timer for the tea', J('happy', 'Five minutes for the tea, starting now!', 'nod', [A('set_timer', minutes=5, label='tea')])),
        ("what's the capital of Australia?", J('proud', 'Canberra!', 'nod')),
        ('how are you?', J('happy', "Pretty great! My servos are warm and my googly eyes are freshly googled. How about you?", 'wiggle')),
        ('why is the sky blue?', J('curious', "Sunlight bounces off the air, and blue light scatters the most, so blue comes at you from every direction.", 'none')),
        ('my favourite colour is green', J('happy', 'Green! Saved, right next to the googly-eye supplies.', 'wiggle', [A('remember', fact=f"{o}'s favourite colour is green")])),
        ('do I need a jacket today?', J('curious', 'Let me peek at the sky for you.', 'look_around', [A('weather', day='today')])),
        ('remind me to call mum at 7', J('happy', "I'll remind you to call your mum at seven.", 'nod', [A('remind', when='at 7', text='call mum')])),
        ('add eggs and milk to the shopping list', J('happy', 'Eggs and milk, on the list!', 'nod', [A('list', action='add', list='shopping', items=['eggs', 'milk'])])),
        ('do a backflip', J('excited', "A backflip? I've never tried one. Hold my popsicle sticks!", 'none', [A('perform', what='a backflip')])),
        ('dance for me', J('excited', 'Oh, you want the moves? Here they come!', 'none', [A('perform', what='dance')])),
        ('what am I holding?', J('curious', 'Ooh, show me! Let me look.', 'lean_in', [A('look', question='What is the person holding?')])),
        ('I failed my exam', J('sad', "Oh no, I'm sorry. That stings, but one exam doesn't define you. Want to talk about it?", 'lean_in')),
        ('every morning at 8, tell me the weather and my reminders', J('excited', "A morning routine! I'll do that every day at eight.", 'nod',
                                                                        [A('teach', name='morning update', steps='Check the weather for today and list my reminders, then tell me both.', when='every day at 8am')])),
        ("I'm bored", J('curious', 'Ooh, want me to invent a dance, or shall we plan something fun for later?', 'look_around')),
        ('yeah no, I told you, I will be there in ten. Bye.', J('neutral', '', 'none', respond=False)),
        ('good night BITCH', J('sleepy', f'Good night, {o}. Sweet dreams!', 'none', [A('perform', what='sleep')])),
    ]


RULES = """
How you reply: always JSON.
- "respond": false only when the words were clearly not meant for you (people talking to each other, a phone call, TV, song lyrics, a fragment). Typed messages and anything with your name are always for you.
- "feeling": how you feel right now.
- "actions": the tools to use now, as {{"tool": name, "args": {{...}}}}. Use [] when none are needed; most chatting needs none.
- "move": a small gesture that goes with your words, or "none".
- "say": what you say out loud.
Your tools:
{tools}
Info tools (weather, look_up, look, list read, reminders list, calculate) send their answer back to you; say a short "let me check" and you will then answer with the result.
For brand-new moves, use perform with a description: you can invent any move. Dances and songs are your big shows.
The note in [square brackets] after a message comes from your own senses and memory, not from the person.
Examples (message -> your JSON):
{examples}"""


class Brain:
    def __init__(s):
        s.q = queue.Queue()
        s.pending_identity = None       # {'name','until'} after asking "is that you, Brandon?"
        s.later = []                    # performances to play after speaking
        s._sys = None; s._sys_key = None
        s.last_spoke = 0.0               # when BITCH last finished saying something (opens a short follow-up window)

    def start(s):
        hub.on('heard', lambda text, device, mode: s.q.put({'kind': 'user', 'text': text, 'device': device, 'mode': mode, 'speak': True}))
        threading.Thread(target=s._worker, daemon=True).start()

    # ----- ways in -----
    def typed(s, text, device, speak=True):
        s.q.put({'kind': 'user', 'text': text, 'device': device, 'mode': 'typed', 'speak': speak})

    def event(s, prompt, why='', origin=None, speak=True, away='notify', mode='event', record=True):
        """Something happened (an arrival, a reminder, a routine). away: what to do if nobody is at the desk: 'notify' or 'skip'."""
        s.q.put({'kind': 'event', 'text': prompt, 'device': origin, 'mode': mode, 'speak': speak, 'why': why, 'away': away, 'record': record})

    def _worker(s):
        while True:
            it = s.q.get()
            try: s.handle(it)
            except Exception as e:
                import traceback; traceback.print_exc(); hub.note(f'brain problem: {e}')
            finally:
                if hub.status in ('thinking', 'understanding', 'hearing'): hub.set_status('asleep' if hub.asleep else 'listening')

    # ----- what the model sees -----
    def system(s):
        o = config.get('owner'); key = (o, tuple(tools.TOOLS))
        if s._sys_key != key:
            persona = open(config.ROOT + '/core/persona.md').read().replace('{owner}', o)
            ex = '\n'.join(f'"{m}" -> {j}' for m, j in examples(o))
            s._sys = persona + RULES.format(tools=tools.describe(), examples=ex); s._sys_key = key
        return s._sys

    def senses(s, it):
        now = dt.datetime.now(); o = config.get('owner'); bits = [f'now: {now:%A %-d %B %Y, %-I:%M %p}']
        d = hub.get(it.get('device') or '')
        if it['kind'] == 'user':
            how = {'typed': 'typed on', 'ptt': 'said into', 'ambient': 'heard by'}.get(it['mode'], 'from')
            bits.append(f'{how} {d.name if d else "the desk"}' + (f' ({o} may be away from the desk)' if d and d.place == 'mobile' else ''))
        cam = hub.get('mac-camera')
        if cam and cam.active('eyes'):
            ps = sorted(hub.people, key=lambda p: p['d'])
            bits.append('you see: ' + ('; '.join(f"{p['name'] or 'someone you do not know yet'} ({p['d'] / 100:.1f} m away, "
                                                 f"{'looking at you' if p['looking'] else 'looking away'})" for p in ps) if ps else 'nobody at the desk'))
        if hub.life: bits.append(f'your mood: {hub.life.mood.describe()}')
        up = [t for t in memory.tasks() if t['kind'] in ('timer', 'reminder')][:3]
        if up: bits.append('coming up: ' + '; '.join(f'{t["kind"]} "{t["text"]}" {timeparse.nice(dt.datetime.fromtimestamp(t["due"]))}' for t in up))
        if not config.get('location'): bits.append(f'you do not know which city {o} lives in yet')
        mem = {m['id']: m['text'] for m in memory.core_memories(5)}
        if it.get('text'):
            for m in memory.recall(it['text'], k=6): mem.setdefault(m['id'], m['text'])
        if mem: bits.append('you remember: ' + '; '.join(mem.values()))
        if it.get('extra'): bits.append(it['extra'])
        if s.pending_identity and time.time() < s.pending_identity['until']:
            bits.append(f"you asked whether this is {s.pending_identity['name']} and are waiting for a yes; don't ask again, just reply to what they said")
        return '[' + ' | '.join(bits) + ']'

    def history(s, n=12):
        out = []
        for m in memory.recent_messages(40, since=time.time() - 3 * 3600):
            if m['role'] == 'you': out.append({'role': 'user', 'content': m['text']})
            elif m['role'] == 'bitch': out.append({'role': 'assistant', 'content': J(m['emotion'] or 'neutral', m['text'], m['move'] or 'none')})
        return out[-n:]

    def ask(s, msgs):
        for attempt in range(2):
            try:
                out = llm.chat(msgs, schema(), temperature=0.55, timeout=60, max_tokens=500)
                if isinstance(out, dict) and 'say' in out: return out
            except Exception as e:
                err = e
        hub.note(f'brain problem: is Ollama open? ({err if "err" in dir() else "bad reply"})')
        return {'respond': True, 'feeling': 'confused', 'actions': [], 'move': 'none', 'say': 'Beep? My brain hiccuped. Is Ollama running on the Mac?'}

    # ----- one thought -----
    def handle(s, it):
        text = (it.get('text') or '').strip(); dev = it.get('device'); mode = it['mode']; o = config.get('owner')
        s.later = []
        if it['kind'] == 'say':                     # something to say as is (a timer ringing, an idea it had)
            if it.get('perform'): s.perform(it['perform'], {'device': dev})
            s.express(text, it.get('feeling') or 'happy', it.get('move') or 'none', it)
            for fn in s.later: fn()
            return
        if it['kind'] == 'user':
            hub.last_talk = time.time()
            hub.chat('you', text, dev, mode=mode)
            pend = s.pending_identity if s.pending_identity and time.time() < s.pending_identity['until'] else None
            if pend and YES.search(text):
                name = pend['name']; s.pending_identity = None
                if hub.sight: hub.sight.learn(name)
                if pend.get('reveal') and hub.life and not memory.kv_get('revealed'):
                    hub.life.reveal(origin=dev if hub.place_of(dev) == 'mobile' else None, inline=True); return
                it['extra'] = f'They just told you they are {name}. You are learning their face right now, so you will recognise them from now on.'
            m = INTRO.search(text)
            if m and m.group(1).lower() not in NOT_NAMES and hub.sight:
                who = m.group(1).capitalize(); tp = next((p for p in hub.people if p.get('target')), None)
                if tp and (not tp['name'] or tp['name'].lower() == who.lower()): hub.sight.learn(who)
            for rx, on, line in ((CAM_OFF, False, "Eyes closed. The camera is off until you say camera on."), (CAM_ON, True, 'Eyes open! I can see again.')):
                if rx.search(text) and (mode != 'ambient' or named(text)):
                    config.set('camera_on', on); hub.ui({'type': 'settings', 'settings': config.all()})
                    s.express(line, 'happy' if on else 'neutral', 'nod', it); return
            if UNMUTE.search(text) and (mode != 'ambient' or named(text)):
                config.set('muted', False); hub.ui({'type': 'settings', 'settings': config.all()})
                s.express('Yay, I can talk again!', 'happy', 'wiggle', it); return
            if MUTE.search(text) and (mode != 'ambient' or named(text)):
                s.express("Okay, I'll be quiet. Say unmute when you want my voice back.", 'sad', 'nod', it)
                config.set('muted', True); hub.ui({'type': 'settings', 'settings': config.all()}); return
            if mode == 'ambient' and not s.engaged(text, pend):        # overheard: people talking to each other, TV, a call
                hub.chat('note', '(heard, but it was not for me)'); return
            if hub.asleep:
                if mode in ('typed', 'ptt') or named(text) or WAKE.search(text): s.wake()
                else: hub.chat('note', '(asleep: say "Hey BITCH" or "wake up")'); return
            if hub.life and not memory.kv_get('revealed') and not pend and (mode != 'ambient' or named(text)):
                hub.life.reveal(origin=dev if hub.place_of(dev) == 'mobile' else None, inline=True)   # the first hello after the upgrade
                if GREETING.match(text): return
        hub.set_status('thinking')
        user = text if it['kind'] == 'user' else f'({text})'
        msgs = [{'role': 'system', 'content': s.system()}] + s.history() + [{'role': 'user', 'content': f'{user}\n{s.senses(it)}'}]
        out = s.ask(msgs)
        if it['kind'] == 'user' and mode == 'ambient' and out.get('respond') is False and not named(text) and not it.get('extra'):
            hub.chat('note', '(not meant for me)'); return
        it['_say'] = out.get('say', '')
        results, redo = s.act(out.get('actions') or [], it)
        if redo:
            msgs += [{'role': 'assistant', 'content': json.dumps(out)},
                     {'role': 'user', 'content': '[results of your actions: ' + ' | '.join(results) +
                      f']\nNow give your final reply to {o} using these results, in the same JSON. Use "actions": [].'}]
            out2 = s.ask(msgs)
            for k in ('say', 'feeling', 'move'):
                if out2.get(k): out[k] = out2[k]
            extra, _ = s.act([a for a in out2.get('actions') or [] if a.get('tool') == 'perform'], it)
        say = s.tidy(out.get('say', ''))
        feeling = out.get('feeling') if out.get('feeling') in FEELINGS else 'neutral'
        move = out.get('move') if out.get('move') in GESTURES else 'none'
        if not say and it['kind'] == 'user': say = 'Yes? I am here!' if named(text) else 'Hmm?'
        s.express(say, feeling, move, it, acts=[a.get('tool') for a in out.get('actions') or []])
        for fn in s.later:
            try: fn()
            except Exception as e: hub.note(f'performance failed: {e}')
        hub.emit('conversation', text=text, reply=say, feeling=feeling, kind=it['kind'], mode=mode)

    def engaged(s, text, pend=None):
        """Is overheard speech meant for BITCH? Only if it says the name, BITCH is mid-conversation (it spoke in the last
        25 seconds or just asked something), or the speaker is looking right at it."""
        if named(text): return True
        if pend and (YES.search(text) or re.search(r"\b(no|nope|nah|my name|i'?m|it'?s me|this is|call me)\b", text, re.I)): return True
        if re.match(r"^\W*([A-Z][a-z]+),", text) and not named(text.split(',')[0]): return False     # "Ryan, look at this"
        if time.time() - s.last_spoke < 25: return True
        watching = [p for p in hub.people if p.get('target')]
        return bool(watching and watching[0].get('looking') and len(hub.people) == 1)

    def act(s, actions, it):
        """Run the chosen tools. Returns (results for the brain, whether it should answer again)."""
        results, redo, seen = [], False, set()
        ctx = {'text': it.get('text', '') if it['kind'] == 'user' else '', 'device': it.get('device'), 'mode': it['mode']}
        for a in actions[:4]:
            if not isinstance(a, dict): continue
            name = a.get('tool'); args = a.get('args') if isinstance(a.get('args'), dict) else {}
            key = (name, json.dumps(args, sort_keys=True))
            if key in seen or name not in tools.TOOLS: continue
            seen.add(key)
            ok, res = tools.run(name, args, ctx)
            print(f'  action {name} {args} -> {ok} {res}', flush=True)
            if res is None:                              # skipped: nobody asked for it. Only answer again if the reply claims it happened.
                if re.search(CLAIMS.get(name, r'$^'), str(it.get('_say', '')), re.I):
                    results.append(f'{name}: not needed, so it was NOT done. Reply without mentioning it'); redo = True
                continue
            results.append(f'{name}: {res}')
            if not ok or tools.TOOLS[name].kind == 'info' or res.startswith(('I could not', 'I need', 'I do not', 'No ', 'There')): redo = True
            hub.ui({'type': 'action', 'tool': name, 'result': res, 'ok': ok})
        return results, redo

    def tidy(s, say):
        say = re.sub(r'\*[^*]{0,60}\*|\[[^\]]{0,80}\]|\([^)]{0,40}(?:beep|boop|whirr|giggle|laugh|wiggle|nod|wave)[^)]{0,40}\)', '', str(say))
        say = re.sub(r'[#`_~]|[\U0001F300-\U0001FAFF☀-➿]', '', say)
        return re.sub(r'\s+', ' ', say).strip()[:600]

    def express(s, say, feeling, move, it, acts=()):
        """Speak (or notify), log it, and let everything that shows BITCH react."""
        dev = it.get('device'); proactive = it['kind'] in ('event', 'say')
        at_desk = hub.someone_at_desk() is not False or (dev is not None and hub.get(dev) and hub.get(dev).place == 'mobile')
        if proactive and not dev and not at_desk:
            if it.get('away') == 'notify' and say and hub.life: hub.life.notify('BITCH', say)
            elif it.get('away') == 'skip': return
            if it.get('record', True): hub.chat('bitch', say, None, emotion=feeling, move=move, proactive=True, why=it.get('why', ''))
            return
        if say and it.get('record', True):
            hub.chat('bitch', say, dev, emotion=feeling, move=move, actions=list(acts), proactive=proactive, why=it.get('why', ''))
        speak = it.get('speak', True)
        voice.say(say, feeling, move, origin=dev, speak=speak)
        if say: s.last_spoke = time.time() - (15 if proactive else 0)     # after speaking up on its own, only a 10 s reply window

    # ----- the body -----
    def perform(s, what, ctx):
        o = config.get('owner')
        r = memory.skill(what) or next((k for k in memory.skills() if k['kind'] == 'routine' and k['name'] in what.lower()), None)
        if r and r['kind'] == 'routine':
            s.later.append(lambda: s.run_routine(r['name'], ctx.get('device'))); return f'Running the routine "{r["name"]}" next.'
        hit = moves.find(what)
        if hit and hit[0] == 'show':
            name = hit[1]
            if name == 'sleep': s.later.append(s.sleep); return 'You will go to sleep right after speaking.'
            if name == 'wake': s.wake(); return 'You are awake.'
            s.later.append(lambda: s.show(name)); return f'Doing your {name} show right after speaking.'
        if hit:
            s.later.append(lambda: s.play(hit[1], hit[2])); return f'Doing "{hit[1]}" right after speaking.'
        s.later.append(lambda: threading.Thread(target=s.invent_and_play, args=(what, ctx.get('device')), daemon=True).start())
        return f'You have never done "{what}", so you are inventing it now. It takes a few seconds.'

    def play(s, name, steps):
        for b in hub.bodies(): b.perform(steps, name)
        hub.ui({'type': 'perform', 'name': name, 'steps': steps})
        memory.skill_used(name)

    def show(s, name):
        for b in hub.bodies(): b.show(name)
        hub.ui({'type': 'show', 'name': name})

    def invent_and_play(s, what, origin=None):
        hub.ui({'type': 'inventing', 'what': what})
        for b in hub.bodies(): b.gesture('curious', 'look_around')
        got = moves.invent(what)
        if not got:
            voice.say("Hmm, my servos got tangled on that one. Can you describe it another way?", 'confused', 'shake', origin=origin); return
        name, steps, after = got
        hub.emit('skills_changed')
        s.play(name, steps)
        time.sleep(moves.length(steps) + 0.3)
        line = after or random.choice(['Ta-da!', 'How was that?', 'Nailed it. I think.'])
        hub.chat('bitch', line, origin, emotion='proud', move='none', learned=name)
        voice.say(line, 'proud', 'none', origin=origin)

    def run_routine(s, name, origin=None):
        r = memory.skill(name)
        if not r: return
        memory.skill_used(name)
        s.event(f'Do your routine "{name}" now. The steps: {r["body"]["steps"]} Use your tools for each step, then tell {config.get("owner")} the results briefly.',
                why=f'routine "{name}"', origin=origin, mode='routine')

    def sleep(s):
        hub.asleep = True; hub.set_status('asleep')
        s.show('sleep'); hub.emit('slept')

    def wake(s):
        if not hub.asleep: return
        hub.asleep = False; hub.set_status('listening')
        s.show('wake'); hub.emit('woke'); time.sleep(3)
