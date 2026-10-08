"""Isolated smolvm mapping probe; this does not start or migrate an agent.

Only an explicit local runtime and rootfs are used. Evidence distinguishes this
synthetic shell workload from worker, authentication and sandbox acceptance.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import pwd
import re
import shutil
import signal
import stat
import subprocess
import threading
from pathlib import Path
from typing import Any

_SYSTEM_E2FS_DIRS = (
    "/opt/homebrew/opt/e2fsprogs/sbin",
    "/usr/local/opt/e2fsprogs/sbin",
    "/opt/homebrew/sbin",
    "/usr/local/sbin",
    "/sbin",
    "/usr/sbin",
)


class SmolvmProbeError(RuntimeError):
    def __init__(self, code: str, message: str, data: dict[str, Any] | None = None):
        self.code = code
        self.data = data or {}
        super().__init__(message)


def _regular_read(path: Path, limit: int = 65536) -> bytes:
    """Open every component without following links, including guest parents."""
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = child
        file_fd = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
        )
        with os.fdopen(file_fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ValueError(f"Not a bounded regular file: {path.name}")
            return stream.read(limit + 1)
    finally:
        os.close(fd)


def _snapshot(path: Path) -> dict[str, Any]:
    """Hash files, never link targets. Metadata changes are also observable."""
    if not path.exists() and not path.is_symlink():
        return {"type": "absent"}
    info = path.lstat()
    result: dict[str, Any] = {"mode": info.st_mode, "uid": info.st_uid}
    if stat.S_ISLNK(info.st_mode):
        result.update(type="symlink", target=os.readlink(path))
    elif stat.S_ISDIR(info.st_mode):
        result.update(
            type="directory",
            children={p.name: _snapshot(p) for p in sorted(path.iterdir())},
        )
    elif stat.S_ISREG(info.st_mode):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            result.update(
                type="file",
                size=info.st_size,
                sha256=hashlib.file_digest(stream, "sha256").hexdigest(),
            )
    else:
        result.update(type="special")
    return result


def _host_canaries() -> dict[str, Any]:
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    paths = [
        home / ".smolvm",
        home / "Library/Application Support/smolvm",
        home / "Library/Caches/smolvm",
    ]
    temp_roots = {Path(os.environ.get("TMPDIR", "/tmp"))}
    if platform.system() == "Darwin":
        # getpwuid/confstr can bypass the synthetic environment passed to the
        # runtime. Observe the actual OS user temp root as well as TMPDIR.
        # CPython may omit this Darwin-specific name from confstr_names.
        # Darwin unistd.h defines _CS_DARWIN_USER_TEMP_DIR as 65537.
        actual_temp = os.confstr(os.confstr_names.get("CS_DARWIN_USER_TEMP_DIR", 65537))
        if actual_temp:
            temp_roots.add(Path(actual_temp))
    for root in temp_roots:
        paths.extend(root.glob("smolvm*"))
    return {str(p): _snapshot(p) for p in sorted(set(paths))}


def _private_environment(output: Path, *, reuse: bool = False) -> dict[str, str]:
    """Private tool environment; reuse requires the caller's ownership fence.

    The standalone probe remains fresh-only. A worker reclaiming its own crashed
    generation may reuse directories, but never links or another user's state.
    """
    engine = output / "engine"
    engine.mkdir(parents=True, mode=0o700, exist_ok=reuse)
    if reuse:
        info = engine.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or engine.resolve() != engine
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise ValueError("Private runtime engine ownership/type/mode mismatch")
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C", "LC_ALL": "C"}
    for key, name in (
        ("HOME", "home"),
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_RUNTIME_DIR", "runtime"),
        ("TMPDIR", "tmp"),
    ):
        path = output / "engine" / name
        path.mkdir(parents=True, mode=0o700, exist_ok=reuse)
        if reuse:
            info = path.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or path.resolve() != path
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700
            ):
                raise ValueError(
                    "Private runtime environment ownership/type/mode mismatch"
                )
        env[key] = str(path)
    return env


def _run(
    argv: list[str], output: Path, label: str, env: dict[str, str], timeout: float
) -> dict[str, Any]:
    result: dict[str, Any] = {"argv": argv, "timeout": False, "exit_code": None}
    with (
        (output / f"{label}.stdout").open("xb") as stdout,
        (output / f"{label}.stderr").open("xb") as stderr,
    ):
        process = subprocess.Popen(
            argv,
            cwd=output,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        try:
            result["exit_code"] = process.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
            result["timeout"] = isinstance(error, subprocess.TimeoutExpired)
            result["interrupted"] = isinstance(error, KeyboardInterrupt)
            # This process group was created by this call. Never kill by name.
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    pass
                try:
                    result["exit_code"] = process.wait(timeout=3)
                    break
                except subprocess.TimeoutExpired:
                    continue
            # The direct child may exit on TERM while its group descendants
            # ignore it. The group belongs exclusively to this invocation.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    (output / f"{label}.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def _succeeded(result: dict[str, Any]) -> bool:
    return (
        result["exit_code"] == 0
        and not result.get("timeout")
        and not result.get("interrupted")
    )


_GUEST = r"""set -eu
cd /hyprial/agent
test "$(cat config/config.txt)" = synthetic-config
test "$(cat state/session.txt)" = synthetic-session
test "$(cat workspace/input.txt)" = synthetic-workspace
test ! -e "$1"
test -z "${OPENAI_API_KEY-}${ANTHROPIC_API_KEY-}${SSH_AUTH_SOCK-}${HYPRIAL_HOME-}${HARNESS_STATE_DIR-}${HYPRIAL_PROBE_HOST_SECRET-}"
printf 'mapped-output\n' > workspace/output.txt
chmod 600 workspace/output.txt
ln -s "$1" workspace/host-link
uname -s > workspace/system.txt
uname -m > workspace/arch.txt
sha256sum config/config.txt state/session.txt workspace/input.txt
printf 'HYPRIAL_MAPPING_OK\n'
"""


def _validate_paths(
    binary: Path, rootfs: Path, output: Path, resize2fs: Path
) -> tuple[Path, Path, Path, Path]:
    for path in (binary, rootfs, output, resize2fs):
        if not path.is_absolute() or any(
            c in str(path) for c in (":", "\n", "\r", "\x00")
        ):
            raise SmolvmProbeError(
                "PROBE_INVALID_PATH",
                "Use absolute paths without mount delimiters or control characters",
            )
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise SmolvmProbeError(
            "PROBE_INVALID_RUNTIME", "smolvm must be an executable local file"
        )
    if not resize2fs.is_file() or not os.access(resize2fs, os.X_OK):
        raise SmolvmProbeError(
            "PROBE_INVALID_RUNTIME",
            "The 1 GiB disk requires an explicit executable resize2fs",
        )
    # 1.19.0 checks these locations before PATH. Refuse a shadowed explicit
    # tool rather than reporting the supplied tool's hash for a different one.
    for directory in _SYSTEM_E2FS_DIRS:
        candidate = Path(directory) / "resize2fs"
        if candidate.exists():
            if candidate.resolve() != resize2fs.resolve():
                raise SmolvmProbeError(
                    "PROBE_SHADOWED_TOOL",
                    f"smolvm would use {candidate}; explicitly select that tool",
                )
            break
    # Absolute links in a guest image are relative to the guest root, not the
    # host (Alpine's /bin/sh -> /bin/busybox is valid on macOS too).
    shell = rootfs / "bin/sh"
    if not rootfs.is_dir() or not (shell.exists() or shell.is_symlink()):
        raise SmolvmProbeError("PROBE_INVALID_ROOTFS", "rootfs must contain bin/sh")
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise SmolvmProbeError(
            "PROBE_OUTPUT_EXISTS",
            "output must be new, with an existing parent directory",
        )
    binary, rootfs = binary.resolve(), rootfs.resolve()
    output = output.parent.resolve() / output.name
    for path in (binary, rootfs, output, resize2fs.resolve()):
        if any(c in str(path) for c in (":", "\n", "\r", "\x00")):
            raise SmolvmProbeError(
                "PROBE_INVALID_PATH",
                "Resolved paths must not contain mount delimiters or control characters",
            )
    seed = binary.parent / "agent-rootfs"
    if not seed.is_dir() or seed.is_symlink():
        raise SmolvmProbeError(
            "PROBE_INVALID_RUNTIME",
            "Use the complete official smolvm bundle with adjacent agent-rootfs",
        )
    cache = (
        "engine/home/Library/Caches"
        if platform.system() == "Darwin"
        else "engine/cache"
    )
    socket_path = output / cache / "smolvm/vms/0123456789abcdef/agent.sock"
    if len(os.fsencode(socket_path)) >= (104 if platform.system() == "Darwin" else 108):
        raise SmolvmProbeError(
            "PROBE_PATH_TOO_LONG",
            "Use a short output path (for example /tmp/at08-1) to fit runtime Unix sockets",
        )
    if output.is_relative_to(rootfs) or rootfs.is_relative_to(output):
        raise SmolvmProbeError(
            "PROBE_INVALID_PATH", "rootfs and output must be separate trees"
        )
    if output.is_relative_to(binary.parent):
        raise SmolvmProbeError(
            "PROBE_INVALID_PATH", "output must be outside the runtime bundle"
        )
    return binary, rootfs, output, resize2fs.resolve()


def _cleanup(binary: Path, output: Path, env: dict[str, str]) -> dict[str, Any]:
    """Enumerate only the exclusive engine HOME; retain evidence on failure."""
    commands = []
    listed = _run(
        [str(binary), "machine", "ls", "--quiet"], output, "cleanup-list", env, 10
    )
    commands.append(listed)
    if not _succeeded(listed):
        return {
            "ok": False,
            "commands": commands,
            "residuals": [str(output / "engine")],
        }
    names = _regular_read(output / "cleanup-list.stdout").decode().splitlines()
    for index, name in enumerate(names):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            return {
                "ok": False,
                "commands": commands,
                "residuals": ["unrecognized machine list"],
            }
        commands.append(
            _run(
                [str(binary), "machine", "delete", "--name", name, "--force"],
                output,
                f"cleanup-delete-{index}",
                env,
                15,
            )
        )
    verified = _run(
        [str(binary), "machine", "ls", "--quiet"], output, "cleanup-verify", env, 10
    )
    commands.append(verified)
    remaining = _regular_read(output / "cleanup-verify.stdout").decode().splitlines()
    # A missing registry row alone does not prove a detached monitor exited.
    # Never kill a PID from a file: it might be stale/reused. Retain the tree
    # and report the residual for inspection instead of deleting live disks.
    for path in (output / "engine").rglob("*.pid"):
        raw = _regular_read(path).decode().strip()
        if not raw:
            continue
        pid = int(raw.split()[0])
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        remaining.append(f"live pid {pid}: {path}")
    ok = all(_succeeded(c) for c in commands) and not remaining
    logs = output / "engine-logs"
    for index, path in enumerate((output / "engine").rglob("*.log")):
        logs.mkdir(exist_ok=True, mode=0o700)
        try:
            raw = _regular_read(path, 1024 * 1024)
            (logs / f"{index}-{path.name}").write_bytes(raw)
        except (OSError, ValueError):
            # Never follow a log link or ingest an unbounded special file.
            (logs / f"{index}-unread.txt").write_text(str(path.relative_to(output)))
    if ok:
        # Only our exclusively-created directory; never an ambient runtime HOME.
        shutil.rmtree(output / "engine")
    return {
        "ok": ok,
        "commands": commands,
        "residuals": remaining if ok else [str(output / "engine"), *remaining],
    }


class _DeferredInterrupt:
    """Hold SIGINT while evidence is written, then let the caller re-raise it.

    The probe has two long windows where a lost interrupt erases the record of
    what was allocated: the pre-run snapshots, and the post-run cleanup,
    snapshots and report write.  An owned stop arrives as one SIGINT, so
    deferring it for those windows keeps ``report.json`` truthful instead of
    aborting the write half way.  SIGTERM is never masked; it stays the escape
    hatch for a stuck run.
    """

    def __init__(self) -> None:
        self.pending: KeyboardInterrupt | None = None
        self._depth = 0
        self._previous: Any = None

    def __enter__(self) -> "_DeferredInterrupt":
        if threading.current_thread() is threading.main_thread():
            if self._depth == 0:
                self._previous = signal.getsignal(signal.SIGINT)
                signal.signal(signal.SIGINT, self._defer)
            self._depth += 1
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        if self._depth:
            self._depth -= 1
            if self._depth == 0 and self._previous is not None:
                signal.signal(signal.SIGINT, self._previous)
                self._previous = None
        return False

    def _defer(self, signum: int, frame: Any) -> None:
        if self.pending is None:
            self.pending = KeyboardInterrupt()

    def note(self, report: dict[str, Any]) -> bool:
        """Record a deferred interrupt in the report; True when one arrived."""
        if self.pending is None:
            return False
        report["interrupted"] = True
        report["interrupt_signal"] = "SIGINT"
        report["errors"].append(
            "Interrupted by SIGINT; the evidence for this run was written before exit"
        )
        return True


def _write_report(output: Path, report: dict[str, Any]) -> None:
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")


def probe_smolvm(
    *, binary: Path, rootfs: Path, output: Path, resize2fs: Path
) -> dict[str, Any]:
    binary, rootfs, output, resize2fs = _validate_paths(
        binary, rootfs, output, resize2fs
    )
    output.mkdir(mode=0o700)  # Exclusive, even if another caller wins the race.
    report: dict[str, Any] = {
        "ok": False,
        "kind": "synthetic-smolvm-mapping",
        "schema_version": 1,
        "output": str(output),
        "phase": "setup",
        "interrupted": False,
        "interrupt_signal": None,
        "host": {"system": platform.system(), "arch": platform.machine()},
        "checks": {
            "mapping": "NOT_RUN",
            "worker": "NOT_RUN",
            "sandbox": "NOT_RUN",
            "authentication": "NOT_RUN",
            "cross_machine": "NOT_RUN",
        },
        "limitations": "Synthetic mapping only; no agent, model credential, harness sandbox or cross-machine transfer tested.",
        "errors": [],
    }
    # Persist a truthful record before anything long or allocating happens, so
    # an interrupt from here on leaves a reviewable artifact instead of none.
    _write_report(output, report)
    deferred = _DeferredInterrupt()
    with deferred:
        report["binary"] = {"path": str(binary), "snapshot": _snapshot(binary)}
        report["resize2fs"] = {
            "path": str(resize2fs),
            "snapshot": _snapshot(resize2fs),
        }
        before_rootfs, before_host = _snapshot(rootfs), _host_canaries()
        before_runtime = _snapshot(binary.parent)
        (output / "rootfs-before.json").write_text(
            json.dumps(before_rootfs, sort_keys=True) + "\n"
        )
        (output / "host-before.json").write_text(
            json.dumps(before_host, sort_keys=True) + "\n"
        )
    if deferred.note(report):
        report["ok"] = False
        _write_report(output, report)
        raise deferred.pending
    env = _private_environment(output)
    try:
        report["phase"] = "guest"
        # smolvm writes readiness markers into its agent seed, even when the
        # workload image is read-only. Never share this mutable seed with a
        # reviewer or with another installed runtime.
        seed = output / "engine/agent-rootfs"
        shutil.copytree(binary.parent / "agent-rootfs", seed, symlinks=True)
        env["SMOLVM_AGENT_ROOTFS"] = str(seed)
        tools = output / "engine/bin"
        tools.mkdir(mode=0o700)
        (tools / "resize2fs").symlink_to(resize2fs)
        env["PATH"] = str(tools) + ":" + env["PATH"]
        (output / "environment.json").write_text(json.dumps(env, indent=2) + "\n")
        (output / "runtime-before.json").write_text(
            json.dumps(before_runtime, sort_keys=True) + "\n"
        )
        version = _run([str(binary), "--version"], output, "version", env, 10)
        report["version"] = _regular_read(output / "version.stdout").decode().strip()
        if not _succeeded(version) or report["version"] != "smolvm 1.19.0":
            raise ValueError("This probe requires smolvm 1.19.0")
        fixture = output / "fixture"
        for folder, file, value in (
            ("config", "config.txt", "synthetic-config\n"),
            ("state", "session.txt", "synthetic-session\n"),
            ("workspace", "input.txt", "synthetic-workspace\n"),
        ):
            directory = fixture / folder
            directory.mkdir(parents=True, mode=0o700)
            (directory / file).write_text(value)
            (directory / file).chmod(0o600)
        canary = output / "unmounted-host-canary"
        canary.write_text("synthetic-host-only-canary\n")
        canary.chmod(0o600)
        argv = [
            str(binary),
            "machine",
            "run",
            "--image",
            str(rootfs),
            "--cpus",
            "2",
            "--mem",
            "1024",
            "--storage",
            "1",
            "--overlay",
            "1",
            "--timeout",
            "15s",
            "--volume",
            f"{fixture}:/hyprial/agent:rw",
            "--",
            "/bin/sh",
            "-c",
            _GUEST,
            "hyprial-probe",
            str(canary),
        ]
        execution = _run(argv, output, "guest", env, 120)
        report["execution"] = execution
        if not _succeeded(execution):
            # A SIGINT that lands while the guest runs surfaces here as an
            # interrupted execution, not as a raised KeyboardInterrupt, so record
            # it before turning the failure into a value error.  Review
            # 2026-09-29 (cc-sw-review): the mapping already became FAIL for this
            # case while ``interrupted`` stayed False; keep the two consistent.
            if execution.get("interrupted"):
                report["interrupted"] = True
                report["interrupt_signal"] = "SIGINT"
            raise ValueError("Guest process failed, timed out or was interrupted")
        if b"HYPRIAL_MAPPING_OK\n" not in _regular_read(output / "guest.stdout"):
            raise ValueError("Guest completion marker missing")
        workspace = fixture / "workspace"
        if _regular_read(workspace / "output.txt") != b"mapped-output\n":
            raise ValueError("Guest writeback missing or incorrect")
        if _regular_read(workspace / "system.txt").strip() != b"Linux":
            raise ValueError("Guest is not Linux")
        report["guest"] = {
            "system": "Linux",
            "arch": _regular_read(workspace / "arch.txt").decode().strip(),
        }
        link = workspace / "host-link"
        if not link.is_symlink():
            raise ValueError("Guest symlink negative control missing")
        try:
            _regular_read(link)
        except OSError:
            report["guest_symlink_read"] = "REJECTED"
        else:
            raise ValueError("Guest symlink was followed")
        report["host_writeback"] = _snapshot(workspace / "output.txt")
        report["checks"]["mapping"] = "PASS"
    except (OSError, ValueError, KeyboardInterrupt) as error:
        report["checks"]["mapping"] = "FAIL"
        report["errors"].append(str(error) or type(error).__name__)
    finally:
        report["phase"] = "cleanup"
        # An interrupt that lands in cleanup, in the post-run snapshots or in
        # the report write must not erase what was allocated: defer it for the
        # whole window, then record it and exit non-zero.
        with deferred:
            try:
                report["cleanup"] = _cleanup(binary, output, env)
            except (OSError, ValueError) as error:
                report["cleanup"] = {
                    "ok": False,
                    "error": str(error),
                    "residuals": [str(output / "engine")],
                }
            if not report["cleanup"]["ok"]:
                report["errors"].append("Private runtime cleanup failed; see residuals")
            after_rootfs, after_host = _snapshot(rootfs), _host_canaries()
            after_runtime = _snapshot(binary.parent)
            (output / "runtime-after.json").write_text(
                json.dumps(after_runtime, sort_keys=True) + "\n"
            )
            (output / "rootfs-after.json").write_text(
                json.dumps(after_rootfs, sort_keys=True) + "\n"
            )
            (output / "host-after.json").write_text(
                json.dumps(after_host, sort_keys=True) + "\n"
            )
            for name, unchanged in (
                ("source_rootfs", before_rootfs == after_rootfs),
                ("host_canaries", before_host == after_host),
                ("runtime_bundle", before_runtime == after_runtime),
            ):
                report["checks"][name] = "PASS" if unchanged else "FAIL"
                if not unchanged:
                    report["errors"].append(f"{name} changed")
            deferred.note(report)
            report["ok"] = not report["errors"]
            _write_report(output, report)
    if deferred.pending is not None:
        raise deferred.pending
    if not report["ok"]:
        raise SmolvmProbeError(
            "SMOLVM_PROBE_FAILED",
            "smolvm mapping probe failed; inspect report.json",
            report,
        )
    return report
