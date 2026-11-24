"""What every device looks like to BITCH.

A device is anything that gives BITCH a sense or a way to act. It says which capabilities it has:

  senses   ears   it hears (sends 16 kHz audio to hearing.py)
           eyes   it sees (sends camera frames to sight.py, or snapshots on request)
           keys   you can type to BITCH on it
  outputs  voice  it can play BITCH's voice
           body   it can move (the robot)
           face   it shows BITCH's face and the app (phone, Mac window, a future OLED)
           notify it can ping you when you're away (phone notifications)

To add new hardware, write a small class like the ones in this folder: subclass Device, list its caps,
and fill in the methods for the outputs it has. Senses just call hub.hearing.feed(...) or hub.sight.frame(...).
Nothing else in BITCH needs to change.
"""

SENSES = ('ears', 'eyes', 'keys')
OUTPUTS = ('voice', 'body', 'face', 'notify')
CAP_LABELS = {'ears': 'Hears', 'eyes': 'Sees', 'keys': 'Typing', 'voice': 'Speaks', 'body': 'Moves', 'face': 'Shows face', 'notify': 'Notifies'}


class Device:
    kind = 'device'      # phone, mac, robot, ...
    place = 'desk'       # 'desk' (next to the robot) or 'mobile' (in your pocket)
    priority = 0         # when several could speak at the desk, the highest wins

    def __init__(s, id, name, caps=(), place=None):
        s.id, s.name = id, name
        s.caps = set(caps)
        s.off = set()            # caps you switched off in the app
        s.online = True
        s.detail = ''            # a short line shown in the Devices screen
        s.hub = None
        if place: s.place = place

    def active(s, cap):
        return cap in s.caps and cap not in s.off and s.online

    def info(s):
        return {'id': s.id, 'name': s.name, 'kind': s.kind, 'place': s.place, 'online': s.online,
                'caps': sorted(s.caps), 'off': sorted(s.off), 'detail': s.detail}

    def start(s): pass
    def stop(s): pass

    # ----- outputs: override the ones this device has -----
    def speak(s, wav, seconds, text='', emotion='neutral', move='none'):
        """Play BITCH's voice (WAV bytes). Should return quickly; the hub waits `seconds`."""
    def hush(s):
        """Stop talking right now."""
    def gesture(s, emotion='neutral', move='none'):
        """A quick emotional pose plus a small move (nod, wave...)."""
    def perform(s, steps, name=''):
        """Play a motion routine: a list of steps from moves.py."""
    def show(s, name):
        """A built-in performance: dance, sing, sleep, wake."""
    def look(s, b=None, n=None, eye=False, seen=True):
        """Turn toward something. b = base (-1 right .. 1 left), n = neck (-1 down .. 0 level)."""
    def notify(s, title, body, tag=''):
        """Ping the person (phone notification)."""
    def snapshot(s, timeout=4.0):
        """A JPEG from this device's camera, or None."""
        return None
    def ui(s, msg):
        """An update for the app's screens. Only devices that show the app care."""
