from __future__ import annotations

import sqlite3
import subprocess

import pytest

from hyprial.daemon.impl.adapters.imessage import OsaScriptSender, SQLiteMessageReader


def _messages_db(path: object) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT NOT NULL);
        CREATE TABLE chat (
            ROWID INTEGER PRIMARY KEY,
            guid TEXT NOT NULL,
            chat_identifier TEXT NOT NULL,
            style INTEGER NOT NULL
        );
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY,
            guid TEXT NOT NULL,
            text TEXT,
            handle_id INTEGER,
            is_from_me INTEGER NOT NULL,
            date INTEGER NOT NULL,
            cache_has_attachments INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE chat_message_join (chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL);
        INSERT INTO handle VALUES (1, '+15550001');
        INSERT INTO chat VALUES (1, 'iMessage;-;+15550001', '+15550001', 45);
        INSERT INTO chat VALUES (2, 'iMessage;+;group', 'group', 43);
        INSERT INTO message VALUES (10, 'm-in', 'hello', 1, 0, 100, 0);
        INSERT INTO message VALUES (11, 'm-self', 'sent', 1, 1, 101, 0);
        INSERT INTO message VALUES (12, 'm-media', 'photo', 1, 0, 102, 1);
        INSERT INTO message VALUES (13, 'm-group', 'group', 1, 0, 103, 0);
        INSERT INTO chat_message_join VALUES (1, 10);
        INSERT INTO chat_message_join VALUES (1, 11);
        INSERT INTO chat_message_join VALUES (1, 12);
        INSERT INTO chat_message_join VALUES (2, 13);
        """
    )
    connection.commit()
    connection.close()


def test_sqlite_reader_opens_read_only_and_emits_only_text_dm_rows(tmp_path: object) -> None:
    path = getattr(tmp_path, "joinpath")("chat.db")
    _messages_db(path)
    reader = SQLiteMessageReader(path, test_path_authority=lambda candidate: candidate == path)
    rows = reader.read_after(0, limit=100)
    assert [(row.rowid, row.message_guid, row.is_from_me) for row in rows] == [
        (10, "m-in", False),
        (11, "m-self", True),
    ]
    assert all(row.chat_guid == "iMessage;-;+15550001" for row in rows)
    assert reader.connection_uri.endswith("?mode=ro")
    assert not getattr(path, "with_name")("chat.db-wal").exists()
    assert not getattr(path, "with_name")("chat.db-journal").exists()


def test_osascript_sender_uses_absolute_binary_argv_and_never_shell(monkeypatch) -> None:
    observed: dict[str, object] = {}
    monkeypatch.setattr(
        "hyprial.daemon.impl.adapters.imessage.io.shutil.which",
        lambda _command, *, path: "/usr/bin/osascript",
    )

    def runner(argv: list[str], **kwargs: object) -> object:
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    outcome = OsaScriptSender(runner=runner).send_text(
        "+15550001", 'hello "quoted"; do shell script "false"'
    )
    argv = observed["argv"]
    assert isinstance(argv, list)
    assert argv[0] == "/usr/bin/osascript"
    assert argv[-2:] == ["+15550001", 'hello "quoted"; do shell script "false"']
    kwargs = observed["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs.get("shell") is False
    assert outcome.state == "unknown"
    assert outcome.definitely_not_sent is False


def test_sqlite_reader_rejects_arbitrary_path_without_explicit_test_authority(
    tmp_path: object,
) -> None:
    path = getattr(tmp_path, "joinpath")("chat.db")
    _messages_db(path)
    with pytest.raises(ValueError, match="canonical"):
        SQLiteMessageReader(path)


def test_osascript_timeout_is_unknown_and_never_blindly_resent(monkeypatch) -> None:
    calls = 0
    monkeypatch.setattr(
        "hyprial.daemon.impl.adapters.imessage.io.shutil.which",
        lambda _command, *, path: "/usr/bin/osascript",
    )

    def runner(*_args: object, **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        raise subprocess.TimeoutExpired("/usr/bin/osascript", 30)

    outcome = OsaScriptSender(runner=runner).send_text("+15550001", "hello")
    assert calls == 1
    assert outcome.state == "unknown"
    assert outcome.definitely_not_sent is False
    assert outcome.reason_code == "IMESSAGE_APPLESCRIPT_TIMEOUT_UNKNOWN"


def test_missing_osascript_never_invokes_the_runner(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        "hyprial.daemon.impl.adapters.imessage.io.shutil.which",
        lambda _command, *, path: None,
    )
    outcome = OsaScriptSender(runner=lambda *args: calls.append(args)).send_text(
        "+15550001", "hello"
    )
    assert outcome.state == "failed-final"
    assert outcome.definitely_not_sent is True
    assert outcome.reason_code == "IMESSAGE_MACOS_REQUIRED"
    assert calls == []
