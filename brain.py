# Listen mode: another device (your Mac) hears you and sends the words here;
# Desk Creature reacts with moves and its own voice.
import threading, queue, random, re, socket, json, time, os
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from desk import *
PORT=8765
GAZE={'span':1.4,'flip':1}   # flip=-1 if it looks the wrong way
Q=queue.Queue()
LOOK={'x':0.5,'y':0.5,'seen':False,'t':0.0,'away':0.0}
class _H(BaseHTTPRequestHandler):
    def _cors(s):
        s.send_header('Access-Control-Allow-Origin','*'); s.send_header('Access-Control-Allow-Private-Network','true')
        s.send_header('Access-Control-Allow-Headers','*')
    def _ok(s,obj=None):
        s.send_response(200); s._cors(); s.send_header('Content-Type','application/json'); s.end_headers()
        s.wfile.write(json.dumps(obj or {'ok':True}).encode())
    def do_OPTIONS(s): s.send_response(204); s._cors(); s.end_headers()
    def do_GET(s):
        u=urlparse(s.path); q={k:v[0] for k,v in parse_qs(u.query).items()}
        if u.path=='/hear' and q.get('text','').strip(): Q.put(('hear',q['text'].strip()))
        elif u.path=='/stop': STOP.set()
        elif u.path=='/emote': Q.put(('emote',q.get('emotion','neutral'),q.get('action','none')))
        elif u.path=='/say': Q.put(('say',q.get('text',''),q.get('emotion','neutral'),q.get('action','none')))
        elif u.path=='/look':
            seen=q.get('seen','1')=='1'
            if seen and not LOOK['seen'] and time.time()-LOOK['t']>8: Q.put(('arrived',))
            LOOK.update(x=float(q.get('x',0.5)),y=float(q.get('y',0.5)),seen=seen,t=time.time(),
                        b=float(q['b']) if 'b' in q else None,n=float(q['n']) if 'n' in q else None)
        s._ok({'ok':True,'busy':BUSY[0]})
    def do_POST(s):
        u=urlparse(s.path); q={k:v[0] for k,v in parse_qs(u.query).items()}
        n=int(s.headers.get('Content-Length',0)); data=s.rfile.read(n)
        if u.path=='/speak':
            p='/tmp/desk_speech_%d.wav'%int(time.time()*1000)
            open(p,'wb').write(data)
            Q.put(('speak',p,q.get('emotion','neutral'),q.get('action','none'),q.get('text','')))
        s._ok({'ok':True,'busy':BUSY[0]})
    def log_message(s,*a): pass
BUSY=[False]
STOP=threading.Event()
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
EMO={ # emotion -> (pose targets, chirp)
 'happy':(dict(a=-0.6,n=-0.5,h=0.15),'happy'),'excited':(dict(a=-0.5,n=-0.45),'excited'),'sad':(dict(a=-0.9,n=-1.0,h=0.3),'sad'),
 'angry':(dict(a=-0.5,n=-0.95,h=0),'angry'),'scared':(dict(a=-0.95,n=-1.0,h=-0.25),'whimper'),'love':(dict(a=-0.6,n=-0.6,h=0.35),'love'),
 'curious':(dict(a=-0.55,n=-0.55,h=-0.35),'curious'),'sleepy':(dict(a=-0.85,n=-0.95,h=0.3),'yawn'),'proud':(dict(a=-0.95,n=-0.45,h=0),'proud'),
 'confused':(dict(a=-0.75,n=-0.7,h=0.45),'question'),'neutral':(dict(a=-0.8,n=-0.8,h=0),None)}
def pose(A,emotion,stiff=1.2):
    t,_=EMO.get(emotion,EMO['neutral']); A.to(stiff=stiff,**t)
def act(A,action):
    """returns 'show'/'sleep' when it ran something that ends this Actor"""
    a=(action or 'none').lower()
    if a=='nod':   [A.to(n=-0.45,stiff=3).wait(0.22) or A.to(n=-0.8).wait(0.22) for _ in range(2)]
    elif a=='shake': [A.to(h=0.35,stiff=3.5).wait(0.16) or A.to(h=-0.35).wait(0.16) for _ in range(2)]; A.to(h=0).wait(0.2)
    elif a=='wiggle': A.shake('h',0.35,3,1.2).shake('b',0.15,3,1.2).wait(1.4)
    elif a=='look_around': A.to(b=0.6,stiff=1.4).wait(1).to(b=-0.6).wait(1.3).to(b=0).wait(0.8)
    elif a=='lean_in': A.to(a=-0.3,n=-0.45,stiff=1).wait(1.5)
    elif a=='lean_back': A.to(a=-0.95,n=-0.95,stiff=1.5).wait(1.2)
    elif a=='spin': A.sfx('excited'); A.to(b=0.85,stiff=1.2).wait(0.9).to(b=-0.85).wait(1.2).to(b=0).wait(0.8)
    elif a=='bow': A.to(a=-0.3,n=-0.15,h=0,stiff=0.7).wait(1.3).to(a=-0.8,n=-0.8).wait(1)
    elif a=='wave': A.shake('h',0.4,2.2,1.4).wait(1.6)
    elif a=='dance': import shows; shows.disco(); return 'show'
    elif a=='sing': import shows; shows.opera(); return 'show'
    elif a=='sleep': bedtime(); return 'sleep'
def _envelope(path,step=0.05):
    import wave as _w, struct as _st
    w=_w.open(path); sr=w.getframerate(); n=w.getnframes(); ch=w.getnchannels(); sw=w.getsampwidth()
    d=w.readframes(n); w.close()
    if sw!=2: return [0.5]*int(n/sr/step)
    x=_st.unpack('<%dh'%(len(d)//2),d)[::ch]; k=max(1,int(sr*step)); env=[]
    for i in range(0,len(x),k):
        seg=x[i:i+k]; env.append((sum(v*v for v in seg)/max(1,len(seg)))**0.5/32768)
    m=max(env+[1e-6]); return [e/m for e in env]
def talk(A,path,emotion='neutral',text=''):
    """play speech on the robot's speaker and move like it is talking"""
    import shutil as _sh, subprocess as _sp
    if text: print(f'  robot: "{text}"',flush=True)
    pose(A,emotion,1.3); A.wait(0.15)
    env=_envelope(path); pl=_sh.which('pw-play') or _sh.which('paplay') or _sh.which('aplay')
    try: pr=_sp.Popen([pl,path],stdout=_sp.DEVNULL,stderr=_sp.DEVNULL) if pl else None
    except Exception: pr=None
    t0,_=EMO.get(emotion,EMO['neutral']); n0=t0.get('n',-0.8); h0=t0.get('h',0)
    STOP.clear()
    for i,e in enumerate(env):                        # 'lip sync': the head bobs with the loudness
        if STOP.is_set():                             # interrupted: hush and look attentive
            if pr:
                try: pr.terminate()
                except Exception: pass
            A.to(n=-0.6,h=0.25,stiff=2); A.wait(0.3); return False
        A.to(n=n0+0.22*e,h=h0+0.08*(1 if (i//6)%2 else -1)*e,stiff=2.4); A.wait(0.05)
    if pr:
        try: pr.wait(timeout=2)
        except Exception: pass
    pose(A,emotion,1.2); A.wait(0.2); return True
def listen():
    srv=ThreadingHTTPServer(('0.0.0.0',PORT),_H); threading.Thread(target=srv.serve_forever,daemon=True).start()
    try: ip=socket.gethostbyname(socket.gethostname()+'.local')
    except Exception: ip='?'
    print(f'\n  LISTEN MODE on port {PORT}. Robot address: raspberrypi.local (or {ip})')
    print('  Start the companion on your Mac. Ctrl+C to stop.\n',flush=True)
    A=Actor(); last=time.time(); A.idle_amt=0.8
    try:
        while True:
            try: item=Q.get(timeout=0.05)
            except queue.Empty:
                if LOOK['seen'] and time.time()-LOOK['t']<1.5:     # someone is in view: look at them
                    if LOOK.get('b') is not None:                       # Mac already worked out the angle
                        A.to(b=max(-0.9,min(0.9,LOOK['b'])),n=max(-1,min(-0.3,LOOK['n'] if LOOK.get('n') is not None else REST[2])),stiff=1.1)
                    else:
                        A.to(b=max(-0.85,min(0.85,(LOOK['x']-0.5)*GAZE['span']*GAZE['flip'])),
                             n=max(-1,min(-0.3,REST[2]+(0.5-LOOK['y'])*0.5)),stiff=1.1)
                    last=time.time()
                elif time.time()-last>random.uniform(5,9):
                    last=time.time(); A.to(b=random.uniform(-0.4,0.4),h=random.uniform(-0.2,0.2),stiff=1.2)
                A.wait(0.05); continue
            BUSY[0]=True; kind=item[0]; res=None
            if kind=='hear': res=react(A,item[1])
            elif kind=='arrived': A.sfx('hello'); pose(A,'happy'); act(A,'wave'); A.rest().wait(0.4)
            elif kind=='emote':
                _,emo,action=item; c=EMO.get(emo,EMO['neutral'])[1]
                if c: A.sfx(c)
                pose(A,emo); A.wait(0.6); res=act(A,action)
            elif kind=='say':
                _,text,emo,action=item
                import sounds; p,_on=sounds.speak_wav(text,emo)
                if talk(A,p,emo,text) is not False: res=act(A,action)
            elif kind=='speak':
                _,p,emo,action,text=item
                if talk(A,p,emo,text) is not False: res=act(A,action)
                try: os.remove(p)
                except Exception: pass
            if res=='sleep':
                print('  (asleep; say "Hey BITCH" or "wake up" to wake it)',flush=True); BUSY[0]=False
                while True:
                    it=Q.get()
                    if it[0] in ('hear','say','speak') and has(str(it[-1] if it[0]=='speak' else it[1]).lower(),'wake','morning','bitch'): break
                    if it[0]=='emote' and it[2]=='wake': break
                A=Actor(); A.idle_amt=0.8
            elif res=='show': A=Actor(); A.idle_amt=0.8
            last=time.time(); BUSY[0]=False
    except KeyboardInterrupt: pass
    finally: A.end()
if __name__=='__main__': listen()
