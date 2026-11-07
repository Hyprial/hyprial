"""Command-level tests for ``hyprial agent provider login``.

The daemon seam is stubbed, so these tests prove exactly what the CLI owns:
which source files it accepts, what bytes it sends, and that a refused source
never reaches the daemon at all.  Reading happens in this process by design, so
a source refusal that did send an IPC call would be a security regression.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.kernel import ipc_errors
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

PAYLOAD = b'{"claudeAiOauth": {"accessToken": "test-only"}}\n'


class _Captured:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def __call__(self, method: str, params: object = None, **_kwargs: object) -> object:
        self.calls.append((method, params))
        return {
            "ok": True,
            "provider": "claude",
            "actor": "worker",
            "files": [{"path": "/agent/home/x"}],
        }


@pytest.fixture
def operator_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "operator-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.delenv("HYPRIAL_WORKER_ACTOR", raising=False)
    return home


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch) -> _Captured:
    captured = _Captured()
    monkeypatch.setattr(_cli_support, "_daemon_request", captured)
    return captured


def _write(home: Path, relative: str, content: bytes = PAYLOAD) -> Path:
    path = home / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _invoke(*arguments: str):  # type: ignore[no-untyped-def]
    return CliRunner().invoke(_cli_root_app, ["agent", "provider", "login", *arguments])


def test_missing_source_is_refused_and_never_reaches_the_daemon(
    operator_home: Path, capture: _Captured
) -> None:
    result = _invoke("claude", "--actor", "worker", "--json")

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["code"] == ipc_errors.PROVIDER_SOURCE_MISSING
    assert capture.calls == []


def test_empty_source_is_refused_and_never_reaches_the_daemon(
    operator_home: Path, capture: _Captured
) -> None:
    _write(operator_home, ".claude/.credentials.json", b"")

    result = _invoke("claude", "--actor", "worker", "--json")

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["code"] == ipc_errors.PROVIDER_SOURCE_MISSING
    assert capture.calls == []


def test_symlinked_source_is_refused_and_never_reaches_the_daemon(
    operator_home: Path, capture: _Captured
) -> None:
    outside = operator_home.parent / "outside.json"
    outside.write_bytes(PAYLOAD)
    link = operator_home / ".claude/.credentials.json"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)

    result = _invoke("claude", "--actor", "worker", "--json")

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["code"] == ipc_errors.PROVIDER_UNSAFE_PATH
    assert capture.calls == []


def test_unknown_provider_is_refused_with_its_registry_code(
    operator_home: Path, capture: _Captured
) -> None:
    result = _invoke("not-a-provider", "--actor", "worker", "--json")

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["code"] == ipc_errors.PROVIDER_UNSUPPORTED
    assert capture.calls == []


def test_pi_refuses_a_single_source_override(
    operator_home: Path, capture: _Captured
) -> None:
    chosen = operator_home / "chosen.json"
    chosen.write_bytes(PAYLOAD)

    result = _invoke(
        "pi", "--actor", "worker", "--source", str(chosen), "--json"
    )

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["code"] == ipc_errors.PROVIDER_SOURCE_AMBIGUOUS
    assert capture.calls == []


def test_success_sends_the_document_under_agent_provider_login(
    operator_home: Path, capture: _Captured
) -> None:
    _write(operator_home, ".claude/.credentials.json")

    result = _invoke("claude", "--actor", "worker", "--json")

    assert result.exit_code == 0, result.output
    assert capture.calls == [
        (
            "agent.provider.login",
            {
                "actor": "worker",
                "provider": "claude",
                "files": [
                    {
                        "filename": ".credentials.json",
                        "contentBase64": base64.b64encode(PAYLOAD).decode("ascii"),
                    }
                ],
                "force": False,
            },
        )
    ]
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["provider"] == "claude"
    assert payload["actor"] == "worker"
    assert payload["files"] == [{"path": "/agent/home/x"}]


def test_force_is_forwarded(
    operator_home: Path, capture: _Captured
) -> None:
    _write(operator_home, ".claude/.credentials.json")

    result = _invoke("claude", "--actor", "worker", "--force", "--json")

    assert result.exit_code == 0, result.output
    _method, params = capture.calls[0]
    assert isinstance(params, dict)
    assert params["force"] is True


def test_pi_seeds_both_documented_files_in_one_call(
    operator_home: Path, capture: _Captured
) -> None:
    auth = _write(operator_home, ".pi/agent/auth.json", b'{"auth": 1}\n')
    store = _write(operator_home, ".pi/agent/models-store.json", b'{"store": 1}\n')

    result = _invoke("pi", "--actor", "worker", "--json")

    assert result.exit_code == 0, result.output
    [(method, sent)] = capture.calls
    assert method == "agent.provider.login"
    assert isinstance(sent, dict)
    assert sent["provider"] == "pi"
    assert sent["actor"] == "worker"
    assert sent["files"] == [
        {
            "filename": "auth.json",
            "contentBase64": base64.b64encode(auth.read_bytes()).decode(),
        },
        {
            "filename": "models-store.json",
            "contentBase64": base64.b64encode(store.read_bytes()).decode(),
        },
    ]


def test_pi_coding_agent_dir_relocates_both_sources(
    operator_home: Path, capture: _Captured, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = operator_home.parent / "pi-ci-agent"
    (configured).mkdir()
    (configured / "auth.json").write_bytes(b'{"auth": "ci"}\n')
    (configured / "models-store.json").write_bytes(b'{"store": "ci"}\n')
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(configured))

    result = _invoke("pi", "--actor", "worker", "--json")

    assert result.exit_code == 0, result.output
    [(_method, sent)] = capture.calls
    assert isinstance(sent, dict)
    assert [item["filename"] for item in sent["files"]] == [
        "auth.json",
        "models-store.json",
    ]
    assert sent["files"][0]["contentBase64"] == base64.b64encode(
        b'{"auth": "ci"}\n'
    ).decode()


def test_a_daemon_refusal_is_reported_with_its_code(
    operator_home: Path, capture: _Captured, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(operator_home, ".pi/agent/auth.json")
    _write(operator_home, ".pi/agent/models-store.json")

    def refuse(method: str, params: object = None, **_kwargs: object) -> object:
        capture.calls.append((method, params))
        raise _cli_get_services().CliError(
            ipc_errors.PROVIDER_CREDENTIAL_EXISTS, "already exists"
        )

    monkeypatch.setattr(_cli_support, "_daemon_request", refuse)

    result = _invoke("pi", "--actor", "worker", "--json")

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["code"] == ipc_errors.PROVIDER_CREDENTIAL_EXISTS
    # One call carries the whole set; the daemon refuses it whole.
    assert len(capture.calls) == 1


def test_actor_is_required_when_this_shell_addresses_no_agent(
    operator_home: Path, capture: _Captured
) -> None:
    _write(operator_home, ".claude/.credentials.json")

    result = _invoke("claude", "--json")

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["code"] == ipc_errors.INVALID_ARGUMENT
    assert capture.calls == []


def test_actor_defaults_to_the_shells_own_agent(
    operator_home: Path, capture: _Captured, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(operator_home, ".claude/.credentials.json")
    monkeypatch.setenv(
        "HYPRIAL_WORKER_ACTOR", "agent:owner:node:self"
    )

    result = _invoke("claude", "--json")

    assert result.exit_code == 0, result.output
    _method, params = capture.calls[0]
    assert isinstance(params, dict)
    assert params["actor"] == "agent:owner:node:self"
