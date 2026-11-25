#!/usr/bin/env python3
"""Center specific motors and hold them there (for attaching horns or lining things up).
  python3 ~/center.py base            one motor
  python3 ~/center.py arm neck        several
  python3 ~/center.py all             every motor
  python3 ~/center.py 1 3             by number (1 base, 2 arm, 3 neck, 4 head)
Add  go  at the end to let go right away instead of holding until you press Enter.

  python3 ~/center.py calibrate         fix crooked centers, joint by joint (or: calibrate base neck)
  python3 ~/center.py basespeed 0.6     gentler base turns in every show (1 = full speed)
  python3 ~/center.py reset             forget the calibration"""
import sys, desk
from time import sleep
ALIAS={'1':0,'s1':0,'base':0,'2':1,'s2':1,'arm':1,'3':2,'s3':2,'neck':2,'4':3,'s4':3,'head':3}
words=[w.lower() for w in sys.argv[1:]]; let_go='go' in words; words=[w for w in words if w!='go']
if not words or words==['help']: print(__doc__); sys.exit()
if words[0]=='reset': desk.TRIM[:]=[0,0,0,0]; desk.save_trim(); print('Calibration cleared.'); sys.exit()
if words[0]=='basespeed':
    desk.BASE_SPEED=max(0.2,min(1.0,float(words[1]))); desk.save_trim(); print(f'Base speed limit set to {desk.BASE_SPEED:.2f}. Applies to every show from now on.'); sys.exit()
if words[0]=='calibrate':
    import termios, tty
    js=[ALIAS[w] for w in words[1:] if w in ALIAS] or [1,2,3,0]
    f=open('/dev/tty'); fd=f.fileno(); old=termios.tcgetattr(fd)
    print('\nFor each joint: a/d nudge (A/D bigger), r undo all, Enter = this is straight, next joint. q quits without saving this joint.')
    try:
        tty.setcbreak(fd)
        for i in js:
            t0=desk.TRIM[i]; desk.S[i].value=0; desk.on[i]=True; desk.cur[i]=0
            while True:
                print(f'\r   {desk.NAMES[i]:5} (S{i+1})  center offset {desk.TRIM[i]:+.3f}    ',end='',flush=True)
                k=f.read(1)
                step={'a':-0.01,'d':0.01,'A':-0.05,'D':0.05}.get(k)
                if step: desk.TRIM[i]=max(-0.9,min(0.9,desk.TRIM[i]+step)); desk.S[i].value=0
                elif k=='r': desk.TRIM[i]=t0; desk.S[i].value=0
                elif k in '\r\n': desk.save_trim(); print(' saved'); break
                elif k=='q': desk.TRIM[i]=t0; print(' kept old'); raise KeyboardInterrupt
    except KeyboardInterrupt: pass
    finally:
        termios.tcsetattr(fd,termios.TCSADRAIN,old); desk.save_trim(); desk.relax(); print('Done. Every command now uses these centers.')
    sys.exit()
js=[0,1,2,3] if 'all' in words else []
for w in words:
    if w!='all':
        if w not in ALIAS: print(f"Don't know '{w}'. Use base, arm, neck, head, all or 1-4."); sys.exit(1)
        js.append(ALIAS[w])
js=sorted(set(js),key=lambda i:(1,2,3,0).index(i))          # arm first so nothing drops
for i in js:
    print(f'   centering {desk.NAMES[i]} (S{i+1}, channel {i})',flush=True)
    desk.S[i].value=0; desk.on[i]=True; desk.cur[i]=0; sleep(0.4)
if let_go: desk.relax(*js); print('Done, let go.')
else:
    try: input(f"\nHolding {', '.join(desk.NAMES[i] for i in js)} at center. Attach horns now, then press Enter to let go. ")
    except (KeyboardInterrupt,EOFError): pass
    desk.relax(*js); print('Let go.')
