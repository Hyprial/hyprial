"""Orchestrator tests: the full cold-migration sequence with a fake remote.

The runner seam is the only fake: daemon answers come from a scripted
``local_request``, so ordering, payloads, fencing and rollback are all
asserted against the real orchestration code.
"""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.transfer import orchestrator as xfer_orchestrator
from hyprial.daemon.impl.transfer.archive.envelope import generate_keypair
from hyprial.daemon.impl.transfer.execution.container import DockerRunner, default_image
from hyprial.daemon.impl.transfer.orchestrator import TransferError, run_transfer
from hyprial.daemon.impl.transfer.execution.ssh import RemoteError, SshRunner

OWNER = "alice"
SOURCE = "laptop"
TARGET = "server"


class FakeRemote:
    def __init__(self, node: str = TARGET, owner: str = OWNER) -> None:
        self.host = "fake-host"
        self.remote_hyprial = "hyprial"
        self.ps = {"ok": True, "daemon": {
            "nodeId": node, "owner": owner, "socket": "/remote/state/daemon.sock",
        }}
        self.precheck: dict[str, Any] = {"ok": True, "conflicts": []}
        self.receive: dict[str, Any] = {
            "ok": True,
            "actor": f"agent:{owner}:{node}:wk",
            "sessionRef": "session-1",
        }
        # The target's credential-envelope key: the chain asks for the public
        # half before it stops the source and seals what it carries.
        self._recipient_private, self.recipient_public = generate_keypair()
        self.binaries = {"pi", "codex", "claude", "uv"}
        self.calls: list[list[str]] = []
        self.uploads: list[tuple[Path, str]] = []
        self.syncs: list[tuple[Path, str]] = []
        self.receive_payload: dict[str, Any] | None = None
        self.fail_on: list[str] | None = None
        self.fail_ship = False
        #: Fail ``sync_tree`` only when the LOCAL source is this path, so the
        #: agent-home chain's worktree ship can fail after the bundle shipped.
        self.fail_ship_local: Path | None = None
        self.dirs: list[str] = []
        self.removed: list[str] = []

    def run_json(self, argv: list[str], stdin: bytes | None = None) -> dict:
        self.calls.append(argv)
        if self.fail_on == argv[:3]:
            raise RemoteError(self.host, tuple(argv), "boom")
        if argv[:2] == ["daemon", "status"]:
            return self.ps
        if argv[:3] == ["agent", "transfer", "precheck"]:
            return self.precheck
        if argv[:3] == ["agent", "transfer", "session-path"]:
            return {"ok": True, "path": "/remote/sessions/session-1.jsonl"}
        if argv[:3] == ["agent", "transfer", "recipient-key"]:
            return {
                "ok": True,
                "algorithm": "x25519",
                "publicKey": base64.b64encode(self.recipient_public).decode("ascii"),
            }
        if argv[:3] == ["agent", "transfer", "receive"]:
            assert stdin is not None
            self.receive_payload = json.loads(stdin.decode())
            return self.receive
        raise AssertionError(f"unexpected remote call: {argv}")

    def has_binary(self, name: str) -> bool:
        return name in self.binaries

    def image_present(self, image: str) -> bool:
        return False

    def login_user(self) -> str:
        return xfer_orchestrator._local_login_user()

    def upload(self, local: Path, remote_path: str) -> None:
        # Snapshot the bytes NOW: the orchestrator deletes temp adaptations
        # after the upload, and the content is the contract under test.
        self.uploads.append((Path(local), remote_path))
        self.uploaded_content = Path(local).read_bytes()

    def sync_tree(self, local_dir: Path, remote_dir: str) -> None:
        if self.fail_ship or (
            self.fail_ship_local is not None
            and Path(local_dir) == self.fail_ship_local
        ):
            raise RemoteError(self.host, ("rsync", str(local_dir)), "ship failed")
        self.syncs.append((Path(local_dir), remote_dir))

    def make_dirs(self, remote_dir: str) -> None:
        self.dirs.append(remote_dir)

    def remove_tree(self, remote_dir: str) -> None:
        self.removed.append(remote_dir)


class FakeLocal:
    """Scripted local answers; lifecycle and discovery calls recorded separately."""

    def __init__(self) -> None:
        # Actual hosts schema: application.py hosts / test_target_kind_honesty.
        # In particular, a host row has NO owner field.
        self.hosts_response: Any = {"hosts": [{"nodeId": TARGET, "status": "online"}]}
        self.discovery_calls: list[tuple[str, dict[str, Any]]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        #: Keyword options the orchestrator passed with each non-discovery call.
        self.options: list[dict[str, Any]] = []
        self.spec: dict[str, Any] = {
            "provider": "claude",
            "name": "wk",
            "headless": True,
            "args": [],
            "ownership": "managed",
            "cwd": "/work/proj",
            "sessionRef": "session-1",
        }
        self.plan: dict[str, Any] = {
            "ok": True,
            "spec": self.spec,
            "actor": f"agent:{OWNER}:{SOURCE}:wk",
            "nodeId": SOURCE,
            "owner": OWNER,
            "agent": {"actor": "wk", "pinnedAdapters": ["lark-1"]},
            "unreadInbox": 0,
        }
        self.quiesce: dict[str, Any] = {**self.plan, "stopped": True}
        self.resume: dict[str, Any] = {"ok": True, "sessionRef": "session-1"}
        self.complete: dict[str, Any] = {"ok": True, "removedAgent": True}
        self.override: dict[str, Any] = {"ok": True, "state": "held-overridden"}
        self.fail_on: str | None = None
        # Typed refusals (codes the daemon owns) as well as plain crashes.
        self.raise_on: dict[str, BaseException] = {}

    def __call__(
        self, method: str, params: dict[str, Any], **options: Any
    ) -> dict[str, Any]:
        if method == "hosts":
            self.discovery_calls.append((method, params))
            return self.hosts_response
        self.calls.append((method, params))
        self.options.append(options)
        if method in self.raise_on:
            raise self.raise_on[method]
        if self.fail_on == method:
            raise RuntimeError(f"{method} exploded")
        return {
            "transfer.plan": self.plan,
            "transfer.quiesce": self.quiesce,
            "transfer.resume": self.resume,
            "transfer.complete": self.complete,
            "transfer.custody.override": self.override,
        }[method]


def _seed_claude_session(home: Path, cwd: str = "/work/proj") -> Path:
    path = home / ".claude" / "projects" / "-work-proj" / "session-1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"type":"session"}\n', encoding="utf-8")
    return path


def _run(home: Path, local: FakeLocal, remote: FakeRemote, **overrides: Any) -> dict:
    kwargs: dict[str, Any] = {
        "name": "wk",
        "harness": None,
        "host": TARGET,
        "target_cwd": None,
        "dry_run": False,
        "yes": True,
        "strict_timeout": 5.0,
        "local_request": local,
        "remote": remote,
        "home": home,
    }
    kwargs.update(overrides)
    return run_transfer(**kwargs)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_runs_the_full_sequence_in_order(tmp_path: Path) -> None:
    session = _seed_claude_session(tmp_path)
    local, remote = FakeLocal(), FakeRemote()

    result = _run(tmp_path, local, remote)

    assert result["ok"] is True
    assert result["from"] == f"agent:{OWNER}:{SOURCE}:wk"
    assert result["to"] == f"agent:{OWNER}:{TARGET}:wk"
    assert result["resumeVerified"] is True
    # Local daemon calls in order: plan -> quiesce -> complete.
    assert [method for method, _ in local.calls] == [
        "transfer.plan",
        "transfer.quiesce",
        "transfer.complete",
    ]
    # Remote calls in order: daemon status -> precheck -> session-path -> receive.
    assert [argv[2] if argv[:2] == ["agent", "transfer"] else argv[0] for argv in remote.calls] == [
        "daemon",
        "precheck",
        "session-path",
        "receive",
    ]
    assert remote.syncs == [(Path("/work/proj"), "/work/proj")]
    assert remote.uploads == [(session, "/remote/sessions/session-1.jsonl")]
    # The receive payload carries the spec, the transferred pins, strict timeout.
    assert remote.receive_payload is not None
    assert remote.receive_payload["spec"]["sessionRef"] == "session-1"
    assert remote.receive_payload["pins"] == ["lark-1"]
    assert remote.receive_payload["strictTimeoutSeconds"] == 5.0


def test_cwd_override_rewrites_the_spec_and_the_session_target(
    tmp_path: Path,
) -> None:
    _seed_claude_session(tmp_path)
    local, remote = FakeLocal(), FakeRemote()

    result = _run(tmp_path, local, remote, target_cwd="/srv/work")

    assert result["cwd"] == "/srv/work"
    assert remote.syncs == [(Path("/work/proj"), "/srv/work")]
    assert remote.receive_payload["spec"]["cwd"] == "/srv/work"  # type: ignore[index]
    session_path_call = next(
        argv for argv in remote.calls if argv[:3] == ["agent", "transfer", "session-path"]
    )
    assert session_path_call[session_path_call.index("--cwd") + 1] == "/srv/work"


def test_dry_run_validates_everything_and_changes_nothing(tmp_path: Path) -> None:
    session = _seed_claude_session(tmp_path)
    local, remote = FakeLocal(), FakeRemote()

    result = _run(tmp_path, local, remote, dry_run=True)

    assert result["dryRun"] is True
    assert result["sessionFile"] == str(session)
    assert result["sessionFileBytes"] == session.stat().st_size
    # Validation ran (plan, fencing, precheck) but nothing was stopped.
    assert [method for method, _ in local.calls] == ["transfer.plan"]
    assert all(argv[:3] != ["agent", "transfer", "receive"] for argv in remote.calls)
    assert remote.syncs == [] and remote.uploads == []






@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("containerized", [False, True])
def test_orchestrator_defaults_to_no_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool, containerized: bool
) -> None:
    local, remote = FakeLocal(), FakeRemote()
    local.spec["sessionRef"] = None
    messages = []

    def no_process(*args, **kwargs):
        pytest.fail("unconfirmed transfer must not execute transport/Docker")

    monkeypatch.setattr(subprocess, "run", no_process)
    with pytest.raises(TransferError) as caught:
        # Deliberately omit yes, including for dry-run and container mode.
        run_transfer(
            name="wk", harness=None, host=TARGET, target_cwd=None,
            dry_run=dry_run, strict_timeout=5, local_request=local, remote=remote,
            home=tmp_path, emit=messages.append, containerized=containerized,
        )
    assert caught.value.code == "TRANSFER_CONFIRMATION_REQUIRED"
    assert caught.value.data["actor"] == local.plan["actor"]
    assert "--yes" in str(caught.value)
    assert any("daemon.socket" in message for message in messages)
    assert remote.calls == [["daemon", "status"]]
    assert [method for method, _ in local.calls] == ["transfer.plan"]
    assert not remote.syncs and not remote.uploads


# ---------------------------------------------------------------------------
# Fencing and admission
# ---------------------------------------------------------------------------


def test_same_node_id_is_a_hard_fencing_error(tmp_path: Path) -> None:
    _seed_claude_session(tmp_path)
    local, remote = FakeLocal(), FakeRemote(node=SOURCE)
    local.hosts_response = {"hosts": [{"nodeId": SOURCE, "status": "online"}]}

    with pytest.raises(TransferError) as failure:
        _run(tmp_path, local, remote, host=SOURCE)

    assert failure.value.code == "TRANSFER_NODE_CONFLICT"
    assert [method for method, _ in local.calls] == ["transfer.plan"]


@pytest.mark.parametrize("host", [TARGET, f"bobadmin@{TARGET}"])
def test_discovered_node_is_selected_from_current_home(tmp_path: Path, host: str) -> None:
    _seed_claude_session(tmp_path)
    local, remote = FakeLocal(), FakeRemote()
    result = _run(tmp_path, local, remote, host=host, dry_run=True)
    assert result["toNode"] == TARGET
    assert local.discovery_calls == [("hosts", {})]
    assert local.hosts_response == {"hosts": [{"nodeId": TARGET, "status": "online"}]}


@pytest.mark.parametrize("host", ["unknown", "user@unknown", "server.alias"])
def test_undiscovered_destination_never_reaches_ssh(tmp_path: Path, host: str) -> None:
    local, remote = FakeLocal(), FakeRemote()
    with pytest.raises(TransferError) as caught:
        _run(tmp_path, local, remote, host=host)
    assert caught.value.code == "TRANSFER_TARGET_UNDISCOVERED"
    assert host in str(caught.value)
    assert "hyprial host list" in str(caught.value)
    assert remote.calls == [] and local.calls == []
    assert local.discovery_calls == [("hosts", {})]


@pytest.mark.parametrize("payload", [None, {}, {"hosts": None}, {"hosts": [None]},
                                     {"hosts": [{"nodeId": 123, "status": "online"}]}])
def test_malformed_discovery_fails_closed(tmp_path: Path, payload: Any) -> None:
    # Malformed responses are fault injection, not fabricated positive rows.
    local, remote = FakeLocal(), FakeRemote()
    local.hosts_response = payload
    with pytest.raises(TransferError) as caught:
        _run(tmp_path, local, remote)
    assert caught.value.code == "TRANSFER_PROTOCOL"
    assert remote.calls == [] and local.calls == []


def test_duplicate_discovery_rows_fail_closed(tmp_path: Path) -> None:
    local, remote = FakeLocal(), FakeRemote()
    local.hosts_response["hosts"] *= 2  # malformed duplicated wire response
    with pytest.raises(TransferError) as caught:
        _run(tmp_path, local, remote)
    assert caught.value.code == "TRANSFER_TARGET_AMBIGUOUS"
    assert remote.calls == [] and local.calls == []


@pytest.mark.parametrize("host", ["", "@server", "user@", "a@b@server", "-F", "user@server x"])
def test_invalid_destination_never_reaches_ssh(tmp_path: Path, host: str) -> None:
    local, remote = FakeLocal(), FakeRemote()
    with pytest.raises(TransferError) as caught:
        _run(tmp_path, local, remote, host=host)
    assert caught.value.code == "TRANSFER_TARGET_INVALID"
    assert remote.calls == [] and local.calls == []


@pytest.mark.parametrize("actual_node", ["wrong-server", None, "", 123])
@pytest.mark.parametrize("containerized", [False, True])
def test_remote_node_mismatch_precedes_owner_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, actual_node: Any, containerized: bool
) -> None:
    def no_process(*args, **kwargs):
        pytest.fail("node fencing must reject before Docker or transport subprocesses")

    monkeypatch.setattr(subprocess, "run", no_process)
    local, remote = FakeLocal(), FakeRemote(owner="different-owner")
    remote.ps["daemon"]["nodeId"] = actual_node
    with pytest.raises(TransferError) as caught:
        _run(tmp_path, local, remote, containerized=containerized)
    assert caught.value.code == "TRANSFER_TARGET_MISMATCH"
    assert repr(TARGET) in str(caught.value)
    assert repr(actual_node) in str(caught.value)
    assert caught.value.data == {"hostsNodeId": TARGET, "remoteNodeId": actual_node}
    assert remote.calls == [["daemon", "status"]]  # before admission, toolchain or ship
    assert [method for method, _ in local.calls] == ["transfer.plan"]
    assert not remote.syncs and not remote.uploads


def test_owner_mismatch_is_refused(tmp_path: Path) -> None:
    _seed_claude_session(tmp_path)
    local, remote = FakeLocal(), FakeRemote(owner="bob")

    with pytest.raises(TransferError) as failure:
        _run(tmp_path, local, remote)

    assert failure.value.code == "TRANSFER_OWNER_MISMATCH"


def test_target_conflicts_stop_before_quiesce(tmp_path: Path) -> None:
    _seed_claude_session(tmp_path)
    local, remote = FakeLocal(), FakeRemote()
    remote.precheck = {"ok": True, "conflicts": ["registered agent 'wk'"]}

    with pytest.raises(TransferError) as failure:
        _run(tmp_path, local, remote)

    assert failure.value.code == "TRANSFER_CONFLICT"
    assert "registered agent" in str(failure.value)
    assert [method for method, _ in local.calls] == ["transfer.plan"]


def test_missing_harness_binary_stops_before_quiesce(tmp_path: Path) -> None:
    _seed_claude_session(tmp_path)
    local, remote = FakeLocal(), FakeRemote()
    remote.binaries.discard("claude")

    with pytest.raises(TransferError) as failure:
        _run(tmp_path, local, remote)

    assert failure.value.code == "TRANSFER_PREREQUISITE"
    assert [method for method, _ in local.calls] == ["transfer.plan"]


@pytest.mark.parametrize("session_present", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize(
    ("containerized", "missing_target", "source_state"),
    [
        (True, {"docker"}, "no-image"),  # one missing item at each end
        (True, {"docker", "uv", "claude", "pi"}, "no-docker"),
        (True, {"uv"}, "stopped"),
        (True, {"pi"}, "timeout"),
        (True, {"claude"}, "denied"),
        (True, {"uv", "claude", "pi"}, "ready"),
        (True, set(), "ready"),
        (False, {"uv", "claude", "pi"}, "no-docker"),
        (False, set(), "no-docker"),  # bare transfer must not need Docker
    ],
)
def test_preflight_collects_both_ends_before_any_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    containerized: bool,
    missing_target: set[str],
    source_state: str,
    dry_run: bool,
    session_present: bool,
) -> None:
    if session_present:
        _seed_claude_session(tmp_path)
    local, remote = FakeLocal(), FakeRemote()
    calls: list[list[str]] = []
    image = default_image()

    def fake_process(argv, **kwargs):
        # Exercise the actual SshRunner and DockerRunner. These are POSIX
        # exit statuses / Docker CLI errors, not made-up preflight JSON.
        calls.append(list(argv))
        if argv[:2] == ["ssh", "bobadmin@node-hq-01"]:
            if argv[2:4] == ["command", "-v"]:
                binary = argv[4]
                found = binary not in missing_target
                return subprocess.CompletedProcess(
                    argv, 0 if found else 1,
                    f"/usr/bin/{binary}\n".encode() if found else b"", b"",
                )
            assert argv[2:] in [
                ["test", "-x", "/usr/local/bin/docker"],
                ["test", "-x", "/opt/homebrew/bin/docker"],
            ]
            return subprocess.CompletedProcess(argv, 1, b"", b"")
        assert argv in [["docker", "info"], ["docker", "image", "inspect", image]]
        if source_state == "no-docker":
            raise FileNotFoundError("docker")
        if source_state == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        if source_state == "denied":
            raise PermissionError("docker: permission denied")
        if source_state == "stopped":
            return subprocess.CompletedProcess(
                argv, 1, b"", b"Cannot connect to the Docker daemon"
            )
        if source_state == "no-image" and argv[1] == "image":
            return subprocess.CompletedProcess(
                argv, 1, b"", f"Error response from daemon: No such image: {image}".encode()
            )
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_process)
    remote.has_binary = SshRunner("bobadmin@node-hq-01").has_binary
    expected = [f"target {TARGET}: {binary}" for binary in sorted(missing_target)]
    if containerized and source_state not in {"ready", "no-image"}:
        expected.append("source docker")
    if containerized and source_state != "ready":
        expected.append(f"source worker image {image!r}")
    if not session_present:
        expected.append("source claude session file")
    kwargs = dict(containerized=containerized, with_credentials=False,
                  docker=DockerRunner(), dry_run=dry_run or not expected)
    if expected:
        with pytest.raises(TransferError) as caught:
            _run(tmp_path, local, remote, **kwargs)
        assert caught.value.code == "TRANSFER_PREREQUISITE"
        for item in expected:
            assert item in str(caught.value)
        assert caught.value.data is not None
        assert len(caught.value.data["missing"]) == len(expected)
        assert "non-interactive SSH PATH" in str(caught.value) or not missing_target - {"docker"}
    else:
        assert _run(tmp_path, local, remote, **kwargs)["dryRun"] is True
    for binary in ("uv", "claude", "pi"):
        assert ["ssh", "bobadmin@node-hq-01", "command", "-v", binary] in calls
    if containerized:
        assert ["ssh", "bobadmin@node-hq-01", "command", "-v", "docker"] in calls
        assert ["docker", "info"] in calls
        assert ["docker", "image", "inspect", image] in calls
    else:
        assert not any(call[0] == "docker" for call in calls)
    assert [method for method, _ in local.calls] == ["transfer.plan"]
    assert remote.uploads == [] and remote.syncs == []
    assert remote.receive_payload is None


@pytest.mark.parametrize("session_present", [False, True])
@pytest.mark.parametrize(
    ("containerized", "with_credentials", "credential", "source_missing", "target_missing"),
    [
        (True, True, "absent", True, True),
        (True, True, "absent", False, False),
        (True, True, "directory", False, False),
        (True, True, "file", True, False),
        (True, True, "file", False, False),
        (True, False, "absent", False, False),
        (False, True, "absent", False, False),
    ],
)
def test_preflight_lists_claude_credentials_with_all_other_source_gaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, containerized: bool,
    with_credentials: bool, credential: str, source_missing: bool, target_missing: bool,
    session_present: bool,
) -> None:
    local, remote = FakeLocal(), FakeRemote()
    if session_present:
        _seed_claude_session(tmp_path)
    if not target_missing:
        remote.binaries.add("docker")
    else:
        remote.binaries.clear()
    path = tmp_path / ".claude" / ".credentials.json"
    if credential != "absent":
        path.parent.mkdir(exist_ok=True)
        if credential == "file":
            path.write_text("synthetic credential sentinel; never read")
        else:
            path.mkdir()
    calls = []
    image = default_image()

    def fake_process(argv, **kwargs):
        calls.append(argv)
        assert argv in [["docker", "info"], ["docker", "image", "inspect", image]]
        if source_missing:
            raise FileNotFoundError("docker")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    original_open = Path.open

    def no_credential_read(candidate, *args, **kwargs):
        if candidate == path:
            pytest.fail("preflight checks existence only; never read credential contents")
        return original_open(candidate, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_process)
    monkeypatch.setattr(Path, "open", no_credential_read)
    credential_missing = containerized and with_credentials and credential != "file"
    expected = []
    if target_missing:
        expected.extend(f"target {TARGET}: {binary}" for binary in ("uv", "claude", "pi"))
        if containerized:
            expected.append(f"target {TARGET}: docker")
    if containerized and source_missing:
        expected.extend(["source docker", f"source worker image {image!r}"])
    if credential_missing:
        expected.append("source claude credentials")
    if not session_present:
        expected.append("source claude session file")
    kwargs = dict(containerized=containerized, with_credentials=with_credentials,
                  docker=DockerRunner(), dry_run=not expected)
    if expected:
        with pytest.raises(TransferError) as caught:
            _run(tmp_path, local, remote, **kwargs)
        assert caught.value.code == "TRANSFER_PREREQUISITE"
        assert len(caught.value.data["missing"]) == len(expected)
        for label in expected:
            assert any(label in item for item in caught.value.data["missing"])
            assert label in str(caught.value)
        if credential_missing:
            assert ".claude/.credentials.json" in str(caught.value)
            assert str(tmp_path) in str(caught.value)
            assert "operator-authorized" in str(caught.value)
    else:
        assert _run(tmp_path, local, remote, **kwargs)["dryRun"] is True
    assert calls == ([["docker", "info"], ["docker", "image", "inspect", image]] if containerized else [])
    assert [method for method, _ in local.calls] == ["transfer.plan"]
    assert remote.syncs == [] and remote.uploads == []


@pytest.mark.parametrize("harness", ["pi", "codex"])
def test_non_claude_credential_errors_keep_existing_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    local, remote = FakeLocal(), FakeRemote()
    local.spec.update(provider=harness, sessionRef=None)
    remote.binaries.add("docker")

    def fake_process(argv, **kwargs):
        assert argv in [["docker", "info"], ["docker", "image", "inspect", default_image()]]
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_process)
    with pytest.raises(TransferError) as caught:
        _run(tmp_path, local, remote, containerized=True)
    assert caught.value.code == "TRANSFER_CREDENTIALS"
    assert [method for method, _ in local.calls] == ["transfer.plan"]


def test_preflight_probe_error_does_not_skip_other_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_claude_session(tmp_path)
    local, remote = FakeLocal(), FakeRemote()
    calls = []
    image = "hyprial-worker:custom-test"

    def fake_process(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["ssh", "bobadmin@node-hq-01"]:
            assert argv[2:4] == ["command", "-v"]
            if argv[4] == "uv":
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            return subprocess.CompletedProcess(argv, 0, f"/usr/bin/{argv[4]}\n".encode(), b"")
        if argv == ["docker", "info"]:
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        assert argv == ["docker", "image", "inspect", image]
        return subprocess.CompletedProcess(argv, 1, b"", b"No such image")

    monkeypatch.setattr(subprocess, "run", fake_process)
    remote.has_binary = SshRunner("bobadmin@node-hq-01").has_binary
    with pytest.raises(TransferError) as caught:
        _run(tmp_path, local, remote, containerized=True, container_image=image, with_credentials=False)
    assert caught.value.code == "TRANSFER_PREREQUISITE"
    assert f"target {TARGET}: uv could not be checked" in str(caught.value)
    assert f"source worker image {image!r}" in str(caught.value)
    assert len(caught.value.data["missing"]) == 2
    assert len(calls) == 6  # all four remote probes, local info AND image inspect
    assert [method for method, _ in local.calls] == ["transfer.plan"]
    assert remote.uploads == [] and remote.syncs == []


@pytest.mark.parametrize("harness", ["claude", "pi", "codex"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_missing_session_file_fails_before_quiesce(
    tmp_path: Path, harness: str, dry_run: bool
) -> None:
    local, remote = FakeLocal(), FakeRemote()  # no session file seeded
    local.spec["provider"] = harness

    with pytest.raises(TransferError) as failure:
        _run(tmp_path, local, remote, dry_run=dry_run)

    assert failure.value.code == "TRANSFER_PREREQUISITE"
    missing = failure.value.data["missing"]
    assert len(missing) == 1
    assert f"source {harness} session file" in missing[0]
    assert "session-1" in missing[0]
    assert str(tmp_path) in missing[0]
    assert "先跑一个 turn" in missing[0]
    assert [method for method, _ in local.calls] == ["transfer.plan"]
    assert not remote.uploads and not remote.syncs
    assert remote.receive_payload is None


def test_ambiguous_session_joins_preflight_without_first_turn_advice(tmp_path: Path) -> None:
    local, remote = FakeLocal(), FakeRemote()
    for project in ("old-one", "old-two"):
        path = tmp_path / ".claude" / "projects" / project / "session-1.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text('{"type":"session"}\n')
    remote.binaries.discard("uv")
    with pytest.raises(TransferError) as caught:
        _run(tmp_path, local, remote)
    assert caught.value.code == "TRANSFER_PREREQUISITE"
    assert len(caught.value.data["missing"]) == 2
    assert "multiple project dirs" in str(caught.value)
    assert "先跑一个 turn" not in str(caught.value)
    assert [method for method, _ in local.calls] == ["transfer.plan"]
    assert not remote.uploads and not remote.syncs


def test_session_locator_directory_match_is_not_a_transferable_file(tmp_path: Path) -> None:
    local, remote = FakeLocal(), FakeRemote()
    path = tmp_path / ".claude" / "projects" / "old-project" / "session-1.jsonl"
    path.mkdir(parents=True)
    with pytest.raises(TransferError) as caught:
        _run(tmp_path, local, remote)
    assert caught.value.code == "TRANSFER_PREREQUISITE"
    assert "not a regular session file" in str(caught.value)
    assert "先跑一个 turn" not in str(caught.value)
    assert [method for method, _ in local.calls] == ["transfer.plan"]
    assert not remote.uploads and not remote.syncs


def test_session_file_appearing_after_first_turn_passes_preflight(tmp_path: Path) -> None:
    local, remote = FakeLocal(), FakeRemote()
    with pytest.raises(TransferError, match="先跑一个 turn"):
        _run(tmp_path, local, remote, dry_run=True)
    # Model only the on-disk result of a turn, not an actual harness call.
    path = _seed_claude_session(tmp_path)
    result = _run(tmp_path, local, remote, dry_run=True)
    assert result["sessionFile"] == str(path)
    assert result["sessionRef"] == "session-1"
    assert result["from"] == local.plan["actor"]
    assert [method for method, _ in local.calls] == ["transfer.plan", "transfer.plan"]
    assert not remote.uploads and not remote.syncs


def test_worker_without_a_ref_transfers_without_session_steps(
    tmp_path: Path,
) -> None:
    local, remote = FakeLocal(), FakeRemote()
    local.spec["sessionRef"] = None
    local.plan["spec"] = local.spec
    local.quiesce["spec"] = local.spec
    remote.receive["sessionRef"] = None

    result = _run(tmp_path, local, remote)

    assert result["ok"] is True
    assert result["sessionRef"] is None
    assert remote.uploads == []
    assert all(argv[:3] != ["agent", "transfer", "session-path"] for argv in remote.calls)


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


def test_receive_failure_rolls_the_source_back(tmp_path: Path) -> None:
    _seed_claude_session(tmp_path)
    local, remote = FakeLocal(), FakeRemote()
    remote.fail_on = ["agent", "transfer", "receive"]

    with pytest.raises(TransferError) as failure:
        _run(tmp_path, local, remote)

    assert failure.value.code == "TRANSFER_REMOTE_FAILED"
    assert [method for method, _ in local.calls] == [
        "transfer.plan",
        "transfer.quiesce",
        "transfer.resume",
    ]
    # The rollback carried the quiesced spec (with its ref).
    resume_call = local.calls[-1]
    assert resume_call[1]["spec"]["sessionRef"] == "session-1"
    assert failure.value.data["rollback"]["ok"] is True  # type: ignore[index]
    # B4: the message may claim a rollback only when one happened.
    assert "the source was rolled back" in str(failure.value)


def test_ship_failure_rolls_the_source_back(tmp_path: Path) -> None:
    _seed_claude_session(tmp_path)
    local, remote = FakeLocal(), FakeRemote()
    remote.fail_on = ["agent", "transfer", "session-path"]

    with pytest.raises(TransferError) as failure:
        _run(tmp_path, local, remote)

    assert failure.value.code == "TRANSFER_REMOTE_FAILED"
    assert local.calls[-1][0] == "transfer.resume"


def test_a_failing_rollback_is_reported_not_masked(tmp_path: Path) -> None:
    _seed_claude_session(tmp_path)
    local, remote = FakeLocal(), FakeRemote()
    remote.fail_on = ["agent", "transfer", "receive"]

    def wrapped(method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "transfer.resume":
            raise RuntimeError("resume exploded")
        return local(method, params)

    with pytest.raises(TransferError) as failure:
        _run(tmp_path, local, remote, local_request=wrapped)

    assert failure.value.data["rollbackError"] == "resume exploded"  # type: ignore[index]
    assert failure.value.data["rollback"] is None  # type: ignore[index]
    # B4: "the source was rolled back" was a false claim whenever the resume
    # failed; the operator has to be told which of the two happened.
    message = str(failure.value)
    assert "could NOT be rolled back" in message
    assert "the source was rolled back" not in message


def test_claude_memory_dir_rides_along_when_present(tmp_path: Path) -> None:
    session = _seed_claude_session(tmp_path)
    memory = session.parent / "memory"
    memory.mkdir()
    (memory / "MEMORY.md").write_text("remembered", encoding="utf-8")
    local, remote = FakeLocal(), FakeRemote()

    result = _run(tmp_path, local, remote)

    assert result["ok"] is True
    # worktree sync + memory sync (target dir is the session dir's sibling)
    assert (Path("/work/proj"), "/work/proj") in remote.syncs
    assert (memory, "/remote/sessions/memory") in remote.syncs


def test_claude_without_memory_dir_ships_only_the_transcript(
    tmp_path: Path,
) -> None:
    _seed_claude_session(tmp_path)
    local, remote = FakeLocal(), FakeRemote()

    result = _run(tmp_path, local, remote)

    assert result["ok"] is True
    assert remote.syncs == [(Path("/work/proj"), "/work/proj")]


def test_pi_transfer_uploads_a_header_adapted_copy(tmp_path: Path) -> None:
    # The pi session file lives under the source cwd's encoded dir.
    session_dir = tmp_path / ".pi" / "agent" / "sessions" / "--work-proj--"
    session_dir.mkdir(parents=True)
    source = session_dir / "2026-08-20T04-50-37-658Z_session-1.jsonl"
    source.write_text(
        json.dumps({"type": "session", "id": "session-1", "cwd": "/work/proj"})
        + "\n{\"entry\":1}\n",
        encoding="utf-8",
    )
    local, remote = FakeLocal(), FakeRemote()
    local.spec["provider"] = "pi"
    local.plan["spec"] = local.spec
    local.quiesce["spec"] = local.spec

    result = _run(tmp_path, local, remote, target_cwd="/srv/work")

    assert result["ok"] is True
    assert len(remote.uploads) == 1
    uploaded, _remote_path = remote.uploads[0]
    assert not uploaded.exists()  # temp copy removed after upload
    # The uploaded bytes carried the TARGET cwd in the header (pi's
    # stored-cwd existence check) while the entry lines stayed verbatim.
    uploaded_lines = remote.uploaded_content.decode("utf-8").splitlines()
    assert json.loads(uploaded_lines[0])["cwd"] == "/srv/work"
    assert uploaded_lines[1] == '{"entry":1}'
    # The source file kept the original cwd (rollback safety).
    assert json.loads(source.read_text(encoding="utf-8").splitlines()[0])[
        "cwd"
    ] == "/work/proj"


def test_complete_failure_is_loud_but_never_rolls_back(tmp_path: Path) -> None:
    _seed_claude_session(tmp_path)
    local, remote = FakeLocal(), FakeRemote()
    local.fail_on = "transfer.complete"
    messages: list[str] = []

    result = _run(tmp_path, local, remote, emit=messages.append)

    assert result["ok"] is True
    assert result["source"]["ok"] is False
    assert any("source cleanup failed" in message for message in messages)
    # No rollback: the worker is live on the target.
    assert all(method != "transfer.resume" for method, _ in local.calls)


# ---------------------------------------------------------------------------
# Transport failure (card 356 item 4: "断网" -- the link dies mid-transfer)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exit_code", "detail"),
    [
        (255, "kex_exchange_identification: Connection closed by remote host"),
        (None, "ssh: connect to host server port 22: Network is unreachable"),
    ],
)
def test_a_transport_failure_after_quiesce_rolls_back_and_can_be_retried(
    tmp_path: Path, exit_code: int | None, detail: str
) -> None:
    """The link dies during ship: the source comes back, and a retry works.

    ``exit_code`` is the transport's own status (ssh returns 255 for its own
    failures); it is ``None`` when no process ever ran (spawn error, DNS
    failure).  Both have to roll the source back, and the operator has to be
    able to simply run it again once the network is back.
    """

    _seed_claude_session(tmp_path)
    local, remote = FakeLocal(), FakeRemote()
    original = remote.run_json
    failures = {"left": 1}

    def flaky(argv: list[str], stdin: bytes | None = None) -> dict:
        if argv[:3] == ["agent", "transfer", "session-path"] and failures["left"] > 0:
            failures["left"] -= 1
            raise RemoteError(remote.host, tuple(argv), detail, exit_code=exit_code)
        return original(argv, stdin)

    remote.run_json = flaky  # type: ignore[method-assign]

    with pytest.raises(TransferError) as failure:
        _run(tmp_path, local, remote)

    assert failure.value.code == "TRANSFER_REMOTE_FAILED"
    assert [method for method, _ in local.calls] == [
        "transfer.plan",
        "transfer.quiesce",
        "transfer.resume",
    ]
    assert failure.value.data["rollback"]["ok"] is True  # type: ignore[index]

    # The exit code is only reported when a process actually produced one.
    if exit_code is None:
        assert "exit " not in str(failure.value)
    else:
        assert f"(exit {exit_code})" in str(failure.value)
    assert detail in str(failure.value)

    # Network is back: the same source transfer can simply be run again.
    result = _run(tmp_path, local, remote)

    assert result["ok"] is True
    assert result["resumeVerified"] is True
    assert [method for method, _ in local.calls[-3:]] == [
        "transfer.plan",
        "transfer.quiesce",
        "transfer.complete",
    ]
