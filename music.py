# Desk Creature background music: original tracks synthesized on the Pi (no downloads,
# no copyrighted songs). Rendered once and cached, then played under the shows.
import math, random, os, wave, struct, shutil, subprocess, atexit, json, hashlib
SR=22050
VOL=0.45     # music level under the voice (0-1)
try: VOL=float(json.load(open(os.path.expanduser('~/voice.json'))).get('MUSIC',VOL))
except Exception: pass
DIR=os.path.join(os.path.expanduser('~/.desk_music'),'m1')
def mtof(m): return 440*2**((m-69)/12)
def _n(sec): return int(SR*sec)
def _mix(buf,x,at,g=1.0):
    if at+len(x)>len(buf): buf.extend([0.0]*(at+len(x)-len(buf)))
    for i,v in enumerate(x): buf[at+i]+=v*g
# ---- instruments ----
def kick(d=0.32):
    o=[];ph=0.0
    for k in range(_n(d)):
        t=k/SR; f=45+110*math.exp(-t*28); ph+=2*math.pi*f/SR; o.append(math.sin(ph)*math.exp(-t*9))
    return o
def snare(d=0.2):
    return [(0.65*random.uniform(-1,1)+0.35*math.sin(2*math.pi*190*k/SR))*math.exp(-k/SR*22) for k in range(_n(d))]
def hat(d=0.05,open_=False):
    lp=0.0; o=[]
    for k in range(_n(d*(3 if open_ else 1))):
        x=random.uniform(-1,1); lp+=0.6*(x-lp); o.append((x-lp)*math.exp(-k/SR*(18 if open_ else 70)))
    return o
def bass(m,d):
    f=mtof(m); o=[]; ph=0.0; lp=0.0
    for k in range(_n(d)):
        ph+=f/SR; ph-=int(ph); x=2*ph-1; cut=0.08+0.25*math.exp(-k/SR*12); lp+=cut*(x-lp)
        o.append(lp*min(1,k/60)*min(1,(_n(d)-k)/200))
    return o
def stab(ms,d,bright=0.5):
    o=[0.0]*_n(d)
    for m in ms:
        f=mtof(m); ph=0.0; lp=0.0
        for k in range(len(o)):
            ph+=f/SR; ph-=int(ph); x=2*ph-1; lp+=(0.05+bright*0.3*math.exp(-k/SR*8))*(x-lp)
            o[k]+=lp*math.exp(-k/SR*3)*min(1,k/80)/len(ms)
    return o
def pluck(m,d=0.6,b=0.996):
    """Karplus-Strong: plucked string / music-box-ish"""
    f=mtof(m); N=max(2,int(SR/f)); buf=[random.uniform(-1,1) for _ in range(N)]; o=[]
    for k in range(_n(d)):
        v=buf[k%N]; nxt=buf[(k+1)%N]; buf[k%N]=b*0.5*(v+nxt); o.append(v)
    return o
def bell(m,d=1.2):
    f=mtof(m); return [math.sin(2*math.pi*f*k/SR+2.2*math.exp(-k/SR*3)*math.sin(2*math.pi*f*3.5*k/SR))*math.exp(-k/SR*3.2) for k in range(_n(d))]
def pad(ms,d):
    o=[0.0]*_n(d)
    for m in ms:
        for det in (0.997,1.003):
            f=mtof(m)*det
            for k in range(len(o)): o[k]+=math.sin(2*math.pi*f*k/SR)*0.5/len(ms)
    n=len(o); return [v*min(1,k/(SR*0.8),(n-k)/(SR*0.8)) for k,v in enumerate(o)]
# ---- tracks ----
def t_dance(bpm=112,bars=11):
    b=60/bpm/4; bar=_n(b*16)
    K,S,H,HO=kick(),snare(),hat(),hat(open_=True)
    prog=[(45,[57,60,64]),(41,[53,57,60]),(43,[55,59,62]),(40,[52,55,59])]   # Am F G Em
    bl=[0,0,12,0, 0,10,0,7, 0,0,12,0, 3,0,5,7]
    def make(i,parts):
        o=[0.0]*bar; root,ch=prog[i%4]
        for s in range(16):
            at=_n(b*s)
            if 'k' in parts and s%4==0: _mix(o,K,at,0.9)
            if 's' in parts and s in (4,12): _mix(o,S,at,0.45)
            if 'h' in parts: _mix(o,HO if s%4==2 else H,at,0.22 if s%2 else 0.12)
            if 'b' in parts and s%2==0: _mix(o,bass(root+bl[s]-12,b*1.6),at,0.5)
            if 'c' in parts and s in (3,6,11,14): _mix(o,stab([m+12 for m in ch],b*1.5),at,0.35)
        return o[:bar]
    plan=['kh','khb','khbs','khbs','khbsc','khbsc','khbsc','khbsc','hc','hcb','khbsc','khbsc','khbsc','khbsc'][:bars]
    out=[]; cache={}
    for i,p in enumerate(plan):
        key=(i%4,p)
        if key not in cache: cache[key]=make(i,p)
        out+=cache[key]
    _mix(out,stab([57,60,64,69],1.5,0.8),len(out),0.5)
    return out
def t_spooky(secs=20):
    out=pad([33,40,46],secs)                    # low uneasy drone
    hb=kick(0.25)
    for k in range(int(secs/0.9)):              # heartbeat
        _mix(out,hb,_n(k*0.9),0.45); _mix(out,hb,_n(k*0.9+0.22),0.3)
    for t,m in ((2,70),(6,69),(9.5,75),(13,74),(16.5,66)):
        if t<secs: _mix(out,bell(m,2.5),_n(t),0.18)
    return out[:_n(secs)]
def t_lullaby(secs=24):
    mel=[76,79,84,83,79,76,74,76, 72,76,79,77,76,74,72,74, 76,79,84,86,84,83,79,76, 77,76,74,72,74,76,72,72]
    out=[0.0]*_n(secs); step=0.55
    for i in range(int(secs/step)):
        _mix(out,bell(mel[i%len(mel)],1.4),_n(i*step),0.25)
        if i%4==0: _mix(out,pluck([48,53,55,48][(i//4)%4],1.8,0.998),_n(i*step),0.2)
    return out[:_n(secs)]
def t_playful(secs=16):
    mel=[72,0,76,79, 77,0,74,0, 72,0,76,79, 84,0,79,0, 81,0,79,77, 76,0,74,0, 72,74,76,72, 79,0,0,0]
    out=[0.0]*_n(secs); step=0.17
    for i in range(int(secs/step)):
        m=mel[i%len(mel)]
        if m: _mix(out,pluck(m,0.4,0.99),_n(i*step),0.35)
        if i%4==0: _mix(out,pluck([48,43,45,41][(i//8)%4],0.6,0.993),_n(i*step),0.3)
        if i%2==1: _mix(out,hat(0.03),_n(i*step),0.06)
    return out[:_n(secs)]
def t_fanfare():
    out=[]
    for at,ch,d in ((0,[60,64,67],0.25),(0.28,[62,65,69],0.25),(0.56,[64,67,72,76],1.4)):
        _mix(out,stab(ch+[c+12 for c in ch],d+0.6,0.9),_n(at),0.6)
    _mix(out,kick(),0,0.6); _mix(out,kick(),_n(0.56),0.7)
    for i,m in enumerate((84,88,91,96)): _mix(out,bell(m,1.0),_n(0.6+i*0.07),0.12)
    return out
def t_ambient(secs=30):
    out=[0.0]*_n(secs)
    for i,ch in enumerate(([57,64,67],[53,60,64],[55,62,67],[52,59,64])*3):
        at=i*3.0
        if at<secs: _mix(out,pad(ch,3.6),_n(at),0.35)
    for i in range(int(secs/1.5)):
        if random.random()<0.6: _mix(out,bell(random.choice([76,79,81,83,84,88]),1.5),_n(i*1.5+random.uniform(0,0.3)),0.07)
    return out[:_n(secs)]
TRACKS={'dance':t_dance,'spooky':t_spooky,'lullaby':t_lullaby,'playful':t_playful,'fanfare':t_fanfare,'ambient':t_ambient}
def _path(n): return os.path.join(DIR,n+'.wav')
def render(name):
    os.makedirs(DIR,exist_ok=True); p=_path(name)
    if not os.path.exists(p):
        random.seed(name); s=TRACKS[name]()
        pk=max(1e-6,max(abs(x) for x in s)); g=0.9/pk
        with wave.open(p+'.tmp','wb') as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
            w.writeframes(b''.join(struct.pack('<h',int(math.tanh(x*g*1.2)*29000)) for x in s))
        os.replace(p+'.tmp',p)
    return p
_PLAYER=shutil.which('paplay') or shutil.which('aplay'); _cur=[None]
def play(name):
    stop()
    if not ON or name not in TRACKS or not _PLAYER: return
    p=render(name)
    args=[_PLAYER,p]
    if _PLAYER.endswith('paplay'): args=[_PLAYER,'--volume',str(int(65536*VOL)),p]
    try: _cur[0]=subprocess.Popen(args,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    except Exception: pass
def stop():
    if _cur[0] and _cur[0].poll() is None:
        try: _cur[0].terminate()
        except Exception: pass
    _cur[0]=None
atexit.register(stop)
ON=True
def build():
    import time
    for n in TRACKS:
        t=time.time(); print('  music:',n,flush=True); render(n); print(f'    {time.time()-t:.0f}s',flush=True)
