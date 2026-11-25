"""Draft in a fresh ChatGPT composer on the logged-in Pi Chromium desktop."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.parse import urlsplit
from uuid import uuid4

HOME_PAGE = 'https://chatgpt.com/'


def draft(prompt):
    service = shutil.which('systemd-run')
    if not service:
        return False
    try:
        result = subprocess.run(
            [service, '--user', '--quiet', '--wait', '--pipe', '--collect', '--property=RuntimeMaxSec=41s',
             sys.executable, str(Path(__file__).resolve())],
            input=prompt, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=44,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


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


def find_composer(atspi, marker):
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
            if address is None or document is None or not matches_address(text_of(address), marker):
                continue
            for node in nodes(document):
                if (node.get_role() in (atspi.Role.ENTRY, atspi.Role.TEXT, atspi.Role.PARAGRAPH)
                        and node.get_state_set().contains(atspi.StateType.EDITABLE)
                        and node.get_name() == 'Ask ChatGPT'
                        and node.get_attributes().get('id') != 'pending-home-input'):
                    return window, address, node
    return None


def fill(prompt):
    import gi

    gi.require_version('Atspi', '2.0')
    from gi.repository import Atspi, GLib

    if not os.environ.get('WAYLAND_DISPLAY'):
        return False
    chromium = shutil.which('chromium')
    copy = shutil.which('wl-copy')
    keys = shutil.which('wtype')
    if not chromium or not copy or not keys:
        return False
    if Atspi.init() not in (0, 1):
        return False
    Atspi.set_timeout(900, 900)
    context = GLib.MainContext.default()

    def wait_composer(marker, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            while context.pending():
                context.iteration(False)
            try:
                target = find_composer(Atspi, marker)
                if target is not None and not composer_text(target[2], Atspi).strip():
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
        return False
    window, address, composer = target
    if composer_text(composer, Atspi).strip():
        return False
    if not (composer.get_state_set().contains(Atspi.StateType.FOCUSED)
            or composer.get_component_iface().grab_focus()):
        return False
    time.sleep(.12)
    if not (window.get_state_set().contains(Atspi.StateType.ACTIVE)
            and matches_address(text_of(address), marker)
            and composer.get_state_set().contains(Atspi.StateType.FOCUSED)
            and not composer_text(composer, Atspi).strip()):
        return False
    with subprocess.Popen([copy, '--foreground', '--type', 'text/plain'], stdin=subprocess.PIPE) as clipboard:
        try:
            clipboard.stdin.write(prompt.encode('utf-8'))
            clipboard.stdin.close()
            time.sleep(.16)
            if not (window.get_state_set().contains(Atspi.StateType.ACTIVE)
                    and matches_address(text_of(address), marker)
                    and composer.get_state_set().contains(Atspi.StateType.FOCUSED)):
                return False
            subprocess.run([keys, '-M', 'ctrl', '-s', '80', '-k', 'v', '-s', '80', '-m', 'ctrl'],
                           check=True, timeout=2)
            for _ in range(12):
                time.sleep(.15)
                while context.pending():
                    context.iteration(False)
                if ' '.join(composer_text(composer, Atspi).split()) == ' '.join(prompt.split()):
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
    try:
        message = sys.stdin.read(3001)
        sys.exit(0 if 0 < len(message) <= 3000 and fill(message) else 1)
    except (ImportError, OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        sys.exit(1)
