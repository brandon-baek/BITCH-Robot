"""A single local Ollama model for conversation."""
import json
import os
import threading
import urllib.request

from . import config

URL = os.environ.get('OLLAMA_URL', 'http://127.0.0.1:11434')


def chat(messages, max_tokens=160, *, model=None, schema=None, timeout=None):
    payload = {
        'model': model or config.get('chat_model'), 'messages': messages, 'stream': False,
        'think': False, 'keep_alive': '30m',
        'options': {'temperature': 0.45, 'num_ctx': 4096, 'num_predict': max_tokens},
    }
    if schema is not None:
        payload['format'] = schema
    data = json.dumps(payload).encode()
    request = urllib.request.Request(URL + '/api/chat', data=data, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=timeout if timeout is not None else (60 if model else 30)) as response:
        return json.load(response)['message']['content']


def warm():
    def load():
        try:
            chat([{'role': 'user', 'content': 'Hi'}], max_tokens=1)
        except (OSError, ValueError, KeyError):
            pass

    threading.Thread(target=load, daemon=True).start()
