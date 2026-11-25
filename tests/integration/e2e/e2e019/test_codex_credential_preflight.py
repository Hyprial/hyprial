"""Real isolated daemon coverage for interactive Codex credential preflight.

Decision (0.5.5.3, CI run 42756 job 104888): the inaccessible-canonical case
injects the access failure as ENOTDIR at the boundary the preflight reads
(``credential.lstat()``): a regular file occupies the credential's parent
directory slot for the failing start, then the directory is restored for the
manual-repair advance. The former ``chmod(0)`` does not deny a root CI user
anything on Linux (search of a 000 directory still resolves, yielding
ENOENT ``missing``), so the failure it encoded was uid-dependent.
"""

from __future__ import annotations

import json
import shutil
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from tests.support.runtime.environment import (
    assert_root_local_environment,
    home_with_node_id,
    isolated_environment,
)


class IsolatedCodexNode:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "hyprial"
        self.state = root / "state"
        self.user_home = root / "home"
        self.work = root / "work"
        for directory in (self.home, self.state, self.user_home, self.work):
            directory.mkdir(mode=0o700)
        home_with_node_id(self.home)
        (self.home / "settings.json").write_text(
            json.dumps({"owner": "codex-preflight-owner"}), encoding="utf-8"
        )
        self.socket_path = self.state / "daemon.sock"
        fake_server = Path(__file__).with_name("fake_codex_interactive.py").resolve()
        codex_probe = self.root / "codex-probe"
        codex_probe.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "app-server" ]; then\n'
            f"  exec {shlex.quote(sys.executable)} {shlex.quote(str(fake_server))} \"$@\"\n"
            "fi\n"
            "exit 17\n",
            encoding="utf-8",
        )
        codex_probe.chmod(0o700)
        self.environment = isolated_environment(root)
        self.environment.update(
            {
                "HOME": str(self.user_home),
                "HYPRIAL_HOME": str(self.home),
                "HARNESS_STATE_DIR": str(self.state),
                "HARNESS_SOCKET_PATH": str(self.socket_path),
                "HYPRIAL_NETWORK_ISOLATED": "1",
                "HYPRIAL_ZENOH_LISTEN": "",
                "HYPRIAL_ZENOH_CONNECT": "",
                "HYPRIAL_PEER_DISCOVERY": "0",
                "HARNESS_CODEX_BIN": str(codex_probe),
            }
        )
        assert_root_local_environment(root, self.environment)
        self.process: subprocess.Popen[str] | None = None

    def start(self) -> None:
        with (self.root / "daemon.log").open("w", encoding="utf-8") as output:
            self.process = subprocess.Popen(
                [sys.executable, "-m", "hyprial.cli", "daemon", "run"],
                env=self.environment,
                stdout=output,
                stderr=subprocess.STDOUT,
                text=True,
            )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                result = self.ipc("ping", {})
            except (OSError, AssertionError):
                time.sleep(0.05)
                continue
            if result.get("running") is True and result.get("restorePending") is not True:
                assert result["zenoh"]["isolated"] is True
                return
            time.sleep(0.05)
        self.stop()
        raise AssertionError((self.root / "daemon.log").read_text(errors="replace"))

    def stop(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        process.terminate()
        process.wait(timeout=30)

    def ipc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        frame = json.dumps(
            {"version": 1, "id": uuid.uuid4().hex, "method": method, "params": params},
            separators=(",", ":"),
        ).encode()
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(15)
        try:
            client.connect(str(self.socket_path))
            client.sendall(frame + b"\n")
            response = bytearray()
            while b"\n" not in response:
                chunk = client.recv(64 * 1024)
                if not chunk:
                    raise OSError("daemon closed IPC")
                response.extend(chunk)
        finally:
            client.close()
        document = json.loads(response.partition(b"\n")[0])
        assert "error" not in document, document
        result = document.get("result")
        assert isinstance(result, dict)
        return result

    def create_agent(self, name: str, *, shared_target: Path | None = None) -> Path:
        personality = self.root / f"{name}-personality"
        native = personality / "native" / "codex"
        native.mkdir(parents=True, mode=0o700)
        (native / "config.toml").write_text(
            'cli_auth_credentials_store = "file"\n', encoding="utf-8"
        )
        config: dict[str, Any] = {
            "sources": [{"path": str(personality), "required": True}],
            "discovery": "explicit-only",
        }
        if shared_target is not None:
            config["sharedCredentials"] = {"codex": str(shared_target)}
        self.ipc(
            "agent.create",
            {"name": name, "cwd": str(self.work), "config": config},
        )
        credential = self.home / "agents" / name / "secrets" / "native" / "codex" / "auth.json"
        credential.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        credential.parent.parent.parent.chmod(0o700)
        credential.parent.parent.chmod(0o700)
        credential.parent.parent.parent.chmod(0o700)
        credential.parent.chmod(0o700)
        if shared_target is not None:
            credential.symlink_to(shared_target)
        return credential

    def install_operator_credential(self) -> None:
        source = self.user_home / ".codex" / "auth.json"
        source.parent.mkdir(mode=0o700)
        source.write_text('{"access_token":"isolated-test"}\n', encoding="utf-8")
        source.chmod(0o600)

    def apply_command(self, command: str) -> dict[str, Any]:
        arguments = shlex.split(command)
        if arguments[0] == "hyprial":
            arguments = [sys.executable, "-m", "hyprial.cli", *arguments[1:], "--json"]
        result = subprocess.run(
            arguments,
            cwd=self.work,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result
        return json.loads(result.stdout) if result.stdout else {"ok": True}

    def start_codex(
        self, name: str
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "hyprial.cli",
                "agent",
                "start",
                "codex",
                "--name",
                name,
                "--cwd",
                str(self.work),
                "--json",
            ],
            cwd=self.work,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return result, json.loads(result.stdout)


@pytest.fixture
def node() -> IsolatedCodexNode:
    root = Path(tempfile.mkdtemp(prefix="codex-preflight-", dir="/tmp")).resolve()
    isolated = IsolatedCodexNode(root)
    isolated.start()
    try:
        yield isolated
    finally:
        isolated.stop()
        shutil.rmtree(root)


_LATER_STAGE_CODES = frozenset({"CODEX_ATTACH_REGISTRATION_FAILED"})


def _assert_command_error(
    payload: dict[str, Any], *, reason: str, path: Path
) -> str:
    assert payload["code"] == "CODEX_CREDENTIAL_MISSING"
    assert payload["data"]["reason"] == reason
    assert payload["data"]["path"] == str(path.resolve(strict=False))
    remediation = payload["data"]["remediation"]
    assert remediation["kind"] == "command"
    return str(remediation["command"])


def _assert_manual_error(
    payload: dict[str, Any], *, reason: str, path: Path
) -> str:
    assert payload["code"] == "CODEX_CREDENTIAL_MISSING"
    assert payload["data"]["reason"] == reason
    resolved = path.resolve(strict=False)
    assert payload["data"]["path"] == str(resolved)
    remediation = payload["data"]["remediation"]
    assert remediation["kind"] == "manual"
    instruction = str(remediation["instruction"])
    assert str(resolved) in instruction
    assert "no hyprial cli command" in instruction.lower()
    assert "hyprial agent provider login" not in json.dumps(payload)
    return instruction


def _assert_start_advanced(
    result: subprocess.CompletedProcess[str], payload: dict[str, Any]
) -> None:
    assert (result.returncode == 0 and payload.get("ok") is True) or (
        result.returncode == 1 and payload.get("code") in _LATER_STAGE_CODES
    ), payload


def _repair_with_credential(path: Path) -> None:
    path.write_text('{"access_token":"isolated-test"}\n', encoding="utf-8")
    path.chmod(0o600)


def test_real_start_classifies_missing_canonical_and_login_advances(
    node: IsolatedCodexNode,
) -> None:
    credential = node.create_agent("canonical-missing")
    node.install_operator_credential()

    first, payload = node.start_codex("canonical-missing")

    assert first.returncode == 1
    command = _assert_command_error(payload, reason="missing", path=credential)
    assert node.apply_command(command)["ok"] is True

    _assert_start_advanced(*node.start_codex("canonical-missing"))


def test_real_start_classifies_empty_canonical_and_forced_login_advances(
    node: IsolatedCodexNode,
) -> None:
    credential = node.create_agent("canonical-empty")
    credential.write_bytes(b"")
    credential.chmod(0o600)
    node.install_operator_credential()

    first, payload = node.start_codex("canonical-empty")

    assert first.returncode == 1
    command = _assert_command_error(payload, reason="empty", path=credential)
    assert "--force" in shlex.split(command)
    assert node.apply_command(command)["ok"] is True

    _assert_start_advanced(*node.start_codex("canonical-empty"))


def test_real_start_classifies_permissions_and_chmod_advances(
    node: IsolatedCodexNode,
) -> None:
    credential = node.create_agent("canonical-permissions")
    _repair_with_credential(credential)
    credential.chmod(0o644)

    first, payload = node.start_codex("canonical-permissions")

    assert first.returncode == 1
    command = _assert_command_error(payload, reason="permissions", path=credential)
    assert node.apply_command(command)["ok"] is True

    _assert_start_advanced(*node.start_codex("canonical-permissions"))


def test_real_start_classifies_not_regular_and_manual_repair_advances(
    node: IsolatedCodexNode,
) -> None:
    credential = node.create_agent("canonical-not-regular")
    credential.mkdir()

    first, payload = node.start_codex("canonical-not-regular")

    assert first.returncode == 1
    _assert_manual_error(payload, reason="not-regular", path=credential)
    credential.rmdir()
    _repair_with_credential(credential)

    _assert_start_advanced(*node.start_codex("canonical-not-regular"))


@pytest.mark.parametrize("initial", ["missing", "empty"])
def test_real_start_classifies_shared_target_and_manual_repair_advances(
    node: IsolatedCodexNode, initial: str
) -> None:
    target = node.root / f"shared-{initial}" / "auth.json"
    target.parent.mkdir(mode=0o700)
    if initial == "empty":
        target.write_bytes(b"")
        target.chmod(0o600)
    node.create_agent(f"shared-{initial}", shared_target=target)

    first, payload = node.start_codex(f"shared-{initial}")

    assert first.returncode == 1
    instruction = _assert_manual_error(
        payload, reason="shared-target", path=target
    )
    assert "config.sharedCredentials.codex" in instruction
    _repair_with_credential(target)

    _assert_start_advanced(*node.start_codex(f"shared-{initial}"))


def test_real_start_classifies_inaccessible_and_manual_repair_advances(
    node: IsolatedCodexNode,
) -> None:
    credential = node.create_agent("canonical-inaccessible")
    canonical = credential.resolve(strict=False)
    parent = credential.parent
    parent.rmdir()
    parent.write_bytes(b"")
    try:
        first, payload = node.start_codex("canonical-inaccessible")
    finally:
        parent.unlink()
        parent.mkdir(mode=0o700)
        parent.chmod(0o700)

    assert first.returncode == 1
    _assert_manual_error(payload, reason="inaccessible", path=canonical)
    _repair_with_credential(credential)

    _assert_start_advanced(*node.start_codex("canonical-inaccessible"))
    assert stat.S_IMODE(parent.stat().st_mode) == 0o700
