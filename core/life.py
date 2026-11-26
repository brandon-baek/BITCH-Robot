"""BITCH's living presence: continuous mood, nap/sleep companion, smart alarm, arrivals, and visual callbacks."""
from datetime import datetime, timedelta
import re
import threading
import time

from . import config, gaze, helper, llm, memory, notifications, streaming, voice
from .hub import hub
from .rhythm import rhythm

NAP_CMD = re.compile(
    r"\b(?:(?:i(?:'|’)m |i am )?(?:going to |gonna |about to )?(?:take a nap|nap|go to sleep|go to bed|sleep for|catch some sleep|"
    r"lay down|lie down)|sleep with me|nap time|bedtime)\b", re.I)
ALARM_CMD = re.compile(
    r"\b(?:wake me(?: up)?|set an? alarm|alarm for|need to be up|gotta be up|have to be up)\b", re.I)
REMIND_CMD = re.compile(
    r"\b(?:remind me|set a reminder)\s+(?:(?:every\s+\w+|on\s+\w+|weekdays?|daily|tomorrow|today)\s+)*"
    r"(?:(?:at|by|for|in)\s+.+?)\s+(to|about)\s+(.+?)[.!?]*$"
    r"|\b(?:remind me|set a reminder)\s+(to|about)\s+(.+?)\s+(?:(?:every\s+\w+|on\s+\w+|weekdays?|daily|tomorrow|today)\s+)*"
    r"(?:at|by|for|in)\s+(.+?)[.!?]*$", re.I)
CANCEL_SCHED_CMD = re.compile(
    r"\b(?:cancel|delete|remove|clear|turn off)\s+(?:my\s+)?(?:(\d{1,2}(?::\d{2})?)\s*(?:a\.?m\.?|p\.?m\.?)?\s*)?"
    r"(?:alarm|wake[- ]?up|reminder|schedule)(?:\s+(?:for|about)\s+(.+?))?[.!?]*$", re.I)
LIST_SCHED_CMD = re.compile(r"\b(?:what(?:'s| is| are) my (?:alarms?|reminders?|daily routine|wake[- ]?up)|list my (?:alarms?|reminders?))\b", re.I)
RECURRING = re.compile(
    r"\b(every\s+day|daily|every\s+morning|weekdays?|on\s+weekdays|for\s+school|weekends?|"
    r"every\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?)\b", re.I)
WAKE_CMD = re.compile(
    r"^(?:(?:hey |okay |ok )?(?:i(?:'|’)m |i am )?(?:awake|up)|good morning|wake up|stop(?: the)? alarm|dismiss(?: the)? alarm|turn off(?: the)? alarm)[.!? ]*$",
    re.I)
SNOOZE_CMD = re.compile(r"\bsnooze(?:\s+(?:for\s+)?(\d+)\s*(?:min(?:ute)?s?)?)?\b", re.I)
TIME_AT = re.compile(r"\b(?:at|by|for)\s+(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?\b", re.I)
DURATION = re.compile(
    r"\b(?:in|for)\s+(?:(\d+(?:\.\d+)?)|an?|a couple(?: of)?|half an)\s*"
    r"(hours?|hrs?|minutes?|mins?|seconds?|secs?)\b", re.I)
BACKUP_CONTACT = re.compile(
    r"\b(?:contact|text|message|call|ping|tell)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+(?:to wake|if i don(?:'|’)t)", re.I)
DELIVERY_SIGNAL = re.compile(
    r"\b(?:delivered|out for delivery|package (?:arrived|was|is|delivered)|has arrived|arriving today|"
    r"shipment (?:arrived|delivered|is)|at your (?:front )?door|left near)\b", re.I)
_TRAILING_TIME = re.compile(
    r"\s*(?:\b(?:on\s+weekdays|weekdays?|weekends?|every\s+(?:day|morning|monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?|daily|tomorrow|today|tonight)\b)\s*$",
    re.I)


def parse_days(text):
    """Parse recurrence wording ('weekdays', 'every Tuesday', 'daily') -> normalized days string or None."""
    lower = (text or '').lower()
    if 'weekday' in lower:
        return 'mon,tue,wed,thu,fri'
    if 'weekend' in lower:
        return 'sat,sun'
    if any(k in lower for k in ('every day', 'daily', 'every morning')):
        return 'daily'
    day_map = [('monday', 'mon'), ('tuesday', 'tue'), ('wednesday', 'wed'), ('thursday', 'thu'),
               ('friday', 'fri'), ('saturday', 'sat'), ('sunday', 'sun')]
    found = [short for full, short in day_map if re.search(rf'\bevery\s+{full}s?\b', lower)]
    if found:
        return ','.join(found)
    if 'school' in lower and not any(k in lower for k in ('tomorrow', 'tonight', 'today')):
        return 'mon,tue,wed,thu,fri'
    return None


def _extract_alarm_purpose(text):
    """Extract an optional alarm reason like ('to', 'get ready for school') or ('for', 'school'), skipping clock times."""
    cleaned = _TRAILING_TIME.sub('', text.strip().rstrip('.!?')).strip()
    m = re.search(
        r'\b(to|for)\s+(get ready for school|school|work|class|the gym|my flight|flight|practice|[a-z][a-z\s\'-]{2,40})$',
        cleaned, re.I,
    )
    if not m:
        return '', ''
    prep, phrase = m[1].lower(), _TRAILING_TIME.sub('', m[2].strip()).strip()
    if not phrase or re.match(r'^(?:\d|a\.?m\.?|p\.?m\.?|tomorrow|today|tonight|weekdays?|weekends?|daily|every\b)', phrase, re.I):
        return '', ''
    return prep, phrase


def schedule_matches_day(days_str, dt):
    days_str = (days_str or 'daily').strip().lower()
    if days_str == 'daily':
        return True
    if days_str == 'weekdays':
        return dt.weekday() < 5
    if days_str == 'weekends':
        return dt.weekday() >= 5
    if days_str.startswith('once:'):
        return days_str[5:] == dt.strftime('%Y-%m-%d')
    today = memory.WEEKDAYS[dt.weekday()]
    return today in {part.strip()[:3] for part in days_str.split(',') if part.strip()}


def parse_wake_time(text, now_dt=None):
    """Parse a natural wake-up target time ('at 7:30', 'in 45 minutes', 'for a couple hours') -> Unix timestamp or None."""
    now_dt = now_dt or datetime.now().astimezone()
    if match := DURATION.search(text):
        raw_num, unit = match[1], match[2].lower()
        if raw_num:
            amount = float(raw_num)
        elif 'couple' in match[0].lower():
            amount = 2.0
        elif 'half' in match[0].lower():
            amount = 0.5
        else:
            amount = 1.0
        seconds = amount * (3600 if unit.startswith('h') else 60 if unit.startswith('m') else 1)
        return (now_dt + timedelta(seconds=seconds)).timestamp()
    if match := TIME_AT.search(text):
        hour = int(match[1])
        minute = int(match[2] or 0)
        ampm = (match[3] or '').replace('.', '').lower()
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        if ampm == 'pm' and hour < 12:
            hour += 12
        elif ampm == 'am' and hour == 12:
            hour = 0
        target = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if not ampm and hour <= 12 and target <= now_dt:
            # Prefer the next upcoming matching clock time (e.g., "at 7" at 3 PM -> 7 PM).
            alt = target + timedelta(hours=12)
            target = alt if alt > now_dt else target + timedelta(days=1)
        elif target <= now_dt:
            target += timedelta(days=1)
        return target.timestamp()
    return None


def fmt_clock(ts):
    return datetime.fromtimestamp(ts).strftime('%-I:%M %p')


class Life:
    def __init__(self):
        self.lock = threading.RLock()
        # Continuous Pixar emotional state: drives posture, breathing, spring stiffness, and WALL-E sounds.
        self.valence = 0.25       # -1 (sad) .. +1 (joyful)
        self.arousal = 0.35       #  0 (sleepy) .. 1 (excited)
        self.loneliness = 0.0     #  0 (connected) .. 1 (neglected)
        self.annoyance = 0.0      #  0 (chill) .. 1 (playfully indignant)
        self.last_chat = time.monotonic()
        self.present_quiet_s = 0.0
        self.pose_override = None  # (mode, until)
        self.last_lonely_cue = 0.0

        # Nap / Sleep companion & Smart Alarm state
        self.owner_name = 'Brandon'
        self.napping = False
        self.auto_napping = False
        self.nap_owner = None
        self.nap_started_at = 0.0
        self.nap_expected_wake = None
        self.alarm_at = None
        self.alarm_owner = None
        self.alarm_title = ''
        self.alarm_ringing = False
        self.alarm_started_ringing = 0.0
        self.alarm_last_nudge = 0.0
        self.alarm_backup_contact = None
        self.alarm_escalated = False

        # Multi-person arrivals & pending introduction context
        self.greeted_names = {}    # name -> monotonic timestamp
        self.greeted_tracks = {}   # track_id -> monotonic timestamp
        self.awaiting_intro = None # (track_id, expires_monotonic)

        # Multimodal observation, auto-sleep detection & notification watcher state
        self.last_scene_check = 0.0
        self.last_wake_check = 0.0
        self.last_checkin_at = 0.0
        self.last_emotion_seen = None  # (emotion, timestamp)
        self.sleep_sightings = 0
        self.last_observation = None
        self.started_at = time.time()
        self.notif_cursor = self.started_at
        self.seen_notifs = {}          # key -> timestamp (ordered oldest-first for clean eviction)
        self.last_notif_poll = 0.0
        self.pending_highlights = []   # excited items/reminders queued while owner sleeps

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def primary_owner(self):
        from .people import people
        names = sorted(people.names('face') | people.names('voice'))
        if 'Brandon' in names:
            return 'Brandon'
        return names[0] if names else self.owner_name

    def mood_vector(self):
        with self.lock:
            return {
                'valence': round(self.valence, 3),
                'arousal': round(self.arousal, 3),
                'loneliness': round(self.loneliness, 3),
                'annoyance': round(self.annoyance, 3),
            }

    def set_pose(self, mode, seconds):
        with self.lock:
            self.pose_override = (mode, time.monotonic() + seconds)

    def active_pose(self, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            if self.alarm_ringing:
                return 'alarm'
            if self.pose_override and now < self.pose_override[1]:
                return self.pose_override[0]
            self.pose_override = None
            return None

    def on_interaction(self, text='', speaker=None, *, annoyed=False, happy=False):
        """Update emotional state & wake status whenever someone talks to BITCH."""
        now = time.monotonic()
        with self.lock:
            self.last_chat = now
            self.present_quiet_s = 0.0
            self.sleep_sightings = 0
            self.loneliness = max(0.0, self.loneliness - 0.65)
            self.valence = min(1.0, self.valence + (0.28 if happy else 0.14))
            self.arousal = min(1.0, max(self.arousal, 0.45) + (0.18 if happy else 0.08))
            if annoyed:
                self.annoyance = min(1.0, self.annoyance + 0.55)
                self.set_pose('grumpy', 2.2)
            else:
                self.annoyance = max(0.0, self.annoyance - 0.25)
            if speaker:
                memory.upsert_person(speaker, talked=True, seen=True)
                self.greeted_names[speaker] = now

    @staticmethod
    def is_alarm_reply(text):
        stripped = (text or '').strip()
        return bool(
            WAKE_CMD.fullmatch(stripped)
            or SNOOZE_CMD.search(stripped)
            or re.search(r"^(?:i(?:'|’)m up|stop|okay|ok|awake|got it|turn it off)[.!? ]*$", stripped, re.I)
        )

    def handle_utterance(self, text, source, speaker=None):
        """Handle sleep/nap companion commands, alarms, wake-ups, and snoozing. Returns a reply or None."""
        who = speaker or self.primary_owner()
        if self.alarm_ringing and (WAKE_CMD.fullmatch(text.strip()) or re.search(r"\b(?:i(?:'|’)m up|stop|okay|ok|awake|got it|turn it off)\b", text, re.I)):
            return self.dismiss_alarm(source, who)
        if snooze := SNOOZE_CMD.search(text):
            if self.alarm_ringing or self.alarm_at:
                mins = int(snooze[1]) if snooze[1] else 10
                return self.snooze_alarm(mins, who)
        if self.napping and WAKE_CMD.fullmatch(text.strip()):
            return self.wake_from_nap(source, who)
        if WAKE_CMD.fullmatch(text.strip()) and rhythm.state == 'asleep':
            rhythm.wake_now(celebrate=False)
            self.on_interaction(text, who, happy=True)
            return f'Morning, {who}! I’m up.' if datetime.now().hour < 12 else f'Hey {who}, I’m awake.'

        if LIST_SCHED_CMD.search(text):
            items = memory.list_schedules(who)
            lower_q = text.lower()
            wants_rem = 'reminder' in lower_q
            wants_alarm = 'alarm' in lower_q or 'wake' in lower_q
            if wants_rem and not wants_alarm:
                items = [it for it in items if it['kind'] == 'reminder']
            elif wants_alarm and not wants_rem:
                items = [it for it in items if it['kind'] == 'wakeup']
            if not items:
                return 'You don’t have any saved alarms or reminders with me yet.'
            parts = []
            for it in items[:8]:
                h12 = (it['hour'] % 12) or 12
                ampm = 'AM' if it['hour'] < 12 else 'PM'
                parts.append(f"{it['title']} at {h12}:{it['minute']:02d} {ampm} ({it['days']})")
            return 'Here’s what I have: ' + '; '.join(parts) + '.'

        if cancel := CANCEL_SCHED_CMD.search(text):
            query = (cancel[2] or cancel[1] or 'alarm').strip()
            removed = memory.remove_schedule(who, query)
            if self.alarm_at and ('alarm' in text.lower() or 'wake' in text.lower()):
                with self.lock:
                    self.alarm_at = None
                    self.alarm_ringing = False
                removed = max(removed, 1)
            return 'Done, canceled.' if removed else 'I couldn’t find a matching alarm or reminder to cancel.'

        if remind := REMIND_CMD.search(text):
            target_ts = parse_wake_time(text)
            if target_ts:
                prep = (remind[1] or remind[3] or 'to').lower()
                raw_title = (remind[2] or remind[4] or 'your reminder').strip()
                title = _TRAILING_TIME.sub('', raw_title).strip() or 'your reminder'
                dt = datetime.fromtimestamp(target_ts)
                days = parse_days(text) or ('once:' + dt.strftime('%Y-%m-%d'))
                memory.add_schedule(who, title, dt.hour, dt.minute, days=days, kind='reminder')
                when_str = fmt_clock(target_ts)
                recur = '' if days.startswith('once:') else f' ({days})'
                return f'Got it — I’ll remind you {prep} {title} at {when_str}{recur}.'

        wants_nap = bool(NAP_CMD.search(text))
        wants_alarm = bool(ALARM_CMD.search(text))
        if not (wants_nap or wants_alarm):
            return None

        target_ts = parse_wake_time(text)
        backup = BACKUP_CONTACT.search(text)
        backup_name = backup[1].strip() if backup else None
        if wants_nap:
            return self.start_nap(who, wake_at=target_ts, backup_contact=backup_name)
        if wants_alarm:
            if not target_ts:
                return 'Sure — what time should I wake you up?'
            days = parse_days(text)
            prep, purpose = _extract_alarm_purpose(text)
            if days:
                dt = datetime.fromtimestamp(target_ts)
                title = purpose or 'wake up'
                memory.add_schedule(who, title, dt.hour, dt.minute, days=days, kind='wakeup')
                label = 'weekdays' if days == 'mon,tue,wed,thu,fri' else days
                reason_str = f' {prep} {purpose}' if purpose else ''
                return f'Saved! I’ll wake you at {fmt_clock(target_ts)} on {label}{reason_str}.'
            return self.set_alarm(who, target_ts, backup_contact=backup_name, title=purpose, prep=prep)
        return None

    def start_nap(self, owner, *, wake_at=None, backup_contact=None):
        now = time.time()
        with self.lock:
            self.napping = True
            self.nap_owner = owner
            self.nap_started_at = now
            self.nap_expected_wake = wake_at
            self.alarm_at = wake_at
            self.alarm_owner = owner if wake_at else None
            self.alarm_ringing = False
            self.alarm_escalated = False
            if backup_contact:
                self.alarm_backup_contact = backup_contact
            self.arousal = 0.1
            self.set_pose('yawn', 2.4)
        status_text = (f'{owner} is taking a nap right now and should be up around {fmt_clock(wake_at)}.'
                       if wake_at else f'{owner} is taking a nap right now.')
        ttl = max(3600, (wake_at - now) + 1800) if wake_at else 4 * 3600
        memory.set_status(owner, status_text, ttl_seconds=ttl, tag='nap')
        # Give BITCH a couple seconds to finish speaking its goodnight, then enter zero-power sleep.
        threading.Timer(4.0, lambda: rhythm.sleep_now(until=wake_at)).start()
        if wake_at:
            extra = f' If you don’t get up, I’ll flag {backup_contact}.' if backup_contact else ''
            return f'Got it, {owner}. I’ll power down and nap with you, then wake you at {fmt_clock(wake_at)}.{extra}'
        return f'Sleep well, {owner}. I’ll rest my motors until you’re back up.'

    def enter_auto_sleep(self, owner):
        """Quietly enter sleep-companion mode when vision confirms the owner fell asleep on their own."""
        now = time.time()
        with self.lock:
            if self.napping:
                return
            self.napping = True
            self.auto_napping = True
            self.nap_owner = owner
            self.nap_started_at = now
            self.arousal = 0.08
            self.sleep_sightings = 0
        memory.set_status(owner, f'{owner} appears to have fallen asleep around {fmt_clock(now)}.',
                          ttl_seconds=8 * 3600, tag='nap')
        rhythm.sleep_now()

    def set_alarm(self, owner, wake_at, *, backup_contact=None, title='', prep='for'):
        with self.lock:
            self.alarm_at = wake_at
            self.alarm_owner = owner
            self.alarm_title = title
            self.alarm_ringing = False
            self.alarm_escalated = False
            if backup_contact:
                self.alarm_backup_contact = backup_contact
        extra = f' If you sleep through it, I’ll ask to reach {backup_contact}.' if backup_contact else ''
        for_note = f' {prep or "for"} {title}' if title else ''
        return f'Alarm set for {fmt_clock(wake_at)}{for_note}, {owner}.{extra}'

    def snooze_alarm(self, minutes, owner):
        target = time.time() + max(1, min(120, minutes)) * 60
        with self.lock:
            self.alarm_ringing = False
            self.alarm_escalated = False
            self.alarm_at = target
            self.alarm_owner = owner
        rhythm.sleep_now(until=target)
        return f'Snoozed for {minutes} minutes. Back at {fmt_clock(target)}.'

    def dismiss_alarm(self, source, owner):
        was_nap = False
        with self.lock:
            self.alarm_ringing = False
            self.alarm_at = None
            self.alarm_escalated = False
            was_nap = self.napping
        if was_nap:
            return self.wake_from_nap(source, owner)
        rhythm.wake_now(celebrate=True)
        self.set_pose('victory', 2.4)
        return f'Alarm off! Good to see you up, {owner}.'

    def wake_from_nap(self, source, owner=None, *, trigger_briefing=True):
        with self.lock:
            who = owner or self.nap_owner or self.primary_owner()
            since = self.nap_started_at or (time.time() - 3600)
            self.napping = False
            self.auto_napping = False
            self.nap_owner = None
            self.nap_expected_wake = None
            self.alarm_ringing = False
            self.alarm_at = None
            self.alarm_title = ''
            self.alarm_escalated = False
            self.sleep_sightings = 0
            highlights = list(self.pending_highlights)
            self.pending_highlights.clear()
            self.valence = min(1.0, self.valence + 0.5)
            self.arousal = 0.78
            self.loneliness = 0.0
            self.set_pose('victory', 2.6)
        memory.clear_status(who, tag='nap')
        rhythm.wake_now(celebrate=True)

        missed = notifications.summary(since=since, limit=5)
        highlight_note = (' ' + ' '.join(highlights[:2])) if highlights else ''
        robot = hub.get('robot')
        if trigger_briefing and robot and getattr(robot, 'handoff', False) and hub.brain:
            prompt = (f'Give {who} a short, cheerful wake-up briefing of anything important they missed '
                      f'since {fmt_clock(since)} (urgent emails, upcoming calendar events, weather).')
            if missed:
                prompt += f'\nNotifications that arrived while they slept: {missed}'
            helper.start(hub.brain, source, who, prompt, (), user_text='Wake-up briefing')
            if highlights:
                return f'Welcome back, {who}!{highlight_note} I’m pulling together the rest of your briefing too.'
            if missed:
                return f'Welcome back, {who}! You got a few notifications while you were out — pulling together your full wake-up briefing now.'
            return f'Welcome back, {who}! Let me pull together what you missed while you were asleep.'
        if highlights:
            return f'Welcome back, {who}!{highlight_note}'
        if missed:
            return f'Welcome back, {who}! While you were asleep: {missed}'
        return f'Welcome back, {who}! Hope that was a good rest.'

    def state(self):
        with self.lock:
            return {
                'mood': self.mood_vector(),
                'napping': self.napping,
                'alarm_at': self.alarm_at,
                'alarm_ringing': self.alarm_ringing,
                'sleeping': rhythm.state == 'asleep',
                'notifications': notifications.status(),
            }

    def _loop(self):
        last_tick = time.monotonic()
        while True:
            time.sleep(2.0)
            now_mono = time.monotonic()
            dt = min(10.0, now_mono - last_tick)
            last_tick = now_mono
            try:
                self._tick_mood(now_mono, dt)
                self._tick_schedule(now_mono)
                self._tick_alarm(now_mono)
                self._tick_notifications(now_mono)
                self._tick_arrivals(now_mono)
                self._tick_observations(now_mono)
            except Exception as error:
                hub.note('life', f'Life loop note: {error}')

    def _tick_mood(self, now, dt):
        sight = hub.sight
        view = sight.in_view() if sight else None
        someone = bool(view and (view['names'] or view['others']))
        with self.lock:
            # Gentle baseline decay for annoyance and high arousal.
            self.annoyance = max(0.0, self.annoyance - dt * 0.008)
            self.arousal += (0.35 - self.arousal) * min(1.0, dt * 0.01)
            if self.napping or rhythm.state == 'asleep':
                return
            quiet_for = now - self.last_chat
            if someone and quiet_for > 1200:
                self.present_quiet_s += dt
                # Slowly climbs toward lonely if Brandon sits nearby for an hour without chatting.
                self.loneliness = min(0.92, self.present_quiet_s / 3600.0)
                if self.loneliness > 0.45:
                    self.valence = max(-0.55, self.valence - dt * 0.0006)
                    if now - self.last_lonely_cue > 720 and config.get('check_ins'):
                        self.last_lonely_cue = now
                        self.set_pose('lonely', 3.4)
            elif not someone:
                self.valence += (0.15 - self.valence) * min(1.0, dt * 0.002)

    def _tick_alarm(self, now_mono):
        now_wall = time.time()
        brain = hub.brain
        ears = hub.selected('ears')
        source = ears.id if ears else 'app'
        with self.lock:
            if self.alarm_at and not self.alarm_ringing and now_wall >= self.alarm_at:
                self.alarm_ringing = True
                self.alarm_started_ringing = now_mono
                self.alarm_last_nudge = 0.0
                self.alarm_escalated = False
                rhythm.wake_now(celebrate=False)
            ringing = self.alarm_ringing
            who = self.alarm_owner or self.primary_owner()
            since_start = now_mono - self.alarm_started_ringing
            due_nudge = ringing and (now_mono - self.alarm_last_nudge >= 22.0)
            should_escalate = ringing and not self.alarm_escalated and since_start >= 150.0 and self.alarm_backup_contact
        if due_nudge and brain and not brain.busy.is_set():
            with self.lock:
                self.alarm_last_nudge = now_mono
                purpose = self.alarm_title
            for_part = f' — time to {purpose}' if purpose and purpose.lower() != 'wake up' else ''
            line = (f'Rise and shine, {who}{for_part}! Say "I’m up" to turn off the alarm.'
                    if since_start < 40 else f'{who}! Seriously, time to get up{for_part}!')
            brain.announce(source, line, heard=False, chime=True)
        if should_escalate and brain:
            with self.lock:
                self.alarm_escalated = True
                contact = self.alarm_backup_contact
            robot = hub.get('robot')
            if robot and getattr(robot, 'handoff', False):
                helper.start(
                    brain, source, who,
                    f'{who} set an alarm and hasn’t woken up yet. Prepare a brief message to {contact} asking them to help wake {who} up.',
                    (), user_text=f'Wake-up backup for {contact}',
                )
            brain.announce(source, f'{who}, you still haven’t gotten up — I’m prepping a wake-up message for {contact}.', chime=True)

    def _tick_schedule(self, now_mono, now_dt=None):
        """Fire recurring wake-ups (like 8:00 AM weekdays for school) and scheduled reminders on time."""
        dt = now_dt or datetime.now().astimezone()
        now_wall = dt.timestamp()
        current_min = dt.hour * 60 + dt.minute
        brain = hub.brain
        ears = hub.selected('ears')
        source = ears.id if ears else 'app'
        for item in memory.list_schedules():
            if not schedule_matches_day(item['days'], dt):
                continue
            fire_min = item['hour'] * 60 + item['minute'] - int(item['lead_minutes'] or 0)
            if not (0 <= current_min - fire_min <= 2):
                continue
            if now_wall - float(item['last_fired'] or 0) < 12 * 3600:
                continue
            memory.mark_schedule_fired(item['id'], when=now_wall, remove_if_once=True)
            who = item['owner'] or self.primary_owner()
            title = item['title']
            if item['kind'] == 'wakeup':
                with self.lock:
                    self.alarm_at = now_wall
                    self.alarm_owner = who
                    self.alarm_title = title
                    self.alarm_ringing = True
                    self.alarm_started_ringing = now_mono
                    self.alarm_last_nudge = 0.0
                    self.alarm_escalated = False
                    rhythm.wake_now(celebrate=False)
                continue
            lead = int(item['lead_minutes'] or 0)
            msg = (f'Heads up, {who} — {title} starts in {lead} minutes.'
                   if lead > 0 else f'Quick reminder, {who}: {title}.')
            if self.napping or rhythm.state == 'asleep':
                with self.lock:
                    self.pending_highlights.append(msg)
            elif brain:
                self.set_pose('notice', 1.8)
                brain.announce(source, msg, chime=True)

    def _tick_notifications(self, now_mono, *, force=False):
        """Watch incoming Mac/phone notifications and react excitedly when one matches your memories (e.g. Amazon package!)."""
        if not force and now_mono - self.last_notif_poll < 8.0:
            return
        self.last_notif_poll = now_mono
        items = notifications.recent(since=max(self.started_at, self.notif_cursor - 5.0), limit=10)
        if not items:
            return
        owner = self.primary_owner()
        brain = hub.brain
        ears = hub.selected('ears')
        source = ears.id if ears else 'app'
        newest_ts = self.notif_cursor
        for item in reversed(items):
            key = (item['app'], item['title'], item['body'], int(item['at']))
            if key in self.seen_notifs:
                continue
            self.seen_notifs[key] = item['at']
            while len(self.seen_notifs) > 200:
                self.seen_notifs.pop(next(iter(self.seen_notifs)))
            newest_ts = max(newest_ts, item['at'])
            notif_text = f"{item['app']}: {item['title']} {item['body']}".strip()
            hit = memory.notification_match(owner, notif_text)
            is_delivery = bool(DELIVERY_SIGNAL.search(notif_text))
            if not (hit or is_delivery):
                continue
            if hit:
                memory.mark_used(hit['id'])
            # Compose a natural, excited reaction referencing what Brandon talked about.
            detail = item['body'] or item['title']
            if hit:
                try:
                    line = llm.chat([
                        {'role': 'system', 'content': (
                            f'You are BITCH, {owner}’s expressive Pixar-like desk robot. '
                            f'A notification just arrived ({notif_text}) and it matches something {owner} talked about earlier: '
                            f'"{hit["text"]}". Write ONE excited, natural spoken reaction (1–2 crisp sentences, under 35 words). '
                            'Focus ONLY on the notification and the matching item/event — ignore unrelated details in the memory. '
                            'No stage directions, no emoji, no ChatGPT mentions.'
                        )},
                        {'role': 'user', 'content': 'React aloud.'},
                    ], max_tokens=70, temperature=0.5)
                    line = streaming.clean(line)
                    if not (10 <= len(line) <= 280):
                        line = ''
                except Exception:
                    line = ''
                if not line:
                    line = f'Ooh, {owner}! You just got a {item["app"]} notification: {detail}!'
            else:
                line = f'Hey {owner}, exciting news from {item["app"]} — {detail}!'

            if self.napping or rhythm.state == 'asleep':
                with self.lock:
                    self.pending_highlights.append(line)
            elif brain and (force or self._can_speak_proactively()):
                with self.lock:
                    self.valence = min(1.0, self.valence + 0.50)
                    self.arousal = 0.90
                    self.loneliness = max(0.0, self.loneliness - 0.35)
                self.set_pose('victory', 2.6)
                brain.announce(source, line, chime=True)
        self.notif_cursor = newest_ts

    def _can_speak_proactively(self):
        brain, hearing = hub.brain, hub.hearing
        if not (config.get('check_ins') and not config.get('muted') and config.get('volume')
                and hub.selected('voice') and brain and hearing and hearing.ready):
            return False
        if brain.busy.is_set() or brain.followup_seconds() or hearing.capture or time.monotonic() - hearing.activity_at < 4:
            return False
        return bool(hub.selected('ears'))

    def _tick_arrivals(self, now):
        sight = hub.sight
        if not sight or not hasattr(sight, 'people_snapshot'):
            return
        snapshot = sight.people_snapshot()
        if not snapshot:
            return
        people_list = snapshot['people']
        if not people_list:
            return
        for entry in people_list:
            if entry['name']:
                memory.upsert_person(entry['name'], seen=True)

        # Wake from nap if the napping owner returns & looks awake after >= 3 minutes.
        with self.lock:
            napping = self.napping
            auto_nap = self.auto_napping
            nap_owner = self.nap_owner or self.primary_owner()
            nap_age = time.time() - self.nap_started_at if napping else 0.0

        ears = hub.selected('ears')
        source = ears.id if ears else 'app'
        if napping and nap_age >= 180 and any(p['name'] == nap_owner and p['face_fresh'] for p in people_list):
            still_sleeping = False
            if snapshot.get('image') is not None and (auto_nap or now - self.last_wake_check >= 45.0):
                if auto_nap and now - self.last_wake_check < 45.0:
                    still_sleeping = True
                else:
                    self.last_wake_check = now
                    try:
                        obs = gaze.observe_scene(snapshot['image'])
                        still_sleeping = obs.get('emotion') == 'sleeping'
                    except Exception:
                        still_sleeping = auto_nap
            if not still_sleeping and hub.brain and not hub.brain.busy.is_set():
                reply = self.wake_from_nap(source, nap_owner)
                hub.brain.announce(source, reply, chime=True)
                return

        if not self._can_speak_proactively():
            return

        # Cooldown so BITCH never chatters back-to-back on arrivals.
        if now - self.last_checkin_at < 25:
            return

        # Identify newly arrived people.
        # Be patient before asking an unidentified person "who are you?" so BITCH never jumps the gun
        # on its own owner before getting clear frontal face-recognition looks!
        from .people import people as enrolled_people
        has_enrolled_faces = bool(enrolled_people.names('face'))
        owner_just_here = (
            len(people_list) == 1
            and not napping
            and (now - self.greeted_names.get(self.primary_owner(), -1e9) < 45.0
                 or now - getattr(hub.sight, 'met', {}).get(self.primary_owner(), -1e9) < 45.0)
        )
        known_in_room = any(p['name'] for p in people_list)
        min_unknown_age = 3.5 if (napping or known_in_room) else (20.0 if owner_just_here else 9.0)
        min_face_checks = 3 if (napping or known_in_room) else (10 if owner_just_here else 6)

        new_known = []
        new_unknown = []
        for entry in people_list:
            if entry['age'] < 1.6:
                continue
            name = entry['name']
            if name:
                if now - self.greeted_names.get(name, -1e9) > 1200:
                    new_known.append(entry)
            else:
                if now - self.greeted_tracks.get(entry['id'], -1e9) <= 900:
                    continue
                if entry['age'] < min_unknown_age:
                    continue
                if not entry.get('face_visible', True):
                    continue
                if has_enrolled_faces and entry.get('face_checks', 0) < min_face_checks:
                    continue
                new_unknown.append(entry)

        if not new_known and not new_unknown:
            return

        # Visitor enters while owner is napping!
        if napping:
            visitors = [p for p in new_known if p['name'] != nap_owner] + new_unknown
            if not visitors:
                return
            target = visitors[0]
            self.last_checkin_at = now
            if target['name']:
                self.greeted_names[target['name']] = now
            else:
                self.greeted_tracks[target['id']] = now
            sight.direct(snapshot['camera'], snapshot['received'], target['center'], 'person')
            statuses = memory.active_statuses(nap_owner)
            nap_note = statuses[0][1] if statuses else f'{nap_owner} is taking a nap right now.'
            if target['name']:
                line = f'Hey {target["name"]} — quiet heads up, {nap_note}'
            else:
                with self.lock:
                    self.awaiting_intro = (target['id'], now + 45.0)
                line = f'Hi there! I’m BITCH, {nap_owner}’s desk robot — who are you? By the way, {nap_note}'
            hub.brain.announce(source, line, chime=False)
            return

        # Awake arrival greeting (handles both solo visitors and groups walking in together!).
        if rhythm.state == 'asleep':
            return
        self.last_checkin_at = now
        parts = []
        if new_known:
            for p in new_known:
                self.greeted_names[p['name']] = now
            names = [p['name'] for p in new_known]
            who_str = names[0] if len(names) == 1 else ', '.join(names[:-1]) + ' and ' + names[-1]
            parts.append(f'Hey {who_str}!')
        if new_unknown:
            target = max(new_unknown, key=lambda p: p['box'][2])
            self.greeted_tracks[target['id']] = now
            sight.direct(snapshot['camera'], snapshot['received'], target['center'], 'person')
            with self.lock:
                self.awaiting_intro = (target['id'], now + 45.0)
            cue = ''
            if len(new_unknown) > 1 or len(people_list) > 2:
                try:
                    cue = gaze.describe_person(snapshot['image'], target['box'])
                except Exception:
                    cue = ''
            owner = self.primary_owner()
            if cue:
                parts.append(f'And hi to you {cue} — I don’t think we’ve met yet. What’s your name, and how do you know {owner}?')
            else:
                parts.append(f'I don’t think we’ve met yet — what’s your name, and how do you know {owner}?')
        if parts:
            self.set_pose('happy', 1.8)
            hub.brain.announce(source, ' '.join(parts), chime=False)

    def _tick_observations(self, now):
        """Multimodal scene check-in: auto-sleep detection, celebrating/stressed moods, and visual memory callbacks."""
        if self.napping or rhythm.state == 'asleep':
            return
        if now - self.last_scene_check < 45.0:
            return
        brain, hearing = hub.brain, hub.hearing
        if brain and (brain.busy.is_set() or brain.followup_seconds()):
            return
        if hearing and hearing.capture:
            return
        sight = hub.sight
        if not sight:
            return
        owner = sight.solo_recognized()
        image, count = sight.current_scene()
        if not owner or image is None or count != 1:
            return
        self.last_scene_check = now
        try:
            obs = gaze.observe_scene(image)
        except Exception:
            return
        self.last_observation = (now, owner, obs)
        ears = hub.selected('ears')
        source = ears.id if ears else 'app'

        # 1. Automatic sleep detection runs silently (never blocked by mute or recent check-ins).
        emotion = obs.get('emotion', 'neutral')
        prev = self.last_emotion_seen
        self.last_emotion_seen = (emotion, now)
        if emotion == 'sleeping':
            quiet_speech = not hearing or (not hearing.capture and now - hearing.activity_at >= 60)
            if quiet_speech:
                with self.lock:
                    self.sleep_sightings += 1
                    count_sleep = self.sleep_sightings
                if count_sleep >= 2:
                    self.enter_auto_sleep(owner)
                    return
            return
        with self.lock:
            self.sleep_sightings = 0

        # Proactive spoken observations respect the 5-minute check-in cooldown and audio/mute gates.
        if now - self.last_checkin_at < 300.0 or not self._can_speak_proactively():
            return

        # 2. Cross-modal memory callback (e.g. spotting a blue/white shirt and teasing about last week's debate!).
        scene_desc = ' '.join(part for part in (obs.get('activity'), obs.get('notable')) if part).strip()
        callback = memory.visual_callbacks(owner, scene_desc) if scene_desc else None
        if callback and self._can_speak_proactively():
            try:
                line = llm.chat([
                    {'role': 'system', 'content': (
                        f'You are BITCH, {owner}’s sassy, observant Pixar-like desk robot. '
                        f'Right now your camera sees {owner}: "{scene_desc}". '
                        f'You remember from earlier: "{callback["text"]}". '
                        'Write ONE short, witty, natural spoken line (1–2 crisp sentences, under 35 words) playfully referencing '
                        'what you see and that memory. No stage directions, no emoji, no ChatGPT mentions.'
                    )},
                    {'role': 'user', 'content': 'Say your comment aloud.'},
                ], max_tokens=70, temperature=0.55)
                line = streaming.clean(line)
                if 10 <= len(line) <= 280:
                    memory.mark_used(callback['id'])
                    self.last_checkin_at = now
                    self.set_pose('curious', 2.2)
                    hub.brain.announce(source, line, chime=False)
                    return
            except Exception:
                pass

        # 3. Emotional check-ins (celebrating or stressed — require confirmation so we don't misfire!).
        if emotion == 'celebrating':
            time.sleep(1.5)
            image2, count2 = sight.current_scene()
            if image2 and count2 == 1:
                try:
                    obs2 = gaze.observe_scene(image2)
                except Exception:
                    obs2 = {}
                if obs2.get('emotion') in ('celebrating', 'happy') and self._can_speak_proactively():
                    self.last_checkin_at = now
                    with self.lock:
                        self.valence = min(1.0, self.valence + 0.45)
                        self.arousal = 0.85
                    self.set_pose('victory', 2.2)
                    hub.brain.announce(source, f'Whoa, what just happened, {owner}? Tell me what we’re celebrating!', chime=False)
                    return
        elif emotion == 'stressed' and prev and prev[0] == 'stressed' and now - prev[1] < 120:
            if self._can_speak_proactively():
                self.last_checkin_at = now
                with self.lock:
                    self.valence = -0.15
                    self.arousal = 0.25
                self.set_pose('sad', 2.4)
                hub.brain.announce(source, f'Hey {owner}, you look a little stressed. Want to talk it out or take a quick breather?', chime=False)


life = Life()
