"""Unresolved UI intents only: never schedule sends or serve message history."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path


class IntentJournal:
    def __init__(self, directory: Path, principal: str):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.is_symlink():
            raise ValueError('GUI state directory must not be a symlink')
        if os.name != 'nt' and directory.stat().st_mode & 0o077:
            raise ValueError('GUI state directory must be owner-only')
        scope = hashlib.sha256(principal.encode()).hexdigest()
        self.path = directory / f'intents-{scope}.sqlite'
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.close(descriptor)
        if os.name != 'nt' and self.path.stat().st_mode & 0o077:
            raise ValueError('GUI intent file must be owner-only')
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS intents (
                key TEXT PRIMARY KEY, session TEXT NOT NULL, target TEXT NOT NULL,
                conversation TEXT NOT NULL, digest TEXT NOT NULL, text TEXT,
                status TEXT NOT NULL)''')

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            with db:
                yield db

    @staticmethod
    def digest(session, target, conversation, text):
        return hashlib.sha256(json.dumps(
            [session, target, conversation, text], ensure_ascii=False,
            separators=(',', ':'),
        ).encode()).hexdigest()

    def begin(self, session, target, conversation, request):
        """Return False on replay. Persist before any call with side effects."""
        key, text = request['idempotencyKey'], request['text']
        digest = self.digest(session, target, conversation, text)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT digest FROM intents WHERE key=?', (key,)).fetchone()
            if row:
                if row['digest'] != digest:
                    raise ValueError('request key is already bound to different content')
                return False
            if db.execute("SELECT 1 FROM intents WHERE session=? AND status='unknown'",
                          (session,)).fetchone():
                raise ValueError('session already has an unresolved send')
            if db.execute("SELECT COUNT(*) FROM intents WHERE status='unknown'").fetchone()[0] >= 100:
                raise ValueError('too many unresolved GUI intents')
            if db.execute('SELECT COUNT(*) FROM intents').fetchone()[0] >= 100000:
                raise ValueError('GUI intent journal capacity reached')
            db.execute('INSERT INTO intents VALUES (?,?,?,?,?,?,?)',
                       (key, session, target, conversation, digest, text, 'unknown'))
        return True

    def resolved(self, key, session, target, conversation, text):
        digest = self.digest(session, target, conversation, text)
        with self.connect() as db:
            # Scrub body after daemon evidence. Keep only an intent tombstone to
            # prevent delayed browser retries from executing the write again.
            db.execute("UPDATE intents SET status='accepted', text=NULL WHERE key=? AND digest=?",
                       (key, digest))

    def rejected(self, key):
        with self.connect() as db:
            db.execute("UPDATE intents SET status='rejected', text=NULL WHERE key=?", (key,))

    def pending(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM intents WHERE status='unknown' ORDER BY rowid").fetchall()
        return [dict(row) for row in rows]
