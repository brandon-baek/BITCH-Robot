"""Things BITCH can actually do in a conversation: timers, lists, weather, its own volume, music on the Mac.

The model picks a tool with the `do` field and fills that tool's fields; the code here does the work. A tool is only
offered when the request sounds like it (so ordinary chat never pays for the extra fields), and BITCH never claims
something happened unless the tool ran.

Two kinds:
- action tools (set a timer, add to a list, change volume): the model says a short confirmation while the code acts.
- data tools (weather, what's on the list, how long is left): the code gets the facts first, then the model answers
  from them, so numbers are never invented.
"""
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

from . import config


@dataclass
class Tool:
    name: str
    when: re.Pattern            # the request sounds like this tool
    line: str                   # how the model should use it
    fields: dict = field(default_factory=dict)
    data: bool = False          # True: look up first, then answer from the result
    run: object = None          # run(args, context) -> str (what happened, or the facts)


# ---------------------------------------------------------------- timers

class Timers:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = {}   # id -> {'label', 'ends', 'source', 'timer'}
        self.next_id = 1

    def start(self, seconds, label, source):
        with self.lock:
            key, self.next_id = self.next_id, self.next_id + 1
            timer = threading.Timer(seconds, self.ring, (key,))
            timer.daemon = True
            self.running[key] = {'label': label, 'ends': time.time() + seconds, 'source': source, 'timer': timer}
            timer.start()
        return key

    def ring(self, key):
        from .hub import hub

        with self.lock:
            item = self.running.pop(key, None)
        if not item or not hub.brain:
            return
        what = f'Your {item["label"]} timer is done.' if item['label'] else 'Time’s up. Your timer is done.'
        hub.brain.announce(item['source'], what, chime=True)

    def cancel(self, label=''):
        with self.lock:
            keys = [key for key, item in self.running.items()
                    if not label or label.casefold() in item['label'].casefold()] or (
                list(self.running)[-1:] if self.running else [])
            for key in keys:
                self.running.pop(key)['timer'].cancel()
        return len(keys)

    def status(self):
        with self.lock:
            now = time.time()
            return [(item['label'], max(0, item['ends'] - now)) for item in self.running.values()]


timers = Timers()


def say_duration(seconds):
    seconds = round(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    parts = [f'{hours} hour{"s" * (hours != 1)}'] if hours else []
    if minutes:
        parts.append(f'{minutes} minute{"s" * (minutes != 1)}')
    if secs and not hours:
        parts.append(f'{secs} second{"s" * (secs != 1)}')
    return ' and '.join(parts) or 'less than a second'


def set_timer(args, context):
    seconds = args.get('seconds')
    if not isinstance(seconds, (int, float)) or not 1 <= seconds <= 24 * 3600:
        return 'FAILED: no usable length. Ask how long.'
    label = (args.get('label') or '').strip()[:40]
    timers.start(seconds, label, context['source'])
    return f'Timer set: {say_duration(seconds)}' + (f' for {label}' if label else '') + '.'


def cancel_timer(args, context):
    count = timers.cancel((args.get('label') or '').strip())
    return f'Canceled {count} timer{"s" * (count != 1)}.' if count else 'FAILED: there is no timer running.'


def check_timers(args, context):
    running = timers.status()
    if not running:
        return 'No timers are running.'
    return 'Running timers: ' + '; '.join(f'{label or "timer"}: {say_duration(left)} left' for label, left in running)


# ---------------------------------------------------------------- lists

def lists_path():
    return config.HOME / 'lists.json'


def load_lists():
    try:
        data = json.loads(lists_path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_lists(data):
    path = lists_path()
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=1))
    temporary.replace(path)


def list_name(args, existing=None):
    name = re.sub(r'^(?:the|my|our)\s+|\s*list$', '', (args.get('list') or '').strip().casefold()).strip()
    existing = load_lists() if existing is None else existing
    if name and name in existing:
        return name
    if not name or name in ('the', 'a', 'that', 'this'):
        # "the list": the shopping list, or the only list there is.
        lists = [key for key, items in existing.items() if items]
        return lists[0] if len(lists) == 1 else 'shopping'
    return name


def clean_items(args):
    items = args.get('items') or []
    return [str(item).strip()[:60] for item in items if str(item).strip()][:12]


def add_to_list(args, context):
    items, name = clean_items(args), list_name(args)
    if not items:
        return 'FAILED: nothing to add. Ask what to add.'
    data = load_lists()
    current = data.setdefault(name, [])
    added = [item for item in items if item.casefold() not in {i.casefold() for i in current}]
    current.extend(added)
    save_lists(data)
    return f'Added to the {name} list: {", ".join(added)}.' if added else f'Already on the {name} list.'


def remove_from_list(args, context):
    items, name = clean_items(args), list_name(args)
    data = load_lists()
    current = data.get(name, [])
    gone = [item for item in current if any(word.casefold() in item.casefold() for word in items)]
    data[name] = [item for item in current if item not in gone]
    save_lists(data)
    return f'Removed from the {name} list: {", ".join(gone)}.' if gone else f'FAILED: not on the {name} list.'


def read_list(args, context):
    name = list_name(args)
    items = load_lists().get(name, [])
    return f'The {name} list has: {", ".join(items)}.' if items else f'The {name} list is empty.'


def clear_list(args, context):
    name = list_name(args)
    data = load_lists()
    count = len(data.pop(name, []))
    save_lists(data)
    return f'Cleared the {name} list ({count} items).'


# ---------------------------------------------------------------- weather (Open-Meteo: free, no account or key)

WMO = {0: 'clear', 1: 'mostly clear', 2: 'partly cloudy', 3: 'overcast', 45: 'foggy', 48: 'foggy',
       51: 'light drizzle', 53: 'drizzle', 55: 'heavy drizzle', 56: 'freezing drizzle', 57: 'freezing drizzle',
       61: 'light rain', 63: 'rain', 65: 'heavy rain', 66: 'freezing rain', 67: 'freezing rain',
       71: 'light snow', 73: 'snow', 75: 'heavy snow', 77: 'snow grains', 80: 'rain showers', 81: 'rain showers',
       82: 'heavy rain showers', 85: 'snow showers', 86: 'heavy snow showers', 95: 'thunderstorms',
       96: 'thunderstorms with hail', 99: 'thunderstorms with hail'}


def fetch(url, timeout=3.5):
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'desk-robot'}), timeout=timeout) as r:
        return json.load(r)


def find_place(name):
    data = fetch('https://geocoding-api.open-meteo.com/v1/search?' + urllib.parse.urlencode({'name': name, 'count': 1}))
    found = (data.get('results') or [None])[0]
    if not found:
        return None
    label = ', '.join(part for part in (found.get('name'), found.get('admin1')) if part)
    return {'name': label, 'lat': found['latitude'], 'lon': found['longitude'],
            'imperial': found.get('country_code') in ('US', 'LR', 'MM')}


def weather(args, context):
    place = (args.get('place') or '').strip()
    try:
        spot = find_place(place) if place else config.get('home_place')
    except (OSError, ValueError):
        return 'FAILED: the weather service did not answer. Say so briefly.'
    if not spot:
        return ('FAILED: home location is not set. Ask what city they live in.' if not place
                else f'FAILED: could not find a place called {place}.')
    imperial = spot.get('imperial', True)
    query = {'latitude': spot['lat'], 'longitude': spot['lon'], 'timezone': 'auto', 'forecast_days': 7,
             'current': 'temperature_2m,apparent_temperature,weather_code,wind_speed_10m,precipitation',
             'hourly': 'precipitation_probability,weather_code,temperature_2m',
             'daily': 'weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max',
             'temperature_unit': 'fahrenheit' if imperial else 'celsius',
             'wind_speed_unit': 'mph' if imperial else 'kmh'}
    try:
        data = fetch('https://api.open-meteo.com/v1/forecast?' + urllib.parse.urlencode(query))
    except (OSError, ValueError):
        return 'FAILED: the weather service did not answer. Say so briefly.'
    unit, wind = ('°F', 'mph') if imperial else ('°C', 'km/h')
    now, daily, hourly = data['current'], data['daily'], data['hourly']
    lines = [f"Weather for {spot['name']}. Now: {round(now['temperature_2m'])}{unit} (feels like "
             f"{round(now['apparent_temperature'])}{unit}), {WMO.get(now['weather_code'], 'unsettled')}, wind "
             f"{round(now['wind_speed_10m'])} {wind}."]
    # The next 12 hours of rain chances, so "will it rain tonight" has an answer.
    current_hour = now['time'][:13]
    start = next((i for i, t in enumerate(hourly['time']) if t[:13] >= current_hour), 0)
    rain = [(hourly['time'][i][11:16], hourly['precipitation_probability'][i]) for i in range(start, min(start + 12, len(hourly['time'])))]
    wet = [(hour, chance) for hour, chance in rain if (chance or 0) >= 30]
    lines.append('Rain chance next 12 hours: ' + (', '.join(f'{h} {c}%' for h, c in wet[:6]) if wet else 'low all along') + '.')
    day = args.get('day') or 'today'
    days = {'now': 1, 'today': 1, 'tomorrow': 2, 'week': 7}.get(day, 2)
    names = ['Today', 'Tomorrow']
    for i in range(days):
        label = names[i] if i < 2 else time.strftime('%A', time.strptime(daily['time'][i], '%Y-%m-%d'))
        lines.append(f"{label}: {WMO.get(daily['weather_code'][i], 'mixed')}, high {round(daily['temperature_2m_max'][i])}{unit}, "
                     f"low {round(daily['temperature_2m_min'][i])}{unit}, rain chance {daily['precipitation_probability_max'][i]}%.")
    return ' '.join(lines)


WEATHER_ASK = re.compile(r'\b(?:weather|forecast|rain\w*|snow\w*|umbrella|temperature|how (?:hot|cold|warm) is it|'
                         r'jacket|coat|sunny|storm\w*)\b', re.I)
_home_weather = {}


def home_weather(text):
    """Home weather for a request that is clearly about it, fetched before the model starts (cached ten minutes),
    so the answer takes one pass instead of two. None when it isn't a weather question, names another place, or
    the service doesn't answer quickly."""
    home = config.get('home_place')
    if not home or not WEATHER_ASK.search(text) or re.search(r'\b(?:in|at|for) [A-Z][a-z]', text):
        return None
    day = 'week' if re.search(r'\b(?:week|weekend|days)\b', text, re.I) else 'tomorrow'
    key = (home['name'], day)
    cached = _home_weather.get(key)
    if cached and time.monotonic() - cached[0] < 600:
        return cached[1]
    result = weather({'place': '', 'day': day}, {})
    if result.startswith('FAILED'):
        return None
    _home_weather[key] = (time.monotonic(), result)
    return result


def set_home(args, context):
    place = (args.get('place') or '').strip()
    if not place:
        return 'FAILED: no place given. Ask which city.'
    try:
        spot = find_place(place)
    except (OSError, ValueError):
        return 'FAILED: could not reach the map service.'
    if not spot:
        return f'FAILED: could not find {place}. Ask them to say the city and state.'
    config.update({'home_place': spot})
    return f"Home is now {spot['name']}."


# ---------------------------------------------------------------- BITCH's own voice volume, and music on the Mac

def volume(args, context):
    change, level = args.get('change'), args.get('level')
    current = config.get('volume')
    if change == 'mute':
        config.update({'muted': True})
        return 'Muted. (Say "unmute" or use the app to bring the voice back.)'
    if change == 'unmute':
        config.update({'muted': False})
        return f'Unmuted at {current}%.'
    if change == 'set' and isinstance(level, (int, float)):
        new = level
    else:
        new = current + (20 if change == 'up' else -20 if change == 'down' else 0)
    new = int(max(10, min(100, new)))
    config.update({'volume': new, 'muted': False})
    return f'Voice volume {current}% -> {new}%.' + (' That is the maximum.' if new == 100 and change == 'up' else '')


def osascript(script):
    return subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=5)


def music(args, context):
    action = args.get('action')
    if sys.platform != 'darwin':
        return 'FAILED: music control only works on the Mac.'
    player = next((app for app in ('Spotify', 'Music')
                   if osascript(f'application "{app}" is running').stdout.strip() == 'true'), 'Music')
    command = {'play': 'play', 'pause': 'pause', 'next': 'next track', 'previous': 'previous track'}.get(action)
    if not command:
        return 'FAILED: unknown music action.'
    result = osascript(f'tell application "{player}" to {command}')
    if result.returncode:
        return f'FAILED: {player} did not respond.'
    if action in ('play', 'next', 'previous'):
        playing = osascript(f'tell application "{player}" to (name of current track) & " by " & (artist of current track)')
        if playing.returncode == 0 and playing.stdout.strip():
            return f'{player}: now playing {playing.stdout.strip()}.'
    return f'{player}: {command}.'


# ---------------------------------------------------------------- the catalog

ITEMS = {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 12}
TOOLS = [
    Tool('timer', re.compile(r'\btimers?\b|\bcount ?down\b|\bin \d+ (?:min|sec)', re.I),
         'timer: start a countdown. Put the total length in seconds and an optional label like "pasta".',
         {'seconds': {'type': 'integer'}, 'label': {'type': 'string', 'maxLength': 40}}, run=set_timer),
    Tool('cancel_timer', re.compile(r'\b(?:cancel|stop|kill|clear)\b.*\btimers?\b', re.I),
         'cancel_timer: stop a running timer (label to pick one, empty for the latest).',
         {'label': {'type': 'string', 'maxLength': 40}}, run=cancel_timer),
    Tool('check_timers', re.compile(r'\btimers?\b|\bhow (?:long|much time)\b', re.I),
         'check_timers: how much time is left on running timers.', data=True, run=check_timers),
    Tool('list_add', re.compile(r'\blists?\b|\badd\b|\bput\b.*\bon\b|\bwe(?:\'re| are) out of\b|\bneed (?:more|to buy)\b', re.I),
         'list_add: put items on a list (shopping unless they name another list).',
         {'list': {'type': 'string', 'maxLength': 30}, 'items': ITEMS}, run=add_to_list),
    Tool('list_remove', re.compile(r'\blists?\b.*\b(?:remove|take|cross|delete)\b|\b(?:remove|take|cross|delete)\b.*\blists?\b', re.I),
         'list_remove: take items off a list.', {'list': {'type': 'string', 'maxLength': 30}, 'items': ITEMS},
         run=remove_from_list),
    Tool('list_read', re.compile(r'\blists?\b|\bwhat do (?:we|i) need\b', re.I),
         'list_read: what is on a list.', {'list': {'type': 'string', 'maxLength': 30}}, data=True, run=read_list),
    Tool('list_clear', re.compile(r'\b(?:clear|empty|wipe|reset)\b.*\blists?\b', re.I),
         'list_clear: empty a whole list, only when they clearly ask.', {'list': {'type': 'string', 'maxLength': 30}},
         run=clear_list),
    Tool('weather', re.compile(r'\b(?:weather|forecast|rain\w*|snow\w*|temperature|degrees|how (?:hot|cold|warm)|'
                               r'umbrella|jacket|coat|sunny|cloudy|windy|outside|storm\w*)\b', re.I),
         'weather: only when they ask about the weather, or what to wear or bring outside. place is the city they '
         'name (empty for home); day is now, today, tomorrow or week. Not for how they feel.',
         {'place': {'type': 'string', 'maxLength': 60}, 'day': {'type': 'string', 'enum': ['now', 'today', 'tomorrow', 'week']}},
         data=True, run=weather),
    Tool('set_home', re.compile(r'\b(?:i live|we live|home is|i\'m from|we\'re in|i\'m in|located in|my city)\b', re.I),
         'set_home: remember which city is home (for weather).', {'place': {'type': 'string', 'maxLength': 60}},
         run=set_home),
    Tool('volume', re.compile(r'\b(?:volume|louder|quieter|softer|speak up|too loud|turn (?:it |yourself )?(?:up|down)|'
                              r'(?:un)?mute)\b', re.I),
         'volume: your own speaking volume. change is up, down, set (with level 10 to 100), mute or unmute.',
         {'change': {'type': 'string', 'enum': ['up', 'down', 'set', 'mute', 'unmute']}, 'level': {'type': 'integer'}},
         run=volume),
    Tool('music', re.compile(r'\b(?:music|song|track|spotify|play(?:ing)?|pause|skip|resume)\b', re.I),
         'music: control music on the Mac (play, pause, next, previous).',
         {'action': {'type': 'string', 'enum': ['play', 'pause', 'next', 'previous']}}, run=music),
]
BY_NAME = {tool.name: tool for tool in TOOLS}


def offered(text):
    """The tools this request might need, so their fields are only in the schema when they could matter."""
    return [tool for tool in TOOLS if tool.when.search(text)]


def schema_fields(tools):
    """Each tool's fields, namespaced (timer.seconds -> timer_seconds), so two tools never collide."""
    fields = {}
    for tool in tools:
        for key, spec in tool.fields.items():
            fields[f'{tool.name}_{key}'] = spec
    return fields


def arguments(tool, turn):
    return {key: turn.get(f'{tool.name}_{key}') for key in tool.fields}


def run(name, turn, context):
    tool = BY_NAME[name]
    try:
        return tool.run(arguments(tool, turn), context)
    except Exception as error:   # a tool failing must never take the conversation down
        return f'FAILED: {error}'
