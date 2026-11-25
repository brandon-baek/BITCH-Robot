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
            [service, '--user', '--quiet', '--wait', '--pipe', '--collect', '--property=RuntimeMaxSec=25s',
             sys.executable, str(Path(__file__).resolve())],
            input=prompt, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=28,
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
    text = node.get_text_iface()
    return text.get_text(0, -1) if text is not None else ''


def matches_address(address, marker):
    # Chromium normally hides the scheme in the accessibility value of the address bar.
    address = address.strip()
    if address.startswith(('chatgpt.com/', 'chatgpt.com#')):
        address = 'https://' + address
    url = urlsplit(address)
    return url.scheme == 'https' and url.netloc == 'chatgpt.com' and url.path in ('', '/') and (
        not url.query and url.fragment == marker)


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
                        and node.get_attributes().get('id') == 'prompt-textarea'):
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
    marker = 'bitch-draft-' + uuid4().hex[:12]
    if Atspi.init() not in (0, 1):
        return False
    Atspi.set_timeout(900, 900)
    subprocess.run(['systemd-run', '--user', '--quiet', '--collect', '/usr/bin/env', '-u', 'NO_AT_BRIDGE',
                    chromium, '--force-renderer-accessibility', '--new-window', HOME_PAGE + '#' + marker],
                   timeout=4, check=True)
    end = time.monotonic() + 18
    while time.monotonic() < end:
        try:
            target = find_composer(Atspi, marker)
        except GLib.Error:
            target = None
        if target is not None:
            break
        time.sleep(.3)
    else:
        return False
    window, address, composer = target
    if text_of(composer).strip():
        return False
    if not (composer.get_state_set().contains(Atspi.StateType.FOCUSED)
            or composer.get_component_iface().grab_focus()):
        return False
    time.sleep(.12)
    if not (window.get_state_set().contains(Atspi.StateType.ACTIVE)
            and matches_address(text_of(address), marker)
            and composer.get_state_set().contains(Atspi.StateType.FOCUSED)
            and not text_of(composer).strip()):
        return False
    subprocess.run([copy, '--paste-once', '--type', 'text/plain;charset=utf-8'], input=prompt,
                   text=True, check=True, timeout=3)
    if not (window.get_state_set().contains(Atspi.StateType.ACTIVE)
            and composer.get_state_set().contains(Atspi.StateType.FOCUSED)):
        return False
    subprocess.run([keys, '-M', 'ctrl', '-k', 'v', '-m', 'ctrl'], check=True, timeout=2)
    for _ in range(10):
        time.sleep(.12)
        if ' '.join(text_of(composer).split()) == ' '.join(prompt.split()):
            return True
    return False


if __name__ == '__main__':
    try:
        message = sys.stdin.read(3001)
        sys.exit(0 if 0 < len(message) <= 3000 and fill(message) else 1)
    except (ImportError, OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        sys.exit(1)
