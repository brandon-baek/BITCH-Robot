"""Understanding times the way people say them: "in 10 minutes", "at 7", "tomorrow morning", "every weekday at 8".

Small language models are bad at date maths, so BITCH passes your words here instead of computing dates itself.
"""
import datetime as dt, re

WEEKDAYS = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
NUMS = {'a': 1, 'an': 1, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
        'eleven': 11, 'twelve': 12, 'fifteen': 15, 'twenty': 20, 'thirty': 30, 'forty': 40, 'forty-five': 45, 'fifty': 50, 'sixty': 60,
        'a couple of': 2, 'a couple': 2, 'a few': 3, 'few': 3}
UNIT = {'s': 1, 'sec': 1, 'secs': 1, 'second': 1, 'seconds': 1, 'm': 60, 'min': 60, 'mins': 60, 'minute': 60, 'minutes': 60,
        'h': 3600, 'hr': 3600, 'hrs': 3600, 'hour': 3600, 'hours': 3600, 'day': 86400, 'days': 86400, 'week': 604800, 'weeks': 604800}
PARTS = {'morning': 9, 'afternoon': 15, 'evening': 19, 'tonight': 20, 'night': 21, 'noon': 12, 'midday': 12, 'lunch': 12, 'midnight': 0}


def _num(w):
    w = w.strip().lower()
    if re.fullmatch(r'\d+(\.\d+)?', w): return float(w)
    return NUMS.get(w)


def duration(text):
    """'10 minutes', 'an hour and a half', '90s' -> seconds, or None."""
    t = text.lower().replace('half an hour', '30 minutes').replace('an hour and a half', '90 minutes').replace('a quarter of an hour', '15 minutes')
    total = 0; found = False
    for m in re.finditer(r'(\d+(?:\.\d+)?|an?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty-five|forty|fifty|sixty|a couple of|a couple|a few|few)\s*'
                         r'(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|[smh])\b', t):
        n = _num(m.group(1)); u = UNIT.get(m.group(2))
        if n is not None and u: total += n * u; found = True
    if found and re.search(r'\band a half\b', t) and total % 3600 == 0: total += 1800
    return total if found else None


def _clock(t):
    """Find a clock time. Returns (hour, minute, has_ampm) or None."""
    m = re.search(r'\b(\d{1,2})(?::|\.)(\d{2})\s*(am|pm|a\.m\.|p\.m\.)?', t) or re.search(r'\b(\d{1,2})\s*(am|pm|a\.m\.|p\.m\.|o\'?clock)', t) \
        or re.search(r'\bat\s+(\d{1,2})\b(?!\s*(?:minutes?|mins?|hours?|seconds?|days?))', t)
    if not m: return None
    g = m.groups(); h = int(g[0]); mi = 0; ap = None
    if len(g) >= 2 and g[1] and g[1].isdigit(): mi = int(g[1]); ap = g[2] if len(g) > 2 else None
    elif len(g) >= 2: ap = g[1]
    if ap and ap.startswith('o'): ap = None
    if ap: ap = 'pm' if ap.startswith('p') else 'am'
    if h > 23 or mi > 59: return None
    if ap == 'pm' and h < 12: h += 12
    if ap == 'am' and h == 12: h = 0
    return h, mi, bool(ap)


def when(text, now=None):
    """A moment in the future from natural words. Returns a datetime or None."""
    now = now or dt.datetime.now()
    t = ' ' + text.lower().strip() + ' '
    m = re.fullmatch(r'\s*(\d{4}-\d{2}-\d{2})[ t](\d{1,2}:\d{2})\s*', t)
    if m:
        try: return dt.datetime.fromisoformat(m.group(1) + ' ' + m.group(2).zfill(5))
        except ValueError: pass
    if re.search(r'\bin\b|\bfrom now\b|\blater\b', t) or re.fullmatch(r'\s*[\d.]+\s*\w+\s*', t):
        d = duration(t)
        if d: return now + dt.timedelta(seconds=d)
    day = None
    if 'tomorrow' in t: day = now.date() + dt.timedelta(days=1)
    elif re.search(r'\b(today|tonight|this (morning|afternoon|evening))\b', t): day = now.date()
    else:
        for i, w in enumerate(WEEKDAYS):
            if re.search(r'\b' + w + r's?\b', t):
                ahead = (i - now.weekday()) % 7
                if ahead == 0 or 'next ' + w in t: ahead = ahead or 7
                day = now.date() + dt.timedelta(days=ahead); break
    c = _clock(t)
    part = next((PARTS[p] for p in PARTS if re.search(r'\b' + p + r'\b', t)), None)
    if c is None and part is None and day is None: return None
    if c is None:
        h, mi, amb = (part if part is not None else 9), 0, False
    else:
        h, mi, has = c; amb = not has and 1 <= h <= 12
        if amb and (part is not None and part >= 15 or 'tonight' in t) and h < 12: h += 12; amb = False
    base = day or now.date()
    cand = dt.datetime.combine(base, dt.time(h % 24, mi))
    if amb:     # "at 7": the next 7 o'clock that hasn't happened yet (7 am or 7 pm)
        options = [cand, cand + dt.timedelta(hours=12)] if h < 12 else [cand]
        if day is None: options += [o + dt.timedelta(days=1) for o in options]
        elif h <= 6: options = [cand + dt.timedelta(hours=12)]          # "tomorrow at 3" means 3 pm
        fut = [o for o in options if o > now]
        return min(fut) if fut else None
    if cand <= now and day is None: cand += dt.timedelta(days=1)
    return cand if cand > now else None


def repeat(text):
    """'every day at 8', 'weekdays at 7:30', 'every monday', 'every 2 hours' -> a repeat rule, or None."""
    t = text.lower()
    m = re.search(r'every\s+(\d+|an?|two|three|four|five|six)?\s*(minutes?|hours?)', t)
    if m:
        n = _num(m.group(1) or '1') or 1
        return {'every': 'interval', 'secs': int(n * (60 if m.group(2).startswith('min') else 3600))}
    c = _clock(' ' + t + ' '); part = next((PARTS[p] for p in PARTS if re.search(r'\b' + p + r'\b', t)), None)
    hh, mm = (c[0], c[1]) if c else ((part, 0) if part is not None else (None, None))
    if c and not c[2] and 1 <= hh <= 6: hh += 12
    days = None
    if re.search(r'\bweekdays?\b|every work ?day', t): days = [0, 1, 2, 3, 4]
    elif re.search(r'\bweekends?\b', t): days = [5, 6]
    elif re.search(r'\b(every ?day|daily|each day|every morning|every evening|every night|every afternoon)\b', t): days = list(range(7))
    else:
        ds = [i for i, w in enumerate(WEEKDAYS) if re.search(r'\b(every|on)\s+' + w + r's?\b', t) or re.search(r'\b' + w + r's\b', t)]
        if ds: days = ds
    if days is None: return None
    return {'every': 'days', 'days': days, 'at': f'{hh if hh is not None else 9:02d}:{mm or 0:02d}'}


def next_due(rule, after=None):
    after = after or dt.datetime.now()
    if rule['every'] == 'interval': return after + dt.timedelta(seconds=rule['secs'])
    h, m = map(int, rule['at'].split(':'))
    for i in range(0, 8):
        d = after.date() + dt.timedelta(days=i)
        c = dt.datetime.combine(d, dt.time(h, m))
        if d.weekday() in rule['days'] and c > after: return c
    return None


def describe_rule(rule):
    if rule['every'] == 'interval':
        s = rule['secs']; return f'every {s // 3600} hours' if s % 3600 == 0 else f'every {s // 60} minutes'
    d = rule['days']
    days = 'every day' if len(d) == 7 else 'weekdays' if d == [0, 1, 2, 3, 4] else 'weekends' if d == [5, 6] else \
           ', '.join(WEEKDAYS[i].capitalize() + 's' for i in d)
    return f'{days} at {nice_time(rule["at"])}'


def nice_time(hhmm):
    h, m = map(int, hhmm.split(':')) if isinstance(hhmm, str) else (hhmm.hour, hhmm.minute)
    return f'{h % 12 or 12}{":%02d" % m if m else ""} {"am" if h < 12 else "pm"}'


def nice(when_dt, now=None):
    """'at 7 pm', 'tomorrow at 9 am', 'on Friday at 10 am', 'in 10 minutes'."""
    now = now or dt.datetime.now()
    secs = (when_dt - now).total_seconds()
    if secs < 3600 and secs > 0:
        mins = round(secs / 60)
        return f'in {mins} minute{"s" if mins != 1 else ""}' if mins >= 1 else f'in {round(secs)} seconds'
    t = nice_time(when_dt)
    if when_dt.date() == now.date(): return f'at {t}'
    if when_dt.date() == now.date() + dt.timedelta(days=1): return f'tomorrow at {t}'
    if (when_dt.date() - now.date()).days < 7: return f'on {WEEKDAYS[when_dt.weekday()].capitalize()} at {t}'
    return f'on {when_dt.strftime("%B %-d")} at {t}'
