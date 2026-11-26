"""Read recent macOS notifications (including iPhone Mirroring) + optional webhook notifications.

Uses macOS's standard Notification Center SQLite database in read-only mode when the user
has granted BITCH Full Disk Access in System Settings, and also accepts pushed notifications
via /api/notify (e.g., from an iOS Shortcut) without requiring extra macOS permissions.
"""
from collections import deque
import os
from pathlib import Path
import plistlib
import sqlite3
import subprocess
import threading
import time

# macOS Core Data timestamps count seconds since 2001-01-01T00:00:00Z.
APPLE_EPOCH = 978307200.0

APP_NAMES = {
    'com.apple.MobileSMS': 'Messages',
    'com.apple.mail': 'Mail',
    'com.apple.iCal': 'Calendar',
    'com.apple.reminders': 'Reminders',
    'com.apple.facetime': 'FaceTime',
    'com.apple.mobilephone': 'Phone',
    'com.tinyspeck.slackmacgap': 'Slack',
    'com.hnc.Discord': 'Discord',
    'net.whatsapp.WhatsApp': 'WhatsApp',
    'com.google.Gmail': 'Gmail',
    'com.burbn.instagram': 'Instagram',
    'com.atebits.Tweetie2': 'X',
    'com.spotify.client': 'Spotify',
    'com.apple.Home': 'Home',
}

lock = threading.Lock()
pushed = deque(maxlen=60)
_db_cache = None


def friendly_app(identifier):
    if not identifier:
        return 'Notification'
    if identifier in APP_NAMES:
        return APP_NAMES[identifier]
    tail = identifier.rsplit('.', 1)[-1]
    return tail.replace('-', ' ').replace('_', ' ').strip() or identifier


def _candidate_dbs():
    paths = [Path.home() / 'Library' / 'Group Containers' / 'group.com.apple.usernoted' / 'db2' / 'db']
    try:
        user_dir = subprocess.run(['getconf', 'DARWIN_USER_DIR'], capture_output=True, text=True, timeout=1).stdout.strip()
        if user_dir:
            paths.append(Path(user_dir) / 'com.apple.notificationcenter' / 'db2' / 'db')
    except (OSError, subprocess.SubprocessError):
        pass
    return [path for path in paths if path.exists()]


def _parse_plist_record(blob, fallback_app, delivered_ts):
    if not blob:
        return None
    try:
        payload = plistlib.loads(blob)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    req = payload.get('req') if isinstance(payload.get('req'), dict) else {}
    app_id = payload.get('app') or fallback_app or ''
    title = str(req.get('titl') or '').strip()
    subtitle = str(req.get('subt') or '').strip()
    body = str(req.get('body') or '').strip()
    if not (title or subtitle or body):
        return None
    ts = float(delivered_ts or 0)
    if ts and ts < 1_000_000_000:
        ts += APPLE_EPOCH
    return {
        'app': friendly_app(app_id),
        'bundle': app_id,
        'title': title[:120],
        'subtitle': subtitle[:120],
        'body': ' '.join(body.split())[:220],
        'at': ts or time.time(),
        'source': 'mac',
    }


def _read_mac_db(since=None, limit=15):
    global _db_cache
    candidates = [_db_cache] if _db_cache and _db_cache.exists() else _candidate_dbs()
    last_error = None
    for db_path in candidates:
        try:
            con = sqlite3.connect(f'file:{db_path}?mode=ro&immutable=1', uri=True, timeout=1.0)
            try:
                rows = con.execute(
                    'select r.data, coalesce(a.identifier, ""), r.delivered_date '
                    'from record r left join app a on a.app_id = r.app_id '
                    'order by r.delivered_date desc limit ?',
                    (max(limit * 3, 40),),
                ).fetchall()
            finally:
                con.close()
            _db_cache = db_path
            items = []
            for blob, app_id, delivered in rows:
                parsed = _parse_plist_record(blob, app_id, delivered)
                if not parsed:
                    continue
                if since and parsed['at'] < since:
                    continue
                items.append(parsed)
                if len(items) >= limit:
                    break
            return items, None
        except (sqlite3.Error, OSError) as error:
            last_error = str(error)
    return [], last_error


def push(app, title='', body='', *, subtitle='', at=None):
    """Record a notification pushed from a paired phone (e.g., iOS Shortcut -> POST /api/notify)."""
    item = {
        'app': str(app or 'Phone').strip()[:60],
        'bundle': '',
        'title': str(title or '').strip()[:120],
        'subtitle': str(subtitle or '').strip()[:120],
        'body': ' '.join(str(body or '').split())[:220],
        'at': float(at) if at else time.time(),
        'source': 'push',
    }
    if not (item['title'] or item['body']):
        return False
    with lock:
        pushed.append(item)
    return True


def recent(since=None, limit=12):
    """Return newest-first merged Mac + pushed notifications since timestamp."""
    mac_items, _ = _read_mac_db(since=since, limit=limit)
    with lock:
        push_items = [item for item in pushed if not since or item['at'] >= since]
    merged = sorted(mac_items + push_items, key=lambda item: item['at'], reverse=True)
    return merged[:limit]


def summary(since=None, limit=6):
    """Compact plain-text summary suitable for BITCH briefings and wake-up recaps."""
    items = recent(since=since, limit=limit)
    if not items:
        return ''
    parts = []
    for item in items:
        lead = f"{item['app']}"
        if item['title']:
            lead += f" ({item['title']})"
        detail = item['body'] or item['subtitle']
        parts.append(f'{lead}: {detail}' if detail else lead)
    return ' | '.join(parts)


def status():
    """Report whether Mac notification access is active for the Settings screen."""
    items, error = _read_mac_db(limit=1)
    with lock:
        pushed_count = len(pushed)
    if error is None:
        return {'available': True, 'source': 'mac', 'pushed': pushed_count,
                'detail': 'Reading Mac & synced phone notifications'}
    return {
        'available': pushed_count > 0,
        'source': 'push' if pushed_count else 'none',
        'pushed': pushed_count,
        'detail': 'Grant Full Disk Access to BITCH in System Settings to read Mac/phone notifications',
    }
