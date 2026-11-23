from desk import *
def each():
    wakeup()
    for i in range(4):
        say('testing '+NAMES[i])
        t=cur[:]; t[i]=REST[i]+0.3; go(t,1); t[i]=REST[i]-0.3 if REST[i]-0.3>=-1 else REST[i]+0.15; go(t,1.2); go(REST,1)
    finish()
def hold(secs=30):
    wakeup(); power_all(); say(f'holding {secs}s - gently wiggle the joints to find loose ones'); sleep(secs); finish()
def spinlean():
    wakeup(); relax(3)
    say('base spin: slow, medium, wide')
    for a,d in ((0.6,2.5),(0.6,1.2),(0.9,2)):
        go(P(b=a),d); go(P(b=-a),d*1.5); go(P(b=0),d)
    say('arm leans, base holding')
    for _ in range(3): go(P(a=-0.35),1.5); go(P(a=-0.8),1.5)
    for _ in range(4): go(P(a=-0.45),0.4); go(P(a=-0.8),0.4)
    finish()
def nudge():
    import termios,tty
    f=open('/dev/tty'); fd=f.fileno(); old=termios.tcgetattr(fd)
    wakeup(); power_all(); sel=1; step=0.1
    def show(): print('\r'+'  '.join(('['+NAMES[i]+f' {cur[i]:+.2f}]') if i==sel else (NAMES[i]+f' {cur[i]:+.2f}') for i in range(4))+f'  step {step}   ',end='',flush=True)
    print('\n1-4 pick joint | a/d nudge | w/s step bigger/smaller | q quit')
    tty.setcbreak(fd)
    try:
        show()
        while True:
            k=f.read(1)
            if k in '1234': sel=int(k)-1
            elif k in 'ad':
                t=cur[:]; t[sel]=max(-1,min(1,t[sel]+(step if k=='d' else -step))); go(t,0.3)
            elif k=='w': step=min(0.4,round(step*2,3))
            elif k=='s': step=max(0.01,round(step/2,3))
            elif k=='q': break
            show()
    finally:
        termios.tcsetattr(fd,termios.TCSADRAIN,old)
        print('\nfinal pose:',' '.join(f'{v:.2f}' for v in cur))
        relax()
