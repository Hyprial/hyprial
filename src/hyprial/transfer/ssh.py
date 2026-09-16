"""SSH control plane for transfer: remote hyprial CLI + file transport.

The daemon's IPC is a local unix socket by design, so cross-machine control
runs the remote machine's own ``hyprial`` CLI over SSH (the internal-SSH
precedent: transport only, never credentials in argv or URLs).  Every method
is one dumb subprocess; all logic lives in the orchestrator and in the
daemon handlers on both ends.  Tests inject a fake runner — nothing here is
mocked away from the real code path in production.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from typing import Protocol


class RemoteError(RuntimeError):
    """One remote invocation failed; carries the remote's own output."""

    def __init__(self, host: str, argv: tuple[str, ...], detail: str) -> None:
        self.host = host
        self.argv = argv
        hint = ""
        if "@" not in host:
            hint = (
                "; no SSH username specified: ssh uses the configured SSH User "
                "or the local login name. If authentication failed, select the "
                f"remote account explicitly with --to user@{host}"
            )
        super().__init__(
            f"remote {host} failed running {' '.join(argv)}: {detail}{hint}"
        )


class RemoteRunner(Protocol):
    """The orchestrator's seam to the target machine."""

    remote_hyprial: str

    def run_json(self, argv: list[str], stdin: bytes | None = None) -> dict:
        """Run ``hyprial <argv...> --json`` remotely; return the parsed object."""
        ...

    def has_binary(self, name: str) -> bool:
        """Probe non-interactive PATH (docker also checks known install paths)."""
        ...

    def login_user(self) -> str:
        """Remote OS user executing SSH commands (not the hyprial owner)."""
        ...

    def image_present(self, image: str) -> bool:
        """True only when the exact image reference inspects successfully."""
        ...

    def upload(self, local: Path, remote_path: str) -> None:
        """Copy one file to an exact remote path (parent must exist)."""
        ...

    def sync_tree(self, local_dir: Path, remote_dir: str) -> None:
        """Recursively copy a directory tree (additive; never deletes)."""
        ...


class SshRunner:
    """The production runner: ssh for commands, scp for files, rsync for trees.

    ``remote_hyprial`` is the remote command prefix (default plain ``hyprial``).
    Real targets run a PRODUCTION hyprial on PATH; pointing at the build under
    test — and at the right daemon — looks like
    ``--remote-hyprial 'HYPRIAL_HOME=/tmp/hyprial-tgt /home/me/transfer-p0/.venv/bin/hyprial'``.
    """

    def __init__(
        self, host: str, *, timeout: float = 30.0, remote_hyprial: str = "hyprial"
    ) -> None:
        if not host or not host.strip():
            raise ValueError("transfer target host must not be empty")
        self.host = host
        self.timeout = timeout
        self.remote_hyprial = remote_hyprial
        self._remote_hyprial = tuple(shlex.split(remote_hyprial))
        if not self._remote_hyprial:
            raise ValueError("remote hyprial command must not be empty")

    def _run(
        self, argv: list[str], *, stdin: bytes | None = None, timeout: float | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                argv,
                input=stdin,
                capture_output=True,
                timeout=self.timeout if timeout is None else timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RemoteError(self.host, tuple(argv), str(error)) from error

    def run_json(self, argv: list[str], stdin: bytes | None = None) -> dict:
        command = ["ssh", self.host, *self._remote_hyprial, *argv, "--json"]
        # Receiving a worker blocks on strict-resume verification; the SSH
        # client timeout must outlive it by a comfortable margin.
        timeout = 300.0 if argv and argv[0] == "transfer-receive" else None
        result = self._run(command, stdin=stdin, timeout=timeout)
        if result.returncode != 0:
            raise RemoteError(
                self.host,
                tuple(command),
                result.stderr.decode("utf-8", errors="replace").strip()
                or result.stdout.decode("utf-8", errors="replace").strip(),
            )
        try:
            value = json.loads(result.stdout.decode("utf-8"))
        except json.JSONDecodeError as error:
            raise RemoteError(
                self.host, tuple(command), f"invalid JSON output: {error}"
            ) from error
        if not isinstance(value, dict):
            raise RemoteError(self.host, tuple(command), "output is not a JSON object")
        if value.get("ok") is not True:
            raise RemoteError(
                self.host,
                tuple(command),
                str(value.get("error") or value),
            )
        return value

    def _docker_path(self) -> str | None:
        result = self._run(["ssh", self.host, "command", "-v", "docker"])
        if result.returncode == 0:
            path = result.stdout.decode("utf-8", errors="replace").strip()
            if path:
                return path
        # macOS non-login SSH can omit Docker Desktop/Homebrew locations.
        # Use the SAME resolved path for image inspection, independently of
        # remote_hyprial, rather than losing the fallback when running Docker.
        for path in ("/usr/local/bin/docker", "/opt/homebrew/bin/docker"):
            if self._run(["ssh", self.host, "test", "-x", path]).returncode == 0:
                return path
        return None

    def has_binary(self, name: str) -> bool:
        if name == "docker":
            return self._docker_path() is not None
        return self._run(["ssh", self.host, "command", "-v", name]).returncode == 0

    def login_user(self) -> str:
        command = ["ssh", self.host, "id", "-un"]
        result = self._run(command)
        user = result.stdout.decode("utf-8", errors="replace").strip()
        if result.returncode != 0 or not user or any(char.isspace() for char in user):
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise RemoteError(
                self.host, tuple(command),
                f"could not determine SSH login user: {detail or repr(user)}",
            )
        return user

    def image_present(self, image: str) -> bool:
        docker = self._docker_path()
        if docker is None:
            return False
        result = self._run([
            "ssh", self.host, shlex.quote(docker), "image", "inspect", "--", shlex.quote(image),
        ])
        return result.returncode == 0

    def upload(self, local: Path, remote_path: str) -> None:
        destination = f"{self.host}:{quote_remote_path(remote_path)}"
        # Separate data-plane budget from the 30s control-plane timeout.
        # Allow 60s setup plus transfer at a conservative 1 MiB/s; do not
        # shorten an explicitly longer runner timeout. stat reads no data.
        upload_timeout = max(self.timeout, 60.0 + local.stat().st_size / (1024 * 1024))
        result = self._run(["scp", "-q", str(local), destination], timeout=upload_timeout)
        if result.returncode != 0:
            raise RemoteError(
                self.host,
                ("scp", str(local), destination),
                result.stderr.decode("utf-8", errors="replace").strip(),
            )

    def sync_tree(self, local_dir: Path, remote_dir: str) -> None:
        # No --protect-args: macOS ships openrsync 2.6.9, which rejects the
        # flag (found by the docker pseudo-machine verification).  Quoting
        # the remote part survives both rsync 3 and openrsync because the
        # remote shell strips it.
        destination = f"{self.host}:{quote_remote_path(remote_dir)}"
        result = self._run(
            [
                "rsync",
                "-a",
                "--",
                f"{local_dir}/",
                destination,
            ],
            timeout=None,
        )
        if result.returncode != 0:
            raise RemoteError(
                self.host,
                ("rsync", f"{local_dir}/", destination),
                result.stderr.decode("utf-8", errors="replace").strip(),
            )


def quote_remote_path(path: str) -> str:
    """Shell-quote a remote path fragment (defense for scp/rsync targets)."""

    return shlex.quote(path)
