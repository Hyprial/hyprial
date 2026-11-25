#!/usr/bin/env python3
"""Isolated acceptance for daemon-owned autoupdate scheduling.

This supporting runner creates a disposable Git remote and uv tool root,
boots the candidate on an older synthetic version, publishes a newer tag,
then asks the daemon scheduler to run.  Nothing is sent to the user around
the restart (R2, owner ruling 2026-11-19): the run is evidenced by the
``autoupdate.run.started`` entry in the old daemon's log and by the last-run
record, which carries the restart and no ``notification``.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Stand on its own when loaded by path (spec_from_file_location in the CI
# baseline tests), not only when run as a script: the sibling helper module
# lives next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _diagnostic_text import clip, one_line
from _daemon_runner import write_project_version
from _runner_shared.ps import ps_from_env

ROOT = Path(__file__).resolve().parents[2]
GIT_SOURCE = "ssh://git@git.internal.hyprial.com/HyprialOS/harness-bridge.git"
INITIAL_VERSION = "0.4.2.dev98"
UPGRADED_VERSION = "0.4.2.dev99"
OWNER = "autoupdate-e2e"
NODE = "autoupdate-e2e-node"


def run(
    *command: str | Path,
    env: dict[str, str],
    cwd: Path | None = None,
    timeout: float = 300.0,
) -> str:
    rendered = [str(item) for item in command]
    completed = subprocess.run(
        rendered,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"command failed ({completed.returncode}): {' '.join(rendered)}\n"
            f"{completed.stdout[-4000:]}"
        )
    return completed.stdout


def hyprial(executable: Path, args: list[str], env: dict[str, str]) -> dict[str, object]:
    output = run(executable, *args, env=env)
    for line in reversed([item for item in output.splitlines() if item.strip()]):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise AssertionError(f"hyprial {' '.join(args)} emitted no JSON object: {output[-2000:]}")


def git_config(path: Path, remote: Path) -> None:
    path.write_text(
        "\n".join(
            (
                '[protocol "file"]',
                "\tallow = always",
                f'[url "{remote.as_uri()}"]',
                f"\tinsteadOf = {GIT_SOURCE}",
                "[user]",
                "\tname = hyprial autoupdate e2e",
                "\temail = hyprial-autoupdate-e2e@example.invalid",
                "",
            )
        ),
        encoding="utf-8",
    )


def seed_remote(root: Path, env: dict[str, str]) -> tuple[Path, Path]:
    remote = root / "remote.git"
    seed = root / "seed"
    run("git", "init", "--bare", remote, env=env)
    shutil.copytree(
        ROOT,
        seed,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", "__pycache__", "*.pyc", ".pytest_cache"
        ),
    )
    run("git", "init", env=env, cwd=seed)
    run("git", "config", "user.name", "hyprial autoupdate e2e", env=env, cwd=seed)
    run(
        "git",
        "config",
        "user.email",
        "hyprial-autoupdate-e2e@example.invalid",
        env=env,
        cwd=seed,
    )
    write_project_version(seed / "pyproject.toml", INITIAL_VERSION)
    run("git", "add", ".", env=env, cwd=seed)
    run("git", "commit", "-m", "test: seed in-process autoupdate", env=env, cwd=seed)
    run("git", "tag", f"v{INITIAL_VERSION}", env=env, cwd=seed)
    run("git", "remote", "add", "origin", remote, env=env, cwd=seed)
    run("git", "push", "origin", "HEAD:dev", "--tags", env=env, cwd=seed)
    return remote, seed


def push_upgrade(seed: Path, env: dict[str, str]) -> str:
    project = seed / "pyproject.toml"
    write_project_version(project, UPGRADED_VERSION, expected=INITIAL_VERSION)
    run("git", "add", "pyproject.toml", env=env, cwd=seed)
    run("git", "commit", "-m", "test: publish in-process upgrade", env=env, cwd=seed)
    commit = run("git", "rev-parse", "HEAD", env=env, cwd=seed).strip()
    run("git", "tag", f"v{UPGRADED_VERSION}", env=env, cwd=seed)
    run("git", "push", "origin", f"v{UPGRADED_VERSION}", env=env, cwd=seed)
    return commit


def free_endpoint() -> str:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    return f"tcp/127.0.0.1:{port}"


def connector(status: dict[str, object]) -> dict[str, object]:
    rows = status.get("connectors")
    if not isinstance(rows, list):
        raise AssertionError(f"ps omitted connectors: {status}")
    for row in rows:
        if isinstance(row, dict) and row.get("id") == "codex:path-probe":
            return row
    raise AssertionError(f"ps omitted codex:path-probe: {rows}")


def main() -> int:
    uv = shutil.which("uv")
    git = shutil.which("git")
    if uv is None or git is None:
        raise AssertionError("uv and git are required")

    with tempfile.TemporaryDirectory(prefix="hyprial-au-e2e-", dir="/tmp") as raw_root:
        root = Path(raw_root)
        home = root / "home"
        hyprial_home = home / ".hyprial"
        state = hyprial_home / "state"
        tool_dir = root / "tools"
        bin_dir = root / "bin"
        cache = root / "cache"
        for directory in (home, state, tool_dir, bin_dir, cache):
            directory.mkdir(parents=True, exist_ok=True)

        environment = dict(os.environ)
        environment.update(
            {
                "HOME": str(home),
                "HYPRIAL_HOME": str(hyprial_home),
                "HARNESS_STATE_DIR": str(state),
                "HARNESS_SOCKET_PATH": str(state / "daemon.sock"),
                "HYPRIAL_OWNER": OWNER,
                "HYPRIAL_NODE_ID": NODE,
                "HYPRIAL_NETWORK_ISOLATED": "1",
                "UV_TOOL_DIR": str(tool_dir),
                "UV_TOOL_BIN_DIR": str(bin_dir),
                "UV_CACHE_DIR": str(cache),
                "UV_LINK_MODE": "copy",
                "UV_NO_PROGRESS": "1",
                "UV_PYTHON_DOWNLOADS": "never",
                "GIT_CONFIG_GLOBAL": str(root / "gitconfig"),
                "GIT_CONFIG_NOSYSTEM": "1",
            }
        )
        environment.pop("HARNESS_CONFIG", None)
        remote, seed = seed_remote(root, environment)
        git_config(Path(environment["GIT_CONFIG_GLOBAL"]), remote)
        executable = bin_dir / "hyprial"
        installed = False
        daemon_pid: int | None = None
        try:
            run(
                uv,
                "tool",
                "install",
                f"git+{GIT_SOURCE}@v{INITIAL_VERSION}",
                "--python",
                sys.executable,
                env=environment,
            )
            installed = True
            tool_python = tool_dir / "hyprial" / "bin" / "python"
            if not tool_python.exists():
                raise AssertionError("isolated uv tool Python is missing")

            # Deliberately exclude the fake Codex script's directory.  The
            # desired-state command is absolute, so both generations must
            # start it without PATH lookup (the production ENOENT criterion).
            restricted_path = os.pathsep.join(
                dict.fromkeys(
                    (
                        str(bin_dir),
                        str(Path(uv).parent),
                        str(Path(git).parent),
                        "/usr/bin",
                        "/bin",
                    )
                )
            )
            environment["PATH"] = restricted_path
            fake_codex = seed / "tests" / "fixtures" / "harnesses" / "fake_codex_app_server.py"
            (state / "desired-state.json.v1").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "asMailbox": False,
                        "providers": [
                            {
                                "provider": "codex",
                                "name": "path-probe",
                                "headless": True,
                                "command": [str(tool_python), str(fake_codex)],
                            }
                        ],
                        "interactiveSessions": [],
                    }
                ),
                encoding="utf-8",
            )

            endpoint = free_endpoint()
            started = hyprial(
                executable,
                ["init", "--listen", endpoint, "--connect", "", "--json"],
                environment,
            )
            daemon = started.get("daemon")
            # New builds answer init with the flat ping payload; older builds
            # nest the same identity under "daemon".
            if not isinstance(daemon, dict) or not daemon:
                daemon = started
            if daemon.get("running") is not True:
                raise AssertionError(f"old daemon failed to start: {started}")
            daemon_pid = int(daemon["pid"])
            old_status = ps_from_env(environment)
            old_connector = connector(old_status)
            if old_connector.get("running") is not True:
                raise AssertionError(f"absolute Codex command was not ready: {old_connector}")
            old_connector_pid = old_connector.get("pid")
            old_scheduler = hyprial(
                executable,
                ["daemon", "upgrade", "status", "--json"],
                environment,
            ).get("scheduler")
            if not isinstance(old_scheduler, dict) or old_scheduler.get("running") is not True:
                raise AssertionError(f"daemon scheduler is not running: {old_scheduler}")
            old_path_digest = old_scheduler.get("environmentPathDigest")

            upgraded_commit = push_upgrade(seed, environment)
            # Spec autoupdate-isolated-home r1: automatic upgrades default
            # OFF in code; this scenario exercises the upgrade path, so it
            # opts in explicitly the way an operator would.
            configured = hyprial(
                executable,
                ["config", "set", "autoUpgrade", "true", "--json"],
                environment,
            )
            if configured.get("value") is not True:
                raise AssertionError(f"autoUpgrade opt-in failed: {configured}")
            triggered = hyprial(
                executable,
                ["daemon", "upgrade", "run", "--json"],
                environment,
            )
            if triggered.get("triggered") is not True:
                raise AssertionError(f"daemon scheduler rejected trigger: {triggered}")

            last_run_path = state / "autoupdate-last.json"
            deadline = time.monotonic() + 240.0
            final_record: dict[str, object] | None = None
            new_status: dict[str, object] | None = None
            while time.monotonic() < deadline:
                try:
                    if last_run_path.exists():
                        candidate = json.loads(last_run_path.read_text(encoding="utf-8"))
                        if isinstance(candidate, dict) and candidate.get("ok") is True:
                            final_record = candidate
                    candidate_status = ps_from_env(environment)
                    candidate_daemon = candidate_status.get("daemon")
                    if (
                        final_record is not None
                        and isinstance(candidate_daemon, dict)
                        and candidate_daemon.get("running") is True
                        and candidate_daemon.get("pid") != daemon_pid
                    ):
                        new_status = candidate_status
                        break
                except (AssertionError, OSError, json.JSONDecodeError):
                    pass
                time.sleep(0.2)
            if final_record is None or new_status is None:
                raise AssertionError(
                    f"upgrade/restart did not converge: last={final_record} status={new_status}"
                )
            restart = final_record.get("restart")
            if not isinstance(restart, dict) or restart.get("restarted") is not True:
                raise AssertionError(f"last-run omitted successful restart: {final_record}")
            if "notification" in final_record:
                raise AssertionError(f"last-run still records a user notification: {final_record}")
            if final_record.get("resolvedCommit") != upgraded_commit:
                raise AssertionError(f"wrong upgraded commit: {final_record}")
            logged = hyprial(
                executable, ["log", "--component", "autoupdate", "--json"], environment
            ).get("entries")
            if not isinstance(logged, list) or not any(
                isinstance(entry, dict) and entry.get("event") == "autoupdate.run.started"
                for entry in logged
            ):
                raise AssertionError(f"daemon log omitted autoupdate.run.started: {logged}")

            new_connector = connector(new_status)
            if (
                new_connector.get("running") is not True
                or new_connector.get("pid") == old_connector_pid
                or "ENOENT" in str(new_connector.get("lastError") or "")
            ):
                raise AssertionError(
                    f"restricted-PATH Codex did not recover cleanly: {new_connector}"
                )
            new_scheduler = hyprial(
                executable,
                ["daemon", "upgrade", "status", "--json"],
                environment,
            ).get("scheduler")
            if (
                not isinstance(new_scheduler, dict)
                or new_scheduler.get("running") is not True
                or new_scheduler.get("environmentPathDigest") != old_path_digest
            ):
                raise AssertionError(
                    f"new daemon did not inherit the old daemon PATH: "
                    f"old={old_scheduler} new={new_scheduler}"
                )

            observed = hyprial(executable, ["version", "--json"], environment)
            if observed.get("packageVersion") != UPGRADED_VERSION:
                raise AssertionError(f"new daemon tool version is wrong: {observed}")
            new_daemon = new_status.get("daemon")
            daemon_pid = int(new_daemon["pid"]) if isinstance(new_daemon, dict) else None
            print(
                "RESULT PASS inprocess-autoupdate "
                f"version={INITIAL_VERSION}->{UPGRADED_VERSION} "
                "notice=log-only restart=fresh-pid "
                "path=inherited codex=absolute-command-ready"
            )
            return 0
        finally:
            if executable.exists() and daemon_pid is not None:
                try:
                    hyprial(executable, ["daemon", "stop", "--json"], environment)
                except Exception:
                    try:
                        os.kill(daemon_pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
            if installed:
                try:
                    run(uv, "tool", "uninstall", "hyprial", env=environment)
                except Exception:
                    pass


if __name__ == "__main__":
    try:
        exit_code = main()
    except BaseException as error:  # noqa: BLE001 - final executable boundary
        clean = one_line(error)
        clean = clip(clean)
        print(
            "RESULT FAIL inprocess-autoupdate "
            f"errorType={type(error).__name__} error={clean}",
            flush=True,
        )
        exit_code = 1
    raise SystemExit(exit_code)
