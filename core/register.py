"""How casually BITCH talks to someone right now.

People don't open with slang and swearing; they start polite and loosen up as they get comfortable, and they
mirror how the other person talks. BITCH does the same: every person starts at a polished, capable register, and
each exchange moves it a little toward how *they* talk (slang, swearing, lowercase texting, "bro", "fr"...).
The level is kept per person, so the next conversation picks up roughly where the last one left off, a bit
more formal after a long break.

Levels: 0 = polished assistant with dry wit, 1 = full friend talk, slang and swearing included.
"""
import re
import time

from . import memory

START = .12          # a new person gets the polished register
STEP = .18           # how far one exchange moves BITCH toward their style (slowly: several turns to change)
WARM = .04           # rapport: each exchange loosens up a little even if they talk normally
CEILING = .35        # rapport alone never goes past this; the rest comes from how they talk
COOL_PER_DAY = .1    # after a break, start a little more formal again

SLANG = re.compile(
    r"\b(?:bro|bruh|bra|dawg|dog|fam|homie|twin|gang|g|lil|fr|frfr|ong|on god|no cap|cap|deadass|lowkey|highkey|"
    r"ngl|tbh|idk|ion|ima|imma|finna|tryna|gonna|wanna|gotta|ain'?t|y'?all|yall|wsg|wsp|wyd|wym|hbu|smh|lmao|lmfao|"
    r"lol|lmk|bet|aight|ight|ard|yo|sus|mid|cooked|crash(?:ing)? out|it'?s giving|slay|ate|bussin|fire|goated|"
    r"rizz|gyat|opps?|pmo|ts|tf|wtf|af|asf|fs|ok+ur+|periodt|valid|vibes?|vibing|delulu|glaze|glazing|lock(?:ed)? in|"
    r"stfu|nah+|yeah+|ya|yea|yuh|chat|type shit|hella|mad|lit|dead|ded)\b", re.I)
SWEAR = re.compile(r"\b(?:fuck\w*|shit\w*|damn|hell|ass|dick|crap|piss\w*)\b", re.I)


def style(text):
    """How casual one thing they said is, 0 to 1."""
    words = re.findall(r"[\w']+", text or '')
    if not words:
        return None
    slang = len(SLANG.findall(text)) + .5 * len(SWEAR.findall(text.replace('BITCH', '')))
    score = min(1., slang / max(3., len(words) * .35))
    if text == text.lower() and len(words) >= 2:
        score += .12        # texting style: no capitals
    if not re.search(r"[.!?]$", text.strip()) and len(words) >= 4:
        score += .05
    return min(1., score)


class Register:
    def __init__(self):
        self.levels = {}     # person -> (level, updated_at)

    def level(self, person, now=None):
        now = time.time() if now is None else now
        if person not in self.levels:
            saved = memory.kv_get('register:' + person) if person else None
            self.levels[person] = tuple(saved) if saved else (START, now)
        level, updated = self.levels[person]
        days = max(0., (now - updated) / 86400)
        return max(START, level - COOL_PER_DAY * days)

    def heard(self, person, text, now=None):
        """Move toward how they just talked, a little. Returns the new level."""
        now = time.time() if now is None else now
        level = self.level(person, now)
        theirs = style(text)
        if theirs is None:
            return level
        target = max(theirs, min(CEILING, level + WARM))
        level = level + (target - level) * STEP if target > level else level + (target - level) * STEP * .5
        level = round(max(0., min(1., level)), 3)
        self.levels[person] = (level, now)
        if person:
            memory.kv_set('register:' + person, [level, now])
        return level

    @staticmethod
    def direction(level):
        """The line that tells the model how loose to be right now."""
        if level < .3:
            return ('Register right now: polished. Be clear, capable and friendly, like a sharp assistant with a dry '
                    'wit. No slang and no swearing yet, even if they use it; you understand it fine.')
        if level < .55:
            return ('Register right now: relaxed. You are comfortable with them: casual and direct, a contraction-heavy '
                    'friendly tone, light slang only where they used it first, rare mild swearing.')
        if level < .8:
            return ('Register right now: casual. Talk like their friend. Match their slang and energy naturally, '
                    'swearing is fine, but stay sharp and actually useful.')
        return ('Register right now: full friend mode. Talk the way they talk, slang, swearing and all, the way a '
                'close friend would. Keep it natural, never a parody, and still get things right.')


register = Register()
