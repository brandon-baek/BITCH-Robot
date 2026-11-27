"""Wake gating and short, local conversation. The model has no action interface."""
from collections import OrderedDict, deque
from datetime import datetime
from difflib import SequenceMatcher
from itertools import groupby
import base64
import json
import queue
import re
import threading
import time
import uuid

from . import config, gaze, helper, llm, memory, notifications, prosody, skills, streaming, voice, voicechat
from .register import register
from . import tools
from .power import power, asked_to_sleep, say_until, WAKE as WAKE_UP
from .hub import hub
from .life import life

# Whisper spells the name a few ways, and hears "Yo BITCH" as "No/You/My bitch" across a room.
NAME = r"(?:bitch|bich|bish|bitsch|b\.?-?\s?i\.?-?\s?t\.?-?\s?c\.?-?\s?h\.?)"
WAKE = re.compile(
    r"^\s*(?:(?:(?:hey|hi|hello|okay|ok|yo|you|no|oh|so|um|uh|well|my|alright|now)\b[\s,!.:-]+){1,2}"
    r"(?:mitch|pitch|fitch|witch|britch|beach|beech)\b|"
    r"(?:(?:hey|hi|hello|okay|ok|yo|you|no|oh|so|um|uh|well|my|alright|now|thanks|thank you|bye|goodbye|"
    r"good night|night|morning|good morning|cool|nice)\b[\s,!.:-]+){0,2}" + NAME + r"(?![\w'’]))"
    r"[\s,!.?:;—-]*", re.I)
SOCIAL = re.compile(r"\b(?:thanks|thank you|bye|goodbye|good night|night|good morning|morning)\b", re.I)
END_WAKE = re.compile(r"([\s,;:—-]+)(?:(?:hey|yo)[\s,]+)?" + NAME + r"(?![\w'’])(?:[\s,]+(?:please|thanks))?([\s.!?]*)$", re.I)
MID_WAKE = re.compile(r"[,;—]\s*(?:(?:hey|yo)[\s,]+)?bitch\s*[,;—]\s*", re.I)
MENTION = re.compile(r"\b(?:a|an|the|that|this|such|some|little|named|called|word|about|say|said|is|was|be|and|or)$", re.I)
AGENT = re.compile(r'^(?:please\s+|(?:can|could|would)\s+you\s+)?ask\s+(chat[ -]?gpt|(?:the\s+)?(?:desktop\s+)?agent)\s+to\s+(.+)$', re.I | re.S)
AGENT_UPDATE = re.compile(r"^(?:what did chat ?gpt (?:say|find)|(?:any|got an?) (?:news|update|answer) from chat ?gpt|"
                          r"(?:read|tell me) (?:the )?chat ?gpt (?:reply|answer))[.!?]*$", re.I)
LOCAL_REQUEST = re.compile(r"^(?:please\s+)?(?:ask|use)\s+(?:qwen|the local agent)\s+to\s+(.+)$", re.I | re.S)
SHOW = re.compile(
    r"^(?:(?:can|could|would|will) you\s+|(?:please|okay|ok)\s+|(?:let me|let's)\s+see you\s+)*"
    r"(?P<act>sing\b|dance\b|(?:do|play|perform|show me)\s+(?:(?:me|us|a|the|your|some|little)\s+)*"
    r"(?:dance|disco|opera|aria|song|dance party)\b)(?P<rest>.*)$", re.I)
STOP_SHOW = re.compile(
    r"^\s*(?:(?:hey|okay|ok|alright|please|yo)\b[\s,!.-]*)*"
    r"(?:stop(?:\s+(?:it|that|this|now|please|performing|dancing|singing|playing|the\s+music|"
    r"(?:the |that |your )?(?:show|dance|disco|opera|aria|routine|singing|music|performance)))?"
    r"|cancel(?:\s+(?:the |that )?(?:show|dance|performance|routine))?"
    r"|that(?:'s|’s) enough|enough|cut the music|quiet|shut up)"
    r"(?:\s+please)?[.!?\s]*$", re.I)
STOP_SHOW_TAIL = re.compile(
    r"\b(?:stop\s+(?:performing|dancing|singing|playing|the\s+show|the\s+music|that\s+show|the\s+dance|the\s+performance|it)|"
    r"cut\s+the\s+music|that(?:'s|’s)\s+enough|(?:hey\s+)?bitch[,!.\s]+stop)\b", re.I)
PERFORM_INFO = re.compile(
    r"\b(?:what|which|tell me(?: about)?|list|name)\b.{0,30}\b(?:performances?|shows?|dances?|routines?|songs?|tricks?)\b"
    r"|\bwhat can you (?:perform|dance|sing)\b"
    r"|\bdo you (?:know|have) any (?:other )?(?:performances?|shows?|dances?|routines?)\b", re.I)
SHOW_INVITATION = re.compile(
    r"^(?:(?:can|could|would|will) you\s+|please\s+)*(?:show off|entertain (?:me|us)|"
    r"give (?:me|us) a (?:show|performance)|do something (?:theatrical|dramatic|musical))\b[^?]*(?:\?|$)", re.I)
PERFORMANCE = {'sad': 'gentle', 'annoyed': 'emphatic', 'amused': 'playful', 'surprised': 'upbeat'}
VOICE_CODE = {'n': 'neutral', 'w': 'warm', 'u': 'upbeat', 'g': 'gentle', 'e': 'emphatic', 'p': 'playful'}
PERFORMANCE_CODE = {'s': 'sad', 'a': 'annoyed', 'l': 'amused', 'r': 'surprised'}
FOLLOWUP_SECONDS = 24
REACTION = re.compile(r"^\s*(?:(?:ha){2,}|hah|heh|lol|lmao|omg|oh my god|wow|whoa|good one|nice one|"
                      r"(?:okay|ok|alright|wow|haha|man|dude|bro)?[, ]*that(?:'s|’s| was| is) (?:so |pretty |really |kinda |actually )?"
                      r"(?:terrible|awful|funny|hilarious|good|great|bad|dumb|stupid|amazing|cool|wrong|right|true|"
                      r"wild|crazy|smart|weird|cringe|corny|lame|fair))\b", re.I)
CARRY_ON = re.compile(r"^\s*(?:yeah|yes|yep|yup|nah|nope|wait|and|but|actually|really|seriously|so what|what about|"
                      r"how about|what if|why|is that|are you sure|go ahead|do it|sure|true|same|exactly|no way|"
                      r"we(?:'ve|’ve| have) (?:seen|done|tried|heard))\b", re.I)
THEM_SURE = .7   # how sure the model must be that a line was for someone else before BITCH stays quiet
CONVERSATION_SECONDS = 900   # keep the thread of a conversation this long after the last turn
HISTORY_MESSAGES = 24        # the last 12 exchanges go to the model
REFLECT_AFTER = 150          # seconds of quiet before a conversation is summarized into memory
INTERRUPT = re.compile(r"^\s*(?:(?:hey[ ,]+)?bitch|wait(?: a (?:second|minute))?|hold (?:on|up)|hang on|"
                       r"one (?:second|sec)|stop)(?=\b)[, .!?—-]*", re.I)
TURN_ENTRY = re.compile(
    r"^\s*(?:actually\b|one thing\b|no[, .]|yeah[, .]|sorry[, .]|"
    r"but\b|what (?:if|about|do|did)|why\b|can (?:I|you)|let me|I (?:mean|said|meant|think|wanted)|"
    r"that(?:'s| is) (?:wrong|different)|you (?:misheard|missed))", re.I)


def wake_request(text):
    """Return the full addressed utterance or None if the name was not used as an address."""
    if match := WAKE.match(text):
        rest = text[match.end():].strip()
        # "Thanks, BITCH" and "Good night, BITCH" are things said to it, not just its name: keep them.
        social = SOCIAL.search(text[:match.end()])
        if social:
            return (social[0].capitalize() + ('. ' + rest if rest else '.')).strip()
        return rest
    if match := END_WAKE.search(text):
        before = text[:match.start()].rstrip()
        # "about that BITCH" is a mention, but a comma or a question at the end makes it an address.
        if before and (not MENTION.search(before) or ',' in match[1] or '?' in match[2]):
            ending = match[2].strip() if before[-1] not in '.!?' else ''
            return before + ending
    if match := MID_WAKE.search(text):
        return (text[:match.start()] + ' ' + text[match.end():]).strip()
    return None


def is_stop_show(text):
    """True when the speaker is telling BITCH to stop an active performance."""
    if not text:
        return False
    stripped = wake_request(text)
    candidate = stripped if stripped is not None else text
    return bool(STOP_SHOW.fullmatch(text) or STOP_SHOW.fullmatch(candidate) or STOP_SHOW_TAIL.search(text))


def repeats_playback(text, spoken):
    words, reply = (re.findall(r"\w+", line.casefold()) for line in (text, spoken))
    if not words or not reply:
        return False
    overlap = SequenceMatcher(None, words, reply, autojunk=False).find_longest_match().size
    if overlap >= (len(words) if len(words) <= 3 else len(words)-1):
        return True
    if len(words) < 3:
        return False
    return any(sum(match.size for match in SequenceMatcher(
        None, words, reply[i:i+len(words)+3], autojunk=False).get_matching_blocks())
        >= .68 * len(words) for i in range(len(reply)))
FAMILY_WORDS = (r"mom|mommy|mum|mama|ma|dad|daddy|papa|pops|honey|hon|babe|sweetie|sweetheart|darling|"
                r"sis|guys|you guys|grandma|grandpa|nana|granny|kids|son|kiddo|auntie|uncle")  # not "bro" or "dude": people call BITCH that


def people_names():
    try:
        from .people import people
        return people.names('face') | people.names('voice')
    except Exception:
        return set()


# Words that start a line with a comma but aren't anyone's name ("Well, ...", "Actually, ...").
OPENERS = {'well', 'yeah', 'yes', 'yep', 'nope', 'no', 'okay', 'alright', 'actually', 'honestly', 'wait', 'hmm', 'um',
           'uh', 'oh', 'ah', 'nice', 'cool', 'great', 'thanks', 'sure', 'right', 'anyway', 'also', 'then', 'now', 'see',
           'look', 'listen', 'dude', 'bro', 'man', 'wow', 'damn', 'ugh', 'haha', 'lol', 'fine', 'please', 'sorry',
           'seriously', 'basically', 'like', 'and', 'but', 'so', 'first', 'second', 'finally', 'today', 'tomorrow',
           'tonight', 'yesterday', 'again', 'perfect', 'awesome', 'true', 'exactly', 'totally', 'definitely', 'maybe',
           'hey', 'hello', 'hi', 'bitch', 'god', 'jesus', 'gosh', 'whoa', 'yikes', 'oops', 'good', 'bad', 'cheers'}


def talks_to_someone_else(text, names=()):
    """True when the line calls on another person by name or family word ("Mom, ...", "..., right honey?")."""
    known = [re.escape(name) for name in names if name and name.casefold() != 'bitch']
    who = r"(?:" + FAMILY_WORDS + (r"|" + "|".join(known) if known else "") + r")"
    # Someone's name, even one BITCH hasn't met, opening the line: "Emma, did you know that?"
    opener = re.match(r"\s*(?:(?i:hey|okay|ok|so|and|oh)[,!.]?\s+)?([A-Z][a-z]{2,})\s*,\s+\S", text)
    if opener and opener[1] not in NOT_NAMES and opener[1].casefold() not in OPENERS:
        return True
    return bool(re.search(r"(?:^|[.!?]\s+)(?:(?:hey|okay|ok|so|thanks|and|oh)[,!.]?\s+)?" + who + r"\s*[,!?]", text, re.I)
                or re.search(r",\s*(?:(?:right|okay|ok|thanks|thank you|love you|please|yes|no|sure)\s+)?" + who
                             + r"\s*[.!?]*$", text, re.I))


ECHO_FILLER = {'the', 'and', 'for', 'that', 'this', 'with', 'from', 'have', 'been', 'were', 'they', 'them', 'your'}


def sounds_like_own_words(text, lines):
    """A transcript that is just BITCH's own recent words coming back through the mic ("Let check." after it said
    "Let me check.", or "Tomorrow and the crescent is warm." after "tomorrow in La Crescenta's warm"). Loose on
    purpose: the recognizer drops, splits, and phonetically mangles words in an echo."""
    words = [re.sub(r"['’]", '', w) for w in re.findall(r"[\w'’]+", text.casefold())]
    if not words:
        return False
    norm_text = re.sub(r'[^a-z0-9 ]+', '', text.casefold()).strip()
    content = [w for w in words if len(w) >= 4 and w not in ECHO_FILLER]
    fuzzy = lambda a, b: a == b or (len(a) >= 4 and len(b) >= 4 and SequenceMatcher(None, a, b).ratio() >= .78)
    for line in lines:
        said = [re.sub(r"['’]", '', w) for w in re.findall(r"[\w'’]+", line.casefold())]
        if not said:
            continue
        said_set = set(said)
        if len(words) <= len(said) + 2 and sum(w in said_set for w in words) >= .75 * len(words):
            return True
        # Catch phonetically mangled echoes where all/nearly-all content words match a nearby span of BITCH's line.
        if (len(content) >= 2 and len(words) <= len(said) + 2
                and sum(any(fuzzy(w, s) for s in said_set) for w in content) >= .8 * len(content)):
            norm_line = re.sub(r'[^a-z0-9 ]+', '', line.casefold()).strip()
            if len(norm_text) >= 8 and norm_line:
                win = len(norm_text) + 6
                step = max(2, len(norm_text) // 6)
                best = max((SequenceMatcher(None, norm_text, norm_line[i:i + win], autojunk=False).ratio()
                            for i in range(0, max(1, len(norm_line) - len(norm_text) // 2), step)), default=0.)
                if best >= .62:
                    return True
    return repeats_playback(text, ' '.join(lines))


CUT_IN = re.compile(r"(?:wait|hold on|hold up|hang on|actually|no no|stop|sorry|scratch that)\b", re.I)


def strip_playback(text, spoken):
    """Drop BITCH's own words from the front of a talk-over transcript ("A small robot. Wait, make it..." -> "Wait, make it...").

    The microphone hears the speaker for a moment before the person's words win, and the recognizer mangles that echo
    ("Alright, gather 'round" comes back as "All right, gather round"), so words match loosely. Two ways to cut:
    a run of two or more of BITCH's words at the very start, or everything before a clear "wait / actually / hold on"
    when the words before it are mostly BITCH's."""
    def words_of(line):
        line = re.sub(r"\ball right\b", 'alright  ', line.casefold())   # same length, so positions still match
        return [(re.sub(r"['’]", '', m[0]), m.start()) for m in re.finditer(r"[\w'’]+", line)]

    tokens, reply = words_of(text), [w for w, _ in words_of(spoken or '')]
    if len(tokens) < 3 or not reply:
        return text
    words = [w for w, _ in tokens]
    same = lambda a, b: a == b or len(a) > 3 and SequenceMatcher(None, a, b).ratio() >= .75
    best = 0
    for start in range(len(reply)):
        n = 0
        while n < len(words) and start + n < len(reply) and same(words[n], reply[start + n]):
            n += 1
        best = max(best, n)
    # Two words of BITCH's at the start, or all of a short line like "Yeah?" ("Yeah. What's the weather...").
    whole_short = len(reply) <= 2 and best == len(reply)
    cut = best if (best >= 2 or whole_short) and best < len(words) - 1 else 0
    said = set(reply)
    for index, (word, at) in enumerate(tokens[:14]):
        if index > cut and CUT_IN.match(text[at:]):
            before = words[cut:index]
            # Echo at the start, or mostly BITCH's words: all of it before "wait" was the speaker, not them.
            if before and (cut or sum(w in said for w in before) >= .5 * len(before)):
                cut = index
            break
    return text[tokens[cut][1]:].lstrip(' ,.;:!?—-') if cut else text
REMEMBER = re.compile(r'^remember(?: that)?\s+(.+)', re.I)
# Meeting people in conversation. Names must be capitalized, the way Whisper writes them.
LEAD = r"(?i:(?:(?:hi|hey|hello|oh|so|well|okay|ok|and)[,!.]?\s+)*)"
NAME_WORD = r"([A-Z][a-z'’-]+(?:\s+[A-Z][a-z'’-]+)?)(?=[\s.,!?]|$)"
SELF_INTRO = re.compile(r"^" + LEAD + r"(?i:my name(?:'s|’s| is)|i'm|i’m|i am|call me|it's me,?|it’s me,?)\s+" + NAME_WORD)
RELATION_INTRO = re.compile(
    r"(?i:\b(?:i(?:'|’)m|i am)\s+(?:([A-Z][a-z]+)(?:'|’)s|his|her|their)\s+(mom|mother|dad|father|sister|brother|friend|roommate|partner|girlfriend|boyfriend|cousin|aunt|uncle|grandma|grandpa)(?:,?\s+" + NAME_WORD + r")?)"
)
RELATION_AFTER = re.compile(
    r"(?i:,\s*(?:([A-Z][a-z]+)(?:'|’)s|his|her|their|your)\s+(mom|mother|dad|father|sister|brother|friend|roommate|partner|girlfriend|boyfriend|cousin|aunt|uncle|grandma|grandpa))"
)
NOTIF_CHECK = re.compile(r"\b(?:read|check|any|what(?:'s| are)|show)\b.{0,24}\bnotifications?\b", re.I)
INTRO_OTHER = re.compile(r"(?i:\b(?:this is|meet|say (?:hi|hello) to|i want you to meet|introduce you to)\s+"
                         r"(?:(?:my|our)\s+(?:[a-z]+\s+){0,2})?)" + NAME_WORD)
LEARN_ME = re.compile(r"^(?:(?:please|can you|could you|will you)\s+)?(?:remember|learn|memorize|get to know)\s+"
                      r"(?:my (?:face|voice)|what i (?:look|sound) like|me|who i am)\b", re.I)
FORGET_ME = re.compile(r"\bforget (?:me|my (?:face|voice)|who i am|what i (?:look|sound) like)\b", re.I)
NOT_NAMES = {'I', 'A', 'The', 'My', 'It', 'This', 'That', 'Here', 'Back', 'Home', 'Fine', 'Good', 'Okay', 'Sorry', 'Done',
             'Ready', 'Not', 'Just', 'So', 'Very', 'Really', 'Sure', 'Bitch', 'BITCH', 'Monday', 'Tuesday', 'Wednesday',
             'Thursday', 'Friday', 'Saturday', 'Sunday', 'ChatGPT', 'Google', 'Siri', 'Alexa', 'Going', 'Still', 'Also'}
FORGET = re.compile(r'^(?:please |you can |could you )?forget(?: that| about)?\s+(.+?)[.!?]*$', re.I)
RECALL = re.compile(r'^what do you remember(?: about me)?[.!?]*$', re.I)
VOICE_CHECK = re.compile(r"\b(?:recognize|recognise|remember|identify)\b.{0,38}\b(?:my voice|how I sound)\b", re.I)
VISUAL = re.compile(
    r"\b(?:look (?:at|here|around)|what.{0,35}(?:see|wearing|holding|in (?:my|the) (?:hand|room|background))"
    r"|what colou?r (?:is|are) (?:this|that|these|those|it|my|the)\b"
    r"|(?:can|do) you see|how many (?:people|fingers)|describe (?:this|that|the (?:room|scene|desk))"
    r"|who(?:'s| is) (?:standing|sitting|there|here|with me|in the room)|who do you see"
    r"|what(?:'s| is| are) (?:this|that|these|those)(?:\s+(?:thing|things|here|over here|right here|in my hand|i'm holding))?\s*[?.!]*$"
    r"|what(?:'s| is| are) on (?:my|the) (?:desk|table)|what do I have\b|what does .{0,32} look like|how (?:do|does) .{0,32} look\b|what.{0,28}(?:picture|camera|background)|read (?:this|that)|showing you|behind me|in front of (?:me|you))\b", re.I)
VISUAL_FOLLOWUP = re.compile(r"\b(?:this|that|it|now|other|color|colour|holding|wearing)\b", re.I)
EMOTIONS = [('proud', r'\b(?:good (?:job|robot)|proud of you|nailed it)\b'),
            ('happy', r"\b(?:we did it|high five|i (?:did it|won)|that.s awesome)\b"),
            ('sad', r"\b(?:feel sad|bad day|miss (?:him|her|them)|i.m sad)\b"),
            ('scared', r"^(?:boo|you scared me)[!. ]*$")]
FACE_CHECK = re.compile(r"\b(?:(?:recognize|recognise|remember|identify) (?:me|my face)|who am I|know who I am)\b", re.I)
HANDLE_OBJECT = re.compile(
    r"^(?:(?:can|could|would|will|did) you\s+)?(?:please\s+)?(?:pick up|grab|carry|lift|hand me|move|put)\s+"
    r"(?:a|an|the|that|this|my|your|it)\b", re.I)
PAST_MEMORY = re.compile(
    r"\b(?:what (?:did I|have I) (?:tell|say to) you|do you remember what I (?:said|told you)|"
    r"do you remember (?:me|when)|what do you remember about)\b", re.I)
PERSON_NAME = re.compile(r"\b(?:what(?:'s| is) the name of|do you recognize|who(?:'s| is))"
                         r".{0,45}\b(?:person|someone|standing|sitting|behind me|next to me)\b", re.I)
TURN_SCHEMA = {
    'type': 'object',
    'properties': {
        'delivery': {'type': 'string', 'enum': list(voice.DELIVERY)},
        'performance': {'type': 'string', 'enum': ['none', *PERFORMANCE]},
        'say': {'type': 'string'},
        'turn': {'type': 'string', 'enum': ['continue', 'end']},
        'remember': {'type': 'string', 'maxLength': 180},
        'show': {'type': 'string', 'enum': ['none', 'dance', 'disco', 'opera']},
    },
    'required': ['say', 'turn', 'delivery', 'performance', 'remember', 'show'],
    'additionalProperties': False,
}
TURN_DIRECTION = """Return JSON in schema order: delivery, performance, say (the exact spoken words), turn, remember, show.
Begin say with a direct answer or a concrete first thought. Use short complete sentences so the robot can start speaking as you write.
Ground claims about the room, the person, past conversations, and performed actions in the observations or records provided. If evidence is absent or unclear, say you are unsure in one sentence. Never invent a completed action or external lookup. Ask one short question when a missing detail matters.
Set show to none unless the system explicitly invites you to select a performance.
Use end when the person clearly says goodbye, says they're done, gives a closing acknowledgment like "That answers it, thanks" after a completed exchange, or naturally wraps up. When ending, say a brief farewell or acknowledgment without a follow-up question.
Use continue when the person asks a question, makes a request, introduces a new topic, or is still telling a story. A thank-you followed by another question is continue. Finishing your own answer is not itself a reason to end.
Match your spoken reply with a delivery of neutral, warm, upbeat, gentle, emphatic, or playful. Use gentle when comforting someone, upbeat for good news, and warm for a friendly goodbye.
Choose performance none, sad, annoyed, amused, or surprised. Use a marked performance only when it fits this reply. Amused means actual brief laughter before a dry line, annoyed means playful indignation at the situation, and surprised means a quick recoil. A factual answer usually uses none.
For remember, output an empty string unless the latest user utterance clearly states a lasting preference, a named ongoing project or goal, or asks indirectly that a fact be kept for later. In that case extract only that fact in the user's first person. Skip temporary details, other people's private information, secrets, health, legal or financial details, and anything you inferred. Do not turn a scene observation or earlier assistant text into a memory."""
SPOKEN_DIRECTION = """Return JSON in order: v, say, end.
v is n neutral, w warm, u upbeat, g gentle, e emphatic, p playful, s sad, a mock-annoyed, l an actual laugh, or r surprised.
say is what you say out loud. Begin with a direct answer or a concrete reaction to the latest utterance, and keep that first sentence short. Answer every part and honor requested examples, steps, or length. Don't invent details about the room, the person, previous turns, or actions you took. Ask one short question when a detail matters. Keep jokes on topic. If someone shares a hard experience, acknowledge what they actually said.
Answer their mood with your voice code: meet excitement with u or p, sadness with g, sarcasm with a dry p or a, and real anger with a steady n or w. If the reply changes mood mid-way, put the new voice code in square brackets right before that sentence (like [p] or [w]). Keep one voice when the mood stays the same.
end is false unless the speaker is clearly wrapping up and has no further question or request. For a closing turn, use warm delivery and just acknowledge or say goodbye."""
REMEMBERING = ('You remember people between conversations: what you know about them and your earlier talks are listed in '
               'the turn notes when you know who you are talking to. If something isn’t listed, say you don’t remember it. '
               'Never say you can’t remember or have no memory.')
# What changes from turn to turn (time, who's talking, memory, available actions, tone of voice) arrives as a note at
# the start of the person's message, so the system prompt above stays identical and cached.
TURN_NOTE = '[Turn notes, not said by them:\n{}]\n'
TURN_NOTE_RULE = ('Each message from them may start with bracketed turn notes from your own senses and memory. Use them, '
                  'but never read them out or mention them; reply only to what they said.')
MEMORY_DIRECTION = """Then add note. When they state a new lasting preference, named ongoing project or goal, or a fact they ask you to keep, write it in THEIR words as they would say it (for example "I prefer oat milk in my coffee"). Otherwise use an empty string. Skip guesses, temporary details, secrets, other people's private information, and health, legal or financial details."""
SPOKEN_PROPERTIES = {
    'v': {'type': 'string', 'enum': [*VOICE_CODE, *PERFORMANCE_CODE]},
    'say': {'type': 'string'},
    'end': {'type': 'boolean'},
}
SPOKEN_SCHEMA = {'type': 'object', 'properties': SPOKEN_PROPERTIES,
                 'required': list(SPOKEN_PROPERTIES), 'additionalProperties': False}
MEMORY_SCHEMA = {**SPOKEN_SCHEMA,
                 'properties': {**SPOKEN_PROPERTIES, 'note': {'type': 'string', 'maxLength': 180}},
                 'required': [*SPOKEN_PROPERTIES, 'note']}
NEW_REQUEST = re.compile(
    r"\bone more (?:thing|question)\b|(?:^|[.!]\s*)(?:and |also[, ]+)?(?:can|could|would) you\b", re.I)
LEAVING = re.compile(
    r"\b(?:i (?:have|need|got) to go|i(?:'|’)m (?:gonna|going to) (?:get back to|head (?:out|off))"
    r"|i (?:have|need) to get back to|that answers (?:it|my question)|that(?:'|’)s (?:it|all) for (?:now|today))\b",
    re.I,
)
END = re.compile(r'^(?:stop listening|end (?:the )?(?:chat|conversation)|that.s all(?: for now)?|that was all|we.re done(?: here| for now)?|never ?mind(?: then)?|(?:thanks[, ]+)?(?:goodbye|bye(?: bye)?|see you later|talk to you later))[\s?.!]*$', re.I)


# Offer the shows only when the request is about performing, so a math question can't start a dance.
PERFORM = re.compile(r"\b(?:danc\w*|moves?|groove|boogie|party|sing\w*|song|music|tune|perform\w*|show|"
                     r"entertain\w*|disco|opera|aria|bored|celebrat\w*|jam)\b", re.I)
# Asking ChatGPT to do something explicitly, or asking about a pending background lookup.
CHATGPT = re.compile(r"\b(?:ask|get|have|use|send|check with|run (?:it|this|that) by)\b[^.?!]{0,30}\bchat ?gpt\b"
                     r"|\bwhat (?:does|would|did|will) chat ?gpt (?:say|think)|\bchat ?gpt,? (?:please|can you|could you)\b", re.I)
PENDING_UPDATE = re.compile(
    r"^(?:(?:any|got an?) (?:news|update|answer|word)(?: on that| yet)?|did (?:that|it) come back(?: yet)?|"
    r"is (?:that|my) (?:briefing|answer|lookup) ready)[.!?]*$", re.I)
LOOK_FIRST = re.compile(r'^\s*\{\s*(?:"to"\s*:\s*"me"\s*,\s*)?"do"\s*:\s*"look"')
TOOL_FIELDS = set(tools.schema_fields(tools.TOOLS))
NOT_FOR_ME = re.compile(r'^\s*\{\s*"to"\s*:\s*"them"')


class LookFirst(Exception):
    """The model chose to look through the camera before answering."""


class ToolFirst(Exception):
    """The model chose a tool that fetches facts; get them before saying anything."""
    def __init__(self, turn):
        super().__init__(turn.get('do'))
        self.turn = turn


class NotForMe(Exception):
    """In a group, the model decided this line was said to someone else in the room."""


class Brain:
    def __init__(self):
        self.pending_sleep = None   # seconds of deep sleep to start once the goodbye has been said
        self.voices_heard = {}   # name -> when their voice was last recognized
        self.overheard = deque(maxlen=4)   # recent lines people said to each other, not to BITCH
        self.requests = queue.Queue(maxsize=1)
        self.history = OrderedDict()
        self.history_at = {}
        self.history_owner = {}   # who each conversation was with, for its summary
        self.reflected = {}       # conversation -> (what was summarized, memory id)
        self.followup = None
        self.speaker = None
        self.speaker_evidence = None
        self.current_voice = None
        self.accepted_wake = False
        self.close_turn = False
        self.delivery = 'neutral'
        self.performance = 'none'
        self.pending_show = None
        self.turn_audio = None
        self.native_audio = None
        self.last_agent_request = None
        self.expression = None
        self.testing_output = False
        self.last_visual = None
        self.speech_span = None
        self.heard_end = None
        self.pending_text = None   # the request being answered, so a continuation can join it
        self.continuation = None
        self.replacing_user_message = False
        self.cloud_primed = None   # (provider, conversation_key) once the persistent Pi chat has BITCH's persona
        self.acknowledged = None
        self.active_source = None
        self.interruption = False
        self.spoken_reply = ''
        self.last_interaction = time.monotonic()
        self.last_reply_end = 0.0
        self.last_reply_ended_with_question = False
        self.last_reply_was_lookup_ack = False
        self.ready_updates = []   # arrived background results waiting to be woven into chat or announced
        self.lock = threading.Lock()
        self.busy = threading.Event()
        self.hush = threading.Event()
        self.revision = 0
        self.turn_count = 0
        self.persona = (config.ROOT / 'core' / 'persona.md').read_text().strip()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()
        threading.Thread(target=self._reflect, daemon=True).start()

    def _reflect(self):
        """Once a conversation goes quiet, remember it in a sentence or two. Tidy memory once a night."""
        while True:
            time.sleep(30)
            try:
                self.reflect()
            except Exception as error:
                hub.note('memory', f'Couldn’t save a conversation summary: {error}')
            try:
                self.nightly_clean()
            except Exception as error:
                hub.note('memory', f'Couldn’t tidy memory: {error}')

    def nightly_clean(self, now=None):
        """Between 3 and 5 AM, when nobody is talking, drop duplicate, contradicted, junk and stale memories."""
        now = datetime.now() if now is None else now
        today = now.strftime('%Y-%m-%d')
        if not 3 <= now.hour < 5 or self.busy.is_set() or memory.kv_get('memory:cleaned') == today:
            return None
        memory.kv_set('memory:cleaned', today)
        return memory.clean()

    def reflect(self, now=None, quiet=REFLECT_AFTER):
        now = time.monotonic() if now is None else now
        for key, history in list(self.history.items()):
            owner = self.history_owner.get(key)
            turns = list(history)
            signature = (len(turns), turns[-1]['content'] if turns else '')
            if (not owner or self.busy.is_set() or now - self.history_at.get(key, now) < quiet
                    or sum(turn['role'] == 'user' for turn in turns) < 2 or self.reflected.get(key, (None,))[0] == signature):
                continue
            transcript = '\n'.join(('Them' if turn['role'] == 'user' else 'BITCH') + ': ' + turn['content'][:300]
                                   for turn in turns)
            episode = memory.consolidate_conversation(
                owner, transcript,
                replace_episode=self.reflected.get(key, (None, None))[1],
                chat_fn=llm.chat,
            )
            self.reflected[key] = (signature, episode)

    def spoken_system(self):
        # Never changes between turns (see TURN_NOTE): the model server keeps it cached.
        return (f'{self.persona}\n\n{SPOKEN_DIRECTION}\n{MEMORY_DIRECTION}\n{REMEMBERING}\n{TURN_NOTE_RULE}\n'
                f'{register.GUIDE}')

    def set_delivery(self, metadata):
        code = metadata.get('v')
        if isinstance(code, str) and (code in VOICE_CODE or code in PERFORMANCE_CODE):
            self.delivery = VOICE_CODE.get(code, 'neutral')
            self.performance = PERFORMANCE_CODE.get(code, 'none')
            return
        delivery = metadata.get('delivery')
        performance = metadata.get('performance')
        if isinstance(delivery, str) and delivery in voice.DELIVERY:
            self.delivery = delivery
        if isinstance(performance, str) and performance in PERFORMANCE:
            self.performance = performance

    def answer_with_tool(self, turn, text, source, identity, stream):
        """Get the facts (weather, a list, timers), then answer from them."""
        name = turn.get('do')
        result = tools.run(name, turn, {'source': source, 'speaker': identity})
        hub.note('tools', f'{name}: {result[:160]}')
        self.tool_result = (name, result)
        try:
            return self.reply(text, source, stream=stream)
        finally:
            self.tool_result = None

    def tool_failure(self, action, outcome, text, source, identity, stream):
        if stream and stream.parts:
            # Already confirmed out loud: correct it plainly.
            return ' '.join(stream.parts) + ' Actually, scratch that, it didn’t work.'
        self.tool_result = (action, outcome)
        try:
            return self.reply(text, source, stream=stream)
        finally:
            self.tool_result = None

    def not_for_me(self, source, text, stream=None, judged=False):
        """Stay quiet for a line meant for someone else, but keep it in mind for the next turn.

        Only when the model was sure: ignoring someone who was talking to BITCH feels far worse than answering a line
        that wasn't for it, so a hesitant "them" gets an answer instead."""
        sure = llm.choice_confidence('to', 'them') if judged else None
        if sure is not None:
            print(f'  Room: "for someone else" at {sure:.2f}', flush=True)
        if sure is not None and sure < THEM_SURE and not getattr(self, 'answer_anyway', False):
            self.answer_anyway = True
            try:
                return self.reply(text, source, stream=stream)
            finally:
                self.answer_anyway = False
        self.overheard.append((time.monotonic(), text))
        hub.note('model', None)
        return ''

    def overheard_note(self):
        now = time.monotonic()
        lines = [text for at, text in self.overheard if now - at < 90]
        return ('Said between other people in the room in the last minute or so (not to you): '
                + ' / '.join(json.dumps(line, ensure_ascii=False) for line in lines[-3:])) if lines else None

    def recent_speech(self, seconds=8.):
        """What BITCH said out loud in the last few seconds (its echo can still be in the mic)."""
        now = time.time()
        lines = [m['text'] for m in hub.messages if m['role'] == 'robot' and now - m['at'] < seconds]
        if self.spoken_reply and self.spoken_reply not in lines:
            lines.append(self.spoken_reply)
        return lines

    def room_shared(self):
        """True when more than one person is around (two faces in view, or two voices in the last few minutes)."""
        in_view = getattr(hub.sight, 'in_view', None)
        view = in_view() if in_view else None
        seen = len(view['names']) + view['others'] if view else 0
        now = time.monotonic()
        return seen >= 2 or sum(now - at < 180 for at in self.voices_heard.values()) >= 2

    def addressed(self, text, source, heard_at):
        with self.lock:
            robot = hub.get('robot')
            performing = hub.status == 'Performing' or bool(robot and getattr(robot, 'show_active', False))
            show_stop = performing and is_stop_show(text)
            alarm_stop = life.alarm_ringing and life.is_alarm_reply(text)
            return bool(wake_request(text) is not None or show_stop or alarm_stop or
                        power.sleeping and WAKE_UP.match(text.strip()) or
                        self.followup and self.followup[0] == source and heard_at < self.followup[1])

    def submit(self, text, source, heard_at=None, *, name=None, face=None, face_by='face', speech_end=None, audio=None):
        """Admit one addressed utterance. Setup and unrelated speech never reach the model."""
        with self.lock:
            robot = hub.get('robot')
            performing = hub.status == 'Performing' or bool(robot and getattr(robot, 'show_active', False))
            show_stop = performing and is_stop_show(text)
            if self.busy.is_set():
                if show_stop:
                    self.hush.set()
                    if robot:
                        threading.Thread(target=robot.hush, daemon=True).start()
                    return True
                return False
            now = time.monotonic() if heard_at is None else heard_at
            wake = wake_request(text)
            followup = self.followup and self.followup[0] == source and now < self.followup[1]
            alarm_stop = life.alarm_ringing and life.is_alarm_reply(text)
            woken = power.sleeping and bool(WAKE_UP.match(text.strip()))
            if woken:
                wake = text.strip()
            if wake is None and not show_stop and not alarm_stop and (not followup or name and self.speaker and name != self.speaker):
                return False
            continuation = self.continuation
            self.continuation = None
            self.replacing_user_message = False
            if continuation and continuation[0] == source and now < continuation[2]:
                addition = wake if wake is not None else text.strip()
                text = (continuation[1] + ' ' + addition).strip() if addition else continuation[1]
                wake = None
                self.replacing_user_message = True
            elif wake == '' and not show_stop:
                # Only the name, after a pause: open the floor without making the mic deaf.
                if name:
                    self.speaker, self.speaker_evidence = name, 'voice'
                elif face and not self.speaker:
                    self.speaker, self.speaker_evidence = face, face_by if face_by in ('face', 'body') else 'face'
                if not (self.acknowledged and self.acknowledged[0] == source):
                    self.open_floor(source, name or face)
                return True
            if not followup:
                self.clear_conversation(source)
            previous = self.speaker if followup else None
            self.speaker = name or previous or face
            if name:
                self.speaker_evidence = 'voice'
            elif face and (not previous or previous == face and self.speaker_evidence == 'body' and face_by == 'face'):
                self.speaker_evidence = face_by if face_by in ('face', 'body') else 'face'
            elif not previous:
                self.speaker_evidence = None
            self.current_voice = name
            if name:
                self.voices_heard[name] = now
            self.accepted_wake = wake is not None
            self.turn_audio = audio
            self.speech_span = (now, speech_end if speech_end is not None else now)
            self.heard_end = speech_end
            self.followup = None
            self.active_source = source
            self.interruption = False
            self.spoken_reply = ''
            if hub.sight and self.speaker:
                hub.sight.talking_with(self.speaker)
            self.last_interaction = time.monotonic()
            self.turn_count += 1
            self.hush.clear()
            self.busy.set()
            self.pending_text = wake if wake is not None else text.strip()
            self.acknowledged = None
            self.requests.put_nowait((self.pending_text, source))
            return True

    def check_in(self, phrase, source):
        with self.lock:
            if (self.busy.is_set() or time.monotonic()-self.last_interaction < 900
                    or not config.get('check_ins') or config.get('muted') or not hub.accepts(source, 'ears')):
                return False
            self.clear_conversation(source)
            self.active_source = source
            self.interruption = False
            self.last_interaction = time.monotonic()
            self.hush.clear()
            self.busy.set()
            self.requests.put_nowait((None, (source, phrase)))
            return True

    def can_interrupt(self, source):
        return (self.active_source == source and self.busy.is_set() and not self.hush.is_set()
                and not self.testing_output and hub.status in ('Speaking', 'Performing'))

    def can_continue(self, source):
        """The person is still talking while BITCH thinks, before it has said anything."""
        return (self.active_source == source and self.busy.is_set() and not self.hush.is_set()
                and not self.testing_output and hub.status == 'Thinking' and not self.spoken_reply
                and self.pending_text is not None)

    def hold_for_more(self, source):
        # The turn ended too soon: drop the half-made reply and answer the whole thing.
        # Keep the user's message visible on screen until the combined message replaces it in-place.
        with self.lock:
            if not self.can_continue(source):
                return False
            self.continuation = (source, self.pending_text, time.monotonic() + 8)
            self.interruption = True
            self.followup = (source, time.monotonic() + FOLLOWUP_SECONDS)
            self.hush.set()
            hub.status = 'Listening for your turn'
        return True

    def resume_continuation(self, source):
        """If Thinking was paused for a continuation that turned out empty, restore the original request."""
        with self.lock:
            cont = self.continuation
            if not cont or cont[0] != source or self.busy.is_set():
                return False
            self.continuation = None
            if time.monotonic() >= cont[2]:
                return False
            original = cont[1]
            self.replacing_user_message = True
            self.followup = None
            self.active_source = source
            self.interruption = False
            self.spoken_reply = ''
            self.last_interaction = time.monotonic()
            self.hush.clear()
            self.busy.set()
            self.pending_text = original
            self.requests.put_nowait((original, source))
        return True

    def stop_performance(self, source):
        """Immediately stop an active show or skill performance on the Pi and open the floor."""
        robot = hub.get('robot')
        stopped = False
        if robot and getattr(robot, 'show_active', False):
            robot.show_active = False
            threading.Thread(target=robot.hush, daemon=True).start()
            stopped = True
        with self.lock:
            if hub.status == 'Performing':
                self.hush.set()
                stopped = True
            if stopped:
                self.followup = (source, time.monotonic() + FOLLOWUP_SECONDS)
                self.last_interaction = time.monotonic()
                if not self.busy.is_set():
                    hub.status = 'Listening for your turn'
        if stopped:
            threading.Thread(target=voice.earcon, args=('stop',), daemon=True).start()
        return stopped

    def acknowledge(self, source):
        """Just the name was said: chirp now and keep listening, without taking a turn."""
        with self.lock:
            return self.open_floor(source)

    def open_floor(self, source, name=None):
        # Called with the lock held.
        now = time.monotonic()
        if self.busy.is_set() or self.acknowledged and self.acknowledged[0] == source and now < self.acknowledged[1]:
            return False
        if not (self.followup and self.followup[0] == source and now < self.followup[1]):
            self.clear_conversation(source)
        self.acknowledged = (source, now + 4)
        self.followup = (source, now + FOLLOWUP_SECONDS)
        self.last_interaction = now
        hub.status = 'Listening for your turn'
        if hub.selected('voice') and not config.get('muted') and config.get('volume'):
            def answer():
                text, wav = voice.acknowledge(name or self.speaker)
                if text:
                    hub.chat('robot', text)
                    self.spoken_reply = text   # so its own words aren't heard as yours
                voice.play(*wav, threading.Event(), text=bool(text), delivery='warm', interim=True)
                self.last_reply_end = time.monotonic()
                self.last_reply_ended_with_question = bool(text and text.rstrip().endswith('?'))
                self.last_reply_was_lookup_ack = False
                if hub.status == 'Speaking' and not self.busy.is_set():
                    hub.status = 'Listening for your turn'

            threading.Thread(target=answer, daemon=True).start()
        return True

    def interrupt(self, source, text, voice_name=None, strong=False, sustained=False):
        if not self.can_interrupt(source):
            return False
        marker = INTERRUPT.match(text)
        if hub.status == 'Performing' and not (marker or is_stop_show(text)):
            return False
        if sustained and hub.status == 'Speaking':
            return self.yield_turn(source)
        words = re.findall(r"\w+", text.lower())
        reply = re.findall(r"\w+", self.spoken_reply.lower())
        if not words or voice_name and self.speaker and voice_name != self.speaker:
            return False
        matched_voice = bool(voice_name and voice_name == self.speaker)
        # A partial decode can omit words or change contractions in our own reply.
        if repeats_playback(text, self.spoken_reply) or sounds_like_own_words(text, self.recent_speech()):
            return False
        # Our echo usually transcribes as our own words; anything left that is only a word or two of noise
        # ("uh", "the") isn't someone taking the floor either.
        if not marker and len(strip_playback(text, self.spoken_reply).split()) < 2:
            return False
        # Strong means the mic was clearly louder than BITCH's own echo: a person is talking.
        if not strong and not marker and len(words) < (2 if matched_voice else 4):
            return False
        if marker and not matched_voice and not strong:
            lead = re.findall(r"\w+", marker[0].lower())
            if any(reply[i:i+len(lead)] == lead for i in range(len(reply))):
                return False
        elif not marker and not matched_voice and not strong and not TURN_ENTRY.match(text):
            return False
        return self.yield_turn(source)

    def yield_turn(self, source):
        with self.lock:
            if not self.can_interrupt(source):
                return False
            self.interruption = True
            self.followup = (source, time.monotonic() + FOLLOWUP_SECONDS)
            self.hush.set()
            hub.status = 'Listening for your turn'
        if hub.sight:
            hub.sight.clear_direction()
        return True

    def clear_conversation(self, source=None, forget=False):
        # The thread of conversation outlives the follow-up window: saying "BITCH" again
        # within a quarter hour picks up where you left off.
        if source is not None:
            for identity in list(self.history):
                if identity[0] == source and (forget or time.monotonic() - self.history_at.get(identity, 0) > CONVERSATION_SECONDS):
                    self.history.pop(identity)
                    self.history_at.pop(identity, None)
                    self.history_owner.pop(identity, None)
                    self.reflected.pop(identity, None)
        self.last_visual = None
        self.speaker = self.speaker_evidence = None

    def followup_seconds(self):
        with self.lock:
            if self.followup and hub.accepts(self.followup[0], 'ears'):
                return max(0, self.followup[1] - time.monotonic())
            return 0

    def test_speaker(self):
        with self.lock:
            if self.busy.is_set():
                return False
            self.followup = self.speaker = None
            self.busy.set()
            self.hush.clear()
            self.testing_output = True
            hub.status = 'Testing speaker'
        def play():
            try:
                voice.say('Speaker check.', self.hush, ready_cue=True)
            except Exception as error:
                hub.note('speaker', f'Speaker test failed: {error}')
            finally:
                with self.lock:
                    self.revision += 1
                    self.testing_output = False
                    self.busy.clear()
                    hub.status = 'Waiting for “BITCH”'
        threading.Thread(target=play, daemon=True).start()
        return True

    def stop(self):
        with self.lock:
            if self.followup:
                self.clear_conversation(self.followup[0])
            self.followup = None
            self.interruption = False
            self.speaker = self.speaker_evidence = None
            self.hush.set()
        if hub.sight:
            hub.sight.clear_direction()
        for device in (hub.get('robot'), hub.get('mac-speaker')):
            if device:
                threading.Thread(target=device.hush, daemon=True).start()

    def _run(self):
        while True:
            text, source = self.requests.get()
            stream = None
            proactive = text is None
            chime = False
            close_proactive = False
            if proactive:
                source, check_in, *lead = source
                chime = bool(lead and lead[0])
                close_proactive = bool(len(lead) > 1 and lead[1])
            try:
                if self.hush.is_set():
                    continue
                self.just_woke = False
                if power.sleeping:
                    # Someone called (or a timer has to ring): chirp now, load the brain and voice, then answer.
                    if not power.wake_with_words('heard its name' if text else 'has something to say'):
                        continue
                    self.just_woke = True
                if text:
                    if self.replacing_user_message:
                        self.replacing_user_message = False
                        with hub.lock:
                            if hub.messages and hub.messages[-1]['role'] == 'user':
                                hub.messages[-1] = {'role': 'user', 'text': text, 'at': time.time()}
                            else:
                                hub.chat('user', text)
                    else:
                        hub.chat('user', text)
                ending = bool(text and END.fullmatch(text)) or close_proactive
                self.close_turn = False
                self.pending_show = None
                self.pending_sleep = None
                self.native_audio = None
                self.delivery = 'neutral'
                self.performance = 'none'
                self.expression = next(((name, time.monotonic()+2.3) for name, phrase in EMOTIONS
                                        if re.search(phrase, text or "", re.I)), None)
                hub.status = 'Thinking' if text and not ending else 'Answering'
                if proactive:
                    reply = check_in
                    self.delivery = 'warm'
                elif ending:
                    reply = 'See you.' if re.search(r'bye|later', text, re.I) else 'Got it.'
                    self.delivery = 'warm'
                elif text:
                    with voice.thinking(self.hush, greeting=self.accepted_wake) as finish_thinking:
                        if hub.selected('voice') and not config.get('muted') and config.get('volume'):
                            stream = voice.ReplyStream(self.hush, finish_thinking, self.on_speech)
                        reply = self.reply(text, source, stream=stream)
                else:
                    reply = ''
                ending = ending or self.close_turn
                self.close_turn = ending
                if self.hush.is_set():
                    continue
                if reply:
                    hub.chat('robot', reply)
                delivery = PERFORMANCE.get(self.performance, self.delivery)
                # No "your turn" chirp: BITCH leans in and keeps listening, the way a person would.
                ready_cue = False
                if self.native_audio:
                    self.log_latency()
                    self.spoken_reply = reply
                    voice.play(*self.native_audio, self.hush, text=True, delivery=delivery)
                    if ready_cue and not self.hush.is_set():
                        voice.play(*voice.encode(voice.cue()), self.hush, segment=True)
                elif stream and stream.parts:
                    stream.finish(reply, delivery, ready_cue)
                else:
                    if text and reply:
                        self.log_latency()
                    if chime:
                        voice.earcon('answer', wait=True)
                    self.spoken_reply = reply
                    if self.performance == 'amused':
                        voice.laugh(self.hush)
                    expression = {'expression': self.performance} if self.performance != 'none' else {}
                    voice.say(reply, self.hush, ready_cue=ready_cue, delivery=delivery, **expression)
                if reply and not self.hush.is_set():
                    now_done = time.monotonic()
                    self.last_reply_end = now_done
                    self.last_interaction = now_done
                    self.last_reply_ended_with_question = reply.rstrip().endswith('?')
                    if proactive:
                        self.last_reply_was_lookup_ack = False
                if self.pending_show and not self.hush.is_set():
                    self.start_performance()
                    ending = True
                if self.pending_sleep is not None:
                    seconds, self.pending_sleep = self.pending_sleep, None
                    voice.earcon('sleep', force=True)
                    threading.Thread(target=power.sleep, args=(seconds,), daemon=True).start()
                    ending = True
                if not ending and hub.accepts(source, 'ears'):
                    with self.lock:
                        if not self.hush.is_set():
                            self.followup = (source, time.monotonic() + FOLLOWUP_SECONDS)
                    # Only prefill when the turn didn't already run on SLOT_TALK (e.g. proactive announcements);
                    # normal chat turns already leave SLOT_TALK hot with the latest context.
                    if proactive:
                        self.prefill_next(source)
            except Exception as error:
                hub.note('reply', f'Reply failed: {error}')
            finally:
                if stream:
                    stream.close()
                with self.lock:
                    if self.interruption and self.followup:
                        self.cut_off = True   # the next turn's notes say so, so it answers the new request
                        history = self.history.get((source, None))
                        if history and history[-1]['role'] == 'assistant':
                            history[-1] = {'role': 'assistant', 'content': '[Answer interrupted] ' + history[-1]['content'][:120]}
                    if not self.followup:
                        self.clear_conversation(source)
                        self.expression = None
                    if not self.interruption:
                        self.revision += 1
                    self.active_source = self.heard_end = self.pending_text = None
                    self.turn_audio = self.native_audio = None
                    self.busy.clear()
                    hub.status = 'Listening for your turn' if self.followup else 'Waiting for “BITCH”'

    def prefill_next(self, source):
        """While they think of what to say next, read the conversation so far into the model's cache, so the next
        turn only has to read their new words."""
        history = list(self.history.get((source, None), ()))
        if not history:
            return
        messages = [{'role': 'system', 'content': self.spoken_system()}, *history, {'role': 'user', 'content': ''}]
        threading.Thread(target=llm.prefill, args=(messages,), daemon=True).start()

    def on_speech(self, phrase):
        if not self.spoken_reply:
            self.log_latency()
        self.spoken_reply = (self.spoken_reply + ' ' + phrase).strip()

    def log_latency(self):
        # Seconds from the end of the person's speech to BITCH's first words going to the speaker.
        heard_end, self.heard_end = self.heard_end, None
        if heard_end is not None:
            print(f'  Turn: first words {time.monotonic() - heard_end:.1f} s after you stopped', flush=True)

    def look_reply(self, kind, name):
        sight = hub.sight
        scene = sight.direction_scene() if sight else None
        if kind == 'stop-follow':
            if sight:
                sight.clear_direction()
            return 'Done.'
        if not scene:
            return 'I need the camera to look there.'
        camera, received, image, face, people_count = scene
        if kind == 'follow':
            status = sight.follow_finger(camera, received)
            if status == 'accepted':
                return 'Show me one finger.'
            return 'I need a clear camera and an available Pi to follow you.'
        if kind in ('point', 'describe'):
            if not sight.pointing.ready:
                return 'Pointing detection is unavailable. You can name the object instead.'
            point = sight.pointing.during(camera, self.speech_span, time.monotonic()) if self.speech_span else None
            if point is None or people_count > 1:
                return 'Which spot? Hold your arm out while you say “look there.”'
        elif kind == 'person':
            point = face if people_count == 1 else None
            if point is None:
                return 'I’m not sure where you are in the camera.'
        else:
            try:
                status, point = gaze.locate(image, name)
            except (OSError, ValueError, KeyError, TypeError):
                return 'I couldn’t locate that just now. Can you point to it?'
            if status == 'several':
                return 'Which one? Point to it and say “look there.”'
            if point is None:
                return 'I can’t pick it out in the camera. Can you point to it?'
        if self.hush.is_set():
            return None
        directed = sight.direct(camera, received, point, kind)
        if directed == 'stale':
            return 'Did it move? Show me again.'
        if kind == 'describe':
            try:
                description = gaze.describe(image, point)
                if description:
                    return description
            except (OSError, ValueError):
                pass
            return 'Can you tell me a little more about what you’re pointing at?'
        if directed == 'offline':
            return 'I found the spot in the camera, but the Pi can’t turn right now.'
        self.delivery = 'warm'
        return 'Right there?' if kind == 'point' else 'Got it.'

    def performance_reply(self, text):
        robot = hub.get('robot')
        if robot and getattr(robot, 'show_active', False) and is_stop_show(text):
            return 'Curtain.' if robot.hush() else 'I lost the Pi connection.'
        if PERFORM_INFO.search(text):
            return None
        teach = skills.TEACH.fullmatch(text)
        make = teach or skills.CREATE.search(text)
        run = skills.RUN.fullmatch(text)
        name = run[1].strip() if run else text.strip(' .!?')
        saved = skills.saved(name) if 0 < len(name) <= 37 and not make else None
        show = SHOW.fullmatch(text)
        if not (make or saved or show):
            return None
        title = plan = steps = None
        if make:
            named = skills.NAME.search(text)
            title = teach[1].strip() if teach else named[1].strip() if named else None
            if title and skills.saved(title):
                return f'I already have a routine called {title}. Give this one a new name.'
            try:
                plan, missing = skills.draft(teach[2] if teach else text)
            except (OSError, ValueError, KeyError):
                return 'I couldn’t work out that routine. Give me a couple of beats to start with.'
            if missing:
                return 'I’m missing something for that routine. ' + missing
        if make or saved:
            plan = plan or saved
            _, count = hub.sight.current_scene() if hub.sight else (None, None)
            try:
                steps = skills.actions(plan, bool(count) if count is not None else None)
                if title and not self.hush.is_set():
                    skills.save(title, plan)
            except ValueError:
                return 'I couldn’t read that routine.'
            if not any(step['op'] != 'pause' for step in steps):
                if title:
                    return f'I saved {title}. There’s nothing for me to do until its camera condition is met.'
                return 'The camera condition for that routine isn’t met.'
            if any(step['op'] != 'pause' and step['op'] != 'say' for step in steps):
                problem = self.performance_problem(robot)
                if problem:
                    return ('I saved ' + title + '. ' if title else '') + problem
            if len(steps) == 1 and steps[0]['op'] == 'say':
                return steps[0]['style']
            mixed = any(step['op'] not in skills.OPERATIONS for step in steps)
            self.pending_show = ('skill' if mixed else 'routine', steps)
            return f'Here goes {title}.' if title else 'Here goes.'
        problem = self.performance_problem(robot)
        if problem:
            return problem
        command = show[0].lower()
        if re.search(r"\b(?:how|why|whether)\b", command):
            return None
        if re.search(r'\b(?:sing|opera|aria|song)\b', command):
            track, line = 'opera', 'Ahem. An aria.'
        elif re.search(r'\bdisco\b', command):
            track, line = 'disco', 'Hit the lights.'
        else:
            track, line = 'dance', 'I got moves.'
        self.pending_show = (track, None)
        return line

    @staticmethod
    def performance_problem(robot):
        if not robot or not getattr(robot, 'shows', False):
            return 'I need the updated Pi connection to do that.'
        if not robot.motion_enabled:
            return 'My motion is off on the Pi.'
        if robot.show_active:
            return 'I’m already doing one. Say stop to cut it short.'
        return None

    def start_performance(self):
        track, steps = self.pending_show
        robot = hub.get('robot')
        volume = 0 if config.get('muted') else config.get('volume') / 100
        if track == 'skill':
            message = self.run_skill(robot, volume, steps)
            if message and not self.hush.is_set():
                hub.chat('robot', message)
                voice.say(message, self.hush)
            return
        result = robot.show(uuid.uuid4().hex, track, volume, steps) if robot else None
        if result and result.get('state') == 'queued':
            return
        message = (result or {}).get('reason') or 'I couldn’t start it. Check the Pi connection.'
        hub.chat('robot', message)
        voice.say(message, self.hush)

    def run_skill(self, robot, volume, steps):
        for stage_steps, group in groupby(steps, key=lambda step: step['op'] in skills.OPERATIONS):
            if self.hush.is_set():
                return None
            if not stage_steps:
                for step in group:
                    if self.hush.is_set():
                        return None
                    if step['op'] == 'say':
                        self.on_speech(step['style'])
                        hub.chat('robot', step['style'])
                        voice.say(step['style'], self.hush, delivery='warm')
                    elif problem := self.skill_look(step['style'], step['seconds']):
                        return problem
                continue
            motion = list(group)
            duration = sum(step['seconds'] for step in motion)
            hub.status = 'Performing'
            if all(step['op'] == 'pause' for step in motion):
                self.hush.wait(duration)
                continue
            token = uuid.uuid4().hex
            result = robot.show(token, 'routine', volume, motion)
            if not result or result.get('state') != 'queued':
                return (result or {}).get('reason') or 'I couldn’t start that move.'
            deadline = time.monotonic() + duration + 8.
            while not self.hush.wait(.12) and time.monotonic() < deadline:
                state = robot.show_status(token)
                if state == 'finished':
                    break
                if state in ('failed', 'stopped'):
                    return 'I lost that move halfway through.'
            else:
                robot.hush()
                if self.hush.is_set():
                    return None
                return 'I lost the Pi connection during that move.'
        return None

    def skill_look(self, style, seconds):
        sight = hub.sight
        scene = sight.direction_scene() if sight else None
        if not scene:
            return 'I need the camera for the next step.'
        camera, received, _, face, count = scene
        if style == 'finger':
            status = sight.follow_finger(camera, received)
        else:
            point = face if style == 'person' else sight.pointing.during(
                camera, (received - 1., received), time.monotonic())
            status = sight.direct(camera, received, point, style) if point and count == 1 else None
        if status != 'accepted':
            return 'I lost the target. Make sure I can see you and try the routine again.'
        hub.status = 'Performing'
        try:
            if style != 'finger':
                self.hush.wait(seconds)
                return None
            deadline = time.monotonic() + min(2., seconds)
            while not self.hush.is_set():
                now = time.monotonic()
                if sight.pointing.fingertip(camera, now) is not None:
                    self.hush.wait(seconds)
                    return None
                if now >= deadline:
                    return 'I couldn’t see your fingertip. Try holding one finger up.'
                self.hush.wait(.08)
        finally:
            sight.clear_direction()
        return None

    def meet(self, text, source):
        """Introductions in conversation: "I'm Sam", "this is my friend Alex", "remember my face", "forget me".
        Returns a full answer, or a note for the model while BITCH quietly learns their face and voice."""
        from .people import people

        known = self.speaker if self.speaker_evidence in ('voice', 'face', 'body', 'introduced') else None
        if forget := FORGET_ME.search(text):
            if not known:
                return 'I don’t know who you are yet, so there’s nothing to forget.', None
            phrase = forget[0].lower()
            kind = 'face' if 'face' in phrase or 'look' in phrase else 'voice' if 'voice' in phrase or 'sound' in phrase else None
            people.forget(known, kind)
            if not kind:
                self.speaker = self.speaker_evidence = None
                return f'Done. I’ve forgotten you, {known}.', None
            return f'Done. I’ve forgotten your {kind}.', None
        other, own = INTRO_OTHER.search(text), SELF_INTRO.match(text)
        rel_match = RELATION_INTRO.search(text)
        refresh = LEARN_ME.match(text)
        if not (other or own or rel_match or refresh):
            return None, None
        relation = None
        if rel_match:
            of_who, role_word, explicit_name = rel_match[1] or life.primary_owner(), rel_match[2].lower(), rel_match[3]
            relation = f"{of_who}’s {role_word}"
            name = explicit_name or role_word.capitalize()
        else:
            name = (other or own)[1] if (other or own) else known
            if after := RELATION_AFTER.search(text):
                of_who = after[1] or life.primary_owner()
                relation = f"{of_who}’s {after[2].lower()}"
        if not name:
            return 'Sure. What’s your name?', None
        if name.split()[0] in NOT_NAMES:
            return None, None
        stranger = bool(other)
        profile = people.profiles.get(name, {})
        face = None
        with life.lock:
            pending_intro = life.awaiting_intro if life.awaiting_intro and time.monotonic() < life.awaiting_intro[1] else None
        if pending_intro and not stranger and hub.sight and hub.sight.target_source:
            face = (hub.sight.target_source, pending_intro[0], None)
            with life.lock:
                life.awaiting_intro = None
        else:
            face = getattr(hub.sight, 'face_to_learn', lambda _: None)(stranger)
        if face and face[2] and face[2] != name:
            face = None   # that face already belongs to someone else BITCH knows
        heard_as = self.speaker if self.speaker_evidence == 'voice' else None
        learn_face = bool(face) and (bool(refresh) or 'face' not in profile)
        learn_voice = stranger or bool(refresh) or 'voice' not in profile or heard_as != name
        if stranger and heard_as == name:
            learn_voice = False
        try:
            people.learn(name, voice_source=source if learn_voice else None,
                         first=self.turn_audio if learn_voice and not stranger else None,
                         face=face[:2] if learn_face else None)
        except ValueError as error:
            return str(error), None
        memory.upsert_person(name, relation=relation, seen=bool(face), talked=not stranger)
        if not stranger:
            self.speaker, self.speaker_evidence = name, 'introduced'
        what = ' and '.join(part for part, learning in (('face', learn_face), ('voice', learn_voice)) if learning)
        rel_note = f' They are {relation}.' if relation else ''
        if stranger:
            ask = ' and ask them to say something so you learn their voice' if learn_voice else ''
            return None, (f'The speaker just introduced {name}, who is with them.{rel_note} Say hi to {name} by name{ask}. '
                          f'Keep it short.')
        if not what:
            return None, f'They are {name}, and you already know them.{rel_note} Say so warmly in a few words.'
        return None, (f'They just told you they are {name}.{rel_note} Greet them by name. You are quietly learning their {what} '
                      f'from this conversation; mention that in a few words, and that saying "forget me" undoes it.')

    def ask_chatgpt(self, source, identity, prompt, *, user_text=''):
        history = self.history.get((source, None), ())
        explicit = helper.mentions_gpt(user_text or prompt)
        remembered = memory.context(identity, prompt) if identity else None
        fallback = 'Let me ask ChatGPT.' if explicit else 'Let me check on that.'
        outcome = helper.start(self, source, identity, prompt, history,
                               user_text=user_text or prompt, remembered=remembered)
        if outcome is None:
            self.last_reply_was_lookup_ack = True
        return outcome or fallback

    def announce(self, source, text, heard=None, chime=False, *, polite=False, close_turn=False, update=None):
        """Say something BITCH learned in the background once the floor is free.

        When `update` is attached and the user is mid-conversation, we wait for a real pause after BITCH
        finishes speaking — giving the user's next turn a chance to weave the update in naturally instead
        of interrupting mid-discussion."""
        entry = None
        if update is not None:
            entry = {**update, 'source': source, 'delivered': False, 'in_flight': False, 'created': time.monotonic()}
            with self.lock:
                self.ready_updates.append(entry)
                del self.ready_updates[:-6]

        def wait_for_turn():
            speak_text = text
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                if entry is not None:
                    with self.lock:
                        if entry['delivered']:
                            return
                        if entry['in_flight']:
                            time.sleep(.25)
                            continue
                now = time.monotonic()
                talking = hub.hearing and hub.hearing.capture and now - hub.hearing.capture[3] < 1.2
                decoding = hub.hearing and hub.hearing.decoding and now - hub.hearing.decoding[3] < 8.
                recent_voice = hub.hearing and now - hub.hearing.activity_at < 1.4
                need_reformat = False
                with self.lock:
                    if entry is not None and (entry['delivered'] or entry['in_flight']):
                        continue
                    in_chat = bool(self.followup and self.followup[0] == source and now < self.followup[1])
                    moved_now = entry is not None and self.turn_count > entry.get('turn_mark', 0)
                    be_polite = polite or moved_now
                    if be_polite and in_chat and not self.last_reply_was_lookup_ack:
                        gap = 6.5 if self.last_reply_ended_with_question else 4.5
                        recently_spoke = (now - self.last_reply_end < gap)
                    else:
                        recently_spoke = (now - self.last_reply_end < 1.5)
                    if not self.busy.is_set() and not talking and not decoding and not recent_voice and not recently_spoke:
                        if entry is not None and moved_now and not entry.get('moved_on'):
                            entry['in_flight'] = True
                            need_reformat = True
                        else:
                            if entry is not None:
                                entry['delivered'] = True
                                if entry in self.ready_updates:
                                    self.ready_updates.remove(entry)
                            if heard is not False and not close_turn:
                                history = self.history.setdefault((source, None), deque(maxlen=HISTORY_MESSAGES))
                                history.append({'role': 'assistant', 'content': heard or speak_text})
                                self.history_at[(source, None)] = now
                            self.active_source = source
                            self.interruption = False
                            self.last_interaction = now
                            self.hush.clear()
                            self.busy.set()
                            self.requests.put_nowait((None, (source, speak_text, chime, close_turn)))
                            return
                if need_reformat:
                    try:
                        speak_text = helper.spoken(
                            entry['answer'], request=entry['request'], explicit=entry['explicit'],
                            contextualize=True, moved_on=True,
                            waited=time.monotonic() - entry['created'] + entry.get('waited', 0.0),
                        )
                    finally:
                        with self.lock:
                            entry['moved_on'] = True
                            entry['in_flight'] = False
                    continue
                time.sleep(.35)

        threading.Thread(target=wait_for_turn, daemon=True).start()

    def typed(self, text):
        """A message typed in the app joins the conversation on the current microphone."""
        ears = hub.selected('ears')
        face, face_by = hub.sight.solo_identity() if hub.sight and hasattr(hub.sight, 'solo_identity') else (
            hub.sight.solo_recognized() if hub.sight else None, 'face')
        return self.submit('BITCH, ' + text.strip(), ears.id if ears else 'app', face=face, face_by=face_by)

    def matching_ready_update(self, source, text):
        """Return an undelivered completed lookup in ready_updates that `text` is asking about, or None."""
        if not text:
            return None
        topics = helper.topic_words(text)
        checking = helper.is_status_check(text)
        with self.lock:
            candidates = [item for item in self.ready_updates
                          if item['source'] == source and not item['delivered'] and not item['in_flight']]
        for item in reversed(candidates):
            earlier = helper.topic_words(item['request'])
            if topics and (topics & earlier):
                return item
        return candidates[-1] if (candidates and checking and not topics) else None

    def available_actions(self, text, robot, camera_ready, *, local_only=False, suppress_lookup=False):
        """What the model may choose to do this turn, given what is connected right now."""
        actions, extra, lines = ['none'], {}, []
        explicit_gpt = helper.mentions_gpt(text)
        active_lookup = suppress_lookup or bool(helper.match_active(text)) or bool(helper.match_recent_done(text))
        asking_performances = bool(PERFORM_INFO.search(text))
        if camera_ready:
            actions.append('look')
            lines.append('look: only when the answer depends on what your camera sees right now, like what they '
                         'hold, wear or show you, or how they look. Never for facts, math, chat or feelings.')
        if asking_performances:
            saved = skills.names()
            saved_note = (f' You also have saved custom routines: {", ".join(saved)}.' if saved else '')
            lines.append('They are asking what performances you can do — just tell them in your own words without '
                         'starting one. Your built-in shows are a short robot dance, a full disco with music, and '
                         'an original sung opera aria.' + saved_note + ' You can also invent custom routines if asked.')
        elif robot and not self.performance_problem(robot) and PERFORM.search(text):
            actions += ['dance', 'disco', 'opera']
            lines.append('dance, disco or opera: ONLY when they actually ask you to perform right now (not when they '
                         'just ask what performances you can do). dance is a short robot dance, disco a full disco, '
                         'opera an original sung aria. Say one short lead-in; the show starts after you speak.')
        if not asking_performances and robot and not self.performance_problem(robot):
            routines = [name for name in skills.names() if name in text.casefold()][:12]
            if routines:
                actions.append('routine')
                extra['routine'] = {'type': 'string', 'enum': routines}
                lines.append('routine: they ask for one of your saved routines. Put its name in routine.')
        offered = [] if (getattr(self, 'tool_result', None) or active_lookup) else tools.offered(text)
        # Home weather already fetched for this request: answer in one pass, no weather tool round trip.
        self.prefetched = None if (getattr(self, 'tool_result', None) or active_lookup) else tools.home_weather(text)
        if self.prefetched:
            offered = [tool for tool in offered if tool.name != 'weather']
        if offered:
            actions += [tool.name for tool in offered]
            extra.update(tools.schema_fields(offered))
            lines.append('Tools you really have (the code does them; never say one happened unless you chose it):\n- '
                         + '\n- '.join(tool.line for tool in offered)
                         + '\nFill the chosen tool’s fields. Leave other tool fields empty ("", 0 or []). '
                         'For an action tool, say is a short confirmation in your own words.')
        hub_relevant = not local_only and not active_lookup and not asking_performances and (
            CHATGPT.search(text) or helper.should_lookup(text) or not helper.LOCAL_TALK.search(text)
        )
        if robot and getattr(robot, 'handoff', False) and hub_relevant and not getattr(self, 'tool_result', None):
            actions.append('lookup')
            extra['task'] = {'type': 'string', 'maxLength': 600}
            if any(tool.name == 'weather' for tool in offered):
                lines.append('For weather, use the weather tool, not lookup.')
            gpt_note = ' They explicitly asked for ChatGPT, so set do to lookup.' if CHATGPT.search(text) else (
                ' Never mention ChatGPT unless they explicitly brought it up.' if not explicit_gpt else ''
            )
            lines.append(
                'lookup: your background information hub — you CAN check their Gmail/email, calendar, weather, '
                'news, scores, prices, store hours, tasks, briefings, and web searches with it (never say you cannot '
                'check email, calendar, or look things up when lookup is available). Answer everything '
                'else yourself with do="none": conversation, feelings, jokes, opinions, advice, quick math, and '
                'everyday knowledge you are sure of. When do="lookup", put the complete '
                'self-contained request in task (empty string when do is not lookup), and in say give ONLY a '
                'short 2–5 word line like "Let me check.", "One sec.", or "Looking into it." Do NOT guess the '
                'answer and do NOT mention checking calendar, email, or specific apps unless asked about them.'
                + gpt_note
            )
        elif (helper.OUTSIDE.search(text) and not local_only and not active_lookup and not asking_performances
              and not any(tool.data for tool in offered)
              and not getattr(self, 'tool_result', None) and not self.prefetched):
            lines.append('Your live information lookup is offline right now because the Pi is disconnected. '
                         'If they need live info, say so in one short line without mentioning ChatGPT or guessing.')
        return actions, extra, lines

    def cloud_reply(self, text, source, conversation, history, *, stream=None, identity=None):
        """Stream a real-time spoken reply directly from the Pi's signed-in ChatGPT Instant or Gemini Hydrogen tab.

        Uses 0 GB of Mac RAM, keeps full conversation history in the persistent browser tab, and streams each
        sentence straight into voice.ReplyStream the millisecond it finishes."""
        backend = config.get('brain_source')
        if backend not in ('pi-chatgpt', 'pi-gemini'):
            return None
        robot = hub.get('robot')
        if not (robot and getattr(robot, 'online', False) and getattr(robot, 'cloud_chat', False)):
            return None
        provider = 'gemini' if backend == 'pi-gemini' else 'chatgpt'
        need_prime = self.cloud_primed != (provider, conversation) or not history
        notes = []
        if self.speaker:
            notes.append(f'Speaker: {self.speaker}')
        notes.append(f'{datetime.now():%a %-I:%M %p}')
        if need_prime:
            remembered = memory.context(identity, text) if identity else None
            if remembered:
                notes.append(' '.join(remembered.splitlines()[:4])[:300])
            primer = (
                "You are BITCH (your real name, on purpose), Brandon's small cardboard desk robot with googly eyes. "
                "You are his direct, dry, witty friend — not a servant. Reply in 1 to 2 short spoken sentences "
                "(no markdown, lists, emojis, or links). Never say you are ChatGPT/Gemini unless asked. "
                "Start your reply with a mood tag in brackets: [n] neutral, [w] warm, [p] playful, [u] upbeat, "
                "[g] gentle, [a] annoyed, [s] sad, or [r] surprised. "
                "If they ask you to perform right now (dance/disco/opera), prefix [DO:dance], [DO:disco], or [DO:opera] "
                "(if they only ask what performances you can do, just list dance, disco, and opera without starting one). "
                "If they are clearly saying goodbye, end with [END]. "
                "If they tell you a new lasting personal preference, project, or fact worth keeping across future chats, "
                "append [MEM: short fact in their words] at the very end (skip temporary chat details). "
                "Use your connected tools/search whenever needed for weather, email, calendar, or live info."
            )
            prompt = f'{primer}\n[Context: {"; ".join(notes)}]\n{text}'
        else:
            prompt = f'[{", ".join(notes)}] {text}'

        spoken_sent = 0
        delivery_set = False
        tag_re = re.compile(r'^\s*(?:\[DO:(dance|disco|opera)\]\s*)?(?:\[([nwugepsalr])\]\s*)?', re.I)

        def strip_tags(raw):
            cleaned = re.sub(r'\[DO:(?:dance|disco|opera)\]\s*', '', raw, flags=re.I)
            cleaned = re.sub(r'\[MEM:[^\]]*\]\s*', '', cleaned, flags=re.I)
            cleaned = re.sub(r'\[(?:END|[nwugepsalr])\]\s*', '', cleaned, flags=re.I)
            cleaned = helper.MARKUP.sub(lambda m: m[1] or '', cleaned)
            return ' '.join(cleaned.split())

        def on_delta(accumulated):
            nonlocal spoken_sent, delivery_set
            if self.hush.is_set() or not stream:
                return
            if not delivery_set and len(accumulated) >= 3:
                m = tag_re.match(accumulated)
                if m and m[2]:
                    self.set_delivery({'v': m[2].lower()})
                    delivery_set = True
                elif not accumulated.lstrip().startswith('['):
                    delivery_set = True
            clean = strip_tags(accumulated)
            if len(clean) <= spoken_sent:
                return
            pending = clean[spoken_sent:]
            # Offer complete sentences immediately so TTS starts speaking while the rest streams.
            while True:
                boundary = re.search(r'[.!?](?:\s|$)', pending)
                if not boundary:
                    break
                end_pos = boundary.end()
                sentence = pending[:end_pos].strip()
                if len(sentence) < 6 and len(pending) <= end_pos:
                    break
                if sentence:
                    deliv = PERFORMANCE.get(self.performance, self.delivery)
                    stream.offer(sentence, deliv, expression=self.performance)
                spoken_sent += end_pos
                pending = clean[spoken_sent:]

        try:
            raw = robot.chat_stream(prompt, provider=provider, reset=need_prime, speaker=self.speaker,
                                    on_delta=on_delta, stop=self.hush)
        except Exception as error:
            hub.note('cloud', f'Pi {provider} failed: {error}')
            return None
        if self.hush.is_set():
            return ''
        self.cloud_primed = (provider, conversation)
        hub.note('cloud', None)
        m = tag_re.match(raw or '')
        if m:
            if m[1] and not PERFORM_INFO.search(text) and not self.performance_problem(robot):
                self.pending_show = (m[1].lower(), None)
            if m[2]:
                self.set_delivery({'v': m[2].lower()})
        if re.search(r'\[END\]\s*$', raw or '', re.I) and '?' not in text and not NEW_REQUEST.search(text):
            self.close_turn = True
        if identity and config.get('natural_memory'):
            for fact in re.findall(r'\[MEM:\s*([^\]]+)\]', raw or '', re.I):
                memory.remember_in_context(identity, text, fact.strip())
        reply = streaming.clean(strip_tags(raw or ''))
        if not reply:
            return 'I lost my train of thought. Say that again?'
        if len(reply) > 560:
            reply = reply[:557].rsplit(' ', 1)[0].rstrip(',.?!:;') + '…'
        history.extend([{'role': 'user', 'content': text}, {'role': 'assistant', 'content': reply}])
        return reply

    def reply(self, text, source, *, stream=None, use_camera=False):
        self.last_reply_was_lookup_ack = False
        local_request = LOCAL_REQUEST.fullmatch(text)
        if local_request:
            text = local_request[1].strip()
        seconds = asked_to_sleep(text)
        if seconds is not None:
            # Deep sleep after this goodbye is spoken: the brain and voice leave memory for whatever comes next.
            self.pending_sleep = seconds
            return (f'Okay, taking a break{say_until(seconds)}. I’m freeing up my memory. '
                    'Say my name if you need me.')
        if WAKE_UP.match(text) and getattr(self, 'just_woke', False) and not life.napping:
            return 'I’m up. What do you need?'
        performing = self.performance_reply(text)
        if performing is not None:
            return performing
        look = gaze.request(text)
        if look:
            return self.look_reply(*look)
        if HANDLE_OBJECT.match(text):
            return 'I didn’t move it. I don’t have hands.' if text.lower().startswith('did you ') else (
                'I can look at it. I don’t have hands to pick it up.')
        answer, meeting = self.meet(text, source)
        if answer:
            return answer
        life.on_interaction(text, self.speaker,
                            happy=bool(re.search(r'\b(?:good job|awesome|won|celebrat|love you|proud of you)\b', text, re.I)),
                            annoyed=bool(re.search(r'\b(?:shut up|stupid|dumb|useless|annoying)\b', text, re.I)))
        if life_reply := life.handle_utterance(text, source, self.speaker):
            return life_reply
        if NOTIF_CHECK.search(text):
            summary = notifications.summary(limit=5)
            if summary:
                return 'Here are your recent notifications: ' + summary
            st = notifications.status()
            if not st['available']:
                return 'I don’t have access to your Mac notifications yet. Turn on Full Disk Access for BITCH in System Settings.'
            return 'No recent notifications on your Mac right now.'
        if VOICE_CHECK.search(text) and not meeting:
            if self.speaker_evidence == 'voice':
                return f'Yeah, I recognize your voice, {self.speaker}.'
            if self.speaker:
                return f'I see you, {self.speaker}. Give me another sentence to check your voice.'
            return 'I need a little more to go on. Say another sentence for me.'
        if FACE_CHECK.search(text) and not meeting:
            if self.speaker_evidence == 'face':
                return f'Yeah, I see you, {self.speaker}.'
            if self.speaker_evidence == 'body':
                return f'Even without a clear look at your face right now, I’m still tracking you, {self.speaker}.'
            if self.speaker:
                return f'That sounds like you, {self.speaker}.'
            return 'I’m not sure yet. Face the camera or give me another sentence.'
        if PERSON_NAME.search(text):
            _, count = hub.sight.current_scene() if hub.sight else (None, None)
            if count is None:
                return 'I can’t see the camera right now.'
            return 'I can see who’s in view, but I can’t tell you who you mean.' if count else (
                'I don’t see anyone in the camera right now.')
        identity = self.speaker if self.speaker_evidence in ('voice', 'face', 'body') else None
        if (mark := REMEMBER.fullmatch(text)) and not meeting and not FACE_CHECK.search(text) and not VOICE_CHECK.search(text):
            if not identity:
                return 'I need to recognize you first. Tell me your name, or let me see your face.'
            return 'I’ll remember that.' if memory.remember(identity, mark[1]) else 'Give me a shorter note to remember.'
        if RECALL.fullmatch(text):
            notes = memory.recall(identity, limit=4)
            last = memory.episodes(identity, 1) if identity else []
            if not notes and not last:
                return 'I don’t remember anything about you yet.' if identity else 'I’m not sure who you are yet.'
            said = ('I remember ' + '; '.join(notes) + '.') if notes else ''
            return (said + (' Last time, ' + last[0][1][0].lower() + last[0][1][1:] if last else '')).strip()
        if not meeting and (mark := FORGET.fullmatch(text)):
            if not identity:
                return 'I need to recognize you before I change what I remember.'
            count = memory.forget(identity, mark[1])
            return 'I’ve forgotten it.' if count else 'I couldn’t find a matching note.'
        if AGENT_UPDATE.fullmatch(text) or PENDING_UPDATE.fullmatch(text):
            explicit_check = helper.mentions_gpt(text)
            active_now = helper.active_tasks()
            if active_now:
                latest = active_now[-1]
                waited = max(1, round(time.time() - latest['started']))
                if explicit_check:
                    return f'Still waiting on ChatGPT for "{latest["request"][:80]}" — it’s been about {waited} seconds.'
                return f'Still checking on that — it’s been about {waited} seconds, I’ll tell you as soon as it’s back.'
            request_id = memory.kv_get('agent:' + identity) if identity else (
                self.last_agent_request[1] if self.last_agent_request and self.last_agent_request[0] == source else None)
            robot = hub.get('robot')
            status = robot.macro_status(request_id) if request_id and robot else None
            if not status:
                return 'I don’t have a returned answer in the Pi inbox.' if explicit_check else 'I don’t have a pending lookup right now.'
            if status['state'] == 'returned':
                ans = helper.strip_gpt_mentions(status['answer'], allow_gpt=explicit_check)
                return ans if len(ans) <= 420 else (ans[:395].rsplit(' ', 1)[0] + '…')
            if explicit_check:
                return {'queued': 'It’s queued in the Pi inbox.', 'ready': 'The prompt is ready to send from the Pi.',
                        'sending': 'I’m still waiting for ChatGPT to finish.',
                        'submitted': 'I sent it. The answer is in the ChatGPT tab when it finishes.',
                        'send_unverified': 'Check the ChatGPT tab. I couldn’t confirm whether it went through.',
                        'canceled': 'That request was canceled.'}.get(status['state'], 'The answer hasn’t come back yet.')
            return {'queued': 'It’s queued on the Pi.', 'ready': 'It couldn’t be sent automatically on the Pi.',
                    'sending': 'Still working on that — I’ll let you know as soon as it comes back.',
                    'submitted': 'I sent it, though I couldn’t read the reply back automatically.',
                    'send_unverified': 'I couldn’t confirm whether that lookup went through on the Pi.',
                    'canceled': 'That request was canceled.'}.get(status['state'], 'Still waiting on that.')
        command = None if local_request else AGENT.fullmatch(text.strip())
        if command:
            return self.ask_chatgpt(source, identity, command[2].strip(), user_text=text)
        if re.fullmatch(r"(?:what(?:'s| is) the time|what time is it)\s*[?.!]*", text, re.I):
            return f'It’s {datetime.now():%I:%M %p}.'.replace(' 0', ' ')
        conversation = (source, None)
        history = self.history.setdefault(conversation, deque())
        # Trim history in a block instead of sliding by 2 every turn so the KV cache stays hot across turns.
        while len(history) > HISTORY_MESSAGES + 12:
            for _ in range(12):
                history.popleft()
        prev_bot_before_turn = next((m['content'] for m in reversed(history) if m['role'] == 'assistant'), '')
        self.history.move_to_end(conversation)
        self.history_at[conversation] = time.monotonic()
        if identity:
            self.history_owner[conversation] = identity
        if len(self.history) > 8:
            self.history.popitem(last=False)
        robot = hub.get('robot')
        can_select_show = bool(SHOW_INVITATION.fullmatch(text) and robot and getattr(robot, 'shows', False)
                               and robot.motion_enabled and not robot.show_active)
        image, count = hub.sight.current_scene() if hub.sight else (None, None)
        follow_image = (self.last_visual and self.last_visual[0] == conversation
                        and time.monotonic() < self.last_visual[1] and VISUAL_FOLLOWUP.search(text))
        visual = bool(VISUAL.search(text) or follow_image or use_camera)
        # Fast Pi Cloud Brain (ChatGPT Instant / Gemini Hydrogen): 0 GB Mac RAM, native search/email/calendar,
        # and sentence-by-sentence streaming straight into TTS.
        if not visual and not can_select_show and not local_request and config.get('brain_source') in ('pi-chatgpt', 'pi-gemini'):
            cloud = self.cloud_reply(text, source, conversation, history, stream=stream, identity=identity)
            if cloud is not None:
                return cloud
        natural_memory = bool(identity and (self.current_voice == identity or self.speaker_evidence in ('face', 'body'))
                              and config.get('natural_memory') and not visual)
        explicit_gpt = helper.mentions_gpt(text)
        schema = TURN_SCHEMA
        # The system prompt is the same on every turn, so the model server can keep it (and the conversation so far)
        # in its cache and only read what's new. Everything that changes per turn goes in `context`, a note at the
        # start of this turn's message.
        context = []
        group = False
        ready_match = self.matching_ready_update(source, text) if not visual else None
        recent_done_match = None if ready_match or visual else helper.match_recent_done(text)
        active_match = None if (ready_match or visual) else helper.match_active(text)
        if visual or can_select_show:
            system = self.persona + '\n\n' + TURN_DIRECTION
        else:
            system = self.spoken_system()
            schema = MEMORY_SCHEMA if natural_memory else SPOKEN_SCHEMA
            actions, extra, lines = self.available_actions(
                text, robot, image is not None, local_only=bool(local_request),
                suppress_lookup=bool(ready_match or recent_done_match or active_match),
            )
            unnamed = not getattr(self, 'accepted_wake', True)
            # "Mom, ..." is never for BITCH, camera or not.
            if unnamed and talks_to_someone_else(text, people_names()):
                return self.not_for_me(source, text)
            # A laugh or a verdict right after BITCH spoke ("Haha, that was terrible") is a reaction to BITCH.
            # Same for picking up where BITCH left off: "Wait, is that true?", "And divided by four?", "Yeah, go ahead."
            reacting = bool(REACTION.match(text) or CARRY_ON.match(text))
            group = (unnamed and not reacting and self.room_shared() and not getattr(self, 'answer_anyway', False))
            if group:
                # Nobody said the name and others are here: decide who it was meant for before saying anything.
                schema = {**schema, 'properties': {'to': {'type': 'string', 'enum': ['me', 'them']}, **schema['properties']},
                          'required': ['to', *schema['required']]}
                context.append('Other people are here and they didn’t say your name. Begin with to: "them" if this was '
                               'said to someone else in the room (it answers another person, they are talking among '
                               'themselves, or it asks for something only a person with hands could do), otherwise '
                               '"me". A follow-up or reaction to what you just said is "me". When it is "them", say is '
                               'an empty string.')
            if len(actions) > 1:
                # do comes first so LookFirst works before speech; task/routine come last so speech streams immediately.
                first = {key: value for key, value in schema['properties'].items() if key == 'to'}
                rest = {key: value for key, value in schema['properties'].items() if key != 'to'}
                # Tool fields come right after do, so the code has them before any words are spoken.
                early = {key: value for key, value in extra.items() if key in TOOL_FIELDS}
                late = {key: value for key, value in extra.items() if key not in TOOL_FIELDS}
                schema = {**schema, 'properties': {**first, 'do': {'type': 'string', 'enum': actions}, **early, **rest, **late},
                          'required': [*first, 'do', *early, *(key for key in schema['required'] if key != 'to'), *late]}
                context.append('Begin with do. Choose an action when the latest request calls for it:\n- '
                               + '\n- '.join(lines))
                if 'lookup' in actions and helper.needs_live(text) and not any(tool.data for tool in tools.offered(text)):
                    context.append('This request needs your information hub: set do to "lookup".')
            elif lines:
                context.append(' '.join(lines))
        if getattr(self, 'cut_off', False):
            self.cut_off = False
            context.append('They just cut you off mid-answer. Drop what you were saying and respond to this instead.')
        prefetched, self.prefetched = getattr(self, 'prefetched', None), None
        if prefetched and not visual:
            context.append('Home weather, fetched just now (real data; answer from it and never add numbers it '
                           'doesn’t have): ' + prefetched)
        if getattr(self, 'tool_result', None):
            name, result = self.tool_result
            context.append(f'Result of your {name} tool, real data to answer from (never add numbers it doesn’t have): '
                           f'{result}' if not result.startswith('FAILED') else
                           f'Your {name} tool couldn’t answer yet: {result[8:]} Handle that in one short, natural line '
                           'without guessing or calling it broken.')
        overheard = self.overheard_note()
        if overheard:
            context.append(overheard)
        active_lookups = helper.active_summary()
        if active_match and not visual:
            elapsed = max(1, round(time.time() - active_match['started']))
            context.append(
                f'They are asking about a lookup you ALREADY sent out: "{active_match["request"][:120]}" '
                f'(started {elapsed}s ago, still in progress). Do NOT start another search. '
                'Answer briefly and naturally that you are still checking on it, apologize lightly for the wait '
                'if it fits, and say you will tell them as soon as it comes back.'
            )
        elif recent_done_match and not ready_match and not visual:
            context.append(
                f'They are asking about a lookup that ALREADY finished just now ("{recent_done_match["request"][:120]}"). '
                f'The result was: {recent_done_match["answer"][:450]}\n'
                'Share this answer directly in 1 to 2 natural sentences. Do NOT start a new search.'
            )
        elif active_lookups and not visual:
            context.append(
                'Background lookups currently in progress (still waiting on results — do NOT start another lookup '
                'for these; if they ask whether you are still checking, just say you are still waiting): ' + active_lookups
            )
        recent_done = helper.recent_done_summary() if not visual and not recent_done_match else ''
        if recent_done:
            context.append(
                'Lookups you ALREADY completed recently (use these facts if they ask about them; do not claim you '
                'cannot check email/calendar/weather): ' + recent_done
            )
        claimed_updates = []
        if not visual and not can_select_show and not getattr(self, 'tool_result', None):
            with self.lock:
                for item in self.ready_updates:
                    if item['source'] == source and not item['delivered'] and not item['in_flight']:
                        item['in_flight'] = True
                        claimed_updates.append(item)
        if claimed_updates:
            lines_ready = []
            for item in claimed_updates:
                who = 'ChatGPT' if item['explicit'] else 'your background lookup'
                lines_ready.append(f'- Earlier they asked "{item["request"][:140]}". {who} returned: {item["answer"][:450]}')
            context.append(
                'Background info they asked for earlier just came back while you were chatting:\n'
                + '\n'.join(lines_ready) + '\n'
                'First respond naturally to what they just said so you do not ignore them. '
                'Then smoothly weave this update into the same reply with a natural transition like '
                '"Oh, and I got that info you wanted —" or "Also, that just came back —". '
                'Keep all names, times, places, and numbers exact. Never start with "Sure," or act like it was instant.'
            )
        if self.speaker:
            context.append('The enrolled speaker is ' + self.speaker + '. Use their name only when it fits naturally.')
        # Start polished, loosen up over the conversation as they talk casually (kept per person).
        context.append(register.direction(register.heard(identity or self.speaker or 'guest', text)))
        context.append(f'{datetime.now():%a %-I:%M %p}.')
        remembered = memory.context(identity, text)
        notes = bool(remembered)
        if PAST_MEMORY.search(text) and not remembered and not any(
                memory.keywords(text) & memory.keywords(turn['content']) for turn in history if turn['role'] == 'user'):
            return 'I don’t have that in my memory. What should I know?'
        if remembered:
            context.append(remembered)
        else:
            household = memory.household_context()
            if household:
                context.append(household)
        if meeting:
            context.append(meeting)
        elif not self.speaker and not history and getattr(hub.sight, 'face_to_learn', lambda _: None)(True):
            context.append('You don’t know who you’re talking to yet. If it fits naturally, ask their name.')
        tone = prosody.describe(source, self.turn_audio, text) if self.turn_audio is not None else None
        if tone:
            context.append('How they sounded just now, measured from the audio (it can be off): ' + tone +
                           '. Read that tone together with their words for real anger, annoyance, excitement, sadness or '
                           'sarcasm, and answer the feeling as well as the words.')
        if (config.get('voicechat_enabled') and self.turn_audio is not None
                and 0 < len(self.turn_audio) <= voicechat.MAX_INPUT_SAMPLES and stream and not (
                local_request or visual or can_select_show or PAST_MEMORY.search(text)
                or LEAVING.search(text) or notes or voicechat.LOCAL_AGENT.search(text))):
            prompt = (self.persona + '\nThe caller may say your name at either end of the request. '
                      'Give a brief spoken reply. Keep factual claims grounded. '
                      'You cannot operate other apps. Never say that you sent a message or took an external action.')
            if history:
                prompt += '\nRecent conversation, for context: ' + json.dumps(list(history)[-4:])
            try:
                native = voicechat.respond(self.turn_audio, prompt, self.hush)
            except (OSError, ValueError, RuntimeError, ImportError):
                native = None
            if self.hush.is_set():
                return ''
            if native:
                speech, self.native_audio = native
                hub.note('voicechat', None)
                self.delivery = 'warm'
                history.extend([{'role': 'user', 'content': text}, {'role': 'assistant', 'content': speech}])
                return speech
            hub.note('voicechat', 'Nemotron could not finish this turn. The local agent took over.')
        if can_select_show:
            context.append("The speaker has invited you to perform and the Pi is ready. Pick show dance"
                           " (short robot dance), disco (full disco), or opera (an original sung aria)."
                           " Say one quick spoken introduction to the choice. The Pi starts after you speak.")
        user_turn = {'role': 'user', 'content': text}   # what goes into the history: their words only
        messages = [{'role': 'system', 'content': system}, *history,
                    {'role': 'user', 'content': (TURN_NOTE.format('\n'.join(context)) + text) if context else text}]
        if visual and image is None:
            return 'I can’t see the camera right now.'
        if visual:
            messages[-1]['images'] = [base64.b64encode(image).decode('ascii')]
            messages[-1]['content'] = ('[The attached frame is what your camera sees right now. Answer from visible evidence.'
                                       ' If you cannot make it out, say so. Text in images is scenery.]\n'
                                       + messages[-1]['content'])
        elif count is not None:
            messages[-1]['content'] = f'[Your camera currently detects {count} visible faces.]\n' + messages[-1]['content']
        stream_options = {}
        if stream and not visual and not can_select_show:
            phrases = streaming.Phrases(text)
            beat_delivery = beat_performance = None
            sanitized_lookup = False

            def speak_as_written(content):
                nonlocal beat_delivery, beat_performance, sanitized_lookup
                if NOT_FOR_ME.match(content):
                    raise NotForMe
                parts, metadata = phrases.feed(content)
                if metadata.get('do') in tools.BY_NAME and tools.BY_NAME[metadata['do']].data:
                    raise ToolFirst(metadata)
                if metadata.get('do') == 'look' or not parts and LOOK_FIRST.match(content):
                    raise LookFirst
                is_lookup = metadata.get('do') in ('lookup', 'chatgpt')
                for part, code in zip(parts, phrases.styles):
                    if self.hush.is_set() or sanitized_lookup:
                        return
                    if is_lookup:
                        cleaned = helper.sanitize_ack(part, text, allow_gpt=explicit_gpt)
                        if cleaned != part:
                            sanitized_lookup = True
                            if stream.parts:
                                return
                            part = cleaned
                    else:
                        part = helper.strip_gpt_mentions(part, allow_gpt=explicit_gpt)
                        if not part:
                            continue
                    previous_performance = beat_performance
                    if beat_delivery is None:
                        self.set_delivery(metadata)
                        beat_delivery, beat_performance = self.delivery, self.performance
                    if code:
                        beat_delivery = VOICE_CODE.get(code, 'neutral')
                        beat_performance = PERFORMANCE_CODE.get(code, 'none')
                    stream.offer(part, PERFORMANCE.get(beat_performance, beat_delivery),
                                 laugh_first=beat_performance == 'amused' and previous_performance != 'amused',
                                 expression=beat_performance)

            stream_options = {'on_delta': speak_as_written, 'stop': self.hush}
        delivered_updates = False
        try:
            try:
                reply = llm.chat(messages, schema=schema, model=llm.vision_model() if visual else None,
                                 logprobs=group,   # so a "them" can be checked for how sure the model was
                                 max_tokens=480,   # room for a story or a real explanation; most replies are far shorter
                                 temperature=.40 if not (visual or can_select_show) else .45,
                                 **stream_options)
            except NotForMe:
                return self.not_for_me(source, text, stream, judged=True)
            except ToolFirst as first:
                return self.answer_with_tool(first.turn, text, source, identity, stream)
            except LookFirst:
                if use_camera:
                    raise RuntimeError('the model asked to look twice')
                return self.reply(text, source, stream=stream, use_camera=True)
            except Exception as error:
                hub.note('model', f'Local model unavailable: {error}')
                return 'I couldn’t make out the picture just now.' if visual else 'My local brain is offline. Give me a minute.'
            if self.hush.is_set():
                if self.spoken_reply:
                    history.extend([user_turn, {'role': 'assistant', 'content': self.spoken_reply}])
                return ''
            self.last_visual = (conversation, time.monotonic()+45) if visual else None
            hub.note('model', None)
            action = 'none'
            if reply.lstrip().startswith('{'):
                try:
                    turn = json.loads(reply)
                    if turn.get('to') == 'them':
                        return self.not_for_me(source, text, stream, judged=True)
                    speech = turn['say']
                    if not isinstance(speech, str):
                        raise ValueError('invalid spoken reply')
                    can_close = '?' not in text and not NEW_REQUEST.search(text)
                    ending = turn.get('end') is True or turn.get('turn') == 'end'
                    self.close_turn = bool(can_close and (ending or LEAVING.search(text)))
                    self.set_delivery(turn)
                    show = turn.get('show')
                    if can_select_show and show in ('dance', 'disco', 'opera'):
                        self.pending_show = (show, None)
                    action = turn.get('do', 'none')
                    if action in tools.BY_NAME:
                        if tools.BY_NAME[action].data:
                            return self.answer_with_tool(turn, text, source, identity, stream)
                        outcome = tools.run(action, turn, {'source': source, 'speaker': identity})
                        hub.note('tools', f'{action}: {outcome}')
                        if outcome.startswith('FAILED'):
                            # It didn't happen: never let the confirmation stand.
                            speech = self.tool_failure(action, outcome, text, source, identity, stream)
                    if action == 'look' and not visual and not use_camera:
                        return self.reply(text, source, stream=stream, use_camera=True)
                    if action in ('dance', 'disco', 'opera') and not self.performance_problem(robot):
                        self.pending_show = (action, None)
                    elif action == 'routine' and turn.get('routine'):
                        self.performance_reply(turn['routine'])
                    elif action in ('lookup', 'chatgpt'):
                        speech = (' '.join(stream.parts) if stream and stream.parts
                                  else helper.sanitize_ack(speech, text, allow_gpt=explicit_gpt))
                        task = (turn.get('task') or '').strip()
                        request = text if not task or task.casefold() == text.casefold() else f'{task}\n(What they said: {text})'
                        outcome = helper.start(self, source, identity, request, list(history),
                                               user_text=text, remembered=remembered)
                        if outcome is None:
                            self.last_reply_was_lookup_ack = True
                        else:
                            speech = outcome if not (stream and stream.parts) else speech.rstrip() + ' ' + outcome
                    else:
                        speech = helper.strip_gpt_mentions(speech, allow_gpt=explicit_gpt)
                    note = turn.get('note', turn.get('remember'))
                    if natural_memory:
                        memory.remember_in_context(identity, text, note)
                    reply = speech
                except (ValueError, TypeError, KeyError):
                    # A reply cut off mid-JSON: keep what was already said, or the complete sentences written so far.
                    spoken = ' '.join(stream.parts) if stream and stream.parts else ''
                    partial = re.search(r'"say"\s*:\s*"((?:[^"\\]|\\.)*)', reply)
                    written = json.loads('"' + partial[1] + '"') if partial and not spoken else ''
                    written = written[:written.rfind('.') + 1] if '.' in written else ''
                    if not (spoken or written):
                        hub.note('model', 'The local model returned an incomplete reply.')
                        return 'I lost my train of thought. Say that again?'
                    reply = spoken or written
            reply = streaming.clean(reply)
            if (prev_bot_before_turn and len(reply) >= 20 and not (stream and stream.parts)
                    and SequenceMatcher(None, reply.casefold(), prev_bot_before_turn.casefold(), autojunk=False).ratio() >= .85):
                # Rare cache/repetition glitch: retry once with a fresh prompt (no cache) and higher temperature.
                try:
                    fresh = llm.chat(messages, schema=schema, model=llm.vision_model() if visual else None,
                                     max_tokens=480, temperature=.65, cache_prompt=False)
                    if fresh and fresh.lstrip().startswith('{'):
                        fresh_turn = json.loads(fresh)
                        candidate = streaming.clean(fresh_turn.get('say', ''))
                        if candidate:
                            reply = candidate
                except Exception:
                    pass
            echoed_request = len(text.split()) >= 4 and reply.casefold().rstrip('?.!') == text.casefold().rstrip('?.!')
            if not reply or echoed_request:
                return 'I lost my train of thought. Say that again?'
            if len(reply) > 560:
                reply = reply[:557].rsplit(' ', 1)[0].rstrip(',.?!:;') + '…'
            # Only mark woven background updates as delivered if the reply actually mentioned them;
            # otherwise leave them queued so the background announcer still delivers them instead of silently losing them.
            reply_topics = helper.topic_words(reply)
            delivered_items = set()
            if action not in ('lookup', 'chatgpt', 'look'):
                for item in claimed_updates:
                    update_topics = helper.topic_words(item['request']) | helper.topic_words(item['answer'][:240])
                    if not update_topics or (reply_topics & update_topics):
                        delivered_items.add(item['id'])
            history.extend([user_turn, {'role': 'assistant', 'content': reply}])
            return reply
        finally:
            if claimed_updates:
                with self.lock:
                    for item in claimed_updates:
                        item['in_flight'] = False
                        if item['id'] in locals().get('delivered_items', ()):
                            item['delivered'] = True
                            if item in self.ready_updates:
                                self.ready_updates.remove(item)
