"""Local, durable requests for a human or future desktop connector on the Pi."""
import argparse
from html import escape
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
import desktop_chatgpt

HOME = Path(os.environ.get('BITCH_PI_HOME', '~/.bitch-pi')).expanduser()
QUEUE = HOME / 'macros'
handoff_lock = threading.Lock()
desktop_lock = threading.Lock()
wake_worker = threading.Event()
ID = re.compile(r'[0-9a-f]{32}')


def format_prompt(prompt):
    return ('BITCH robot handoff\n'
            'I’m BITCH, the account holder’s desk robot. I’m passing along their request on their behalf.\n\n'
            'Request from the account holder\n'
            + prompt.strip() + '\n\n'
            'Reply for BITCH\n'
            'Put the main answer first and keep it easy to read aloud. If a detail is missing, ask a focused question I can relay.')


def job_path(request_id):
    if not isinstance(request_id, str) or not ID.fullmatch(request_id):
        raise ValueError('invalid request ID')
    return QUEUE / (request_id + '.json')


def enqueue(item):
    path = job_path(item.get('id'))
    prompt = item.get('prompt')
    if item.get('name') != 'agent' or not isinstance(prompt, str) or not 0 < len(prompt.strip()) <= 2500:
        raise ValueError('expected an agent prompt of up to 2500 characters')
    QUEUE.mkdir(parents=True, exist_ok=True, mode=0o700)
    record = {'id': item['id'], 'version': 1, 'macro': 'agent', 'prompt': prompt.strip(),
              'created_at': datetime.now(timezone.utc).isoformat()}
    if item.get('auto_send') is True:
        record.update(auto_send=True, state='queued')
    if item.get('mode') in ('fast', 'think'):
        record['mode'] = item['mode']
    with tempfile.NamedTemporaryFile(mode='w', dir=QUEUE, encoding='utf-8') as temporary:
        json.dump(record, temporary, ensure_ascii=False)
        temporary.flush()
        os.fsync(temporary.fileno())
        try:
            os.link(temporary.name, path)
        except FileExistsError:
            previous = json.loads(path.read_text())
            if previous['prompt'] != record['prompt']:
                raise ValueError('request ID already exists')
    if record.get('auto_send'):
        wake_worker.set()


def clipboard(prompt):
    if os.environ.get('WAYLAND_DISPLAY') and shutil.which('wl-copy'):
        command = ['wl-copy']
    elif os.environ.get('DISPLAY') and shutil.which('xclip'):
        command = ['xclip', '-selection', 'clipboard']
    else:
        raise RuntimeError('Run prepare in the Pi desktop with wl-copy or xclip installed.')
    subprocess.run(command, input=prompt, text=True, timeout=5, check=True)


def pending():
    return sorted(QUEUE.glob('*.json'), key=lambda path: path.stat().st_mtime)


def prepare_desktop(path, desktop_id):
    job = json.loads(path.read_text())
    if desktop_id:
        subprocess.run(['gtk-launch', '--', desktop_id], check=True, timeout=10)
    clipboard(format_prompt(job['prompt']))
    print('Request copied. Paste it in the desktop agent when ready.', flush=True)


def watch_desktop(desktop_id):
    print('Watching for agent requests. Press Ctrl+C to stop.', flush=True)
    while True:
        jobs = [job for job in pending() if handoff_status(job.stem)['state'] == 'queued']
        if not jobs:
            time.sleep(1)
            continue
        job = jobs[0]
        prepare_desktop(job, desktop_id)
        print(f'Waiting for this request to be handled. Run macros.py done {job.stem} when finished.', flush=True)
        while job.exists():
            time.sleep(1)


def read_job(request_id):
    return json.loads(job_path(request_id).read_text())


def handoff_status(request_id):
    record = read_job(request_id)
    status = {'state': record.get('state', 'queued'), 'answer': record.get('answer')}
    if record.get('reason'):
        status['reason'] = record['reason']
    if record.get('timing'):
        status['timing'] = record['timing']   # where the seconds went on the Pi (new chat, mode, paste, send, answer)
    return status


def write_job(path, record):
    with tempfile.NamedTemporaryFile(mode='w', dir=QUEUE, encoding='utf-8', delete=False) as temporary:
        json.dump(record, temporary, ensure_ascii=False)
        temporary.flush()
        os.fsync(temporary.fileno())
    try:
        Path(temporary.name).replace(path)
        directory = os.open(QUEUE, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary.name).unlink(missing_ok=True)


def send_pending():
    for path in pending():
        try:
            with handoff_lock:
                record = read_job(path.stem)
                if record.get('state') == 'sending':
                    # The previous service may have clicked Send just before it stopped.
                    record['state'] = 'send_unverified'
                    write_job(path, record)
                    continue
                if record.get('state') != 'queued' or record.get('auto_send') is not True:
                    continue
                record['state'] = 'sending'
                write_job(path, record)
            with desktop_lock:
                result = desktop_chatgpt.submit(format_prompt(record['prompt']), mode=record.get('mode'))
            if result.get('state') not in ('ready', 'submitted', 'send_unverified', 'returned'):
                result = {'state': 'send_unverified'}
            with handoff_lock:
                record.update(result)
                write_job(path, record)
        except (OSError, ValueError):
            continue


def send_worker():
    # Nothing may stop this loop: if it died, every later ChatGPT request would wait forever.
    while True:
        wake_worker.clear()
        try:
            send_pending()
        except Exception as error:
            print(f'ChatGPT handoff error: {error!r}', flush=True)
        wake_worker.wait(5)


def update_handoff(request_id, action, answer=''):
    path = job_path(request_id)
    with handoff_lock:
        record = read_job(request_id)
        state = record.get('state', 'queued')
        if state in ('returned', 'canceled', 'sending'):
            raise ValueError('This request is already closed.')
        if action == 'send' and state in ('queued', 'ready'):
            record.update(state='queued', auto_send=True)
        elif action in ('prepare', 'compose') and state in ('queued', 'ready'):
            record['state'] = 'ready'
            record['auto_send'] = False
            if action == 'compose':
                with desktop_lock:
                    record['drafted'] = desktop_chatgpt.draft(format_prompt(record['prompt']))
        elif action == 'return':
            answer = answer.strip()
            if state not in ('ready', 'submitted', 'send_unverified') or not 1 <= len(answer) <= 12000:
                raise ValueError('Prepare the prompt first and paste an answer of up to 12,000 characters.')
            record.update(state='returned', answer=answer)
        elif action == 'cancel':
            record['state'] = 'canceled'
        else:
            raise ValueError('Unknown handoff action.')
        write_job(path, record)
    if action == 'send':
        wake_worker.set()


def inbox_html():
    sections = []
    records = []
    for path in reversed(pending()):
        try:
            records.append(read_job(path.stem))
        except FileNotFoundError:
            continue
    for record in sorted(records, key=lambda item: item.get('state') in ('returned', 'canceled')):
        request_id = record['id']
        state = record.get('state', 'queued')
        label = {'queued': 'Queued', 'ready': 'Drafted in ChatGPT' if record.get('drafted') else 'Ready to paste',
                 'returned': 'Answer returned', 'sending': 'Sending and waiting for ChatGPT',
                 'submitted': 'Sent to ChatGPT', 'send_unverified': 'Check the ChatGPT tab',
                 'canceled': 'Canceled'}[state]
        prompt = format_prompt(record['prompt']) if state == 'ready' else record['prompt']
        section = f'<section><h2>{label}</h2><textarea readonly rows="{8 if state == "ready" else 4}" aria-label="Request">{escape(prompt)}</textarea>'
        if record.get('reason'):
            section += f'<p>{escape(record["reason"])}</p>'
        if state in ('queued', 'ready'):
            section += (f'<form method="post"><input type="hidden" name="id" value="{request_id}">'
                        '<button name="action" value="send">Send with ChatGPT</button> '
                        '<button name="action" value="compose">Draft in ChatGPT</button> '
                        '<button name="action" value="prepare">Show formatted prompt</button> '
                        '<button name="action" value="cancel">Cancel</button></form>')
        if state == 'ready':
            if record.get('drafted'):
                section += '<p>Review and send the draft in ChatGPT, then paste its answer below.</p>'
            else:
                if 'drafted' in record:
                    section += '<p>The browser draft could not be verified. The formatted request is above.</p>'
                section += ('<p><a href="https://chatgpt.com/" target="_blank" rel="noopener noreferrer">Open ChatGPT</a>, '
                            'paste the formatted request, and send it. Paste its answer below.</p>')
        if state in ('submitted', 'send_unverified'):
            url = record.get('url') or ''
            if desktop_chatgpt.chat_url(url):
                section += f'<p><a href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">Open the conversation</a>.</p>'
            if state == 'send_unverified':
                section += '<p>The browser could have sent this request. Check the tab before sending it yourself.</p>'
        if state in ('ready', 'submitted', 'send_unverified'):
            section += (
                        f'<form method="post"><input type="hidden" name="id" value="{request_id}">'
                        '<label>ChatGPT answer<textarea name="answer" rows="5" maxlength="12000" required></textarea></label>'
                        '<button name="action" value="return">Return to BITCH</button></form>')
        elif state == 'returned':
            section += f'<pre>{escape(record["answer"])}</pre>'
        sections.append(section + '</section>')
        if len(sections) == 30:
            break
    content = ''.join(sections) or '<p>No requests yet.</p>'
    return ('<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>BITCH ChatGPT handoff</title><style>'
            'body{color-scheme:light dark;font:16px system-ui;margin:2rem auto;padding:0 1rem;max-width:45rem}'
            'textarea{display:block;box-sizing:border-box;width:100%;margin:.5rem 0;font:inherit}'
            'section{border-top:1px solid GrayText;padding:1rem 0}pre{white-space:pre-wrap;font:inherit}'
            '</style><h1>ChatGPT handoff</h1><p><a href="/inbox">Refresh requests</a></p>' + content)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest='command', required=True)
    subcommands.add_parser('list', help='list pending requests')
    prepare = subcommands.add_parser('prepare', help='copy a request to the local desktop clipboard')
    prepare.add_argument('id')
    prepare.add_argument('--desktop-id', help='optionally launch an installed desktop app with gtk-launch')
    watch = subcommands.add_parser('watch', help='prepare the next request in an opted-in desktop session')
    watch.add_argument('--desktop-id', required=True)
    done = subcommands.add_parser('done', help='remove a handled request from the inbox')
    done.add_argument('id')
    args = parser.parse_args()
    try:
        if args.command == 'list':
            for path in pending():
                job = json.loads(path.read_text())
                print(job['id'], ' '.join(job['prompt'].split())[:80])
        elif args.command == 'prepare':
            prepare_desktop(job_path(args.id), args.desktop_id)
        elif args.command == 'watch':
            watch_desktop(args.desktop_id)
        else:
            job_path(args.id).unlink()
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        parser.exit(1, f'{error}\n')


if __name__ == '__main__':
    main()
