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

HOME = Path(os.environ.get('BITCH_PI_HOME', '~/.bitch-pi')).expanduser()
QUEUE = HOME / 'macros'
handoff_lock = threading.Lock()
ID = re.compile(r'[0-9a-f]{32}')


def job_path(request_id):
    if not isinstance(request_id, str) or not ID.fullmatch(request_id):
        raise ValueError('invalid request ID')
    return QUEUE / (request_id + '.json')


def enqueue(item):
    path = job_path(item.get('id'))
    prompt = item.get('prompt')
    if item.get('name') != 'agent' or not isinstance(prompt, str) or not 0 < len(prompt.strip()) <= 1500:
        raise ValueError('expected an agent prompt of up to 1500 characters')
    QUEUE.mkdir(parents=True, exist_ok=True, mode=0o700)
    record = {'id': item['id'], 'version': 1, 'macro': 'agent', 'prompt': prompt.strip(),
              'created_at': datetime.now(timezone.utc).isoformat()}
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
    clipboard(job['prompt'])
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
    return {'state': record.get('state', 'queued'), 'answer': record.get('answer')}


def update_handoff(request_id, action, answer=''):
    path = job_path(request_id)
    with handoff_lock:
        record = read_job(request_id)
        state = record.get('state', 'queued')
        if state in ('returned', 'canceled'):
            raise ValueError('This request is already closed.')
        if action == 'prepare':
            record['state'] = 'ready'
        elif action == 'return':
            answer = answer.strip()
            if state != 'ready' or not 1 <= len(answer) <= 12000:
                raise ValueError('Prepare the prompt first and paste an answer of up to 12,000 characters.')
            record.update(state='returned', answer=answer)
        elif action == 'cancel':
            record['state'] = 'canceled'
        else:
            raise ValueError('Unknown handoff action.')
        with tempfile.NamedTemporaryFile(mode='w', dir=QUEUE, encoding='utf-8', delete=False) as temporary:
            json.dump(record, temporary, ensure_ascii=False)
        try:
            Path(temporary.name).replace(path)
        finally:
            Path(temporary.name).unlink(missing_ok=True)


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
        label = {'queued': 'Queued', 'ready': 'Ready to paste', 'returned': 'Answer returned',
                 'canceled': 'Canceled'}[state]
        section = f'<section><h2>{label}</h2><textarea readonly rows="4" aria-label="Request">{escape(record["prompt"])}</textarea>'
        if state in ('queued', 'ready'):
            section += (f'<form method="post"><input type="hidden" name="id" value="{request_id}">'
                        '<button name="action" value="prepare">Prepare request</button> '
                        '<button name="action" value="cancel">Cancel</button></form>')
        if state == 'ready':
            section += ('<p><a href="https://chatgpt.com/" target="_blank" rel="noopener noreferrer">Open ChatGPT</a>. '
                        'Copy the request above and send it in ChatGPT. Paste its answer below.</p>'
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
