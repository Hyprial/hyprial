"""Container runtime for containerized worker transfer (design: docs/design-transfer-container.md).

A containerized worker runs its harness process inside a docker container
that carries the ORIGINAL owner's credentials, so the landed worker keeps
working under the original owner's identity and quota.  This module owns
everything about that runtime:

- the image reference (built from the repo, shipped as a ``docker save``
  tar through the existing transfer pipeline -- decision D-C);
- the credential bundle manifest per harness and the staging -> volume ->
  tmpfs-HOME pipeline (decision D-A: credentials live in a per-worker
  docker volume during adoption, are shredded from staging right after
  receive, and the volume is removed when the worker retires);
- the ``docker run`` argv that wraps a harness launch command, keeping the
  three worker<->daemon channels (pi stdio + bridge socket, codex
  app-server stdio, claude MCP agent-channel) alive through same-path
  bind mounts;
- retire/prune helpers used by down/destroy/transfer cleanup.

Security invariants asserted by tests:

- credentials NEVER enter the image (built identity-less) and NEVER travel
  in argv/env -- they move scp -> staging (0700/0600) -> named volume and
  are mounted read-only;
- the container sees NO host HOME and NO API-key environment, so a worker
  whose credential mount is removed MUST fail authentication instead of
  silently falling back to host credentials (the mutation criterion).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Mapping, Sequence

from hyprial.contracts import ipc_errors

#: Fixed paths INSIDE the worker image (docker/Dockerfile.hyprial-worker).
CONTAINER_HOME = "/home/hyprial"
CRED_MOUNT = "/hyprial-cred"
CONTAINER_PYTHON = "/opt/hyprial/venv/bin/python"
#: The image's persistent claude Agent SDK environment: isolation is
#: inherent inside a container, so no runtime `uv run --isolated` (and no
#: runtime network) is needed -- P0 docker verification lesson #3.
CONTAINER_SDK_PYTHON = "/opt/hyprial/sdk-venv/bin/python"
CONTAINER_UV = "uv"  # on the image PATH
CONTAINER_PI_EXTENSION = "/opt/hyprial/pi_harness_bridge.ts"
CONTAINER_SDK_WORKER = "/opt/hyprial/_agent_sdk_worker.py"
CONTAINER_USER = "hyprial"

#: docker label marking every worker container for prune-by-worker.
WORKER_LABEL = "hyprial.worker"

#: Environment the container inherits from the daemon BY NAME ONLY.
#: Everything else -- above all host HOME and any *_API_KEY -- stays out,
#: which is what makes the no-fallback mutation criterion provable.
PASSTHROUGH_ENV = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)

#: Prefixes every env_delta key must match to enter the container at all
#: (worker/channel/options env is all HYPRIAL_*/HARNESS_*).  Tests assert the
#: three harness clients never hand docker_run_argv a delta outside this
#: set -- an *_API_KEY riding a delta would silently break the
#: no-fallback credential boundary.
CONTAINER_ENV_PREFIXES = ("HYPRIAL_", "HARNESS_")

#: Fixed keys the wrapper itself sets (HOME/USER/LANG) plus the
#: passthrough whitelist -- the full legal key set for any -e flag.
CONTAINER_ENV_FIXED = ("HOME", "USER", "LANG")

#: Credential files per harness, as home-relative archive names.  Verified
#: 2026-08-24 against claude 2.1.241 / codex 0.147.0 / pi 0.83.0 (paths and
#: permission bits only; contents never read).  claude on macOS ALSO keeps
#: its primary credential in the login Keychain -- exporting that is an
#: explicit, operator-authorized source-side step that lands the same file.
CREDENTIAL_FILES: dict[str, tuple[tuple[str, bool], ...]] = {
    "claude": ((".claude/.credentials.json", True),),
    "codex": (
        (".codex/auth.json", True),
        (".codex/.credentials.json", False),
    ),
    "pi": ((".pi/agent/auth.json", True),),
}

#: Session state dirs per harness, home-relative.  These are bind-mounted
#: read-write from the per-worker host home so transcripts persist across
#: container restarts while the tmpfs HOME (and the credentials the
#: entrypoint copies into it) never touches host storage.
SESSION_DIRS: dict[str, str] = {
    "claude": ".claude/projects",
    "codex": ".codex/sessions",
    "pi": ".pi/agent/sessions",
}

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")


class ContainerError(RuntimeError):
    """Container-pipeline failure with a stable code for callers."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def default_image() -> str:
    """The worker image tag for this hyprial build (``+`` is illegal in tags)."""

    from hyprial import __version__

    return f"hyprial-worker:{__version__.replace('+', '-')}"


def _digest(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]


def volume_name(name: str) -> str:
    """The per-worker credential volume (removed when the worker retires)."""

    return f"hyprial-xfer-cred-{_digest(name)}"


def staging_dir(state_dir: Path, name: str) -> Path:
    """Where credential bundles (and the image tar) stage on the target."""

    return Path(state_dir) / "xfer-cred" / _digest(name)


def worker_home(state_dir: Path, harness: str, name: str) -> Path:
    """The per-worker host home holding session dirs (never credentials)."""

    safe = _SAFE_NAME.sub("-", f"{harness}-{name}")
    return Path(state_dir) / "container-workers" / safe / "home"


def session_mount(harness: str, home: Path) -> tuple[Path, str]:
    """(host dir, container path) of the worker's persistent session state."""

    try:
        relative = SESSION_DIRS[harness]
    except KeyError as error:
        raise ContainerError(
            ipc_errors.TRANSFER_UNSUPPORTED_HARNESS,
            f"{harness} has no container session layout",
        ) from error
    return Path(home) / relative, f"{CONTAINER_HOME}/{relative}"


def credential_bundle(harness: str, home: Path) -> dict[str, Path]:
    """The source credential files to ship, arcname -> local path.

    Fails loud when a REQUIRED file is missing: a containerized transfer
    without the owner's credentials would land a worker that cannot act as
    the owner -- worse than refusing.  Reads nothing; paths and existence
    only.
    """

    try:
        manifest = CREDENTIAL_FILES[harness]
    except KeyError as error:
        raise ContainerError(
            ipc_errors.TRANSFER_UNSUPPORTED_HARNESS,
            f"{harness} has no credential bundle manifest",
        ) from error
    bundle: dict[str, Path] = {}
    missing: list[str] = []
    for arcname, required in manifest:
        candidate = Path(home) / arcname
        if candidate.is_file():
            bundle[arcname] = candidate
        elif required:
            missing.append(arcname)
    if missing:
        raise ContainerError(
            ipc_errors.TRANSFER_CREDENTIALS,
            f"missing required {harness} credential file(s) under {home}: "
            + ", ".join(missing)
            + "; on macOS claude's primary credential lives in the login "
            "Keychain -- export it explicitly (operator-authorized) to "
            ".claude/.credentials.json first",
        )
    return bundle


def docker_run_argv(
    *,
    harness: str,
    name: str,
    image: str,
    inner_argv: Sequence[str],
    env_delta: Mapping[str, str],
    state_dir: Path,
    cwd: str,
    home: Path,
    cred_volume: str | None,
    uid: int,
    gid: int,
    docker: str = "docker",
    environ: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Wrap a harness launch command in ``docker run``.

    Mount contract (see module docstring): worktree and daemon state dir at
    their SAME absolute paths (keeps every unix-socket channel working),
    the harness session dir read-write from the worker home, the credential
    volume read-only, a tmpfs HOME the entrypoint populates and then drops
    privileges into.  Environment enters only through explicit ``-e``
    flags: the worker/channel delta, HOME/USER/LANG, the entrypoint's
    uid/gid, and the proxy whitelist.
    """

    environ = os.environ if environ is None else environ
    session_host, session_container = session_mount(harness, home)
    argv: list[str] = [
        docker,
        "run",
        "-i",
        "--rm",
        "--label",
        f"{WORKER_LABEL}={harness}:{name}",
        "--tmpfs",
        f"{CONTAINER_HOME}:rw,uid={uid},gid={gid}",
        "-v",
        f"{cwd}:{cwd}",
        "-v",
        f"{Path(state_dir)}:{Path(state_dir)}",
        "-v",
        f"{session_host}:{session_container}",
        "-w",
        cwd,
        "-e",
        f"HOME={CONTAINER_HOME}",
        "-e",
        f"USER={CONTAINER_USER}",
        "-e",
        "LANG=C.UTF-8",
        "-e",
        f"HYPRIAL_CONTAINER_UID={uid}",
        "-e",
        f"HYPRIAL_CONTAINER_GID={gid}",
    ]
    if cred_volume is not None:
        argv += ["-v", f"{cred_volume}:{CRED_MOUNT}:ro"]
    for key in PASSTHROUGH_ENV:
        value = environ.get(key)
        if value:
            # Docker copies a bare ``-e KEY`` from the CLI process environment.
            # Never place proxy credentials or API tokens in process argv.
            argv += ["-e", key]
    for key in env_delta:
        argv += ["-e", key]
    argv += [image, *inner_argv]
    return tuple(argv)


#: Image-fixed absolute path prefixes that survive argv[0] rewriting
#: (the claude SDK venv python lives at one).
_IMAGE_PATH_PREFIXES = ("/opt/hyprial/",)


def rewrite_inner_argv(
    harness: str, inner_argv: Sequence[str]
) -> tuple[str, ...]:
    """Container-mode argv[0]: the image's binary, never a host path.

    #259 pins ``resolved_command`` to the HOST absolute path (e.g.
    ``/Users/yaosh/.local/bin/codex``); that path does not exist inside
    the container, so appending it verbatim lands ENOENT for pi/codex
    (review §A; claude is immune because its argv[0] is already an
    image-fixed constant).  The image pins its own harness binaries on
    PATH -- that pinning is the image's whole purpose -- so argv[0]
    degrades to the bare harness binary name unless it already points
    inside the image.
    """

    if not inner_argv:
        raise ContainerError(ipc_errors.TRANSFER_CONTAINER, "empty inner argv")
    head = inner_argv[0]
    if head.startswith(_IMAGE_PATH_PREFIXES):
        return tuple(inner_argv)
    return (harness, *inner_argv[1:])


def wrap_worker_launch(
    spec: object,
    *,
    inner_argv: Sequence[str],
    env_delta: Mapping[str, str],
    state_dir: Path,
) -> tuple[str, ...]:
    """One seam for the three harness clients: wrap a launch in docker run.

    ``spec`` is a HarnessLaunchSpec (duck-typed here to keep this module
    import-light for the harness clients).  Requires spec.containerized.
    """

    harness = str(getattr(spec, "harness"))
    name = str(getattr(spec, "name"))
    cwd = getattr(spec, "cwd") or "/"
    image = getattr(spec, "container_image") or default_image()
    return docker_run_argv(
        harness=harness,
        name=name,
        image=str(image),
        inner_argv=rewrite_inner_argv(harness, inner_argv),
        env_delta=env_delta,
        state_dir=Path(state_dir),
        cwd=str(cwd),
        home=worker_home(state_dir, harness, name),
        cred_volume=volume_name(name),
        uid=os.getuid(),
        gid=os.getgid(),
    )


class DockerRunner:
    """Local docker via dumb subprocesses; tests inject a fake."""

    def __init__(self, docker: str = "docker", timeout: float = 120.0) -> None:
        self.docker = docker
        self.timeout = timeout

    def run(self, argv: Sequence[str], *, timeout: float | None = None) -> None:
        command = [self.docker, *argv]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                timeout=self.timeout if timeout is None else timeout,
                check=False,
            )
        except FileNotFoundError as error:
            raise ContainerError(
                ipc_errors.TRANSFER_PREREQUISITE, "docker is not installed on this host"
            ) from error
        except subprocess.TimeoutExpired as error:
            raise ContainerError(
                ipc_errors.TRANSFER_CONTAINER, f"docker {' '.join(argv)} timed out"
            ) from error
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise ContainerError(
                ipc_errors.TRANSFER_CONTAINER,
                f"docker {' '.join(argv)} failed: {detail}",
            )

    def output(self, argv: Sequence[str]) -> str:
        command = [self.docker, *argv]
        try:
            result = subprocess.run(
                command, capture_output=True, timeout=self.timeout, check=False
            )
        except FileNotFoundError as error:
            raise ContainerError(
                ipc_errors.TRANSFER_PREREQUISITE, "docker is not installed on this host"
            ) from error
        if result.returncode != 0:
            return ""
        return result.stdout.decode("utf-8", errors="replace")

    def image_present(self, image: str) -> bool:
        command = [self.docker, "image", "inspect", image]
        try:
            result = subprocess.run(
                command, capture_output=True, timeout=self.timeout, check=False
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0


def prepare_worker(
    runner: DockerRunner,
    *,
    image: str,
    name: str,
    staging: Path,
    state_dir: Path,
    harness: str,
    credentials: bool = True,
) -> str:
    """Receive-side container setup; returns the credential volume name.

    Loads the image tar when one staged along, creates the per-worker
    volume, copies the staged bundle in through a short-lived helper
    container, then SHREDS the staging dir -- the staging residual window
    is the receive call itself.  Worker home/session dirs are created with
    owner-only permissions.
    """

    if not runner.image_present(image):
        tar = Path(staging) / "image.tar"
        if tar.is_file():
            runner.run(["load", "-i", str(tar)], timeout=600.0)
        if not runner.image_present(image):
            raise ContainerError(
                ipc_errors.TRANSFER_PREREQUISITE,
                f"worker image {image!r} is not loaded on this host",
            )
    home = worker_home(state_dir, harness, name)
    session_host, _ = session_mount(harness, home)
    session_host.mkdir(parents=True, exist_ok=True)
    os.chmod(Path(home).parent, 0o700)
    os.chmod(home, 0o700)
    volume = volume_name(name)
    if credentials:
        runner.run(["volume", "create", volume])
        runner.run(
            [
                "run",
                "--rm",
                "--entrypoint",
                "sh",
                "-v",
                f"{staging}:/staging:ro",
                "-v",
                f"{volume}:/cred",
                image,
                "-c",
                # Everything EXCEPT the image tar: the bundle alone is the
                # credential volume's content.
                "cd /staging && tar -cf - --exclude=./image.tar . | "
                "tar -C /cred -xf - && chmod -R go-rwx /cred",
            ]
        )
    shutil.rmtree(staging, ignore_errors=True)
    return volume


def prune_worker(
    runner: DockerRunner,
    *,
    harness: str,
    name: str,
    state_dir: Path | None = None,
    keep_volume: bool = False,
) -> list[str]:
    """Best-effort retire: containers by label, the volume, the worker home.

    Returns human-readable leftovers (never raises): cleanup must not mask
    the operation that triggered it, but leftovers are reported loudly.
    """

    problems: list[str] = []
    label = f"{WORKER_LABEL}={harness}:{name}"
    ids = runner.output(["ps", "-aq", "--filter", f"label={label}"]).split()
    for container_id in ids:
        try:
            runner.run(["rm", "-f", container_id])
        except ContainerError as error:
            problems.append(f"container {container_id}: {error}")
    if not keep_volume:
        try:
            runner.run(["volume", "rm", "-f", volume_name(name)])
        except ContainerError as error:
            problems.append(f"volume {volume_name(name)}: {error}")
    if state_dir is not None:
        home = worker_home(state_dir, harness, name).parent
        try:
            shutil.rmtree(home)
        except FileNotFoundError:
            pass
        except OSError as error:
            problems.append(f"worker home {home}: {error}")
    return problems


def shred_tree(path: Path) -> None:
    """Remove a staging tree (credentials must not linger on the host)."""

    shutil.rmtree(path, ignore_errors=True)
