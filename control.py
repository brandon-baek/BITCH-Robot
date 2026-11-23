# Drive Desk Creature live from the keyboard, like a video game.
import sys, termios, tty, select, time
from desk import *
KEYS={'a':(0,+1),'d':(0,-1),'w':(1,+1),'s':(1,-1),'i':(2,+1),'k':(2,-1),'j':(3,+1),'l':(3,-1)}
SOUNDS={'1':'hello','2':'happy','3':'question','4':'sad','5':'angry','6':'giggle','7':'scream','8':'love','9':'think','0':'tada'}
HELP="""
  DESK CREATURE CONTROLLER
  A / D   turn base          W / S   lean arm
  I / K   nod head           J / L   tilt head
  hold SHIFT for big fast moves (capital letters)
  Z  nod yes     X  shake no     C  happy wiggle    V  look around
  1-0  sounds: hello happy question sad angry giggle scream love think ta-da
  SPACE  back to rest      P  print pose      Q  quit (eases to rest)
  If a direction feels backwards, tell Claude and it gets flipped.
"""
def drive():
    f=open('/dev/tty'); fd=f.fileno(); old=termios.tcgetattr(fd)
    A=Actor(); A.idle_amt=0.25; A.to(stiff=2.2)
    print(HELP,flush=True)
    tty.setcbreak(fd)
    def burst(seq):
        for tgt,dur in seq: A.to(**tgt); A.wait(dur)
    try:
        while True:
            r,_,_=select.select([f],[],[],0)
            keys=''
            while r:
                keys+=f.read(1); r,_,_=select.select([f],[],[],0)
            for ch in keys:
                lo=ch.lower()
                if lo in KEYS:
                    j,dr=KEYS[lo]; step=0.18 if ch.isupper() else 0.05
                    A.tgt[j]=max(A.LO[j],min(A.HI[j],A.tgt[j]+dr*step))
                    A.stiff=3.0 if ch.isupper() else 2.2
                elif ch in SOUNDS: A.sfx(SOUNDS[ch])
                elif lo=='z':
                    n0=A.tgt[2]; A.sfx('beepboop'); burst([(dict(n=min(-0.1,n0+0.3),stiff=3.5),0.2),(dict(n=n0),0.2)]*2)
                elif lo=='x':
                    h0=A.tgt[3]; A.sfx('grumble'); burst([(dict(h=h0+0.35,stiff=3.5),0.15),(dict(h=h0-0.35),0.15)]*2+[(dict(h=h0),0.2)])
                elif lo=='c':
                    b0=A.tgt[0]; A.sfx('happy'); burst([(dict(b=b0+0.2,h=0.3,stiff=4),0.14),(dict(b=b0-0.2,h=-0.3),0.14)]*3+[(dict(b=b0,h=0),0.2)])
                elif lo=='v':
                    A.sfx('scan'); burst([(dict(b=0.6,stiff=1.6),0.9),(dict(b=-0.6),1.2),(dict(b=0),0.8)])
                elif ch==' ': A.rest(stiff=1.5); A.sfx('blip')
                elif lo=='p': print('\r  pose:',' '.join(f'{v:+.2f}' for v in A.tgt),'      ',flush=True)
                elif lo=='q': raise KeyboardInterrupt
            A.wait(0.02)
    except KeyboardInterrupt: pass
    finally:
        termios.tcsetattr(fd,termios.TCSADRAIN,old)
        print('\n  parking...',flush=True); A.end()
if __name__=='__main__': drive()
