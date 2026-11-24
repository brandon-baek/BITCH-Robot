"""Start BITCH on the Mac:  ~/dc-venv/bin/python -m core   (the BITCH app in Applications does this for you)

Options:
  --app              open in its own window (what the BITCH app does)
  --pi ADDRESS       where the robot is (default raspberrypi.local)
  --camera N|FILE    which camera (1 = iPhone via Continuity Camera), or a test video file
  --no-mic --no-camera --no-robot --no-browser
"""
import argparse, os, subprocess, sys, threading, time
from . import config


def main():
    ap = argparse.ArgumentParser(description='BITCH, the desk robot')
    ap.add_argument('--app', action='store_true', help='open in its own window')
    ap.add_argument('--pi', default='raspberrypi.local')
    ap.add_argument('--camera', default='0')
    ap.add_argument('--no-mic', action='store_true'); ap.add_argument('--no-camera', action='store_true')
    ap.add_argument('--no-robot', action='store_true'); ap.add_argument('--no-browser', action='store_true')
    ap.add_argument('--no-life', action='store_true', help='no reminders, greetings or speaking up (for tests)')
    ap.add_argument('--quiet', action='store_true', help="don't use the Mac's speaker (for tests)")
    config.args = args = ap.parse_args()

    from .hub import hub
    from . import llm, memory, server, voice
    from .brain import Brain
    from .hearing import Hearing
    from .sight import Sight
    from .life import Life
    from .devices.robot import Robot
    from .devices.mac import MacSpeaker, MacMic, MacCamera, ask_permission

    port = server.HTTP
    print(f'\n  BITCH\n  on this Mac:  http://localhost:{port}\n  on your phone: http://' + __import__('core.tls', fromlist=['x']).host() + f':{port}\n', flush=True)
    if not llm.available():
        subprocess.run(['open', '-a', 'Ollama'], capture_output=True)
        hub.note('starting Ollama (the brain)...', problem=False)

    hub.brain, hub.hearing, hub.sight, hub.life = Brain(), Hearing(), Sight(), Life()
    devs = [] if args.quiet else [MacSpeaker()]
    if not args.no_robot: devs.append(Robot(args.pi))
    if not args.no_mic: devs.append(MacMic())
    if not args.no_camera: devs.append(MacCamera(args.camera))
    for d in devs:
        d.off = set(memory.kv_get('off_' + d.id, []))
        hub.add(d)

    for ev, msg in (('tasks_changed', 'tasks'), ('memories_changed', 'memories'), ('skills_changed', 'skills'), ('lists_changed', 'tasks')):
        hub.on(ev, lambda m=msg: hub.ui({'type': m}))
    hub.hearing.start(); hub.sight.start(); hub.brain.start()
    if not args.no_life: hub.life.start()
    server.start()
    llm.warm(); voice.warm()
    if args.no_mic: hub.set_status('listening')

    if args.app:
        ask_permission('camera'); ask_permission('mic')       # macOS asks once: "BITCH would like to access..."
        try:
            import webview
            webview.create_window('BITCH', f'http://127.0.0.1:{port}', width=1320, height=900, min_size=(380, 600), background_color='#DFE4E8')
            webview.start()
            os._exit(0)
        except ImportError:
            hub.note('the app window needs pywebview; opening the browser instead')
    if not args.no_browser and not args.app:
        threading.Timer(1.5, lambda: subprocess.run(['open', f'http://localhost:{port}'])).start()
    try:
        while True: time.sleep(3600)
    except KeyboardInterrupt:
        print('\n  bye!')
        os._exit(0)
