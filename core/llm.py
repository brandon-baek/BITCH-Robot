"""A single local Ollama model for conversation."""
import json
import os
import threading
import urllib.request

from . import config

URL = os.environ.get('OLLAMA_URL') or config.get('ollama_url')
CHAT_KEEP_ALIVE = '2h'


def request(path, payload, timeout):
    return urllib.request.urlopen(urllib.request.Request(
        URL + path, data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'}),
        timeout=timeout)


def chat(messages, max_tokens=160, *, model=None, schema=None, timeout=None, on_delta=None, stop=None,
         temperature=0.45):
    payload = {
        'model': model or config.get('chat_model'), 'messages': messages, 'stream': on_delta is not None,
        'think': False, 'keep_alive': '30m' if model else CHAT_KEEP_ALIVE,
        'options': {'temperature': temperature, 'num_ctx': 8192, 'num_predict': max_tokens},
    }
    if schema is not None:
        payload['format'] = schema
    with request('/api/chat', payload, timeout if timeout is not None else (60 if model else 30)) as response:
        if on_delta is None:
            return json.load(response)['message']['content']
        content = ''
        for line in response:
            if stop is not None and stop.is_set():
                return ''
            if not line.strip():
                continue
            chunk = json.loads(line)
            if chunk.get('error'):
                raise ValueError(chunk['error'])
            delta = chunk.get('message', {}).get('content', '')
            if delta:
                content += delta
                on_delta(content)
            if chunk.get('done'):
                return content
        raise OSError('Model stream ended before completion')


def warm_context(system):
    model = config.get('chat_model')
    try:
        with request('/api/show', {'model': model}, 3) as response:
            details = json.load(response)
        if details['details']['family'] == 'qwen35' and details.get('template', '').strip() == '{{ .Prompt }}':
            # End inside the fixed system message so the recurrent checkpoint
            # survives differences in speaker facts, history, and user input.
            prompt = '<|im_start|>system\n' + system
            with request('/api/generate', {
                'model': model, 'prompt': prompt, 'raw': True, 'think': False, 'stream': False,
                'keep_alive': CHAT_KEEP_ALIVE, 'options': {'num_ctx': 8192, 'num_predict': 1},
            }, 30) as response:
                json.load(response)
            return
    except (OSError, ValueError, KeyError):
        pass
    chat([{'role': 'system', 'content': system}, {'role': 'user', 'content': 'Hello.'}], max_tokens=1)


def warm(system):
    def load():
        try:
            warm_context(system)
        except (OSError, ValueError, KeyError):
            pass

    threading.Thread(target=load, daemon=True).start()
