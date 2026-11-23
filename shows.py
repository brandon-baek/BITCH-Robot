from desk import *
def idle():
    wakeup(); say('Idle: breathing and glancing (40s)')
    drift(40,lambda t:[0.35*sin(t*0.28),0.22*sin(t*0.6),0.25*sin(t*0.6+0.5)+0.1*sin(t*0.23),0.3*sin(t*0.35+2)]); finish()
def fly():
    wakeup(); say('Watching a fly (25s)')
    drift(25,lambda t:[0.4*sin(t*0.9),0.15*sin(t*1.3),0.3*sin(t*1.8+1),0.35*sin(t*1.1)]); finish()
def dance():
    wakeup(); say('Little groove (25s)')
    drift(25,lambda t:[0.3*sin(t*1.6),0.25*abs(sin(t*3.2)),0.3*sin(t*3.2+0.6),0.45*sin(t*1.6+1.57)]); finish()
def curious():
    wakeup()
    say('Notices something to the left'); go(P(b=0.4),1.6); relax(0); sleep(1.2)
    say('Slow curious head tilt'); go(P(h=0.6),1.4); relax(3); sleep(1.5)
    say('Leans in close'); go(P(a=-0.3,n=-0.45),2.2); sleep(1.5)
    say('Tilts the other way'); go(P(h=-0.5),1.6); relax(3); sleep(1.2)
    say('Gentle nods')
    for _ in range(2): go(P(n=-0.25),0.8); go(P(n=-0.5),0.8)
    say('Back to center, content'); go(P(b=0,a=-0.8,n=-0.8,h=0),2.5); finish()
def graceful():
    wakeup(); sleep(0.8)
    say('1. Slow, deep breaths'); drift(9,lambda t:[0,0.25*sin(t*0.7),0.28*sin(t*0.7+0.4),0])
    say('2. Gazing across the room'); go(P(b=-0.45,h=-0.15),3.5); sleep(0.4); go(P(b=0.45,h=0.15),5.5); sleep(0.4); go(P(b=0,h=0),3)
    say('3. Leans in, curious'); go(P(a=-0.35,n=-0.5),3); go(P(h=0.5),1.6); sleep(0.6); go(P(h=-0.4),2); sleep(0.4); go(P(h=0),1.2)
    say('4. Floating like underwater'); drift(14,lambda t:[0.35*sin(t*0.45),0.18*sin(t*0.6),0.22*sin(t*0.75+1),0.35*sin(t*0.55+2)])
    say('5. Graceful nod hello'); go(P(n=-0.2),1.5); go(P(n=-0.55),1.5)
    say('6. Swaying to a quiet song'); drift(12,lambda t:[0.25*sin(t*1.0),0.12*abs(sin(t*2.0)),0.15*sin(t*2.0+0.5),0.4*sin(t*1.0+1.57)])
    say('7. Settling back to rest'); go(REST,4); sleep(0.8); relax(); print('\n*** fin ***')
def alive():
    st={'T':0.0,'W':0.0}; pose=REST[:]
    def off(t): return [0.12*sin(t*0.31)+0.06*sin(t*0.13),0.10*sin(t*0.85),0.12*sin(t*0.85+0.6)+0.05*sin(t*0.27),0.12*sin(t*0.23+1)+0.05*sin(t*0.61)]
    def tick(wt=None):
        st['T']+=0.02
        if wt is not None: st['W']+=(wt-st['W'])*0.02
        o=off(st['T'])
        for i in range(4): cur[i]=max(-1,min(1,pose[i]+o[i]*st['W'])); S[i].value=cur[i]
        sleep(0.02)
    def move(t,d,wt=None):
        s0=pose[:]; n=max(1,int(d/0.02))
        for k in range(1,n+1):
            f=k/n; f=f*f*(3-2*f)
            for i in range(4): pose[i]=s0[i]+(t[i]-s0[i])*f
            tick(wt)
    def live(d,wt=None):
        for _ in range(int(d/0.02)): tick(wt)
    def Q(b=None,a=None,n=None,h=None):
        return [pose[0] if b is None else b, pose[1] if a is None else a, pose[2] if n is None else n, pose[3] if h is None else h]
    wakeup(); sleep(0.5); power_all()
    say('1. Coming to life...'); live(5,1.0)
    say('2. Glances left, curious'); move(Q(b=0.4,h=0.25),2.5); live(3)
    say('3. Leans in for a closer look'); move(Q(a=-0.35,n=-0.5),2.5); live(2.5)
    say('4. Head tilt: huh?'); move(Q(h=-0.45),1.4); live(2.5)
    say('5. Pulls back, thinking'); move(Q(b=0.1,a=-0.8,n=-0.85,h=0.1),2.5); live(4)
    say('6. Glances right'); move(Q(b=-0.45,h=-0.2),3); live(3)
    say('7. Happy little nods')
    for _ in range(2): move(Q(n=-0.5),0.6); move(Q(n=-0.8),0.6)
    live(2)
    say('8. Content, just existing'); move(Q(b=0,a=-0.8,n=-0.8,h=0),3); live(8)
    say('9. Winding down'); live(4,0.0); move(REST,2,0.0)
    relax(); print('\n*** fin ***')
def story(side=0.95):
    wakeup(); relax(0,3)
    say('1. Hears something to its left'); sleep(1.5); go(P(b=-0.08),0.3); go(P(b=0.45),0.9); relax(0); sleep(1.5)
    say('2. Big curious head tilt'); go(P(h=-0.15),0.25); go(P(h=0.75),0.7); relax(3); sleep(2)
    say('3. Leans way in'); go(P(a=-0.25,n=-0.45),1.8); sleep(1.5)
    say('4. Oh, it is YOU! Pulls back'); go(P(a=-0.85,n=-0.95,h=0),0.4); relax(3); sleep(1.5)
    say('5. Happy nod hello (x3)')
    for _ in range(3): go(P(n=-0.35),0.35); go(P(n=-0.85),0.35)
    sleep(1)
    say('6. Turns back to center'); go(P(b=0.5),0.3); go(P(b=0),1.6); relax(0); sleep(1)
    say('7. Sleepy...'); bedtime(side)
def googly(side=0.95):
    wakeup(); relax(0,3)
    say('1. Wakes with a shake')
    for _ in range(4): go(P(h=0.3),0.1); go(P(h=-0.3),0.1)
    go(P(h=0),0.2); relax(3); sleep(1)
    say('2. Double take!'); go(P(b=0.35),0.6); relax(0); sleep(0.6); go(P(b=0),0.4); sleep(0.3); go(P(b=0.45),0.2); relax(0); sleep(1.5)
    say('3. Suspicious tilt'); go(P(h=0.6),1.5); relax(3); sleep(1.5); go(P(h=-0.6),0.3); relax(3); sleep(1.2)
    say('4. Gets DIZZY'); go(P(h=0),0.3)
    for _ in range(3): go(P(b=0.8),0.5); go(P(b=-0.8),0.5)
    go(P(b=0),0.4)
    for _ in range(3): go(P(h=0.35,n=-0.6),0.3); go(P(h=-0.35,n=-0.9),0.3)
    go(P(h=0,n=-0.8),0.4); relax(0); sleep(1)
    say('5. Shakes it off')
    for _ in range(5): go(P(h=0.4),0.08); go(P(h=-0.4),0.08)
    go(P(h=0),0.15); relax(3); sleep(1)
    say('6. Sneezes!'); go(P(a=-0.95,n=-1.0),1.2); sleep(0.4); go(P(a=-0.5,n=-0.4),0.3); sleep(0.3); go(P(a=-0.8,n=-0.8),0.8); sleep(1)
    say('7. Proud bow'); go(P(a=-0.55,n=-0.35),1.2); sleep(0.8); go(P(a=-0.8,n=-0.8),1.2)
    say('8. Off to sleep'); bedtime(side)

def lively(side=0.95):
    wakeup(); sleep(0.3)
    say('1. Startled awake!'); go(P(a=-0.6,n=-0.55),0.35,'bouncy'); go(P(h=0.25),0.25,'bouncy'); go(P(h=-0.2),0.3,'bouncy'); go(P(h=0),0.4,'bouncy'); sleep(0.4)
    say('2. Quick look around'); go(P(b=0.45),0.5,'bouncy'); sleep(0.5); go(P(b=-0.45),0.7,'bouncy'); sleep(0.5); go(P(b=0),0.5,'bouncy'); sleep(0.3)
    say('3. Double take!'); go(P(b=0.3),0.6); go(P(b=0.05),0.25,'snappy'); sleep(0.25); go(P(b=0.4,h=0.3),0.3,'bouncy'); sleep(0.8)
    say('4. Leans in, super curious'); go(P(a=-0.3,n=-0.5),0.8,'bouncy'); go(P(h=-0.45),0.4,'bouncy'); sleep(0.5); go(P(h=0.45),0.45,'bouncy'); sleep(0.5); go(P(h=0),0.4,'bouncy')
    say('5. Excited nods')
    for _ in range(3): go(P(n=-0.3),0.22,'snappy'); go(P(n=-0.6),0.22,'snappy')
    sleep(0.2)
    say('6. Happy wiggle')
    for _ in range(4): go(P(b=0.2,h=0.3),0.18,'snappy'); go(P(b=-0.2,h=-0.3),0.18,'snappy')
    go(P(b=0,h=0),0.3,'bouncy'); sleep(0.4)
    say('7. Big slow yawn (contrast!)'); go(P(a=-0.95,n=-0.95,h=0.25),2.5); sleep(0.6); go(P(a=-0.8,n=-0.8,h=0),1.5); sleep(0.3)
    say('8. One more quick glance...'); go(P(b=-0.35),0.35,'bouncy'); sleep(0.6); go(P(b=0),0.6,'bouncy')
    say('9. Bedtime'); bedtime(side)

def life(secs=75, seed=None):
    """Spring-driven character engine: eyes lead, body follows, nothing ever freezes."""
    import random
    R=random.Random(seed)
    wakeup(); power_all()
    x=cur[:]; v=[0.0]*4; tgt=REST[:]
    K=[55.0,30.0,65.0,95.0]   # stiffness: base, arm, neck, head
    C=[11.0,10.0,11.5,10.0]   # damping (lower = more overshoot / follow-through)
    LO=[-0.9,-0.95,-1.0,-0.8]; HI=[0.9,-0.15,-0.1,0.8]
    ev=[]; st={'T':0.0,'soft':1.0}
    def at(dt,**j):
        for k,val in j.items(): ev.append((st['T']+dt,'banh'.index(k[0]) if k!='soft' else -1,val))
    def glance():
        d=R.choice([-1,1])*R.uniform(0.25,0.6)
        at(0.0,h=d*0.35, n=tgt[2]+0.08)           # eyes/head lead
        at(0.12,b=d)                              # body follows
        at(0.45,h=d*0.15, n=REST[2])              # head settles
        say('glances '+('left' if d>0 else 'right')); return R.uniform(1.8,3.5)
    def saccades():
        say('scanning the room'); t=0; b=tgt[0]
        for _ in range(R.randint(2,4)):
            b=max(-0.7,min(0.7,b+R.uniform(-0.35,0.35))); at(t,b=b,h=R.uniform(-0.2,0.2)); t+=R.uniform(0.35,0.8)
        return t+0.6
    def curious():
        s=R.choice([-1,1]); say('something interesting... leans in')
        at(0.0,n=-0.55); at(0.15,a=-0.35); at(0.9,h=0.45*s); at(2.0,h=-0.4*s); at(3.0,h=0.1*s)
        at(3.8,a=REST[1],n=REST[2],h=0); return 4.6
    def nod():
        say('nods'); t=0
        for _ in range(R.randint(2,3)): at(t,n=-0.45); at(t+0.22,n=-0.85); t+=0.45
        at(t,n=REST[2]); return t+0.6
    def puzzled():
        s=R.choice([-1,1]); say('huh?'); at(0,h=0.6*s, n=-0.65); at(1.6,h=0,n=REST[2]); return 2.3
    def yawn():
        say('big yawn and stretch'); at(0,soft=0.35)
        at(0,a=-0.95,n=-1.0,h=0.2); at(1.8,a=-0.6,n=-0.5,h=-0.15); at(3.0,a=REST[1],n=REST[2],h=0); at(3.8,soft=1.0); return 4.4
    def startle():
        say('whoa! startled'); at(0,a=-0.95,n=-0.95,h=R.uniform(-0.3,0.3)); at(0.6,a=REST[1],n=REST[2],h=0); return 1.6
    def fidget():
        say('fidgets'); at(0,b=tgt[0]+R.uniform(-0.12,0.12),h=R.uniform(-0.15,0.15)); return R.uniform(0.8,1.6)
    def center_up():
        say('back to center'); at(0,b=0,h=0); at(0.2,a=REST[1],n=REST[2]); return 1.5
    B=[(glance,5),(saccades,3),(curious,2),(nod,2),(puzzled,2),(yawn,1),(startle,1),(fidget,3),(center_up,2)]
    pool=[f for f,w in B for _ in range(w)]
    nxt=0.8; dt=0.02; last=None
    while st['T']<secs:
        T=st['T']
        if T>=nxt:
            f=R.choice(pool)
            while f is last: f=R.choice(pool)
            last=f; nxt=T+f()+R.uniform(0.3,1.5)
        for e in [e for e in ev if e[0]<=T]:
            ev.remove(e)
            if e[1]==-1: st['soft']=e[2]
            else: tgt[e[1]]=e[2]
        idle=[0.05*sin(T*0.37)+0.03*sin(T*1.13),
              0.07*sin(T*1.05),                       # breathing
              0.06*sin(T*1.05+0.7)+0.03*sin(T*0.29),
              0.06*sin(T*0.43+1)+0.03*sin(T*1.7)]
        for i in range(4):
            k=K[i]*st['soft']
            a=k*(tgt[i]+idle[i]-x[i])-C[i]*(st['soft']**0.5)*v[i]
            v[i]+=a*dt; v[i]=max(-4,min(4,v[i])); x[i]+=v[i]*dt
            x[i]=max(LO[i],min(HI[i],x[i])); cur[i]=x[i]; S[i].value=x[i]
        sleep(dt); st['T']+=dt
    say('settling down'); finish()

# ================= CROWD PLEASERS =================
def hello():
    say('MEET DESK CREATURE'); A=Actor()
    say('...is someone there?'); A.to(n=-0.65).wait(0.6).to(b=0.35,h=0.2).wait(1.2).to(b=-0.35,h=-0.2).wait(1.4)
    say('Oh! Hi!!'); A.to(b=0,h=0,a=-0.55,n=-0.5,stiff=1.6).wait(0.6)
    say('waves hello'); A.shake('h',0.45,2.2,1.6).wait(1.8)
    say('takes a bow'); A.to(a=-0.3,n=-0.15,h=0,stiff=0.7).wait(1.4).to(a=-0.8,n=-0.8).wait(1.2)
    say('a little shy...'); A.to(b=0.45,h=0.35,n=-0.95,stiff=0.8).wait(1.6)
    say('...peeks back'); A.to(b=0.2,h=0.15,n=-0.75).wait(0.9).to(b=0,h=0,n=-0.6,stiff=1.4).wait(0.6)
    say('happy wiggle!'); A.shake('h',0.35,3,1.2).shake('b',0.15,3,1.2).wait(1.5)
    A.end()

def disco(bpm=112):
    say('DANCE PARTY'); A=Actor(); beat=60/bpm
    say('getting into the groove'); A.idle_amt=0.3
    for k in range(8): A.to(n=-0.55 if k%2 else -0.85,stiff=2).wait(beat)
    say('side to side')
    for k in range(8): s=1 if k%2 else -1; A.to(b=0.3*s,h=-0.3*s,stiff=1.8).wait(beat)
    say('the robot (snap!)')
    poses=[(0.4,-0.5,-0.5,0.4),(-0.4,-0.5,-0.5,-0.4),(0.4,-0.85,-0.9,-0.3),(-0.4,-0.85,-0.9,0.3)]
    for k in range(8): b,a,n,h=poses[k%4]; A.to(b,a,n,h,stiff=3.5).wait(beat)
    say('head bang!')
    A.to(b=0,a=-0.6,h=0,stiff=2).wait(beat)
    for k in range(8): A.to(n=-0.3 if k%2 else -0.9,stiff=3).wait(beat/2)
    say('spin!'); A.to(b=0.85,stiff=1.2).wait(beat*2).to(b=-0.85).wait(beat*3).to(b=0).wait(beat*2)
    say('...and FREEZE!'); A.to(b=0.25,a=-0.4,n=-0.3,h=0.55,stiff=4).wait(0.2); A.idle_amt=0; A.wait(2.2); A.idle_amt=1
    A.end()

def peekaboo():
    say('PEEKABOO'); A=Actor()
    say('hides!'); A.to(b=0.85,a=-0.95,n=-1.0,h=0.3,stiff=1.3).wait(1.8)
    for k in range(2):
        say('...slowly peeks'); A.to(b=0.5,n=-0.8,h=0.15,stiff=0.5).wait(1.6)
        say('nope! hides again'); A.to(b=0.85,n=-1.0,h=0.3,stiff=2.2).wait(1.4)
    say('...'); A.wait(1.0)
    say('BOO!!'); A.to(b=0,a=-0.4,n=-0.35,h=0,stiff=3.5).wait(0.5).shake('h',0.3,4,0.6).wait(1.0)
    say('giggles'); A.shake('n',0.12,5,1.2).wait(1.5)
    A.end()

def spooked():
    say('SOMETHING IS BEHIND YOU...'); A=Actor()
    say('hears a noise...'); A.idle_amt=0.4; A.to(h=0.2,n=-0.7,stiff=0.6).wait(1.5)
    say('...slowly turns to look'); A.to(b=0.75,h=0.1,stiff=0.35).wait(3.0)
    say('AAAH!'); A.to(b=0.2,a=-0.95,n=-1.0,h=-0.3,stiff=4).wait(0.4)
    say('trembling'); A.shake('h',0.12,9,2.2).shake('n',0.06,8,2.2).wait(2.4)
    say('hides'); A.to(b=-0.7,n=-1.0,h=-0.3,stiff=1).wait(1.6)
    say('...peeks back very carefully'); A.to(b=-0.2,n=-0.8,h=0.1,stiff=0.3).wait(2.4)
    say('phew... nothing there'); A.idle_amt=1; A.to(b=0,a=-0.6,n=-0.5,h=0,stiff=0.7).wait(1.2).to(a=-0.85,n=-0.85).wait(1.5)
    A.end()

def sleepy():
    say('SO... SLEEPY...'); A=Actor()
    say('big yawn'); A.to(a=-0.95,n=-1.0,h=0.25,stiff=0.35).wait(2.5).rest(stiff=0.6).wait(1.5)
    for k,depth in enumerate((-0.45,-0.3,-0.15)):
        say('nodding off...'); A.to(a=-0.6,n=depth,h=0.25,stiff=0.25).wait(2.6)
        say('*snaps awake*'); A.to(a=-0.85,n=-0.9,h=0,stiff=4).wait(0.35).shake('h',0.25,4,0.5).wait(0.8)
        A.rest(stiff=0.6).wait(0.8)
    say('gives up...'); A.end(bed=True)

def catch_fly():
    say('THERE IS A FLY'); A=Actor(); import random; R=random.Random()
    say('tracking it...'); A.idle_amt=0.3
    for _ in range(7):
        A.to(b=R.uniform(-0.6,0.6),n=R.uniform(-0.8,-0.4),h=R.uniform(-0.3,0.3),stiff=R.choice([1.2,2.5])).wait(R.uniform(0.3,0.7))
    say('...it landed. waiting.'); A.to(b=0.35,n=-0.6,h=0.1,stiff=0.6).wait(2.0)
    say('POUNCE!'); A.to(a=-0.25,n=-0.25,stiff=4).wait(0.6)
    say('missed it...'); A.idle_amt=1; A.to(a=-0.7,n=-0.95,h=-0.3,stiff=0.4).wait(2)
    say('*grumble*'); A.shake('b',0.1,3,1).wait(1.2)
    A.end()

def emotions():
    say('A RANGE OF FEELINGS'); A=Actor()
    say('happy!'); A.to(a=-0.5,n=-0.45,stiff=1.5).wait(0.5).shake('h',0.3,2.5,1.4).wait(1.6)
    say('sad...'); A.to(b=-0.15,a=-0.9,n=-1.0,h=0.35,stiff=0.3).wait(3)
    say('curious?'); A.to(b=0.2,a=-0.4,n=-0.55,h=-0.5,stiff=0.8).wait(2)
    say('angry!!'); A.to(b=0,a=-0.45,n=-0.95,h=0,stiff=3).wait(0.4).shake('b',0.12,6,1.2).wait(1.4)
    say('proud'); A.to(a=-0.95,n=-0.35,h=0,stiff=0.7).wait(2)
    say('in love <3'); A.to(a=-0.55,n=-0.6,h=0.35,stiff=0.5).wait(1).to(h=-0.35).wait(1.2).to(h=0.3).wait(1.2)
    A.end()

def showtime():
    say('*** SHOWTIME: THE FULL SHOW ***')
    hello(); disco(); peekaboo(); spooked(); emotions(); sleepy()
