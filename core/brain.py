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
from .hub import hub
from .life import life

# Whisper spells the name a few ways, and hears "Yo BITCH" as "No/You/My bitch" across a room.
NAME = r"(?:bitch|bich|bish|bitsch|b\.?-?\s?i\.?-?\s?t\.?-?\s?c\.?-?\s?h\.?)"
WAKE = re.compile(
    r"^\s*(?:(?:(?:hey|hi|hello|okay|ok|yo|you|no|oh|so|um|uh|well|my|alright|now)\b[\s,!.:-]+){1,2}"
    r"(?:mitch|pitch|fitch|witch|britch|beach|beech)\b|"
    r"(?:(?:hey|hi|hello|okay|ok|yo|you|no|oh|so|um|uh|well|my|alright|now)\b[\s,!.:-]+){0,2}" + NAME + r"(?![\w'’]))"
    r"[\s,!.?:;—-]*", re.I)
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
STOP_SHOW = re.compile(r"^\s*(?:please )?(?:stop(?: (?:the |that )?(?:show|dance|routine|singing|music|performance))?"
                       r"|that's enough|cut the music)[.!? ]*$", re.I)
SHOW_INVITATION = re.compile(
    r"^(?:(?:can|could|would|will) you\s+|please\s+)*(?:show off|entertain (?:me|us)|"
    r"give (?:me|us) a (?:show|performance)|do something (?:theatrical|dramatic|musical))\b[^?]*(?:\?|$)", re.I)
PERFORMANCE = {'sad': 'gentle', 'annoyed': 'emphatic', 'amused': 'playful', 'surprised': 'upbeat'}
VOICE_CODE = {'n': 'neutral', 'w': 'warm', 'u': 'upbeat', 'g': 'gentle', 'e': 'emphatic', 'p': 'playful'}
PERFORMANCE_CODE = {'s': 'sad', 'a': 'annoyed', 'l': 'amused', 'r': 'surprised'}
FOLLOWUP_SECONDS = 24
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
        return text[match.end():].strip()
    if match := END_WAKE.search(text):
        before = text[:match.start()].rstrip()
        # "about that BITCH" is a mention, but a comma or a question at the end makes it an address.
        if before and (not MENTION.search(before) or ',' in match[1] or '?' in match[2]):
            ending = match[2].strip() if before[-1] not in '.!?' else ''
            return before + ending
    if match := MID_WAKE.search(text):
        return (text[:match.start()] + ' ' + text[match.end():]).strip()
    return None


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


def talks_to_someone_else(text, names=()):
    """True when the line calls on another person by name or family word ("Mom, ...", "..., right honey?")."""
    known = [re.escape(name) for name in names if name and name.casefold() != 'bitch']
    who = r"(?:" + FAMILY_WORDS + (r"|" + "|".join(known) if known else "") + r")"
    return bool(re.search(r"(?:^|[.!?]\s+)(?:(?:hey|okay|ok|so|thanks|and|oh)[,!.]?\s+)?" + who + r"\s*[,!?]", text, re.I)
                or re.search(r",\s*(?:right\s+|okay\s+|ok\s+)?" + who + r"\s*[.!?]*$", text, re.I))


def strip_playback(text, spoken):
    """Drop BITCH's own words from the front of a talk-over transcript ("A small robot. Wait, make it..." -> "Wait, make it...").

    The microphone hears the speaker for a moment before the person's words win; only a run of two or more words
    that BITCH really said, at the very start, is removed."""
    reply = re.findall(r"\w+", (spoken or '').casefold())
    tokens = list(re.finditer(r"[\w'’]+", text))
    if len(tokens) < 3 or len(reply) < 2:
        return text
    words = [re.sub(r"['’]", '', t[0].casefold()) for t in tokens]
    reply = [re.sub(r"['’]", '', w) for w in reply]
    best = 0
    for start in range(len(reply)):
        n = 0
        while n < len(words) and start + n < len(reply) and words[n] == reply[start + n]:
            n += 1
        best = max(best, n)
    if best < 2 or best >= len(words) - 1:
        return text
    return text[tokens[best].start():].lstrip(' ,.;:!?—-')
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
Answer their mood with your voice code: meet excitement with u or p, sadness with g, sarcasm with a dry p or a, and real anger with a steady n or w. If the reply changes mood, put a new voice code in square brackets just before the next sentence, for example "Oh, please. [p] Fine, you win." Keep one voice when the mood stays the same.
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
CHATGPT = re.compile(r"\b(?:ask|get|have|tell|let|use|send|check with|run (?:it|this|that) by)\b[^.?!]{0,30}\bchat ?gpt\b"
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
        self.acknowledged = None
        self.active_source = None
        self.interruption = False
        self.spoken_reply = ''
        self.last_interaction = time.monotonic()
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

    def not_for_me(self, source, text):
        """Stay quiet for a line meant for someone else, but keep it in mind for the next turn."""
        self.overheard.append((time.monotonic(), text))
        hub.note('model', None)
        return ''

    def overheard_note(self):
        now = time.monotonic()
        lines = [text for at, text in self.overheard if now - at < 90]
        return ('Said between other people in the room in the last minute or so (not to you): '
                + ' / '.join(json.dumps(line, ensure_ascii=False) for line in lines[-3:])) if lines else None

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
            show_stop = robot and getattr(robot, 'show_active', False) and STOP_SHOW.fullmatch(text)
            alarm_stop = life.alarm_ringing and life.is_alarm_reply(text)
            return bool(wake_request(text) is not None or show_stop or alarm_stop or
                        self.followup and self.followup[0] == source and heard_at < self.followup[1])

    def submit(self, text, source, heard_at=None, *, name=None, face=None, face_by='face', speech_end=None, audio=None):
        """Admit one addressed utterance. Setup and unrelated speech never reach the model."""
        with self.lock:
            if self.busy.is_set():
                return False
            now = time.monotonic() if heard_at is None else heard_at
            wake = wake_request(text)
            followup = self.followup and self.followup[0] == source and now < self.followup[1]
            robot = hub.get('robot')
            show_stop = robot and getattr(robot, 'show_active', False) and STOP_SHOW.fullmatch(text)
            alarm_stop = life.alarm_ringing and life.is_alarm_reply(text)
            if wake is None and not show_stop and not alarm_stop and (not followup or name and self.speaker and name != self.speaker):
                return False
            continuation = self.continuation
            self.continuation = None
            if continuation and continuation[0] == source and now < continuation[2]:
                text = continuation[1] + ' ' + (wake if wake is not None else text.strip())
                wake = None
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
        with self.lock:
            if not self.can_continue(source):
                return False
            self.continuation = (source, self.pending_text, time.monotonic() + 8)
            self.interruption = True
            self.followup = (source, time.monotonic() + FOLLOWUP_SECONDS)
            self.hush.set()
            hub.status = 'Listening for your turn'
            if hub.messages and hub.messages[-1]['role'] == 'user':
                hub.messages.pop()
        return True

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
                if hub.status == 'Speaking' and not self.busy.is_set():
                    hub.status = 'Listening for your turn'

            threading.Thread(target=answer, daemon=True).start()
        return True

    def interrupt(self, source, text, voice_name=None, strong=False, sustained=False):
        if not self.can_interrupt(source):
            return False
        marker = INTERRUPT.match(text)
        if hub.status == 'Performing' and not marker:
            return False
        if sustained and hub.status == 'Speaking':
            return self.yield_turn(source)
        words = re.findall(r"\w+", text.lower())
        reply = re.findall(r"\w+", self.spoken_reply.lower())
        if not words or voice_name and self.speaker and voice_name != self.speaker:
            return False
        matched_voice = bool(voice_name and voice_name == self.speaker)
        # A partial Whisper decode can omit words or change contractions in our own reply.
        if repeats_playback(text, self.spoken_reply):
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
                if text:
                    hub.chat('user', text)
                ending = bool(text and END.fullmatch(text)) or close_proactive
                self.close_turn = False
                self.pending_show = None
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
                if self.pending_show and not self.hush.is_set():
                    self.start_performance()
                    ending = True
                if not ending and hub.accepts(source, 'ears'):
                    with self.lock:
                        if not self.hush.is_set():
                            self.followup = (source, time.monotonic() + FOLLOWUP_SECONDS)
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
        if robot and getattr(robot, 'show_active', False) and STOP_SHOW.fullmatch(text):
            return 'Curtain.' if robot.hush() else 'I lost the Pi connection.'
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
        return helper.start(self, source, identity, prompt, history,
                            user_text=user_text or prompt, remembered=remembered) or fallback

    def announce(self, source, text, heard=None, chime=False, *, polite=False, close_turn=False):
        """Say something BITCH learned in the background once the floor is free."""
        def wait_for_turn():
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                talking = hub.hearing and hub.hearing.capture and time.monotonic() - hub.hearing.capture[3] < 1.2
                decoding = hub.hearing and hub.hearing.decoding and time.monotonic() - hub.hearing.decoding[3] < 8.
                with self.lock:
                    recently_spoke = polite and self.followup and (time.monotonic() - self.last_interaction < 3.5)
                    if not self.busy.is_set() and not talking and not decoding and not recently_spoke:
                        if heard is not False and not close_turn:
                            history = self.history.setdefault((source, None), deque(maxlen=HISTORY_MESSAGES))
                            history.append({'role': 'assistant', 'content': heard or text})
                            self.history_at[(source, None)] = time.monotonic()
                        self.active_source = source
                        self.interruption = False
                        self.last_interaction = time.monotonic()
                        self.hush.clear()
                        self.busy.set()
                        self.requests.put_nowait((None, (source, text, chime, close_turn)))
                        return
                time.sleep(.4)

        threading.Thread(target=wait_for_turn, daemon=True).start()

    def typed(self, text):
        """A message typed in the app joins the conversation on the current microphone."""
        ears = hub.selected('ears')
        face, face_by = hub.sight.solo_identity() if hub.sight and hasattr(hub.sight, 'solo_identity') else (
            hub.sight.solo_recognized() if hub.sight else None, 'face')
        return self.submit('BITCH, ' + text.strip(), ears.id if ears else 'app', face=face, face_by=face_by)

    def available_actions(self, text, robot, camera_ready, *, local_only=False):
        """What the model may choose to do this turn, given what is connected right now."""
        actions, extra, lines = ['none'], {}, []
        explicit_gpt = helper.mentions_gpt(text)
        if camera_ready:
            actions.append('look')
            lines.append('look: only when the answer depends on what your camera sees right now, like what they '
                         'hold, wear or show you, or how they look. Never for facts, math, chat or feelings.')
        if robot and not self.performance_problem(robot) and PERFORM.search(text):
            actions += ['dance', 'disco', 'opera']
            lines.append('dance, disco or opera: they ask you to dance, party, sing or perform in any words. '
                         'dance is a short robot dance, disco a full disco, opera an original sung aria. '
                         'Say one short lead-in; the show starts after you speak.')
        if robot and not self.performance_problem(robot):
            routines = [name for name in skills.names() if name in text.casefold()][:12]
            if routines:
                actions.append('routine')
                extra['routine'] = {'type': 'string', 'enum': routines}
                lines.append('routine: they ask for one of your saved routines. Put its name in routine.')
        offered = [] if getattr(self, 'tool_result', None) else tools.offered(text)
        if offered:
            actions += [tool.name for tool in offered]
            extra.update(tools.schema_fields(offered))
            lines.append('Tools you really have (the code does them; never say one happened unless you chose it):\n- '
                         + '\n- '.join(tool.line for tool in offered)
                         + '\nFill the chosen tool’s fields. Leave other tool fields empty ("", 0 or []). '
                         'For an action tool, say is a short confirmation in your own words.')
        hub_relevant = not local_only and (
            CHATGPT.search(text) or helper.should_lookup(text) or not helper.LOCAL_TALK.search(text)
        )
        if robot and getattr(robot, 'handoff', False) and hub_relevant:
            actions.append('lookup')
            extra['task'] = {'type': 'string', 'maxLength': 600}
            if any(tool.name == 'weather' for tool in offered):
                lines.append('For weather, use the weather tool, not lookup.')
            gpt_note = ' They explicitly asked for ChatGPT, so set do to lookup.' if CHATGPT.search(text) else (
                ' Never mention ChatGPT unless they explicitly brought it up.' if not explicit_gpt else ''
            )
            lines.append(
                'lookup: your background information hub, for what you cannot know yourself: live or personal '
                'data (weather, news, scores, prices, store hours, their calendar, email, messages, tasks, '
                'briefings), web searches, and long detailed explanations or plans they ask for. Answer everything '
                'else yourself with do="none": conversation, feelings, jokes, opinions, advice, quick math, and '
                'everyday knowledge you are sure of. When do="lookup", put the complete '
                'self-contained request in task (empty string when do is not lookup), and in say give ONLY a '
                'short 2–5 word line like "Let me check.", "One sec.", or "Looking into it." Do NOT guess the '
                'answer and do NOT mention checking calendar, email, or specific apps unless asked about them.'
                + gpt_note
            )
        elif helper.OUTSIDE.search(text) and not local_only and not any(tool.data for tool in offered):
            lines.append('Your live information lookup is offline right now because the Pi is disconnected. '
                         'If they need live info, say so in one short line without mentioning ChatGPT or guessing.')
        return actions, extra, lines

    def reply(self, text, source, *, stream=None, use_camera=False):
        local_request = LOCAL_REQUEST.fullmatch(text)
        if local_request:
            text = local_request[1].strip()
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
        history = self.history.setdefault(conversation, deque(maxlen=HISTORY_MESSAGES))
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
        natural_memory = bool(identity and (self.current_voice == identity or self.speaker_evidence in ('face', 'body'))
                              and config.get('natural_memory') and not visual)
        explicit_gpt = helper.mentions_gpt(text)
        schema = TURN_SCHEMA
        # The system prompt is the same on every turn, so the model server can keep it (and the conversation so far)
        # in its cache and only read what's new. Everything that changes per turn goes in `context`, a note at the
        # start of this turn's message.
        context = []
        if visual or can_select_show:
            system = self.persona + '\n\n' + TURN_DIRECTION
        else:
            system = self.spoken_system()
            schema = MEMORY_SCHEMA if natural_memory else SPOKEN_SCHEMA
            actions, extra, lines = self.available_actions(text, robot, image is not None, local_only=bool(local_request))
            group = not getattr(self, 'accepted_wake', True) and self.room_shared()
            if group and talks_to_someone_else(text, people_names()):
                return self.not_for_me(source, text)
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
        if getattr(self, 'tool_result', None):
            name, result = self.tool_result
            context.append(f'Result of your {name} tool, real data to answer from (never add numbers it doesn’t have): '
                           f'{result}' if not result.startswith('FAILED') else
                           f'Your {name} tool couldn’t answer yet: {result[8:]} Handle that in one short, natural line '
                           'without guessing or calling it broken.')
        overheard = self.overheard_note()
        if overheard:
            context.append(overheard)
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
        try:
            reply = llm.chat(messages, schema=schema, model=llm.vision_model() if visual else None,
                             max_tokens=480,   # room for a story or a real explanation; most replies are far shorter
                             temperature=.27 if not (visual or can_select_show) else .45,
                             **stream_options)
        except NotForMe:
            return self.not_for_me(source, text)
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
        if reply.lstrip().startswith('{'):
            try:
                turn = json.loads(reply)
                if turn.get('to') == 'them':
                    return self.not_for_me(source, text)
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
                    request = text if not task or task.casefold() == text.casefold() else f'{text}\n(Context: {task})'
                    outcome = helper.start(self, source, identity, request, list(history),
                                           user_text=text, remembered=remembered)
                    if outcome:
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
        echoed_request = len(text.split()) >= 4 and reply.casefold().rstrip('?.!') == text.casefold().rstrip('?.!')
        if not reply or echoed_request:
            return 'I lost my train of thought. Say that again?'
        if len(reply) > 560:
            reply = reply[:557].rsplit(' ', 1)[0].rstrip(',.?!:;') + '…'
        history.extend([user_turn, {'role': 'assistant', 'content': reply}])
        return reply
