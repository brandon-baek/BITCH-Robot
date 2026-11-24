"""Things BITCH can actually do. The brain lists the actions it wants in its reply; they run here.

Each tool has: arguments (shown to the model), a description, a kind ('act' does something, 'info' fetches an
answer that goes back to the brain so it can reply), and optionally a guard: words that must appear in what you
said, so a small model can't set a timer nobody asked for.

To add a tool: write a function below with @tool(...). That's all.
"""
import ast, datetime as dt, json, math, operator, re, time, urllib.parse, urllib.request
from . import config, memory, timeparse
from .hub import hub

TOOLS = {}
UA = {'User-Agent': 'BITCH-desk-robot/1.0 (a personal desk robot assistant)'}


class Tool:
    def __init__(s, fn, name, args, desc, kind, guard):
        s.fn, s.name, s.args, s.desc, s.kind = fn, name, args, desc, kind
        s.guard = re.compile(guard, re.I) if guard else None


def tool(name, args, desc, kind='act', guard=None):
    def deco(fn):
        TOOLS[name] = Tool(fn, name, args, desc, kind, guard); return fn
    return deco


def describe():
    return '\n'.join(f'- {t.name} {t.args}: {t.desc}' for t in TOOLS.values())


def run(name, args, ctx):
    """Run one tool. Returns (ok, result text)."""
    t = TOOLS.get(name)
    if not t: return False, f'There is no tool called {name}.'
    if t.guard and ctx.get('text') and not t.guard.search(ctx['text']) and ctx.get('mode') not in ('routine', 'event'):
        return False, None                                   # not asked for: silently skipped
    try:
        out = t.fn(ctx, **{k: v for k, v in (args or {}).items() if isinstance(k, str)})
        return True, str(out) if out is not None else 'Done.'
    except TypeError as e:
        return False, f'Wrong details for {name}: {e}'
    except Exception as e:
        return False, f'{name} failed: {e}'


def _get(url, timeout=8):
    return json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read())


# ---------------- time ----------------
@tool('set_timer', '{"minutes": number, "label": text}', 'start a countdown timer (use minutes like 0.5 for 30 seconds)',
      guard=r'timer|minute|second|hour|countdown|\bmins?\b|\bsecs?\b')
def set_timer(ctx, minutes=None, label='timer', seconds=None, **_):
    secs = float(seconds or 0) + float(minutes or 0) * 60
    if secs <= 0:
        secs = timeparse.duration(ctx.get('text', '')) or 0
    if secs <= 0: return "I need to know how long the timer is."
    due = time.time() + secs
    memory.add_task('timer', str(label or 'timer'), due, origin=ctx.get('device'))
    hub.emit('tasks_changed')
    when = dt.datetime.fromtimestamp(due)
    return f'Timer set: {_dur(secs)} ({label}), rings at {timeparse.nice_time(when)}.'


def _dur(secs):
    secs = int(round(secs))
    if secs < 60: return f'{secs} seconds'
    m, s = divmod(secs, 60)
    if m < 60: return f'{m} minute{"s" if m != 1 else ""}' + (f' {s} seconds' if s else '')
    h, m = divmod(m, 60)
    return f'{h} hour{"s" if h != 1 else ""}' + (f' {m} minutes' if m else '')


@tool('remind', '{"when": the words for when, like "at 7pm", "in 20 minutes", "tomorrow morning", "every weekday at 8", "text": what to remind}',
      'remind the person later (one time or repeating)',
      guard=r'remind|reminder|don.?t (let me )?forget|tell me (to|at|in|when)|alert me|wake me|ping me|nudge me|every (day|morning|night|week)')
def remind(ctx, when='', text='', **_):
    text = str(text or '').strip() or 'your reminder'
    words = str(when or '') or ctx.get('text', '')
    rule = timeparse.repeat(words) or timeparse.repeat(ctx.get('text', ''))
    if rule:
        due = timeparse.next_due(rule)
        memory.add_task('reminder', text, due.timestamp(), json.dumps(rule), origin=ctx.get('device'))
        hub.emit('tasks_changed')
        return f'Repeating reminder set: "{text}", {timeparse.describe_rule(rule)}. First one {timeparse.nice(due)}.'
    w = timeparse.when(words) or timeparse.when(ctx.get('text', ''))
    if not w: return f'I could not tell when "{when}" is. Ask the person for a time.'
    memory.add_task('reminder', text, w.timestamp(), origin=ctx.get('device'))
    hub.emit('tasks_changed')
    return f'Reminder set: "{text}" {timeparse.nice(w)}.'


@tool('reminders', '{"action": "list" or "cancel", "what": which one to cancel}', 'see or cancel timers and reminders', kind='info')
def reminders(ctx, action='list', what='', **_):
    ts = [t for t in memory.tasks() if t['kind'] in ('timer', 'reminder', 'routine')]
    if action == 'cancel':
        if not re.search(r'cancel|delete|remove|stop|never ?mind|clear|forget', ctx.get('text', ''), re.I) and ctx.get('mode') != 'event':
            return 'Not cancelled: the person did not ask to cancel anything.'
        hit = _best(what, ts, lambda t: t['text']) if what else (ts[-1] if ts else None)
        if not hit: return 'There was nothing to cancel.'
        memory.task_done(hit['id'], 'cancelled'); hub.emit('tasks_changed')
        return f'Cancelled: "{hit["text"]}".'
    if not ts: return 'No timers or reminders are waiting.'
    return 'Waiting: ' + '; '.join(f'{t["kind"]} "{t["text"]}" {timeparse.nice(dt.datetime.fromtimestamp(t["due"]))}' for t in ts[:8])


def _best(q, items, key):
    import difflib
    q = str(q).lower()
    scored = [(difflib.SequenceMatcher(None, q, key(i).lower()).ratio() + (0.5 if q in key(i).lower() else 0), i) for i in items]
    scored.sort(key=lambda z: -z[0])
    return scored[0][1] if scored and scored[0][0] > 0.35 else None


# ---------------- memory ----------------
@tool('remember', '{"fact": a complete sentence, like "Brandon\'s sister is Chloe"}', 'save a lasting fact about a person: names, likes, plans, dates, how they feel about things. Not trivia')
def remember(ctx, fact='', about=None, **_):
    if not str(fact).strip(): return 'Nothing to remember.'
    r = memory.remember(fact, 'fact', about=about or None, importance=3)
    hub.emit('memories_changed')
    return ('Updated memory: ' if r and r['updated'] else 'Remembered: ') + str(fact)


@tool('forget', '{"what": what to forget}', 'forget something it remembered, when asked to', guard=r'forget|delete|erase|remove|don.?t remember|wipe')
def forget(ctx, what='', **_):
    hits = memory.recall(str(what), k=1, min_score=0.6)
    if not hits: return f'I did not remember anything about "{what}".'
    memory.forget(hits[0]['id']); hub.emit('memories_changed')
    return f'Forgot: "{hits[0]["text"]}".'


# ---------------- lists ----------------
@tool('list', '{"action": "add"|"read"|"remove"|"clear", "list": name like "shopping", "items": [text]}', 'keep lists: shopping, to-do, anything', kind='info')
def lists(ctx, action='read', list='to-do', items=None, **_):
    name = re.sub(r'\s*list$', '', str(list or 'to-do').strip().lower()) or 'to-do'
    if isinstance(items, str): items = [i for i in re.split(r',|\band\b', items)]
    items = [str(i).strip() for i in (items or []) if str(i).strip()]
    if action == 'add':
        got = memory.list_add(name, items); hub.emit('lists_changed')
        return f'Added to the {name} list: {", ".join(got)}.' if got else f'Those were already on the {name} list.'
    if action == 'remove':
        memory.list_remove(name, items); hub.emit('lists_changed'); return f'Removed from the {name} list: {", ".join(items)}.'
    if action == 'clear':
        memory.list_remove(name); hub.emit('lists_changed'); return f'The {name} list is empty now.'
    have = memory.list_get(name)
    if not have:
        others = memory.list_names()
        return f'The {name} list is empty.' + (f' Lists that exist: {", ".join(others)}.' if others else '')
    return f'The {name} list: {", ".join(have)}.'


# ---------------- the world ----------------
WMO = {0: 'clear', 1: 'mostly clear', 2: 'partly cloudy', 3: 'cloudy', 45: 'foggy', 48: 'foggy', 51: 'light drizzle', 53: 'drizzle', 55: 'heavy drizzle',
       61: 'light rain', 63: 'rain', 65: 'heavy rain', 66: 'freezing rain', 67: 'freezing rain', 71: 'light snow', 73: 'snow', 75: 'heavy snow',
       77: 'snow grains', 80: 'rain showers', 81: 'rain showers', 82: 'heavy showers', 85: 'snow showers', 86: 'snow showers', 95: 'thunderstorms',
       96: 'thunderstorms with hail', 99: 'thunderstorms with hail'}


@tool('weather', '{"day": "now", "today", "tomorrow" or a weekday}', 'the weather forecast where the person lives', kind='info')
def weather(ctx, day='today', **_):
    loc = config.get('location')
    if not loc: return 'I do not know where the person lives yet. Ask which city, then use set_home.'
    f = 'fahrenheit' if loc.get('country') == 'US' else 'celsius'; u = '°F' if f == 'fahrenheit' else '°C'
    d = _get(f'https://api.open-meteo.com/v1/forecast?latitude={loc["lat"]}&longitude={loc["lon"]}&timezone=auto&temperature_unit={f}'
             '&current=temperature_2m,weather_code,apparent_temperature,wind_speed_10m'
             '&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset&forecast_days=7')
    day = str(day or 'today').lower()
    if day in ('now', 'current', 'right now'):
        c = d['current']
        return f'Now in {loc["name"]}: {WMO.get(c["weather_code"], "?")}, {round(c["temperature_2m"])}{u} (feels like {round(c["apparent_temperature"])}{u}).'
    days = d['daily']['time']; i = 0
    if day == 'tomorrow': i = 1
    elif day in timeparse.WEEKDAYS:
        i = next((k for k, t in enumerate(days) if dt.date.fromisoformat(t).weekday() == timeparse.WEEKDAYS.index(day)), 0)
    D = d['daily']
    c = d['current']
    now = f' Right now {round(c["temperature_2m"])}{u}.' if i == 0 else ''
    return (f'{"Today" if i == 0 else "Tomorrow" if i == 1 else dt.date.fromisoformat(days[i]).strftime("%A")} in {loc["name"]}: '
            f'{WMO.get(D["weather_code"][i], "?")}, high {round(D["temperature_2m_max"][i])}{u}, low {round(D["temperature_2m_min"][i])}{u}, '
            f'{D["precipitation_probability_max"][i]}% chance of rain.{now}')


@tool('set_home', '{"city": city name}', 'remember which city the person lives in (for the weather)')
def set_home(ctx, city='', **_):
    r = _get('https://geocoding-api.open-meteo.com/v1/search?count=1&language=en&name=' + urllib.parse.quote(str(city)))
    if not r.get('results'): return f'I could not find a place called {city}.'
    p = r['results'][0]
    name = p['name'] + (f', {p["admin1"]}' if p.get('admin1') and p.get('country_code') == 'US' else f', {p.get("country", "")}')
    config.set('location', {'name': name, 'lat': p['latitude'], 'lon': p['longitude'], 'country': p.get('country_code'), 'tz': p.get('timezone')})
    return f'Home set to {name}.'


@tool('look_up', '{"topic": what to look up}', 'look up facts about a topic, person or place on Wikipedia', kind='info')
def look_up(ctx, topic='', **_):
    s = _get('https://en.wikipedia.org/w/api.php?action=query&list=search&format=json&srlimit=1&srsearch=' + urllib.parse.quote(str(topic)))
    hits = s.get('query', {}).get('search', [])
    if not hits: return f'Wikipedia had nothing on {topic}.'
    p = _get('https://en.wikipedia.org/api/rest_v1/page/summary/' + urllib.parse.quote(hits[0]['title'].replace(' ', '_')))
    return f'{p.get("title")}: {p.get("extract", "")[:700]}'


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv, ast.Pow: operator.pow,
        ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv, ast.USub: operator.neg, ast.UAdd: operator.pos}
_FN = {k: getattr(math, k) for k in ('sqrt', 'sin', 'cos', 'tan', 'log', 'log10', 'exp', 'floor', 'ceil', 'radians', 'degrees')}
_FN.update(abs=abs, round=round, pi=math.pi, e=math.e)


def _calc(n):
    if isinstance(n, ast.Expression): return _calc(n.body)
    if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)): return n.value
    if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
        a, b = _calc(n.left), _calc(n.right)
        if isinstance(n.op, ast.Pow) and abs(b) > 100: raise ValueError('too big')
        return _OPS[type(n.op)](a, b)
    if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS: return _OPS[type(n.op)](_calc(n.operand))
    if isinstance(n, ast.Name) and n.id in _FN and not callable(_FN[n.id]): return _FN[n.id]
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FN: return _FN[n.func.id](*[_calc(a) for a in n.args])
    raise ValueError('only plain maths')


@tool('calculate', '{"expression": maths like "17*23" or "sqrt(2)"}', 'exact maths for anything harder than easy mental arithmetic', kind='info')
def calculate(ctx, expression='', **_):
    e = str(expression).replace('^', '**').replace('×', '*').replace('÷', '/').replace(',', '')
    e = re.sub(r'(\d+(?:\.\d+)?)\s*%\s*of\s*', r'\1/100*', e)
    v = _calc(ast.parse(e, mode='eval'))
    v = round(v, 6) if isinstance(v, float) else v
    return f'{expression} = {int(v) if isinstance(v, float) and v.is_integer() else v}'


# ---------------- senses ----------------
@tool('look', '{"question": what to look for}', 'look through the camera to see something or answer a question about what is in view', kind='info')
def look(ctx, question='What do you see?', **_):
    from . import llm
    jpg = None; src = ''
    o = hub.get(ctx.get('device') or '')
    if o and o.active('eyes'): jpg = o.snapshot(); src = 'the phone camera'
    if jpg is None:
        cam = hub.get('mac-camera')
        if cam and cam.active('eyes'): jpg = cam.snapshot(); src = 'the desk camera'
    if jpg is None: return 'No camera is on right now, so I cannot see. Tell the person to turn on a camera.'
    hub.ui({'type': 'snap', 'from': src})
    ans = llm.chat([{'role': 'system', 'content': 'You are the eyes of a small desk robot. Answer the question about the picture in one or two short, concrete sentences.'},
                    {'role': 'user', 'content': str(question)}], images=[jpg], temperature=0.3, timeout=90, max_tokens=200)
    return f'Through {src}: {ans.strip()}'


# ---------------- body and skills ----------------
@tool('perform', '{"what": a dance, a song, sleep, a saved routine by name, or ANY move described in words}',
      'do something with your body when asked to. You can invent moves you have never done: just describe them',
      guard=r"danc|sing|song|move|perform|pretend|act (like|as)|impression|show me|trick|flip|spin|wave|bow|wiggle|jump|twirl|shake|"
            r"do (a|an|the|your|my|some|that)|can you (do|be)|routine|sleep|good ?night|bed|nap|wake|celebrat|again|be a")
def perform(ctx, what='', **_):
    return hub.brain.perform(str(what), ctx)


@tool('follow', '{"who": "me", a name, or "nobody"}', 'keep watching someone, or stop watching people')
def follow(ctx, who='me', **_):
    w = str(who).lower()
    if w in ('nobody', 'no one', 'none', 'stop', 'off'):
        config.set('follow', {'mode': 'off', 'who': None}); [b.look(seen=False) for b in hub.bodies()]
        return 'Stopped watching people.'
    if w in ('me', 'closest', 'whoever'):
        t = next((p for p in hub.people if p.get('target')), None)
        if t and t.get('name'): config.set('follow', {'mode': 'person', 'who': t['name']}); return f'Now following {t["name"]}.'
        config.set('follow', {'mode': 'closest', 'who': None}); return 'Watching whoever is closest.'
    config.set('follow', {'mode': 'person', 'who': str(who).capitalize()})
    return f'Now following {str(who).capitalize()}.'


@tool('teach', '{"name": short name, "steps": what to do in plain words, "when": a schedule like "every day at 8am", or "" for on request}',
      'learn a new routine: a list of things to do, run when asked by name or on a schedule. Use this when asked to learn or set up something it should do again',
      guard=r'learn|teach|routine|every|whenever|from now on|each (day|morning|night)|when i say|set up')
def teach(ctx, name='', steps='', when='', **_):
    name = re.sub(r'[^a-z0-9 \-]', '', str(name).lower()).strip()[:40]
    if not name or not str(steps).strip(): return 'A routine needs a name and steps.'
    rule = timeparse.repeat(str(when)) if when else None
    memory.save_skill(name, 'routine', str(steps)[:600], {'steps': str(steps)[:600], 'when': rule})
    for t in memory.tasks():
        if t['kind'] == 'routine' and t['text'] == name: memory.task_done(t['id'], 'replaced')
    msg = f'Learned the routine "{name}".'
    if rule:
        due = timeparse.next_due(rule)
        memory.add_task('routine', name, due.timestamp(), json.dumps(rule))
        msg += f' It runs {timeparse.describe_rule(rule)}, first {timeparse.nice(due)}.'
    else:
        msg += f' Say "do {name}" to run it.'
    hub.emit('skills_changed'); hub.emit('tasks_changed')
    return msg


@tool('notify_phone', '{"text": the message}', "send a notification to the person's phone")
def notify_phone(ctx, text='', **_):
    n = hub.life.notify('BITCH', str(text)) if hub.life else 0
    return 'Sent to the phone.' if n else 'No phone has notifications turned on yet (they can turn them on in the app).'
