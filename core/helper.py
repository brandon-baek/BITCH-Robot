"""Information hub backed by the signed-in GPT session on the Pi.

Handles briefings, live info (weather, calendar, email, files, web), and substantive knowledge
requests in the background. Quick answers come right back; slow ones turn into a future
notification so conversation can move on or end in the meantime."""
from datetime import datetime
import re
import threading
import time
import uuid

from . import llm, memory, notifications, streaming
from .hub import hub

EXPLICIT_GPT = re.compile(r"\b(?:chat[ -]?gpt|gpt|openai)\b", re.I)
BRIEFING = re.compile(
    r"\b(?:brief(?:ing)?|catch me up|rundown|run-down|update me|what(?:'s| is) (?:my day|today|this week) "
    r"(?:look(?:ing)? like|like)|morning update|daily summary|anything i (?:missed|should know))\b", re.I)
# Requests that clearly belong to the external/information hub rather than robot small talk.
OUTSIDE = re.compile(
    r"\b(?:brief(?:ing)?|catch me up|rundown|run-down|update me|daily summary|"
    r"weather|forecast|rain(?:ing)?|snow(?:ing)?|temperature|humid(?:ity)?|windy|sunny|cold outside|hot outside|umbrella|jacket|"
    r"calendar|schedule|meetings?|appointments?|agenda|events? (?:today|tomorrow|this|next)|am I (?:free|busy)|my day|my week|"
    r"e-?mails?|inbox|gmail|unread|messages? from|"
    r"news|headlines|scores?|standings|stocks?|share price|market|bitcoin|crypto|"
    r"look (?:it |that |this )?up|search (?:for|the web)|google|find out|latest|current|right now|"
    r"traffic|directions|commute|flights?|package|deliver(?:y|ies)|order status|"
    r"remind me|reminders?|to-?do|my (?:tasks|notes|docs?|documents|files|drive)|spreadsheet|"
    r"recommend|suggestions? for|recipe|how (?:do|does|to|much|many|far|long|tall|old)|"
    r"explain|summarize|summary of|compare|plan (?:my|a|out)|research|who (?:is|was|won)|what (?:is|was|are|were) (?:a |an |the )?(?!time\b|your\b))\b",
    re.I)
LOCAL_TALK = re.compile(
    r"\b(?:your (?:name|voice|eyes|arm|neck|base|servos?|body|favorite|favourite)|"
    r"how are you|how(?:'s| is) it going|are you (?:awake|there|okay|mad|happy|tired|alive)|"
    r"who (?:am i|are you)|do you (?:recognize|remember|like|love|hate|know me|see)|"
    r"what (?:am i|do you see|did i|do you remember|can you do)|"
    r"look (?:at|here|there|around)|follow my|dance|disco|opera|sing|routine)\b", re.I)

# Requests that deserve ChatGPT's thinking (Medium) instead of Instant: reasoning, planning, writing, briefings.
THINK = re.compile(
    r"\b(?:brief(?:ing)?|catch me up|rundown|plan(?:ning)?|compare|comparison|versus|vs\.?|pros and cons|"
    r"trade-?offs?|explain|why (?:do|does|is|are|did|would)|how (?:does|do|would|could|can) .{0,40}work|"
    r"step[- ]by[- ]step|research|analy[sz]e|summari[sz]e|recommend|which (?:one|is better)|should I|"
    r"write|draft|debug|calculate|figure out|strategy|itinerary|budget)\b", re.I)

# Natural ways someone asks about a lookup BITCH already sent out.
STATUS_CHECK = re.compile(
    r"\b(?:have you (?:found|gotten|got|checked|heard|finished|looked)|"
    r"did you (?:find|get|check|finish|hear back)|"
    r"didn(?:'t|’t) i ask (?:you )?(?:about|to check|for)|"
    r"did (?:that|it|the .+?)\s+come back|"
    r"(?:are you )?still (?:searching|looking|checking|working|waiting)|"
    r"(?:any|got an?) (?:news|update|word|answer|luck)|"
    r"is (?:that|it|my|the) .{0,28}\b(?:ready|done|back|finished)|"
    r"how(?:'s|’s| is| are| were)? (?:that|the|my|those)?\s*.{0,24}\b(?:search(?:es|ers)?|lookups?)\b|"
    r"how(?:'s|’s| is) (?:that|the|my) .{0,28}\b(?:going|coming)|"
    r"what(?:'s|’s| is) taking (?:so long|forever)|"
    r"what happened to (?:that|the|my))\b", re.I)

STOP_WORDS = {
    'the', 'and', 'for', 'you', 'your', 'have', 'has', 'had', 'did', 'does', 'are', 'was', 'were',
    'what', 'when', 'where', 'how', 'who', 'why', 'can', 'could', 'would', 'will', 'that', 'this',
    'there', 'here', 'from', 'with', 'about', 'into', 'out', 'yet', 'still', 'just', 'any', 'got',
    'get', 'gotten', 'find', 'found', 'check', 'checking', 'checked', 'look', 'looking', 'looked',
    'search', 'searches', 'searchers', 'searching', 'searched', 'back', 'come', 'came', 'ready',
    'done', 'finished', 'update', 'news', 'word', 'answer', 'luck', 'please', 'hey', 'bitch',
    'going', 'coming', 'happened', 'tell', 'know', 'anything', 'something', 'thing', 'working',
    'waiting', 'long', 'taking', 'today', 'tomorrow', 'now', 'heard', 'lookup', 'lookups',
    'asked', 'ask', 'earlier', 'before', 'those', 'these', 'alright', 'day', 'been',
}


def topic_words(text):
    """Meaningful topic keywords from a request or follow-up question."""
    return {word for word in re.findall(r"[a-z0-9']+", (text or '').casefold())
            if len(word) >= 3 and word not in STOP_WORDS}


def is_status_check(text):
    return bool(text and STATUS_CHECK.search(text))


def active_tasks():
    """Currently in-flight lookups (working, background, or queued), oldest to newest."""
    now = time.time()
    with lock:
        return [dict(task) for task in tasks
                if task['state'] in ('working', 'background', 'queued') and now - task['started'] < WAIT]


def match_active(text):
    """Return an in-flight lookup that `text` is asking about or duplicating, or None."""
    active = active_tasks()
    if not active or not text:
        return None
    topics = topic_words(text)
    checking = is_status_check(text)
    for task in reversed(active):
        earlier = topic_words(task['request'])
        overlap = topics & earlier
        if overlap:
            if checking or len(overlap) >= max(1, round(.5 * min(len(topics), len(earlier)))):
                return task
    if checking and not topics:
        return active[-1]
    return None


def match_recent_done(text, within=360.):
    """Return a recently completed lookup matching a status/follow-up question, so asking about it doesn't restart it."""
    if not text or not is_status_check(text):
        return None
    now = time.time()
    topics = topic_words(text)
    with lock:
        done = [dict(t) for t in tasks if t['state'] == 'done' and t.get('answer') and now - t['started'] < within]
    for task in reversed(done):
        earlier = topic_words(task['request']) | topic_words(task['answer'][:200])
        if topics and (topics & earlier):
            return task
    return done[-1] if (done and not topics) else None


def recent_done_summary(within=360.):
    """Short ledger of lookups that already finished recently, so BITCH remembers what it already checked."""
    now = time.time()
    with lock:
        done = [dict(t) for t in tasks if t['state'] == 'done' and t.get('answer') and now - t['started'] < within]
    if not done:
        return ''
    return '; '.join(f'"{t["request"][:80]}" -> "{t["answer"][:180]}"' for t in done[-3:])


def effort(text):
    """'fast' (Instant, a few seconds) for quick facts and lookups; 'think' (Medium) when it takes real reasoning."""
    text = text or ''
    return 'think' if THINK.search(text) or len(text.split()) > 30 else 'fast'


WAIT = 480              # total seconds to keep watching a long background request
BACKGROUND_AFTER = 20   # seconds before a pending lookup turns into a background notification (most answers: 14-17 s)
LOST = 25               # seconds without hearing from the Pi before reporting connection loss
SPOKEN = 420            # answers longer than this get condensed before BITCH speaks them
MARKUP = re.compile(r'\[([^\]]+)\]\([^)]+\)|https?://\S+|[*_#`>|]+|^\s*[-•\d.]+\s+', re.M)
GPT_SELF = re.compile(r'\b(?:as chat[ -]?gpt[, ]*|chat[ -]?gpt (?:here|says|found)[: ,]*|in chat[ -]?gpt\b)', re.I)
REALTIME_OPENER = re.compile(
    r"^(?:(?:sure|of course|certainly|absolutely|definitely|happy to help|no problem)[,!.\s]+)+"
    r"(?:(?:here(?:'s|’s| is)|here are)\s+)?", re.I)
PROMISE_ACTION = re.compile(
    r"\b(?:i(?:'ll|’ll| will)|let me)\s+(?:go ahead and\s+)?"
    r"(?:check|look up|look into|find|get|pull up|fetch|search(?:\s+for)?)\b", re.I)
OFFER_ONLY = re.compile(r"\blet me know if\b|\bwant me to\b|\bready for you to confirm\b", re.I)
FACT_SIGNAL = re.compile(
    r"\b\d+(?:\s*(?:°|degrees?|%|am|pm|mph|km/h)\b|:\d{2})|"
    r"\b(?:free|nothing scheduled|no events|sunny|cloudy|rain|snow|clear|overcast|high of|low of)\b", re.I)

lock = threading.Lock()
tasks = []              # recent hub requests, newest last, shown in Settings


def is_promise_only(answer):
    """True when ChatGPT only promised to check/look up instead of actually answering."""
    plain = ' '.join((answer or '').split())
    if not plain or len(plain) > 260 or OFFER_ONLY.search(plain) or FACT_SIGNAL.search(plain):
        return False
    return bool(PROMISE_ACTION.search(plain))


def strip_realtime_opener(text):
    """Drop 'Sure, here's...' style openers that sound robotic after a several-second wait."""
    if not text:
        return text
    cleaned = REALTIME_OPENER.sub('', text.strip()).strip()
    if not cleaned:
        return text.strip()
    if cleaned[0].islower() and (len(cleaned) == 1 or not cleaned[1].isupper()):
        cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned


# Things BITCH cannot know by itself: live data, their accounts, and searches. These always go to ChatGPT.
LIVE = re.compile(
    r"\b(?:brief(?:ing)?|catch me up|rundown|update me|daily summary|weather|forecast|rain(?:ing)?|snow(?:ing)?|"
    r"temperature (?:outside|today|tomorrow)|umbrella|calendar|schedule|meetings?|appointments?|agenda|am I (?:free|busy)|"
    r"e-?mails?|inbox|gmail|unread|messages? from|news|headlines|scores?|standings|who won|stocks?|share price|"
    r"bitcoin|crypto|prices?|how much (?:is|does|are) .{0,30}(?:cost|now)|open (?:now|today|until)|close[sd]? (?:on|at|today)|"
    r"hours|look (?:it |that |this )?up|search (?:for|the web)|google|latest|traffic|directions|commute|flights?|"
    r"order status|deliver(?:y|ies)|my (?:tasks|docs?|documents|files|drive))\b", re.I)


def needs_live(text):
    return bool(text and not LOCAL_TALK.search(text) and not is_status_check(text)
                and (EXPLICIT_GPT.search(text) or LIVE.search(text)))


def mentions_gpt(text):
    return bool(text and EXPLICIT_GPT.search(text))


def should_lookup(text):
    if not text or LOCAL_TALK.search(text):
        return False
    return bool(EXPLICIT_GPT.search(text) or OUTSIDE.search(text))


def strip_gpt_mentions(text, *, allow_gpt=False):
    if allow_gpt or not text:
        return text
    cleaned = GPT_SELF.sub('', text)
    cleaned = re.sub(r'\b(?:with |on |through |via |in |using |from )?chat[ -]?gpt\b', '', cleaned, flags=re.I)
    return ' '.join(cleaned.split())


def sanitize_ack(speech, user_text, *, allow_gpt=False):
    """Keep the immediate spoken handoff natural, short, and free of hallucinated tool narration."""
    text = (speech or '').strip()
    if not text:
        return 'Let me check.'
    lower_user = (user_text or '').casefold()
    lower_say = text.casefold()
    if not allow_gpt and EXPLICIT_GPT.search(text):
        return 'Let me check.'
    # Block the local model from answering twice or narrating unrelated connectors during a hub handoff.
    if len(text.split()) > 8:
        return 'Let me check.'
    for word in ('calendar', 'email', 'inbox', 'gmail', 'spreadsheet', 'drive'):
        if word in lower_say and word not in lower_user and not BRIEFING.search(user_text or ''):
            return 'Let me check.'
    return text


def instructions(request, conversation, *, speaker=None, remembered=None, explicit=False):
    now = datetime.now().astimezone()
    clean_req = request.strip()
    is_briefing = bool(BRIEFING.search(clean_req))
    lines = [clean_req, '', f'Current time: {now:%A, %B %-d, %Y, %-I:%M %p} ({now:%Z}).']
    if speaker:
        lines.append(f'Person speaking: {speaker}.')
    if remembered:
        snippet = ' '.join(remembered.splitlines()[:5])[:360]
        if snippet:
            lines.append(f'Known context: {snippet}')
    if conversation:
        recent_turns = ' / '.join(
            ('Them' if turn['role'] == 'user' else 'BITCH') + ': ' + turn['content'][:160]
            for turn in conversation[-4:]
        )
        lines.append('Recent conversation: ' + recent_turns)
    if is_briefing:
        notifs = notifications.summary(limit=6)
        if notifs:
            lines.append(f'Recent Mac/phone notifications: {notifs}')
        lines.append(
            'Give a crisp personal briefing using their connected apps (calendar, email, tasks), recent notifications, '
            'and web search as helpful: cover upcoming schedule, important unread messages/notifications, local weather, '
            'and key updates. Write 3 to 5 short, natural spoken sentences with no bullet lists, links, or markdown.'
        )
    else:
        lines.append(
            'Use web search or their connected apps (calendar, email, files) ONLY when relevant to what they '
            'actually asked — for example, just check weather/web for weather, and only check calendar or email '
            'when the request is about their schedule, plans, or messages. '
            'Answer in 1 to 3 short sentences that sound natural read aloud, with no lists, links, or markdown.'
        )
    lines.append(
        'This is a single-turn handoff — you cannot send a follow-up message. '
        'NEVER say "I\'ll check", "Let me look that up", or promise to fetch something later: '
        'do any needed search or app check RIGHT NOW and include the complete answer in THIS reply. '
        'If they are correcting a detail from the recent conversation (like their city, date, or name), '
        'immediately answer their earlier question with the corrected detail in this same reply. '
        'Start directly with the answer — do not open with filler like "Sure,", "Of course,", or "Here\'s".'
    )
    if not explicit:
        lines.append('Write the reply directly so their desk robot can speak it as its own answer; do not refer to yourself as ChatGPT.')
    lines.append(
        'If fulfilling the request would send a message to someone else or spend money, prepare the draft '
        'without sending it and state that it is ready for them to confirm.'
    )
    return '\n'.join(lines)[:2400]


def start(brain, source, identity, request, conversation=(), *, user_text='', remembered=None):
    """Send a request to the Pi GPT hub and watch for the answer. Returns an immediate error string or None."""
    explicit = mentions_gpt(user_text or request)
    robot = hub.get('robot')
    if not robot or not getattr(robot, 'handoff', False):
        return ('I can’t reach ChatGPT right now. Make sure the Pi is connected.'
                if explicit else 'I can’t look that up right now — the Pi isn’t connected.')
    asked = set(re.findall(r'\w+', (user_text or request).casefold()))
    asked_topics = topic_words(user_text or request)
    with lock:
        for task in tasks:
            if task['state'] not in ('working', 'background', 'queued') or time.time() - task['started'] >= WAIT:
                continue
            earlier = set(re.findall(r'\w+', task['request'].casefold()))
            earlier_topics = topic_words(task['request'])
            word_match = bool(asked and earlier and len(asked & earlier) >= .6 * min(len(asked), len(earlier)))
            topic_match = bool(asked_topics and earlier_topics
                               and len(asked_topics & earlier_topics) >= max(1, round(.5 * min(len(asked_topics), len(earlier_topics)))))
            if word_match or topic_match:
                return 'I’m still looking into that one — I’ll let you know as soon as it comes back.'
    prompt = instructions(request, list(conversation), speaker=identity or getattr(brain, 'speaker', None),
                          remembered=remembered, explicit=explicit)
    request_id = uuid.uuid4().hex
    if not robot.macro(request_id, 'agent', prompt[:2400], effort(user_text or request)):
        return ('I couldn’t reach the Pi to ask ChatGPT.'
                if explicit else 'I couldn’t reach the Pi to check on that right now.')
    if identity:
        memory.kv_set('agent:' + identity, request_id)
    brain.last_agent_request = (source, request_id)
    display_request = (user_text or request).strip().splitlines()[0][:240]
    task = {
        'id': request_id,
        'request': display_request,
        'prompt': prompt[:2400],
        'identity': identity,
        'state': 'working',
        'started': time.time(),
        'explicit': explicit,
        'turn_mark': getattr(brain, 'turn_count', 0),
        'answer': '',
        'retried': False,
    }
    with lock:
        tasks.append(task)
        del tasks[:-8]
    if getattr(robot, 'auto_handoff', False):
        threading.Thread(target=watch, args=(brain, source, task), daemon=True).start()
        return None
    task['state'] = 'queued'
    return 'I queued it in the Pi’s ChatGPT inbox.' if explicit else 'I queued that request on the Pi.'


def watch(brain, source, task):
    """Wait for the answer and deliver it, converting long waits into a background notification."""
    robot = hub.get('robot')
    started = last_heard = time.monotonic()
    explicit = task.get('explicit', False)
    backgrounded = False

    while time.monotonic() - started < WAIT:
        time.sleep(.5)
        now = time.monotonic()
        status = robot.macro_status(task['id']) if robot else None
        state = status.get('state') if status else None
        reason = (status.get('reason') or '') if status else ''
        if status:
            last_heard = now

        if state == 'returned' and status.get('answer'):
            answer = status['answer'].strip()
            # If ChatGPT only promised to check ("Oh, La Crescenta! I'll check the forecast instead.")
            # without actually fetching the answer, immediately retry once with that recognized context.
            if is_promise_only(answer) and not task.get('retried') and robot:
                task['retried'] = True
                retry_id = uuid.uuid4().hex
                retry_prompt = (
                    f'In your previous reply you only said "{answer}" without giving the actual answer. '
                    'This is a single-turn handoff — perform that check right now using web search or connected apps '
                    'and reply directly with the complete answer in 1 to 3 short spoken sentences. '
                    'Do not promise to check later.\n\n'
                    f'Original request:\n{task.get("prompt") or task["request"]}'
                )[:2400]
                if robot.macro(retry_id, 'agent', retry_prompt, effort(task['request'])):
                    print('  Lookup: ChatGPT promised to check without answering; retrying automatically', flush=True)
                    task['id'] = retry_id
                    if task.get('identity'):
                        memory.kv_set('agent:' + task['identity'], retry_id)
                    brain.last_agent_request = (source, retry_id)
                    last_heard = now
                    continue
            elapsed = now - started
            moved_on = getattr(brain, 'turn_count', 0) > task.get('turn_mark', 0)
            delayed = backgrounded or moved_on or (elapsed >= BACKGROUND_AFTER)
            task.update(state='done', answer=answer)
            pi = status.get('timing')
            print(f'  Lookup: answer back {elapsed:.1f} s after asking' + (f' (Pi: {pi})' if pi else ''), flush=True)
            out_loud = spoken(answer, request=task['request'], explicit=explicit,
                              contextualize=delayed, moved_on=moved_on, waited=elapsed)
            history_line = f'ChatGPT said: {answer[:600]}' if explicit else strip_gpt_mentions(answer[:600], allow_gpt=False)
            extra = {'polite': True} if moved_on else {}
            update = {
                'id': task['id'],
                'request': task['request'],
                'answer': history_line,
                'explicit': explicit,
                'turn_mark': task.get('turn_mark', 0),
                'moved_on': moved_on,
                'waited': elapsed,
            }
            brain.announce(source, out_loud, heard=history_line, chime=True, update=update, **extra)
            return

        if state == 'canceled':
            task['state'] = 'canceled'
            return

        if state == 'ready':
            task['state'] = 'failed'
            if 'sign in' in reason.casefold():
                msg = ('ChatGPT on the Pi needs you to sign in first.'
                       if explicit else 'I couldn’t look that up — the browser on the Pi needs to be signed in first.')
            else:
                msg = ('I couldn’t send that to ChatGPT on the Pi. Check the Pi desktop.'
                       if explicit else 'I couldn’t get that lookup through on the Pi right now.')
            brain.announce(source, msg)
            return

        if state in ('submitted', 'send_unverified'):
            task['state'] = 'unread'
            msg = ('I sent it to ChatGPT, but I couldn’t read its answer back. Check the ChatGPT tab on the Pi.'
                   if explicit else 'I couldn’t read that answer back from the Pi.')
            brain.announce(source, msg)
            return

        if now - last_heard > LOST:
            task['state'] = 'lost'
            msg = ('I lost the Pi while waiting on ChatGPT. Ask me again in a minute.'
                   if explicit else 'I lost connection to the Pi while checking on that. Ask me again in a minute.')
            brain.announce(source, msg)
            return

        if not backgrounded and now - started >= BACKGROUND_AFTER:
            backgrounded = True
            task['state'] = 'background'
            # Only let them know once if they haven't already moved on to another topic.
            if getattr(brain, 'turn_count', 0) <= task.get('turn_mark', 0) and not brain.busy.is_set():
                msg = ('ChatGPT is taking a bit — I’ll come back to you once it finishes.'
                       if explicit else 'That’s taking a moment — I’ll get back to you as soon as I have it.')
                brain.announce(source, msg, close_turn=True)

    task['state'] = 'slow'
    msg = ('ChatGPT never finished answering. The browser on the Pi might be stuck.'
           if explicit else 'That lookup timed out before coming back.')
    brain.announce(source, msg)


def waiting():
    """True only during the brief initial foreground check, before backgrounding."""
    with lock:
        return any(task['state'] == 'working' for task in tasks)


def pending():
    """Number of lookups currently running (foreground or background)."""
    with lock:
        return sum(task['state'] in ('working', 'background') for task in tasks)


def active_summary():
    """Compact summary of lookups currently in flight, so the brain knows not to duplicate them."""
    now = time.time()
    with lock:
        active = [t for t in tasks if t['state'] in ('working', 'background', 'queued') and now - t['started'] < WAIT]
    if not active:
        return ''
    return '; '.join(f'"{t["request"][:100]}" ({t["state"]}, started {max(1, round(now - t["started"]))}s ago)' for t in active)


def spoken(answer, *, request='', explicit=False, contextualize=False, moved_on=False, waited=0.0):
    """Turn a hub answer into clean, natural spoken prose."""
    plain = ' '.join(MARKUP.sub(lambda match: match[1] or '', answer).split())
    plain = strip_gpt_mentions(plain, allow_gpt=explicit)
    is_briefing = bool(BRIEFING.search(request))
    limit = 540 if is_briefing else SPOKEN
    after_wait = contextualize or moved_on or waited >= 5.0

    if len(plain) <= limit and not after_wait:
        return plain

    if after_wait:
        plain = strip_realtime_opener(plain)

    try:
        if moved_on and request:
            context_rule = (
                f'You are chiming back in during an ongoing conversation with the answer to their earlier request ("{request[:140]}"). '
                'Open with a brief, natural conversational transition so it fits smoothly — for example '
                '"Oh, I got that info you wanted —", "Hey, that weather check just came back —", '
                'or "By the way, I got your calendar —". '
                'NEVER open with "Sure,", "Of course,", "Certainly,", "Here\'s", or a stiff label like "Your calendar." '
            )
        elif after_wait and request:
            context_rule = (
                f'They waited several seconds while you checked their request ("{request[:140]}"). '
                'Deliver the answer naturally as someone returning with the info (for example "Alright, got it —", '
                '"Okay, I checked —", or jump straight into the facts). '
                'NEVER open with realtime filler like "Sure,", "Of course,", "Certainly,", or "Here\'s". '
            )
        else:
            context_rule = ''
        gpt_rule = '' if explicit else 'Never mention ChatGPT or GPT. '
        sentence_limit = 'five' if is_briefing else 'three'
        short = llm.chat([
            {'role': 'system', 'content': (
                f'Rewrite this answer so a desk robot can say it aloud in at most {sentence_limit} short sentences. '
                f'{context_rule}{gpt_rule}'
                'Keep names, times, places, and numbers exact. No bullet lists or markdown. '
                'Reply with only the spoken words.'
            )},
            {'role': 'user', 'content': plain[:4500]},
        ], max_tokens=180 if is_briefing else 140, temperature=.2, background=True)
        short = strip_gpt_mentions(streaming.clean(short), allow_gpt=explicit)
        if after_wait:
            short = strip_realtime_opener(short)
        if 12 <= len(short) <= 560:
            return short
    except (OSError, ValueError, KeyError):
        pass

    trimmed = plain if len(plain) <= limit else plain[:limit].rsplit('. ', 1)[0].rstrip('.') + '.'
    if after_wait and request and len(trimmed) < 500:
        lead = 'Oh, I got that info you wanted — ' if moved_on else 'Alright, got that for you — '
        body = trimmed[0].lower() + trimmed[1:] if trimmed[:2].isalpha() and not trimmed[:2].isupper() else trimmed
        return lead + body
    return trimmed


def recent():
    with lock:
        # Always include active lookups plus recent finished ones (up to 6 total).
        active = [t for t in tasks if t['state'] in ('working', 'background', 'queued')]
        active_ids = {t['id'] for t in active}
        others = [t for t in tasks if t['id'] not in active_ids]
        keep = (others + active)[-6:]
        for t in active:
            if t not in keep:
                keep.append(t)
        return [{key: task[key] for key in ('id', 'request', 'state', 'answer', 'started')} for task in keep]
