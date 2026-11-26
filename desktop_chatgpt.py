"""Use the signed-in Chromium desktop to draft and send a ChatGPT handoff."""
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from uuid import uuid4

HOME_PAGE = 'https://chatgpt.com/'
CHAT_PATH = re.compile(r'/c/[a-f0-9-]{36}/?')
SEND_WAIT_SECONDS = 420.


def draft(prompt):
    return run_desktop(prompt, send=False).get('state') == 'drafted'


def submit(prompt):
    return run_desktop(prompt, send=True)


def desktop_env():
    env = dict(os.environ)
    env.pop('NO_AT_BRIDGE', None)
    env['ACCESSIBILITY_ENABLED'] = '1'
    runtime = Path(env.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}')
    if runtime.is_dir():
        env.setdefault('XDG_RUNTIME_DIR', str(runtime))
        if (runtime / 'bus').exists():
            env.setdefault('DBUS_SESSION_BUS_ADDRESS', f'unix:path={runtime / "bus"}')
        if not env.get('WAYLAND_DISPLAY'):
            for candidate in sorted(runtime.glob('wayland-*')):
                if candidate.name.endswith('.lock'):
                    continue
                try:
                    if stat.S_ISSOCK(candidate.stat().st_mode):
                        env['WAYLAND_DISPLAY'] = candidate.name
                        break
                except OSError:
                    continue
    if not env.get('WAYLAND_DISPLAY') and not env.get('DISPLAY') and Path('/tmp/.X11-unix/X0').exists():
        env['DISPLAY'] = ':0'
    return env


def _spawn_worker(command, prompt, timeout, env):
    with tempfile.TemporaryFile('w+', encoding='utf-8') as output:
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=output, stderr=subprocess.DEVNULL,
            encoding='utf-8', env=env,
        )
        try:
            process.communicate(prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            return None, True
        output.seek(0)
        raw = output.read().strip()
        return (json.loads(raw) if raw else None), False


def run_desktop(prompt, *, send):
    env = desktop_env()
    mode = 'send' if send else 'draft'
    script = str(Path(__file__).resolve())
    timeout = int(SEND_WAIT_SECONDS) + 45 if send else 54
    max_sec = f'--property=RuntimeMaxSec={timeout - 5}s'
    service = shutil.which('systemd-run')
    env_args = ['/usr/bin/env', '-u', 'NO_AT_BRIDGE', 'ACCESSIBILITY_ENABLED=1']
    for key in ('XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS', 'WAYLAND_DISPLAY', 'DISPLAY'):
        if env.get(key):
            env_args.append(f'{key}={env[key]}')
    # The result goes to a file, not a pipe: a timed-out run whose service still held the pipe
    # could block forever, leaving the request stuck in "sending" and every later request behind it.
    try:
        if service:
            parsed, timed_out = _spawn_worker(
                [service, '--user', '--quiet', '--wait', '--pipe', '--collect', max_sec,
                 *env_args, sys.executable, script, mode],
                prompt, timeout, env,
            )
            if timed_out:
                return {'state': 'send_unverified', 'reason': 'Timed out waiting for ChatGPT.'} if send else {'state': 'ready'}
            if isinstance(parsed, dict):
                return parsed
        parsed, timed_out = _spawn_worker([*env_args, sys.executable, script, mode], prompt, timeout, env)
        if timed_out:
            return {'state': 'send_unverified', 'reason': 'Timed out waiting for ChatGPT.'} if send else {'state': 'ready'}
        if isinstance(parsed, dict):
            return parsed
        return {'state': 'ready', 'reason': 'Could not start the Pi desktop helper.'}
    except (OSError, ValueError) as error:
        return ({'state': 'send_unverified', 'reason': str(error)} if send else {'state': 'ready'})


def children(node):
    try:
        count = node.get_child_count()
    except Exception:
        return
    for index in range(count):
        try:
            child = node.get_child_at_index(index)
        except Exception:
            child = None
        if child is not None:
            yield child


def is_nav(node, atspi):
    try:
        if node.get_role() == getattr(atspi.Role, 'NAVIGATION', None):
            return True
        attrs = node.get_attributes() or {}
        return attrs.get('tag') == 'nav' or attrs.get('xml-roles') == 'navigation'
    except Exception:
        return False


def nodes(root, *, skip_nav=False, limit=6000):
    stack = [root]
    for _ in range(limit):
        if not stack:
            return
        node = stack.pop()
        if skip_nav and node is not root and is_nav(node, _atspi()):
            continue
        yield node
        stack.extend(reversed(list(children(node))))


def _atspi():
    from gi.repository import Atspi
    return Atspi


def text_of(node):
    Atspi = _atspi()
    text = node.get_text_iface()
    value = Atspi.Text.get_text(text, 0, -1) if text is not None else ''
    return (value or '').replace('\ufffc', ' ')


def full_text(node, depth=0):
    """Read accessible text recursively, expanding embedded child objects (\ufffc)."""
    if depth > 8 or node is None:
        return ''
    Atspi = _atspi()
    iface = node.get_text_iface()
    raw = (Atspi.Text.get_text(iface, 0, -1) if iface is not None else '') or ''
    kids = list(children(node))
    if '\ufffc' in raw and kids:
        parts = raw.split('\ufffc')
        rebuilt = [parts[0]]
        for index, part in enumerate(parts[1:]):
            child_text = full_text(kids[index], depth + 1) if index < len(kids) else ''
            rebuilt.append(child_text)
            rebuilt.append(part)
        joined = ''.join(rebuilt).strip()
        if joined:
            return joined
    cleaned = raw.replace('\ufffc', ' ').strip()
    if cleaned:
        return cleaned
    if kids:
        sub = [full_text(child, depth + 1) for child in kids]
        return '\n'.join(piece for piece in sub if piece)
    return (node.get_name() or '').strip()


def composer_text(composer, atspi):
    # Chromium exposes the editor's paragraph as an embedded accessible object.
    paragraphs = [full_text(node) for node in children(composer) if node.get_role() == atspi.Role.PARAGRAPH]
    return '\n'.join(paragraphs) if paragraphs else full_text(composer)


def _parse_chatgpt_url(address):
    address = (address or '').strip()
    if address == 'chatgpt.com' or address.startswith(('chatgpt.com/', 'chatgpt.com#', 'chatgpt.com?')):
        address = 'https://' + address
    return urlsplit(address)


def matches_address(address, marker):
    # Chromium normally hides the scheme in the accessibility value of the address bar.
    url = _parse_chatgpt_url(address)
    return url.scheme == 'https' and url.netloc == 'chatgpt.com' and url.path in ('', '/') and (
        not url.query and url.fragment == marker)


def is_home_address(address, marker=''):
    if matches_address(address, marker):
        return True
    url = _parse_chatgpt_url(address)
    return url.scheme == 'https' and url.netloc == 'chatgpt.com' and url.path in ('', '/')


def chat_url(address):
    url = _parse_chatgpt_url(address)
    if url.scheme == 'https' and url.netloc == 'chatgpt.com' and CHAT_PATH.fullmatch(url.path):
        return 'https://chatgpt.com' + url.path.rstrip('/')
    return None


def is_chatgpt_address(address):
    url = _parse_chatgpt_url(address)
    return url.scheme == 'https' and url.netloc == 'chatgpt.com'


def normalized(text):
    return ' '.join((text or '').split())


def browser_windows(atspi):
    found = []
    desktop = atspi.get_desktop(0)
    for app in children(desktop):
        name = (app.get_name() or '').casefold()
        if not ('chromium' in name or 'google chrome' in name or name == 'chrome'):
            continue
        for window in children(app):
            active = window.get_state_set().contains(atspi.StateType.ACTIVE)
            found.append((not active, window))
    found.sort(key=lambda item: item[0])
    return [window for _, window in found]


def focus_window(window):
    wlrctl = shutil.which('wlrctl')
    if wlrctl:
        for selector in ('app_id:chromium', 'app_id:chromium-browser', 'app_id:Chromium', 'title:ChatGPT'):
            res = subprocess.run([wlrctl, 'toplevel', 'focus', selector],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1.5)
            if res.returncode == 0:
                break
    try:
        comp = window.get_component_iface()
        if comp is not None:
            comp.grab_focus()
    except Exception:
        pass


def page_of(window, atspi):
    address = document = None
    for node in nodes(window, limit=800):
        role = node.get_role()
        name = (node.get_name() or '').casefold()
        if role == atspi.Role.ENTRY and ('address' in name or 'search bar' in name or 'url' in name):
            address = node
        elif role == atspi.Role.DOCUMENT_WEB:
            document = node
        if address is not None and document is not None:
            return window, address, document
    return None


def active_page(atspi):
    candidates = []
    for window in browser_windows(atspi):
        page = page_of(window, atspi)
        if page is not None:
            if is_chatgpt_address(text_of(page[1])):
                return page
            candidates.append(page)
    return candidates[0] if candidates else None


def select_open_chat(atspi):
    for window in browser_windows(atspi):
        page = page_of(window, atspi)
        if page is not None and is_chatgpt_address(text_of(page[1])):
            focus_window(window)
            return True
        for node in nodes(window, limit=900):
            if node.get_role() == atspi.Role.PAGE_TAB and 'chatgpt' in (node.get_name() or '').casefold():
                focus_window(window)
                if node.do_action(0):
                    return True
    return False


def click_new_chat(document, atspi):
    for node in nodes(document, limit=2500):
        role = node.get_role()
        label = (node.get_name() or '').strip().casefold()
        if role in (atspi.Role.PUSH_BUTTON, atspi.Role.LINK) and label in ('new chat', 'start new chat'):
            if node.do_action(0):
                return True
    return False


def is_composer(node, atspi):
    if node.get_role() not in (atspi.Role.ENTRY, atspi.Role.TEXT, atspi.Role.PARAGRAPH, atspi.Role.SECTION):
        return False
    if not node.get_state_set().contains(atspi.StateType.EDITABLE):
        return False
    attrs = node.get_attributes() or {}
    if attrs.get('id') == 'pending-home-input':
        return False
    if attrs.get('id') == 'prompt-textarea':
        return True
    label = (node.get_name() or '').strip().casefold()
    return any(phrase in label for phrase in ('ask chatgpt', 'ask anything', 'message chatgpt', 'send a message'))


def find_composer(atspi, marker, *, allow_home=False):
    page = active_page(atspi)
    if page is None:
        return None
    addr = text_of(page[1])
    if not (is_home_address(addr, marker) if allow_home else matches_address(addr, marker)):
        return None
    window, address, document = page
    for node in nodes(document, skip_nav=True):
        if is_composer(node, atspi):
            return window, address, node, document
    return None


def logged_out(document, atspi):
    for node in nodes(document, skip_nav=True, limit=2500):
        if node.get_role() in (atspi.Role.PUSH_BUTTON, atspi.Role.LINK):
            if (node.get_name() or '').strip().casefold() in ('log in', 'sign in'):
                return True
    return False


def send_button(document, atspi):
    for node in nodes(document, skip_nav=True):
        if node.get_role() != atspi.Role.PUSH_BUTTON:
            continue
        if not node.get_state_set().contains(atspi.StateType.ENABLED):
            continue
        attrs = node.get_attributes() or {}
        label = (node.get_name() or '').strip().casefold()
        if (attrs.get('id') == 'composer-submit-button'
                or attrs.get('data-testid') == 'send-button'
                or label in ('send', 'send prompt', 'send message', 'submit')):
            return node
    return None


def is_generating(document, atspi):
    for node in nodes(document, skip_nav=True):
        if node.get_role() != atspi.Role.PUSH_BUTTON:
            continue
        attrs = node.get_attributes() or {}
        label = (node.get_name() or '').strip().casefold()
        if (label in ('stop streaming', 'stop generating', 'stop response')
                or attrs.get('data-testid') == 'stop-button'
                or attrs.get('id') == 'composer-submit-button' and 'stop' in label):
            return True
    return False


def prompt_matches(text, prompt):
    norm_text = normalized(text)
    norm_prompt = normalized(prompt)
    if not norm_text or not norm_prompt:
        return False
    if norm_text == norm_prompt or norm_prompt in norm_text:
        return True
    # Multi-paragraph prompts can be split across sibling blocks; match the unique request body.
    lines = [normalized(part) for part in prompt.splitlines() if len(normalized(part)) >= 12]
    body = next((line for line in lines if not line.startswith(('BITCH robot handoff', 'I’m BITCH', 'Request from', 'Reply for BITCH', 'Put the main answer'))), '')
    return bool(body and body in norm_text)


def extract_turn_state(document, prompt, atspi):
    """Locate our user turn, the assistant reply text after it, and its Copy button."""
    seen_user = False
    in_assistant = False
    assistant_parts = []
    copy_btn = None
    last_copy_btn = None
    last_assistant_parts = []

    for node in nodes(document, skip_nav=True):
        role = node.get_role()
        label = (node.get_name() or '').strip().casefold()
        attrs = node.get_attributes() or {}
        author = attrs.get('data-message-author-role', '')

        if role == atspi.Role.HEADING:
            heading = label.rstrip(':')
            if heading in ('you said', 'user'):
                in_assistant = False
            elif heading in ('chatgpt said', 'assistant') and seen_user:
                in_assistant = True
                assistant_parts = []
                copy_btn = None
                continue
        if author == 'user':
            in_assistant = False
            if prompt_matches(full_text(node), prompt):
                seen_user = True
            continue
        if author == 'assistant' and seen_user:
            text = full_text(node).strip()
            if text:
                last_assistant_parts = [text]
            in_assistant = True
            continue

        if not in_assistant and role in (atspi.Role.PARAGRAPH, atspi.Role.SECTION):
            if prompt_matches(full_text(node), prompt):
                seen_user = True
        elif in_assistant:
            if role == atspi.Role.PUSH_BUTTON and label in ('copy', 'copy response', 'copy message'):
                copy_btn = node
                last_copy_btn = node
                if assistant_parts:
                    last_assistant_parts = list(assistant_parts)
                in_assistant = False
            elif role in (atspi.Role.PARAGRAPH, atspi.Role.LIST_ITEM, atspi.Role.HEADING):
                piece = full_text(node).strip()
                if piece and piece.casefold().rstrip(':') not in ('chatgpt said', 'assistant'):
                    if not assistant_parts or piece not in assistant_parts[-1]:
                        assistant_parts.append(piece)

    if assistant_parts and not last_assistant_parts:
        last_assistant_parts = assistant_parts
    answer_text = '\n'.join(last_assistant_parts).strip()
    return seen_user, last_copy_btn or copy_btn, answer_text


def read_clipboard():
    if os.environ.get('WAYLAND_DISPLAY') and shutil.which('wl-paste'):
        res = subprocess.run(['wl-paste', '--no-newline'], capture_output=True,
                             text=True, encoding='utf-8', timeout=2)
        if res.returncode == 0:
            return res.stdout.strip()
    if os.environ.get('DISPLAY') and shutil.which('xclip'):
        res = subprocess.run(['xclip', '-selection', 'clipboard', '-o'], capture_output=True,
                             text=True, encoding='utf-8', timeout=2)
        if res.returncode == 0:
            return res.stdout.strip()
    return ''


def clear_clipboard():
    if os.environ.get('WAYLAND_DISPLAY') and shutil.which('wl-copy'):
        subprocess.run(['wl-copy', '--clear'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)


def wait_response(prompt, atspi, context, errors):
    end = time.monotonic() + SEND_WAIT_SECONDS
    conversation = None
    stable_ticks = 0
    previous_text = ''
    sent_at = time.monotonic()

    while time.monotonic() < end:
        while context.pending():
            context.iteration(False)
        try:
            page = active_page(atspi)
            if page and is_chatgpt_address(text_of(page[1])):
                window, address_node, document = page
                url = chat_url(text_of(address_node))
                if url and conversation is None:
                    conversation = url
                if url is None or conversation is None or url == conversation:
                    matched, button, direct_answer = extract_turn_state(document, prompt, atspi)
                    if matched and url:
                        conversation = url
                    generating = is_generating(document, atspi)
                    if matched and not generating and (button is not None or (direct_answer and time.monotonic() - sent_at > 4.)):
                        if direct_answer == previous_text and (button is not None or len(direct_answer) > 2):
                            stable_ticks += 1
                        else:
                            previous_text = direct_answer
                            stable_ticks = 0
                        if stable_ticks >= 1:
                            if button is not None:
                                focus_window(window)
                                clear_clipboard()
                                if button.do_action(0):
                                    time.sleep(.3)
                                    copied = read_clipboard()
                                    if copied and normalized(copied) != normalized(prompt) and not prompt_matches(copied, prompt):
                                        return {'state': 'returned', 'answer': copied[:12000], 'url': conversation}
                            if direct_answer and normalized(direct_answer) != normalized(prompt) and not prompt_matches(direct_answer, prompt):
                                return {'state': 'returned', 'answer': direct_answer[:12000], 'url': conversation}
                    else:
                        stable_ticks = 0
        except errors:
            pass
        time.sleep(.65)
    if conversation:
        return {'state': 'submitted', 'url': conversation, 'reason': 'ChatGPT took too long to finish.'}
    return {'state': 'send_unverified', 'reason': 'Could not read the answer back from Chromium on the Pi.'}


def chromium_running():
    res = subprocess.run(['pgrep', '-u', str(os.getuid()), '-f', '(chromium|chrome).*--type=renderer'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if res.returncode == 0:
        return True
    res = subprocess.run(['pgrep', '-u', str(os.getuid()), '-x', 'chromium'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return res.returncode == 0


def clean_chromium_exit_flag():
    prefs = Path.home() / '.config' / 'chromium' / 'Default' / 'Preferences'
    if not prefs.is_file():
        return
    try:
        data = json.loads(prefs.read_text(encoding='utf-8'))
        profile = data.get('profile')
        if isinstance(profile, dict) and (profile.get('exit_type') != 'Normal' or not profile.get('exited_cleanly', True)):
            profile['exit_type'] = 'Normal'
            profile['exited_cleanly'] = True
            tmp = prefs.with_suffix('.bitch.tmp')
            tmp.write_text(json.dumps(data), encoding='utf-8')
            tmp.replace(prefs)
    except (OSError, ValueError):
        pass


def launch_chromium(chromium, url, *, restore=False):
    env = desktop_env()
    flags = [
        '--force-renderer-accessibility',
        '--no-first-run',
        '--no-default-browser-check',
        '--hide-crash-restore-bubble',
    ]
    if restore:
        flags.append('--restore-last-session')
    service = shutil.which('systemd-run')
    env_args = ['/usr/bin/env', '-u', 'NO_AT_BRIDGE', 'ACCESSIBILITY_ENABLED=1']
    for key in ('XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS', 'WAYLAND_DISPLAY', 'DISPLAY'):
        if env.get(key):
            env_args.append(f'{key}={env[key]}')
    if service:
        res = subprocess.run([service, '--user', '--quiet', '--collect', *env_args, chromium, *flags, url],
                             timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        if res.returncode == 0:
            return
    subprocess.Popen([*env_args, chromium, *flags, url],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, start_new_session=True)


def restart_chromium_with_accessibility(chromium, url):
    for pattern in ('chromium', 'chromium-browser', 'chrome'):
        subprocess.run(['pkill', '-TERM', '-u', str(os.getuid()), '-f', pattern],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 6.
    while time.monotonic() < deadline and chromium_running():
        time.sleep(.25)
    clean_chromium_exit_flag()
    launch_chromium(chromium, url, restore=False)


def set_composer_via_atspi(composer, prompt, atspi, context):
    try:
        comp = composer.get_component_iface()
        if comp is not None:
            comp.grab_focus()
        editable = composer.get_editable_text_iface()
        if editable is None:
            return False
        if not atspi.EditableText.set_text_contents(editable, prompt):
            return False
        for _ in range(8):
            time.sleep(.12)
            while context.pending():
                context.iteration(False)
            if normalized(composer_text(composer, atspi)) == normalized(prompt):
                return True
    except Exception:
        pass
    return False


def paste(prompt, window, composer, copy_bin, keys_bin, atspi, context):
    if not copy_bin or not keys_bin:
        return False
    focus_window(window)
    try:
        comp = composer.get_component_iface()
        if comp is not None:
            comp.grab_focus()
    except Exception:
        pass
    time.sleep(.15)
    if os.environ.get('WAYLAND_DISPLAY') and 'wl-copy' in copy_bin:
        with subprocess.Popen([copy_bin, '--foreground', '--type', 'text/plain'], stdin=subprocess.PIPE) as clipboard:
            try:
                clipboard.stdin.write(prompt.encode('utf-8'))
                clipboard.stdin.close()
                time.sleep(.16)
                focus_window(window)
                try:
                    comp = composer.get_component_iface()
                    if comp is not None:
                        comp.grab_focus()
                except Exception:
                    pass
                time.sleep(.1)
                subprocess.run([keys_bin, '-M', 'ctrl', '-s', '80', '-k', 'v', '-s', '80', '-m', 'ctrl'],
                               check=True, timeout=3)
                for _ in range(14):
                    time.sleep(.15)
                    while context.pending():
                        context.iteration(False)
                    if normalized(composer_text(composer, atspi)) == normalized(prompt):
                        return True
                return False
            finally:
                if clipboard.poll() is None:
                    clipboard.terminate()
                    try:
                        clipboard.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        clipboard.kill()
    elif os.environ.get('DISPLAY') and 'xclip' in copy_bin:
        subprocess.run([copy_bin, '-selection', 'clipboard'], input=prompt, text=True, check=True, timeout=3)
        subprocess.run([keys_bin, 'key', '--clearmodifiers', 'ctrl+v'], check=True, timeout=3)
        for _ in range(14):
            time.sleep(.15)
            while context.pending():
                context.iteration(False)
            if normalized(composer_text(composer, atspi)) == normalized(prompt):
                return True
    return False


def fill(prompt, *, send=False):
    env = desktop_env()
    os.environ.update(env)
    os.environ.pop('NO_AT_BRIDGE', None)

    import gi

    gi.require_version('Atspi', '2.0')
    from gi.repository import Atspi, GLib

    if not os.environ.get('WAYLAND_DISPLAY') and not os.environ.get('DISPLAY'):
        return {'state': 'ready', 'reason': 'No desktop display session found on the Pi.'} if send else False
    chromium = shutil.which('chromium') or shutil.which('chromium-browser') or shutil.which('google-chrome')
    if not chromium:
        return {'state': 'ready', 'reason': 'Chromium is not installed on the Pi.'} if send else False
    copy_bin = shutil.which('wl-copy') if os.environ.get('WAYLAND_DISPLAY') else shutil.which('xclip')
    keys_bin = shutil.which('wtype') if os.environ.get('WAYLAND_DISPLAY') else shutil.which('xdotool')
    if Atspi.init() not in (0, 1):
        return {'state': 'ready', 'reason': 'Desktop accessibility bus unavailable on the Pi.'} if send else False
    Atspi.set_timeout(1200, 1200)
    context = GLib.MainContext.default()

    def wait_composer(marker, seconds, *, allow_home=False):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            while context.pending():
                context.iteration(False)
            try:
                target = find_composer(Atspi, marker, allow_home=allow_home)
                if target is not None:
                    current = normalized(composer_text(target[2], Atspi))
                    if not current or send and current == normalized(prompt):
                        return target
            except GLib.Error:
                pass
            time.sleep(.3)
        return None

    marker = ''
    target = None
    if select_open_chat(Atspi):
        target = wait_composer('', 1.8, allow_home=True)
        if target is None:
            page = active_page(Atspi)
            if page is not None and is_chatgpt_address(text_of(page[1])):
                if logged_out(page[2], Atspi):
                    return {'state': 'ready', 'reason': 'Sign in to ChatGPT in Chromium on the Pi.'} if send else False
                if click_new_chat(page[2], Atspi):
                    target = wait_composer('', 4.0, allow_home=True)

    if target is None:
        marker = 'bitch-draft-' + uuid4().hex[:12]
        url = HOME_PAGE + '#' + marker
        had_windows = bool(browser_windows(Atspi))
        if not had_windows and chromium_running():
            # Chromium was started without AT-SPI accessibility; relaunch cleanly so BITCH can drive it.
            restart_chromium_with_accessibility(chromium, url)
        else:
            if not had_windows:
                clean_chromium_exit_flag()
            launch_chromium(chromium, url)
        target = wait_composer(marker, 22, allow_home=True)
        if target is None and not browser_windows(Atspi) and chromium_running():
            restart_chromium_with_accessibility(chromium, url)
            target = wait_composer(marker, 25, allow_home=True)
        if target is None:
            page = active_page(Atspi)
            if page is not None and logged_out(page[2], Atspi):
                return {'state': 'ready', 'reason': 'Sign in to ChatGPT in Chromium on the Pi.'} if send else False
            return {'state': 'ready', 'reason': 'Could not open the ChatGPT composer on the Pi.'} if send else False

    window, address, composer, document = target
    if logged_out(document, Atspi):
        return {'state': 'ready', 'reason': 'Sign in to ChatGPT in Chromium on the Pi.'} if send else False
    focus_window(window)
    current = normalized(composer_text(composer, Atspi))
    if current and (not send or current != normalized(prompt)):
        return {'state': 'ready', 'reason': 'The ChatGPT composer on the Pi already had unsent text.'} if send else False

    pasted = bool(current)
    if not pasted:
        pasted = set_composer_via_atspi(composer, prompt, Atspi, context)
    if not pasted:
        pasted = paste(prompt, window, composer, copy_bin, keys_bin, Atspi, context)
    if not send:
        return pasted
    if not pasted:
        return {'state': 'ready', 'reason': 'Could not enter the request into ChatGPT on the Pi.'}

    page = active_page(Atspi)
    if page is None or not is_home_address(text_of(page[1]), marker):
        return {'state': 'ready', 'drafted': True, 'reason': 'ChatGPT page changed before sending.'}
    document = page[2]
    if logged_out(document, Atspi):
        return {'state': 'ready', 'drafted': True, 'reason': 'Sign in to ChatGPT in Chromium on the Pi.'}

    button = None
    for _ in range(8):
        while context.pending():
            context.iteration(False)
        button = send_button(document, Atspi)
        if button is not None:
            break
        time.sleep(.2)
    if button is None:
        if keys_bin and os.environ.get('WAYLAND_DISPLAY') and composer.get_state_set().contains(Atspi.StateType.FOCUSED):
            subprocess.run([keys_bin, '-k', 'Return'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
            return wait_response(prompt, Atspi, context, (GLib.Error, OSError, subprocess.SubprocessError, ValueError))
        return {'state': 'ready', 'drafted': True, 'reason': 'Could not find an enabled Send button in ChatGPT.'}
    if not button.do_action(0):
        if keys_bin and os.environ.get('WAYLAND_DISPLAY') and composer.get_state_set().contains(Atspi.StateType.FOCUSED):
            subprocess.run([keys_bin, '-k', 'Return'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
        else:
            return {'state': 'send_unverified', 'reason': 'Could not click Send in ChatGPT.'}
    return wait_response(prompt, Atspi, context, (GLib.Error, OSError, subprocess.SubprocessError, ValueError))


if __name__ == '__main__':
    sending = sys.argv[1:] == ['send']
    try:
        message = sys.stdin.read(3001)
        result = fill(message, send=sending) if 0 < len(message) <= 3000 else {'state': 'ready', 'reason': 'Request too long.'}
    except (ImportError, OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        result = {'state': 'send_unverified' if sending else 'ready', 'reason': str(exc)}
    print(json.dumps(result if isinstance(result, dict) else {'state': 'drafted' if result else 'ready'}))
