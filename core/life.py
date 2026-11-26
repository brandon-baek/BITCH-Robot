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
        self.nap_owner = None
        self.nap_started_at = 0.0
        self.nap_expected_wake = None
        self.alarm_at = None
        self.alarm_owner = None
        self.alarm_ringing = False
        self.alarm_started_ringing = 0.0
        self.alarm_last_nudge = 0.0
        self.alarm_backup_contact = None
        self.alarm_escalated = False

        # Multi-person arrivals & pending introduction context
        self.greeted_names = {}    # name -> monotonic timestamp
        self.greeted_tracks = {}   # track_id -> monotonic timestamp
        self.awaiting_intro = None # (track_id, expires_monotonic)

        # Multimodal observation state
        self.last_scene_check = 0.0
        self.last_checkin_at = 0.0
        self.last_emotion_seen = None  # (emotion, timestamp)
        self.last_observation = None

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

    def handle_utterance(self, text, source, speaker=None):
        """Handle sleep/nap companion commands, alarms, wake-ups, and snoozing. Returns a reply or None."""
        who = speaker or self.primary_owner()
        if self.alarm_ringing and (WAKE_CMD.fullmatch(text.strip()) or re.search(r"\b(?:i(?:'|’)m up|stop|okay|ok|awake|got it)\b", text, re.I)):
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
            return self.set_alarm(who, target_ts, backup_contact=backup_name)
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

    def set_alarm(self, owner, wake_at, *, backup_contact=None):
        with self.lock:
            self.alarm_at = wake_at
            self.alarm_owner = owner
            self.alarm_ringing = False
            self.alarm_escalated = False
            if backup_contact:
                self.alarm_backup_contact = backup_contact
        extra = f' If you sleep through it, I’ll ask to reach {backup_contact}.' if backup_contact else ''
        return f'Alarm set for {fmt_clock(wake_at)}, {owner}.{extra}'

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
            self.nap_owner = None
            self.nap_expected_wake = None
            self.alarm_ringing = False
            self.alarm_at = None
            self.alarm_escalated = False
            self.valence = min(1.0, self.valence + 0.5)
            self.arousal = 0.78
            self.loneliness = 0.0
            self.set_pose('victory', 2.6)
        memory.clear_status(who, tag='nap')
        rhythm.wake_now(celebrate=True)

        missed = notifications.summary(since=since, limit=5)
        robot = hub.get('robot')
        if trigger_briefing and robot and getattr(robot, 'handoff', False) and hub.brain:
            prompt = (f'Give {who} a short, cheerful wake-up briefing of anything important they missed '
                      f'since {fmt_clock(since)} (urgent emails, upcoming calendar events, weather).')
            if missed:
                prompt += f'\nNotifications that arrived while they slept: {missed}'
            helper.start(hub.brain, source, who, prompt, (), user_text='Wake-up briefing')
            if missed:
                return f'Welcome back, {who}! You got a few notifications while you were out — pulling together your full wake-up briefing now.'
            return f'Welcome back, {who}! Let me pull together what you missed while you were asleep.'
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
                self._tick_alarm(now_mono)
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
            line = (f'Hey {who}, time to get up! Say "I’m up" to turn off the alarm.'
                    if since_start < 40 else f'{who}! Seriously, time to wake up!')
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

        # Wake from nap if the napping owner returns & looks at BITCH after >= 3 minutes.
        with self.lock:
            napping = self.napping
            nap_owner = self.nap_owner or self.primary_owner()
            nap_age = time.time() - self.nap_started_at if napping else 0.0

        ears = hub.selected('ears')
        source = ears.id if ears else 'app'
        if napping and nap_age >= 180 and any(p['name'] == nap_owner and p['face_fresh'] for p in people_list):
            if hub.brain and not hub.brain.busy.is_set():
                reply = self.wake_from_nap(source, nap_owner)
                hub.brain.announce(source, reply, chime=True)
                return

        if not self._can_speak_proactively():
            return

        # Cooldown so BITCH never chatters back-to-back on arrivals.
        if now - self.last_checkin_at < 25:
            return

        # Identify newly arrived people (stable in view >= 1.6s, not greeted in the last 20 minutes).
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
                if now - self.greeted_tracks.get(entry['id'], -1e9) > 900 and entry['age'] >= 2.4:
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
        """Multimodal scene check-in: spot celebrating/stressed moods and visual memory callbacks."""
        if self.napping or rhythm.state == 'asleep':
            return
        if now - self.last_scene_check < 45.0 or now - self.last_checkin_at < 300.0:
            return
        if not self._can_speak_proactively():
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

        # 1. Cross-modal memory callback (e.g. spotting a blue/white shirt and teasing about last week's debate!).
        scene_desc = ' '.join(part for part in (obs.get('activity'), obs.get('notable')) if part).strip()
        callback = memory.visual_callbacks(owner, scene_desc) if scene_desc else None
        if callback and self._can_speak_proactively():
            try:
                line = llm.chat([
                    {'role': 'system', 'content': (
                        f'You are BITCH, {owner}’s sassy, observant Pixar-like desk robot. '
                        f'Right now your camera sees {owner}: "{scene_desc}". '
                        f'You remember from earlier: "{callback["text"]}". '
                        'Write ONE short, witty, natural spoken line (at most 2 sentences) playfully referencing '
                        'what you see and that memory. No stage directions, no emoji, no ChatGPT mentions.'
                    )},
                    {'role': 'user', 'content': 'Say your comment aloud.'},
                ], max_tokens=70, temperature=0.55)
                line = streaming.clean(line)
                if 10 <= len(line) <= 220:
                    memory.mark_used(callback['id'])
                    self.last_checkin_at = now
                    self.set_pose('curious', 2.2)
                    hub.brain.announce(source, line, chime=False)
                    return
            except Exception:
                pass

        # 2. Emotional check-ins (celebrating or stressed — require confirmation so we don't misfire!).
        emotion = obs.get('emotion', 'neutral')
        prev = self.last_emotion_seen
        self.last_emotion_seen = (emotion, now)
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
