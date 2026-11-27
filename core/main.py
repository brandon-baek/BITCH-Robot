"""Run the desk companion on the Mac."""
import argparse
import socket
import threading
import time
import urllib.request
import webbrowser

from . import config, llm, voice


def prefer_offline():
    """Load the speech models straight from disk when they're all downloaded. Otherwise every start asks Hugging Face
    for updates first, which hangs or fails when the Wi-Fi is down."""
    import os
    from pathlib import Path

    from .hearing import PARAKEET, WHISPER

    hub_dir = Path(os.environ.get('HF_HOME', Path.home() / '.cache' / 'huggingface')) / 'hub'
    needed = [PARAKEET, WHISPER, voice.QWEN_MODELS.get(config.get('voice_model'), voice.QWEN_MODELS['0.6B'])]
    if all(any((hub_dir / ('models--' + repo.replace('/', '--')) / 'snapshots').glob('*/*')) for repo in needed):
        os.environ.setdefault('HF_HUB_OFFLINE', '1')


def main():
    prefer_offline()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', action='store_true', help='open a Mac window')
    parser.add_argument('--pi', help='set the Raspberry Pi hostname or IPv4 address')
    parser.add_argument('--camera', default='0', help='Mac camera index or test video file')
    parser.add_argument('--mic', type=int, help='Mac audio input index (see python -m sounddevice)')
    for name in ('mic', 'camera', 'robot', 'browser'):
        parser.add_argument(f'--no-{name}', action='store_true')
    parser.add_argument('--quiet', action='store_true', help='skip the Mac speaker and voice warmup')
    args = parser.parse_args()

    from . import server
    from .brain import Brain
    from .devices.mac import MacCamera, MacMic, MacSpeaker, ask_permission
    from .devices.robot import Robot
    from .hearing import Hearing
    from .hub import hub
    from .sight import Sight

    try:
        with socket.socket() as port:
            port.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            port.bind(('0.0.0.0', server.HTTP))
    except OSError:
        parser.exit(1, f'The setup port {server.HTTP} is in use. Open the existing setup screen or quit the other listener.\n')

    if args.pi:
        if not server.valid_host(args.pi):
            parser.error('--pi needs a hostname or IPv4 address')
        config.update({'robot_host': args.pi})
    hub.brain, hub.hearing, hub.sight = Brain(), Hearing(), Sight()
    if not args.no_robot:
        hub.add(Robot())
    if not args.quiet:
        hub.add(MacSpeaker())
    if not args.no_mic:
        if config.get('mic_source') == 'mac-mic':
            ask_permission('mic')
        hub.add(MacMic(args.mic))
    if not args.no_camera:
        if config.get('camera_source') == 'mac-camera':
            ask_permission('camera')
        hub.add(MacCamera(args.camera))
    hub.status = 'Waiting for “BITCH”'
    from .life import life
    from .people import people
    from .rhythm import rhythm

    people.on_learned = lambda name, kind: voice.earcon('learned')
    rhythm.on_sleep = lambda: voice.earcon('sleep')
    hub.brain.start()
    hub.hearing.start()
    hub.sight.start()
    life.start()
    from .power import power
    power.start()   # deep sleep, and the watchdog that keeps the Mac from running out of memory
    server.start()
    if config.get('brain_source') == 'local':
        llm.warm(hub.brain.spoken_system())
    if not args.quiet:
        voice.warm()

    url = f'http://127.0.0.1:{server.HTTP}'
    print(f'BITCH setup · {url}', flush=True)
    if args.app:
        try:
            from . import mac_app

            for _ in range(40):
                try:
                    with urllib.request.urlopen(url + '/api/hello', timeout=0.3):
                        break
                except OSError:
                    time.sleep(0.2)
            mac_app.run(url)
            return
        except ImportError:
            hub.note('window', 'Open the setup screen in your browser.')
    if not args.no_browser:
        threading.Timer(1, webbrowser.open, args=(url,)).start()
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        hub.brain.stop()
