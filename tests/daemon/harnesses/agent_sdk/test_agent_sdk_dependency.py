"""Dependency-boundary tests for the isolated Claude Agent SDK worker.

The daemon environment intentionally does NOT install claude-agent-sdk (the
pinned SDK declares ``mcp<2`` while Harness Bridge requires MCP 2), so the
worker runs under ``uv run --isolated`` with a pinned release. These tests
guard that boundary: the pin, the clean daemon env, the fail-closed capability
check for per-worker identity options, and clear errors when the SDK is
missing instead of an opaque ImportError traceback.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

import hyprial.daemon.impl.harnesses.agent_sdk as agent_sdk_module
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.agent_sdk import (
    AGENT_SDK_VERSION,
    IsolatedAgentSdkClient,
    create_claude_sdk_client,
    sdk_worker_command,
)

WORKER = Path(agent_sdk_module.__file__).with_name("_agent_sdk_worker.py")


def _clean_env(**overrides: str) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", "")}
    env.update(overrides)
    return env


def _run_worker(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(WORKER), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _write_fake_sdk(package_dir: Path, options_signature: str) -> None:
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text(
        f"""
class ClaudeAgentOptions:
    def __init__({options_signature}):
        pass

class ClaudeSDKClient:
    def __init__(self, options):
        self.options = options

class ResultMessage:
    def __init__(self, result=None, is_error=False):
        self.result = result
        self.is_error = is_error
""",
        encoding="utf-8",
    )


def test_default_client_construction_never_imports_agent_sdk() -> None:
    """The daemon env stays dependency-clean: building the client and its
    launch command must not import claude_agent_sdk into this interpreter."""

    sys.modules.pop("claude_agent_sdk", None)
    spec = HarnessLaunchSpec(
        harness="claude", name="cc-guard", headless=True, cwd="/work"
    )

    client = create_claude_sdk_client(spec)

    assert isinstance(client, IsolatedAgentSdkClient)
    assert f"claude-agent-sdk=={AGENT_SDK_VERSION}" in client.command
    assert "claude_agent_sdk" not in sys.modules


def test_sdk_worker_command_is_isolated_and_version_pinned() -> None:
    command = sdk_worker_command()

    assert "--isolated" in command
    assert "--no-project" in command
    assert f"claude-agent-sdk=={AGENT_SDK_VERSION}" in command
    assert command[-1] == str(WORKER)


def test_sdk_worker_command_requires_uv_with_a_clear_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    with pytest.raises(RuntimeError, match="uv is required"):
        sdk_worker_command()


# --------------------------------------------------------------------------- #
# Interpreter selection: a managed worker inherits an isolated HOME/XDG and a
# PATH that may offer a `python3` below the SDK's 3.10 floor, so uv's own
# discovery elected Python 3.9 and the pinned release failed to resolve
# (2026-10-16). The launch must request the running interpreter explicitly.
# --------------------------------------------------------------------------- #
def _write_shadow_python(directory: Path, log: Path) -> None:
    """Stand-ins for the older PATH interpreter uv's discovery elects first.

    They occupy the PATH slot uv inspects (and, when the entry is unusable,
    fails on) instead of a real Python 3.9, which no test machine is required
    to have.  Every invocation is logged, so a launch that consults PATH at
    all is visible even when it happens to pick an acceptable interpreter.
    """

    for name in ("python3", "python3.9"):
        shim = directory / name
        shim.write_text(
            "#!/bin/sh\n"
            f'printf "%s %s\\n" "{name}" "$*" >> "{log}"\n'
            "exit 1\n",
            encoding="utf-8",
        )
        shim.chmod(0o755)


def test_sdk_worker_command_pins_the_running_interpreter() -> None:
    """uv must be told which Python to build the isolated SDK env from."""

    command = sdk_worker_command()

    assert "--python" in command, command
    index = command.index("--python")
    assert command[index + 1] == sys.executable
    # The pinned SDK still arrives as an isolated `uv run` requirement, and
    # the worker stays the script uv executes with the selected interpreter.
    assert command[index + 2 :] == ("python", str(WORKER))
    assert "--isolated" in command and "--no-project" in command
    assert f"claude-agent-sdk=={AGENT_SDK_VERSION}" in command


def test_sdk_worker_command_ignores_path_and_home_interpreter_discovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An isolated HOME plus an older python first on PATH changes nothing:
    neither is consulted once the running interpreter is requested."""

    shadow = tmp_path / "shadow-bin"
    shadow.mkdir()
    _write_shadow_python(shadow, tmp_path / "shadow.log")
    monkeypatch.setattr(shutil, "which", lambda name: "/opt/uv/bin/uv")
    monkeypatch.setenv("PATH", str(shadow))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("UV_PYTHON_INSTALL_DIR", str(tmp_path / "data" / "uv"))

    command = sdk_worker_command()

    assert command[command.index("--python") + 1] == sys.executable


@pytest.mark.parametrize("interpreter", ["", "relative/python3", "missing/python3"])
def test_sdk_worker_command_without_a_usable_interpreter_keeps_uv_discovery(
    monkeypatch: pytest.MonkeyPatch, interpreter: str
) -> None:
    """An unusable running-interpreter path degrades to the historical
    discovery instead of failing the launch outright."""

    monkeypatch.setattr(sys, "executable", interpreter)

    command = sdk_worker_command()

    assert "--python" not in command
    assert command[-1] == str(WORKER)


def test_sdk_worker_command_keeps_a_spaced_interpreter_path_whole(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The interpreter is one argv element: a path with spaces must reach uv
    unsplit (the launch never goes through a shell)."""

    interpreter = tmp_path / "Hyprial Runtime" / "bin" / "python3"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("", encoding="utf-8")
    interpreter.chmod(0o700)
    monkeypatch.setattr(sys, "executable", str(interpreter))

    command = sdk_worker_command()

    assert command[command.index("--python") + 1] == str(interpreter)
    assert "Hyprial Runtime" not in command  # never split into a fragment


def test_bundled_sdk_bootstrap_is_unaffected_by_the_interpreter_pin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A desktop build ships its own bootstrap and is spawned with the running
    interpreter directly: no uv, no `--python`, no PATH lookup."""

    entry = tmp_path / "bundled-sdk-worker.py"
    entry.write_text("", encoding="utf-8")
    monkeypatch.setenv("HYPRIAL_BUNDLED_SDK_BOOTSTRAP", str(entry))
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(sys, "executable", "/nonexistent/python3")

    command = sdk_worker_command()

    assert command == ("/nonexistent/python3", "-I", "-S", "-B", str(entry))


_INTERPRETER_PROBE = (
    "import json, sys\n"
    "print(json.dumps({\n"
    '    "version": list(sys.version_info[:3]),\n'
    '    "baseExecutable": getattr(sys, "_base_executable", sys.executable),\n'
    "}))\n"
)


def _direct_interpreter_facts(interpreter: str) -> dict[str, object]:
    result = subprocess.run(
        [interpreter, "-c", _INTERPRETER_PROBE],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip())


def _launch_command_for_probe(probe: Path) -> list[str]:
    """The real launch argv with the SDK install swapped for a probe script.

    Dropping `--with <pin>` removes the download; every interpreter-selection
    flag stays exactly as the daemon passes it."""

    command = list(sdk_worker_command())
    pin = command.index("--with")
    del command[pin : pin + 2]
    assert command[-2] == "python"
    command[-1] = str(probe)
    return command


def _assert_launch_selects(root: Path, interpreter: str) -> None:
    """Run the reduced launch isolated from HOME, PATH and downloads, and
    assert the interpreter uv actually ran is `interpreter`'s."""

    root.mkdir(parents=True, exist_ok=True)
    for name in ("home", "data", "cache", "config"):
        (root / name).mkdir(exist_ok=True)
    shadow = root / "shadow-bin"
    shadow.mkdir()
    log = root / "shadow-python.log"
    if os.name != "nt":
        _write_shadow_python(shadow, log)
    probe = root / "probe.py"
    probe.write_text(_INTERPRETER_PROBE, encoding="utf-8")
    # An agent-isolated HOME/XDG registers no interpreter installation, PATH
    # offers only the stand-in, and downloads are off: uv has nothing to
    # elect unless it is told which Python to use.
    environment = {
        "HOME": str(root / "home"),
        "XDG_DATA_HOME": str(root / "data"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "UV_PYTHON_INSTALL_DIR": str(root / "data" / "uv" / "python"),
        "UV_PYTHON_DOWNLOADS": "never",
        "PATH": str(shadow),
    }
    result = subprocess.run(
        _launch_command_for_probe(probe),
        env=environment,
        # cwd must not sit inside a Python project: uv's fallback would then
        # find this checkout's .venv and the regression would hide.
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout.strip())
    expected = _direct_interpreter_facts(interpreter)
    assert observed["version"] == expected["version"], (observed, expected)
    assert Path(str(observed["baseExecutable"])).resolve() == Path(
        str(expected["baseExecutable"])
    ).resolve(), (observed, expected)
    assert not log.exists(), (
        "uv inspected a PATH interpreter: "
        f"{log.read_text(encoding='utf-8')[:200] if log.exists() else ''}"
    )


def test_isolated_launch_selects_the_running_interpreter_not_a_path_one(
    tmp_path: Path,
) -> None:
    """Live regression (2026-10-16): with an isolated HOME/XDG and only an
    older `python3` stand-in on PATH, the launch still runs the interpreter
    that runs Hyprial -- and never consults PATH for one."""

    if shutil.which("uv") is None:
        pytest.skip("uv is required to launch the isolated Agent SDK")

    _assert_launch_selects(tmp_path, sys.executable)


def test_isolated_launch_supports_a_spaced_interpreter_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same live launch with the interpreter under a directory containing
    a space: uv receives one unsplit argv element and selects that base."""

    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required to launch the isolated Agent SDK")

    venv = tmp_path / "dir with space" / "venv"
    created = subprocess.run(
        [uv, "venv", "--python", sys.executable, str(venv)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert created.returncode == 0, created.stderr
    interpreter = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    assert interpreter.is_file()
    monkeypatch.setattr(sys, "executable", str(interpreter))

    _assert_launch_selects(tmp_path / "spaced-run", str(interpreter))


def test_sdk_worker_command_finds_standard_user_uv_outside_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    user_uv = tmp_path / ".local" / "bin" / (
        "uv.exe" if os.name == "nt" else "uv"
    )
    user_uv.parent.mkdir(parents=True)
    user_uv.write_text("", encoding="utf-8")
    user_uv.chmod(0o700)

    assert sdk_worker_command()[0] == str(user_uv)


def test_worker_without_agent_sdk_fails_with_a_clear_error() -> None:
    """Missing package => one actionable stderr line, not a traceback. The
    test-suite interpreter deliberately has no claude_agent_sdk installed."""

    result = _run_worker("--probe", env=_clean_env())

    assert result.returncode == 1
    assert "claude-agent-sdk" in result.stderr
    assert "missing module: claude_agent_sdk" in result.stderr
    assert "Traceback" not in result.stderr


def test_worker_rejects_an_sdk_without_identity_option_support(
    tmp_path: Path,
) -> None:
    """An SDK release lacking setting_sources/strict_mcp_config fails closed:
    per-worker Harness identity must never silently degrade."""

    _write_fake_sdk(tmp_path / "claude_agent_sdk", "self, cwd=None, model=None")

    result = _run_worker("--probe", env=_clean_env(PYTHONPATH=str(tmp_path)))

    assert result.returncode == 1
    assert "setting_sources" in result.stderr
    assert "strict_mcp_config" in result.stderr


def test_worker_accepts_an_sdk_with_explicit_identity_options(
    tmp_path: Path,
) -> None:
    """A release exposing the identity options as explicit parameters passes
    the capability guard (no **kwargs escape hatch)."""

    _write_fake_sdk(
        tmp_path / "claude_agent_sdk",
        "self, cwd=None, model=None, system_prompt=None, "
        "setting_sources=None, strict_mcp_config=None, extra_args=None, "
        "resume=None, session_id=None, disallowed_tools=None, max_buffer_size=None",
    )

    result = _run_worker("--probe", env=_clean_env(PYTHONPATH=str(tmp_path)))

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip())["type"] == "probe"


def test_worker_rejects_an_sdk_without_disallowed_tools(tmp_path: Path) -> None:
    """Allow-without-deny is forbidden: an SDK that cannot express the deny
    half must fail closed instead of silently permitting ``hyprial daemon``."""

    _write_fake_sdk(
        tmp_path / "claude_agent_sdk",
        "self, cwd=None, model=None, system_prompt=None, "
        "setting_sources=None, strict_mcp_config=None, extra_args=None, "
        "resume=None, session_id=None",
    )

    result = _run_worker("--probe", env=_clean_env(PYTHONPATH=str(tmp_path)))

    assert result.returncode == 1
    assert "disallowed_tools" in result.stderr


@pytest.mark.anyio
async def test_client_startup_failure_reports_the_worker_stderr() -> None:
    """The client seam surfaces the worker's clear stderr line when the SDK
    is missing, instead of an opaque ConnectionError."""

    spec = HarnessLaunchSpec(
        harness="claude", name="cc-guard", headless=True, cwd="/work"
    )
    client = IsolatedAgentSdkClient(
        spec,
        worker_command=(sys.executable, str(WORKER)),
        env=_clean_env(),
        startup_timeout_seconds=5.0,
    )

    with pytest.raises(ConnectionError, match="claude-agent-sdk"):
        async with client:
            pass


@pytest.mark.skipif(
    os.environ.get("HYPRIAL_TEST_REAL_AGENT_SDK") != "1",
    reason="downloads the pinned SDK; set HYPRIAL_TEST_REAL_AGENT_SDK=1 to run",
)
def test_pinned_sdk_release_supports_the_identity_options() -> None:
    """Live check: the pinned release boots in the isolated environment and
    passes the capability guard (setting_sources + strict_mcp_config)."""

    result = subprocess.run(
        [*sdk_worker_command(), "--probe"],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())
    assert payload == {"type": "probe", "client": "ClaudeSDKClient"}


# --------------------------------------------------------------------------- #
# Connect watchdog: a hung CLI connect must fail LOUD and BOUNDED, never loop
# silently (the zero-log blind spot behind "worker alive, turn never flows")
# --------------------------------------------------------------------------- #
def _write_slow_connect_sdk(package_dir: Path) -> None:
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text(
        """
import asyncio

class ClaudeAgentOptions:
    def __init__(self, cwd=None, model=None, system_prompt=None,
                 setting_sources=None, strict_mcp_config=None, extra_args=None,
                 resume=None, session_id=None, disallowed_tools=None, max_buffer_size=None):
        pass

class ClaudeSDKClient:
    def __init__(self, options):
        self.options = options

    async def __aenter__(self):
        await asyncio.sleep(30)
        return self

    async def __aexit__(self, *args):
        return False

class ResultMessage:
    def __init__(self, result=None, is_error=False):
        self.result = result
        self.is_error = is_error
""",
        encoding="utf-8",
    )


def test_worker_connect_watchdog_exits_loud_and_bounded(tmp_path: Path) -> None:
    """A connect that never completes is converted into ONE actionable
    stderr line and a non-zero exit at the watchdog bound -- the reason the
    parent's worker.exited record can carry instead of a silent 300s loop."""

    _write_slow_connect_sdk(tmp_path / "claude_agent_sdk")

    started = time.monotonic()
    result = _run_worker(
        env=_clean_env(
            PYTHONPATH=str(tmp_path),
            HYPRIAL_AGENT_SDK_CONNECT_TIMEOUT_SECONDS="1",
        )
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 2
    assert "did not finish connecting" in result.stderr
    assert "harness args" in result.stderr
    assert "ready" not in result.stdout
    assert elapsed < 20, f"watchdog did not bound the connect ({elapsed:.1f}s)"


@pytest.mark.anyio
async def test_client_startup_timeout_logs_a_distinct_event(tmp_path: Path) -> None:
    """The parent marks 'alive but never ready' explicitly: without a
    distinct startup_timeout record, the restart loop is silent about why."""

    events: list[tuple[str, str, dict[str, object]]] = []

    class _Logger:
        def info(self, event: str, **fields: object) -> None:
            events.append(("info", event, fields))

        def log(self, level: str, event: str, **fields: object) -> None:
            events.append((level, event, fields))

    spec = HarnessLaunchSpec(
        harness="claude", name="cc-hang", headless=True, cwd="/work"
    )
    client = IsolatedAgentSdkClient(
        spec,
        worker_command=(
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
        ),
        env=_clean_env(),
        startup_timeout_seconds=0.5,
        logger=_Logger(),  # type: ignore[arg-type]
    )

    with pytest.raises((ConnectionError, TimeoutError)):
        async with client:
            pass

    startup_timeouts = [f for _level, event, f in events if event == "worker.startup_timeout"]
    assert len(startup_timeouts) == 1, events
    assert startup_timeouts[0]["timeoutSeconds"] == 0.5
    assert "pid" in startup_timeouts[0]


@pytest.mark.anyio
async def test_client_startup_timeout_keeps_a_verbose_error_body() -> None:
    """A rate-limit-style verbose stderr must survive into the log record:
    the routine tail (8 lines/4 KiB) could truncate away the explaining
    line; the startup-critical budget (32 lines/16 KiB) keeps it."""

    events: list[tuple[str, str, dict[str, object]]] = []

    class _Logger:
        def info(self, event: str, **fields: object) -> None:
            events.append(("info", event, fields))

        def log(self, level: str, event: str, **fields: object) -> None:
            events.append((level, event, fields))

    noisy = (
        "import sys, time\n"
        "for i in range(20):\n"
        "    sys.stderr.write('rate limit retry %d body=%s\\n' % (i, 'x' * 400))\n"
        "sys.stderr.flush()\n"
        "time.sleep(30)\n"
    )
    spec = HarnessLaunchSpec(
        harness="claude", name="cc-noisy", headless=True, cwd="/work"
    )
    client = IsolatedAgentSdkClient(
        spec,
        worker_command=(sys.executable, "-c", noisy),
        env=_clean_env(),
        startup_timeout_seconds=0.5,
        logger=_Logger(),  # type: ignore[arg-type]
    )

    with pytest.raises((ConnectionError, TimeoutError)):
        async with client:
            pass

    (fields,) = [f for _level, event, f in events if event == "worker.startup_timeout"]
    tail = str(fields["stderrTail"])
    # 20 noisy lines of ~420 chars each (> 8 lines, > 4 KiB) all survive.
    assert tail.count("rate limit retry") == 20
    assert "rate limit retry 0 " in tail


@pytest.mark.parametrize("mode", ["missing", "relative", "valid"])
def test_desktop_worker_requires_selected_external_cli(tmp_path, mode):
    _write_fake_sdk(
        tmp_path / "claude_agent_sdk",
        "self, *, strict_mcp_config=False, allowed_tools=None, **kwargs",
    )
    cli = tmp_path / "claude"
    cli.write_text("placeholder")
    env = _clean_env(PYTHONPATH=str(tmp_path), HYPRIAL_DESKTOP_COMPONENTS="1")
    if mode != "missing":
        env["HYPRIAL_CLAUDE_EXECUTABLE"] = str(cli) if mode == "valid" else "claude"
    result = _run_worker("--probe", env=env)
    if mode == "valid":
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0
        assert ("not enabled" if mode == "missing" else "absolute file") in result.stderr
