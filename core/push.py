"""Phone notifications (Web Push). Works even when the BITCH app on your phone is closed.

The signing key is made once and kept in ~/.bitch/push.pem. On iPhone, notifications need the app added to the
Home Screen and opened from there (iOS 16.4 or newer); the app walks you through it.
"""
import base64, json, os
from . import config

KEY = os.path.join(config.HOME, 'push.pem')
_v = [None]


def vapid():
    if _v[0] is None:
        from py_vapid import Vapid
        if os.path.exists(KEY): _v[0] = Vapid.from_file(KEY)
        else:
            v = Vapid(); v.generate_keys(); v.save_key(KEY); os.chmod(KEY, 0o600); _v[0] = v
    return _v[0]


def public_key():
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    raw = vapid().public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


def send(sub, title, body, tag=''):
    """True if sent, False if the phone turned notifications off (forget it), None on a temporary problem."""
    if not sub: return False
    from pywebpush import webpush, WebPushException
    try:
        webpush(subscription_info=sub, data=json.dumps({'title': title, 'body': body, 'tag': tag or 'bitch'}),
                vapid_private_key=vapid(), vapid_claims={'sub': 'mailto:bitch-robot@example.com'}, ttl=3600, timeout=10)
        return True
    except WebPushException as e:
        code = getattr(e.response, 'status_code', None)
        print(f'  notification failed ({code}): {e}', flush=True)
        return False if code in (404, 410) else None
    except Exception as e:
        print(f'  notification failed: {e}', flush=True)
        return None
