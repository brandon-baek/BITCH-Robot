"""BITCH's local language model.

The conversation model runs in BITCH's own llama.cpp server (the `llama-server` that ships inside Ollama), loaded
from the model file Ollama already downloaded. Why not just Ollama: the model is a hybrid (part transformer, part
recurrent), and Ollama re-reads the whole prompt on every turn for those, about 1.5 s. llama-server keeps context
checkpoints, so a follow-up turn only reads the new words (about 0.3 s). Two slots: one for the conversation, one
for background work (memory, summaries), so background jobs never sit in front of a reply.

Anything else (the small vision model for quick camera checks, or the conversation model if the server can't run)
goes to Ollama as before.
"""
import atexit
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

from . import config

URL = os.environ.get('OLLAMA_URL') or config.get('ollama_url')
SERVER_PORT = int(os.environ.get('BITCH_LLM_PORT', '11450'))
SERVER_URL = f'http://127.0.0.1:{SERVER_PORT}'
CONTEXT = 8192
CHAT_KEEP_ALIVE = '2h'
SLOT_TALK, SLOT_BACKGROUND = 0, 1
last_stats = None   # prompt tokens read and timing of the last streamed reply, for latency checks

server = None           # the llama-server process BITCH started, if any
server_model = None     # which chat model it is serving
server_lock = threading.Lock()


def request(path, payload, timeout, base=None):
    return urllib.request.urlopen(urllib.request.Request(
        (base or URL) + path, data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'}),
        timeout=timeout)


# ---------------------------------------------------------------- BITCH's own model server

def models_dir():
    custom = os.environ.get('OLLAMA_MODELS')
    candidates = ([Path(custom)] if custom else []) + [config.HOME / 'ollama', Path('~/.bitch/ollama').expanduser(),
                                                       Path('~/.ollama/models').expanduser()]
    for folder in candidates:
        if not (folder / 'manifests').is_dir():
            continue
        return folder
    return None


def model_files(name):
    """The GGUF weights (and vision projector) Ollama downloaded for a model name, or (None, None)."""
    if not name:
        return None, None
    custom = os.environ.get('OLLAMA_MODELS')
    for folder in ([Path(custom)] if custom else []) + [config.HOME / 'ollama', Path('~/.bitch/ollama').expanduser(),
                                                        Path('~/.ollama/models').expanduser()]:
        found = model_files_in(folder, name)
        if found[0]:
            return found
    return None, None


def model_files_in(folder, name):
    repo, _, tag = name.partition(':')
    parts = repo.split('/')
    if len(parts) == 1:
        parts = ['library'] + parts
    manifest = folder / 'manifests' / 'registry.ollama.ai' / Path(*parts) / (tag or 'latest')
    try:
        layers = json.loads(manifest.read_text())['layers']
    except (OSError, ValueError, KeyError):
        return None, None
    blob = lambda digest: folder / 'blobs' / digest.replace(':', '-')
    weights = next((blob(l['digest']) for l in layers if l['mediaType'].endswith('.model')), None)
    projector = next((blob(l['digest']) for l in layers if l['mediaType'].endswith('.projector')), None)
    return (weights if weights and weights.exists() else None), (projector if projector and projector.exists() else None)


def server_binary():
    for path in (os.environ.get('BITCH_LLAMA_SERVER'), '/Applications/Ollama.app/Contents/Resources/llama-server',
                 shutil.which('llama-server')):
        if path and os.access(path, os.X_OK):
            return path
    return None


def server_up():
    try:
        with urllib.request.urlopen(SERVER_URL + '/health', timeout=1) as response:
            return json.load(response).get('status') == 'ok'
    except (OSError, ValueError):
        return False


def start_server(wait=240):
    """Start (or adopt) the llama-server for the chat model. Returns True when it is serving."""
    global server, server_model
    model = config.get('chat_model')
    with server_lock:
        if server_model == model and server and server.poll() is None and server_up():
            return True
        if os.environ.get('BITCH_LLM_SERVER') == 'off' or not config.get('llm_server'):
            return False
        binary = server_binary()
        weights, projector = model_files(model)
        if not binary or not weights:
            return False
        stop_server_locked()
        # The same weights loaded in Ollama would take the memory twice.
        try:
            request('/api/generate', {'model': model, 'keep_alive': 0}, 5).read()
        except (OSError, ValueError):
            pass
        command = [binary, '-m', str(weights), '--host', '127.0.0.1', '--port', str(SERVER_PORT),
                   '-c', str(CONTEXT * 2), '-np', '2', '--jinja', '-ngl', '99', '--no-webui',
                   # A checkpoint every few tokens: a follow-up turn resumes right after the last one. A few recent
                   # ones are enough, and each holds the model's whole recurrent state (hundreds of MB).
                   '--ctx-checkpoints', '8', '--checkpoint-min-step', '16',
                   # The saved-prompt cache defaults to 8 GB of RAM; on a 32 GB Mac that pushes the model's own
                   # weights out to disk and every word stutters.
                   '--cache-ram', '1024']
        if projector:
            command += ['--mmproj', str(projector)]
        log = open(config.HOME / 'llm-server.log', 'ab')
        server = subprocess.Popen(command, stdout=log, stderr=log, start_new_session=True)
        server_model = model
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if server.poll() is not None:
            return False
        if server_up():
            return True
        time.sleep(.5)
    return False


def stop_server_locked():
    global server, server_model
    if server and server.poll() is None:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
    server = server_model = None


@atexit.register
def stop_server():
    with server_lock:
        stop_server_locked()


def served(model):
    """True when this call can go to BITCH's own server."""
    return ((model is None or model == config.get('chat_model')) and server is not None and server.poll() is None
            and server_model == config.get('chat_model'))


def openai_messages(messages):
    """Ollama-style messages (with an `images` list) -> OpenAI-style content parts for llama-server."""
    out = []
    for message in messages:
        images = message.get('images')
        if not images:
            out.append({'role': message['role'], 'content': message['content']})
            continue
        parts = [{'type': 'text', 'text': message['content']}]
        for image in images:
            data = image if isinstance(image, str) else base64.b64encode(image).decode('ascii')
            parts.append({'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + data}})
        out.append({'role': message['role'], 'content': parts})
    return out


def server_chat(messages, max_tokens, schema, timeout, on_delta, stop, temperature, slot):
    global last_stats
    payload = {'messages': openai_messages(messages), 'max_tokens': max_tokens, 'temperature': temperature,
               'stream': on_delta is not None, 'cache_prompt': True, 'id_slot': slot,
               'chat_template_kwargs': {'enable_thinking': False}}
    if schema is not None:
        payload['response_format'] = {'type': 'json_schema', 'json_schema': {'name': 'reply', 'schema': schema}}
    with request('/v1/chat/completions', payload, timeout, base=SERVER_URL) as response:
        if on_delta is None:
            data = json.load(response)
            return data['choices'][0]['message']['content']
        content = ''
        for line in response:
            if stop is not None and stop.is_set():
                return ''
            line = line.strip()
            if not line.startswith(b'data:'):
                continue
            body = line[5:].strip()
            if body == b'[DONE]':
                break
            chunk = json.loads(body)
            if chunk.get('error'):
                raise ValueError(chunk['error'])
            choice = (chunk.get('choices') or [{}])[0]
            delta = (choice.get('delta') or {}).get('content') or ''
            if delta:
                content += delta
                on_delta(content)
            timings = chunk.get('timings')
            if timings:
                last_stats = {'tokens': timings.get('prompt_n'), 'prefill_s': round(timings.get('prompt_ms', 0) / 1000, 2),
                              'out_tokens': timings.get('predicted_n'),
                              'tok_per_s': round(timings.get('predicted_per_second') or 0, 1)}
            if choice.get('finish_reason'):
                break
        return content


# ---------------------------------------------------------------- the one call everything uses

def chat(messages, max_tokens=160, *, model=None, schema=None, timeout=None, on_delta=None, stop=None,
         temperature=0.45, background=False):
    """Ask the model. `background` work uses its own slot so it never holds up a reply."""
    if served(model):
        try:
            return server_chat(messages, max_tokens, schema, timeout if timeout is not None else 60, on_delta, stop,
                               temperature, SLOT_BACKGROUND if background else SLOT_TALK)
        except (OSError, ValueError, KeyError, urllib.error.URLError):
            if stop is not None and stop.is_set():
                return ''
            # Fall through to Ollama for this call; the server may be restarting.
    return ollama_chat(messages, max_tokens, model, schema, timeout, on_delta, stop, temperature)


def ollama_chat(messages, max_tokens, model, schema, timeout, on_delta, stop, temperature):
    global last_stats
    payload = {
        'model': model or config.get('chat_model'), 'messages': messages, 'stream': on_delta is not None,
        'think': False, 'keep_alive': '30m' if model else CHAT_KEEP_ALIVE,
        'options': {'temperature': temperature, 'num_ctx': CONTEXT, 'num_predict': max_tokens},
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
                last_stats = {'tokens': chunk.get('prompt_eval_count'),
                              'prefill_s': round((chunk.get('prompt_eval_duration') or 0) / 1e9, 2),
                              'out_tokens': chunk.get('eval_count'),
                              'tok_per_s': round((chunk.get('eval_count') or 0) / max(1e-9, (chunk.get('eval_duration') or 0) / 1e9), 1)}
                return content
        raise OSError('Model stream ended before completion')


SMALL_VISION = 'qwen3.5:4b-q4_K_M'   # quick background image checks, and the fallback for camera answers
_vision = {}


def vision_model():
    """The chat model when it can see (so camera answers get the smart brain), otherwise the small one."""
    model = config.get('chat_model')
    if served(None):
        return None if model_files(model)[1] else SMALL_VISION
    if model not in _vision:
        try:
            with request('/api/show', {'model': model}, 5) as response:
                _vision[model] = 'vision' in (json.load(response).get('capabilities') or ())
        except (OSError, ValueError, KeyError):
            return SMALL_VISION
    return model if _vision[model] else SMALL_VISION


def prefill(messages):
    """Read a conversation into the cache ahead of time (while the person is still talking)."""
    if not served(None):
        return
    try:
        server_chat(messages, 1, None, 30, None, None, 0, SLOT_TALK)
    except (OSError, ValueError, KeyError, urllib.error.URLError):
        pass


def warm_context(system):
    if not served(None):
        start_server()
    chat([{'role': 'system', 'content': system}, {'role': 'user', 'content': 'Hello.'}], max_tokens=1)


def warm(system):
    def load():
        try:
            warm_context(system)
        except (OSError, ValueError, KeyError):
            pass

    threading.Thread(target=load, daemon=True).start()
