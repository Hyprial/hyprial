"""Bounded SQLite images, never a path-based SQLite filesystem connection.

The caller holds the slot OS lock from safe image load through atomic fsync
publication. SQLite transactions run in memory; SQL COMMIT alone is not durable.
"""

from contextlib import contextmanager
import sqlite3

from ..runtime.schema import decode, encode, exact, fail

DATABASE = "store.sqlite3"
MAX_IMAGE_BYTES = 16 * 1024 * 1024


def _checkpoint(_name):
    """Publication durability boundaries for deterministic fault tests."""


class Store:
    def __init__(self, slot, ca_sha256):
        self.slot = slot
        self.ca_sha256 = ca_sha256
        self.parts = (*slot.state, "authority")

    def _preflight(self, directory):
        for name in (DATABASE, DATABASE + "-journal", DATABASE + "-wal", DATABASE + "-shm"):
            try:
                metadata = (directory / name).lstat()
            except FileNotFoundError:
                continue
            self.slot._file_safe(metadata, 0o600)
            # This implementation never creates disk-side SQLite journals.
            if name != DATABASE:
                fail("UNSAFE_AUTHORITY_STATE", "authority")

    @contextmanager
    def transaction(self):
        with self.slot.directory(self.parts, create=True) as directory:
            self._preflight(directory)
        marker_path = (*self.parts, "store.json")
        marker_raw = self.slot.read(marker_path)
        image = self.slot.read((*self.parts, DATABASE), maximum=MAX_IMAGE_BYTES)
        marker = {"version": "hyprial.tailnet.credential-authority-store.v1", "home": self.slot.identity,
                  "tailnetId": self.slot.tailnet_id, "caSha256": self.ca_sha256}
        if marker_raw is None:
            if image is not None:
                fail("UNSAFE_AUTHORITY_STATE", "authority")
            # If init crashes after this marker but before an image, refuse reset.
            self.slot.write(marker_path, encode(marker), exclusive=True)
        else:
            try:
                if exact(decode(marker_raw), marker.keys()) != marker or image is None:
                    raise ValueError("authority database mismatch")
            except (ValueError, TypeError, UnicodeError):
                fail("UNSAFE_AUTHORITY_STATE", "authority")
        connection = sqlite3.connect(":memory:")
        try:
            if not hasattr(connection, "serialize") or not hasattr(connection, "deserialize"):
                fail("UNSAFE_AUTHORITY_STATE", "authority")
            connection.row_factory = sqlite3.Row
            if image is None:
                connection.executescript("""
                    CREATE TABLE metadata(value BLOB NOT NULL);
                    CREATE TABLE bindings(binding_id TEXT PRIMARY KEY, device_id TEXT NOT NULL,
                        network BLOB NOT NULL, state TEXT NOT NULL, material BLOB NOT NULL);
                    CREATE TABLE current(device_id TEXT PRIMARY KEY, binding_id TEXT NOT NULL);
                    CREATE TABLE operations(device_id TEXT NOT NULL, operation_id TEXT NOT NULL,
                        route TEXT NOT NULL, request BLOB NOT NULL, network BLOB NOT NULL,
                        status INTEGER NOT NULL, response BLOB NOT NULL,
                        PRIMARY KEY(device_id, operation_id));
                    PRAGMA user_version=1;
                """)
                connection.execute("INSERT INTO metadata VALUES(?)", (encode(marker),))
                connection.commit()
                _checkpoint("BEFORE_INITIAL_IMAGE")
                self._publish(connection)
                _checkpoint("INITIALIZED")
            else:
                try:
                    connection.deserialize(image)
                except sqlite3.DatabaseError:
                    fail("UNSAFE_AUTHORITY_STATE", "authority")
            self._validate(connection, marker)
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            _checkpoint("BEFORE_COMMIT")
            connection.commit()
            _checkpoint("BEFORE_PUBLISH")
            self._publish(connection)
            _checkpoint("AFTER_COMMIT")
        finally:
            if connection.in_transaction:
                connection.rollback()
            connection.close()

    def _publish(self, connection):
        payload = connection.serialize()
        if len(payload) > MAX_IMAGE_BYTES:
            raise sqlite3.DataError("authority image capacity exceeded")
        with self.slot.directory(self.parts) as directory:
            self._preflight(directory)
        self.slot.write((*self.parts, DATABASE), payload)

    @staticmethod
    def _validate(connection, marker):
        try:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if (tables != {"metadata", "bindings", "current", "operations"}
                    or connection.execute("PRAGMA quick_check").fetchone()[0] != "ok"
                    or connection.execute("PRAGMA user_version").fetchone()[0] != 1
                    or [row[0] for row in connection.execute("SELECT value FROM metadata")] != [encode(marker)]):
                fail("UNSAFE_AUTHORITY_STATE", "authority")
        except sqlite3.DatabaseError:
            fail("UNSAFE_AUTHORITY_STATE", "authority")
