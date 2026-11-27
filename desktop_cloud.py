"""Hyper-optimized real-time streaming driver for signed-in Gemini & ChatGPT on the Pi.

Unlike desktop_chatgpt.py (which opens a fresh chat and waits for the entire answer to finish),
this driver keeps persistent browser tabs and persistent DevTools WebSockets open, sends prompts
in a single round-trip (~40 ms), and streams replies via MutationObserver + CDP Runtime.addBinding
with near-zero polling overhead.

Benchmark both providers on the Pi:
    python3 desktop_cloud.py bench
Ask a streaming question:
    python3 desktop_cloud.py ask gemini "Say hi in one short sentence."
    python3 desktop_cloud.py ask chatgpt "Say hi in one short sentence."
"""
import base64
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

import desktop_chatgpt as cdp

CHATGPT_URL = os.environ.get('BITCH_CHATGPT_URL', 'https://chatgpt.com/?temporary-chat=true')
GEMINI_URL = os.environ.get('BITCH_GEMINI_URL', 'https://gemini.google.com/app')
TEMP_CHAT = os.environ.get('BITCH_TEMP_CHAT', '1') != '0'
MAX_TURNS_PER_CHAT = 24
IDLE_RESET_SECONDS = 900.

# ---------------------------------------------------------------- Page-side helpers (single-eval fast send + push stream)

CHATGPT_HELPERS = r'''
const all = (s) => Array.from(document.querySelectorAll(s));
const vis = (el) => !!el && el.offsetParent !== null;
const composer = () => all('#prompt-textarea, div.ProseMirror[contenteditable="true"], textarea[name="prompt-textarea"]')
  .find((el) => vis(el) && (el.isContentEditable || el.tagName === 'TEXTAREA')) || null;
const sendBtn = () => document.querySelector('[data-testid="send-button"], #composer-submit-button, button[aria-label="Send prompt"], button[aria-label="Send message"]');
const stopBtn = () => document.querySelector('[data-testid="stop-button"], button[aria-label="Stop streaming"], button[aria-label^="Stop"]');
const first = (sels) => { for (const s of sels) { const f = all(s); if (f.length) return f; } return []; };
const replies = () => first(['[data-response-body]', '[data-markdown-text-style="assistant-message"]',
                             '[data-message-author-role="assistant"]']);
const textOf = (el) => {
  if (!el) return '';
  const body = el.querySelector('.markdown') || el;
  return (body.textContent || '').trim();
};
const isStreaming = () => {
  if (stopBtn()) return true;
  const turns = all('[data-talvt-turn-state]');
  const last = turns[turns.length - 1];
  return !!last && last.getAttribute('data-talvt-turn-state') !== 'complete';
};
const isComplete = () => {
  const r = replies();
  const last = r[r.length - 1];
  const turn = last && last.closest('[data-talvt-turn-state]');
  return !!turn && turn.getAttribute('data-talvt-turn-state') === 'complete';
};
'''

GEMINI_HELPERS = r'''
const all = (s) => Array.from(document.querySelectorAll(s));
const vis = (el) => !!el && el.offsetParent !== null;
const composer = () => all('.ql-editor[contenteditable="true"], rich-textarea [contenteditable="true"], [aria-label*="prompt for Gemini"]')
  .find(vis) || null;
const sendBtn = () => document.querySelector('button.send-button:not([disabled]), button[aria-label="Send message"]:not([disabled])');
const stopBtn = () => document.querySelector('button[aria-label*="Stop"], button.stop, .stop-icon');
const replies = () => {
  for (const s of ['model-response message-content .markdown', 'model-response .markdown', 'message-content .markdown', 'model-response']) {
    const f = all(s);
    if (f.length) return f;
  }
  return [];
};
const textOf = (el) => (el ? (el.textContent || '').trim() : '');
const isStreaming = () => {
  if (stopBtn()) return true;
  return !!document.querySelector('model-response[aria-busy="true"], .loading-indicator, mat-progress-bar');
};
const isComplete = () => false;
'''


class StreamTab:
    """Persistent CDP WebSocket that also receives push notifications from Runtime.addBinding."""

    def __init__(self, ws_url):
        self.ws = cdp.Socket(ws_url)
        self.next_id = 0
        self.events = []
        self.file_chooser = None

    def call(self, method, params=None, timeout=15.):
        self.next_id += 1
        ident = self.next_id
        self.ws.send(json.dumps({'id': ident, 'method': method, 'params': params or {}}))
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f'{method} timed out')
            msg = json.loads(self.ws.recv(left))
            if msg.get('id') == ident:
                if 'error' in msg:
                    raise RuntimeError(f"{method}: {msg['error'].get('message')}")
                return msg.get('result', {})
            if msg.get('method') == 'Runtime.bindingCalled':
                self.events.append(msg.get('params', {}))
            elif msg.get('method') == 'Page.fileChooserOpened':
                self.file_chooser = msg.get('params', {})

    def wait_file_chooser(self, timeout=4.):
        if self.file_chooser is not None:
            fc, self.file_chooser = self.file_chooser, None
            return fc
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            try:
                msg = json.loads(self.ws.recv(left))
            except (TimeoutError, socket.timeout):
                return None
            if msg.get('method') == 'Page.fileChooserOpened':
                return msg.get('params', {})
            if msg.get('method') == 'Runtime.bindingCalled':
                self.events.append(msg.get('params', {}))

    def eval(self, expression, timeout=15., user_gesture=False):
        res = self.call('Runtime.evaluate', {
            'expression': expression, 'awaitPromise': True, 'returnByValue': True,
            'userGesture': user_gesture,
        }, timeout)
        if res.get('exceptionDetails'):
            det = res['exceptionDetails']
            raise RuntimeError((det.get('exception') or {}).get('description') or det.get('text', 'eval failed'))
        return (res.get('result') or {}).get('value')

    def next_binding(self, name, timeout=1.):
        for i, ev in enumerate(self.events):
            if ev.get('name') == name:
                return self.events.pop(i).get('payload')
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            try:
                msg = json.loads(self.ws.recv(left))
            except (TimeoutError, socket.timeout):
                return None
            if msg.get('method') == 'Runtime.bindingCalled':
                params = msg.get('params', {})
                if params.get('name') == name:
                    return params.get('payload')
                self.events.append(params)

    def drain(self):
        self.events.clear()

    def close(self):
        self.ws.close()


import socket  # noqa: E402


class CloudProvider:
    """One persistent browser tab for either 'gemini' or 'chatgpt'."""

    def __init__(self, name):
        self.name = name
        self.url = GEMINI_URL if name == 'gemini' else CHATGPT_URL
        self.host = urlsplit(self.url).hostname
        self.helpers = GEMINI_HELPERS if name == 'gemini' else CHATGPT_HELPERS
        self.lock = threading.Lock()
        self.tab = None
        self.tab_id = None
        self.turns = 0
        self.last_used = 0.
        self.primed = False
        self.mode_checked = False

    def _connect(self, *, close_others=False):
        if self.tab is not None:
            return self.tab
        err = cdp.start_browser()
        if err:
            raise RuntimeError(err)
        pages = [p for p in cdp.devtools('/json/list') if p.get('type') == 'page']
        matches = [p for p in pages if urlsplit(p.get('url', '')).hostname == self.host]
        page = matches[0] if matches else None
        if close_others:
            for other in pages:
                if page is None or other['id'] != page['id']:
                    try:
                        cdp.devtools('/json/close/' + other['id'], timeout=1.5)
                    except Exception:
                        pass
        if page is None:
            page = cdp.devtools('/json/new?' + self.url, method='PUT', timeout=5.)
            time.sleep(2.)
        self.tab_id = page['id']
        try:
            cdp.devtools('/json/activate/' + self.tab_id, timeout=2.)
        except Exception:
            pass
        tab = StreamTab(page['webSocketDebuggerUrl'])
        tab.call('Runtime.enable')
        tab.call('Runtime.addBinding', {'name': '__bitchStream'})
        self.tab = tab
        self.mode_checked = False
        return tab

    def _close(self):
        if self.tab is not None:
            try:
                self.tab.close()
            except Exception:
                pass
        self.tab = None
        self.tab_id = None
        self.mode_checked = False

    def _ensure_ready(self, tab, *, wait=20.):
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            try:
                ready = tab.eval(f'(() => {{ {self.helpers} return !!composer(); }})()', timeout=4.)
                if ready:
                    return True
            except Exception:
                pass
            time.sleep(.25)
        return False

    def _ensure_temp_chat(self, tab):
        """Enable Temporary Chat mode so BITCH conversations never clutter Brandon's ChatGPT or Gemini history."""
        if not TEMP_CHAT:
            return
        try:
            if self.name == 'chatgpt':
                in_temp = tab.eval(
                    '(() => location.search.includes("temporary-chat=true") '
                    '|| /temporary chat/i.test(document.body.innerText.slice(0, 600)))()',
                    timeout=3.,
                )
                if not in_temp:
                    clicked = tab.eval(
                        '(() => { const b = Array.from(document.querySelectorAll("button, a"))'
                        '.find(e => e.offsetParent && /temporary chat/i.test((e.getAttribute("aria-label") || "") + " " + (e.innerText || "")));'
                        'if (b) { b.click(); return true; } return false; })()',
                        timeout=3.,
                    )
                    if not clicked:
                        tab.call('Page.enable')
                        tab.call('Page.navigate', {'url': CHATGPT_URL})
                    time.sleep(.35)
            else:
                clicked = tab.eval(
                    '(() => { const b = Array.from(document.querySelectorAll("button"))'
                    '.find(e => e.offsetParent && /turn on temporary chat/i.test(e.getAttribute("aria-label") || ""));'
                    'if (b) { b.click(); return true; } return false; })()',
                    timeout=3.,
                )
                if clicked:
                    time.sleep(.25)
        except Exception:
            pass

    def _ensure_fast_mode(self, tab):
        if self.mode_checked:
            return
        try:
            if self.name == 'gemini':
                # Switch Gemini to "Everyday help 4 Hydrogen" AND "Thinking level -> Low (Fast and efficient)".
                cur = tab.eval(
                    '(() => { const b = Array.from(document.querySelectorAll("button"))'
                    '.find(e => e.offsetParent && /mode picker/i.test(e.getAttribute("aria-label") || ""));'
                    'return b ? (b.innerText || b.getAttribute("aria-label") || "").trim() : null; })()',
                    timeout=3.,
                )
                if cur is not None:
                    if not re.search(r'hydrogen|fast|flash', cur, re.I):
                        tab.eval(
                            '(() => { const b = Array.from(document.querySelectorAll("button"))'
                            '.find(e => e.offsetParent && /mode picker/i.test(e.getAttribute("aria-label") || ""));'
                            'if (b) b.click(); return true; })()',
                            timeout=3.,
                        )
                        time.sleep(.35)
                        tab.eval(
                            '(() => { const items = Array.from(document.querySelectorAll('
                            '"[role=menuitem],[role=menuitemradio],[role=option],button.mat-mdc-menu-item"))'
                            '.filter(e => e.offsetParent);'
                            'const hit = items.find(e => /hydrogen|everyday|fast|flash/i.test(e.innerText || ""));'
                            'if (hit) { hit.click(); return true; } return false; })()',
                            timeout=3.,
                        )
                        time.sleep(.25)
                    # Now set Thinking level -> Low (Fast and efficient).
                    tab.eval(
                        '(() => { const b = Array.from(document.querySelectorAll("button"))'
                        '.find(e => e.offsetParent && /mode picker/i.test(e.getAttribute("aria-label") || ""));'
                        'if (b) b.click(); return true; })()',
                        timeout=3.,
                    )
                    time.sleep(.3)
                    tab.eval(
                        '(() => { const x = Array.from(document.querySelectorAll("[role=menuitem],button"))'
                        '.find(e => e.offsetParent && /thinking level/i.test(e.innerText || ""));'
                        'if (x) x.click(); return !!x; })()',
                        timeout=3.,
                    )
                    time.sleep(.3)
                    picked = tab.eval(
                        '(() => { const items = Array.from(document.querySelectorAll('
                        '"[role=menuitem],[role=menuitemradio],[role=option],button.mat-mdc-menu-item"))'
                        '.filter(e => e.offsetParent);'
                        'const hit = items.find(e => /^low\\b|fast and efficient/i.test((e.innerText || "").trim()));'
                        'if (hit) { hit.click(); return (hit.innerText || "").trim(); } return null; })()',
                        timeout=3.,
                    )
                    if not picked:
                        for kind in ('keyDown', 'keyUp'):
                            tab.call('Input.dispatchKeyEvent', {'type': kind, 'key': 'Escape', 'code': 'Escape', 'windowsVirtualKeyCode': 27})
                    time.sleep(.2)
                self.mode_checked = True
            else:
                # ChatGPT: verify Instant mode is active (skip opening menu if already Instant).
                cur = tab.eval(
                    f'(() => {{ {self.helpers} const box = composer(); const scope = (box && box.closest("form")) || document;'
                    'const b = Array.from(scope.querySelectorAll("button")).find(e => vis(e) && e.getAttribute("aria-haspopup")'
                    ' && /instant|light|medium|heavy|standard|extended|thinking|auto|fast|pro/i.test((e.innerText||"")+" "+(e.getAttribute("aria-label")||"")));'
                    'return b ? (b.innerText || "").trim() : null; })()',
                    timeout=3.,
                )
                if cur and re.search(r'instant|fast|light|auto', cur, re.I):
                    self.mode_checked = True
                    return
                self.mode_checked = True
        except Exception:
            self.mode_checked = True

    def reset_chat(self):
        """Start a fresh chat in the persistent tab (in-SPA click when possible so it takes < 0.5 s)."""
        with self.lock:
            tab = self._connect()
            if self.name == 'gemini':
                clicked = tab.eval(
                    '(() => { const b = Array.from(document.querySelectorAll("a, button"))'
                    '.find(e => e.offsetParent && (/new chat/i.test(e.getAttribute("aria-label") || "") '
                    '|| /^new chat$/i.test((e.innerText || "").trim())));'
                    'if (b) { b.click(); return true; } return false; })()',
                    timeout=4.,
                )
                if not clicked:
                    tab.call('Page.enable')
                    tab.call('Page.navigate', {'url': self.url})
            else:
                clicked = tab.eval(
                    '(() => { const b = Array.from(document.querySelectorAll("[data-testid=create-new-chat-button], a, button"))'
                    '.find(e => e.offsetParent && ((e.getAttribute("aria-label") || "").trim() === "New chat"'
                    ' || (e.innerText || "").trim() === "New chat" || e.matches("[data-testid=create-new-chat-button]")));'
                    'if (b) { b.click(); return true; } return false; })()',
                    timeout=4.,
                )
                if not clicked:
                    tab.call('Page.enable')
                    tab.call('Page.navigate', {'url': self.url})
            time.sleep(.35)
            self._ensure_ready(tab, wait=15.)
            self.turns = 0
            self.primed = False

    def warm(self):
        """Pre-load a fresh Temporary Chat in the background so Turn 1 never waits 20s for page hydration."""
        with self.lock:
            try:
                tab = self._connect(close_others=True)
                self._ensure_ready(tab, wait=25.)
                self._ensure_temp_chat(tab)
                self._ensure_ready(tab, wait=25.)
                self._ensure_fast_mode(tab)
                self.turns = 0
                self.primed = False
                return True
            except Exception:
                self._close()
                return False

    def _attach_image_gemini(self, tab, image_b64):
        """Attach a JPEG on Gemini via CDP file chooser interception (no OS dialog, trusted upload)."""
        tmp_path = os.path.join(tempfile.gettempdir(), 'bitch_camera.jpg')
        with open(tmp_path, 'wb') as f:
            f.write(base64.b64decode(image_b64))
        before = tab.eval('''(() => document.querySelectorAll(
            'img[src^="blob:"], img[src^="data:image"], uploader-file-preview, file-preview-chip, ' +
            '.file-preview-container, button[aria-label*="Remove" i]'
        ).length)()''', timeout=4.) or 0
        tab.file_chooser = None
        tab.call('Page.enable', timeout=4.)
        tab.call('DOM.enable', timeout=4.)
        tab.call('Page.setInterceptFileChooserDialog', {'enabled': True}, timeout=4.)
        try:
            menu_ready = tab.eval('''(async () => {
                const btn = Array.from(document.querySelectorAll('button'))
                    .find(b => b.offsetParent !== null && /upload|add files/i.test(b.ariaLabel || b.textContent || ''));
                if (!btn) return {ok: false, reason: 'no_upload_btn'};
                btn.click();
                const end = performance.now() + 2000;
                while (performance.now() < end) {
                    const item = Array.from(document.querySelectorAll('button, [role="menuitem"], .mat-mdc-menu-item, li'))
                        .find(el => el.offsetParent !== null && /^\\s*(?:upload files?|upload image|add from computer)\\s*$/i.test((el.textContent || '').trim()));
                    if (item) return {ok: true, label: (item.textContent || '').trim()};
                    await new Promise(r => setTimeout(r, 50));
                }
                return {ok: false, reason: 'no_upload_menu_item'};
            })()''', timeout=6., user_gesture=True)
            if not menu_ready or not menu_ready.get('ok'):
                return menu_ready or {'ok': False, 'reason': 'menu_failed'}
            # Synchronous click under userGesture=True so Chromium treats it as trusted and opens the file chooser.
            opened = tab.eval('''(() => {
                const item = Array.from(document.querySelectorAll('button, [role="menuitem"], .mat-mdc-menu-item, li'))
                    .find(el => el.offsetParent !== null && /^\\s*(?:upload files?|upload image|add from computer)\\s*$/i.test((el.textContent || '').trim()));
                if (!item) return {ok: false, reason: 'menu_closed'};
                item.click();
                return {ok: true, clicked: (item.textContent || '').trim()};
            })()''', timeout=4., user_gesture=True)
            if not opened or not opened.get('ok'):
                return opened or {'ok': False, 'reason': 'menu_failed'}
            fc = tab.wait_file_chooser(timeout=4.)
            if not fc or 'backendNodeId' not in fc:
                return {'ok': False, 'reason': 'no_file_chooser', 'opened': opened}
            tab.call('DOM.setFileInputFiles', {'backendNodeId': fc['backendNodeId'], 'files': [tmp_path]}, timeout=6.)
        finally:
            try:
                tab.call('Page.setInterceptFileChooserDialog', {'enabled': False}, timeout=4.)
            except Exception:
                pass
        return tab.eval(f'''(async () => {{
            const thumbs = () => document.querySelectorAll(
                'img[src^="blob:"], img[src^="data:image"], uploader-file-preview, file-preview-chip, ' +
                '.file-preview-container, button[aria-label*="Remove" i]'
            );
            const deadline = performance.now() + 7000;
            let saw = false;
            while (performance.now() < deadline) {{
                if (thumbs().length > {int(before)}) saw = true;
                const uploading = !!document.querySelector('mat-progress-spinner, [aria-label*="Uploading" i], .loading-spinner');
                if (saw && !uploading) {{
                    await new Promise(r => setTimeout(r, 180));
                    return {{ok: true, thumb: true}};
                }}
                await new Promise(r => setTimeout(r, 100));
            }}
            return {{ok: saw, thumb: saw, reason: saw ? 'still_uploading' : 'no_thumbnail'}};
        }})()''', timeout=10.)

    def _attach_image(self, tab, image_b64):
        """Attach a base64 JPEG to ChatGPT or Gemini's composer and wait until upload is ready."""
        if self.name == 'gemini':
            return self._attach_image_gemini(tab, image_b64)
        b64_js = json.dumps(image_b64)
        attach_script = f'''(async () => {{
            {self.helpers}
            const box = composer();
            if (!box) return {{ok: false, reason: 'no composer'}};
            const scope = box.closest('form, input-area-v2, .input-area-container, .text-input-field') || document;
            const thumbs = () => scope.querySelectorAll(
                'img[src^="blob:"], img[src^="data:image"], [data-testid*="attachment"], [data-test-id*="preview"], ' +
                '[data-test-id*="image"], .image-preview, uploader-file-preview, file-preview-chip, .file-preview-container, ' +
                'button[aria-label*="Remove file" i], button[aria-label*="Remove image" i]'
            );
            const beforeThumbs = thumbs().length;
            const raw = atob({b64_js});
            const bytes = new Uint8Array(raw.length);
            for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
            const file = new File([bytes], 'camera.jpg', {{type: 'image/jpeg'}});
            const makeDT = () => {{ const d = new DataTransfer(); d.items.add(file); return d; }};
            box.focus();
            // 1) Try clipboard paste on composer (native in ChatGPT ProseMirror & Gemini Quill).
            box.dispatchEvent(new ClipboardEvent('paste', {{clipboardData: makeDT(), bubbles: true, cancelable: true}}));
            await new Promise(r => setTimeout(r, 220));
            const hasNewThumb = () => thumbs().length > beforeThumbs;
            // 2) Fallback: drag-and-drop onto composer/container (Gemini Angular dropzone).
            if (!hasNewThumb()) {{
                for (const target of [box, scope]) {{
                    if (!target) continue;
                    const dtDrop = makeDT();
                    target.dispatchEvent(new DragEvent('dragenter', {{dataTransfer: dtDrop, bubbles: true, cancelable: true}}));
                    target.dispatchEvent(new DragEvent('dragover', {{dataTransfer: dtDrop, bubbles: true, cancelable: true}}));
                    target.dispatchEvent(new DragEvent('drop', {{dataTransfer: dtDrop, bubbles: true, cancelable: true}}));
                }}
                await new Promise(r => setTimeout(r, 220));
            }}
            // 3) Fallback: populate any file input on the page directly.
            if (!hasNewThumb()) {{
                const inp = Array.from(document.querySelectorAll('input[type="file"]'))
                    .find(e => !e.accept || /image|\\*/i.test(e.accept)) || document.querySelector('input[type="file"]');
                if (inp) {{
                    inp.files = makeDT().files;
                    inp.dispatchEvent(new Event('change', {{bubbles: true}}));
                }}
            }}
            // Wait up to 6 s for image upload spinner to finish and Send button / preview to settle.
            const deadline = performance.now() + 6000;
            let sawThumb = false;
            while (performance.now() < deadline) {{
                const thumb = hasNewThumb();
                if (thumb) sawThumb = true;
                const uploading = !!scope.querySelector(
                    '[aria-label*="Uploading" i], [aria-label*="uploading" i], circle.animate-spin, mat-progress-spinner, [data-testid*="upload-progress"]'
                );
                if (sawThumb && !uploading) {{
                    await new Promise(r => setTimeout(r, 120));
                    return {{ok: true, thumb: true}};
                }}
                await new Promise(r => setTimeout(r, 100));
            }}
            return {{ok: sawThumb, thumb: sawThumb, reason: sawThumb ? 'still_uploading' : 'no_thumbnail'}};
        }})()'''
        return tab.eval(attach_script, timeout=10.)

    def stream(self, prompt, *, reset=False, image_b64=None, timeout=90., stop_event=None):
        """Send `prompt` (with optional base64 JPEG `image_b64`) and yield incremental stream dicts."""
        with self.lock:
            t0 = time.monotonic()
            tab = self._connect()
            need_reset = (reset or (self.turns >= MAX_TURNS_PER_CHAT)
                          or (self.last_used and time.monotonic() - self.last_used > IDLE_RESET_SECONDS))
            if need_reset:
                if self.name == 'gemini':
                    clicked = tab.eval(
                        '(() => { const b = Array.from(document.querySelectorAll("a, button"))'
                        '.find(e => e.offsetParent && (/new chat/i.test(e.getAttribute("aria-label") || "") '
                        '|| /^new chat$/i.test((e.innerText || "").trim())));'
                        'if (b) { b.click(); return true; } return false; })()',
                        timeout=4.,
                    )
                    if not clicked:
                        tab.call('Page.enable')
                        tab.call('Page.navigate', {'url': self.url})
                    time.sleep(.35)
                    self._ensure_temp_chat(tab)
                else:
                    temp_flag = 'true' if TEMP_CHAT else 'false'
                    try:
                        already_fresh_temp = tab.eval(
                            f'(() => {{ {self.helpers} return location.pathname === "/" && replies().length === 0 && !!composer() '
                            f'&& (!{temp_flag} || location.search.includes("temporary-chat=true") '
                            f'|| /temporary chat/i.test((document.body.textContent || "").slice(0, 800))); }})()',
                            timeout=4.,
                        )
                    except Exception:
                        self._close()
                        tab = self._connect()
                        already_fresh_temp = False
                    if not already_fresh_temp:
                        # Prefer fast in-SPA "New chat" click + Temporary Chat toggle (~0.6 s) over full reload (~20 s).
                        in_spa = tab.eval(
                            '(() => { const b = Array.from(document.querySelectorAll("[data-testid=create-new-chat-button], a, button"))'
                            '.find(e => e.offsetParent && ((e.getAttribute("aria-label") || "").trim() === "New chat"'
                            ' || (e.innerText || "").trim() === "New chat" || e.matches("[data-testid=create-new-chat-button]")));'
                            'if (b) { b.click(); return true; } return false; })()',
                            timeout=4.,
                        )
                        if not in_spa:
                            tab.call('Page.enable')
                            tab.call('Page.navigate', {'url': CHATGPT_URL if TEMP_CHAT else self.url})
                        end_reset = time.monotonic() + 12.
                        while time.monotonic() < end_reset:
                            try:
                                if tab.eval(f'(() => {{ {self.helpers} return replies().length === 0 && !!composer(); }})()', timeout=3.):
                                    break
                            except Exception:
                                pass
                            time.sleep(.15)
                        if in_spa and TEMP_CHAT:
                            self._ensure_temp_chat(tab)
                self.turns = 0
                self.primed = False
            elif self.turns == 0:
                self._ensure_temp_chat(tab)
            if not self.mode_checked:
                if not self._ensure_ready(tab, wait=15.):
                    raise RuntimeError(f'{self.name} composer not ready')
                self._ensure_fast_mode(tab)
            if image_b64:
                att = self._attach_image(tab, image_b64)
                if not att or not att.get('ok'):
                    raise RuntimeError(f'{self.name} image attach failed: {att}')
            t_ready = time.monotonic()

            # Single-roundtrip send + observer setup: paste prompt, click Send (or fallback to Enter), and install 45 ms textContent ticker.
            tab.drain()
            one_line = ' '.join(line.strip() for line in prompt.splitlines() if line.strip())
            prompt_js = json.dumps(one_line)
            use_paste = 'true' if self.name == 'chatgpt' else 'false'
            go_script = f'''(() => {{
                {self.helpers}
                if (window.__bitchObs) {{ try {{ window.__bitchObs.disconnect(); }} catch (e) {{}} window.__bitchObs = null; }}
                if (window.__bitchTimer) {{ clearInterval(window.__bitchTimer); window.__bitchTimer = null; }}
                const beforeCount = replies().length;
                const prevText = beforeCount ? textOf(replies()[beforeCount - 1]) : '';
                const box = composer();
                if (!box) return {{ok: false, reason: 'no composer'}};
                box.focus();
                document.execCommand('selectAll', false, null);
                document.execCommand('delete', false, null);
                let pasted = false;
                if ({use_paste}) {{
                    try {{
                        const dt = new DataTransfer();
                        dt.setData('text/plain', {prompt_js});
                        box.dispatchEvent(new ClipboardEvent('paste', {{clipboardData: dt, bubbles: true, cancelable: true}}));
                        pasted = !!(box.value ?? box.textContent ?? '').trim();
                    }} catch (e) {{ pasted = false; }}
                }} else {{
                    pasted = document.execCommand('insertText', false, {prompt_js}) && !!(box.textContent || '').trim();
                }}
                let clicked = false;
                if (pasted) {{
                    const b = sendBtn();
                    if (b && !b.disabled) {{ b.click(); clicked = true; }}
                }}
                let lastSent = '';
                let stableTicks = 0;
                let sawStreaming = false;
                let finished = false;
                let nudged = false;
                const started = performance.now();
                const tick = () => {{
                    if (finished) return;
                    const rs = replies();
                    if (rs.length <= beforeCount) {{
                        if (!nudged && performance.now() - started > 300) {{
                            nudged = true;
                            const b = sendBtn();
                            if (b && !b.disabled) b.click();
                        }}
                        return;
                    }}
                    const el = rs[rs.length - 1];
                    const txt = textOf(el);
                    const busy = isStreaming();
                    if (busy) sawStreaming = true;
                    const complete = isComplete();
                    if (txt && txt !== prevText && txt !== lastSent) {{
                        lastSent = txt;
                        stableTicks = 0;
                        window.__bitchStream(JSON.stringify({{text: txt, done: false}}));
                    }} else if (txt && !busy) {{
                        stableTicks++;
                    }} else {{
                        stableTicks = 0;
                    }}
                    const doneNow = Boolean(txt && !busy && (complete || (sawStreaming && stableTicks >= 5) || stableTicks >= 18));
                    if (doneNow) {{
                        finished = true;
                        clearInterval(window.__bitchTimer);
                        window.__bitchTimer = null;
                        window.__bitchStream(JSON.stringify({{text: txt, done: true}}));
                    }}
                }};
                window.__bitchTimer = setInterval(tick, 45);
                return {{ok: true, pasted, clicked}};
            }})()'''
            try:
                res = tab.eval(go_script, timeout=6.)
            except Exception:
                # Reconnect once if the WebSocket dropped.
                self._close()
                tab = self._connect()
                res = tab.eval(go_script, timeout=6.)
            if not res or not res.get('ok'):
                raise RuntimeError(f'{self.name} send failed: {res}')
            if not res.get('pasted'):
                tab.call('Input.insertText', {'text': one_line}, timeout=6.)
                time.sleep(.02)
            if not res.get('clicked'):
                for kind in ('keyDown', 'keyUp'):
                    tab.call('Input.dispatchKeyEvent', {
                        'type': kind, 'key': 'Enter', 'code': 'Enter',
                        'windowsVirtualKeyCode': 13, 'text': '\r' if kind == 'keyDown' else '',
                    }, timeout=4.)
            t_sent = time.monotonic()

            full_text = ''
            ttft = None
            ttfs = None
            deadline = time.monotonic() + timeout
            try:
                while time.monotonic() < deadline:
                    if stop_event is not None and stop_event.is_set():
                        # Tell browser to click Stop if still streaming.
                        try:
                            tab.eval(f'(() => {{ {self.helpers} const s = stopBtn(); if (s) s.click(); return true; }})()', timeout=2.)
                        except Exception:
                            pass
                        return
                    raw = tab.next_binding('__bitchStream', timeout=.15)
                    if raw is None:
                        continue
                    data = json.loads(raw)
                    txt = data.get('text') or ''
                    done = bool(data.get('done'))
                    now = time.monotonic()
                    if txt and ttft is None:
                        ttft = now - t_sent
                    if txt and ttfs is None and (done or (len(txt) >= 12 and re.search(r'[.!?](?:\s|$)', txt))):
                        ttfs = now - t_sent
                    delta = txt[len(full_text):] if txt.startswith(full_text) else txt
                    full_text = txt
                    timing = {
                        'setup_s': round(t_ready - t0, 3),
                        'send_s': round(t_sent - t_ready, 3),
                        'ttft_s': round(ttft, 3) if ttft is not None else None,
                        'ttfs_s': round(ttfs, 3) if ttfs is not None else None,
                        'total_s': round(now - t0, 3),
                    }
                    if delta or done:
                        yield {'text': full_text, 'delta': delta, 'done': done, 'timing': timing, 'provider': self.name}
                    if done:
                        self.turns += 1
                        self.last_used = time.monotonic()
                        return
                raise TimeoutError(f'{self.name} reply timed out')
            finally:
                try:
                    tab.eval('if(window.__bitchObs){window.__bitchObs.disconnect();window.__bitchObs=null;}'
                             'if(window.__bitchTimer){clearInterval(window.__bitchTimer);window.__bitchTimer=null;}1;',
                             timeout=2.)
                except Exception:
                    pass


PROVIDERS = {}
PROVIDERS_LOCK = threading.Lock()


def get_provider(name='gemini'):
    name = 'gemini' if name == 'gemini' else 'chatgpt'
    with PROVIDERS_LOCK:
        if name not in PROVIDERS:
            PROVIDERS[name] = CloudProvider(name)
        return PROVIDERS[name]


def ask(provider, prompt, *, reset=False, timeout=90.):
    last = None
    for event in get_provider(provider).stream(prompt, reset=reset, timeout=timeout):
        last = event
    return last or {'text': '', 'done': False, 'timing': {}, 'provider': provider}


def benchmark():
    """Head-to-head latency benchmark on the Pi: Gemini (4 Hydrogen) vs ChatGPT (Instant)."""
    prompts = [
        ("Turn 1 (fresh chat)", "Reply in 1 short sentence, no markdown: what's 17 times 23?", True),
        ("Turn 2 (warm persistent chat)", "Now add 100 to that in 1 short sentence.", False),
        ("Turn 3 (banter, warm chat)", "In one witty sentence, tell me if a hotdog is a sandwich.", False),
    ]
    for name in ('gemini', 'chatgpt'):
        print(f'\n=== {name.upper()} ===', flush=True)
        for label, prompt, reset in prompts:
            try:
                res = ask(name, prompt, reset=reset, timeout=45.)
                t = res.get('timing', {})
                print(f"  {label}: send={t.get('send_s')}s | TTFT={t.get('ttft_s')}s | "
                      f"1st-sentence={t.get('ttfs_s')}s | total={t.get('total_s')}s", flush=True)
                print(f"    -> {res.get('text', '')[:120]!r}", flush=True)
            except Exception as error:
                print(f'  {label}: FAILED ({error})', flush=True)


if __name__ == '__main__':
    if sys.argv[1:2] == ['bench']:
        benchmark()
    elif sys.argv[1:2] == ['ask'] and len(sys.argv) >= 4:
        prov = sys.argv[2]
        reset = '--reset' in sys.argv[3:]
        q = ' '.join(a for a in sys.argv[3:] if a != '--reset')
        for ev in get_provider(prov).stream(q, reset=reset):
            if ev['delta']:
                print(ev['delta'], end='', flush=True)
            if ev['done']:
                print('\n', json.dumps(ev['timing']), flush=True)
    else:
        print('usage: python3 desktop_cloud.py bench | ask <gemini|chatgpt> [--reset] "prompt"')
