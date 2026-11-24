"""A pretend Ollama for tests: answers instantly with simple, predictable replies, so BITCH can be tested without
the real models.  Run:  python testing/fake_ollama.py 11500   then start BITCH with OLLAMA_URL=http://localhost:11500

It understands just enough: timers, reminders, "remember", lists, dances and new moves; everything else gets a
friendly reply. Embeddings are made from word hashes, so similar sentences still look similar.
"""
import hashlib, json, re, sys
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler


def reply(text):
    t = text.lower(); acts = []; say = "Beep boop! I'm a pretend brain, but I heard you."; feel = 'happy'
    if m := re.search(r'(\d+) minute timer', t): acts = [{'tool': 'set_timer', 'args': {'minutes': int(m.group(1)), 'label': 'timer'}}]; say = 'Timer started!'
    elif 'remind me' in t: acts = [{'tool': 'remind', 'args': {'when': t, 'text': 'the thing'}}]; say = "I'll remind you."
    elif 'remember' in t: acts = [{'tool': 'remember', 'args': {'fact': text.split('remember', 1)[1].strip(' ,.') or text}}]; say = 'Saved!'
    elif 'list' in t and 'add' in t: acts = [{'tool': 'list', 'args': {'action': 'add', 'list': 'shopping', 'items': ['milk']}}]; say = 'Added.'
    elif 'dance' in t: acts = [{'tool': 'perform', 'args': {'what': 'dance'}}]; say = 'Dance time!'; feel = 'excited'
    elif 'do a ' in t: acts = [{'tool': 'perform', 'args': {'what': t.split('do a ', 1)[1]}}]; say = 'Here goes!'; feel = 'excited'
    return {'respond': True, 'feeling': feel, 'actions': acts, 'move': 'nod', 'say': say}


FRAMES = [{'base': 0.4, 'arm': -0.6, 'neck': -0.5, 'head': 0.3, 'secs': 0.4, 'speed': 'snappy', 'wiggle': 'none', 'sound': 'excited'},
          {'base': -0.4, 'arm': -0.6, 'neck': -0.5, 'head': -0.3, 'secs': 0.4, 'speed': 'snappy', 'wiggle': 'head', 'sound': 'none'},
          {'base': 0, 'arm': -0.8, 'neck': -0.8, 'head': 0, 'secs': 0.6, 'speed': 'normal', 'wiggle': 'none', 'sound': 'none'}]


class H(BaseHTTPRequestHandler):
    def _send(s, obj):
        b = json.dumps(obj).encode(); s.send_response(200); s.send_header('Content-Type', 'application/json'); s.send_header('Content-Length', str(len(b))); s.end_headers(); s.wfile.write(b)

    def do_GET(s): s._send({'version': 'fake'})

    def do_POST(s):
        body = json.loads(s.rfile.read(int(s.headers.get('Content-Length', 0))) or b'{}')
        if s.path == '/api/embed':
            v = [0.0] * 64
            for w in re.findall(r'[a-z]+', str(body.get('input', '')).lower()): v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 64] += 1
            return s._send({'embeddings': [v]})
        msgs = body.get('messages', []); last = msgs[-1]['content'] if msgs else ''
        fmt = body.get('format') or {}
        props = fmt.get('properties', {}) if isinstance(fmt, dict) else {}
        if 'frames' in props: out = {'name': 'test move', 'frames': FRAMES, 'after': 'Ta-da!'}
        elif 'memories' in props: out = {'memories': [], 'summary': 'A short chat.'}
        elif 'do' in props: out = {'do': 'nothing', 'say': '', 'move': '', 'feeling': 'neutral', 'why': 'test'}
        elif 'say' in props: out = reply(last.split('\n[')[0].strip('()'))
        else: return s._send({'message': {'role': 'assistant', 'content': 'A pretend answer.'}})
        s._send({'message': {'role': 'assistant', 'content': json.dumps(out)}})

    def log_message(s, *a): pass


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 11500
    print(f'fake Ollama on http://localhost:{port}', flush=True)
    ThreadingHTTPServer(('127.0.0.1', port), H).serve_forever()
