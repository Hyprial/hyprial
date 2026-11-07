"""RED-first tests for resolving a host login into an agent credential source.

The command reads the operator's own harness files, so every refusal here is a
security-shaped refusal: a symlink, a directory, or an empty file must stop the
command before anything is sent to the daemon.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.kernel import ipc_errors

from hyprial.shell.impl.cli.commands.agent.provider import (
    PROVIDER_SOURCE_FILES,
    ProviderSourceError,
    resolve_provider_sources,
)

PAYLOAD = b'{"token": "test-only"}\n'


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "operator-home"
    home.mkdir()
    return home


def _write(home: Path, relative: str, content: bytes = PAYLOAD) -> Path:
    path = home / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_resolves_the_documented_source_for_each_provider(tmp_path: Path) -> None:
    home = _home(tmp_path)

    claude = _write(home, ".claude/.credentials.json")
    codex = _write(home, ".codex/auth.json")
    pi_auth = _write(home, ".pi/agent/auth.json")
    pi_store = _write(home, ".pi/agent/models-store.json")

    assert resolve_provider_sources("claude", home=home) == (claude,)
    assert resolve_provider_sources("codex", home=home) == (codex,)
    # pi needs both files: an empty pair was the 2026-10-13 autodl failure.
    assert resolve_provider_sources("pi", home=home) == (pi_auth, pi_store)
    assert PROVIDER_SOURCE_FILES["pi"] == (
        ".pi/agent/auth.json",
        ".pi/agent/models-store.json",
    )


def test_unknown_provider_is_refused(tmp_path: Path) -> None:
    home = _home(tmp_path)

    with pytest.raises(ProviderSourceError) as refused:
        resolve_provider_sources("not-a-provider", home=home)

    assert refused.value.code == "PROVIDER_UNSUPPORTED"


def test_missing_empty_symlinked_and_non_regular_sources_are_refused(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)

    with pytest.raises(ProviderSourceError) as missing:
        resolve_provider_sources("claude", home=home)
    assert missing.value.code == ipc_errors.PROVIDER_SOURCE_MISSING

    _write(home, ".claude/.credentials.json", b"")
    with pytest.raises(ProviderSourceError) as empty:
        resolve_provider_sources("claude", home=home)
    assert empty.value.code == ipc_errors.PROVIDER_SOURCE_MISSING

    outside = tmp_path / "outside.json"
    outside.write_bytes(PAYLOAD)
    link = home / ".claude/.credentials.json"
    link.unlink()
    link.symlink_to(outside)
    with pytest.raises(ProviderSourceError) as linked:
        resolve_provider_sources("claude", home=home)
    assert linked.value.code == ipc_errors.PROVIDER_UNSAFE_PATH

    link.unlink()
    link.mkdir()
    with pytest.raises(ProviderSourceError) as directory:
        resolve_provider_sources("claude", home=home)
    assert directory.value.code == ipc_errors.PROVIDER_UNSAFE_PATH


def test_source_flag_names_one_file_and_only_for_single_file_providers(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)

    chosen = tmp_path / "chosen.json"
    chosen.write_bytes(PAYLOAD)
    assert resolve_provider_sources("codex", home=home, source=chosen) == (chosen,)

    # pi reads two files, so one --source cannot name its set.
    with pytest.raises(ProviderSourceError) as ambiguous:
        resolve_provider_sources("pi", home=home, source=chosen)
    assert ambiguous.value.code == "PROVIDER_SOURCE_AMBIGUOUS"
