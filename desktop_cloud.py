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
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

import desktop_chatgpt as cdp

CHATGPT_URL = os.environ.get('BITCH_CHATGPT_URL', 'https://chatgpt.com/')
GEMINI_URL = os.environ.get('BITCH_GEMINI_URL', 'https://gemini.google.com/app')
MAX_TURNS_PER_CHAT = 24
IDLE_RESET_SECONDS = 900.

# ---------------------------------------------------------------- Page-side helpers (single-eval fast send + push stream)

CHATGPT_HELPERS = r'''
const q = (s) => document.querySelector(s);
const all = (s) => Array.from(document.querySelectorAll(s));
const vis = (el) => !!el && el.offsetParent !== null;
const composer = () => all('#prompt-textarea, div.ProseMirror[contenteditable="true"], textarea[name="prompt-textarea"]')
  .find((el) => vis(el) && (el.isContentEditable || el.tagName === 'TEXTAREA')) || null;
const sendBtn = () => all('[data-testid="send-button"], #composer-submit-button, button[aria-label="Send prompt"], button[aria-label="Send message"]')
  .find((el) => vis(el) && !el.disabled && !/stop/i.test(el.getAttribute('aria-label') || '')) || null;
const stopBtn = () => all('[data-testid="stop-button"], button[aria-label^="Stop"]').find(vis) || null;
const first = (sels) => { for (const s of sels) { const f = all(s); if (f.length) return f; } return []; };
const replies = () => first(['[data-response-body]', '[data-markdown-text-style="assistant-message"]',
                             '[data-message-author-role="assistant"]']);
const textOf = (el) => {
  if (!el) return '';
  const body = el.querySelector('.markdown') || el;
  return (body.innerText || '').trim();
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
const sendBtn = () => all('button.send-button, button[aria-label="Send message"], button[aria-label^="Send"], .send-button-container button')
  .find((el) => vis(el) && !el.disabled && el.getAttribute('aria-disabled') !== 'true' && !/stop/i.test(el.getAttribute('aria-label') || '')) || null;
const stopBtn = () => all('button[aria-label*="Stop"], button.stop, .stop-icon').find(vis) || null;
const replies = () => {
  for (const s of ['model-response message-content .markdown', 'model-response .markdown', 'message-content .markdown', 'model-response']) {
    const f = all(s);
    if (f.length) return f;
  }
  return [];
};
const textOf = (el) => (el ? (el.innerText || '').trim() : '');
const isStreaming = () => {
  if (stopBtn()) return true;
  const busy = all('model-response[aria-busy="true"], .loading-indicator, mat-progress-bar').find(vis);
  return !!busy;
};
const isComplete = () => false;
'''


class StreamTab:
    """Persistent CDP WebSocket that also receives push notifications from Runtime.addBinding."""

    def __init__(self, ws_url):
        self.ws = cdp.Socket(ws_url)
        self.next_id = 0
        self.events = []

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

    def eval(self, expression, timeout=15.):
        res = self.call('Runtime.evaluate', {
            'expression': expression, 'awaitPromise': True, 'returnByValue': True,
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

    def _connect(self):
        if self.tab is not None:
            try:
                self.tab.eval('1', timeout=2.)
                return self.tab
            except Exception:
                self._close()
        err = cdp.start_browser()
        if err:
            raise RuntimeError(err)
        pages = [p for p in cdp.devtools('/json/list') if p.get('type') == 'page']
        matches = [p for p in pages if urlsplit(p.get('url', '')).hostname == self.host]
        page = matches[0] if matches else None
        if page is None:
            page = cdp.devtools('/json/new?' + self.url, method='PUT', timeout=5.)
            # Wait for initial load if newly created.
            time.sleep(2.)
        self.tab_id = page['id']
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

    def _ensure_fast_mode(self, tab):
        if self.mode_checked:
            return
        try:
            if self.name == 'gemini':
                # Switch Gemini to "Everyday help 4 Hydrogen" (or "Fast" / "Flash") if not already on it.
                cur = tab.eval(
                    '(() => { const b = Array.from(document.querySelectorAll("button"))'
                    '.find(e => e.offsetParent && /mode picker/i.test(e.getAttribute("aria-label") || ""));'
                    'return b ? (b.innerText || b.getAttribute("aria-label") || "").trim() : null; })()',
                    timeout=3.,
                )
                if cur and re.search(r'hydrogen|fast|flash', cur, re.I):
                    self.mode_checked = True
                    return
                if cur is not None:
                    tab.eval(
                        '(() => { const b = Array.from(document.querySelectorAll("button"))'
                        '.find(e => e.offsetParent && /mode picker/i.test(e.getAttribute("aria-label") || ""));'
                        'if (b) b.click(); return true; })()',
                        timeout=3.,
                    )
                    time.sleep(.35)
                    picked = tab.eval(
                        '(() => { const items = Array.from(document.querySelectorAll('
                        '"[role=menuitem],[role=menuitemradio],[role=option],button.mat-mdc-menu-item"))'
                        '.filter(e => e.offsetParent);'
                        'const hit = items.find(e => /hydrogen|everyday|fast|flash/i.test(e.innerText || ""));'
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

    def stream(self, prompt, *, reset=False, timeout=90., stop_event=None):
        """Send `prompt` and yield incremental dicts: {'text': full_so_far, 'delta': new_chars, 'done': bool, 'timing': ...}."""
        with self.lock:
            t0 = time.monotonic()
            tab = self._connect()
            if reset or (self.turns >= MAX_TURNS_PER_CHAT) or (self.last_used and time.monotonic() - self.last_used > IDLE_RESET_SECONDS):
                # Unlock not needed: inline fast reset
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
                self.turns = 0
                self.primed = False
            if not self._ensure_ready(tab, wait=15.):
                raise RuntimeError(f'{self.name} composer not ready')
            self._ensure_fast_mode(tab)
            t_ready = time.monotonic()

            # Single-eval fast insert + send + install push MutationObserver.
            tab.drain()
            prompt_js = json.dumps(prompt)
            send_script = f'''(async () => {{
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
                if (box.tagName === 'TEXTAREA') {{
                    const set = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
                    set.call(box, {prompt_js});
                    box.dispatchEvent(new Event('input', {{bubbles: true}}));
                }} else {{
                    const ok = document.execCommand('insertText', false, {prompt_js});
                    if (!ok || !(box.innerText || '').trim()) {{
                        const dt = new DataTransfer();
                        dt.setData('text/plain', {prompt_js});
                        box.dispatchEvent(new ClipboardEvent('paste', {{clipboardData: dt, bubbles: true, cancelable: true}}));
                    }}
                }}
                // Wait up to 8 animation frames (~120 ms max) for Send button to enable, then click immediately.
                let sent = false;
                for (let i = 0; i < 10; i++) {{
                    await new Promise(r => requestAnimationFrame(r));
                    const btn = sendBtn();
                    if (btn) {{ btn.click(); sent = true; break; }}
                }}
                if (!sent) {{
                    box.dispatchEvent(new KeyboardEvent('keydown', {{key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}}));
                    sent = true;
                }}
                // Install push observer: notifies Python instantly on text changes and completion.
                let lastSent = '';
                let stableTicks = 0;
                let sawStreaming = false;
                let finished = false;
                const check = () => {{
                    if (finished) return;
                    const rs = replies();
                    if (rs.length <= beforeCount) return;
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
                    const doneNow = Boolean(txt && !busy && (complete || (sawStreaming && stableTicks >= 4) || stableTicks >= 9));
                    if (doneNow) {{
                        finished = true;
                        if (window.__bitchObs) {{ window.__bitchObs.disconnect(); window.__bitchObs = null; }}
                        if (window.__bitchTimer) {{ clearInterval(window.__bitchTimer); window.__bitchTimer = null; }}
                        window.__bitchStream(JSON.stringify({{text: txt, done: true}}));
                    }}
                }};
                window.__bitchObs = new MutationObserver(check);
                window.__bitchObs.observe(document.body, {{childList: true, subtree: true, characterData: true}});
                window.__bitchTimer = setInterval(check, 55);
                return {{ok: true, beforeCount}};
            }})()'''
            sent_res = tab.eval(send_script, timeout=8.)
            if not sent_res or not sent_res.get('ok'):
                raise RuntimeError(f'{self.name} send failed: {sent_res}')
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
