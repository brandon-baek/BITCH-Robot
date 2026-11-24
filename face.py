"""BITCH's face on its OLED: two googly eyes that blink, wobble, look at you, show its feelings and move when it talks.
brain.py runs it automatically when the screen is found; without a screen it quietly does nothing.

Wiring (I2C): VCC to 3.3V, GND to GND, SDA to pin 3, SCL to pin 5, RES to pin 7 (GPIO4).
Once on the Pi:  pip install adafruit-circuitpython-ssd1306 --break-system-packages
Check it:        python3 face.py          (shows each feeling for a moment)
Without a Pi:    python3 face.py --png /tmp/faces   (draws the frames as pictures instead)
"""
import math, random, struct, sys, threading, time, zlib

W, H = 128, 64
# brow angle (radians; + tips the inner ends down) and height; same feelings as the app's drawing
BROWS = {'happy': (-0.12, -2), 'excited': (-0.15, -4), 'love': (-0.25, -2), 'sad': (-0.5, 0), 'angry': (0.5, 3), 'scared': (-0.35, -5),
         'curious': ((-0.12, -5), (0.15, 0)), 'confused': ((-0.4, -3), (0.3, 1)), 'proud': (-0.05, -3), 'sleepy': (0.05, 2), 'neutral': (0, 0)}
SMILE = {'happy': 3, 'excited': 4, 'love': 3, 'proud': 2, 'sad': -3, 'angry': -2, 'scared': -2}


class Canvas:
    """A 128x64 one-bit picture in the OLED's own memory layout (8 rows per byte)."""
    def __init__(s): s.buf = bytearray(W * H // 8)
    def clear(s): s.buf[:] = bytes(len(s.buf))
    def px(s, x, y, on=True):
        if 0 <= x < W and 0 <= y < H:
            i = (y >> 3) * W + x
            if on: s.buf[i] |= 1 << (y & 7)
            else: s.buf[i] &= ~(1 << (y & 7)) & 0xFF
    def span(s, y, x0, x1, on=True):
        for x in range(max(0, int(x0)), min(W, int(x1) + 1)): s.px(x, y, on)
    def disc(s, cx, cy, r, on=True, top=None):
        """A filled circle; with top, only the part below row `top`."""
        for dy in range(-int(r) + 1, int(r)):
            y = int(round(cy + dy))
            if top is not None and y < top: continue
            w = math.sqrt(max(0, r * r - dy * dy)) - 0.6; s.span(y, math.floor(cx - w + 0.5), math.floor(cx + w + 0.5), on)
    def ring(s, cx, cy, r, on=True):
        for a in range(0, 360, 4):
            s.px(int(round(cx + r * math.cos(math.radians(a)))), int(round(cy + r * math.sin(math.radians(a)))), on)
    def line(s, x0, y0, x1, y1, on=True, thick=1):
        n = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
        for k in range(n + 1):
            x = x0 + (x1 - x0) * k / n; y = y0 + (y1 - y0) * k / n
            for t in range(thick): s.px(int(round(x)), int(round(y)) + t - thick // 2, on)
    def rect(s, x0, y0, x1, y1, on=True):
        for y in range(int(y0), int(y1) + 1): s.span(y, x0, x1, on)
    def png(s, path, scale=4):
        rows = b''.join(b'\x00' + bytes(255 if s.buf[(y >> 3) * W + x] >> (y & 7) & 1 else 0
                                         for x in range(W) for _ in range(scale)) for y in range(H) for _ in range(scale))
        def chunk(t, d): return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
        open(path, 'wb').write(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', W * scale, H * scale, 8, 0, 0, 0, 0))
                               + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))


def open_screen():
    """Find the OLED (resetting it first through RES on pin 7). Returns the display, or None."""
    try:
        import board, busio, digitalio, adafruit_ssd1306
    except ImportError as e:
        print(f'  face: no OLED library ({e.name}); for the face run: pip install adafruit-circuitpython-ssd1306 --break-system-packages', flush=True)
        return None
    try:
        rst = digitalio.DigitalInOut(board.D4)
        rst.direction = digitalio.Direction.OUTPUT
        rst.value = False; time.sleep(0.02); rst.value = True; time.sleep(0.05)
        i2c = busio.I2C(board.SCL, board.SDA)
        while not i2c.try_lock(): time.sleep(0.01)
        found = i2c.scan(); i2c.unlock()
        addr = next((a for a in (0x3C, 0x3D) if a in found), None)
        if addr is None:
            print(f'  face: no OLED on I2C (found {[hex(a) for a in found]}); check SDA pin 3, SCL pin 5, RES pin 7', flush=True); return None
        oled = adafruit_ssd1306.SSD1306_I2C(W, H, i2c, addr=addr, reset=rst)
        print(f'  face: OLED found at {hex(addr)}', flush=True)
        return oled
    except Exception as e:
        print(f'  face: OLED not available ({e})', flush=True); return None


class Face:
    def __init__(s, screen=None):
        s.screen = screen; s.c = Canvas(); s.ok = screen is not None
        s.feeling = 'neutral'; s.level = 0.0; s.talk_until = 0; s.asleep = False; s.thinking = False
        s.gaze = [0.0, 0.0]; s.lid = 0.0; s.blink_at = time.time() + 2; s.blink = 0.0
        s.pup = [[0.0, 3.0, 0.0, 0.0], [0.0, 3.0, 0.0, 0.0]]          # x, y, vx, vy for each pupil
        s.brow = [[0.0, 0.0], [0.0, 0.0]]; s.bob = 0.0; s.last = time.time()

    # what brain.py tells it
    def feel(s, feeling): s.feeling = feeling if feeling in BROWS else 'neutral'; s.poke(0.4)
    def talk(s, level): s.level = level; s.talk_until = time.time() + 0.25
    def sleep(s, on=True): s.asleep = on
    def look(s, b=None, n=None):
        if b is not None: s.gaze[0] = max(-1, min(1, -b * 1.3))
        if n is not None: s.gaze[1] = max(-1, min(1, -(n + 0.6) * 1.5))
    def poke(s, amount=1.0):
        for p in s.pup: p[2] += random.uniform(-30, 30) * amount; p[3] -= 25 * amount

    def start(s):
        if s.ok: threading.Thread(target=s._run, daemon=True).start()

    def _run(s):
        fb = None
        while True:
            try:
                s.draw()
                if fb is None:
                    b = s.screen.buffer; fb = memoryview(b)[1:] if len(b) == len(s.c.buf) + 1 else memoryview(b)
                fb[:] = s.c.buf; s.screen.show()
            except Exception as e:
                print(f'  face: {e}', flush=True); time.sleep(2)
            time.sleep(0.03)

    def draw(s, now=None):
        now = now or time.time(); dt = min(0.1, max(0.001, now - s.last)); s.last = now
        c = s.c; c.clear()
        talking = now < s.talk_until
        s.bob += ((s.level * 3 if talking else 0) - s.bob) * 0.5
        if now > s.blink_at: s.blink_at = now + random.uniform(2.5, 6); s.blink = 1.0
        s.blink = max(0.0, s.blink - dt * 7)
        lid_goal = 1.0 if s.asleep else 0.5 if s.feeling == 'sleepy' else 0.2 if s.feeling == 'angry' else 0.0
        s.lid += (max(lid_goal, 1.0 if s.blink > 0.5 else 0.0) - s.lid) * min(1, dt * 18)
        R, PR = 20, 8
        for i, cx in enumerate((38, 90)):
            cy = 31 + s.bob
            P = s.pup[i]                                         # a loose pupil: it lags, falls a little and bounces
            tx = s.gaze[0] * (R - PR) * 0.6; ty = s.gaze[1] * (R - PR) * 0.6 + 2.5
            P[2] += ((tx - P[0]) * 55 - P[2] * 4.5) * dt; P[3] += ((ty - P[1]) * 55 - P[3] * 4.5) * dt
            P[0] += P[2] * dt; P[1] += P[3] * dt
            d = math.hypot(P[0], P[1]); lim = R - PR - 1.5
            if d > lim: P[0] *= lim / d; P[1] *= lim / d; P[2] *= -0.6; P[3] *= -0.6
            if s.lid > 0.92:                                     # closed: a sleepy smile line
                for k in range(-R + 3, R - 2):
                    c.px(cx + k, int(cy + 2 + 4 * (1 - (k / R) ** 2)), True); c.px(cx + k, int(cy + 3 + 4 * (1 - (k / R) ** 2)), True)
            else:
                c.disc(cx, cy, R, True, top=int(cy - R + 2 * R * s.lid))
                if s.lid > 0.05: c.line(cx - R + 2, int(cy - R + 2 * R * s.lid), cx + R - 2, int(cy - R + 2 * R * s.lid), True, 2)
                c.disc(cx + P[0], cy + P[1], PR, False, top=int(cy - R + 2 * R * s.lid))
                c.px(int(cx + P[0] - 3), int(cy + P[1] - 3), True); c.px(int(cx + P[0] - 2), int(cy + P[1] - 3), True)
            b = BROWS.get(s.feeling, (0, 0)); ang, dy = (b[i] if isinstance(b[0], tuple) else b)
            s.brow[i][0] += (ang - s.brow[i][0]) * 0.2; s.brow[i][1] += (dy - s.brow[i][1]) * 0.2
            a, h = s.brow[i]; by = cy - R - 4 + h; t = math.tan(a) * 6     # inner ends sit near the middle of the face
            if i == 0: c.line(cx - 12, by - t, cx + 12, by + t, True, 3)
            else: c.line(cx - 12, by + t, cx + 12, by - t, True, 3)
        my = 59 + s.bob
        if talking:                                              # the speaker slit opens with the sound
            o = 1 + int(s.level * 5); c.rect(58, my - o, 70, my + o // 2, True)
        else:
            sm = SMILE.get(s.feeling, 0)
            for k in range(-7, 8): c.px(64 + k, int(round(my + sm * (1 - (k / 7) ** 2) - sm * 0.6)), True)   # + sm: the middle dips = a smile
        if s.asleep:
            z = int(now * 2) % 3
            for i in range(z + 1):
                x, y = 108 + i * 6, 14 - i * 6; c.line(x, y, x + 4, y, True); c.line(x + 4, y, x, y + 4, True); c.line(x, y + 4, x + 4, y + 4, True)
        if s.thinking:
            for i in range(3):
                if int(now * 3) % 3 >= i: c.disc(112 + i * 5, 8, 1.5, True)
        return c


if __name__ == '__main__':
    if '--png' in sys.argv:
        import os
        out = sys.argv[sys.argv.index('--png') + 1]; os.makedirs(out, exist_ok=True)
        f = Face(); t = 1000.0
        for name in ['neutral', 'happy', 'sad', 'angry', 'curious', 'sleepy']:
            f.feel(name)
            for k in range(40): t += 0.05; f.draw(t)
            f.c.png(f'{out}/{name}.png')
        f.feel('happy')
        for k in range(30): t += 0.05; f.draw(t)
        f.talk_until = t + 1; f.level = 0.9; f.draw(t + 0.05); f.c.png(f'{out}/talking.png'); f.talk_until = 0
        f.sleep(True)
        for k in range(30): t += 0.05; f.draw(t)
        f.c.png(f'{out}/asleep.png'); print('frames in', out)
    else:
        f = Face(open_screen())
        if not f.ok: sys.exit(1)
        f.start()
        for name in ['happy', 'curious', 'sad', 'angry', 'love', 'sleepy', 'excited', 'neutral']:
            print('  ', name); f.feel(name); time.sleep(2.5)
        f.talk(0.8); time.sleep(0.2)
