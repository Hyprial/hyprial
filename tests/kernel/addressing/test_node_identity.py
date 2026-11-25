"""One node-id reader for every product surface: env override, else home file."""

from __future__ import annotations

import logging
import socket
from pathlib import Path

import pytest

from hyprial.kernel import is_device_id_segment, resolve_node_id
from hyprial.kernel.impl.configuration.home import initialize_hyprial_home
from hyprial.kernel.impl.primitives.node_identity import (
    NodeIdNotConfigured,
    mint_node_id,
    node_id_path,
)


def _home(tmp_path: Path, content: str | None) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    if content is not None:
        node_id_path(home).write_text(content, encoding="utf-8")
    return {"HYPRIAL_HOME": str(home)}


def test_override_wins_over_the_file(tmp_path: Path) -> None:
    environ = _home(tmp_path, "d.from-file\n")
    environ["HYPRIAL_NODE_ID"] = " node-a "
    assert resolve_node_id(environ) == "node-a"


def test_file_value_is_returned_verbatim_and_opaque(tmp_path: Path) -> None:
    # An existing install writes its old host-derived value once; from then
    # on it is an opaque string, compared with nothing.
    assert resolve_node_id(_home(tmp_path, "old-host.example\n")) == "old-host.example"


def test_explicit_empty_override_warns_once_and_reads_the_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    environ = _home(tmp_path, "d.from-file")
    environ["HYPRIAL_NODE_ID"] = " \t "
    with caplog.at_level(logging.WARNING):
        resolved = resolve_node_id(environ)

    assert resolved == "d.from-file"
    assert [record.getMessage() for record in caplog.records] == [
        "HYPRIAL_NODE_ID is empty after trimming; ignoring it and reading "
        "the home's node-id file"
    ]


@pytest.mark.parametrize(("content", "reason"), [(None, "is missing"), (" \n", "is empty")])
def test_missing_or_empty_file_refuses_and_never_reads_the_host_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str | None, reason: str
) -> None:
    def forbidden() -> str:
        raise AssertionError("the host name must never be read for identity")

    monkeypatch.setattr(socket, "gethostname", forbidden)
    with pytest.raises(NodeIdNotConfigured, match=reason) as caught:
        resolve_node_id(_home(tmp_path, content))

    assert caught.value.code == "NODE_ID_NOT_CONFIGURED"
    assert "migration-im-push-by-agent-binding.md" in str(caught.value)
    assert isinstance(caught.value, ValueError)


def test_mint_writes_a_device_shaped_id_and_never_replaces(tmp_path: Path) -> None:
    value = mint_node_id(tmp_path)

    assert is_device_id_segment(value)
    assert node_id_path(tmp_path).read_text(encoding="utf-8") == value + "\n"
    with pytest.raises(FileExistsError):
        mint_node_id(tmp_path)


def test_init_mints_only_into_a_new_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fresh = tmp_path / "fresh"
    monkeypatch.setenv("HYPRIAL_HOME", str(fresh))
    initialize_hyprial_home(ensure_dispatch_policy=lambda _path: None)
    minted = node_id_path(fresh).read_text(encoding="utf-8").strip()
    assert is_device_id_segment(minted)

    # Re-running init keeps the value.
    initialize_hyprial_home(ensure_dispatch_policy=lambda _path: None)
    assert node_id_path(fresh).read_text(encoding="utf-8").strip() == minted

    # An existing home without the file is not converted: the operator
    # writes its old value (migration guide), init never invents one.
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "settings.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HYPRIAL_HOME", str(existing))
    initialize_hyprial_home(ensure_dispatch_policy=lambda _path: None)
    assert not node_id_path(existing).exists()
