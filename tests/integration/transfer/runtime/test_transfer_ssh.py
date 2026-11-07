"""SshRunner argv construction: transport flags must run on REAL machines.

The docker pseudo-machine verification caught macOS openrsync 2.6.9 rejecting
``--protect-args``; these tests pin the argv shapes so the transport stays
compatible with the oldest rsync/ssh a laptop plausibly ships.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hyprial.daemon.impl.transfer.execution.ssh import RemoteError, SshRunner


class _Recorder:
    def __init__(self, returncode: int = 0, stdout: bytes = b"") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.argv: list[list[str]] = []
        self.timeouts: list[float | None] = []

    def __call__(self, argv, **kwargs):  # noqa: ANN001, ANN202
        self.argv.append(list(argv))
        self.timeouts.append(kwargs.get("timeout"))
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, b"")


def _runner(monkeypatch: pytest.MonkeyPatch, recorder: _Recorder) -> SshRunner:
    runner = SshRunner("example-host")
    monkeypatch.setattr(subprocess, "run", recorder)
    return runner


def test_sync_tree_avoids_protect_args_for_openrsync_compat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Recorder()
    runner = _runner(monkeypatch, recorder)

    runner.sync_tree(Path("/local/proj"), "/remote/proj")

    argv = recorder.argv[0]
    assert argv[0] == "rsync"
    assert "--protect-args" not in argv
    assert argv[-2] == "/local/proj/"
    assert argv[-1] == "example-host:/remote/proj"


def test_make_dirs_creates_the_missing_parent_rsync_will_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A1: GNU rsync fails on a missing parent, openrsync silently creates it.

    The staging directory above the destination is therefore created by an
    explicit remote ``mkdir -p`` -- the transport supplies the one filesystem
    fact rsync does not, and the path stays quoted for both platforms.
    """

    recorder = _Recorder()
    runner = _runner(monkeypatch, recorder)

    runner.make_dirs("/remote/state/transfer-incoming")

    assert recorder.argv[0] == [
        "ssh",
        "example-host",
        "mkdir",
        "-p",
        "/remote/state/transfer-incoming",
    ]


def test_make_dirs_quotes_a_path_with_spaces(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder()
    runner = _runner(monkeypatch, recorder)

    runner.make_dirs("/remote/my state/transfer-incoming")

    assert recorder.argv[0][-1] == "'/remote/my state/transfer-incoming'"


def test_make_dirs_reports_a_remote_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder(returncode=1)
    runner = _runner(monkeypatch, recorder)

    with pytest.raises(RemoteError) as error:
        runner.make_dirs("/remote/state/transfer-incoming")

    assert error.value.exit_code == 1
    assert error.value.argv[:3] == ("ssh", "example-host", "mkdir")


def test_remove_tree_is_recursive_and_quoted(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder()
    runner = _runner(monkeypatch, recorder)

    runner.remove_tree("/remote/state/transfer-incoming/transfer-p2:abc")

    assert recorder.argv[0] == [
        "ssh",
        "example-host",
        "rm",
        "-rf",
        "/remote/state/transfer-incoming/transfer-p2:abc",
    ]


def test_remove_tree_reports_a_remote_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder(returncode=1)
    runner = _runner(monkeypatch, recorder)

    with pytest.raises(RemoteError) as error:
        runner.remove_tree("/remote/state/transfer-incoming/transfer-p2:abc")

    assert error.value.exit_code == 1
    assert error.value.argv[:3] == ("ssh", "example-host", "rm")


def test_sync_tree_quotes_remote_paths_with_spaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Recorder()
    runner = _runner(monkeypatch, recorder)

    runner.sync_tree(Path("/local/proj"), "/remote/my proj")

    assert recorder.argv[0][-1] == "example-host:'/remote/my proj'"


def test_sync_tree_failure_raises_remote_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Recorder(returncode=1)
    runner = _runner(monkeypatch, recorder)

    with pytest.raises(RemoteError):
        runner.sync_tree(Path("/local/proj"), "/remote/proj")


def test_run_json_appends_json_flag_and_parses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Recorder(stdout=b'{"ok": true, "value": 1}')
    runner = _runner(monkeypatch, recorder)

    result = runner.run_json(["daemon", "status"])

    assert result == {"ok": True, "value": 1}
    assert recorder.argv[0][:2] == ["ssh", "example-host"]
    assert recorder.argv[0][-1] == "--json"


@pytest.mark.parametrize("host", ["node-hq-01", "bobadmin@node-hq-01"])
@pytest.mark.parametrize("operation", ["status", "upload", "sync_tree"])
def test_ssh_failure_explains_implicit_username(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, host: str, operation: str
) -> None:
    # Exact stderr from readings §2.1, where omitting bobadmin@ failed.
    detail = b"Connection closed by 100.64.30.3 port 22\n"

    def fake_ssh(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 255, b"", detail)

    local_file = tmp_path / "session"
    local_file.write_bytes(b"synthetic session")
    monkeypatch.setattr(subprocess, "run", fake_ssh)
    runner = SshRunner(host)
    with pytest.raises(RemoteError) as exc:
        if operation == "status":
            runner.run_json(["daemon", "status"])
        elif operation == "upload":
            runner.upload(local_file, "/remote/session")
        else:
            runner.sync_tree(Path("/local/project"), "/remote/project")
    message = str(exc.value)
    assert detail.decode().strip() in message
    assert exc.value.host == host
    if "@" not in host:
        assert "local login name" in message
        assert "--to user@node-hq-01" in message
        assert "SSH User" in message
    else:
        assert "--to user@" not in message


def test_run_json_rejects_not_ok_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder(stdout=b'{"ok": false, "code": "X", "error": "nope"}')
    runner = _runner(monkeypatch, recorder)

    with pytest.raises(RemoteError, match="nope"):
        runner.run_json(["daemon", "status"])


@pytest.mark.parametrize(
    ("path", "installed", "expected", "probes"),
    [
        ("/usr/bin:/bin", {"/usr/bin/docker"}, True, 1),
        # cards.md / pr417-readings-10563.md §3.1: non-login PATH
        # omits /usr/local/bin, although docker is installed there.
        ("/opt/homebrew/bin:/usr/bin:/bin", {"/usr/local/bin/docker"}, True, 2),
        ("/usr/bin:/bin", {"/opt/homebrew/bin/docker"}, True, 3),
        ("/usr/bin:/bin", set(), False, 3),
    ],
)
def test_docker_probe_checks_non_login_path_then_known_locations(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    installed: set[str],
    expected: bool,
    probes: int,
) -> None:
    calls = []

    def fake_ssh(argv, **kwargs):
        # Actual ssh command argv and POSIX command -v / test exit statuses,
        # not an invented hyprial JSON response. No real remote is contacted.
        calls.append(argv)
        assert argv[:2] == ["ssh", "bobadmin@node-hq-01"]
        command = argv[2:]
        if command == ["command", "-v", "docker"]:
            found = next(
                (f"{directory}/docker" for directory in path.split(":")
                 if f"{directory}/docker" in installed),
                None,
            )
            return subprocess.CompletedProcess(
                argv, 0 if found else 1, f"{found}\n".encode() if found else b"", b""
            )
        assert command in [
            ["test", "-x", "/usr/local/bin/docker"],
            ["test", "-x", "/opt/homebrew/bin/docker"],
        ]
        return subprocess.CompletedProcess(argv, 0 if command[-1] in installed else 1, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_ssh)
    runner = SshRunner(
        "bobadmin@node-hq-01",
        remote_hyprial="HYPRIAL_HOME=/tmp/rehearsal /tmp/rehearsal/bin/hyprial",
    )
    assert runner.has_binary("docker") is expected
    assert len(calls) == probes
    assert all("/tmp/rehearsal/bin/hyprial" not in " ".join(argv) for argv in calls)


@pytest.mark.parametrize("binary", ["uv", "claude", "pi"])
def test_toolchain_probe_stays_on_non_interactive_path(
    monkeypatch: pytest.MonkeyPatch, binary: str
) -> None:
    recorder = _Recorder(returncode=1)
    runner = _runner(monkeypatch, recorder)
    assert not runner.has_binary(binary)
    assert recorder.argv == [["ssh", "example-host", "command", "-v", binary]]


@pytest.mark.parametrize("size", [0, 50_000_000, 544_000_000, 544 * 1024 * 1024, 2_300_000_000])
def test_upload_timeout_scales_with_actual_file_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    local = tmp_path / "image.tar"
    # Real stat shape, sparse file: no GB allocation or actual upload.
    with local.open("wb") as stream:
        stream.truncate(size)
    recorder = _Recorder(stdout=b'{"ok": true}')
    runner = _runner(monkeypatch, recorder)
    runner.upload(local, "/staging/image.tar")
    runner.run_json(["daemon", "status"])
    runner.run_json(["agent", "transfer", "receive"], stdin=b"{}")
    assert recorder.timeouts == [60.0 + size / (1024 * 1024), 30.0, 300.0]
    if size in {544_000_000, 544 * 1024 * 1024}:
        # Corrected tester report: tar 544MB, 40.4s (~1.35 x 30s), NOT
        # a measured 2.3GB/142s upload. Cover either MB convention; retain
        # 2.3GB above solely as a synthetic larger-file regression.
        assert recorder.timeouts[0] > 10 * 40.4


def test_upload_keeps_a_longer_configured_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local = tmp_path / "session"
    local.write_bytes(b"{}")
    recorder = _Recorder()
    monkeypatch.setattr(subprocess, "run", recorder)
    SshRunner("example-host", timeout=600).upload(local, "/remote/session")
    assert recorder.timeouts == [600]


@pytest.mark.parametrize("docker", ["/usr/bin/docker", "/usr/local/bin/docker", "/opt/homebrew/bin/docker"])
@pytest.mark.parametrize("present", [False, True])
def test_image_probe_uses_resolved_docker_and_exact_tag(
    monkeypatch: pytest.MonkeyPatch, docker: str, present: bool
) -> None:
    calls = []
    image = "hyprial-worker:0.4.4"

    def fake_ssh(argv, **kwargs):
        calls.append(argv)
        assert argv[:2] == ["ssh", "user@server"]
        assert kwargs["timeout"] == 30
        command = argv[2:]
        if command == ["command", "-v", "docker"]:
            found = docker == "/usr/bin/docker"
            return subprocess.CompletedProcess(argv, 0 if found else 1, docker.encode() if found else b"", b"")
        if command[:2] == ["test", "-x"]:
            return subprocess.CompletedProcess(argv, 0 if command[2] == docker else 1, b"", b"")
        assert command == [docker, "image", "inspect", "--", image]
        return subprocess.CompletedProcess(argv, 0 if present else 1, b"[]" if present else b"", b"" if present else b"No such image")

    monkeypatch.setattr(subprocess, "run", fake_ssh)
    runner = SshRunner("user@server", remote_hyprial="HYPRIAL_HOME=/tmp/rehearsal /tmp/bin/hyprial")
    assert runner.image_present(image) is present
    assert calls[-1][2:] == [docker, "image", "inspect", "--", image]
    assert not any("rehearsal" in " ".join(call) for call in calls)


def test_image_probe_without_docker_never_claims_a_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder(returncode=1)
    runner = _runner(monkeypatch, recorder)
    assert not runner.image_present("hyprial-worker:0.4.4")
    assert len(recorder.argv) == 3
    assert not any("inspect" in call for call in recorder.argv)


def test_upload_timeout_remains_a_remote_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local = tmp_path / "image.tar"
    local.write_bytes(b"tar")

    def timeout(argv, **kwargs):
        assert kwargs["timeout"] > 30
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(RemoteError, match="timed out"):
        SshRunner("user@server").upload(local, "/staging/image.tar")


@pytest.mark.parametrize("user", ["amyws", "bobadmin"])
def test_login_user_is_reported_by_ssh_id(monkeypatch: pytest.MonkeyPatch, user: str) -> None:
    recorder = _Recorder(stdout=f"{user}\n".encode())
    runner = _runner(monkeypatch, recorder)
    assert runner.login_user() == user
    assert recorder.argv == [["ssh", "example-host", "id", "-un"]]
    assert recorder.timeouts == [30.0]


@pytest.mark.parametrize(("rc", "stdout"), [(255, b""), (0, b""), (0, b"banner\nuser\n")])
def test_login_user_unknown_is_not_guessed(monkeypatch: pytest.MonkeyPatch, rc: int, stdout: bytes) -> None:
    recorder = _Recorder(returncode=rc, stdout=stdout)
    runner = _runner(monkeypatch, recorder)
    with pytest.raises(RemoteError, match="SSH login user"):
        runner.login_user()


def test_custom_remote_hyprial_prefix_replaces_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Real targets run a production hyprial on PATH; the build under test is
    # addressed by absolute path plus an env prefix.
    recorder = _Recorder(stdout=b'{"ok": true}')
    runner = SshRunner(
        "example-host",
        remote_hyprial="HYPRIAL_HOME=/tmp/hyprial-tgt /home/me/p0/.venv/bin/hyprial",
    )
    monkeypatch.setattr(subprocess, "run", recorder)
    assert runner.remote_hyprial == "HYPRIAL_HOME=/tmp/hyprial-tgt /home/me/p0/.venv/bin/hyprial"

    runner.run_json(["agent", "transfer", "precheck", "--harness", "pi", "--name", "wk"])

    argv = recorder.argv[0]
    assert argv[:7] == [
        "ssh",
        "example-host",
        "HYPRIAL_HOME=/tmp/hyprial-tgt",
        "/home/me/p0/.venv/bin/hyprial",
        "agent",
        "transfer",
        "precheck",
    ]


def test_remote_failure_keeps_the_exit_code_and_the_raw_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AT07 item 4: a failed remote call must keep exit status AND raw output."""
    detail = b"rsync: connection unexpectedly closed (0 bytes received so far)\n"

    def fake(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 255, b"", detail)

    monkeypatch.setattr(subprocess, "run", fake)
    runner = SshRunner("example-host")
    local_file = tmp_path / "session"
    local_file.write_bytes(b"synthetic")

    for operation in ("status", "upload", "sync_tree"):
        with pytest.raises(RemoteError) as exc:
            if operation == "status":
                runner.run_json(["daemon", "status"])
            elif operation == "upload":
                runner.upload(local_file, "/remote/session")
            else:
                runner.sync_tree(Path("/local/project"), "/remote/project")
        assert exc.value.exit_code == 255, operation
        assert exc.value.detail == detail.decode().strip(), operation
        assert "(exit 255)" in str(exc.value), operation
        assert detail.decode().strip() in str(exc.value), operation


def test_a_spawn_failure_has_no_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """OSError/timeout means no process status: exit_code stays None, not a lie."""

    def boom(argv, **kwargs):
        raise FileNotFoundError("ssh not found")

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(RemoteError) as exc:
        SshRunner("example-host").run_json(["daemon", "status"])
    assert exc.value.exit_code is None
    assert "(exit" not in str(exc.value)
    assert "ssh not found" in exc.value.detail
