"""``hyprial transfer-trust-source-*``: the operator's offline enrollment surface.

A landing refuses every bundle whose key the target has not enrolled, and this
slice fetches no keys online, so these three commands are the only way a source
machine ever becomes acceptable.  What these tests pin is that surface: it acts
on the target's *own configured home*, the refusals carry the daemon's own
codes, and enrolling a key is an explicit act rather than a silent wildcard.
"""

from __future__ import annotations

import base64
import json
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.daemon.impl.transfer.archive.signature import (
    generate_signing_key,
    public_key_id,
    write_signing_key,
)
from hyprial.daemon.impl.transfer.archive.trust import (
    load_trust_store,
    trust_store_path,
)
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    return home


def _source_key_record(tmp_path: Path) -> tuple[Path, str, bytes]:
    """A source machine's key record, as a real source has it on disk."""

    private_bytes, public_bytes = generate_signing_key()
    record = tmp_path / "source-signing-key.json"
    write_signing_key(record, private_bytes, public_bytes)
    return record, public_key_id(public_bytes), public_bytes


def _invoke(*args: str, json_output: bool = True):
    argv = list(args)
    if json_output:
        argv.append("--json")
    return CliRunner().invoke(_cli_root_app, argv)


def test_add_enrolls_the_key_under_the_targets_own_home(
    home: Path, tmp_path: Path
) -> None:
    record, key_id, _public = _source_key_record(tmp_path)

    result = _invoke(
        "agent", "transfer", "trust-source-add",
        "--actor",
        "wk",
        "--source-machine",
        "laptop",
        "--public-key",
        str(record),
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["keyId"] == key_id
    assert payload["actor"] == "wk"
    assert payload["sourceMachine"] == "laptop"
    assert payload["keys"] == 1
    assert payload["trustStore"] == str(trust_store_path(home))

    store = load_trust_store(trust_store_path(home))
    assert [key.key_id for key in store.keys] == [key_id]
    # The anchor is the target's private material, not a published file.
    assert stat.S_IMODE(trust_store_path(home).stat().st_mode) == 0o600
    assert stat.S_IMODE(trust_store_path(home).parent.stat().st_mode) == 0o700


def test_add_accepts_the_bare_base64_key(home: Path, tmp_path: Path) -> None:
    _record, key_id, public_bytes = _source_key_record(tmp_path)
    inline = base64.b64encode(public_bytes).decode("ascii")

    result = _invoke(
        "agent", "transfer", "trust-source-add",
        "--actor",
        "wk",
        "--source-machine",
        "laptop",
        "--public-key",
        inline,
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["keyId"] == key_id


def test_re_enrolling_the_same_key_is_idempotent(home: Path, tmp_path: Path) -> None:
    record, key_id, _public = _source_key_record(tmp_path)
    args = (
        "agent", "transfer", "trust-source-add",
        "--actor",
        "wk",
        "--source-machine",
        "laptop",
        "--public-key",
        str(record),
    )

    assert _invoke(*args).exit_code == 0
    second = _invoke(*args)

    assert second.exit_code == 0, second.output
    assert json.loads(second.stdout)["keys"] == 1
    assert [key.key_id for key in load_trust_store(trust_store_path(home)).keys] == [
        key_id
    ]


def test_the_same_key_for_a_different_machine_is_refused(
    home: Path, tmp_path: Path
) -> None:
    record, _key_id, _public = _source_key_record(tmp_path)
    assert (
        _invoke(
            "agent", "transfer", "trust-source-add",
            "--actor",
            "wk",
            "--source-machine",
            "laptop",
            "--public-key",
            str(record),
        ).exit_code
        == 0
    )

    result = _invoke(
        "agent", "transfer", "trust-source-add",
        "--actor",
        "wk",
        "--source-machine",
        "desktop",
        "--public-key",
        str(record),
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "trust_store_key_conflict"


def test_an_asserted_key_id_that_does_not_match_is_refused(
    home: Path, tmp_path: Path
) -> None:
    record, key_id, _public = _source_key_record(tmp_path)

    result = _invoke(
        "agent", "transfer", "trust-source-add",
        "--actor",
        "wk",
        "--source-machine",
        "laptop",
        "--public-key",
        str(record),
        "--key-id",
        "ed25519:" + "0" * 32,
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "bundle_signature_malformed"
    assert key_id in payload["error"]
    # A refusal must not leave a half-written store behind.
    assert not trust_store_path(home).exists()


def test_a_public_key_that_is_not_a_key_is_refused(home: Path) -> None:
    result = _invoke(
        "agent", "transfer", "trust-source-add",
        "--actor",
        "wk",
        "--source-machine",
        "laptop",
        "--public-key",
        "not base64 at all!!",
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "bundle_signature_malformed"
    assert not trust_store_path(home).exists()


def test_list_on_a_fresh_target_is_empty_and_creates_nothing(home: Path) -> None:
    result = _invoke("agent", "transfer", "trust-source-list")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["keys"] == []
    assert payload["trustStore"] == str(trust_store_path(home))
    # "Trust nobody" is the default, and reading it must not mint the file.
    assert not trust_store_path(home).exists()


def test_list_after_add_shows_the_enrolled_key(home: Path, tmp_path: Path) -> None:
    record, key_id, _public = _source_key_record(tmp_path)
    assert (
        _invoke(
            "agent", "transfer", "trust-source-add",
            "--actor",
            "wk",
            "--source-machine",
            "laptop",
            "--public-key",
            str(record),
        ).exit_code
        == 0
    )

    payload = json.loads(_invoke("agent", "transfer", "trust-source-list").stdout)

    assert [key["keyId"] for key in payload["keys"]] == [key_id]
    assert payload["keys"][0]["retiredAt"] is None


def test_retire_records_the_line_and_keeps_the_key(home: Path, tmp_path: Path) -> None:
    record, key_id, _public = _source_key_record(tmp_path)
    assert (
        _invoke(
            "agent", "transfer", "trust-source-add",
            "--actor",
            "wk",
            "--source-machine",
            "laptop",
            "--public-key",
            str(record),
        ).exit_code
        == 0
    )

    result = _invoke("agent", "transfer", "trust-source-retire", "--key-id", key_id)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["retiredAt"] is not None
    store = load_trust_store(trust_store_path(home))
    # Retirement is a record, never a delete: the audit needs the line.
    assert [key.key_id for key in store.keys] == [key_id]
    assert store.keys[0].retired_at is not None


def test_retiring_an_unknown_key_is_refused(home: Path) -> None:
    result = _invoke(
        "agent", "transfer", "trust-source-retire", "--key-id", "ed25519:" + "1" * 32
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "trust_store_unknown_key"
