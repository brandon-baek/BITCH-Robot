# Listen mode: another device (your Mac) hears you and sends the words here;
# Desk Creature reacts with moves and its own voice.
import threading, queue, random, re, socket, json, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from desk import *
PORT=8765
Q=queue.Queue()
class _H(BaseHTTPRequestHandler):
    def _cors(s):
        s.send_header('Access-Control-Allow-Origin','*'); s.send_header('Access-Control-Allow-Private-Network','true')
        s.send_header('Access-Control-Allow-Headers','*')
    def do_OPTIONS(s): s.send_response(204); s._cors(); s.end_headers()
    def do_GET(s):
        u=urlparse(s.path); q=parse_qs(u.query)
        if u.path=='/hear':
            t=(q.get('text') or [''])[0].strip()
            if t: Q.put(t)
        s.send_response(200); s._cors(); s.send_header('Content-Type','application/json'); s.end_headers()
        s.wfile.write(json.dumps({'ok':True}).encode())
    def log_message(s,*a): pass
def has(t,*ws): return any(re.search(r'\b'+w+r'\b',t) for w in ws)
def react(A,text):
    t=text.lower(); print(f'\n  heard: "{text}"',flush=True)
    if has(t,'dance','party','groove','disco'): say('dance time!'); import shows; shows.disco(); return 'show'
    if has(t,'sing','opera','song'): say('performing!'); import shows; shows.opera(); return 'show'
    if has(t,'good night','goodnight','sleep','bed'): say('ok... sleepy'); bedtime(); return 'sleep'
    if has(t,'scary','boo','ghost','monster'): say('AAAH!'); A.to(a=-0.95,n=-1.0,h=-0.3,stiff=4).wait(0.4).shake('h',0.1,9,1.2).wait(1.4); A.rest(stiff=0.8).wait(1); return
    if has(t,'hi','hello','hey','yo','morning','sup'):
        say('Hi! waves'); A.to(a=-0.55,n=-0.5,stiff=1.6).wait(0.4).shake('h',0.4,2.2,1.4).wait(1.6); A.rest(stiff=1).wait(0.8); return
    if has(t,'bye','goodbye','see you','later'):
        A.sfx('hello'); A.shake('h',0.35,2,1.4).wait(1.6); A.sfx('sad'); A.to(n=-0.95,stiff=0.6).wait(1.5); A.rest().wait(1); return
    if has(t,'name','who are you'):
        A.sfx('walle'); A.to(a=-0.95,n=-0.45,h=0.2,stiff=0.9).wait(1.6); A.rest().wait(0.8); return
    if has(t,'how are you',"how's it going",'how you doing','feeling'):
        say('happy!'); A.to(a=-0.55,n=-0.45,stiff=1.5).wait(0.4).shake('h',0.3,2.5,1.2).wait(1.4); A.rest().wait(0.8); return
    if has(t,'love','cute','good boy','good job','awesome','amazing','nice','smart','cool','best','beautiful'):
        say('in love <3 (shy)'); A.to(b=0.4,h=0.35,n=-0.95,stiff=0.8).wait(1.4); A.to(b=0,h=0,n=-0.6,stiff=1.3).wait(0.6); A.sfx('giggle'); A.rest().wait(0.8); return
    if has(t,'stupid','dumb','bad','ugly','hate','shut up','idiot','suck'):
        if random.random()<0.5: say('sad...'); A.to(b=-0.15,a=-0.9,n=-1.0,h=0.35,stiff=0.3).wait(2.5)
        else: say('angry!!'); A.to(a=-0.45,n=-0.95,stiff=3).wait(0.4).shake('b',0.12,6,1).wait(1.2)
        A.rest(stiff=0.6).wait(1); return
    if has(t,'haha','lol','funny','joke','hehe'): say('giggle'); A.shake('n',0.12,5,1.2).wait(1.4); return
    if has(t,'left'): A.sfx('blip'); A.to(b=0.7,stiff=1.5).wait(1.5); return
    if has(t,'right'): A.sfx('blip'); A.to(b=-0.7,stiff=1.5).wait(1.5); return
    if has(t,'come here','closer','come closer'): A.sfx('curious'); A.to(a=-0.3,n=-0.45,stiff=1).wait(1.5); return
    if has(t,'back up','go back','too close'): A.sfx('startle'); A.to(a=-0.95,n=-0.95,stiff=1.5).wait(1.2); return
    if has(t,'spin','turn around'): A.sfx('excited'); A.to(b=0.85,stiff=1.2).wait(0.9).to(b=-0.85).wait(1.2).to(b=0).wait(0.8); return
    if has(t,'yes','yeah','yep','ok','okay','sure'): A.sfx('beepboop'); [A.to(n=-0.5,stiff=3).wait(0.2) or A.to(n=-0.8).wait(0.2) for _ in range(2)]; return
    if has(t,'no','nope','nah'): A.sfx('grumble'); [A.to(h=0.35,stiff=3.5).wait(0.15) or A.to(h=-0.35).wait(0.15) for _ in range(2)]; A.to(h=0).wait(0.3); return
    if t.endswith('?') or has(t,'what','why','how','who','where','when','can','do','is','are','will','should','would'):
        say('hmm... thinking'); A.sfx('think'); A.to(h=random.choice([-0.45,0.45]),n=-0.6,stiff=1).wait(1.6)
        r=random.random()
        if r<0.4: say('yes!'); [A.to(n=-0.45,stiff=3).wait(0.22) or A.to(n=-0.8).wait(0.22) for _ in range(2)]
        elif r<0.75: say('nope'); A.sfx('grumble'); [A.to(h=0.35,stiff=3.5).wait(0.16) or A.to(h=-0.35).wait(0.16) for _ in range(2)]
        else: say('no idea?'); A.to(b=0.15,h=0.4).wait(0.3).to(b=-0.15,h=-0.4).wait(0.3)
        A.rest(stiff=1).wait(0.8); return
    say('huh? curious'); A.to(h=random.choice([-0.4,0.4]),a=-0.6,n=-0.6,stiff=1).wait(1.4); A.rest().wait(0.6)
def listen():
    srv=ThreadingHTTPServer(('0.0.0.0',PORT),_H); threading.Thread(target=srv.serve_forever,daemon=True).start()
    try: ip=socket.gethostbyname(socket.gethostname()+'.local')
    except Exception: ip='?'
    print(f'\n  LISTEN MODE on port {PORT}. Robot address: raspberrypi.local (or {ip})')
    print('  Talk to it from talk.html on your Mac. Ctrl+C to stop.\n',flush=True)
    A=Actor(); last=time.time()
    try:
        while True:
            try: text=Q.get(timeout=0.05)
            except queue.Empty:
                A.wait(0.05)
                if time.time()-last>random.uniform(5,9):
                    last=time.time(); A.to(b=random.uniform(-0.4,0.4),h=random.uniform(-0.2,0.2),stiff=1.2)
                continue
            res=react(A,text); last=time.time()
            if res=='sleep':
                print('  (asleep; say "wake up" to wake it)',flush=True)
                while True:
                    t=Q.get()
                    if has(t.lower(),'wake','morning','hey','hello'): break
                A=Actor()
            elif res=='show': A=Actor()
            while not Q.empty(): Q.get()          # ignore anything said during a reaction
    except KeyboardInterrupt: pass
    finally: A.end()
if __name__=='__main__': listen()
