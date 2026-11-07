"""The local operator CLI against the real durable authority.

The command runs in-process but every layer below it is the production one:
``admin.main`` opens the SQLite store, mints the code, and prints JSON.  The
pairing code appears once on stdout and nowhere in the database bytes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.shell.impl.gui.adapter.mobile import MobileAccessStore
from hyprial.shell.impl.gui.adapter.mobile.admin import main
from tests.shell.gui.adapter.mobile.helpers import NODE, PEER_A


def _run(db: Path, *argv: str, capsys) -> tuple[int, object]:
    code = main(
        ["--db", str(db), "--node-id", NODE, "--operator", "operator:test", *argv]
    )
    raw = capsys.readouterr().out.strip()
    return code, json.loads(raw) if raw else None


def test_the_operator_cli_runs_pair_approve_list_and_revoke(
    tmp_path: Path, capsys
) -> None:
    db = tmp_path / "access.sqlite3"
    code, issued = _run(db, "pair", "issue", capsys=capsys)
    assert code == 0
    pairing_code = issued["code"]
    assert pairing_code
    assert pairing_code.encode() not in db.read_bytes()

    with MobileAccessStore(db, node_id=NODE) as store:
        request = store.redeem_pairing_code(
            code=pairing_code,
            device_id="dev-a",
            device_uid="uid-a",
            name="mate phone",
            owner="agent:owner",
            client_public=PEER_A,
        )

    code, inspected = _run(
        db, "pair", "inspect", request["requestId"], capsys=capsys
    )
    assert code == 0
    assert inspected["status"] == "pending"
    assert inspected["fingerprint"] == request["fingerprint"]

    code, approved = _run(
        db,
        "pair",
        "approve",
        request["requestId"],
        "--fingerprint",
        request["fingerprint"],
        "--device-id",
        "dev-a",
        "--client-public",
        PEER_A,
        capsys=capsys,
    )
    assert code == 0
    assert approved["status"] == "active"

    code, devices = _run(db, "device", "list", capsys=capsys)
    assert code == 0
    assert [row["deviceId"] for row in devices] == ["dev-a"]

    code, revoked = _run(
        db, "device", "revoke", "dev-a", "--reason", "operator-test", capsys=capsys
    )
    assert code == 0
    assert revoked["keyGeneration"] == 1

    code, devices = _run(db, "device", "list", capsys=capsys)
    assert code == 0
    assert devices[0]["status"] == "revoked"


def test_the_operator_cli_reports_a_refusal_without_a_traceback(
    tmp_path: Path, capsys
) -> None:
    code, answer = _run(
        tmp_path / "access.sqlite3",
        "pair",
        "inspect",
        "pair-missing",
        capsys=capsys,
    )

    assert code == 1
    assert answer["ok"] is False
    assert answer["error"]["reason"] == "pairing-request-unknown"


def test_the_operator_label_is_required_and_never_read_from_the_host_login(
    tmp_path: Path,
) -> None:
    with pytest.raises(SystemExit) as refused:
        main(
            [
                "--db",
                str(tmp_path / "access.sqlite3"),
                "--node-id",
                NODE,
                "device",
                "list",
            ]
        )

    assert refused.value.code == 2, "there is no implicit operator fallback"
