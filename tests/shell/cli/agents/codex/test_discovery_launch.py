"""cli_codex_interactive: discovery launch scenarios."""

from __future__ import annotations
import base64
import json
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
import pytest
from typer.testing import CliRunner
from hyprial.shell.impl.cli.commands.agent.runtime import sessions as codex_sessions
from hyprial.daemon.impl.harnesses import codex
from hyprial.daemon.impl.mcp.channel import ownership as mcp_channel
from hyprial.identity import install_native_credential, native_secrets_root

from .support import (
    ACTOR,
    THREAD_ID,
    _legacy_runtime_context as _legacy_runtime_context,
    candidate,
)
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.commands.agent.runtime.interactive as _cli_owner_1
import subprocess as _cli_subprocess
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


@pytest.mark.parametrize(
    ("listed", "loaded", "reads", "expected_mode", "expected_calls"),
    [
        ([candidate(Path("/tmp/codex-pr1"))], [], {}, "thread/list", ["thread/list"]),
        ([], [THREAD_ID], {THREAD_ID: candidate(Path("/tmp/codex-pr1"))}, "loaded/list + thread/read fallback", ["thread/list", "thread/loaded/list", "thread/read"]),
    ],
)
def test_codex_discovery_uses_the_three_level_fallback(
    listed: list[dict[str, object]],
    loaded: list[str],
    reads: dict[str, dict[str, object]],
    expected_mode: str,
    expected_calls: list[str],
) -> None:
    cwd = Path("/tmp/codex-pr1")
    server = codex.CodexInteractiveAppServer(Path("/tmp/codex-pr1.sock"), cwd=cwd)
    calls: list[str] = []

    def request(method: str, params: object = None) -> dict[str, object]:
        calls.append(method)
        if method == "thread/list":
            return {"data": listed}
        if method == "thread/loaded/list":
            return {"data": loaded}
        if method == "thread/read":
            thread_id = str((params or {}).get("threadId"))
            return {"thread": reads[thread_id]}
        raise AssertionError(method)

    server.request = request  # type: ignore[method-assign]
    assert server.discover_thread() == (THREAD_ID, expected_mode)
    assert calls == expected_calls


def test_rpc_lock_serializes_concurrent_requests_on_the_single_socket() -> None:
    """Two requests cannot share the app-server request/response stream.

    This is event-driven rather than timing-window based: the first fake-socket
    call blocks until either the second call enters the socket (the no-lock
    failure path) or the observed outer lock proves the second call is blocked
    (the production path).  Scheduler delay before the second call cannot make
    an unlocked run look serialized.
    """

    class ObservableRLock:
        def __init__(self) -> None:
            self._lock = threading.RLock()
            self.blocked = threading.Event()

        def __enter__(self) -> "ObservableRLock":
            if not self._lock.acquire(blocking=False):
                self.blocked.set()
                self._lock.acquire()
            return self

        def __exit__(self, *_args: object) -> None:
            self._lock.release()

    class CollisionSocket:
        def __init__(self) -> None:
            self.condition = threading.Condition()
            self.first_entered = threading.Event()
            self.second_entered = threading.Event()
            self.release_first = threading.Event()
            self.active = 0
            self.max_active = 0
            self.calls = 0

        def request(
            self, method: str, params: object, request_id: int
        ) -> dict[str, object]:
            with self.condition:
                self.calls += 1
                first = self.calls == 1
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                if first:
                    self.first_entered.set()
                else:
                    self.second_entered.set()
                    self.release_first.set()
            if first:
                # No deadline is part of the expected behavior: production
                # releases this from the lock-blocked proof; a missing lock is
                # released by the second socket entry itself.
                self.release_first.wait()
            with self.condition:
                self.active -= 1
            return {
                "id": request_id,
                "result": {"method": method, "requestId": request_id},
            }

    server = codex.CodexInteractiveAppServer(
        Path("/tmp/codex-lock.sock"), cwd=Path("/tmp")
    )
    fake = CollisionSocket()
    observed_lock = ObservableRLock()
    server._socket = fake  # type: ignore[assignment]
    server._rpc_lock = observed_lock  # type: ignore[assignment]
    failures: list[BaseException] = []
    results: list[dict[str, object]] = []

    def call(method: str) -> None:
        try:
            results.append(server.request(method, {}))
        except BaseException as error:  # noqa: BLE001 - collected thread boundary
            failures.append(error)

    first = threading.Thread(target=call, args=("thread/list",))
    second = threading.Thread(target=call, args=("thread/loaded/list",))
    first.start()
    assert fake.first_entered.wait(timeout=5.0)
    second.start()

    deadline = time.monotonic() + 5.0
    while (
        not observed_lock.blocked.is_set()
        and not fake.second_entered.is_set()
        and time.monotonic() < deadline
    ):
        time.sleep(0.001)
    assert observed_lock.blocked.is_set() or fake.second_entered.is_set(), (
        "second RPC neither entered the socket nor proved itself blocked on "
        "the outer lock"
    )
    fake.release_first.set()
    first.join(timeout=5.0)
    second.join(timeout=5.0)

    assert not first.is_alive()
    assert not second.is_alive()
    assert failures == []
    assert fake.max_active == 1, (
        "concurrent RPC calls overlapped on the single app-server request "
        f"stream (max_active={fake.max_active})"
    )
    assert sorted(int(response["requestId"]) for response in results) == [1, 2]


def test_codex_discovery_without_any_fallback_is_red() -> None:
    cwd = Path("/tmp/codex-pr1")
    server = codex.CodexInteractiveAppServer(Path("/tmp/codex-pr1.sock"), cwd=cwd)

    def request(method: str, params: object = None) -> dict[str, object]:
        if method == "thread/list":
            return {"data": []}
        if method == "thread/loaded/list":
            return {"data": []}
        raise AssertionError(method)

    server.request = request  # type: ignore[method-assign]
    with pytest.raises(ConnectionError, match="thread/list, loaded/list, or thread/read"):
        server.discover_thread()


def test_codex_discovery_falls_back_when_thread_list_is_unavailable() -> None:
    cwd = Path("/tmp/codex-pr1")
    server = codex.CodexInteractiveAppServer(Path("/tmp/codex-pr1.sock"), cwd=cwd)
    calls: list[str] = []

    def request(method: str, params: object = None) -> dict[str, object]:
        calls.append(method)
        if method == "thread/list":
            raise codex.CodexAppServerRpcError("method not found")
        if method == "thread/loaded/list":
            return {"data": [THREAD_ID]}
        if method == "thread/read":
            return {"thread": candidate(cwd)}
        raise AssertionError(method)

    server.request = request  # type: ignore[method-assign]
    assert server.discover_thread() == (
        THREAD_ID,
        "loaded/list + thread/read fallback",
    )
    assert calls == ["thread/list", "thread/loaded/list", "thread/read"]


@pytest.mark.parametrize("skill_warning", [False, True])
def test_start_codex_interactive_golden(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, skill_warning: bool
) -> None:
    from types import SimpleNamespace

    warning_text = "Codex skills/list reported loader errors: missing YAML frontmatter delimited by ---"
    events: list[tuple[str, Any]] = []
    daemon_calls: list[tuple[str, dict[str, object]]] = []

    def daemon_request(
        method: str,
        params: dict[str, object] | None = None,
        *,
        timeout: float = 15.0,
        restore_wait: float | None = None,
    ) -> dict[str, object]:
        del restore_wait
        payload = params or {}
        daemon_calls.append((method, payload))
        if method == "ps":
            return {"daemon": {"owner": "op", "nodeId": "node-1"}}
        if method == "agent.runtime-context":
            return {"ok": True, "mode": "legacy", "environment": {}}
        return {"ok": True}

    class FakeServer:
        pid = 4321
        native_load_evidence = SimpleNamespace(warnings=(warning_text,) if skill_warning else ())

        def __init__(self, socket_path: Path, **kwargs: object) -> None:
            self.socket_path = socket_path
            events.append(("server.init", kwargs))

        def start(self) -> None:
            events.append(("server.start", None))

        def discover_thread(self) -> tuple[str, str]:
            events.append(("server.discover", None))
            return THREAD_ID, "thread/list"

        def stop(self) -> None:
            events.append(("server.stop", None))

    class FakeProcess:
        def __init__(self, argv: list[str], **kwargs: object) -> None:
            self.argv = argv
            self.kwargs = kwargs
            self.returncode: int | None = None
            events.append(("tui.start", argv))

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            self.returncode = 0
            events.append(("tui.wait", None))
            return 0

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setenv("HARNESS_CODEX_BIN", "/test/bin/codex")
    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)
    monkeypatch.setattr("hyprial.daemon.CodexInteractiveAppServer", FakeServer)
    monkeypatch.setattr(_cli_subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(
        mcp_channel, "_read_process_identity", lambda _pid: "proc-starttime:99"
    )

    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent",
            "start",
            "codex",
            "--name",
            "codex-tui",
            "--cwd",
            str(tmp_path),
            "--json",
            "--",
            "--model",
            "gpt-5.6-luna",
            "-c",
            "check_for_update_on_startup=true",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    if skill_warning:
        assert payload.pop("warnings") == [
            {"code": "CODEX_SKILL_LOAD_WARNING", "message": warning_text}
        ]
        assert warning_text in result.stderr
        log = _cli_get_services()._state_dir() / "logs/daemon.jsonl"
        assert warning_text in log.read_text()
    assert payload == {
        "ok": True,
        "actor": ACTOR,
        "provider": "codex",
        "runtime": "interactive",
        "sessionRef": THREAD_ID,
        "runtimeExitCode": 0,
    }
    methods = [method for method, _ in daemon_calls]
    assert methods[:4] == [
        "agent.create",
        "agent.runtime-context",
        "ps",
        "session.register",
    ]
    assert "message.pending.list" in methods
    assert methods[-1] == "session.unregister"
    registration = daemon_calls[3][1]
    assert registration["source"] == "codex-app-server"
    assert registration["runtime"] == "codex_interactive"
    assert registration["sessionRef"] == THREAD_ID
    assert registration["processPid"] == 4321
    assert registration["processIdentity"] == "proc-starttime:99"
    tui_argv = next(value for name, value in events if name == "tui.start")
    assert tui_argv == [
        "/test/bin/codex",
        "--no-alt-screen",
        "-c",
        "check_for_update_on_startup=false",
        "--ask-for-approval",
        "on-request",
        "--model",
        "gpt-5.6-luna",
        "-c",
        "check_for_update_on_startup=true",
        "--remote",
        tui_argv[-1],
    ]
    assert [name for name, _ in events][-2:] == ["tui.wait", "server.stop"]


def test_start_codex_merges_granted_secret_into_server_and_tui_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    canary = "HYPRIAL-CANARY-codex-interactive"
    home = tmp_path / "home"
    state = tmp_path / "state"
    home.mkdir()
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.test:8443")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    calls: list[str] = []
    observed: dict[str, object] = {}
    ambient = dict(__import__("os").environ)

    def daemon_request(method, params=None, **_kwargs):
        calls.append(method)
        if method == "agent.runtime-context":
            return {
                "ok": True,
                "mode": "agent-home-p2",
                "authorityPrepared": True,
                "launchToken": "context",
                "actor": ACTOR,
                "harness": "codex",
                "projectionRoot": str(tmp_path / "projection"),
                "nativeRoot": str(tmp_path / "native"),
                "sessionRoot": str(tmp_path / "sessions"),
                "environment": {
                    "HOME": str(tmp_path / "tool-home"),
                    "CODEX_HOME": str(tmp_path / "native"),
                },
            }
        if method == "agent.runtime-launch.acquire":
            return {
                "ok": True,
                "operationId": params["operationId"],
                "leaseToken": "lease",
                "secretEnvironment": {"DEEPSEEK_API_KEY": canary},
            }
        if method == "ps":
            return {"daemon": {"owner": "op", "nodeId": "node-1"}}
        return {"ok": True}

    class FakeServer:
        pid = 4321

        def __init__(self, _socket_path: Path, **kwargs: object) -> None:
            observed["server_env"] = dict(kwargs["env"])  # type: ignore[arg-type]

        def start(self) -> None:
            assert dict(__import__("os").environ) == ambient

        def discover_thread(self) -> tuple[str, str]:
            return THREAD_ID, "thread/list"

        def stop(self) -> None:
            return None

    class FakeProcess:
        def __init__(self, argv: list[str], **kwargs: object) -> None:
            observed["argv"] = list(argv)
            observed["tui_env"] = dict(kwargs["env"])  # type: ignore[arg-type]
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            self.returncode = 0
            return 0

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)
    monkeypatch.setattr("hyprial.daemon.CodexInteractiveAppServer", FakeServer)
    monkeypatch.setattr(_cli_subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(mcp_channel, "_read_process_identity", lambda _pid: None)

    result = _cli_get_services()._start_interactive_codex(
        name="codex-tui",
        nickname=None,
        cwd=tmp_path,
        runtime_args=(),
        model_provider="deepseek",
        model="deepseek-flash",
        json_output=True,
    )

    for key in ("server_env", "tui_env"):
        environment = observed[key]
        assert isinstance(environment, dict)
        assert environment["DEEPSEEK_API_KEY"] == canary
        assert environment["HTTPS_PROXY"] == "http://proxy.test:8443"
        assert environment["NO_PROXY"] == "127.0.0.1,localhost"
    assert all(canary not in item for item in observed["argv"])
    assert dict(__import__("os").environ) == ambient
    assert result["ok"] is True
    assert calls.count("agent.runtime-launch.acquire") == 1
    assert calls.count("agent.runtime-launch.release") == 1


def test_start_codex_interactive_preflights_native_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    native_root = tmp_path / "native"
    native_root.mkdir(mode=0o700)
    calls: list[str] = []

    def daemon_request(method, params=None, **_kwargs):
        calls.append(method)
        if method == "agent.runtime-context":
            return {
                "ok": True,
                "mode": "agent-home-p2",
                "authorityPrepared": True,
                "launchToken": "context",
                "actor": ACTOR,
                "harness": "codex",
                "projectionRoot": str(tmp_path / "projection"),
                "nativeRoot": str(native_root),
                "sessionRoot": str(tmp_path / "sessions"),
                "environment": {
                    "HOME": str(tmp_path / "tool-home"),
                    "CODEX_HOME": str(native_root),
                },
            }
        if method == "ps":
            return {"daemon": {"owner": "op", "nodeId": "node-1"}}
        return {"ok": True}

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)
    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent",
            "start",
            "codex",
            "--name",
            "codex-tui",
            "--cwd",
            str(tmp_path),
            "--json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "CODEX_CREDENTIAL_MISSING"
    assert payload["data"]["reason"] == "missing"
    assert "hyprial agent provider login codex --actor codex-tui" in payload["error"]
    assert str(native_root / "auth.json") in payload["error"]
    assert "agent.runtime-launch.acquire" not in calls
    assert "ps" not in calls


def _credential_projection(
    agent_home: Path, *, shared_target: Path | None = None
) -> dict[str, object]:
    native_root = native_secrets_root(agent_home, "codex")
    return {
        "nativeRoot": str(native_root),
        **(
            {
                "sharedCredential": {
                    "authMode": "native-shared-link",
                    "nativePath": str(native_root / "auth.json"),
                    "targetPath": str(shared_target),
                }
            }
            if shared_target is not None
            else {}
        ),
    }


def _credential_preflight_error(
    projection: dict[str, object],
) -> _cli_get_services().CliError:
    with pytest.raises(_cli_get_services().CliError) as raised:
        codex_sessions._preflight_codex_credential(
            projection, name="codex-tui", model_provider=None
        )
    return raised.value


def _install_from_recommended_login(
    monkeypatch: pytest.MonkeyPatch,
    *,
    operator_home: Path,
    agent_home: Path,
    command: str,
) -> None:
    source = operator_home / ".codex/auth.json"
    source.parent.mkdir(parents=True)
    source.write_bytes(b'{"access_token":"isolated-test"}\n')
    monkeypatch.setenv("HOME", str(operator_home))
    monkeypatch.delenv("HYPRIAL_WORKER_ACTOR", raising=False)

    def daemon_request(method: str, params=None, **_kwargs):
        assert method == "agent.provider.login"
        assert isinstance(params, dict)
        [(document)] = params["files"]
        receipt = install_native_credential(
            agent_home,
            "codex",
            document["filename"],
            base64.b64decode(document["contentBase64"]),
            force=params["force"],
        )
        return {
            "ok": True,
            "provider": "codex",
            "actor": params["actor"],
            "files": [receipt],
        }

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)
    arguments = shlex.split(command)
    assert arguments.pop(0) == "hyprial"
    result = CliRunner().invoke(_cli_root_app, [*arguments, "--json"])
    assert result.exit_code == 0, result.output


def test_codex_credential_missing_recommends_working_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent_home = tmp_path / "agent-home"
    agent_home.mkdir(mode=0o700)
    projection = _credential_projection(agent_home)

    error = _credential_preflight_error(projection)

    assert error.data["reason"] == "missing"
    remediation = error.data["remediation"]
    assert remediation["kind"] == "command"
    _install_from_recommended_login(
        monkeypatch,
        operator_home=tmp_path / "operator-home",
        agent_home=agent_home,
        command=remediation["command"],
    )
    codex_sessions._preflight_codex_credential(
        projection, name="codex-tui", model_provider=None
    )


def test_codex_credential_empty_recommends_working_forced_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent_home = tmp_path / "agent-home"
    agent_home.mkdir(mode=0o700)
    target = native_secrets_root(agent_home, "codex") / "auth.json"
    install_native_credential(agent_home, "codex", "auth.json", b"old")
    target.write_bytes(b"")
    projection = _credential_projection(agent_home)

    error = _credential_preflight_error(projection)

    assert error.data["reason"] == "empty"
    remediation = error.data["remediation"]
    assert remediation["kind"] == "command"
    assert "--force" in shlex.split(remediation["command"])
    _install_from_recommended_login(
        monkeypatch,
        operator_home=tmp_path / "operator-home",
        agent_home=agent_home,
        command=remediation["command"],
    )
    codex_sessions._preflight_codex_credential(
        projection, name="codex-tui", model_provider=None
    )


def test_codex_credential_permissions_recommends_working_chmod(tmp_path: Path) -> None:
    agent_home = tmp_path / "agent-home"
    agent_home.mkdir(mode=0o700)
    target = native_secrets_root(agent_home, "codex") / "auth.json"
    install_native_credential(agent_home, "codex", "auth.json", b"credential")
    target.chmod(0o644)
    projection = _credential_projection(agent_home)

    error = _credential_preflight_error(projection)

    assert error.data["reason"] == "permissions"
    remediation = error.data["remediation"]
    assert remediation["kind"] == "command"
    subprocess.run(shlex.split(remediation["command"]), check=True)
    codex_sessions._preflight_codex_credential(
        projection, name="codex-tui", model_provider=None
    )


def test_codex_credential_not_regular_names_manual_repair_without_refused_login(
    tmp_path: Path,
) -> None:
    agent_home = tmp_path / "agent-home"
    agent_home.mkdir(mode=0o700)
    target = native_secrets_root(agent_home, "codex") / "auth.json"
    target.mkdir(parents=True)

    error = _credential_preflight_error(_credential_projection(agent_home))

    assert error.data["reason"] == "not-regular"
    remediation = error.data["remediation"]
    assert remediation["kind"] == "manual"
    assert str(target) in remediation["instruction"]
    assert "no hyprial cli command" in remediation["instruction"].lower()
    assert "hyprial agent provider login" not in str(error)


def test_codex_credential_inaccessible_names_manual_repair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent_home = tmp_path / "agent-home"
    agent_home.mkdir(mode=0o700)
    target = native_secrets_root(agent_home, "codex") / "auth.json"
    original_lstat = Path.lstat

    def inaccessible(path: Path):
        if path == target:
            raise PermissionError("isolated test refusal")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", inaccessible)
    error = _credential_preflight_error(_credential_projection(agent_home))

    assert error.data["reason"] == "inaccessible"
    remediation = error.data["remediation"]
    assert remediation["kind"] == "manual"
    assert str(target) in remediation["instruction"]
    assert "no hyprial cli command" in remediation["instruction"].lower()
    assert "hyprial agent provider login" not in str(error)


def test_codex_shared_credential_names_external_target_and_config_repair(
    tmp_path: Path,
) -> None:
    agent_home = tmp_path / "agent-home"
    agent_home.mkdir(mode=0o700)
    target = tmp_path / "shared" / "auth.json"

    error = _credential_preflight_error(
        _credential_projection(agent_home, shared_target=target)
    )

    assert error.data["reason"] == "shared-target"
    remediation = error.data["remediation"]
    assert remediation["kind"] == "manual"
    assert str(target) in remediation["instruction"]
    assert "config.sharedCredentials.codex" in remediation["instruction"]
    assert "no hyprial cli command" in remediation["instruction"].lower()
    assert "hyprial agent provider login" not in str(error)


def test_runtime_launch_custody_acquires_and_releases_opaque_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def request(method, params=None, **_kwargs):
        calls.append((method, params))
        if method == "agent.runtime-launch.acquire":
            return {
                "ok": True,
                "operationId": params["operationId"],
                "actor": "agent:owner:node:worker",
                "leaseToken": "lease-1",
                "expiresAtMs": 42,
            }
        if method == "agent.runtime-launch.release":
            return {
                "ok": True,
                "actor": "agent:owner:node:worker",
                "leaseToken": "lease-1",
                "released": True,
            }
        raise AssertionError(method)

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    projection = {"authorityPrepared": True, "launchToken": "context-1"}
    with _cli_owner_1._runtime_launch_custody(projection) as secret_environment:
        assert secret_environment == {}
        assert len(calls) == 1
        assert calls[0][0] == "agent.runtime-launch.acquire"
        operation_id = calls[0][1]["operationId"]
        assert calls[0][1] == {
            "launchToken": "context-1",
            "operationId": operation_id,
        }
    assert calls[-1] == (
        "agent.runtime-launch.release",
        {"leaseToken": "lease-1", "operationId": operation_id},
    )


def test_runtime_launch_custody_refuses_missing_authority_token() -> None:
    with pytest.raises(_cli_get_services().CliError, match="missing launchToken"):
        with _cli_owner_1._runtime_launch_custody({"authorityPrepared": True}):
            raise AssertionError("spawn body must not run")


def test_runtime_launch_custody_never_releases_a_mismatched_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def request(method, params=None, **_kwargs):
        calls.append((method, params))
        if method == "agent.runtime-launch.acquire":
            return {
                "ok": True,
                "operationId": "another-attempt",
                "actor": "agent:owner:node:worker",
                "leaseToken": "another-lease",
            }
        raise AssertionError(method)

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    with pytest.raises(_cli_get_services().CliError, match="mismatched.*operation"):
        with _cli_owner_1._runtime_launch_custody({
            "authorityPrepared": True,
            "launchToken": "context-1",
        }):
            raise AssertionError("spawn body must not run")

    assert len(calls) == 1
    assert calls[0][0] == "agent.runtime-launch.acquire"


@pytest.mark.parametrize(
    "secret_environment",
    [[], {"BAD-NAME": "value"}, {"GOOD_NAME": 1}],
)
def test_runtime_launch_custody_rejects_invalid_secret_environment_and_releases(
    monkeypatch: pytest.MonkeyPatch, secret_environment: object
) -> None:
    calls = []

    def request(method, params=None, **_kwargs):
        calls.append((method, params))
        if method == "agent.runtime-launch.acquire":
            return {
                "ok": True,
                "operationId": params["operationId"],
                "actor": "agent:owner:node:worker",
                "leaseToken": "lease-1",
                "secretEnvironment": secret_environment,
            }
        if method == "agent.runtime-launch.release":
            return {"ok": True, "released": True, "leaseToken": "lease-1"}
        raise AssertionError(method)

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    with pytest.raises(_cli_get_services().CliError, match="invalid Agent launch secret environment"):
        with _cli_owner_1._runtime_launch_custody(
            {"authorityPrepared": True, "launchToken": "context-1"}
        ):
            raise AssertionError("invalid acquire response must not reach spawn")

    assert [method for method, _params in calls] == [
        "agent.runtime-launch.acquire",
        "agent.runtime-launch.release",
    ]


@pytest.mark.parametrize("flag", ["--remote", "--remote-auth-token-env"])
def test_start_codex_rejects_hyprial_owned_remote_flags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, flag: str
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda method, params=None, **kwargs: calls.append(method) or {},
    )
    result = CliRunner().invoke(
        _cli_root_app,
        ["agent", "start", "codex", "--name", "codex-tui", "--cwd", str(tmp_path), "--", flag, "x"],
    )
    assert result.exit_code != 0
    assert "hyprial owns" in " ".join(result.output.split())
    assert calls == []
