from adafruit_servokit import ServoKit
from time import sleep
from math import sin
import time as _time
kit=ServoKit(channels=16)
import json as _j, os as _o
_TF=_o.path.expanduser('~/.desk_trim.json')
def _trim_load():
    try: t=_j.load(open(_TF))
    except Exception: t={}
    return [float(x) for x in t.get('trim',[0,0,0,0])], float(t.get('base_speed',1.0))
TRIM,BASE_SPEED=_trim_load()       # TRIM: where "center" really is for each joint (from: python3 ~/center.py calibrate)
def save_trim():
    try: _j.dump({'trim':[round(x,3) for x in TRIM],'base_speed':BASE_SPEED},open(_TF,'w'))
    except Exception: pass
class _Servo:
    """value -1..1 maps onto lo..hi microseconds around this joint's calibrated center.
    Values a little past -1/1 are allowed (REACH) for joints that can physically go further."""
    def __init__(s,ch,lo,hi):
        s.ch=ch; s.c=kit.servo[ch]; s.lo,s.hi=lo,hi; s.wide=True; s.c.set_pulse_width_range(500,2500); s.c.actuation_range=180
    def us(s,u):
        """set an exact pulse in microseconds (500-2500)"""
        u+=TRIM[s.ch]*(s.hi-s.lo)/2
        s.c.angle=max(0,min(180,(u-500)/2000*180))
    @property
    def value(s):
        a=s.c.angle
        return None if a is None else ((a/180*2000+500)-1500)/((s.hi-s.lo)/2)-TRIM[s.ch]
    @value.setter
    def value(s,v):
        lo,hi=REACH[s.ch]; v=max(lo,min(hi,v))
        s.us(1500+v*(s.hi-s.lo)/2)
    def detach(s): s.c.angle=None
REST=[0.0,-0.8,-0.8,0.0]
REACH=[(-1,1),(-1.5,1.5),(-1,1),(-1,1)]      # arm can lean back past -1 (and lie down past +1)
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
    if BASE_SPEED<1 and mv[0]: d=max(d,abs(t[0]-cur[0])/(2.5*BASE_SPEED)/SPEED)   # gentle base: never faster than the limit
    st=cur[:]; n=max(1,int(d*SPEED/0.02)); nxt=_time.monotonic()
    for k in range(1,n+1):
        f=_ease(k/n,e)
        for i in range(4):
            if mv[i]: cur[i]=max(REACH[i][0],min(REACH[i][1],st[i]+(t[i]-st[i])*f)); S[i].value=cur[i]
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

from actor import Actor
