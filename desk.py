from adafruit_servokit import ServoKit
from time import sleep
from math import sin
import time as _time
kit=ServoKit(channels=16)
class _Servo:
    def __init__(s,ch,lo,hi):
        s.c=kit.servo[ch]; s.lo,s.hi=lo,hi; s.wide=False; s.c.set_pulse_width_range(lo,hi); s.c.actuation_range=180
    def us(s,u):
        """set an exact pulse in microseconds (500-2500), beyond the normal range"""
        if not s.wide: s.c.set_pulse_width_range(500,2500); s.wide=True
        s.c.angle=max(0,min(180,(u-500)/2000*180))
    @property
    def value(s):
        a=s.c.angle
        return None if a is None else a/90-1
    @value.setter
    def value(s,v):
        if s.wide: s.c.set_pulse_width_range(s.lo,s.hi); s.wide=False
        s.c.angle=(max(-1,min(1,v))+1)*90
    def detach(s): s.c.angle=None
REST=[0.0,-0.8,-0.8,0.0]
S=[_Servo(0,500,2500)]+[_Servo(i,1000,2000) for i in (1,2,3)]
cur=REST[:]; on=[False]*4
NAMES=['base','arm','neck','head']
import json as _json, os as _os, atexit as _atexit
_PF=_os.path.expanduser('~/.desk_pose.json')
def _save():
    try: _json.dump([round(v,3) for v in cur],open(_PF,'w'))
    except Exception: pass
def _load():
    try:
        p=_json.load(open(_PF)); return p if len(p)==4 else None
    except Exception: return None
_atexit.register(_save)
SPEED=0.65
def _ease(f,e):
    if e=='snappy': return 1-(1-f)**3
    if e=='bouncy':
        c1=1.9; c3=c1+1; return 1+c3*(f-1)**3+c1*(f-1)**2
    return f*f*(3-2*f)
def go(t,d,e='smooth'):
    mv=[abs(t[i]-cur[i])>0.001 for i in range(4)]
    for i in range(4):
        if mv[i] and not on[i]: S[i].value=cur[i]; on[i]=True
    st=cur[:]; n=max(1,int(d*SPEED/0.02)); nxt=_time.monotonic()
    for k in range(1,n+1):
        f=_ease(k/n,e)
        for i in range(4):
            if mv[i]: cur[i]=max(-1,min(1,st[i]+(t[i]-st[i])*f)); S[i].value=cur[i]
        nxt+=0.02; sleep(max(0,nxt-_time.monotonic()))
def relax(*js):
    for i in (js or range(4)): S[i].detach(); on[i]=False
    if not any(on): _save()
def P(b=None,a=None,n=None,h=None):
    return [cur[0] if b is None else b, cur[1] if a is None else a, cur[2] if n is None else n, cur[3] if h is None else h]
def say(x):
    print('\n>> '+x, flush=True)
    if VOICE:
        try:
            import sounds; sounds.cue(x)
        except Exception: pass
VOICE=True
def power_all():
    for i in range(4):
        if not on[i]: S[i].value=cur[i]; on[i]=True
def drift(secs,fn):
    power_all(); b=cur[:]; n=int(secs/0.02)
    for k in range(n):
        t=k*0.02; fade=min(1,k/75,(n-k)/75); off=fn(t)
        for i in range(4): cur[i]=max(-1,min(1,b[i]+off[i]*fade)); S[i].value=cur[i]
        sleep(0.02)
def _one_at_a_time(pose,known=None):
    for i in (1,2,3,0):
        if known and abs(known[i]-pose[i])<0.06:
            S[i].value=pose[i]; on[i]=True; cur[i]=pose[i]; sleep(0.08)      # already there: no wait
            continue
        print('   moving',NAMES[i],flush=True)
        S[i].value=pose[i]; on[i]=True; cur[i]=pose[i]; sleep(0.35)
    relax(0,3)
def center(): say('center, one joint at a time'); _one_at_a_time([0,0,0,0],_load())
def rest(): say('rest, one joint at a time'); _one_at_a_time(REST,_load())
def wakeup():
    known=_load()
    if known:
        say('waking up')
        for i in (1,2,3,0):                                 # power each joint exactly where it was left
            if abs(known[i])>1: S[i].us(1500+500*known[i])
            else: S[i].value=known[i]
            on[i]=True; cur[i]=known[i]; sleep(0.08)
        for i in range(4):                                  # ease back from past the normal range
            if abs(cur[i])>1:
                u0=1500+500*cur[i]; u1=1500+500*(1 if cur[i]>0 else -1)
                for k in range(1,41): S[i].us(u0+(u1-u0)*k/40); sleep(0.03)
                cur[i]=1.0 if cur[i]>0 else -1.0; S[i].value=cur[i]
        go(REST,1.8)                                        # then ooze smoothly into rest
        relax(0,3)
    else:
        center(); say('oozing into rest'); go(REST,2)       # unknown pose: safe route
    chatter(True)
def chatter(on=True):
    if not VOICE: return
    try:
        import sounds
        sounds.ambient_start() if on else sounds.ambient_stop()
    except Exception: pass
def finish(): go(REST,2); sleep(0.5); relax(); chatter(False); music_stop(); print('\n*** done ***')
def sfx(name):
    if VOICE:
        try:
            import sounds; sounds.play(name)
        except Exception: pass
def music(name):
    try:
        import music as _m; _m.play(name)
    except Exception: pass
def music_stop():
    try:
        import music as _m; _m.stop()
    except Exception: pass
def bedtime(side=0.95, arm=1.0, tuck=-0.9, deep=2350):
    """turn to the side and lie all the way down, chin tucked so the face can't snag.
    deep: how far past the normal range the arm keeps lowering (microseconds, 2000 = normal max).
    tuck: neck while lying; if the face sticks OUT instead of tucking in, use tuck=0.9"""
    say('lying down')
    go(P(n=tuck),1.5); go(P(b=side),3.5); relax(0)
    go(P(a=-0.5,n=tuck,h=0),3.5); relax(3)
    go(P(a=0.3,n=tuck),4.0)
    go(P(a=arm,n=tuck),5.0)
    if deep>2000:
        u0=1500+500*arm
        for k in range(1,121):
            u=u0+(deep-u0)*k/120; S[1].us(u); cur[1]=(u-1500)/500; sleep(0.035)
    chatter(False); sleep(1.0); say('Zzz'); relax(); sleep(1.5); music_stop(); print('\n*** zzz ***')

class Actor:
    """Spring-driven puppet: set targets, then wait() while springs carry the motion."""
    K=[55.0,30.0,65.0,95.0]; C=[11.0,10.0,11.5,10.0]
    LO=[-0.9,-0.95,-1.0,-0.85]; HI=[0.9,-0.15,-0.1,0.85]
    def __init__(s,wake=True):
        if wake: wakeup()
        power_all()
        s.x=cur[:]; s.v=[0.0]*4; s.tgt=REST[:]; s.T=0.0; s.stiff=1.0; s.idle_amt=1.0; s.fx=[]; s._whine=-9
    def to(s,b=None,a=None,n=None,h=None,stiff=None):
        for i,val in enumerate((b,a,n,h)):
            if val is not None: s.tgt[i]=val
        if stiff is not None: s.stiff=stiff
        return s
    def rest(s,stiff=None): return s.to(*REST,stiff=stiff)
    def shake(s,joint,amp,hz,dur):
        s.fx.append(('banh'.index(joint),amp,hz,s.T,s.T+dur)); return s
    def wait(s,secs):
        dt=0.02; nxt=_time.monotonic()
        for _ in range(int(secs/dt)):
            T=s.T; m=s.idle_amt
            idle=[m*(0.05*sin(T*0.37)+0.03*sin(T*1.13)),m*0.07*sin(T*1.05),
                  m*(0.06*sin(T*1.05+0.7)+0.03*sin(T*0.29)),m*(0.06*sin(T*0.43+1)+0.03*sin(T*1.7))]
            add=[0.0]*4
            for (j,amp,hz,t0,t1) in s.fx:
                if t0<=T<t1:
                    env=min(1,(T-t0)/0.1,(t1-T)/0.1); add[j]+=amp*env*sin(6.2832*hz*(T-t0))
            s.fx=[f for f in s.fx if f[4]>T]
            for i in range(4):
                k=s.K[i]*s.stiff; c=s.C[i]*s.stiff**0.5
                acc=k*(s.tgt[i]+idle[i]-s.x[i])-c*s.v[i]
                s.v[i]=max(-5,min(5,s.v[i]+acc*dt)); s.x[i]+=s.v[i]*dt
                s.x[i]=max(s.LO[i],min(s.HI[i],s.x[i]))
                val=max(s.LO[i],min(s.HI[i],s.x[i]+add[i]))
                cur[i]=val; S[i].value=val
            if VOICE and max(abs(x) for x in s.v)>1.6 and s.T-s._whine>0.7:
                s._whine=s.T
                try:
                    import sounds; sounds.play('servo',vol=0.3)
                except Exception: pass
            nxt+=dt; sleep(max(0,nxt-_time.monotonic())); s.T+=dt
        return s
    def sfx(s,name):
        sfx(name); return s
    def end(s,bed=False,side=0.95):
        s.rest(stiff=0.6).wait(1.5)
        if bed: bedtime(side)
        else: finish()
