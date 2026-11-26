"""Use the signed-in Chromium desktop to draft and send a ChatGPT handoff."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import urlsplit
from uuid import uuid4

HOME_PAGE = 'https://chatgpt.com/'
CHAT_PATH = re.compile(r'/c/[a-f0-9-]{36}/?')


def draft(prompt):
    return run_desktop(prompt, send=False).get('state') == 'drafted'


def submit(prompt):
    return run_desktop(prompt, send=True)


def run_desktop(prompt, *, send):
    service = shutil.which('systemd-run')
    if not service:
        return {'state': 'ready'}
    import tempfile

    # The result goes to a file, not a pipe: a timed-out run whose service still held the pipe
    # could block forever, leaving the request stuck in "sending" and every later request behind it.
    try:
        with tempfile.TemporaryFile('w+', encoding='utf-8') as output:
            process = subprocess.Popen(
                [service, '--user', '--quiet', '--wait', '--pipe', '--collect',
                 '--property=RuntimeMaxSec=195s' if send else '--property=RuntimeMaxSec=41s',
                 sys.executable, str(Path(__file__).resolve()), 'send' if send else 'draft'],
                stdin=subprocess.PIPE, stdout=output, stderr=subprocess.DEVNULL, encoding='utf-8')
            try:
                process.communicate(prompt, timeout=198 if send else 44)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                return {'state': 'send_unverified' if send else 'ready'}
            output.seek(0)
            return json.loads(output.read())
    except (OSError, ValueError):
        return {'state': 'send_unverified' if send else 'ready'}


def children(node):
    for index in range(node.get_child_count()):
        child = node.get_child_at_index(index)
        if child is not None:
            yield child


def nodes(root):
    stack = [root]
    for _ in range(1400):
        if not stack:
            return
        node = stack.pop()
        yield node
        stack.extend(reversed(list(children(node))))


def text_of(node):
    from gi.repository import Atspi

    text = node.get_text_iface()
    value = Atspi.Text.get_text(text, 0, -1) if text is not None else ''
    return (value or '').replace('\ufffc', ' ')


def composer_text(composer, atspi):
    # Chromium exposes the editor's paragraph as an embedded accessible object.
    paragraphs = [text_of(node) for node in children(composer) if node.get_role() == atspi.Role.PARAGRAPH]
    return '\n'.join(paragraphs) if paragraphs else text_of(composer)


def matches_address(address, marker):
    # Chromium normally hides the scheme in the accessibility value of the address bar.
    address = address.strip()
    if address == 'chatgpt.com' or address.startswith(('chatgpt.com/', 'chatgpt.com#')):
        address = 'https://' + address
    url = urlsplit(address)
    return url.scheme == 'https' and url.netloc == 'chatgpt.com' and url.path in ('', '/') and (
        not url.query and url.fragment == marker)


def select_open_chat(atspi):
    for app in children(atspi.get_desktop(0)):
        if app.get_name().casefold() not in ('chromium', 'google chrome'):
            continue
        for window in children(app):
            if not window.get_state_set().contains(atspi.StateType.ACTIVE):
                continue
            for node in nodes(window):
                if node.get_role() == atspi.Role.PAGE_TAB and node.get_name() == 'ChatGPT':
                    return node.do_action(0)
    return False


def active_page(atspi):
    desktop = atspi.get_desktop(0)
    for app in children(desktop):
        if app.get_name().casefold() not in ('chromium', 'google chrome'):
            continue
        for window in children(app):
            if not window.get_state_set().contains(atspi.StateType.ACTIVE):
                continue
            address = document = None
            for node in nodes(window):
                role = node.get_role()
                if role == atspi.Role.ENTRY and node.get_name() == 'Address and search bar':
                    address = node
                if role == atspi.Role.DOCUMENT_WEB:
                    document = node
                if address is not None and document is not None:
                    break
            if address is not None and document is not None:
                return window, address, document
    return None


def find_composer(atspi, marker):
    page = active_page(atspi)
    if page is None or not matches_address(text_of(page[1]), marker):
        return None
    window, address, document = page
    for node in nodes(document):
        if (node.get_role() in (atspi.Role.ENTRY, atspi.Role.TEXT, atspi.Role.PARAGRAPH)
                and node.get_state_set().contains(atspi.StateType.EDITABLE)
                and node.get_name() == 'Ask ChatGPT'
                and node.get_attributes().get('id') != 'pending-home-input'):
            return window, address, node
    return None


def normalized(text):
    return ' '.join(text.split())


def chat_url(address):
    address = address.strip()
    if address.startswith('chatgpt.com/'):
        address = 'https://' + address
    url = urlsplit(address)
    if (url.scheme == 'https' and url.netloc == 'chatgpt.com' and not url.query
            and CHAT_PATH.fullmatch(url.path)):
        return 'https://chatgpt.com' + url.path
    return None


def send_button(document, atspi):
    return next((node for node in nodes(document)
                 if node.get_role() == atspi.Role.PUSH_BUTTON
                 and (node.get_attributes().get('id') == 'composer-submit-button'
                      or node.get_name() in ('Send', 'Send prompt'))
                 and node.get_state_set().contains(atspi.StateType.ENABLED)), None)


def response_copy(document, prompt, atspi):
    seen_user = seen_assistant = False
    for node in nodes(document):
        role, label = node.get_role(), node.get_name().strip().casefold()
        if role == atspi.Role.HEADING:
            if label.rstrip(':') in ('you said', 'user'):
                seen_user = False
            elif label.rstrip(':') in ('chatgpt said', 'assistant'):
                seen_assistant = seen_user
        elif not seen_assistant and role in (atspi.Role.PARAGRAPH, atspi.Role.SECTION):
            # The visible user message is a paragraph or an embedded group with one text leaf.
            direct = text_of(node)
            leaf = ' '.join(child.get_name() for child in children(node)
                            if child.get_role() in (atspi.Role.TEXT, atspi.Role.STATIC))
            if any(normalized(value) == normalized(prompt) for value in (direct, leaf, node.get_name())):
                seen_user = True
        elif seen_assistant and role == atspi.Role.PUSH_BUTTON and label in ('copy', 'copy response'):
            return True, node
    return seen_user, None


def wait_response(prompt, atspi, context, errors):
    end = time.monotonic() + 150.
    conversation = None
    copy_available = False
    while time.monotonic() < end:
        while context.pending():
            context.iteration(False)
        try:
            page = active_page(atspi)
            url = chat_url(text_of(page[1])) if page else None
            if url and (conversation is None or url == conversation):
                document = page[2]
                matched, button = response_copy(document, prompt, atspi)
                if matched:
                    conversation = url
                if button:
                    generating = any(node.get_role() == atspi.Role.PUSH_BUTTON
                                     and node.get_name().lower() in ('stop streaming', 'stop generating', 'stop response')
                                     for node in nodes(document))
                    if not generating and copy_available:
                        subprocess.run(['wl-copy', '--clear'], check=True, timeout=2)
                        if button.do_action(0):
                            time.sleep(.25)
                            answer = subprocess.run(['wl-paste', '--no-newline'], capture_output=True,
                                                    text=True, encoding='utf-8', timeout=2).stdout.strip()
                            if answer and normalized(answer) != normalized(prompt):
                                return {'state': 'returned', 'answer': answer[:12000], 'url': conversation}
                    copy_available = not generating
                else:
                    copy_available = False
        except errors:
            pass
        time.sleep(.65)
    return {'state': 'submitted', 'url': conversation} if conversation else {'state': 'send_unverified'}


def fill(prompt, *, send=False):
    import gi

    gi.require_version('Atspi', '2.0')
    from gi.repository import Atspi, GLib

    if not os.environ.get('WAYLAND_DISPLAY'):
        return {'state': 'ready'} if send else False
    chromium = shutil.which('chromium')
    copy = shutil.which('wl-copy')
    keys = shutil.which('wtype')
    if not chromium or not copy or not keys:
        return {'state': 'ready'} if send else False
    if Atspi.init() not in (0, 1):
        return {'state': 'ready'} if send else False
    Atspi.set_timeout(900, 900)
    context = GLib.MainContext.default()

    def wait_composer(marker, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            while context.pending():
                context.iteration(False)
            try:
                target = find_composer(Atspi, marker)
                if target is not None:
                    current = normalized(composer_text(target[2], Atspi))
                    if not current or send and current == normalized(prompt):
                        return target
            except GLib.Error:
                pass
            time.sleep(.3)
        return None

    marker = ''
    target = wait_composer(marker, 2.4) if select_open_chat(Atspi) else None
    if target is None:
        marker = 'bitch-draft-' + uuid4().hex[:12]
        subprocess.run(['systemd-run', '--user', '--quiet', '--collect', '/usr/bin/env', '-u', 'NO_AT_BRIDGE',
                        chromium, '--force-renderer-accessibility', '--new-window', HOME_PAGE + '#' + marker],
                       timeout=4, check=True)
        target = wait_composer(marker, 30)
    if target is None:
        return {'state': 'ready'} if send else False
    window, address, composer = target
    current = normalized(composer_text(composer, Atspi))
    if current and (not send or current != normalized(prompt)):
        return {'state': 'ready'} if send else False
    if not (composer.get_state_set().contains(Atspi.StateType.FOCUSED)
            or composer.get_component_iface().grab_focus()):
        return {'state': 'ready'} if send else False
    time.sleep(.12)
    if not (window.get_state_set().contains(Atspi.StateType.ACTIVE)
            and matches_address(text_of(address), marker)
            and composer.get_state_set().contains(Atspi.StateType.FOCUSED)
            and normalized(composer_text(composer, Atspi)) == current):
        return {'state': 'ready'} if send else False
    pasted = bool(current)
    if not pasted:
        pasted = paste(prompt, window, address, composer, marker, copy, keys, Atspi, context)
    if not send:
        return pasted
    if not pasted:
        return {'state': 'ready'}
    page = active_page(Atspi)
    if (page is None or not matches_address(text_of(page[1]), marker)
            or not composer.get_state_set().contains(Atspi.StateType.FOCUSED)
            or normalized(composer_text(composer, Atspi)) != normalized(prompt)):
        return {'state': 'ready', 'drafted': True}
    document = page[2]
    if not any(node.get_role() == Atspi.Role.PUSH_BUTTON and node.get_name() == 'Open profile menu'
               for node in nodes(document)):
        return {'state': 'ready', 'drafted': True}
    button = send_button(document, Atspi)
    if button is None:
        return {'state': 'ready', 'drafted': True}
    if not button.do_action(0):
        return {'state': 'send_unverified'}
    return wait_response(prompt, Atspi, context, (GLib.Error, OSError, subprocess.SubprocessError, ValueError))


def paste(prompt, window, address, composer, marker, copy, keys, atspi, context):
    with subprocess.Popen([copy, '--foreground', '--type', 'text/plain'], stdin=subprocess.PIPE) as clipboard:
        try:
            clipboard.stdin.write(prompt.encode('utf-8'))
            clipboard.stdin.close()
            time.sleep(.16)
            if not (window.get_state_set().contains(atspi.StateType.ACTIVE)
                    and matches_address(text_of(address), marker)
                    and composer.get_state_set().contains(atspi.StateType.FOCUSED)):
                return False
            subprocess.run([keys, '-M', 'ctrl', '-s', '80', '-k', 'v', '-s', '80', '-m', 'ctrl'],
                           check=True, timeout=2)
            for _ in range(12):
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


if __name__ == '__main__':
    sending = sys.argv[1:] == ['send']
    try:
        message = sys.stdin.read(3001)
        result = fill(message, send=sending) if 0 < len(message) <= 3000 else {'state': 'ready'}
    except (ImportError, OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        result = {'state': 'send_unverified' if sending else 'ready'}
    print(json.dumps(result if isinstance(result, dict) else {'state': 'drafted' if result else 'ready'}))
