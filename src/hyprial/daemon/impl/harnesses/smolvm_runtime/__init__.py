"""Owned smolvm carrier resources. Host authority is never projected into guest state."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid
import warnings
from dataclasses import replace
from pathlib import Path

from hyprial.kernel import SmolvmRuntimeSpec
from hyprial.kernel import GUEST_WORKER_STATE, WorkerBinding
from hyprial.daemon.impl.transfer.execution.smolvm import (
    _host_canaries,
    _private_environment,
    _regular_read,
    _snapshot,
)
from hyprial.daemon.impl.harnesses.worker_channel  import WorkerChannel
from hyprial.daemon.impl.harnesses.worker_relay  import BoundWorkerRelay


class SmolvmRuntimeError(ValueError):
    code = "SMOLVM_RUNTIME_INVALID"


_SANDBOX_SMOKE = r"""
set -eu
export CODEX_HOME=/run/hyprial-native-sandbox-check/codex
export PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
mkdir -p "$CODEX_HOME" /run/hyprial-native-sandbox-check
sentinel=/run/hyprial-native-sandbox-check/sentinel
printf before > "$sentinel"
before=$(sha256sum "$sentinel")
identity=$(stat -c '%d:%i:%u:%g:%a' "$sentinel")
"$1" sandbox -c 'sandbox_mode="read-only"' -- /bin/sh -c '
  test -r /run/hyprial-native-sandbox-check/sentinel
  if printf changed > /run/hyprial-native-sandbox-check/sentinel; then exit 91; fi
'
test "$(sha256sum "$sentinel")" = "$before"
test "$(stat -c '%d:%i:%u:%g:%a' "$sentinel")" = "$identity"
printf after > "$sentinel"
test "$(cat "$sentinel")" = after
rm -rf /run/hyprial-native-sandbox-check "$CODEX_HOME"
printf 'native-read-only-sentinel: PASS\n'
"""


def tree_digest(path: Path) -> str:
    """snapshot-json-v1: sorted compact UTF-8 JSON including mode/uid, no link targets read."""
    return hashlib.sha256(
        json.dumps(
            _snapshot(path), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
    ).hexdigest()


def validate_materials(spec: SmolvmRuntimeSpec) -> None:
    # Parsing persisted input is repeated too: no trusted construction shortcut.
    SmolvmRuntimeSpec.from_json(spec.to_json())
    for raw, expected in (
        (spec.smolvm_path, spec.smolvm_sha256),
        (spec.resize2fs_path, spec.resize2fs_sha256),
    ):
        path = Path(raw)
        if (
            not path.is_absolute()
            or path.resolve(strict=True) != path
            or not os.access(path, os.X_OK)
        ):
            raise SmolvmRuntimeError(
                "Runtime executables must be canonical executable files"
            )
        if (
            hashlib.sha256(_regular_read(path, 512 * 1024 * 1024)).hexdigest()
            != expected
        ):
            raise SmolvmRuntimeError("Runtime executable digest mismatch")
    if tree_digest(Path(spec.smolvm_path).parent) != spec.smolvm_bundle_digest:
        raise SmolvmRuntimeError("Runtime bundle snapshot-json-v1 digest mismatch")
    root = Path(spec.rootfs_path)
    if (
        root.resolve(strict=True) != root
        or not root.is_dir()
        or tree_digest(root) != spec.rootfs_digest
    ):
        raise SmolvmRuntimeError("Rootfs snapshot-json-v1 digest mismatch")
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise SmolvmRuntimeError(
            "smolvm worker execution is NOT_IMPLEMENTED on this host"
        )


def _directory(path: Path) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or path.resolve() != path
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise SmolvmRuntimeError("Private runtime directory ownership/mode mismatch")


def runtime_parent() -> Path:
    base = Path("/private/tmp") if platform.system() == "Darwin" else Path("/tmp")
    return base / f"hv{os.getuid()}"


def runtime_directory(state_dir: Path, actor: str) -> Path:
    # Short enough for Darwin's 104-byte socket limit; marker detects hash collisions.
    digest = hashlib.sha256(f"{state_dir.resolve()}\n{actor}".encode()).hexdigest()[:8]
    return runtime_parent() / digest


# A successful close removes the engine and owner.json but keeps the private
# directory and its runtime.lock. Those husks are unreachable through the public
# entry points, so they only accumulate (measured on jjkysy-dev 2026-09-29: 37
# lock-only directories under /private/tmp/hv501, newest lock written
# 16:24:22Z). A minimum age keeps the reaper away from a generation that is
# starting or still writing evidence right now.
_LOCK_HUSK_MIN_AGE_SECONDS = 300.0


def reap_lock_husks(
    parent: Path | None = None,
    *,
    keep: Path | None = None,
    minimum_age_seconds: float = _LOCK_HUSK_MIN_AGE_SECONDS,
    now: float | None = None,
) -> list[Path]:
    """Delete this uid's lock-only runtime husks; never raise, never guess.

    A directory is only removed when all of these hold: it is a direct child of
    this uid's runtime parent, owned by this uid with mode 0700, holds nothing
    but `runtime.lock` (an empty leftover from an interrupted reap is also
    accepted), is older than `minimum_age_seconds`, is not `keep` (the runtime
    this call is about to create), and its lock can be taken with
    ``flock(LOCK_EX|LOCK_NB)`` - so no live generation still owns it. Anything
    else is left untouched. Returns the removed directories.
    """
    parent = runtime_parent() if parent is None else Path(parent)
    reference = time.time() if now is None else now
    removed: list[Path] = []
    try:
        candidates = sorted(parent.iterdir())
    except OSError:
        return removed
    for candidate in candidates:
        if keep is not None and candidate == keep:
            continue
        try:
            info = candidate.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                continue
            if stat.S_IMODE(info.st_mode) != 0o700:
                continue
            entries = sorted(candidate.iterdir())
            if [entry.name for entry in entries] not in ([], ["runtime.lock"]):
                continue
            lock = candidate / "runtime.lock"
            newest = info.st_mtime
            if entries:
                lock_info = lock.lstat()
                if not stat.S_ISREG(lock_info.st_mode):
                    continue
                if lock_info.st_uid != os.getuid():
                    continue
                newest = max(newest, lock_info.st_mtime)
            if minimum_age_seconds and reference - newest < minimum_age_seconds:
                continue
            if entries:
                fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW)
                try:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError:
                        # A live generation still owns this lock; leave it alone.
                        continue
                    lock.unlink()
                finally:
                    os.close(fd)
            candidate.rmdir()
        except OSError:
            continue
        except Exception:
            continue
        removed.append(candidate)
    return removed


def guest_channel(channel: WorkerChannel, spec: SmolvmRuntimeSpec) -> WorkerChannel:
    server = dict(channel.mcp_server)
    server["command"] = spec.guest_python
    server["env"] = {
        "HYPRIAL_HOME": GUEST_WORKER_STATE,
        "HARNESS_STATE_DIR": GUEST_WORKER_STATE,
    }
    return replace(
        channel,
        hyprial_home=Path(GUEST_WORKER_STATE),
        state_dir=Path(GUEST_WORKER_STATE),
        mcp_server=server,
    )


def mount_roots(channel: WorkerChannel, cwd: str | None) -> tuple[Path, ...]:
    context = channel.runtime_context
    if context is None or context.actor != channel.actor:
        raise SmolvmRuntimeError("smolvm requires the resolved P2 actor context")
    if context.shared_credential is not None:
        raise SmolvmRuntimeError(
            "Shared host login links are not portable; reauthenticate in agent home"
        )
    roots = context.roots
    candidates = (
        roots.agent_home,
        roots.config_source,
        Path(cwd) if cwd else roots.agent_home,
    )
    mounts: list[Path] = []
    for path in candidates:
        if not path.is_dir() or path.resolve() != path or ":" in str(path):
            raise SmolvmRuntimeError(
                "smolvm mounts require canonical explicit directories"
            )
        # A workspace must never turn into a mount of the host HOME/state or peers.
        forbidden = (
            Path.home().resolve(),
            channel.hyprial_home.resolve(),
            channel.state_dir.resolve(),
        )
        if path == Path("/") or any(
            p == path or p.is_relative_to(path) for p in forbidden
        ):
            raise SmolvmRuntimeError("Refusing host HOME/state parent mount")
        if path != roots.agent_home and roots.agent_home.is_relative_to(path):
            raise SmolvmRuntimeError(
                "Workspace/config cannot include other agent homes"
            )
        if any(path.is_relative_to(existing) for existing in mounts):
            continue
        # No host link is chased into a new mount. Absolute links outside these
        # explicit roots are rejected; no native shared login/host git-dir adoption.
        mounts.append(path)
    for path in mounts:
        for base, dirs, files in os.walk(path, followlinks=False):
            for name in (*dirs, *files):
                item = Path(base) / name
                if item.is_symlink() and not any(
                    item.resolve().is_relative_to(r) for r in mounts
                ):
                    raise SmolvmRuntimeError(
                        "Payload contains an external host symbolic link"
                    )
        git = path / ".git"
        if git.is_file() and not git.is_symlink():
            # A worktree gitdir pointer can escape an otherwise valid cwd.
            raise SmolvmRuntimeError(
                "External git worktree metadata must be materialized before VM launch"
            )
    return tuple(mounts)


class SmolvmWorkerRuntime:
    """One channel/epoch and one private VM; cleanup is also the restore fence."""

    def __init__(
        self,
        spec: SmolvmRuntimeSpec,
        channel: WorkerChannel,
        epoch: str,
        cwd: str | None,
    ):
        self.spec, self.channel, self.cwd = spec, channel, cwd
        self.binding = WorkerBinding(channel.actor, channel.session_ref, epoch)
        self.output = runtime_directory(channel.state_dir, channel.actor)
        self._lock = threading.RLock()
        self._lease = None
        self._relay: BoundWorkerRelay | None = None
        self._env: dict[str, str] | None = None
        self._before_host: dict | None = None
        self._closed = False
        self._generation = uuid.uuid4().hex
        self._owned = False
        self.cleanup_complete = True
        self.reaped_lock_husks: list[Path] = []
        self._active_record = channel.state_dir / "smolvm-runtimes" / self.output.name

    def _admin(self, *args: str, timeout: float = 20) -> bytes:
        assert self._env is not None
        # Only administrative commands; never credentials, model prompts or output.
        result = subprocess.run(
            [self.spec.smolvm_path, *args],
            env=self._env,
            cwd=self.output,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        if result.returncode:
            raise SmolvmRuntimeError(
                f"smolvm {args[0]} administrative operation failed ({result.returncode})"
            )
        return result.stdout

    def _clean_engine(self) -> None:
        if not (self.output / "engine").exists():
            return
        engine = self.output / "engine"
        # delete may erase PID files before a manager has actually exited. Keep
        # their identities across both that deletion and a failed cleanup retry.
        pending = engine / "cleanup-manager-pids.json"
        pids = set(json.loads(_regular_read(pending))) if pending.exists() else set()
        for path in engine.rglob("*.pid"):
            raw = _regular_read(path).decode().strip()
            if raw:
                pids.add(int(raw.split()[0]))
        if any(type(pid) is not int or pid <= 1 for pid in pids):
            raise SmolvmRuntimeError("Invalid private runtime manager PID")
        fd, temporary = tempfile.mkstemp(prefix=".cleanup-pids-", dir=engine)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(sorted(pids), stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, pending)
            directory = os.open(engine, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)
        names = self._admin("machine", "ls", "--quiet").decode().splitlines()
        if any(name != "worker" for name in names):
            raise SmolvmRuntimeError("Unowned VM in private runtime; refusing cleanup")
        for name in names:
            self._admin("machine", "delete", "--name", name, "--force")
        if self._admin("machine", "ls", "--quiet").strip():
            raise SmolvmRuntimeError("VM cleanup incomplete")
        for pid in pids:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            raise SmolvmRuntimeError(
                "Runtime manager still alive; retain its disk and refuse replacement"
            )
        shutil.rmtree(self.output / "engine")

    def start(
        self, command: tuple[str, ...], environment: dict[str, str]
    ) -> tuple[tuple[str, ...], dict[str, str]]:
        with self._lock:
            if self._closed or self._lease is not None:
                raise SmolvmRuntimeError("Runtime object cannot be reused")
            validate_materials(self.spec)
            mounts = mount_roots(self.channel, self.cwd)
            _directory(self.output.parent)
            # Opportunistic maintenance: older generations leave lock-only
            # husks behind, and only this uid's parent is scanned.
            self.reaped_lock_husks = reap_lock_husks(
                self.output.parent, keep=self.output
            )
            _directory(self.output)
            fd = os.open(
                self.output / "runtime.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
            self._lease = os.fdopen(fd, "w")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BaseException:
                self._lease.close()
                self._lease = None
                raise SmolvmRuntimeError("Another runtime owns this worker") from None
            marker = self.output / "owner.json"
            expected = dict(
                actor=self.channel.actor,
                stateDir=str(self.channel.state_dir.resolve()),
                spec=self.spec.to_json(),
            )
            try:
                if marker.exists():
                    previous = json.loads(_regular_read(marker))
                    previous.pop("generation", None)
                    if previous != expected:
                        raise SmolvmRuntimeError(
                            "Runtime ownership/spec changed; clean the original runtime first"
                        )
                marker_fd = os.open(
                    marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
                )
                with os.fdopen(marker_fd, "w") as stream:
                    json.dump({**expected, "generation": self._generation}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                self._owned = True
                self.cleanup_complete = False
                _directory(self._active_record.parent)
                fd = os.open(
                    self._active_record,
                    os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                    0o600,
                )
                with os.fdopen(fd, "w") as stream:
                    json.dump(
                        {"actor": self.channel.actor, "output": str(self.output)},
                        stream,
                    )
                    stream.flush()
                    os.fsync(stream.fileno())
                self._env = _private_environment(self.output, reuse=True)
                # Reclaim ONLY the same worker's prior generation, before new relay.
                self._env["SMOLVM_AGENT_ROOTFS"] = str(
                    self.output / "engine/agent-rootfs"
                )
                tools = self.output / "engine/bin"
                self._env["PATH"] = str(tools) + ":" + self._env["PATH"]
                if (self.output / "engine/agent-rootfs").exists():
                    self._clean_engine()
                    self._env = _private_environment(self.output)
                self._before_host = _host_canaries()
                seed = self.output / "engine/agent-rootfs"
                shutil.copytree(
                    Path(self.spec.smolvm_path).parent / "agent-rootfs",
                    seed,
                    symlinks=True,
                )
                self._env["SMOLVM_AGENT_ROOTFS"] = str(seed)
                tools.mkdir(mode=0o700)
                (tools / "resize2fs").symlink_to(self.spec.resize2fs_path)
                self._env["PATH"] = str(tools) + ":" + self._env["PATH"]
                if self._admin("--version").strip() != b"smolvm 1.19.0":
                    raise SmolvmRuntimeError("Only pinned smolvm 1.19.0 is implemented")
                relay_dir = self.output / "relay"
                if relay_dir.exists():
                    # Prior daemon died: its listener cannot survive process exit.
                    # We hold the actor lease and never adopt another directory.
                    if relay_dir.is_symlink() or set(
                        p.name for p in relay_dir.iterdir()
                    ) - {"daemon.sock"}:
                        raise SmolvmRuntimeError("Unexpected stale relay contents")
                    (relay_dir / "daemon.sock").unlink(missing_ok=True)
                    relay_dir.rmdir()
                self._relay = BoundWorkerRelay(
                    directory=relay_dir,
                    daemon_socket=self.channel.state_dir / "daemon.sock",
                    binding=self.binding,
                )
                self._relay.start()
                create = [
                    "machine",
                    "create",
                    "--name",
                    "worker",
                    "--image",
                    self.spec.rootfs_path,
                    "--cpus",
                    "2",
                    "--mem",
                    "1024",
                    "--storage",
                    "1",
                    "--overlay",
                    "1",
                    "--net",
                    "--mount-socket",
                    f"{self._relay.socket_path}:{GUEST_WORKER_STATE}/daemon.sock",
                ]
                for mount in mounts:
                    # Same path, different namespace: host receipts remain authoritative.
                    create += ["--volume", f"{mount}:{mount}:rw"]
                self._admin(*create)
                self._admin("machine", "start", "--name", "worker", timeout=30)
                # Independent native sandbox smoke. Failure is a failed sandbox,
                # not a compatibility-matrix veto. No model credentials needed.
                warnings.warn(
                    "smolvm Codex carrier is outside the published support matrix; "
                    "native sandbox smoke is required and model/resume remain unverified",
                    RuntimeWarning,
                    stacklevel=2,
                )
                self._admin(
                    "machine",
                    "exec",
                    "--name",
                    "worker",
                    "--timeout",
                    "10s",
                    "--",
                    "/bin/sh",
                    "-c",
                    _SANDBOX_SMOKE,
                    "hyprial-sandbox-smoke",
                    self.spec.guest_command[0],
                )
                guest = dict(environment)
                guest.update(
                    HYPRIAL_HOME=GUEST_WORKER_STATE,
                    HARNESS_STATE_DIR=GUEST_WORKER_STATE,
                    PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                    TMPDIR="/tmp",
                )
                if any(
                    key in guest
                    for key in ("SSH_AUTH_SOCK", "SSH_ASKPASS", "GIT_SSH_COMMAND")
                ):
                    raise SmolvmRuntimeError(
                        "Host SSH capabilities are not implemented in this runtime"
                    )
                # Secret values are in the exec environment only, under fresh names;
                # never argv, disk/VM config or administrative command capture.
                argv = [
                    self.spec.smolvm_path,
                    "machine",
                    "exec",
                    "--name",
                    "worker",
                    "-i",
                    "--stream",
                ]
                if self.cwd:
                    argv += ["--workdir", self.cwd]
                host_env = dict(self._env)
                for index, (key, value) in enumerate(sorted(guest.items())):
                    if not re.fullmatch("[A-Za-z_][A-Za-z0-9_]*", key):
                        raise SmolvmRuntimeError("Invalid guest environment name")
                    private_name = f"HYPRIAL_VM_VALUE_{index}"
                    host_env[private_name] = value
                    argv += ["--secret-env", f"{key}={private_name}"]
                argv += ["--", *command]
                return tuple(argv), host_env
            except BaseException:
                self.close()
                raise

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._lease is None:
                if not self._owned or self.cleanup_complete:
                    return
                fd = os.open(self.output / "runtime.lock", os.O_RDWR | os.O_NOFOLLOW)
                self._lease = os.fdopen(fd, "w")
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BaseException:
                    self._lease.close()
                    self._lease = None
                    raise SmolvmRuntimeError(
                        "Replacement generation owns runtime cleanup"
                    ) from None
                marker = self.output / "owner.json"
                try:
                    replaced = (
                        not marker.exists()
                        or json.loads(_regular_read(marker)).get("generation")
                        != self._generation
                    )
                except BaseException:
                    self._lease.close()
                    self._lease = None
                    raise
                if replaced:
                    # A newer generation already reclaimed our resources. Never
                    # stop its VM when an old process retries bounded cleanup.
                    self._lease.close()
                    self._lease = None
                    self.cleanup_complete = True
                    return
            if not self._owned:
                self._lease.close()
                self._lease = None
                return
            errors = []
            try:
                if self._relay is not None:
                    try:
                        self._relay.close()  # Fence first; still attempt VM stop on relay error.
                        self._relay = None
                    except Exception:
                        errors.append("Relay cleanup incomplete")
                if self._env is not None:
                    try:
                        self._clean_engine()
                    except Exception:
                        errors.append(
                            "VM cleanup incomplete; retain owned runtime for recovery"
                        )
                if (self.output / "engine").exists() or (
                    self.output / "engine"
                ).is_symlink():
                    # Environment validation can fail before it is assigned,
                    # while a crashed generation's VM is still alive. Never
                    # erase the ownership proof without proving engine cleanup.
                    errors.append(
                        "Runtime engine remains; retain owned engine for recovery"
                    )
                if tree_digest(Path(self.spec.rootfs_path)) != self.spec.rootfs_digest:
                    errors.append("Source rootfs changed")
                if (
                    self._before_host is not None
                    and self._before_host != _host_canaries()
                ):
                    errors.append("Real HOME/TMP canary changed")
                if errors:
                    raise SmolvmRuntimeError("; ".join(errors))
                (self.output / "owner.json").unlink(missing_ok=True)
                self._active_record.unlink(missing_ok=True)
                self.cleanup_complete = True
            finally:
                # On failure the marker/engine stays: replacement must first clean it.
                self._lease.close()
                self._lease = None
