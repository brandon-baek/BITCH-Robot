"""Lightweight, Pi-native persistent memory (SQLite + FTS5, stdlib only).

Runs in < 2 ms on a Raspberry Pi 4 with zero GPU / Ollama dependencies so BITCH keeps full
long-term memory across Temporary Chats on ChatGPT and Gemini — even when the Pi runs standalone.
"""
from difflib import SequenceMatcher
from pathlib import Path
import os
import re
import sqlite3
import threading
import time

PI_HOME = Path(os.environ.get('BITCH_PI_HOME', '~/.bitch-pi')).expanduser()
DB_PATH = PI_HOME / 'memory.db'

_lock = threading.Lock()
_db = None

SENSITIVE = re.compile(r'\b(?:password|passcode|pin code|ssn|social security|credit card|cvv|api ?key|secret key|token)\b', re.I)
STOP = {'the', 'and', 'for', 'that', 'this', 'with', 'from', 'have', 'been', 'were', 'they', 'them', 'your',
        'what', 'when', 'where', 'how', 'who', 'why', 'about', 'into', 'just', 'like', 'some', 'very', 'really'}


def _conn():
    global _db
    if _db is None:
        PI_HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
        _db = sqlite3.connect(DB_PATH, check_same_thread=False)
        _db.executescript("""
            create table if not exists memories(
              id integer primary key,
              t real not null,
              kind text not null default 'fact',
              about text not null default '',
              text text not null,
              importance integer not null default 3,
              last_used real default 0,
              uses integer not null default 0
            );
            create virtual table if not exists memories_fts using fts5(
              text, about, content='memories', content_rowid='id'
            );
            create trigger if not exists pi_mem_ai after insert on memories begin
              insert into memories_fts(rowid, text, about) values (new.id, new.text, new.about);
            end;
            create trigger if not exists pi_mem_ad after delete on memories begin
              insert into memories_fts(memories_fts, rowid, text, about) values('delete', old.id, old.text, old.about);
            end;
            create trigger if not exists pi_mem_au after update of text, about on memories begin
              insert into memories_fts(memories_fts, rowid, text, about) values('delete', old.id, old.text, old.about);
              insert into memories_fts(rowid, text, about) values (new.id, new.text, new.about);
            end;
        """)
    return _db


def _similar(a, b):
    return SequenceMatcher(None, a.casefold(), b.casefold(), autojunk=False).ratio()


def remember(owner, text, *, kind='fact', importance=3):
    """Save a fact or episode on the Pi, deduplicating near-duplicates and rejecting secrets."""
    owner = (owner or 'Brandon').strip()[:40]
    clean = ' '.join((text or '').split()).rstrip(' .!?')
    if not 4 <= len(clean) <= 280 or SENSITIVE.search(clean):
        return False
    now = time.time()
    with _lock:
        db = _conn()
        with db:
            rows = db.execute(
                'select id, text from memories where kind=? and about=? order by t desc limit 200',
                (kind, owner),
            ).fetchall()
            for row_id, existing in rows:
                if existing.casefold() == clean.casefold() or (kind == 'fact' and _similar(clean, existing) >= .72):
                    db.execute(
                        'update memories set text=?, t=?, importance=max(importance, ?) where id=?',
                        (clean, now, importance, row_id),
                    )
                    return True
            db.execute(
                'insert into memories(t, kind, about, text, importance, last_used) values (?, ?, ?, ?, ?, ?)',
                (now, kind, owner, clean, importance, now),
            )
    return True


def forget(owner, query):
    """Delete matching memories for `owner` (or all if query == 'everything')."""
    owner = (owner or 'Brandon').strip()
    q = (query or '').strip().casefold()
    if not q:
        return 0
    with _lock:
        db = _conn()
        with db:
            if q == 'everything':
                return db.execute('delete from memories where about=?', (owner,)).rowcount
            rows = db.execute('select id, text from memories where about=?', (owner,)).fetchall()
            drop = [rid for rid, txt in rows if q in txt.casefold() or _similar(q, txt) >= .65]
            if drop:
                db.executemany('delete from memories where id=?', [(rid,) for rid in drop])
            return len(drop)


def recall(owner, query='', *, limit=8):
    """Retrieve the most relevant facts + recent episodes for `owner` using FTS5 + recency/importance."""
    owner = (owner or 'Brandon').strip()
    now = time.time()
    tokens = [w for w in re.findall(r'[a-zA-Z0-9]{3,}', (query or '').casefold()) if w not in STOP]
    fts_ids = {}
    with _lock:
        db = _conn()
        if tokens:
            fts_query = ' OR '.join(f'"{t}"' for t in tokens[:8])
            try:
                for rank, (rid,) in enumerate(db.execute(
                    'select rowid from memories_fts where memories_fts match ? order by bm25(memories_fts) limit 12',
                    (fts_query,),
                )):
                    fts_ids[rid] = max(0., 3.0 - rank * .25)
            except sqlite3.OperationalError:
                pass
        rows = db.execute(
            'select id, t, kind, text, importance, uses from memories where about=? order by t desc limit 120',
            (owner,),
        ).fetchall()
        scored = []
        for rid, t_val, kind, txt, imp, uses in rows:
            age_days = max(0., (now - t_val) / 86400.)
            recency = 1.5 / (1. + age_days / 14.)
            score = fts_ids.get(rid, 0.) + imp * .45 + recency + min(.8, uses * .1)
            scored.append((score, rid, kind, txt))
        scored.sort(reverse=True)
        picked = scored[:limit]
        if picked:
            with db:
                db.executemany('update memories set uses=uses+1, last_used=? where id=?',
                               [(now, rid) for _, rid, _, _ in picked])
    return [{'id': rid, 'kind': kind, 'text': txt} for _, rid, kind, txt in picked]


def context(owner, query='', *, limit=6):
    """Compact one-line memory note ready to inject into a fresh Temporary Chat primer."""
    items = recall(owner, query, limit=limit)
    if not items:
        return ''
    facts = [it['text'] for it in items if it['kind'] == 'fact']
    episodes = [it['text'] for it in items if it['kind'] == 'episode']
    parts = []
    if facts:
        parts.append('Known facts about ' + (owner or 'them') + ': ' + '; '.join(facts))
    if episodes:
        parts.append('Earlier chats: ' + '; '.join(episodes[:2]))
    return ' | '.join(parts)


def everything(limit=50):
    with _lock:
        rows = _conn().execute(
            'select id, t, kind, about, text, importance from memories order by t desc limit ?', (limit,)
        ).fetchall()
    return [{'id': r[0], 't': r[1], 'kind': r[2], 'about': r[3], 'text': r[4], 'importance': r[5]} for r in rows]


def delete_id(row_id):
    with _lock:
        db = _conn()
        with db:
            return db.execute('delete from memories where id=?', (int(row_id),)).rowcount
