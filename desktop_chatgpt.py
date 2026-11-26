"""Ask the signed-in ChatGPT on the Pi through BITCH's own Chromium.

BITCH starts one Chromium with its own profile (~/.bitch-pi/chromium) and the DevTools protocol open on
127.0.0.1 only, then drives the chatgpt.com page directly: open a new chat, paste the request, press Send,
and read the answer from the page when it stops streaming. This is the standard way to automate a browser,
and unlike reading the screen's accessibility tree it doesn't break when ChatGPT moves a button.

The window stays on the Pi desktop, so Brandon can sign in there once and watch it work.
Test it on the Pi with:  python3 desktop_chatgpt.py ask "What is 17 times 23?"
"""
import base64
import json
import os
import re
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

HOME_PAGE = os.environ.get('BITCH_CHATGPT_URL', 'https://chatgpt.com/')
PORT = int(os.environ.get('BITCH_CHROMIUM_PORT', '9223'))
PI_HOME = Path(os.environ.get('BITCH_PI_HOME', '~/.bitch-pi')).expanduser()
PROFILE = PI_HOME / 'chromium'
DEFAULT_PROFILE = Path('~/.config/chromium').expanduser()
SEND_WAIT_SECONDS = 420.
# ChatGPT's thinking levels, most preferred first. Fast answers in a few seconds; think for real reasoning.
MODES = {'fast': (r'^instant', r'^light', r'^fast', r'^auto'),
         'think': (r'^medium', r'^standard', r'^thinking', r'^extended')}
LOAD_SECONDS = 45.       # a Pi 4 needs a while to start ChatGPT's page

# Everything the page script needs to know about ChatGPT's layout lives here, in one place.
PAGE = r'''
(() => {
  const q = (s) => document.querySelector(s);
  const all = (s) => Array.from(document.querySelectorAll(s));
  const visible = (el) => !!el && el.offsetParent !== null;
  const composer = () => all('#prompt-textarea, div.ProseMirror[contenteditable="true"], textarea[name="prompt-textarea"]')
    .find((el) => visible(el) && (el.isContentEditable || el.tagName === 'TEXTAREA')) || null;
  const sendButton = () => all('[data-testid="send-button"], #composer-submit-button, button[aria-label="Send prompt"], button[aria-label="Send message"]')
    .find((el) => visible(el) && !el.disabled && !/stop/i.test(el.getAttribute('aria-label') || '')) || null;
  // ChatGPT renames its markup now and then, so each lookup tries the current names first, then older ones.
  const first = (selectors) => { for (const s of selectors) { const found = all(s); if (found.length) return found; } return []; };
  const replies = () => first(['[data-response-body]', '[data-markdown-text-style="assistant-message"]',
                               '[data-message-author-role="assistant"]']);
  const asks = () => first(['[data-user-message-bubble]', '[data-conversation-role="user"]',
                            '[data-message-author-role="user"]']);
  const streaming = () => {
    if (all('[data-testid="stop-button"], button[aria-label^="Stop"]').find(visible)) return true;
    const turns = all('[data-talvt-turn-state]');
    const last = turns[turns.length - 1];
    return !!last && last.getAttribute('data-talvt-turn-state') !== 'complete';
  };
  const loginButton = () => all('[data-testid="login-button"], a[href*="/auth/login"], button')
    .find((el) => visible(el) && (el.matches('[data-testid="login-button"], a[href*="/auth/login"]') || /^log in$/i.test((el.innerText || '').trim())));
  const textOf = (el) => {
    if (!el) return '';
    const body = el.querySelector('.markdown') || el;
    return (body.innerText || '').trim();
  };
  // The thinking-level pill next to the composer ("Instant", "Medium", ...) and its menu.
  const modeButton = () => {
    const box = composer();
    const scope = (box && box.closest('form')) || document;
    return Array.from(scope.querySelectorAll('button')).find((b) => visible(b) && b.getAttribute('aria-haspopup')
      && /instant|light|medium|heavy|standard|extended|thinking|auto|fast|pro/i.test((b.innerText || '') + ' ' + (b.getAttribute('aria-label') || ''))) || null;
  };
  const modeItems = () => all('[role="menuitemradio"], [role="menuitem"], [role="option"]').filter(visible);
  return {composer, sendButton, streaming, replies, asks, loginButton, textOf, modeButton, modeItems};
})()
'''


# ---------------------------------------------------------------- a small WebSocket client (stdlib only)

class Socket:
    """Just enough of RFC 6455 to talk to Chromium's DevTools endpoint on localhost."""

    def __init__(self, url, timeout=10.):
        parts = urlsplit(url)
        self.sock = socket.create_connection((parts.hostname, parts.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = parts.path + ('?' + parts.query if parts.query else '')
        self.sock.sendall((f'GET {path} HTTP/1.1\r\nHost: {parts.hostname}:{parts.port}\r\n'
                           'Upgrade: websocket\r\nConnection: Upgrade\r\n'
                           f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
        self.buffer = b''
        while b'\r\n\r\n' not in self.buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError('DevTools closed the connection')
            self.buffer += chunk
        head, self.buffer = self.buffer.split(b'\r\n\r\n', 1)
        if b' 101 ' not in head.split(b'\r\n', 1)[0]:
            raise ConnectionError('DevTools refused the connection: ' + head.split(b'\r\n', 1)[0].decode(errors='replace'))

    def send(self, text, opcode=0x1):
        data = text.encode() if isinstance(text, str) else text
        header = bytearray([0x80 | opcode])
        if len(data) < 126:
            header.append(0x80 | len(data))
        elif len(data) < 65536:
            header += bytes([0x80 | 126]) + struct.pack('>H', len(data))
        else:
            header += bytes([0x80 | 127]) + struct.pack('>Q', len(data))
        mask = os.urandom(4)
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, count):
        while len(self.buffer) < count:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError('DevTools closed the connection')
            self.buffer += chunk
        data, self.buffer = self.buffer[:count], self.buffer[count:]
        return data

    def recv(self, timeout):
        self.sock.settimeout(max(.05, timeout))
        message = b''
        while True:
            first, second = self._read(2)
            opcode, size = first & 0x0F, second & 0x7F
            if size == 126:
                size = struct.unpack('>H', self._read(2))[0]
            elif size == 127:
                size = struct.unpack('>Q', self._read(8))[0]
            mask = self._read(4) if second & 0x80 else None
            payload = self._read(size)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise ConnectionError('DevTools closed the connection')
            if opcode == 0x9:
                self.send(payload, 0xA)
                continue
            if opcode == 0xA:
                continue
            message += payload
            if first & 0x80:
                return message.decode('utf-8', errors='replace')

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class Tab:
    """One Chromium page, driven over the DevTools protocol."""

    def __init__(self, websocket_url):
        self.ws = Socket(websocket_url)
        self.next_id = 0

    def call(self, method, params=None, timeout=15.):
        self.next_id += 1
        ident = self.next_id
        self.ws.send(json.dumps({'id': ident, 'method': method, 'params': params or {}}))
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f'{method} took too long')
            message = json.loads(self.ws.recv(left))
            if message.get('id') == ident:
                if 'error' in message:
                    raise RuntimeError(f"{method}: {message['error'].get('message')}")
                return message.get('result', {})

    def run(self, body, timeout=15.):
        """Run a script with ChatGPT's page helpers in scope as `page`, and return its JSON value."""
        expression = f'(async () => {{ const page = {PAGE}; {body} }})()'
        result = self.call('Runtime.evaluate', {'expression': expression, 'awaitPromise': True,
                                                'returnByValue': True}, timeout)
        if result.get('exceptionDetails'):
            details = result['exceptionDetails']
            raise RuntimeError((details.get('exception') or {}).get('description') or details.get('text', 'script failed'))
        return (result.get('result') or {}).get('value')

    def close(self):
        self.ws.close()


# ---------------------------------------------------------------- BITCH's own Chromium

def devtools(path, method='GET', timeout=2.):
    request = urllib.request.Request(f'http://127.0.0.1:{PORT}{path}', method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode() or 'null')


def browser_up():
    try:
        return bool(devtools('/json/version', timeout=1.))
    except (OSError, ValueError, urllib.error.URLError):
        return False


def desktop_env():
    """The Pi desktop session's display variables, found even when BITCH runs from a service."""
    env = dict(os.environ)
    runtime = Path(env.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}')
    if runtime.is_dir():
        env.setdefault('XDG_RUNTIME_DIR', str(runtime))
        if (runtime / 'bus').exists():
            env.setdefault('DBUS_SESSION_BUS_ADDRESS', f'unix:path={runtime / "bus"}')
        if not env.get('WAYLAND_DISPLAY'):
            for candidate in sorted(runtime.glob('wayland-*')):
                if not candidate.name.endswith('.lock'):
                    env['WAYLAND_DISPLAY'] = candidate.name
                    break
    if not env.get('WAYLAND_DISPLAY') and not env.get('DISPLAY') and Path('/tmp/.X11-unix/X0').exists():
        env['DISPLAY'] = ':0'
    return env


def chromium_path():
    # The real binary, not Raspberry Pi OS's wrapper script, which forces renderer accessibility on every page.
    if os.access('/usr/lib/chromium/chromium', os.X_OK):
        return '/usr/lib/chromium/chromium'
    return shutil.which('chromium') or shutil.which('chromium-browser') or shutil.which('google-chrome')


def stop_other_chromium():
    """Close Chromium windows that aren't BITCH's, so the copied sign-in is consistent."""
    subprocess.run(['pkill', '-TERM', '-u', str(os.getuid()), '-f', r'chromium(-browser)?( |$)'],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 8.
    while time.monotonic() < deadline:
        if subprocess.run(['pgrep', '-u', str(os.getuid()), '-f', 'chromium'],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
            return
        time.sleep(.25)
    subprocess.run(['pkill', '-KILL', '-u', str(os.getuid()), '-f', 'chromium'],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def seed_profile():
    """First run: copy the Pi's normal Chromium profile so BITCH's browser is already signed in."""
    if PROFILE.exists():
        return
    PI_HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
    if DEFAULT_PROFILE.is_dir():
        stop_other_chromium()
        skip = shutil.ignore_patterns('Singleton*', '*Cache*', 'Crash Reports', 'GPUCache', 'Service Worker',
                                      'component_crx_cache', 'BrowserMetrics*', 'DeferredBrowserMetrics')
        try:
            shutil.copytree(DEFAULT_PROFILE, PROFILE, ignore=skip, symlinks=True)
            return
        except (OSError, shutil.Error):
            shutil.rmtree(PROFILE, ignore_errors=True)
    PROFILE.mkdir(parents=True, exist_ok=True, mode=0o700)


def mark_clean_exit():
    """Skip Chromium's "restore pages?" bubble after a power cut."""
    prefs = PROFILE / 'Default' / 'Preferences'
    try:
        data = json.loads(prefs.read_text(encoding='utf-8'))
        profile = data.setdefault('profile', {})
        if profile.get('exit_type') != 'Normal' or not profile.get('exited_cleanly', True):
            profile.update(exit_type='Normal', exited_cleanly=True)
            temporary = prefs.with_suffix('.bitch.tmp')
            temporary.write_text(json.dumps(data), encoding='utf-8')
            temporary.replace(prefs)
    except (OSError, ValueError, AttributeError):
        pass


def start_browser(wait=30.):
    """Make sure BITCH's Chromium is running with DevTools on localhost. Returns an error or None."""
    if browser_up():
        return None
    chromium = chromium_path()
    if not chromium:
        return 'Chromium is not installed on the Pi.'
    env = desktop_env()
    if not env.get('WAYLAND_DISPLAY') and not env.get('DISPLAY'):
        return 'No desktop session on the Pi. Log in to the Pi desktop first.'
    seed_profile()
    mark_clean_exit()
    for name in ('SingletonLock', 'SingletonSocket', 'SingletonCookie'):
        # A stale lock from a crash makes Chromium hand the URL to a process that no longer exists.
        path = PROFILE / name
        if path.is_symlink() and not browser_up():
            path.unlink(missing_ok=True)
    # --password-store=basic: Chromium otherwise waits on the GNOME keyring for the key to its cookies, and when
    # the keyring is stuck (two daemons, or locked with no prompt on screen) every page hangs on "Loading...".
    # The profile folder is private to this user (0700). No accessibility bridge: it only slows the Pi down.
    command = [chromium, f'--user-data-dir={PROFILE}', f'--remote-debugging-port={PORT}',
               '--remote-debugging-address=127.0.0.1', '--no-first-run', '--no-default-browser-check',
               '--password-store=basic', '--disable-renderer-accessibility',
               '--hide-crash-restore-bubble', '--disable-session-crashed-bubble',
               '--disable-background-timer-throttling', '--disable-renderer-backgrounding',
               '--disable-backgrounding-occluded-windows', HOME_PAGE]
    env['NO_AT_BRIDGE'] = '1'
    names = [key for key in ('XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS', 'WAYLAND_DISPLAY', 'DISPLAY', 'NO_AT_BRIDGE')
             if env.get(key)]
    service = shutil.which('systemd-run')
    launched = False
    if service:
        # Its own unit, so the browser outlives a restart of brain.py.
        launched = subprocess.run(
            [service, '--user', '--quiet', '--collect', '--unit', f'bitch-chromium-{int(time.time())}',
             '/usr/bin/env', *(f'{key}={env[key]}' for key in names), *command],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, timeout=10).returncode == 0
    if not launched:
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, start_new_session=True)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if browser_up():
            return None
        time.sleep(.5)
    return 'BITCH’s Chromium did not start on the Pi.'


def chatgpt_tab():
    """Reuse the ChatGPT tab (closing extra ones), or open one."""
    pages = [item for item in devtools('/json/list') if item.get('type') == 'page']
    site = urlsplit(HOME_PAGE).hostname
    chat = [item for item in pages if urlsplit(item.get('url', '')).hostname == site]
    keep = chat[0] if chat else pages[0] if pages else None
    for item in chat[1:]:
        try:
            devtools('/json/close/' + item['id'])
        except (OSError, ValueError, urllib.error.URLError):
            pass
    if keep is None:
        keep = devtools('/json/new?' + HOME_PAGE, method='PUT', timeout=5.)
    try:
        devtools('/json/activate/' + keep['id'])
    except (OSError, ValueError, urllib.error.URLError):
        pass
    return Tab(keep['webSocketDebuggerUrl'])


# ---------------------------------------------------------------- asking

def normalized(text):
    return ' '.join((text or '').split())


def open_new_chat(tab):
    """Load a fresh chat and wait for the composer. Returns an error or None."""
    tab.call('Page.enable')
    tab.call('Page.navigate', {'url': HOME_PAGE})
    deadline = time.monotonic() + LOAD_SECONDS
    state = {}
    while time.monotonic() < deadline:
        time.sleep(.8)
        try:
            state = tab.run('return {ready: document.readyState, composer: !!page.composer(), '
                            'login: !!page.loginButton(), url: location.href, title: document.title};', 6.)
        except (RuntimeError, TimeoutError):
            continue
        if state and state.get('composer') and state.get('ready') != 'loading':
            if state.get('login'):
                # Signed out, ChatGPT still answers simple questions but has no access to the calendar or email.
                return 'Sign in to ChatGPT in the BITCH Chromium window on the Pi.'
            return None
    if state and state.get('login'):
        return 'Sign in to ChatGPT in the BITCH Chromium window on the Pi.'
    if state and 'just a moment' in (state.get('title') or '').casefold():
        return 'ChatGPT is showing a “verify you are human” check on the Pi. Click it once in the BITCH Chromium window.'
    return 'ChatGPT did not finish loading on the Pi.'


def enter_prompt(tab, prompt):
    """Paste the request into the composer the way a person would. Returns True when it's in."""
    text = json.dumps(prompt)
    tab.run(f'''
        const box = page.composer();
        if (!box) return false;
        box.focus();
        if (box.tagName === 'TEXTAREA') {{
          const set = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
          set.call(box, {text});
          box.dispatchEvent(new Event('input', {{bubbles: true}}));
          return true;
        }}
        const data = new DataTransfer();
        data.setData('text/plain', {text});
        box.dispatchEvent(new ClipboardEvent('paste', {{clipboardData: data, bubbles: true, cancelable: true}}));
        return true;''')
    check = 'const box = page.composer(); return box ? (box.value ?? box.innerText) : "";'
    want = normalized(prompt)
    for _ in range(10):
        time.sleep(.2)
        if normalized(tab.run(check)) == want:
            return True
    # Fallback: type it as one line through the browser's own input path.
    tab.run('const box = page.composer(); if (box) { box.focus(); document.execCommand("selectAll"); '
            'document.execCommand("delete"); } return true;')
    tab.call('Input.insertText', {'text': ' '.join(line.strip() for line in prompt.splitlines() if line.strip())})
    time.sleep(.4)
    return bool(normalized(tab.run(check)))


def press_send(tab):
    for _ in range(25):
        if tab.run('const b = page.sendButton(); if (b) { b.click(); return true; } return false;'):
            return True
        time.sleep(.2)
    # No button found: Enter sends in ChatGPT's composer.
    tab.run('const box = page.composer(); if (box) box.focus(); return true;')
    for kind in ('keyDown', 'keyUp'):
        tab.call('Input.dispatchKeyEvent', {'type': kind, 'key': 'Enter', 'code': 'Enter',
                                            'windowsVirtualKeyCode': 13, 'text': '\r' if kind == 'keyDown' else ''})
    return True


def read_reply(tab, before, wait=SEND_WAIT_SECONDS):
    """Wait for the new answer to finish streaming and return its text."""
    deadline = time.monotonic() + wait
    sent = False
    last, stable = None, 0
    started = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(1.)
        try:
            state = tab.run('const r = page.replies(); return {asks: page.asks().length, count: r.length, '
                            'streaming: page.streaming(), text: page.textOf(r[r.length - 1]), url: location.href};', 8.)
        except (RuntimeError, TimeoutError):
            continue
        sent = sent or state['asks'] > before[0] or '/c/' in state['url']
        if not sent and time.monotonic() - started > 20:
            return {'state': 'ready', 'drafted': True, 'reason': 'ChatGPT on the Pi did not take the message.'}
        if state['count'] <= before[1] or state['streaming'] or not state['text']:
            last, stable = None, 0
            continue
        if state['text'] == last:
            stable += 1
            if stable >= 2:
                url = state['url'] if '/c/' in state['url'] else None
                return {'state': 'returned', 'answer': state['text'][:12000], 'url': url}
        else:
            last, stable = state['text'], 0
    return {'state': 'submitted' if sent else 'send_unverified', 'reason': 'ChatGPT took too long to finish.'}


def run_chatgpt(prompt, *, send, mode=None):
    problem = start_browser()
    if problem:
        return {'state': 'ready', 'reason': problem}
    tab = None
    sent = False
    try:
        tab = chatgpt_tab()
        problem = open_new_chat(tab)
        if problem:
            return {'state': 'ready', 'reason': problem}
        if mode:
            try:
                choose_mode(tab, mode)
            except (RuntimeError, TimeoutError):
                close_menu(tab)
        if not enter_prompt(tab, prompt):
            return {'state': 'ready', 'reason': 'Could not type the request into ChatGPT on the Pi.'}
        if not send:
            return {'state': 'drafted'}
        before = tab.run('return [page.asks().length, page.replies().length];')
        press_send(tab)
        sent = True
        return read_reply(tab, before)
    except (OSError, ValueError, RuntimeError, TimeoutError, KeyError, TypeError, urllib.error.URLError) as error:
        if sent:
            return {'state': 'send_unverified', 'reason': f'Lost the ChatGPT page while it answered: {error}'}
        return {'state': 'ready', 'reason': f'Could not drive ChatGPT on the Pi: {error}'}
    finally:
        if tab is not None:
            tab.close()


def open_mode_menu(tab):
    """Open the thinking-level menu. Returns (current label, [option labels]) or (None, [])."""
    label = tab.run('const b = page.modeButton(); if (!b) return null; b.focus(); '
                    'for (const t of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) '
                    '  b.dispatchEvent(new MouseEvent(t, {bubbles: true})); '
                    'return (b.innerText || b.getAttribute("aria-label") || "").trim();')
    if label is None:
        return None, []
    for _ in range(10):
        time.sleep(.25)
        items = tab.run('return page.modeItems().map((e) => (e.innerText || "").trim().split("\\n")[0]);')
        if items:
            return label, items
    return label, []


def close_menu(tab):
    for kind in ('keyDown', 'keyUp'):
        tab.call('Input.dispatchKeyEvent', {'type': kind, 'key': 'Escape', 'code': 'Escape', 'windowsVirtualKeyCode': 27})


def press(tab, key, code=None, keycode=0):
    for kind in ('keyDown', 'keyUp'):
        tab.call('Input.dispatchKeyEvent', {'type': kind, 'key': key, 'code': code or key, 'windowsVirtualKeyCode': keycode})


def choose_mode(tab, mode):
    """Set ChatGPT's thinking effort for this request: 'fast' (Instant) or 'think' (Medium).

    The effort control is a slider in the menu next to the composer (Instant, Medium, then higher). Setting it
    with the keyboard is the standard way to drive a slider. Returns the level ChatGPT shows afterwards, or None
    if the control wasn't found (then ChatGPT just uses whatever level it was on)."""
    if mode not in MODES:
        return None
    label, items = open_mode_menu(tab)
    if label is None:
        return None
    level = None
    focused = tab.run('const s = Array.from(document.querySelectorAll(\'[role="slider"]\')).find((e) => e.offsetParent); '
                      'if (!s) return false; s.focus(); return true;')
    if focused:
        press(tab, 'Home', keycode=36)                       # Instant
        if mode == 'think':
            press(tab, 'ArrowRight', keycode=39)             # one step up: Medium
        time.sleep(.3)
        level = tab.run('const s = Array.from(document.querySelectorAll(\'[role="slider"]\')).find((e) => e.offsetParent); '
                        'return s ? (s.getAttribute("aria-valuetext") || s.getAttribute("aria-valuenow")) : null;')
    else:
        # An older layout listed the levels as menu items instead.
        for pattern in MODES[mode]:
            index = next((i for i, text in enumerate(items) if re.search(pattern, text, re.I)), None)
            if index is not None:
                tab.run(f'const e = page.modeItems()[{index}]; if (e) e.click(); return true;')
                level = items[index]
                break
    close_menu(tab)
    time.sleep(.2)
    return level


def draft(prompt):
    return run_chatgpt(prompt, send=False).get('state') == 'drafted'


def submit(prompt, mode=None):
    return run_chatgpt(prompt, send=True, mode=mode)


def status():
    """What BITCH's ChatGPT browser looks like right now, for /ping and for checking by hand."""
    if not browser_up():
        return {'browser': False}
    tab = None
    try:
        tab = chatgpt_tab()
        state = tab.run('return {url: location.href, composer: !!page.composer(), login: !!page.loginButton(), '
                        'title: document.title};', 6.)
        return {'browser': True, **(state or {})}
    except (OSError, ValueError, RuntimeError, TimeoutError, KeyError, urllib.error.URLError) as error:
        return {'browser': True, 'error': str(error)}
    finally:
        if tab is not None:
            tab.close()


if __name__ == '__main__':
    if sys.argv[1:2] == ['restart']:
        subprocess.run(['pkill', '-u', str(os.getuid()), '-f', f'remote-debugging-port={PORT}'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)
        print(start_browser() or f'BITCH Chromium restarted (DevTools on 127.0.0.1:{PORT}).')
    elif sys.argv[1:2] == ['start']:
        print(start_browser() or f'BITCH Chromium is running (DevTools on 127.0.0.1:{PORT}).')
    elif sys.argv[1:2] == ['modes']:
        start_browser()
        tab = chatgpt_tab()
        try:
            print(json.dumps(dict(zip(('current', 'options'), open_mode_menu(tab))), indent=2))
            close_menu(tab)
        finally:
            tab.close()
    elif sys.argv[1:2] == ['status']:
        print(json.dumps(status(), indent=2))
    elif sys.argv[1:2] == ['ask'] and len(sys.argv) > 2:
        mode = sys.argv[2] if sys.argv[2] in MODES else None
        question = ' '.join(sys.argv[3:] if mode else sys.argv[2:])
        started = time.monotonic()
        print(json.dumps(submit(question, mode=mode), indent=2, ensure_ascii=False))
        print(f'{time.monotonic() - started:.1f} s')
    else:
        print('usage: python3 desktop_chatgpt.py start | restart | status | modes | ask [fast|think] "question"')
