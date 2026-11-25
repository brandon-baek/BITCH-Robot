"""Retain pairing records in the existing SQLite database."""
import json
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
