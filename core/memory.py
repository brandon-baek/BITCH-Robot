"""Retain pairing records in the existing SQLite database."""
import json
import re
import time
import sqlite3
import threading

from . import config

lock = threading.Lock()
database = sqlite3.connect(config.HOME / 'bitch.db', check_same_thread=False)
database.execute('create table if not exists kv(k text primary key, v text)')


def kv_get(key, default=None):
    with lock:
        row = database.execute('select v from kv where k=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default


def kv_set(key, value):
    with lock, database:
        database.execute('insert into kv(k,v) values(?,?) on conflict(k) do update set v=excluded.v',
                         (key, json.dumps(value)))


database.execute('create table if not exists personal_notes(owner text, note text collate nocase, added real, primary key(owner, note))')


def remember(owner, note):
    note = ' '.join(note.split()).rstrip(' .!?')
    if not owner or not 3 <= len(note) <= 240:
        return False
    with lock, database:
        database.execute('insert or replace into personal_notes values (?,?,?)', (owner, note, time.time()))
        database.execute('delete from personal_notes where owner=? and rowid not in '
                         '(select rowid from personal_notes where owner=? order by added desc limit 80)', (owner, owner))
    return True


def recall(owner):
    if not owner:
        return []
    with lock:
        return [row[0] for row in database.execute(
            'select note from personal_notes where owner=? order by added desc', (owner,))]


def forget(owner, query):
    query = query.strip().casefold()
    if not owner or not query:
        return 0
    matches = [note for note in recall(owner) if query == 'everything' or query in note.casefold()]
    with lock, database:
        database.executemany('delete from personal_notes where owner=? and note=?', ((owner, note) for note in matches))
    return len(matches)


def relevant(owner, query):
    words = set(re.findall(r"[a-z]{3,}", query.lower())) - {
        'what', 'that', 'this', 'the', 'for', 'can', 'could', 'would', 'remember', 'did', 'about', 'was', 'you',
    }
    matching = [(len(words & set(re.findall(r"[a-z]{3,}", note.lower()))), note) for note in recall(owner)]
    return [note for score, note in sorted(matching, reverse=True) if score][:4]
