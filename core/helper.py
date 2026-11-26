"""ChatGPT as BITCH's helper for anything that needs accounts or the outside world.

ChatGPT already has the calendar, email, files and web search connected, so instead of a
connector per service BITCH hands those requests to the signed-in ChatGPT on the Pi, says a
few words while it works, and speaks the answer when it comes back."""
from datetime import datetime
import re
import threading
import time
import uuid

from . import llm, memory, streaming
from .hub import hub

# Requests that need live information or the person's accounts.
OUTSIDE = re.compile(
    r"\b(?:weather|forecast|rain(?:ing)?|snow(?:ing)?|temperature outside|umbrella|jacket|"
    r"calendar|schedule|meetings?|appointments?|agenda|events? (?:today|tomorrow|this)|am I (?:free|busy)|"
    r"e-?mails?|inbox|gmail|unread|messages? from|"
    r"news|headlines|scores?|stocks?|share price|"
    r"look (?:it |that )?up|search (?:for|the web)|google|find out|latest|"
    r"traffic|directions|commute|flights?|package|deliver(?:y|ies)|order status|"
    r"remind me|reminders?|to-?do|my (?:tasks|notes|docs?|documents|files|drive)|spreadsheet)\b", re.I)
WAIT = 200          # seconds to wait for ChatGPT's answer
SPOKEN = 300        # answers longer than this get condensed before BITCH says them
MARKUP = re.compile(r'\[([^\]]+)\]\([^)]+\)|https?://\S+|[*_#`>|]+|^\s*[-•\d.]+\s+', re.M)
lock = threading.Lock()
tasks = []          # recent helper requests, newest last, shown in the app


def instructions(request, conversation):
    now = datetime.now().astimezone()
    context = ''
    if conversation:
        context = '\nOur conversation just before this (for context): ' + ' / '.join(
            ('They' if turn['role'] == 'user' else 'BITCH') + ': ' + turn['content'][:160] for turn in conversation[-4:])
    return (f'{request.strip()}\n\nIt is {now:%A, %B %-d, %Y, %-I:%M %p} ({now:%Z}) on their clock.{context}\n'
            'Use their connected apps (calendar, email, files) and web search when that helps. '
            'Answer in at most three short sentences that sound natural read aloud, with no lists, links or markdown. '
            'Carry out exactly what they asked. If it would send a message to someone else or spend money, '
            'prepare it but do not send it, and say it is ready in ChatGPT for them to confirm.')


def start(brain, source, identity, request, conversation=()):
    """Send a request to ChatGPT and come back with the answer. Returns what BITCH says now."""
    robot = hub.get('robot')
    if not robot or not getattr(robot, 'handoff', False):
        return 'I can’t reach ChatGPT. The Pi needs to be connected.'
    request_id = uuid.uuid4().hex
    if not robot.macro(request_id, 'agent', instructions(request, list(conversation))[:1500]):
        return 'I couldn’t reach the Pi to ask ChatGPT.'
    if identity:
        memory.kv_set('agent:' + identity, request_id)
    brain.last_agent_request = (source, request_id)
    task = {'id': request_id, 'request': request.strip()[:240], 'state': 'working', 'started': time.time(),
            'answer': ''}
    with lock:
        tasks.append(task)
        del tasks[:-8]
    if getattr(robot, 'auto_handoff', False):
        threading.Thread(target=watch, args=(brain, source, task), daemon=True).start()
        return None
    task['state'] = 'queued'
    return 'I queued it in the Pi’s ChatGPT inbox.'


def watch(brain, source, task):
    robot = hub.get('robot')
    deadline = time.monotonic() + WAIT
    status = None
    while time.monotonic() < deadline:
        time.sleep(1.5)
        status = robot.macro_status(task['id']) if robot else None
        if status and status.get('state') == 'returned' and status.get('answer'):
            answer = status['answer'].strip()
            task.update(state='done', answer=answer)
            brain.announce(source, spoken(answer), heard=('ChatGPT said: ' + answer[:600]))
            return
        if status and status.get('state') == 'canceled':
            task['state'] = 'canceled'
            return
    task['state'] = 'slow'
    brain.announce(source, 'ChatGPT is taking a while. Its answer will be in the ChatGPT tab on the Pi.')


def spoken(answer):
    """Something BITCH can say out loud: short, with no markdown."""
    plain = ' '.join(MARKUP.sub(lambda match: match[1] or '', answer).split())
    if len(plain) <= SPOKEN:
        return plain
    try:
        short = llm.chat([
            {'role': 'system', 'content': 'Rewrite this answer so a desk robot can say it aloud in at most three short '
                                          'sentences. Keep names, times, places and numbers exactly. No lists. '
                                          'Reply with only the spoken words.'},
            {'role': 'user', 'content': plain[:4000]}], max_tokens=140, temperature=.2)
        short = streaming.clean(short)
        if 20 <= len(short) <= 480:
            return short
    except (OSError, ValueError, KeyError):
        pass
    return plain[:SPOKEN].rsplit('. ', 1)[0].rstrip('.') + '.'


def recent():
    with lock:
        return [{key: task[key] for key in ('id', 'request', 'state', 'answer', 'started')} for task in tasks[-4:]]
