"""Fixed commands sent to the paired Pi."""
import http.client
import json
import threading
import time
import urllib.error
import urllib.request

from .base import Device
from ..rhythm import rhythm
from .. import config


def address(host):
    """'raspberrypi.local' -> ('raspberrypi.local', 8765); a test Pi can say 'host:port'."""
    name, _, port = host.rpartition(':') if host.count(':') == 1 else (host, '', '')
    return (name, int(port)) if port.isdigit() else (host, 8765)


class Robot(Device):
    def __init__(self):
        super().__init__('robot', 'Raspberry Pi', ('body', 'voice'), 'Enter the connection key')
        self.online = False
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.connection_error = ''
        self.audio_monitor = False
        self.handoff = False
        self.auto_handoff = False
        self.cloud_chat = False
        self.gestures = False
        self.rhythm = False
        self.pixar = False
        self.wear = None
        self.audio_warm = False
        self.warming_since = None
        self.expressive = False
        self.performance = False
        self.emotive_performance = False
        self.motion_enabled = False
        self.joint_attention = False
        self.shows = False
        self.show_active = False

    def start(self):
        threading.Thread(target=self.watch, daemon=True).start()

    def request(self, path, data=None, content_type='application/json', timeout=2):
        host, key = config.get('robot_host'), config.get('robot_key')
        if not host or not key:
            return None
        request = urllib.request.Request(f'http://{address(host)[0]}:{address(host)[1]}/{path}', data=data, headers={
            'Authorization': 'Bearer ' + key, 'Content-Type': content_type,
        })
        try:
            with self.opener.open(request, timeout=timeout) as response:
                return json.load(response)
        except (OSError, ValueError) as error:
            if path == 'ping':
                if isinstance(error, urllib.error.HTTPError):
                    self.connection_error = f'HTTP {error.code}'
                else:
                    self.connection_error = str(getattr(error, 'reason', error))
            return None

    def post(self, path, item):
        return self.request(path, json.dumps(item).encode())

    def watch(self):
        while True:
            result = self.request('ping')
            self.online = bool(result and result.get('protocol') == 1)
            self.handoff = bool(result and result.get('handoff', 0) >= 2)
            self.auto_handoff = bool(result and result.get('handoff', 0) >= 3)
            self.cloud_chat = bool(result and result.get('cloud_chat'))
            self.audio_monitor = bool(result and result.get('playback_monitor'))
            self.streams = bool(result and result.get('speak_stream'))
            self.expressive = bool(result and result.get('character'))
            self.performance = bool(result and result.get('performance', 0) >= 2)
            self.emotive_performance = bool(result and result.get('performance', 0) >= 3)
            self.motion_enabled = bool(result and result.get('motion', True))
            self.joint_attention = bool(result and result.get('joint_attention'))
            self.gestures = bool(result and result.get('gestures'))
            self.rhythm = bool(result and result.get('rhythm'))
            self.pixar = bool(result and result.get('pixar'))
            self.wear = result.get('wear') if result else None
            self.shows = bool(result and result.get('shows') == 1)
            self.show_active = bool(result and result.get('show_active'))
            if self.online and result.get('audio'):
                self.caps.add('voice')
            else:
                self.caps.discard('voice')
            self.detail = ('Connected' if self.active('voice') else 'Connected · speaker unavailable') if self.online else (
                'Pi unreachable, old software, or key rejected' if config.get('robot_key') else 'Enter the connection key')
            if result and result.get('audio_route'):
                self.detail += ' · ' + result['audio_route']
            if not self.online and self.connection_error:
                self.detail += f' ({self.connection_error})'
            if self.online and not result.get('motion', True):
                self.detail += ' · motion off on Pi'
            keep = (self.online and result.get('audio_keepalive') and self.active('voice')
                    and rhythm.resting_for() < 600
                    and self.hub.selected('voice') is self and config.get('keep_audio') and not config.get('muted')
                    and config.get('volume'))
            if keep:
                if self.post('keepalive', {'volume': config.get('volume') / 100}):
                    now = time.monotonic()
                    if self.warming_since is None:
                        self.warming_since = now
                    self.audio_warm = bool(result.get('audio_warm') and now - self.warming_since >= 5)
                else:
                    self.audio_warm = False
                    self.warming_since = None
            else:
                if self.warming_since is not None and self.online:
                    self.post('keepalive', {'volume': 0})
                self.audio_warm = False
                self.warming_since = None
            time.sleep(2)

    def look(self, mode, base=0, neck=-0.8, energy=0., accent=0., tilt=0., lean=0., calm=False,
             mood=None, sfx_volume=0., sfx=None):
        if not self.pixar and mode in ('lonely', 'grumpy', 'victory', 'alarm', 'yawn'):
            mode = {'lonely': 'sad', 'grumpy': 'ponder', 'victory': 'happy', 'alarm': 'double-take', 'yawn': 'doze'}[mode]
        item = {'mode': mode, 'base': base, 'neck': neck, 'energy': energy}
        if calm and self.rhythm:
            item['calm'] = True
        if self.pixar:
            if mood:
                item['mood'] = {k: round(float(v), 3) for k, v in mood.items()}
            if sfx_volume > 0:
                item['sfx_volume'] = round(max(0., min(1., float(sfx_volume))), 2)
            if sfx:
                item['sfx'] = sfx
        if self.pixar:
            item.update(accent=max(-.75, min(.75, accent)), tilt=max(-.75, min(.75, tilt)), lean=max(-.45, min(.45, lean)))
        elif self.gestures:
            item.update(accent=max(-.5, min(.5, accent)), tilt=max(-.45, min(.45, tilt)), lean=max(-.25, min(.25, lean)))
        else:
            # An older Pi rejects accents past this.
            item['accent'] = max(-.35, min(.35, accent))
        return self.post('look', item)

    def speak(self, wav):
        return bool(self.request('speak', wav, 'audio/wav', timeout=4))

    streams = False      # the Pi can play speech as it arrives
    streaming = None     # (thread, stop event) while a live line is being sent

    def speak_stream(self, chunks):
        """Send 24 kHz mono PCM to the Pi's speaker as it is generated (chunked HTTP), so the first words play
        before the whole reply is rendered."""
        host, key = config.get('robot_host'), config.get('robot_key')
        if not (self.streams and self.online and host and key):
            return False
        self.stop_stream()
        stop = threading.Event()

        def run():
            connection = http.client.HTTPConnection(*address(host), timeout=10)
            try:
                connection.putrequest('POST', '/speak_stream')
                connection.putheader('Authorization', 'Bearer ' + key)
                connection.putheader('Content-Type', 'audio/L16; rate=24000; channels=1')
                connection.putheader('Transfer-Encoding', 'chunked')
                connection.endheaders()
                for data in chunks:
                    if stop.is_set():
                        break
                    if data:
                        connection.send(b'%x\r\n%s\r\n' % (len(data), data))
                connection.send(b'0\r\n\r\n')
                connection.getresponse().read()
            except (OSError, http.client.HTTPException) as error:
                self.connection_error = f'speech stream: {error}'
            finally:
                connection.close()

        thread = threading.Thread(target=run, daemon=True)
        self.streaming = (thread, stop)
        thread.start()
        return True

    def stop_stream(self):
        if self.streaming:
            self.streaming[1].set()
            self.streaming = None

    def playing(self):
        if self.streaming and self.streaming[0].is_alive():
            return True
        if self.audio_monitor:
            result = self.request('playback', timeout=.45)
            if result is not None:
                return result.get('playing')
        return None

    def hush(self):
        self.stop_stream()
        result = self.post('stop', {})
        if result:
            self.show_active = False
        return bool(result)

    def show(self, token, name, volume, steps=None):
        item = {'id': token, 'name': name, 'volume': volume}
        if steps is not None:
            item['steps'] = steps
        result = self.post('show', item)
        if result and result.get('state') == 'queued':
            self.show_active = True
        return result

    def show_status(self, token):
        result = self.request('show', timeout=.45)
        if result and result.get('id') == token:
            state = result.get('state')
            self.show_active = state in ('preparing', 'playing')
            return state
        return None

    def macro(self, request_id, name, prompt, mode=None):
        item = {'id': request_id, 'name': name, 'prompt': prompt, 'auto_send': self.auto_handoff}
        if mode in ('fast', 'think'):
            item['mode'] = mode   # ChatGPT's thinking effort: Instant or Medium
        return bool(self.post('macro', item) or self.post('macro', item))

    def macro_status(self, request_id):
        return self.request('macro/status?id=' + request_id) if self.handoff else None

    def chat_stream(self, prompt, *, provider='chatgpt', reset=False, speaker=None, image=None, on_delta=None, stop=None, timeout=60.):
        """Stream a real-time reply (text or multimodal camera frame) from the Pi's signed-in ChatGPT or Gemini tab."""
        import base64
        import socket
        import threading
        host, key = config.get('robot_host'), config.get('robot_key')
        if not (self.online and self.cloud_chat and host and key):
            raise OSError('Pi streaming cloud brain is unavailable')
        payload = {'provider': provider, 'prompt': prompt, 'reset': bool(reset),
                   'speaker': speaker or 'Brandon'}
        if image is not None:
            if isinstance(image, (bytes, bytearray)) and len(image) > 35_000:
                try:
                    import cv2, numpy as np
                    frame = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_COLOR)
                    if frame is not None and frame.shape[1] > 640:
                        scale = 640 / frame.shape[1]
                        frame = cv2.resize(frame, (640, max(1, int(frame.shape[0] * scale))), interpolation=cv2.INTER_AREA)
                        ok, buf = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 78])
                        if ok:
                            image = buf.tobytes()
                except Exception:
                    pass
            payload['image'] = image if isinstance(image, str) else base64.b64encode(image).decode('ascii')
        body = json.dumps(payload).encode('utf-8')
        conn = http.client.HTTPConnection(*address(host), timeout=timeout)
        sock_ref = [None]
        done_ev = threading.Event()
        if stop is not None:
            def _watch_stop():
                while not done_ev.is_set():
                    if stop.wait(0.05):
                        s = sock_ref[0] or conn.sock
                        if s is not None:
                            try:
                                s.shutdown(socket.SHUT_RDWR)
                            except OSError:
                                pass
                        try:
                            conn.close()
                        except OSError:
                            pass
                        return
            threading.Thread(target=_watch_stop, daemon=True).start()
        try:
            conn.request('POST', '/chat_stream', body=body, headers={
                'Authorization': 'Bearer ' + key,
                'Content-Type': 'application/json',
                'Content-Length': str(len(body)),
            })
            sock_ref[0] = conn.sock
            resp = conn.getresponse()
            if resp.status != 200:
                raise OSError(f'Pi cloud chat returned HTTP {resp.status}')
            full_text = ''
            timing = {}
            while True:
                if stop is not None and stop.is_set():
                    return ''
                try:
                    line = resp.readline()
                except OSError:
                    if stop is not None and stop.is_set():
                        return ''
                    raise
                if not line:
                    break
                raw = line.strip()
                if not raw:
                    continue
                event = json.loads(raw.decode('utf-8', errors='replace'))
                if event.get('ping'):
                    continue
                if event.get('error'):
                    raise RuntimeError(event['error'])
                if event.get('timing'):
                    timing = event['timing']
                txt = event.get('text') or ''
                if txt and txt != full_text:
                    full_text = txt
                    if on_delta:
                        on_delta(full_text)
                if event.get('done'):
                    break
            if timing:
                print(f'  Cloud({provider}): send={timing.get("send_s")}s ttft={timing.get("ttft_s")}s '
                      f'1st-sent={timing.get("ttfs_s")}s total={timing.get("total_s")}s', flush=True)
            return full_text
        except OSError:
            if stop is not None and stop.is_set():
                return ''
            raise
        finally:
            done_ev.set()
            conn.close()

    def archive_cloud(self, provider='chatgpt'):
        """Ask the Pi to archive the finished ChatGPT conversation and prepare a clean fresh chat."""
        if not (self.online and self.cloud_chat):
            return False
        res = self.request('chat_archive', json.dumps({'provider': provider}).encode(), timeout=12.)
        return bool((res or {}).get('archived'))
