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


def kv_keys(prefix):
    with lock:
        return [row[0] for row in database.execute(
            "select k from kv where substr(k, 1, ?) = ? order by k", (len(prefix), prefix))]


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


def keywords(text):
    generic = {'what', 'that', 'this', 'the', 'for', 'can', 'could', 'would', 'remember',
               'did', 'about', 'was', 'you', 'your', 'have', 'know', 'really', 'which',
               'favorite', 'favourite', 'prefer', 'like', 'love', 'been', 'with', 'tell', 'told',
               'yesterday', 'earlier', 'before', 'when', 'said', 'say'}
    return {word[:-1] if len(word) > 4 and word.endswith('s') and not word.endswith('ss') else word
            for word in re.findall(r"[a-z]{3,}", text.casefold()) if word not in generic}


def relevant(owner, query):
    words = keywords(query)
    matching = [(len(words & keywords(note)), note) for note in recall(owner)]
    return [note for score, note in sorted(matching, key=lambda row: row[0], reverse=True) if score][:4]


_PRIVATE = re.compile(r"\b(?:password|passcode|pin code|api key|account number|social security|credit card|"
                      r"diagnos\w*|allerg\w*|medication|prescription|bank|salary|lawsuit|home address)\b", re.I)
_PREFERENCE = re.compile(r"\b(?:i (?:definitely |usually |really )?(?:prefer|like|love|hate|dislike|always use|go by)|"
                         r"my (?:favorite|favourite|ongoing|default|go-to|nickname)|i(?:['’]m| am) (?:building|working on|learning|into)|"
                         r"(?:keep|hold) that (?:in mind|for later)|(?:might|will) ask you again)\b", re.I)


_CLEAR_PREFERENCE = re.compile(r"^I (?:definitely |usually )?prefer [^.?!]+[.]?$", re.I)


def remember_in_context(owner, text, suggestion):
    if not suggestion and _CLEAR_PREFERENCE.fullmatch(text.strip()):
        suggestion = text.strip()
    if not isinstance(suggestion, str) or not suggestion or len(suggestion) > 180:
        return False
    if '?' in text or _PRIVATE.search(text + ' ' + suggestion) or not _PREFERENCE.search(text):
        return False
    words = lambda line: set(re.findall(r"[a-z]{3,}", line.casefold()))
    proposed = words(suggestion) - {'prefer', 'like', 'love', 'favorite', 'favourite', 'building', 'working', 'learning'}
    return bool(proposed and len(proposed - words(text)) <= 1 and remember(owner, suggestion))
