#!/usr/bin/env python3
"""Center specific motors and hold them there (for attaching horns or lining things up).
  python3 ~/center.py base            one motor
  python3 ~/center.py arm neck        several
  python3 ~/center.py all             every motor
  python3 ~/center.py 1 3             by number (1 base, 2 arm, 3 neck, 4 head)
Add  go  at the end to let go right away instead of holding until you press Enter."""
import sys, desk
from time import sleep
ALIAS={'1':0,'s1':0,'base':0,'2':1,'s2':1,'arm':1,'3':2,'s3':2,'neck':2,'4':3,'s4':3,'head':3}
words=[w.lower() for w in sys.argv[1:]]; let_go='go' in words; words=[w for w in words if w!='go']
if not words or words==['help']: print(__doc__); sys.exit()
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
