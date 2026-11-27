"""BITCH's deep sleep, and the watchdog that keeps the Mac from running out of memory.

Awake, BITCH's brain (about 17 GB) and voice (about 2 GB) sit in memory. Deep sleep lets go of both, plus Whisper,
parks the motors, and keeps only the fast ears (Parakeet, about 1.3 GB) listening, so "Hey BITCH" or "wake up"
still works. Ways in:
- asked: "BITCH, take a break for two hours", "go to sleep until 5", "free up some memory", or the app's button
- while you nap with it (life.py's nap companion)
- the watchdog: when macOS says memory is close to running out, BITCH steps aside before anything can crash

Ways out: its name, "wake up", the app, the timer it was given, or a timer or alarm that needs to ring. Waking takes
roughly 10-20 seconds (reading the brain back from disk), so BITCH chirps right away and answers once it's up.
After a memory nap it only wakes on its own once memory has been comfortable for a while.
"""
from datetime import datetime
import re
import subprocess
import threading
import time

from . import config
from .hub import hub

CRITICAL_LEVEL = 8        # % of memory macOS says is free: below this (or at critical pressure), step aside
COMFORTABLE_LEVEL = 35    # % free that counts as "the heavy work is over"
COMFORT_FOR = 120         # seconds of comfort before waking up from a memory nap on its own
CRITICAL_FOR = 15         # seconds of critical memory before going to sleep (one blip isn't a reason)
CHECK_EVERY = 5
HEADROOM = 2.5e9          # bytes to leave for everything else when loading the brain
NUMBERS = {'one': 1, 'a': 1, 'an': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8,
           'nine': 9, 'ten': 10, 'fifteen': 15, 'twenty': 20, 'thirty': 30, 'forty': 40, 'forty-five': 45,
           'forty five': 45, 'fifty': 50, 'sixty': 60, 'ninety': 90, 'a couple': 2, 'a few': 3, 'several': 4}

# Things said to BITCH that mean "go to deep sleep" (not "I'm going to take a nap", which is the nap companion).
SLEEP = re.compile(
    r"^(?:(?:can|could|would|will) you\s+|please\s+|go ahead and\s+|okay[, ]+|ok[, ]+|alright[, ]+|hey[, ]+|so[, ]+)*"
    r"(?:go (?:to sleep|take a (?:break|nap|rest))|take (?:(?:a|some) (?:break|nap|rest|breather)|five|ten)|"
    r"power (?:down|off)|shut (?:down|off)|turn (?:yourself )?off|get some (?:rest|sleep)|go rest|nap|sleep|"
    r"go offline|log off|clock out|free up (?:some )?(?:memory|ram|space|the mac)|offload(?: yourself)?|"
    r"give me (?:the|my|some) (?:ram|memory|mac) back)\b(?P<rest>.*)$", re.I)
HEAVY = re.compile(r"\b(?:i(?:'|’)m|i am) (?:about to|going to|gonna) (?:\w+ ){0,6}?(?:render|game|gaming|play|edit|"
                   r"train|compile|export|stream|record|run something heavy)\b.*\b(?:sleep|break|rest|offload|free up|"
                   r"power down|shut down)\b", re.I)
WAKE = re.compile(r"^(?:(?:hey|okay|ok|yo)[, ]+)?(?:(?:you can |time to |please )?(?:wake up|get up|you(?:'|’)re up)"
                  r"|(?:i(?:'|’)m|i am) (?:up|awake|back)"
                  r"|rise and shine|good morning|come back(?: online)?|back to work)\b", re.I)


def _level():
    """(percent of memory free, pressure 1 normal / 2 warning / 4 critical) as macOS sees it."""
    try:
        out = subprocess.run(['sysctl', '-n', 'kern.memorystatus_level', 'kern.memorystatus_vm_pressure_level'],
                             capture_output=True, text=True, timeout=2).stdout.split()
        return int(out[0]), int(out[1])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return 100, 1


def _memory_bytes():
    try:
        return int(subprocess.run(['sysctl', '-n', 'hw.memsize'], capture_output=True, text=True, timeout=2).stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 32 * 1024 ** 3


def asked_to_sleep(text):
    """Seconds to sleep (0 = until woken) if this is a request for BITCH to go to deep sleep, else None."""
    if not (SLEEP.match(text.strip()) or HEAVY.search(text)):
        return None
    if short := re.search(r'\btake (five|ten)\b', text, re.I):
        return 300. if short[1].lower() == 'five' else 600.
    return duration(text)


def duration(text):
    from .life import parse_wake_time

    spoken = text.casefold()
    for word, number in sorted(NUMBERS.items(), key=lambda item: -len(item[0])):
        spoken = re.sub(rf'\b{word}\b(?= (?:hours?|hrs?|minutes?|mins?|seconds?)\b)', str(number), spoken)
    spoken = re.sub(r'\b(?:until|till|til)\b', 'at', spoken)
    target = parse_wake_time(spoken)
    if not target:
        return 0
    return max(60., min(24 * 3600., target - time.time()))


def say_until(seconds):
    if not seconds:
        return ''
    ends = datetime.fromtimestamp(time.time() + seconds)
    if seconds < 3600:
        return f' for {round(seconds / 60)} minutes'
    return f' until {ends:%-I:%M %p}'.replace(':00 ', ' ')


class Power:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = 'awake'        # awake, sleeping, waking
        self.reason = None          # asked, nap, memory
        self.until = None           # wall-clock time to wake up by itself, if any
        self.since = time.time()
        self.ready = threading.Event()
        self.ready.set()
        self.started = False
        self.failed = False
        self.comfortable_since = None
        self.critical_since = None
        self.last_level = (100, 1)
        self.waking_since = 0.

    def start(self):
        if not self.started:
            self.started = True
            threading.Thread(target=self.watch, daemon=True).start()

    @property
    def sleeping(self):
        return self.state != 'awake'

    def estimate(self):
        """Seconds a wake-up takes, learned from the last few."""
        return float(config.get('wake_seconds') or 15)

    def status(self):
        level, pressure = self.last_level
        eta = max(1, round(self.waking_since + self.estimate() - time.time())) if self.state == 'waking' else None
        return {'state': self.state, 'reason': self.reason, 'until': self.until, 'since': self.since, 'eta': eta,
                'memory_free': level, 'memory_pressure': {1: 'normal', 2: 'warning', 4: 'critical'}.get(pressure, 'normal')}

    # ------------------------------------------------------------ going to sleep

    def sleep(self, seconds=0, reason='asked'):
        """Let go of the brain, voice and Whisper; park; keep listening for the name. seconds=0: until woken."""
        with self.lock:
            if self.state == 'sleeping' and reason != 'asked':
                return
            self.state, self.reason, self.since = 'sleeping', reason, time.time()
            self.until = time.time() + seconds if seconds else None
            self.ready.clear()
            self.comfortable_since = None
        from . import llm, voice
        from .rhythm import rhythm

        llm.stop_server()
        voice.unload()
        if hub.hearing and hasattr(hub.hearing, 'unload_whisper'):
            hub.hearing.unload_whisper()
        rhythm.sleep_now(until=time.monotonic() + seconds if seconds else None)
        hub.status = 'Deep sleep'
        hub.note('power', self.describe())
        print(f'  Power: deep sleep ({reason}){say_until(seconds)}', flush=True)

    def describe(self):
        if not self.sleeping:
            return None
        until = (f' until {datetime.fromtimestamp(self.until):%-I:%M %p}' if self.until else '')
        return {'asked': f'BITCH is in deep sleep{until} to free up memory. Say its name or tap Wake to bring it back.',
                'nap': 'BITCH is napping with you and has freed its memory.',
                'memory': 'Your Mac ran low on memory, so BITCH went to deep sleep. It wakes when memory frees up, '
                          'or when you call it.'}.get(self.reason, 'BITCH is in deep sleep.')

    # ------------------------------------------------------------ waking up

    def room_for_brain(self):
        """Is there room to load the brain without squeezing the Mac?"""
        from . import llm

        # The brain is read from disk on demand (macOS can drop and re-read it), so it doesn't need all 17 GB free
        # up front: refuse only when memory is already tight. If it gets tight later, the watchdog steps in.
        weights, _ = llm.model_files(config.get('chat_model'))
        need = .55 * (weights.stat().st_size if weights else 17e9) + HEADROOM
        level, pressure = _level()
        return pressure < 4 and _memory_bytes() * level / 100 >= need

    def wake(self, reason='asked', wait=0.):
        """Load everything back. Returns True once awake (when wait > 0), False if there isn't room yet."""
        with self.lock:
            if self.state == 'awake':
                return True
            starting = self.state == 'sleeping'
            if starting:
                self.state = 'waking'
        if starting:
            threading.Thread(target=self._load, args=(reason,), daemon=True).start()
        return self.ready.wait(wait) if wait else False

    def _load(self, reason):
        from . import llm, voice
        from .rhythm import rhythm

        if not self.room_for_brain():
            with self.lock:
                self.state = 'sleeping'
            self.failed = True
            hub.note('power', 'Not enough free memory to wake BITCH up yet. Close something heavy and call it again.')
            print('  Power: stayed asleep, not enough free memory to load the brain', flush=True)
            return
        self.failed = False
        self.waking_since = started = time.time()
        hub.status = 'Waking up'
        rhythm.wake_now()   # the stretch plays while the brain loads, so it looks awake straight away
        with self.lock:
            self.state = 'waking'   # start_server refuses while 'sleeping'
        loaded = llm.start_server(wait=240)
        if loaded and hub.brain:
            try:
                # Read the system prompt into the cache now, so the first answer after waking is as quick as any.
                llm.warm_context(hub.brain.spoken_system())
            except (OSError, ValueError, KeyError):
                pass
        voice.warm()
        voice.warmed.wait(90)
        took = time.time() - started
        if loaded:
            # Remember how long it really took (smoothed), for next time's "give me about N seconds".
            config.update({'wake_seconds': round(.6 * self.estimate() + .4 * took, 1)})
        with self.lock:
            self.state, self.reason, self.until, self.since = 'awake', None, None, time.time()
            self.ready.set()
        hub.note('power', None if loaded else 'BITCH woke up, but its brain could not start. Check the model.')
        hub.status = 'Waiting for “BITCH”'
        print(f'  Power: awake ({reason}) in {took:.1f} s', flush=True)

    def wake_with_words(self, reason, stop=None):
        """Wake up the way a person would: a chirp, "give me about N seconds", "almost there" if it drags on.
        Returns True once BITCH can think and talk again."""
        from . import voice

        self.failed = False
        voice.earcon('waking', force=True)
        self.wake(reason)
        estimate = self.estimate()
        if self.ready.wait(1.2):
            return True
        if not self.failed:
            nearest = min(voice.WAKING_SECONDS, key=lambda n: abs(n - estimate))
            voice.say_recorded('waking_short' if estimate <= 8 else 'waking_long' if estimate > 25
                               else f'waking_{nearest}', stop)
        if self.ready.wait(max(1., estimate + 6 - (time.time() - self.waking_since))):
            return True
        if not self.failed and self.state == 'waking':
            voice.say_recorded('almost', stop)
        if self.ready.wait(150):
            return True
        if self.failed:
            voice.say_recorded('cant_wake', stop)
        return False

    # ------------------------------------------------------------ the watchdog

    def watch(self):
        while True:
            time.sleep(CHECK_EVERY)
            now = time.time()
            level, pressure = self.last_level = _level()
            if self.state == 'sleeping' and self.until and now >= self.until:
                self.wake('timer')
                continue
            critical = pressure >= 4 or level <= CRITICAL_LEVEL
            if self.state == 'awake':
                self.critical_since = (self.critical_since or now) if critical else None
                busy = hub.brain and hub.brain.busy.is_set()
                if self.critical_since and now - self.critical_since >= CRITICAL_FOR and (
                        not busy or now - self.critical_since >= 60):
                    self.critical_since = None
                    from . import voice
                    voice.say_recorded('memory_nap')
                    self.sleep(reason='memory')
            elif self.state == 'sleeping' and self.reason == 'memory':
                comfortable = pressure == 1 and level >= COMFORTABLE_LEVEL
                self.comfortable_since = (self.comfortable_since or now) if comfortable else None
                if self.comfortable_since and now - self.comfortable_since >= COMFORT_FOR:
                    self.wake('memory freed up')


power = Power()
