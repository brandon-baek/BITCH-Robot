"""The desk puppet's spring integrator, shared by the shows and the live controller."""
import math
import time


def bounded(x, low, high):
    return max(low, min(high, x))


class Actor:
    K = (55., 30., 65., 95.)
    C = (11., 10., 11.5, 10.)
    LO = (-.9, -1.3, -1., -.85)
    HI = (.9, -.15, -.1, .85)

    def __init__(self, wake=True, *, desk=None, powered=False):
        if desk is None:
            import desk
        self.desk = desk
        if wake:
            desk.wakeup()
        if not powered:
            desk.power_all()
        self.x = desk.cur[:]
        self.v = [0.] * 4
        self.tgt = list(desk.REST) if hasattr(desk, 'REST') else self.x[:]
        self.T = 0.
        self.stiff = self.idle_amt = 1.
        self.idle_scale = (1.,) * 4
        self.fx = []
        self._whine = -9.
        self.low, self.high = self.LO, self.HI
        self.speed = (5 * desk.BASE_SPEED, 5, 5, 5)
        self.accel = (math.inf,) * 4

    def to(self, b=None, a=None, n=None, h=None, stiff=None):
        for i, value in enumerate((b, a, n, h)):
            if value is not None:
                self.tgt[i] = value
        if stiff is not None:
            self.stiff = stiff
        return self

    def rest(self, stiff=None):
        return self.to(*self.desk.REST, stiff=stiff)

    def shake(self, joint, amp, hz, dur):
        self.fx.append(('banh'.index(joint), amp, hz, self.T, self.T + dur))
        return self

    def step(self, dt=.02):
        t = self.T
        sin = math.sin
        idle = (.05*sin(t*.37) + .03*sin(t*1.13), .07*sin(t*1.42),
                .06*sin(t*1.42+.7) + .03*sin(t*.29), .06*sin(t*.43+1) + .03*sin(t*1.7))
        offsets = [0.] * 4
        self.fx = [effect for effect in self.fx if effect[4] > t]
        for joint, amp, hz, start, end in self.fx:
            env = bounded(min((t-start)/.1, (end-t)/.1), 0, 1)
            offsets[joint] += amp * env * sin(math.tau * hz * (t-start))
        for i in range(4):
            old = self.x[i]
            target = bounded(self.tgt[i] + self.idle_amt * self.idle_scale[i] * idle[i], self.low[i], self.high[i])
            acc = self.K[i] * self.stiff * (target-old) - self.C[i] * math.sqrt(self.stiff) * self.v[i]
            v = self.v[i] + bounded(acc, -self.accel[i], self.accel[i]) * dt
            self.v[i] = bounded(v, -self.speed[i], self.speed[i])
            # Return gradually from a remembered show pose outside the live envelope.
            lo, hi = min(old, self.low[i]), max(old, self.high[i])
            x = old + self.v[i] * dt
            self.x[i] = bounded(x, lo, hi)
            if self.x[i] != x:
                self.v[i] = 0.
            value = bounded(self.x[i] + offsets[i], lo, hi)
            self.desk.cur[i] = value
            self.desk.S[i].us(1500 + value * (1000 if i == 0 else 500))
        self.T += dt
        return self

    def wait(self, secs):
        self._goal = getattr(self, '_goal', self.T) + secs
        nxt = getattr(self, '_nxt', time.monotonic())
        if time.monotonic() - nxt > .25:
            nxt = time.monotonic()
        for _ in range(max(0, round((self._goal-self.T)/.02))):
            self.step()
            if self.desk.VOICE and max(map(abs, self.v)) > 1.6 and self.T-self._whine > .7:
                self._whine = self.T
                try:
                    import sounds
                    sounds.play('servo', vol=.3)
                except Exception:
                    pass
            nxt += .02
            time.sleep(max(0, nxt-time.monotonic()))
        self._nxt = nxt
        return self

    def sfx(self, name):
        self.desk.sfx(name)
        return self

    def end(self, bed=False, side=.95):
        self.rest(stiff=.6).wait(1.5)
        if bed:
            self.desk.bedtime(side)
        else:
            self.desk.finish()
