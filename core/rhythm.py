"""How awake BITCH is.

A Pixar character saves its big moments for when someone is watching. BITCH does the same, which is
also what lets the motors run for years on a desk that is never switched off:

- engaged:   talking, just spoken to, or someone just walked in. Fully animated.
- attentive: someone is nearby but busy. BITCH watches calmly and glances now and then.
- resting:   no one around, or a long quiet stretch. Parked with the motors unpowered,
             with a rare look up if someone is still there.
- asleep:    at night. It nods off and stays parked until someone says its name.

Unprompted motion has an hourly budget; past it, BITCH rests until the hour turns over.
"""
from collections import deque
import random
import time
from datetime import datetime

from . import config

ENGAGED_FOR = 90          # seconds of full animation after the last exchange
ARRIVAL_FOR = 20          # seconds of full attention when someone shows up
ATTENTIVE_FOR = 600       # calm watching of someone busy nearby, before resting
LOOK_AROUND_FOR = 25      # after someone leaves, how long BITCH looks for them
GLANCE_EVERY = (900, 1800)
GLANCE_FOR = 6
NIGHT_GRACE = 300         # at night, nod off this long after the last exchange
IDLE_BUDGET = 360         # seconds of unprompted motion allowed per hour
DOZE_FOR = 5.4            # the nodding-off beat
STRETCH_FOR = 2.8


class Rhythm:
    def __init__(self):
        now = time.monotonic()
        self.state = 'resting'
        self.since = now
        self.someone = False
        self.seen_at = -1e9
        self.arrived_at = -1e9
        self.glance_at = now + random.uniform(*GLANCE_EVERY)
        self.glancing_until = 0.
        self.beat = None          # (name, until): a transition beat such as nodding off
        self.spent = deque()      # (time, seconds) of unprompted motion in the last hour
        self.last = now
        self.on_sleep = None      # called when BITCH nods off with someone watching
        self.manual_sleep = False
        self.sleep_until = None

    def sleep_now(self, until=None, now=None):
        """Put BITCH into quiet zero-power sleep/nap mode until woken or alarm hits."""
        now = time.monotonic() if now is None else now
        self.manual_sleep = True
        self.sleep_until = until
        if self.state != 'asleep':
            self.enter('asleep', now)

    def wake_now(self, *, celebrate=False, now=None):
        """Wake BITCH up immediately from night sleep or nap companion mode."""
        now = time.monotonic() if now is None else now
        was_asleep = self.state == 'asleep' or self.manual_sleep
        self.manual_sleep = False
        self.sleep_until = None
        self.state = 'engaged'
        self.since = now
        if celebrate:
            self.beat = ('victory', now + 2.4)
        elif was_asleep:
            self.beat = ('stretch', now + STRETCH_FOR)

    def night(self):
        if not config.get('sleep_at_night'):
            return False
        start, end, hour = config.get('quiet_start'), config.get('quiet_end'), datetime.now().hour
        return start <= hour or hour < end if start > end else start <= hour < end

    def over_budget(self, now):
        while self.spent and now - self.spent[0][0] > 3600:
            self.spent.popleft()
        return sum(seconds for _, seconds in self.spent) >= IDLE_BUDGET

    def update(self, someone, engaged, last_interaction, now=None):
        """Return (state, beat). beat is a one-off motion to play first, like 'doze' or 'stretch'."""
        now = time.monotonic() if now is None else now
        elapsed, self.last = min(1., now - self.last), now
        if engaged and self.manual_sleep:
            self.manual_sleep = False
            self.sleep_until = None
        if self.manual_sleep and self.sleep_until is not None and time.time() >= self.sleep_until:
            self.manual_sleep = False
            self.sleep_until = None
        if someone and not self.someone and now - self.seen_at > 60:
            self.arrived_at = now
        if someone:
            self.seen_at = now
        self.someone = someone
        quiet = now - last_interaction
        if self.manual_sleep:
            state = 'asleep'
        elif engaged or quiet < ENGAGED_FOR or someone and now - self.arrived_at < ARRIVAL_FOR:
            state = 'engaged'
        elif self.night() and quiet > NIGHT_GRACE:
            state = 'asleep'
        elif someone and now - max(last_interaction, self.arrived_at) < ATTENTIVE_FOR and not self.over_budget(now):
            state = 'attentive'
        elif not someone and now - self.seen_at < LOOK_AROUND_FOR:
            state = 'looking'
        else:
            state = 'resting'
        if state == 'resting' and someone and not self.over_budget(now):
            if now >= self.glance_at:
                self.glancing_until = now + GLANCE_FOR
                self.glance_at = now + random.uniform(*GLANCE_EVERY)
            if now < self.glancing_until:
                state = 'glancing'
        if state != self.state:
            self.enter(state, now)
        if state in ('attentive', 'looking', 'glancing'):
            self.spent.append((now, elapsed))
        beat = self.beat[0] if self.beat and now < self.beat[1] else None
        return state, beat

    def enter(self, state, now):
        previous, self.state, self.since = self.state, state, now
        if state == 'asleep' and previous != 'asleep':
            self.beat = ('doze', now + DOZE_FOR)
            if self.someone and self.on_sleep:
                self.on_sleep()
        elif previous == 'asleep' and state in ('engaged', 'attentive') and not self.night():
            self.beat = ('stretch', now + STRETCH_FOR)   # the morning stretch
        else:
            self.beat = None

    def resting_for(self, now=None):
        now = time.monotonic() if now is None else now
        return now - self.since if self.state in ('resting', 'asleep') else 0.


rhythm = Rhythm()
