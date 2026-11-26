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

WAIT = 480              # total seconds to keep watching a long background request
BACKGROUND_AFTER = 12   # seconds before a pending lookup turns into a background notification
LOST = 25               # seconds without hearing from the Pi before reporting connection loss
SPOKEN = 420            # answers longer than this get condensed before BITCH speaks them
MARKUP = re.compile(r'\[([^\]]+)\]\([^)]+\)|https?://\S+|[*_#`>|]+|^\s*[-•\d.]+\s+', re.M)
GPT_SELF = re.compile(r'\b(?:as chat[ -]?gpt[, ]*|chat[ -]?gpt (?:here|says|found)[: ,]*|in chat[ -]?gpt\b)', re.I)

lock = threading.Lock()
tasks = []              # recent hub requests, newest last, shown in Settings


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
    prompt = instructions(request, list(conversation), speaker=identity or getattr(brain, 'speaker', None),
                          remembered=remembered, explicit=explicit)
    request_id = uuid.uuid4().hex
    if not robot.macro(request_id, 'agent', prompt[:2400]):
        return ('I couldn’t reach the Pi to ask ChatGPT.'
                if explicit else 'I couldn’t reach the Pi to check on that right now.')
    if identity:
        memory.kv_set('agent:' + identity, request_id)
    brain.last_agent_request = (source, request_id)
    display_request = (user_text or request).strip().splitlines()[0][:240]
    task = {
        'id': request_id,
        'request': display_request,
        'state': 'working',
        'started': time.time(),
        'explicit': explicit,
        'turn_mark': getattr(brain, 'turn_count', 0),
        'answer': '',
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
        time.sleep(1.2)
        now = time.monotonic()
        status = robot.macro_status(task['id']) if robot else None
        state = status.get('state') if status else None
        reason = (status.get('reason') or '') if status else ''
        if status:
            last_heard = now

        if state == 'returned' and status.get('answer'):
            answer = status['answer'].strip()
            moved_on = getattr(brain, 'turn_count', 0) > task.get('turn_mark', 0)
            delayed = backgrounded or moved_on or (now - started >= BACKGROUND_AFTER)
            task.update(state='done', answer=answer)
            out_loud = spoken(answer, request=task['request'], explicit=explicit, contextualize=delayed)
            history_line = f'ChatGPT said: {answer[:600]}' if explicit else strip_gpt_mentions(answer[:600], allow_gpt=False)
            extra = {'polite': True} if moved_on else {}
            brain.announce(source, out_loud, heard=history_line, chime=True, **extra)
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


def spoken(answer, *, request='', explicit=False, contextualize=False):
    """Turn a hub answer into clean, natural spoken prose."""
    plain = ' '.join(MARKUP.sub(lambda match: match[1] or '', answer).split())
    plain = strip_gpt_mentions(plain, allow_gpt=explicit)
    is_briefing = bool(BRIEFING.search(request))
    limit = 540 if is_briefing else SPOKEN

    if len(plain) <= limit and not contextualize:
        return plain

    try:
        context_rule = (
            f'This is a delayed callback for their earlier request ("{request[:140]}"). '
            'Open with a brief natural 2–5 word reminder of the topic so it makes sense out of the blue, '
            'then give the answer. '
            if contextualize and request else ''
        )
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
        ], max_tokens=180 if is_briefing else 140, temperature=.2)
        short = strip_gpt_mentions(streaming.clean(short), allow_gpt=explicit)
        if 12 <= len(short) <= 560:
            return short
    except (OSError, ValueError, KeyError):
        pass

    trimmed = plain if len(plain) <= limit else plain[:limit].rsplit('. ', 1)[0].rstrip('.') + '.'
    if contextualize and request and len(trimmed) < 500:
        return 'Following up on earlier: ' + trimmed[0].lower() + trimmed[1:] if trimmed[:2].isalpha() and not trimmed[:2].isupper() else 'Following up: ' + trimmed
    return trimmed


def recent():
    with lock:
        return [{key: task[key] for key in ('id', 'request', 'state', 'answer', 'started')} for task in tasks[-5:]]
