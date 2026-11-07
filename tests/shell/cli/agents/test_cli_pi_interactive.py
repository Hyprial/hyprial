"""CLI orchestration contract for `hyprial agent start pi` interactive attach.

Layer 1 (docs/notes/tui-test-layering.md): no terminal, no real pi — the Popen
stub captures the launch, the daemon stub answers agent.create/ps.  What is
pinned is the launcher's observable orchestration: argv sequence, injected
carrier identity environment, registration polling, failure teardown, and
the --session-id boundary translation.

The real-TUI smoke and Allen's attended acceptance (trust prompt, followUp
not interrupting work, reply round-trip) are layers 2/3 and run manually.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.daemon.impl.harnesses.pi import PI_HARNESS_ATTACH_EXTENSION
from hyprial.daemon.impl.harnesses.pi.loader import PI_CONTROLLED_LOADER
from hyprial.daemon.impl.harnesses.pi import loader as pi_loader
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import subprocess as _cli_subprocess
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

SESSION_REF = "11111111-2222-3333-4444-555555555555"
ACTOR = "agent:op:node-1:pi-tui"
SECRET_CANARY = "HYPRIAL-CANARY-pi-interactive"


def _pi_session(
    agent_dir: Path, cwd: Path, session_id: str, *, stamp: str = "2026-10-06"
) -> Path:
    from hyprial.daemon.impl.transfer.archive.session_files import pi_session_dir_name

    directory = agent_dir / "sessions" / pi_session_dir_name(str(cwd))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stamp}_{session_id}.jsonl"
    path.write_text(
        json.dumps({"type": "session", "id": session_id}) + "\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture(autouse=True)
def _legacy_runtime_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """These pre-P22 cases pin the unconfigured interactive mode."""

    monkeypatch.setattr(_cli_get_services(), "_runtime_context_environment", lambda **_kwargs: None)


@pytest.fixture()
def launch_rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    state_dir = tmp_path / "state"
    hyprial_home = tmp_path / "home"
    hyprial_home.mkdir(mode=0o700)
    monkeypatch.setattr(_cli_get_services(), "uuid4", lambda: __import__("uuid").UUID(SESSION_REF))
    monkeypatch.setenv("HYPRIAL_HOME", str(hyprial_home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state_dir))
    monkeypatch.setenv("HARNESS_PI_BIN", "/test/bin/pi")

    rig: dict[str, Any] = {"daemon_calls": [], "observed": {}, "register": True}

    def daemon_request(
        method: str, params: dict[str, Any] | None = None, *, timeout: float = 15.0
    ) -> dict[str, Any]:
        rig["daemon_calls"].append((method, params or {}))
        response: dict[str, Any] = {
            "daemon": {"running": True, "nodeId": "node-1", "owner": "op"}
        }
        if method == "agent.create":
            response["agent"] = {"name": (params or {}).get("name")}
        if method == "agent.start.preflight":
            response["resumeCommitGuardVersion"] = 1
        if method == "agent.runtime-launch.acquire":
            response.update(
                {
                    "operationId": (params or {})["operationId"],
                    "leaseToken": "pi-lease",
                    "secretEnvironment": dict(
                        rig.get("secret_environment", {"DEEPSEEK_API_KEY": SECRET_CANARY})
                    ),
                }
            )
        if rig["register"] and rig["observed"].get("spawned"):
            response["interactiveSessions"] = [
                {
                    "actor": ACTOR,
                    "sessionRef": SESSION_REF,
                    "source": "pi-extension",
                }
            ]
        return response

    class Process:
        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            rig["observed"]["argv"] = list(argv)
            rig["observed"]["kwargs"] = kwargs
            binding_path = kwargs["env"].get("HYPRIAL_WORKER_SESSION_REF_FILE")
            if binding_path:
                rig["observed"]["session_ref_file_value"] = Path(
                    binding_path
                ).read_text()
            rig["observed"]["spawned"] = True
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            self.returncode = 0 if self.returncode is None else self.returncode
            return self.returncode

        def terminate(self) -> None:
            rig["observed"]["terminated"] = True
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)
    monkeypatch.setattr(_cli_subprocess, "Popen", Process)
    return rig


def test_start_pi_interactive_golden(launch_rig: dict[str, Any], tmp_path: Path) -> None:
    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent",
            "start",
            "pi",
            "--name",
            "pi-tui",
            "--cwd",
            str(tmp_path),
            "--json",
            "--",
            "--model",
            "k3",
        ],
    )

    assert result.exit_code == 0, result.output
    observed = launch_rig["observed"]

    # 1. argv: runtime args first, then hyprial-owned identity + carrier wiring.
    argv = observed["argv"]
    assert argv[0] == "/test/bin/pi"
    assert argv[1:3] == ["--model", "k3"]
    assert argv[3:5] == ["--session-id", SESSION_REF]
    assert argv[5:7] == ["--name", "pi-tui"]
    assert argv[7] == "--extension"
    assert argv[8] == str(PI_HARNESS_ATTACH_EXTENSION)
    assert argv[9] == "--append-system-prompt"
    assert "Harness Network is connected through the hyprial pi harness-bridge" in argv[10]
    assert "harness_send" in argv[10]

    # 2. The carrier identity environment: the session's canonical actor and
    # THIS daemon's socket roots, never an ambient production daemon.
    env = observed["kwargs"]["env"]
    assert env["HYPRIAL_AGENT_SESSION"] == "1"
    assert env["HYPRIAL_WORKER_ACTOR"] == ACTOR
    assert observed["session_ref_file_value"] == SESSION_REF
    assert "HYPRIAL_WORKER_SESSION_REF" not in env
    assert env["HYPRIAL_HOME"] == str((tmp_path / "home").resolve())
    assert env["HARNESS_STATE_DIR"] == str((tmp_path / "state").resolve())
    assert observed["kwargs"]["cwd"] == tmp_path

    # 3. agent.create precedes the spawn (A5) and uses the pi/interactive
    # identity; ps polling gates on the carrier's registration.
    methods = [method for method, _ in launch_rig["daemon_calls"]]
    assert methods[0] == "agent.create"
    create_params = launch_rig["daemon_calls"][0][1]
    assert create_params["harness"] == "pi"
    assert create_params["runtime"] == "interactive"
    assert "ps" in methods

    # 4. Result payload mirrors the claude interactive shape.
    payload = json.loads(result.stdout)
    assert payload == {
        "ok": True,
        "actor": ACTOR,
        "provider": "pi",
        "runtime": "interactive",
        "sessionRef": SESSION_REF,
        "runtimeExitCode": 0,
    }


def test_start_pi_p2_uses_controlled_sdk_tui_and_runtime_profile(
    launch_rig: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pi = shutil.which("pi")
    if pi is None:
        pytest.skip("pi not installed")
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / ".git").mkdir()
    projection_root = tmp_path / "projection"
    native_root = tmp_path / "native"
    session_root = tmp_path / "sessions"
    tool_home = tmp_path / "tool-home"
    for path in (projection_root, native_root, session_root, tool_home):
        path.mkdir()
    public_projection = {
        "mode": "agent-home-p2",
        "authorityPrepared": True,
        "launchToken": "pi-launch-token",
        "harness": "pi",
        "projectionRoot": str(projection_root),
        "nativeRoot": str(native_root),
        "sessionRoot": str(session_root),
        "environment": {
            "HOME": str(tool_home),
            "PI_CODING_AGENT_DIR": str(native_root),
        },
    }
    monkeypatch.setenv("HARNESS_PI_BIN", pi)
    monkeypatch.setattr(
        _cli_get_services(),
        "_runtime_context_environment",
        lambda **_kwargs: public_projection,
    )
    monkeypatch.setattr(
        pi_loader.subprocess,
        "run",
        lambda *_args, **_kwargs: __import__("subprocess").CompletedProcess(
            args=(), returncode=0, stdout=f"{repository}\n", stderr=""
        ),
    )

    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent",
            "start",
            "pi",
            "--name",
            "pi-tui",
            "--cwd",
            str(repository),
            "--json",
            "--",
            "--approve",
        ],
    )

    assert result.exit_code == 0, result.output
    observed = launch_rig["observed"]
    argv = observed["argv"]
    assert argv[:3] == ["node", str(PI_CONTROLLED_LOADER), "run-tui"]
    payload = json.loads(argv[3])
    assert payload["projectionRoot"] == str(projection_root)
    assert payload["nativeRoot"] == str(native_root)
    assert payload["sessionRoot"] == str(session_root)
    assert payload["repositoryRoot"] == str(repository)
    assert payload["additionalExtensionPaths"][0] == str(
        PI_HARNESS_ATTACH_EXTENSION
    )
    environment = observed["kwargs"]["env"]
    assert environment["HOME"] == str(tool_home)
    assert environment["PI_CODING_AGENT_DIR"] == str(native_root)
    assert environment["DEEPSEEK_API_KEY"] == SECRET_CANARY
    methods = [method for method, _params in launch_rig["daemon_calls"]]
    assert methods.count("agent.runtime-launch.acquire") == 1
    assert methods.count("agent.runtime-launch.release") == 1


def test_start_pi_p2_refuses_when_no_provider_secret_is_granted(
    launch_rig: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A secretless P2 pi launch refuses at start, before pi can run.

    Production shape (2026-10-16): an agent-home P2 pi agent with no granted
    agent secret launched secretless, failed PROVIDER_AUTHENTICATION_FAILED and
    was blocked credential-invalid.  The credential source stays the per-agent
    grant: an operator Pi key must NOT be substituted, and the launch must
    refuse instead of running without a credential.
    """

    pi = shutil.which("pi")
    if pi is None:
        pytest.skip("pi not installed")
    # An operator key exists; it must not be used for this P2 agent.
    operator_home = tmp_path / "operator-home"
    (operator_home / ".pi" / "agent").mkdir(parents=True)
    (operator_home / ".pi" / "agent" / "auth.json").write_text(
        json.dumps(
            {"deepseek": {"type": "api_key", "key": "sk-operator-must-not-be-used"}}
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(operator_home))
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / ".git").mkdir()
    projection_root = tmp_path / "projection"
    native_root = tmp_path / "native"
    session_root = tmp_path / "sessions"
    tool_home = tmp_path / "tool-home"
    for path in (projection_root, native_root, session_root, tool_home):
        path.mkdir()
    public_projection = {
        "mode": "agent-home-p2",
        "authorityPrepared": True,
        "launchToken": "pi-launch-token",
        "harness": "pi",
        "projectionRoot": str(projection_root),
        "nativeRoot": str(native_root),
        "sessionRoot": str(session_root),
        "environment": {
            "HOME": str(tool_home),
            "PI_CODING_AGENT_DIR": str(native_root),
        },
    }
    launch_rig["secret_environment"] = {}
    monkeypatch.setenv("HARNESS_PI_BIN", pi)
    monkeypatch.setattr(
        _cli_get_services(),
        "_runtime_context_environment",
        lambda **_kwargs: public_projection,
    )
    monkeypatch.setattr(
        pi_loader.subprocess,
        "run",
        lambda *_args, **_kwargs: __import__("subprocess").CompletedProcess(
            args=(), returncode=0, stdout=f"{repository}\n", stderr=""
        ),
    )

    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent",
            "start",
            "pi",
            "--name",
            "pi-tui",
            "--cwd",
            str(repository),
            "--provider",
            "deepseek",
            "--model",
            "deepseek-flash",
            "--json",
            "--",
            "--approve",
        ],
    )

    assert result.exit_code != 0, result.output
    assert "DEEPSEEK_API_KEY" in result.output
    # No pi process was spawned for a launch with no credential.
    assert "spawned" not in launch_rig["observed"] or not launch_rig["observed"][
        "spawned"
    ]


def test_start_pi_p2_resume_requires_existing_native_session(
    launch_rig: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pi = shutil.which("pi")
    if pi is None:
        pytest.skip("pi not installed")
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / ".git").mkdir()
    projection_root = tmp_path / "projection"
    native_root = tmp_path / "native"
    session_root = tmp_path / "sessions"
    tool_home = tmp_path / "tool-home"
    for path in (projection_root, native_root, session_root, tool_home):
        path.mkdir()
    monkeypatch.setenv("HARNESS_PI_BIN", pi)
    monkeypatch.setattr(
        _cli_get_services(),
        "_runtime_context_environment",
        lambda **_kwargs: {
            "mode": "agent-home-p2",
            "harness": "pi",
            "projectionRoot": str(projection_root),
            "nativeRoot": str(native_root),
            "sessionRoot": str(session_root),
            "environment": {
                "HOME": str(tool_home),
                "PI_CODING_AGENT_DIR": str(native_root),
            },
        },
    )
    monkeypatch.setattr(
        pi_loader.subprocess,
        "run",
        lambda *_args, **_kwargs: __import__("subprocess").CompletedProcess(
            args=(), returncode=0, stdout=f"{repository}\n", stderr=""
        ),
    )
    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent",
            "start",
            "pi",
            "--name",
            "pi-tui",
            "--cwd",
            str(repository),
            "--resume",
            SESSION_REF,
            "--json",
            "--",
            "--approve",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(launch_rig["observed"]["argv"][3])
    assert payload["sessionId"] == SESSION_REF
    assert payload["requireExistingSession"] is True


@pytest.mark.parametrize(
    "flag",
    [
        "--session-id",
        "--session",
        "--fork",
        "--continue",
        "-c",
        "--resume",
        "-r",
        "--mode",
        "--print",
        "-p",
        "--no-session",
    ],
)
def test_start_pi_rejects_session_identity_flags_before_any_daemon_call(
    launch_rig: dict[str, Any], tmp_path: Path, flag: str
) -> None:
    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent",
            "start",
            "pi",
            "--name",
            "pi-tui",
            "--cwd",
            str(tmp_path),
            "--",
            flag,
            "value",
        ],
    )

    assert result.exit_code != 0
    assert "hyprial owns" in result.output
    assert launch_rig["daemon_calls"] == []
    assert "spawned" not in launch_rig["observed"]


def test_start_pi_registration_timeout_terminates_the_tui(
    launch_rig: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    launch_rig["register"] = False
    # Compress the 180s attended deadline: time never stops, but the launch
    # aborts as soon as the deadline passes.
    # Patch the wait's own clock, never the process-global time.monotonic:
    # a leaked thread could take the first tick and the wait would never end.
    from hyprial.shell.impl.cli.commands.agent.runtime import sessions

    ticks = iter([0.0] + [200.0] * 100)
    monkeypatch.setattr(sessions, "_registration_clock", lambda: next(ticks, 200.0))

    result = CliRunner().invoke(
        _cli_root_app,
        ["agent", "start", "pi", "--name", "pi-tui", "--cwd", str(tmp_path)],
    )

    assert result.exit_code != 0
    assert "attach registration" in result.output
    assert "project-trust" in result.output
    assert launch_rig["observed"]["terminated"] is True


def test_start_pi_process_exit_before_registration_is_an_error(
    launch_rig: dict[str, Any], tmp_path: Path
) -> None:
    launch_rig["register"] = False

    class DeadOnArrival:
        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            launch_rig["observed"]["spawned"] = True
            self.returncode = 2

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            return self.returncode

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(_cli_subprocess, "Popen", DeadOnArrival)
    try:
        result = CliRunner().invoke(
            _cli_root_app,
            ["agent", "start", "pi", "--name", "pi-tui", "--cwd", str(tmp_path)],
        )
    finally:
        monkeypatch.undo()

    assert result.exit_code != 0
    assert "attach registration" in result.output


def test_start_pi_interactive_resume_uses_exact_native_session_and_defers_write(
    launch_rig: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent_dir = tmp_path / "pi-agent"
    session_path = _pi_session(agent_dir, tmp_path, SESSION_REF)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent",
            "start",
            "pi",
            "--name",
            "pi-tui",
            "--cwd",
            str(tmp_path),
            "--resume",
            SESSION_REF,
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    argv = launch_rig["observed"]["argv"]
    assert argv[1:3] == ["--session", str(session_path.resolve())]
    methods = [method for method, _ in launch_rig["daemon_calls"]]
    assert methods[0] == "agent.start.preflight"
    assert "agent.create" not in methods
    environment = launch_rig["observed"]["kwargs"]["env"]
    assert environment["HYPRIAL_AGENT_SESSION"] == "1"
    assert launch_rig["observed"]["session_ref_file_value"] == SESSION_REF
    assert "HYPRIAL_WORKER_SESSION_REF" not in environment
    assert environment["HYPRIAL_RESUME_GUARDED"] == "1"


def test_start_pi_unknown_resume_is_distinct_and_does_not_create_agent(
    launch_rig: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    launch_rig["register"] = False
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi-agent"))

    class MissingSession:
        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            launch_rig["observed"]["argv"] = argv
            launch_rig["observed"]["kwargs"] = kwargs
            launch_rig["observed"]["spawned"] = True
            self.returncode = 2

        def poll(self) -> int:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            return self.returncode

    monkeypatch.setattr(_cli_subprocess, "Popen", MissingSession)
    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent",
            "start",
            "pi",
            "--name",
            "pi-tui",
            "--cwd",
            str(tmp_path),
            "--resume",
            "unknown-session",
            "--json",
        ],
    )
    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "PI_RESUME_SESSION_UNKNOWN"
    assert all(method != "agent.create" for method, _ in launch_rig["daemon_calls"])


def test_start_pi_resume_never_uses_a_prefix_match(
    launch_rig: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent_dir = tmp_path / "pi-agent"
    _pi_session(agent_dir, tmp_path, f"{SESSION_REF}-longer")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))

    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent",
            "start",
            "pi",
            "--name",
            "pi-tui",
            "--cwd",
            str(tmp_path),
            "--resume",
            SESSION_REF,
            "--json",
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "PI_RESUME_SESSION_UNKNOWN"
    assert "spawned" not in launch_rig["observed"]


def test_start_pi_resume_refuses_an_exact_session_from_another_project(
    launch_rig: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent_dir = tmp_path / "pi-agent"
    other = tmp_path / "other-project"
    other.mkdir()
    _pi_session(agent_dir, other, SESSION_REF)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))

    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent",
            "start",
            "pi",
            "--name",
            "pi-tui",
            "--cwd",
            str(tmp_path),
            "--resume",
            SESSION_REF,
            "--json",
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "PI_RESUME_SESSION_OUTSIDE_CWD"
    assert "spawned" not in launch_rig["observed"]


def test_start_pi_resume_refuses_duplicate_exact_files(
    launch_rig: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent_dir = tmp_path / "pi-agent"
    _pi_session(agent_dir, tmp_path, SESSION_REF, stamp="first")
    _pi_session(agent_dir, tmp_path, SESSION_REF, stamp="second")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))

    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent",
            "start",
            "pi",
            "--name",
            "pi-tui",
            "--cwd",
            str(tmp_path),
            "--resume",
            SESSION_REF,
            "--json",
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "PI_RESUME_SESSION_AMBIGUOUS"
    assert "spawned" not in launch_rig["observed"]


def test_pi_connector_argv_sanitizes_session_ref_at_the_boundary() -> None:
    """Item 4: the interactive pty connector passes session refs through the
    same [A-Za-z0-9._-] translation as the RPC boundary (#192 follow-up)."""

    from hyprial.kernel import HarnessLaunchSpec
    from hyprial.daemon.impl.harnesses.pi import PiConnector

    spec = HarnessLaunchSpec(
        harness="pi",
        name="pi-tui",
        cwd=Path("/tmp"),
        args=("--model", "k3"),
        headless=False,
        session_ref="squire:alw:usrb",
    )
    argv = PiConnector().build_argv(spec)
    assert argv[-2:] == ("--session-id", "squire.alw.usrb")


def test_pi_session_id_translation_is_deterministic_and_lossless() -> None:
    from hyprial.daemon.impl.harnesses.pi.session import pi_session_id

    assert pi_session_id("clean-ref_1.2") == "clean-ref_1.2"
    assert pi_session_id("squire:alw:usrb") == "squire.alw.usrb"
    assert pi_session_id("a:b/c d") == "a.b.c.d"
    # The RPC connector consumes the same translation (one boundary, #192).
    from hyprial.daemon.impl.harnesses.pi.rpc_client import _pi_session_id

    assert _pi_session_id is pi_session_id


def test_start_claude_interactive_still_routes_to_the_channel_launcher(
    launch_rig: dict[str, Any], tmp_path: Path
) -> None:
    """The pi gate must not swallow the claude channel path."""

    called: list[str] = []
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        _cli_get_services(),
        "_start_interactive_claude",
        lambda **kwargs: called.append("claude") or {"ok": True},
    )
    monkeypatch.setattr(
        _cli_get_services(),
        "_start_interactive_pi",
        lambda **kwargs: called.append("pi") or {"ok": True},
    )
    try:
        result = CliRunner().invoke(
            _cli_root_app,
            ["agent", "start", "claude", "--name", "cc-tui", "--cwd", str(tmp_path), "--json"],
        )
    finally:
        monkeypatch.undo()

    assert result.exit_code == 0, result.output
    assert called == ["claude"]

def test_a_stolen_global_clock_tick_cannot_hang_the_registration_wait(
    launch_rig: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A leaked thread consuming the global clock first must not hang the wait (#1233)."""

    from hyprial.shell.impl.cli.commands.agent.runtime import sessions

    launch_rig["register"] = False
    seam = iter([0.0] + [200.0] * 100)
    monkeypatch.setattr(sessions, "_registration_clock", lambda: next(seam, 200.0))
    ticks = iter([0.0] + [200.0] * 100)
    monkeypatch.setattr(_cli_get_services().time, "monotonic", lambda: next(ticks, 200.0))
    _cli_get_services().time.monotonic()  # what a leaked background thread did

    result = CliRunner().invoke(
        _cli_root_app, ["agent", "start", "pi", "--name", "pi-tui", "--cwd", str(tmp_path)]
    )

    assert result.exit_code != 0
    assert "attach registration" in result.output
