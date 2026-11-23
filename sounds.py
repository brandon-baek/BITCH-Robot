# Desk Creature voice: R2-D2 / WALL-E style chirps, built fresh with a little randomness
# so the robot never says the exact same thing twice. No downloads needed.
import math, wave, struct, os, random, shutil, subprocess, threading, json, hashlib
SR=22050
# --- style knobs (overridden by ~/voice.json) ---
PITCH=1.0    # overall pitch
SPEED=1.0    # >1 = faster, chattier
BRIGHT=0.12  # 0 = pure whistle, 0.6 = buzzy/electronic
VARIANTS=3
CHATTER=0.35  # background 'busy robot' sounds: 0 = off   # different takes kept per sound
try:
    _c=json.load(open(os.path.expanduser('~/voice.json')))
    if 'CHATTER' in _c: CHATTER=float(_c['CHATTER'])
    for _k in ('PITCH','SPEED','BRIGHT'):
        if _k in _c: globals()[_k]=float(_c[_k])
except Exception: pass
DIR=os.path.join(os.path.expanduser('~/.desk_sounds'),hashlib.md5(json.dumps([PITCH,SPEED,BRIGHT,'r2v5']).encode()).hexdigest()[:8])

# ---------- primitives ----------
def _osc(ph):
    return (1-BRIGHT)*math.sin(ph)+BRIGHT*math.tanh(4*math.sin(ph))
def tone(f0,f1,d,vol=0.5,vib=0.0,vrate=0.0,curve=1.0):
    """pitch glide f0->f1 (curve>1 = slow start/fast end), optional vibrato"""
    d/=SPEED; n=max(1,int(SR*d)); out=[]; ph=0.0
    for k in range(n):
        t=(k/n)**curve
        f=f0*(f1/f0)**t*PITCH
        if vib: f*=1+vib*math.sin(2*math.pi*vrate*k/SR)
        ph+=2*math.pi*f/SR
        env=min(1,k/(0.004*SR),(n-k)/(0.012*SR))
        out.append(vol*env*_osc(ph))
    return out
def warble(fa,fb,rate,d,vol=0.45):
    """R2-style rapid two-pitch flutter"""
    d/=SPEED; n=int(SR*d); out=[]; ph=0.0
    for k in range(n):
        m=0.5+0.5*math.tanh(3*math.sin(2*math.pi*rate*k/SR))
        f=(fa+(fb-fa)*m)*PITCH; ph+=2*math.pi*f/SR
        env=min(1,k/(0.005*SR),(n-k)/(0.015*SR)); out.append(vol*env*_osc(ph))
    return out
def gap(d): return [0.0]*int(SR*d/SPEED)
def blip(f,d=0.05,vol=0.45): return tone(f,f*1.04,d,vol)
def whir(f0,f1,d,vol=0.22):
    """little motor / processing whir"""
    n=int(SR*d); out=[]; ph=0.0
    for k in range(n):
        t=k/n; f=f0+(f1-f0)*t; ph+=2*math.pi*f/SR
        saw=((ph/(2*math.pi))%1)*2-1
        env=min(1,k/(0.02*SR),(n-k)/(0.03*SR)); out.append(vol*env*(0.7*saw+0.3*random.uniform(-1,1)))
    return out
def click(vol=0.35): return [vol*random.uniform(-1,1)*(1-k/90) for k in range(90)]
def chatter(n,lo,hi,each=0.035):
    out=[]
    for _ in range(n): out+=blip(random.uniform(lo,hi),each*random.uniform(0.7,1.3),0.35)+gap(0.012)
    return out
def J(x,p=0.1): return x*random.uniform(1-p,1+p)   # jitter for variety

# ---------- speech-like phrases ----------
# A phrase is a string of connected syllables, like talking: pitch never sits still,
# syllables slur into each other, and intervals are speech-like, not musical notes.
def say_(sylls,vol=0.5,breath=0.04):
    """sylls: list of (seconds, [pitch points...], vibrato_depth). Phase is continuous."""
    out=[]; ph=0.0
    for (d,pts,vib) in sylls:
        d=J(d,0.12)/SPEED; n=max(2,int(SR*d)); m=len(pts)-1
        pts=[J(p,0.06) for p in pts]
        vr=random.uniform(5.5,8.5)
        for k in range(n):
            t=k/(n-1); x=t*m; i=min(int(x),m-1); u=x-i
            u=(1-math.cos(math.pi*u))/2                       # smooth curve between points
            f=(pts[i]*(1-u)+pts[i+1]*u)*PITCH
            f*=1+vib*min(1,t*3)*math.sin(2*math.pi*vr*k/SR)   # vibrato blooms in, like a voice
            ph+=2*math.pi*f/SR
            env=math.sin(math.pi*t)**0.35                     # syllable swell, dips between
            w=_osc(ph)+0.25*math.sin(2*ph+0.3)
            out.append(vol*(0.25+0.75*env)*(w*(1-breath)+breath*random.uniform(-1,1)))
    # fade phrase edges
    for k in range(min(300,len(out))): out[k]*=k/300; out[-1-k]*=k/300
    return out
def chirp(lo,hi,d=0.07,up=None):
    up=random.random()<0.5 if up is None else up
    return say_([(d,[lo,hi,hi*0.94] if up else [hi,lo*1.06,lo],0)],vol=0.42)
def chatter(n,lo,hi,each=0.045):
    o=[]
    for _ in range(n): o+=chirp(random.uniform(lo,hi),random.uniform(lo,hi)*random.uniform(1.1,1.5),each*random.uniform(0.7,1.4))+gap(random.uniform(0.0,0.025))
    return o
def J(x,p=0.1): return x*random.uniform(1-p,1+p)

# ---------- machine-y primitives: sudden jumps, FM twangs, dial-up hopping ----------
def fm(f0,f1,ratio,i0,i1,d,vol=0.45):
    """FM 'twang/bloop': carrier glides f0->f1, metallic sidebands from index i0->i1"""
    d=J(d,0.1)/SPEED; n=max(2,int(SR*d)); o=[]; pc=pm=0.0
    for k in range(n):
        t=k/(n-1); f=f0*(f1/f0)**t*PITCH; I=i0+(i1-i0)*t
        pc+=2*math.pi*f/SR; pm+=2*math.pi*f*ratio/SR
        env=min(1,k/(0.004*SR),(n-k)/(0.015*SR)); o.append(vol*env*math.sin(pc+I*math.sin(pm)))
    return o
def zipp(f0,f1,d=0.05,vol=0.45):
    """very fast sweep - the 'zwip!'"""
    return fm(J(f0),J(f1),1.0,0,0,d,vol)
def hop(n,lo,hi,each=0.03,vol=0.4):
    """dial-up style: rapid random pitch steps"""
    o=[]; ph=0.0
    for _ in range(n):
        f=random.uniform(lo,hi)*PITCH; m=max(2,int(SR*each*random.uniform(0.7,1.3)/SPEED))
        for k in range(m):
            ph+=2*math.pi*f/SR; env=min(1,k/40,(m-k)/40); o.append(vol*env*_osc(ph))
    return o
def bubble(d,lo,hi,rate=30,vol=0.42):
    """R2 burble: pitch wanders fast and randomly"""
    n=int(SR*J(d,0.1)/SPEED); o=[]; ph=0.0; f=random.uniform(lo,hi); tgt=f; step=max(1,int(SR/rate))
    for k in range(n):
        if k%step==0: tgt=random.uniform(lo,hi)
        f+= (tgt-f)*0.004; ph+=2*math.pi*f*PITCH/SR
        env=min(1,k/(0.006*SR),(n-k)/(0.02*SR)); o.append(vol*env*_osc(ph))
    return o

# ---------- organic layer: a tiny 'creature voice' made with vowel resonances ----------
VOW={'a':(800,1250,2700),'e':(520,1900,2700),'i':(330,2350,3100),'o':(520,880,2550),'u':(340,780,2300)}
FSCALE=1.35   # small creature = shorter 'throat' = higher resonances
class _BP:
    def __init__(s): s.x1=s.x2=s.y1=s.y2=0.0; s.b0=s.b2=s.a1=s.a2=0.0
    def set(s,f,q):
        w=2*math.pi*min(f,SR*0.45)/SR; al=math.sin(w)/(2*q); a0=1+al
        s.b0=al/a0; s.b2=-al/a0; s.a1=-2*math.cos(w)/a0; s.a2=(1-al)/a0
    def __call__(s,x):
        y=s.b0*x+s.b2*s.x2-s.a1*s.y1-s.a2*s.y2
        s.x2=s.x1; s.x1=x; s.y2=s.y1; s.y1=y; return y
def vox(sylls,vol=0.55,breath=0.12,grit=0.0):
    """sylls: (seconds, pitch points, vowel string, vibrato). A buzzy throat through moving vowel shapes."""
    out=[]; ph=0.0; F=[_BP(),_BP(),_BP()]; G=(1.0,0.55,0.28); drift=0.0
    for (d,pts,vw,vib) in sylls:
        d=J(d,0.12)/SPEED; n=max(2,int(SR*d)); m=len(pts)-1; pts=[J(p,0.05) for p in pts]
        vs=[VOW[c] for c in vw]; mv=len(vs)-1; vr=random.uniform(5,7.5)
        for k in range(n):
            t=k/(n-1); x=t*m; i=min(int(x),m-1); u=(1-math.cos(math.pi*(x-i)))/2
            drift+=random.uniform(-1,1)*0.0009; drift*=0.995                 # natural pitch wobble
            f=(pts[i]*(1-u)+pts[i+1]*u)*PITCH*(1+drift)*(1+vib*min(1,t*2.5)*math.sin(2*math.pi*vr*k/SR))
            ph+=f/SR; ph-=int(ph)
            src=(2*ph-1)*(1-breath)+breath*random.uniform(-1,1)*1.6             # buzzy throat + air
            if k%32==0:
                y=t*mv; j=min(int(y),mv-1) if mv>0 else 0; w=(y-j) if mv>0 else 0
                for q in range(3):
                    fq=(vs[j][q]*(1-w)+(vs[min(j+1,mv)][q])*w)*FSCALE
                    F[q].set(fq,fq/(70+50*q))
            v=sum(G[q]*F[q](src) for q in range(3))
            env=math.sin(math.pi*t)**0.4
            if grit: v=math.tanh(v*(1+grit*4))
            out.append(vol*1.8*(0.3+0.7*env)*v)
    for k in range(min(300,len(out))): out[k]*=k/300; out[-1-k]*=k/300
    return out
def _fx(s):
    """shared 'speaker + room': soft saturation, gentle tone, tiny room reverb"""
    lp=0.0; o=[]
    for x in s:
        x=math.tanh(1.4*x); lp+=0.55*(x-lp); o.append(lp)
    D=[int(SR*0.031),int(SR*0.043),int(SR*0.057)]; out=o+[0.0]*int(SR*0.25)
    wet=[0.0]*len(out)
    for dl in D:
        buf=[0.0]*len(out)
        for k in range(len(out)):
            buf[k]=(o[k] if k<len(o) else 0.0)+(0.42*buf[k-dl] if k>=dl else 0.0)
            wet[k]+=buf[k]/len(D)
    return [(out[k]+0.16*wet[k]) for k in range(len(out))]

def s_hello():  return zipp(500,1800,0.04,0.35)+gap(0.02)+vox([(0.42,[480,650,950,880],'iii',0.045)],breath=0.15)+bubble(0.12,1800,2700,38,0.3)+zipp(2000,3300,0.05,0.3)
def s_walle():  return vox([(0.26,[290,380,350],'ao',0.02),(0.5,[350,520,700,640],'eii',0.035)])+gap(0.05)+zipp(1600,3000,0.05,0.3)
def s_eva():    return vox([(0.32,[560,700,650],'ie',0.02),(0.46,[620,500,330],'aa',0.03)])
def s_question():return vox([(0.32,[260,240,420,640],'uuu',0.0)],breath=0.2)+fm(800,2600,1.5,2,0,0.18,0.3)
def s_blip():   return zipp(random.choice([800,1500,2500]),random.choice([1200,2200,3400]),0.04)
def s_beepboop():return hop(random.randint(2,4),500,2200,0.08)+gap(0.03)+zipp(1400,600,0.05)
def s_curious():return hop(3,500,1800,0.05)+vox([(0.34,[300,340,560,720],'ouu',0.02)])
def s_happy():  return vox([(0.1,[520,700],'ii',0),(0.1,[600,820],'ee',0),(0.28,[700,1000,900],'ii',0.05)],breath=0.15)+bubble(0.12,1800,2800,40,0.3)+zipp(2000,3400,0.05,0.35)
def s_excited():return bubble(0.25,1000,3000,45)+vox([(0.09,[600,800],'aa',0),(0.09,[650,850],'aa',0),(0.3,[700,1100,1000],'ei',0.06)])
def s_giggle():
    sy=[]; f=J(900)
    for i in range(5): sy.append((0.075,[f,f*1.25,f*1.1],'ii',0)); f*=0.9
    return vox(sy,breath=0.3)
def s_sad():    return vox([(0.35,[420,460,400],'oo',0.02),(0.9,[420,330,240,200],'ouu',0.04)],vol=0.5,breath=0.18)
def s_whimper():return vox([(0.22,[720,780,560],'iu',0.06),(0.28,[680,720,480],'iu',0.06)],vol=0.38,breath=0.25)
def s_sigh():   return vox([(0.8,[420,360,260,220],'aou',0.01)],vol=0.42,breath=0.6)
def s_startle():return vox([(0.16,[400,900,850],'aa',0.03)],breath=0.15)+zipp(1800,3600,0.05,0.4)
def s_scream(): return vox([(0.55,[600,950,1150,1200],'aeee',0.07)],vol=0.55,breath=0.2,grit=0.4)+bubble(0.15,2000,3500,45,0.35)
def s_boo():    return vox([(0.12,[200,260],'uu',0),(0.36,[300,700,640],'ua',0.05)],vol=0.6)+fm(2400,2000,0.5,4,1,0.2,0.35)
def s_pounce(): return click()+vox([(0.14,[350,800,750],'aa',0)],grit=0.3)+zipp(1500,3500,0.06,0.4)
def s_angry():  return vox([(0.14,[170,150],'aa',0.05),(0.14,[180,140],'aa',0.05),(0.3,[190,160,120],'ao',0.08)],vol=0.55,grit=0.8)+fm(260,140,1.41,5,2,0.2,0.35)
def s_grumble():return vox([(0.2,[200,180,190],'ou',0.05),(0.3,[190,160,140],'ou',0.05)],vol=0.5,grit=0.5,breath=0.15)
def s_love():   return vox([(0.3,[380,480,520],'uu',0.03),(0.55,[500,560,500,420],'ouu',0.05)],breath=0.15)
def s_proud():  return hop(2,700,1200,0.06)+vox([(0.15,[350,420],'aa',0),(0.42,[420,560,700,680],'aaa',0.04)])
def s_tada():   return zipp(600,1800,0.05)+vox([(0.12,[420,520],'aa',0),(0.4,[520,760,720],'aa',0.05)])+fm(2000,2600,3.0,2,0,0.2,0.35)
def s_yawn():   return vox([(0.5,[260,380,500],'aaa',0.015),(1.0,[500,380,260,220],'aou',0.02)],vol=0.5,breath=0.3)+whir(200,130,0.3,0.1)
def s_snore():  return vox([(0.7,[110,120,110],'oo',0)],vol=0.4,breath=0.55)+gap(0.2)+whir(110,150,0.5,0.15)
def s_think():  return click()+hop(14,600,2600,0.025,0.32)+whir(260,380,0.25)+fm(500,700,3.7,4,1,0.15,0.3)+click()
def s_scan():   return whir(300,520,0.2)+hop(8,900,2400,0.03,0.33)+click()+zipp(2200,500,0.15,0.35)
def s_powerup():return whir(120,600,0.45,0.18)+zipp(200,3000,0.5,0.45)+vox([(0.3,[300,500,450],'ae',0.02)])
def s_powerdown():return vox([(0.5,[500,300,180],'aou',0.02)],vol=0.45)+fm(1500,80,1.5,3,0,0.6,0.35)+whir(300,80,0.4,0.15)
# quiet background 'machine doing stuff' sounds (used by the chatter loop, not by captions)
def s_tick():    return click(0.25)+gap(0.03)+zipp(random.choice([1800,2400,3000]),random.choice([2200,2800,3400]),0.025,0.3)
def s_process(): return hop(random.randint(4,9),900,2600,0.022,0.3)
def s_whirr():   return whir(random.uniform(180,260),random.uniform(280,420),random.uniform(0.15,0.35),0.25)
def s_idle():    return vox([(0.18,[random.uniform(380,520),random.uniform(450,650)],random.choice(['u','o','i']),0.02)],vol=0.4,breath=0.2)
def s_servo():   return whir(random.uniform(300,380),random.uniform(420,560),random.uniform(0.12,0.22),0.2)
def s_doubleblip():return zipp(1400,2000,0.03,0.3)+gap(0.05)+zipp(1700,2300,0.03,0.3)
V={n[2:]:f for n,f in list(globals().items()) if n.startswith('s_') and callable(f)}

def _path(name,i): return os.path.join(DIR,f'{name}_{i}.wav')
_LOCK=threading.Lock()
def _render(name,i):
  with _LOCK:
    os.makedirs(DIR,exist_ok=True); p=_path(name,i)
    if not os.path.exists(p):
        random.seed(hash((name,i)))
        s=_fx(V[name]())
        pk=max(1e-6,max(abs(x) for x in s)); rms=max(1e-6,math.sqrt(sum(x*x for x in s)/len(s))); g=min(0.95/pk,0.2/rms)
        with wave.open(p+'.tmp','wb') as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
            w.writeframes(b''.join(struct.pack('<h',int(x*g*32000)) for x in s))
        os.replace(p+'.tmp',p)
    return p
_PLAYER=shutil.which('pw-play') or shutil.which('paplay') or shutil.which('aplay')
import time as _t
_last=[0.0]
def play(name,wait=False,vol=1.0):
    if name not in V: return
    _last[0]=_t.time()
    def go():
        p=_render(name,random.randrange(VARIANTS))
        if _PLAYER:
            args=[_PLAYER,p]
            if vol<0.999:
                if _PLAYER.endswith('pw-play'): args=[_PLAYER,'--volume',f'{vol:.2f}',p]
                elif _PLAYER.endswith('paplay'): args=[_PLAYER,'--volume',str(int(65536*vol)),p]
            try:
                pr=subprocess.Popen(args,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                if wait: pr.wait()
            except Exception: pass
    if wait: go()
    else: threading.Thread(target=go,daemon=True).start()
# ---- background chatter: the robot is never fully silent ----
_amb={'on':False}
AMB=[('tick',4),('process',3),('whirr',2),('idle',2),('doubleblip',2),('blip',1)]
def ambient_start():
    if _amb['on'] or CHATTER<=0: return
    _amb['on']=True
    pool=[n for n,w in AMB for _ in range(w)]
    def loop():
        while _amb['on']:
            _t.sleep(random.uniform(0.9,3.2))
            if _amb['on'] and _t.time()-_last[0]>0.9:
                n=random.choice(pool); v=CHATTER*random.uniform(0.55,1.0)
                play(n,vol=v); _last[0]=_t.time()-0.5   # don't block real cues
    threading.Thread(target=loop,daemon=True).start()
def ambient_stop(): _amb['on']=False
def build():
    '''pre-build every sound now (about a minute on the Pi)'''
    import time; t=time.time()
    for n in V:
        print('  building',n,flush=True)
        for i in range(VARIANTS): _render(n,i)
    print(f'voice ready in {time.time()-t:.0f}s')
    try:
        import music; music.build(); print('music ready')
    except Exception as e: print('music skipped:',e)
def warm():
    """pre-build every sound so shows never wait (runs in the background)"""
    def go():
        for n in V:
            for i in range(VARIANTS): _render(n,i)
    threading.Thread(target=go,daemon=True).start()
# caption keyword -> sound (first match wins)
CUES=[('BOO','boo'),('AAAH','scream'),('whoa','startle'),('snaps awake','startle'),('Hi!','hello'),('MEET','hello'),
 ('trembl','whimper'),('phew','sigh'),('yawn','yawn'),('Zzz','snore'),('lying down','powerdown'),('giggle','giggle'),
 ('sad','sad'),('missed','sad'),('angry','angry'),('grumble','grumble'),('love','love'),('POUNCE','pounce'),
 ('proud','proud'),('bow','tada'),('FREEZE','tada'),('DANCE','excited'),('happy','happy'),('wiggle','excited'),('nod','beepboop'),
 ('curious','curious'),('interesting','curious'),('huh','question'),('?','question'),('scanning','scan'),('look around','scan'),('tracking','think'),
 ('glance','blip'),('peeks','blip'),('noise','question'),('hides','whimper'),('waves','hello'),('Startled','startle'),
 ('center, one joint','powerup'),('oozing','beepboop'),('waiting','think'),('settling','sigh')]
def cue(text):
    for k,n in CUES:
        if k.lower() in text.lower(): play(n); return
def demo():
    import time
    for n in V:
        print(n,flush=True); play(n,wait=True); time.sleep(0.4)
