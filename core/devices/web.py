"""Any device that connects over the web link (/link): your phone, the Mac's BITCH window, and later the robot's
own hardware if it runs a small client.

A web device says what it has when it connects (its caps), and can change them later, for example when you
turn on the phone's mic. Messages are JSON; audio and pictures are binary frames that start with one type byte:

  to the device     0x01 + WAV        BITCH's voice to play
  from the device   0x02 + PCM16      16 kHz mic audio
                    0x03 + JPEG       a camera snapshot
Phones that turned on notifications keep working as 'notify' devices even when the app is closed.
"""
import asyncio, json, threading, time
from .base import Device
from .. import memory

AUDIO_OUT, MIC_IN, SNAP_IN = 1, 2, 3


def saved():
    return memory.kv_get('webdevices', {})


def save(dev_id, **kv):
    d = saved(); d.setdefault(dev_id, {}).update(kv); memory.kv_set('webdevices', d)


def forget(dev_id):
    d = saved(); d.pop(dev_id, None); memory.kv_set('webdevices', d)


class WebDevice(Device):
    kind = 'phone'
    priority = 5

    def __init__(s, dev_id, name, kind='phone', place='mobile'):
        super().__init__(dev_id, name, caps=('keys', 'face'), place=place)
        s.kind = kind
        s.loop = None; s.q = None
        s.online = False
        s._snap = None; s._snap_ev = threading.Event()
        rec = saved().get(dev_id, {})
        s.push = rec.get('push')
        s.off = set(rec.get('off', []))
        if s.push: s.caps.add('notify')

    def active(s, cap):
        if cap == 'notify': return 'notify' in s.caps and 'notify' not in s.off and bool(s.push)
        return super().active(cap)

    # ----- connection (called by server.py) -----
    def attach(s, loop, q):
        s.loop, s.q, s.online = loop, q, True
        s.detail = 'Connected'

    def detach(s):
        s.online = False; s.loop = None; s.q = None
        s.caps -= {'ears', 'eyes', 'voice'}
        s.detail = 'App closed' + (' (notifications still on)' if s.push else '')

    def set_caps(s, caps):
        keep = {'keys', 'face'} | ({'notify'} if s.push else set())
        s.caps = keep | (set(caps) & {'ears', 'eyes', 'voice'})
        if s.hub: s.hub.devices_changed()

    def _put(s, item):
        if s.loop and s.q is not None:
            try: s.loop.call_soon_threadsafe(s.q.put_nowait, item)
            except RuntimeError: pass

    # ----- outputs -----
    def ui(s, msg):
        s._put(msg)

    def speak(s, wav, seconds, text='', emotion='neutral', move='none'):
        s._put(bytes([AUDIO_OUT]) + wav)

    def hush(s):
        s._put({'type': 'hush'})

    def notify(s, title, body, tag=''):
        from .. import push
        ok = push.send(s.push, title, body, tag)
        if ok is False:                                    # the phone unsubscribed: stop trying
            s.push = None; s.caps.discard('notify'); save(s.id, push=None)
            if s.hub: s.hub.devices_changed()
        return ok

    def snapshot(s, timeout=4.0):
        if not s.active('eyes'): return None
        s._snap = None; s._snap_ev.clear(); s._put({'type': 'snap'})
        s._snap_ev.wait(timeout)
        return s._snap

    # ----- input from the device -----
    def got_binary(s, data):
        kind, body = data[0], data[1:]
        if kind == MIC_IN and s.active('ears') and s.hub and s.hub.hearing:
            import numpy as np
            s.hub.hearing.feed(s.id, np.frombuffer(body, '<i2').astype(np.float32) / 32768)
        elif kind == SNAP_IN:
            s._snap = bytes(body); s._snap_ev.set()
