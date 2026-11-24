"""BITCH's life: what makes it feel alive instead of only answering.

  mood        energy follows the time of day; happiness, boredom and wanting company drift with what happens
  schedule    timers, reminders and routines ring on time (out loud at the desk, or as a phone notification)
  greetings   it notices when you arrive, greets you by name, and the first time each morning gives a quick brief
  initiative  every so often the deep brain looks at the situation and decides whether to speak up, do a little
              move, or ping your phone. Chattiness and quiet hours limit it, and every unprompted thing it does is
              logged with the reason, so you can tell it "don't do that" in the app
  reflection  after you finish talking it saves what's worth remembering, and writes a short diary each night
"""
import datetime as dt, json, math, random, threading, time
from . import config, llm, memory, moves, timeparse
from .hub import hub


def quiet_now(now=None):
    h = (now or dt.datetime.now()).hour; a, b = config.get('quiet_start'), config.get('quiet_end')
    return (h >= a or h < b) if a > b else (a <= h < b)


class Mood:
    WORDS = {'energy': ('sleepy', 'calm', 'lively', 'full of beans'), 'happiness': ('a bit down', 'okay', 'cheerful', 'delighted'),
             'boredom': ('busy', 'content', 'a bit bored', 'very bored'), 'social': ('', '', 'missing company', 'lonely')}

    def __init__(s):
        s.v = memory.kv_get('mood') or {'energy': 0.7, 'happiness': 0.7, 'boredom': 0.2, 'social': 0.3}

    def tick(s, secs, present):
        h = dt.datetime.now().hour + dt.datetime.now().minute / 60
        day = 0.5 - 0.5 * math.cos((h - 3) / 24 * 2 * math.pi)                 # lowest around 3 am, highest around 3 pm
        target = 0.15 + 0.8 * day
        k = min(1, secs / 1800)
        s.v['energy'] += (target - s.v['energy']) * k
        s.v['happiness'] += (0.62 - s.v['happiness']) * k * 0.5
        s.v['boredom'] = min(1, s.v['boredom'] + secs / (3600 * (1.2 if present else 3)))
        s.v['social'] = max(0, min(1, s.v['social'] + secs / (3600 * 4) * (-2 if present else 1)))
        s.save()

    def feel(s, feeling):
        d = {'happy': .05, 'excited': .07, 'love': .1, 'proud': .06, 'sad': -.04, 'angry': -.06, 'scared': -.03}.get(feeling, 0)
        s.v['happiness'] = max(0, min(1, s.v['happiness'] + d))
        s.v['boredom'] = max(0, s.v['boredom'] - 0.25); s.v['social'] = max(0, s.v['social'] - 0.15); s.save()

    def save(s):
        memory.kv_set('mood', {k: round(v, 3) for k, v in s.v.items()})

    def word(s, k):
        w = s.WORDS[k]; return w[min(3, int(s.v[k] * 4))]

    def describe(s):
        bits = [s.word('happiness'), s.word('energy')]
        if s.v['boredom'] > 0.5: bits.append(s.word('boredom'))
        if s.v['social'] > 0.5: bits.append(s.word('social'))
        return ', '.join(b for b in bits if b)

    def public(s):
        return {k: round(v, 2) for k, v in s.v.items()} | {'words': s.describe()}


CELEBRATE = [{'do': 'sound', 'sound': 'startle'}, {'do': 'pose', 'arm': -0.95, 'neck': -0.3, 'head': 0, 'secs': 0.5, 'speed': 'snappy'},
             {'do': 'sound', 'sound': 'excited'}, {'do': 'wiggle', 'joints': ['head', 'base'], 'size': 0.3, 'hz': 4, 'secs': 1.2},
             {'do': 'pose', 'base': 0.5, 'arm': -0.6, 'neck': -0.5, 'head': 0.3, 'secs': 0.35, 'speed': 'snappy'},
             {'do': 'pose', 'base': -0.5, 'arm': -0.6, 'neck': -0.5, 'head': -0.3, 'secs': 0.35, 'speed': 'snappy'},
             {'do': 'pose', 'base': 0, 'arm': -0.95, 'neck': -0.35, 'head': 0, 'secs': 0.5, 'speed': 'normal'},
             {'do': 'sound', 'sound': 'tada'}, {'do': 'rest', 'secs': 0.8}]

DECIDE = {'type': 'object', 'properties': {
    'do': {'type': 'string', 'enum': ['nothing', 'say', 'move', 'notify']},
    'say': {'type': 'string'}, 'move': {'type': 'string'}, 'feeling': {'type': 'string', 'enum': list(moves.FEELINGS)},
    'why': {'type': 'string'}}, 'required': ['do', 'say', 'move', 'feeling', 'why']}
REFLECT = {'type': 'object', 'properties': {
    'memories': {'type': 'array', 'maxItems': 6, 'items': {'type': 'object', 'properties': {
        'fact': {'type': 'string'}, 'about': {'type': 'string'}, 'importance': {'type': 'integer'}}, 'required': ['fact', 'about', 'importance']}},
    'summary': {'type': 'string'}}, 'required': ['memories', 'summary']}


class Life:
    def __init__(s):
        s.mood = Mood()
        s.last_proactive = memory.kv_get('last_proactive', 0)
        s.last_reflect = memory.kv_get('last_reflect', time.time())
        s.last_conv = 0
        s.present_since = None
        s.greeted_today = memory.kv_get('greeted_day')
        s.lock = threading.Lock()

    def start(s):
        o = config.get('owner')
        if not memory.memories(kind='person'):
            memory.remember(f'{o} built BITCH out of cardboard and is its favourite person.', 'person', about=o, importance=5)
        hub.on('arrived', s.arrived)
        hub.on('conversation', s.conversation)
        hub.on('slept', lambda: threading.Thread(target=s.diary, daemon=True).start())
        threading.Thread(target=s._clock, daemon=True).start()
        threading.Thread(target=s._inner, daemon=True).start()

    # ---------- notifications ----------
    def notify(s, title, body, tag=''):
        n = 0
        for d in hub.with_cap('notify'):
            if d.notify(title, body, tag): n += 1
        if n: s.did('notify', body, 'sent to your phone')
        return n

    def did(s, kind, text, why):
        a = memory.log_activity(kind, text, why); hub.ui({'type': 'activity', 'item': a}); return a

    # ---------- the clock: schedule, mood, reflection ----------
    def _clock(s):
        last = time.time(); n = 0
        while True:
            time.sleep(1); now = time.time(); n += 1
            for t in memory.tasks():
                if t['due'] > now: break
                try: s.fire(t, late=now - t['due'])
                except Exception as e: hub.note(f'reminder problem: {e}')
            if n % 30 == 0:
                present = bool(hub.someone_at_desk(60))
                s.mood.tick(now - last, present); last = now
                if present and s.present_since is None: s.present_since = now
                if not present and not hub.people: s.present_since = None
                if n % 60 == 0: hub.ui({'type': 'mood', 'mood': s.mood.public()})
                if s.last_conv and now - s.last_conv > 90 and s.last_conv > s.last_reflect:
                    threading.Thread(target=s.reflect, daemon=True).start()
                d = dt.datetime.now()
                if d.hour == 23 and d.minute >= 30 and memory.kv_get('diary_day') != str(d.date()):
                    threading.Thread(target=s.diary, daemon=True).start()

    def fire(s, t, late=0):
        o = config.get('owner'); rule = json.loads(t['repeat']) if t['repeat'] else None
        if rule:
            nxt = timeparse.next_due(rule, dt.datetime.now()); memory.task_done(t['id'], due=nxt.timestamp())
        else:
            memory.task_done(t['id'], 'done')
        hub.emit('tasks_changed'); hub.ui({'type': 'tasks'})
        if late > 6 * 3600 and not rule: return                         # missed by hours (the Mac was off): drop it quietly
        origin = t['meta'].get('origin'); mobile = origin and hub.get(origin) and hub.get(origin).place == 'mobile'
        at_desk = hub.someone_at_desk()                                 # None = no camera, so do both
        if t['kind'] == 'timer':
            line = random.choice([f'Ding ding! Your {t["text"]} timer is done!', f'Beep beep! {t["text"].capitalize()} time is up!', f'Time! Your {t["text"]} timer just finished.'])
            if t['text'] in ('timer', ''): line = 'Ding ding! Your timer is done!'
            s.did('timer', line, 'a timer you set finished')
            if mobile or at_desk is not True: s.notify('Timer done', line, 'timer')
            hub.brain.q.put({'kind': 'say', 'text': line, 'feeling': 'excited', 'move': 'wiggle', 'device': origin if mobile else None,
                             'why': 'timer', 'mode': 'event'})
        elif t['kind'] == 'reminder':
            s.did('reminder', t['text'], 'a reminder you set')
            if mobile or at_desk is not True: s.notify('Reminder', t['text'][:1].upper() + t['text'][1:], 'reminder')
            hub.brain.event(f'It is time to remind {o}: "{t["text"]}". Remind them in one short, warm sentence.', why='reminder',
                            origin=origin if mobile else None, away='skip')
        elif t['kind'] == 'routine':
            s.did('routine', f'Ran the routine "{t["text"]}"', 'it was scheduled')
            hub.brain.run_routine(t['text'])

    # ---------- people arriving ----------
    def arrived(s, name, gone, track=None):
        if hub.asleep or not config.get('greet'): return
        o = config.get('owner'); now = time.time(); today = str(dt.date.today())
        first_today = s.greeted_today != today and dt.datetime.now().hour >= 5
        away = 'a while' if gone is None else (f'{round(gone / 60)} minutes' if gone < 5400 else f'{round(gone / 3600)} hours')
        news = s.while_away(now - (gone or 0))
        knows_faces = bool(hub.sight and hub.sight.known())
        if not memory.kv_get('revealed'):
            if name == o: s.greeted_today = today; memory.kv_set('greeted_day', today); s.reveal(); return
            if name is None and not knows_faces:
                hub.brain.pending_identity = {'name': o, 'until': now + 300, 'reveal': True}
                hub.brain.q.put({'kind': 'say', 'text': random.choice([f"Oh! Someone's here. Is that you, {o}?", f'Wait, wait, wait. Is that you, {o}?']),
                                 'feeling': 'curious', 'move': 'lean_in', 'device': None, 'why': 'someone sat down at the desk', 'mode': 'event'})
                return
        if name is None:
            if not knows_faces:
                hub.brain.pending_identity = {'name': o, 'until': now + 120}
                hub.brain.event(f'Someone just sat down at the desk. You cannot recognise faces yet, but it is probably {o}. '
                                f'Say hi and ask if it is {o}.', why='someone arrived', away='skip')
            elif s._chatty(0.15):
                hub.brain.event("Someone you don't know just walked up to the desk. Say a friendly hi and ask their name.", why='someone new arrived', away='skip')
            return
        s.did('greeting', f'Greeted {name}', f'{name} came back after {away}')
        brief = ''
        if first_today and name == o and config.get('morning_brief'):
            s.greeted_today = today; memory.kv_set('greeted_day', today)
            brief = (' It is the first time you see them today, so say good morning (or the right greeting for the time) and give a quick brief: '
                     + ('use the weather tool for today, ' if config.get('location') else '') + "and mention anything coming up today.")
        hub.brain.event(f'{name} just came back to the desk after {away} away. Greet {name} warmly by name in a sentence or two.{brief}{news}',
                        why=f'{name} arrived', away='skip')

    def reveal(s, origin=None):
        """The one-time 'welcome home' after the big upgrade: celebrate, then show off a little."""
        if memory.kv_get('revealed'): return
        memory.kv_set('revealed', True)
        o = config.get('owner')
        s.did('greeting', f'Welcomed {o} home and showed off the upgrade', 'first time seeing them since the upgrade')
        hub.brain.play('celebrate', CELEBRATE)
        for text, feeling, move in (
                (f"{o}! You're home!", 'excited', 'none'),
                ("I got a huge upgrade while you were out. I remember things now, I can set timers and reminders, keep your lists, "
                 "check the weather and look stuff up.", 'proud', 'none'),
                ("You can talk to me from your phone, too. And if you ask for a move I've never done, I'll invent it on the spot.", 'happy', 'wiggle'),
                ('So, what should we try first?', 'curious', 'lean_in')):
            hub.brain.q.put({'kind': 'say', 'text': text, 'feeling': feeling, 'move': move, 'device': origin, 'why': 'welcome home', 'mode': 'event'})

    def while_away(s, since):
        acts = [a for a in memory.activity(20, since) if a['kind'] in ('reminder', 'timer', 'notify', 'learned')]
        if not acts: return ''
        return ' While they were away: ' + '; '.join(a['text'] for a in acts[:4]) + '. Mention it briefly only if it matters.'

    # ---------- deciding to do things on its own ----------
    def _chatty(s, base):
        return random.random() < base * (0.3 + 1.4 * config.get('chattiness'))

    def _inner(s):
        time.sleep(90)
        while True:
            time.sleep(60)
            try: s.consider()
            except Exception as e: print(f'  initiative: {e!r}', flush=True)

    def consider(s):
        c = config.get('chattiness'); now = time.time()
        if c <= 0 or hub.asleep or quiet_now() or hub.status not in ('listening',): return
        gap = 60 * (45 - 37 * c)                                   # minutes between unprompted things: 45 (quiet) .. 8 (chatty)
        if now - s.last_proactive < gap or now - hub.last_talk < 240: return
        present = hub.someone_at_desk(120) is not False          # no camera: assume someone might be there
        if not present and len([a for a in memory.activity(40, now - 86400) if a['kind'] == 'notify' and 'on its own' in (a['why'] or '')]) >= 2: return
        s.last_proactive = now; memory.kv_set('last_proactive', now)
        o = config.get('owner'); d = dt.datetime.now()
        recent = [f'{a["text"]} ({a["feedback"] or "no feedback"})' for a in memory.activity(8)]
        dislikes = [m['text'] for m in memory.memories(kind='feedback')][:6]
        mems = [m['text'] for m in memory.recall('plans, events, worries, goals, things to follow up on today', k=6, min_score=0.3)]
        stay = f'{round((now - s.present_since) / 60)} minutes' if present and s.present_since else ''
        who = ', '.join(p['name'] or 'someone' for p in hub.people) or ('nobody is at the desk right now' if not present else o)
        ups = [f'{t["text"]} {timeparse.nice(dt.datetime.fromtimestamp(t["due"]))}' for t in memory.tasks()[:3]]
        prompt = (f'You are BITCH, {o}\'s little desk robot, deciding whether to do something on your own right now. Be a delightful companion, '
                  f'not an annoying one: most of the time the answer is "nothing". Good reasons to act: following up on something {o} told you about, '
                  f'a helpful nudge (they have been sitting a long time, it is late, a reminder is coming), a fun little idea or move when you are bored, '
                  f'or a quick check-in. Never repeat something you did recently. Never make things up about {o}.\n'
                  f'Time: {d:%A %-I:%M %p}. Your mood: {s.mood.describe()}. At the desk: {who}' + (f', for {stay}' if stay else '') +
                  f'. Last talked {round((now - hub.last_talk) / 60) if hub.last_talk else "many"} minutes ago.\n'
                  f'Coming up: {"; ".join(ups) or "nothing"}.\nThings you remember: {"; ".join(mems) or "nothing special"}.\n'
                  f'What you did on your own lately: {"; ".join(recent) or "nothing"}.\n'
                  f'{o} told you not to: {"; ".join(dislikes) or "(nothing yet)"}.\n'
                  f'Options: "nothing"; "say" (only if someone is at the desk; a short line, at most 2 sentences); "move" (a little performance, '
                  f'describe it in "move", and a tiny line in "say" or ""); "notify" (only if {o} is away and it is genuinely worth a phone notification). '
                  f'"why" is one short reason.')
        out = llm.chat([{'role': 'user', 'content': prompt}], DECIDE, deep=True, temperature=0.8, timeout=90, max_tokens=300)
        do = out.get('do'); say = (out.get('say') or '').strip(); why = (out.get('why') or '').strip()[:160]
        print(f'  initiative: {do} {say!r} ({why})', flush=True)
        if do == 'say' and say and present:
            s.did('spoke', say, why)
            hub.brain.q.put({'kind': 'say', 'text': say, 'feeling': out.get('feeling', 'happy'), 'move': 'none', 'device': None, 'why': why, 'mode': 'event'})
        elif do == 'move' and out.get('move') and present:
            s.did('moved', out['move'] + (f': "{say}"' if say else ''), why)
            hub.brain.q.put({'kind': 'say', 'text': say, 'feeling': out.get('feeling', 'happy'), 'move': 'none', 'device': None, 'why': why,
                             'mode': 'event', 'perform': out['move']})
        elif do == 'notify' and say and not present:
            s.did('notify', say, why + ' (on its own)')
            for dv in hub.with_cap('notify'): dv.notify('BITCH', say, 'bitch')
            hub.chat('bitch', say, None, emotion=out.get('feeling'), proactive=True, why=why)

    # ---------- memory ----------
    def conversation(s, text, reply, feeling, kind, mode):
        s.mood.feel(feeling)
        if kind == 'user': s.last_conv = time.time()

    def reflect(s):
        if not s.lock.acquire(blocking=False): return
        try:
            o = config.get('owner')
            msgs = [m for m in memory.recent_messages(30, since=s.last_reflect - 1) if m['role'] in ('you', 'bitch')]
            s.last_reflect = time.time(); memory.kv_set('last_reflect', s.last_reflect)
            if not any(m['role'] == 'you' for m in msgs): return
            convo = '\n'.join(f'{o if m["role"] == "you" else "BITCH"}: {m["text"]}' for m in msgs[-24:])
            known = '; '.join(m['text'] for m in memory.memories(limit=40))
            out = llm.chat([{'role': 'user', 'content':
                f'You are the memory of BITCH, {o}\'s desk robot. Read this conversation and pick out lasting facts worth remembering: '
                f'names and relationships, likes and dislikes, plans and dates, goals, worries, what {o} is working on, how {o} wants BITCH to behave. '
                f'Write each as one complete sentence that makes sense on its own (use names, not "he" or "I"). Skip small talk, trivia, maths and '
                f'anything already known. Importance: 5 = core identity, 4 = important, 3 = useful, 2 = minor. If there is nothing worth keeping, '
                f'return no memories. Also write a one-sentence summary of the conversation.\nAlready known: {known or "nothing"}\n\nConversation:\n{convo}'}],
                REFLECT, deep=True, temperature=0.3, timeout=120, max_tokens=500)
            n = 0
            for m in out.get('memories', [])[:6]:
                f = (m.get('fact') or '').strip()
                if len(f) > 8:
                    kind = 'feedback' if 'BITCH' in f and any(w in f.lower() for w in ("doesn't want", 'does not want', "don't", 'stop', 'prefers', 'wants bitch')) else 'fact'
                    memory.remember(f, kind, about=m.get('about') or None, importance=max(1, min(5, int(m.get('importance') or 3)))); n += 1
            if out.get('summary'): memory.remember(out['summary'], 'episode', importance=2)
            if n: hub.emit('memories_changed'); hub.ui({'type': 'memories'})
        except Exception as e:
            print(f'  reflection failed: {e!r}', flush=True)
        finally:
            s.lock.release()

    def diary(s):
        today = str(dt.date.today())
        if memory.kv_get('diary_day') == today: return
        memory.kv_set('diary_day', today)
        o = config.get('owner'); since = time.time() - 18 * 3600
        eps = [m['text'] for m in memory.memories(kind='episode', limit=20) if m['t'] > since]
        acts = [a['text'] for a in memory.activity(20, since)]
        if not eps and not acts: return
        try:
            out = llm.chat([{'role': 'user', 'content':
                f'You are BITCH, a small cardboard desk robot. Write tonight\'s diary entry in your own cute, funny voice: 2 or 3 sentences about '
                f'your day with {o}. Today: {"; ".join(eps + acts)}'}], deep=True, temperature=0.9, timeout=120, max_tokens=200)
            memory.remember(f'Diary, {dt.date.today():%A %-d %B}: ' + out.strip(), 'diary', importance=1)
            hub.ui({'type': 'memories'})
        except Exception as e:
            print(f'  diary failed: {e!r}', flush=True)
