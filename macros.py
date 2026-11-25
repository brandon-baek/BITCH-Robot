"""Local, durable requests for a human or future desktop connector on the Pi."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

HOME = Path(os.environ.get('BITCH_PI_HOME', '~/.bitch-pi')).expanduser()
QUEUE = HOME / 'macros'
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
        jobs = pending()
        if not jobs:
            time.sleep(1)
            continue
        job = jobs[0]
        prepare_desktop(job, desktop_id)
        print(f'Waiting for this request to be handled. Run macros.py done {job.stem} when finished.', flush=True)
        while job.exists():
            time.sleep(1)


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
