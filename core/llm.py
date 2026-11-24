"""Talking to the on-device language models in Ollama (http://localhost:11434).

Two brains, both Qwen 3.5 and both free and local:
  chat_model (4B)  answers you in about a second
  deep_model (9B)  slower and smarter, used in the background: inventing moves, sorting memories, deciding
                   whether to speak up on its own
Replies are JSON that follows a schema, which keeps small models reliable.
"""
import base64, json, os, threading, time, urllib.request
import numpy as np
from . import config

URL = os.environ.get('OLLAMA_URL', 'http://localhost:11434')   # tests point this at testing/fake_ollama.py
_ok = [None]


def _post(path, body, timeout):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def available():
    try:
        urllib.request.urlopen(URL + '/api/version', timeout=2).read(); return True
    except Exception:
        return False


def chat(messages, schema=None, model=None, deep=False, temperature=0.6, images=None, timeout=120, keep_alive='30m', max_tokens=900):
    """Ask a model. Returns the parsed JSON if a schema is given, else the text. Raises on failure."""
    model = model or config.get('deep_model' if deep else 'chat_model')
    msgs = [dict(m) for m in messages]
    if images:
        msgs[-1]['images'] = [base64.b64encode(i).decode() for i in images]
    body = {'model': model, 'messages': msgs, 'stream': False, 'think': False, 'keep_alive': keep_alive,
            'options': {'temperature': temperature, 'num_ctx': 8192, 'num_predict': max_tokens}}
    if schema: body['format'] = schema
    try:
        r = _post('/api/chat', body, timeout)
    except Exception:
        if deep and model != config.get('chat_model'):          # the big brain isn't there: use the small one
            return chat(messages, schema, config.get('chat_model'), False, temperature, images, timeout, keep_alive, max_tokens)
        raise
    out = r['message']['content']
    return json.loads(out) if schema else out


_emb_lock = threading.Lock()
_emb_ok = [True]


def embed(text, kind='doc'):
    """A meaning vector for memory search (nomic-embed-text). None if it's not available."""
    if not _emb_ok[0]: return None
    prefix = 'search_query: ' if kind == 'query' else 'search_document: '
    try:
        with _emb_lock:
            r = _post('/api/embed', {'model': config.get('embed_model'), 'input': prefix + text, 'keep_alive': '60m'}, 30)
        v = np.asarray(r['embeddings'][0], np.float32)
        return v / (np.linalg.norm(v) + 1e-9)
    except Exception:
        _emb_ok[0] = False
        threading.Timer(120, lambda: _emb_ok.__setitem__(0, True)).start()     # try again in a while
        return None


def warm():
    """Load the fast brain into memory so the first answer is quick."""
    def go():
        for _ in range(30):
            try:
                _post('/api/chat', {'model': config.get('chat_model'), 'messages': [{'role': 'user', 'content': 'hi'}],
                                    'stream': False, 'think': False, 'keep_alive': '30m', 'options': {'num_predict': 1, 'num_ctx': 8192}}, 120)
                embed('hello'); return
            except Exception:
                time.sleep(5)
    threading.Thread(target=go, daemon=True).start()
