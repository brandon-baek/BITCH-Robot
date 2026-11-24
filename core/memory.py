"""BITCH's memory: one SQLite file, ~/.bitch/bitch.db. Open it with any SQLite viewer if you're curious.

  messages   everything said, from any device
  memories   facts, people, plans, feedback and diary entries, with an embedding so it can recall by meaning
  tasks      timers, reminders and routines on a schedule
  lists      shopping list, to-do list, any list you ask for
  skills     moves and routines it invented or you taught it
  activity   things it did on its own, and why (shown in the app so you can say "don't do that")
  kv         small bits of state (mood, paired phones, devices)
"""
import json, math, os, re, sqlite3, threading, time
import numpy as np
from . import config

DB = os.path.join(config.HOME, 'bitch.db')
_lock = threading.RLock()
_c = sqlite3.connect(DB, check_same_thread=False)
_c.row_factory = sqlite3.Row
_c.executescript("""
pragma journal_mode=wal;
create table if not exists messages(id integer primary key, t real, role text, text text, device text, emotion text, move text, meta text);
create table if not exists memories(id integer primary key, t real, kind text, about text, text text, importance integer default 3,
                                    last_used real, uses integer default 0, emb blob);
create virtual table if not exists memories_fts using fts5(text, about, content='memories', content_rowid='id');
create trigger if not exists mem_ai after insert on memories begin
  insert into memories_fts(rowid,text,about) values (new.id,new.text,new.about); end;
create trigger if not exists mem_ad after delete on memories begin
  insert into memories_fts(memories_fts,rowid,text,about) values('delete',old.id,old.text,old.about); end;
create trigger if not exists mem_au after update of text,about on memories begin
  insert into memories_fts(memories_fts,rowid,text,about) values('delete',old.id,old.text,old.about);
  insert into memories_fts(rowid,text,about) values (new.id,new.text,new.about); end;
create table if not exists tasks(id integer primary key, t real, kind text, text text, due real, repeat text, status text default 'waiting', meta text);
create table if not exists lists(id integer primary key, t real, list text, item text);
create table if not exists skills(name text primary key, t real, kind text, description text, body text, uses integer default 0, last real);
create table if not exists activity(id integer primary key, t real, kind text, text text, why text, feedback text);
create table if not exists kv(k text primary key, v text);
""")


def q(sql, *a):
    with _lock:
        return [dict(r) for r in _c.execute(sql, a).fetchall()]


def x(sql, *a):
    with _lock:
        cur = _c.execute(sql, a); _c.commit(); return cur.lastrowid


# ---------------- small state ----------------
def kv_get(k, default=None):
    r = q('select v from kv where k=?', k)
    return json.loads(r[0]['v']) if r else default


def kv_set(k, v):
    x('insert into kv(k,v) values(?,?) on conflict(k) do update set v=excluded.v', k, json.dumps(v))


# ---------------- conversation ----------------
def add_message(role, text, device=None, emotion=None, move=None, **meta):
    t = time.time()
    i = x('insert into messages(t,role,text,device,emotion,move,meta) values(?,?,?,?,?,?,?)',
          t, role, text, device, emotion, move, json.dumps(meta) if meta else None)
    return dict(id=i, t=t, role=role, text=text, device=device, emotion=emotion, move=move, **meta)


def recent_messages(n=40, since=None, roles=None):
    sql = 'select * from messages' + (' where t>?' if since else '') + ' order by id desc limit ?'
    rows = q(sql, *([since] if since else []), n)
    out = []
    for r in reversed(rows):
        if roles and r['role'] not in roles: continue
        m = json.loads(r.pop('meta') or '{}'); r.update(m); out.append(r)
    return out


# ---------------- long-term memories ----------------
def _emb(text, kind):
    try:
        from . import llm
        return llm.embed(text, kind)
    except Exception:
        return None


def remember(text, kind='fact', about=None, importance=3):
    """Save something worth keeping. If it already knows almost the same thing, it updates that instead."""
    text = re.sub(r'\s+', ' ', str(text)).strip()[:400]
    if not text: return None
    e = _emb(text, 'doc')
    if e is not None:
        best, sim = None, 0.0
        for r in q('select id, emb, importance from memories where kind=? and emb is not null', kind):
            s = float(np.dot(e, np.frombuffer(r['emb'], np.float32)))
            if s > sim: best, sim = r, s
        if best and sim > 0.93:
            x('update memories set text=?, t=?, emb=?, importance=max(importance,?) where id=?',
              text, time.time(), e.tobytes(), int(importance), best['id'])
            return dict(id=best['id'], text=text, updated=True)
    i = x('insert into memories(t,kind,about,text,importance,emb) values(?,?,?,?,?,?)',
          time.time(), kind, about, text, int(importance), None if e is None else e.tobytes())
    return dict(id=i, text=text, updated=False)


def recall(query, k=6, kinds=None, min_score=0.5):
    """The memories that matter most for `query`: close in meaning, keyword hits, important, recent."""
    rows = q('select id,t,kind,about,text,importance,emb from memories' +
             (' where kind in (%s)' % ','.join('?' * len(kinds)) if kinds else ''), *(kinds or []))
    if not rows: return []
    qe = _emb(query, 'query')
    words = [w for w in re.findall(r'[a-z0-9]{3,}', query.lower()) if w not in _STOP]
    hits = set()
    if words:
        try: hits = {r['rowid'] for r in q('select rowid from memories_fts where memories_fts match ? limit 30', ' OR '.join(words))}
        except Exception: pass
    now = time.time(); scored = []
    for r in rows:
        s = 0.0
        if qe is not None and r['emb']: s += float(np.dot(qe, np.frombuffer(r['emb'], np.float32)))
        elif qe is None: s += 0.4
        if r['id'] in hits: s += 0.12
        s += 0.02 * r['importance'] + 0.04 * math.exp(-(now - r['t']) / 86400 / 30)
        scored.append((s, r))
    scored.sort(key=lambda z: -z[0])
    out = [r for s, r in scored[:k] if s >= min_score or r['id'] in hits]
    for r in out:
        r.pop('emb', None); x('update memories set last_used=?, uses=uses+1 where id=?', now, r['id'])
    return out


_STOP = set('the and you for are with that this what when where who how have has was your about from they them can will just like'.split())


def core_memories(n=8):
    """Things always worth knowing (importance 5), like who you are."""
    return q('select id,kind,about,text from memories where importance>=5 order by t desc limit ?', n)


def memories(kind=None, search=None, limit=500):
    sql, a = 'select id,t,kind,about,text,importance,uses from memories', []
    cond = []
    if kind: cond.append('kind=?'); a.append(kind)
    if search: cond.append('(text like ? or about like ?)'); a += ['%' + search + '%'] * 2
    if cond: sql += ' where ' + ' and '.join(cond)
    return q(sql + ' order by t desc limit ?', *a, limit)


def edit_memory(i, text=None, importance=None, about=None, kind=None):
    if text is not None:
        e = _emb(text, 'doc')
        x('update memories set text=?, emb=? where id=?', text, None if e is None else e.tobytes(), i)
    if importance is not None: x('update memories set importance=? where id=?', int(importance), i)
    if about is not None: x('update memories set about=? where id=?', about, i)
    if kind is not None: x('update memories set kind=? where id=?', kind, i)


def forget(i):
    x('delete from memories where id=?', i)


# ---------------- reminders, timers, routines ----------------
def add_task(kind, text, due, repeat=None, **meta):
    return x('insert into tasks(t,kind,text,due,repeat,meta) values(?,?,?,?,?,?)',
             time.time(), kind, text, due, repeat, json.dumps(meta))


def tasks(status='waiting'):
    rows = q('select * from tasks where status=? order by due', status)
    for r in rows: r['meta'] = json.loads(r['meta'] or '{}')
    return rows


def task_done(i, status='done', due=None):
    if due is not None: x('update tasks set due=? where id=?', due, i)
    else: x('update tasks set status=? where id=?', status, i)


# ---------------- lists ----------------
def list_add(name, items):
    name = name.strip().lower() or 'list'
    have = {r['item'].lower() for r in q('select item from lists where list=?', name)}
    added = [i for i in items if i.strip() and i.strip().lower() not in have]
    for i in added: x('insert into lists(t,list,item) values(?,?,?)', time.time(), name, i.strip())
    return added


def list_get(name):
    return [r['item'] for r in q('select item from lists where list=? order by id', name.strip().lower())]


def list_remove(name, items=None):
    name = name.strip().lower()
    if items is None: x('delete from lists where list=?', name); return
    for i in items: x('delete from lists where list=? and lower(item)=lower(?)', name, i.strip())


def list_names():
    return [r['list'] for r in q('select list, max(id) m from lists group by list order by m desc')]


# ---------------- skills (custom actions) ----------------
def save_skill(name, kind, description, body):
    x('insert into skills(name,t,kind,description,body) values(?,?,?,?,?) on conflict(name) do update set '
      't=excluded.t, kind=excluded.kind, description=excluded.description, body=excluded.body',
      name, time.time(), kind, description, json.dumps(body))


def skill(name):
    r = q('select * from skills where lower(name)=lower(?)', name)
    if not r: return None
    r = r[0]; r['body'] = json.loads(r['body']); return r


def skills():
    rows = q('select * from skills order by coalesce(last,t) desc')
    for r in rows: r['body'] = json.loads(r['body'])
    return rows


def skill_used(name):
    x('update skills set uses=uses+1, last=? where lower(name)=lower(?)', time.time(), name)


def delete_skill(name):
    x('delete from skills where lower(name)=lower(?)', name)


# ---------------- things it did on its own ----------------
def log_activity(kind, text, why=''):
    t = time.time(); i = x('insert into activity(t,kind,text,why) values(?,?,?,?)', t, kind, text, why)
    return dict(id=i, t=t, kind=kind, text=text, why=why, feedback=None)


def activity(n=60, since=None):
    if since: return q('select * from activity where t>? order by id desc limit ?', since, n)
    return q('select * from activity order by id desc limit ?', n)


def activity_feedback(i, fb):
    x('update activity set feedback=? where id=?', fb, i)
