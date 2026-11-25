"""Prepare an isolated hyprial home that a serving daemon projects as ready.

Decision record (S2, 2026-11-21):
- Callers: ``gui/tests/**`` browser acceptance, ``tests/**`` pytest suites, and
  future ``contract/e2e`` runners. Product code under ``src/hyprial/**`` must
  never import this module. It lives in ``tests/support/`` rather than the
  product CLI so it is excluded from shipped wheels (``pyproject.toml`` packs
  only ``src/hyprial``) and cannot be invoked accidentally against a user home.
- Sequence: ``mint_node_id`` -> ``write_settings_identity`` ->
  ``write_device_record`` -> ``OperationService`` (``accept_initialize`` +
  ``mark_initialize(..., "completed")``), followed by ``.test-ready-home``.
  No product JSON file is hand-written; every product file comes from its
  product writer.
- ``OperationService`` and ``mint_node_id`` are imported from ``daemon/impl``
  and ``kernel/impl`` (matching ``test_serving_account_recovery.py`` and
  ``tests/support/runtime/environment.py``, per acceptance plan R5).
- ``mode="casdoor"`` with ``DEFAULT_ISSUER`` plus ``user_id`` and
  ``device_uid`` is used because ``read_onboarding_subject`` requires a non-null
  issuer, userId, and deviceUid, while ``write_settings_identity`` rejects
  ``local-usage`` when an issuer is provided.
- First-run state is intentionally NOT written here: callers send a real
  ``onboarding.firstRun.skip`` (or run the first-run flow) over IPC/HTTP after
  starting the daemon.
- Real HOME protection (fail closed, err on over-rejection):
  Pytest's autouse ``_isolated_hyprial_home`` fixture rewrites ``HOME`` in
  ``os.environ`` to a temporary ``operating-home`` directory. Checking only
  ``Path.home()`` would therefore miss the caller's real OS account directory
  during tests, whereas checking only ``pwd`` would miss tests that simulate a
  user home via ``HOME``. We reject any target (or explicit ``state_dir``) that
  equals or lies under ANY of:
  1. the real OS account directory from ``pwd.getpwuid(os.getuid()).pw_dir``;
  2. ``os.environ["HOME"]`` / ``Path.home()`` when set;
  3. ``Path("~/.hyprial").expanduser()`` and ``default_hyprial_home()[0]``.
  Because this errs on the side of safety, checkouts inside a user's real home
  (such as ``/home/<user>/Workspace/.../.tmp-work``) are also rejected; callers
  on such hosts must place isolated homes outside ``HOME`` (e.g. ``/tmp``).
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from uuid import uuid4

from hyprial.daemon.impl.onboarding.operations import OperationService, journal
from hyprial.daemon.impl.request_catalog.onboarding import CallContext, Surface
from hyprial.identity import (
    DeviceRecord,
    device_key_path,
    device_record_path,
    settings_path,
    write_device_record,
    write_settings_identity,
)
from hyprial.kernel import default_hyprial_home, is_identity_id_segment
from hyprial.kernel.impl.primitives.node_identity import mint_node_id, node_id_path

DEFAULT_ISSUER = "https://issuer.example.test"
DEFAULT_MODE = "casdoor"
DEFAULT_DEVICE_NAME = "test-ready-device"
DEFAULT_GENERATION = "test-ready-1"
DEFAULT_INITIALIZE_KEY = "test-ready-initialize-key-0001"
DEFAULT_SERVER_PUBLIC = "nodekey:test-ready-server-public"
DEFAULT_CLIENT_PUBLIC = "nodekey:test-ready-client-public"
MARKER_FILENAME = ".test-ready-home"
DEVICE_IDENTITY_RELPATH = "state/tailcat/device-identity.json"
MAX_OWNER_LENGTH = 320
MAX_SURFACE_TEXT = 512


class ReadyHomeError(RuntimeError):
    """Raised when preparing a test-ready home violates an isolation or safety guard."""


@dataclass(frozen=True, slots=True)
class ReadyHome:
    """Summary of one prepared, device-ready isolated home."""

    home: Path
    state_dir: Path
    operations_root: Path
    owner: str
    node_id: str
    user_id: str
    device_uid: str
    issuer: str
    mode: str
    marker_path: Path
    written_files: tuple[Path, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": True,
            "home": str(self.home),
            "stateDir": str(self.state_dir),
            "operationsRoot": str(self.operations_root),
            "owner": self.owner,
            "nodeId": self.node_id,
            "userId": self.user_id,
            "deviceUid": self.device_uid,
            "issuer": self.issuer,
            "mode": self.mode,
            "markerPath": str(self.marker_path),
            "writtenFiles": [str(path) for path in self.written_files],
        }


def _require_network_isolated() -> None:
    if os.environ.get("HYPRIAL_NETWORK_ISOLATED") != "1":
        raise ReadyHomeError(
            "HYPRIAL_NETWORK_ISOLATED must equal '1' in the process environment"
        )


def _validate_owner(owner: str) -> str:
    if not isinstance(owner, str):
        raise ReadyHomeError("owner must be a non-empty string")
    value = owner.strip()
    if len(value) < 3 or len(value) > MAX_OWNER_LENGTH:
        raise ReadyHomeError(
            f"owner length must be between 3 and {MAX_OWNER_LENGTH} characters"
        )
    if ":" in value or is_identity_id_segment(value.lower()):
        raise ReadyHomeError(
            f"owner must not contain ':' or match an identity id shape: {owner!r}"
        )
    return value


def _real_account_home() -> Path | None:
    try:
        import pwd
    except ImportError:
        return None
    try:
        pw_dir = pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:
        return None
    if not pw_dir:
        return None
    return Path(pw_dir).expanduser().resolve()


def _forbidden_bases() -> tuple[tuple[str, Path], ...]:
    bases: list[tuple[str, Path]] = []
    real_home = _real_account_home()
    if real_home is not None:
        bases.append(("caller real HOME (passwd)", real_home))
    env_home = os.environ.get("HOME")
    if env_home and env_home.strip():
        bases.append(("HOME environment directory", Path(env_home).expanduser().resolve()))
    try:
        bases.append(("Path.home()", Path.home().expanduser().resolve()))
    except RuntimeError:
        pass
    bases.append(("~/.hyprial", Path("~/.hyprial").expanduser().resolve()))
    bases.append(("default hyprial home", default_hyprial_home()[0].resolve()))
    return tuple(bases)


def _validate_isolated_directory(raw_path: Path | str, *, label: str) -> Path:
    if isinstance(raw_path, str):
        if not raw_path.strip():
            raise ReadyHomeError(f"explicit {label} path is required")
        candidate = Path(raw_path)
    elif isinstance(raw_path, Path):
        if not str(raw_path).strip():
            raise ReadyHomeError(f"explicit {label} path is required")
        candidate = raw_path
    else:
        raise ReadyHomeError(f"explicit {label} path is required")
    expanded = candidate.expanduser()
    if expanded.is_symlink():
        raise ReadyHomeError(f"{label} must not be a symbolic link: {expanded}")
    if expanded.exists() and not expanded.is_dir():
        raise ReadyHomeError(f"{label} exists and is not a directory: {expanded}")
    resolved = expanded.resolve()
    if resolved == Path(resolved.anchor):
        raise ReadyHomeError(f"{label} must not be a filesystem root: {resolved}")
    for reason, base in _forbidden_bases():
        if resolved == base or resolved.is_relative_to(base):
            raise ReadyHomeError(
                f"refusing {label} {resolved}: matches or lies inside {reason} ({base})"
            )
    return resolved


def _ensure_no_existing_artifacts(home: Path, operations_root: Path) -> None:
    if settings_path({}, home).exists() or settings_path({}, home).is_symlink():
        raise ReadyHomeError(
            f"refusing to overwrite existing settings.json in {home}"
        )
    device_paths = (
        device_record_path(home),
        home / DEVICE_IDENTITY_RELPATH,
        device_key_path(home),
    )
    for path in device_paths:
        if path.exists() or path.is_symlink():
            raise ReadyHomeError(
                f"refusing to overwrite existing device record at {path}"
            )
    if node_id_path(home).exists() or node_id_path(home).is_symlink():
        raise ReadyHomeError(
            f"refusing to overwrite existing node-id at {node_id_path(home)}"
        )
    marker = home / MARKER_FILENAME
    if marker.exists() or marker.is_symlink():
        raise ReadyHomeError(
            f"target home already carries {MARKER_FILENAME}: {marker}"
        )
    existing_journal = journal.journal_path(operations_root)
    if existing_journal.exists() or existing_journal.is_symlink():
        raise ReadyHomeError(
            f"refusing to overwrite existing onboarding journal at {existing_journal}"
        )
    if home.exists():
        for directory, dirnames, filenames in os.walk(home, followlinks=False):
            current = Path(directory)
            for name in dirnames:
                if (current / name).is_symlink():
                    raise ReadyHomeError(
                        f"target home contains a symbolic link: {current / name}"
                    )
            if filenames:
                raise ReadyHomeError(
                    f"target home must not contain existing files: {current / filenames[0]}"
                )
    if operations_root.exists():
        for directory, dirnames, filenames in os.walk(operations_root, followlinks=False):
            current = Path(directory)
            for name in dirnames:
                if (current / name).is_symlink():
                    raise ReadyHomeError(
                        f"operations root contains a symbolic link: {current / name}"
                    )
            if filenames:
                raise ReadyHomeError(
                    f"operations root must not contain existing files: {current / filenames[0]}"
                )


def _write_marker(marker_path: Path, *, owner: str, node_id: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    payload = f"test-ready-home\nowner={owner}\nnode_id={node_id}\n".encode("utf-8")
    descriptor = os.open(marker_path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor != -1:
            os.close(descriptor)


def prepare_ready_home(
    home: Path | str,
    *,
    owner: str,
    state_dir: Path | str | None = None,
    issuer: str = DEFAULT_ISSUER,
    user_id: str | None = None,
    device_uid: str | None = None,
    device_name: str = DEFAULT_DEVICE_NAME,
    generation: str = DEFAULT_GENERATION,
    initialize_key: str = DEFAULT_INITIALIZE_KEY,
) -> ReadyHome:
    """Create an isolated, device-ready hyprial home using product writers."""

    _require_network_isolated()
    owner_value = _validate_owner(owner)
    resolved_home = _validate_isolated_directory(home, label="home")
    if state_dir is None:
        resolved_state = resolved_home / "state"
    else:
        resolved_state = _validate_isolated_directory(state_dir, label="state_dir")
    operations_root = resolved_state / "onboarding"
    _ensure_no_existing_artifacts(resolved_home, operations_root)

    resolved_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(resolved_home, 0o700)
    resolved_state.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(resolved_state, 0o700)

    resolved_user_id = user_id or uuid4().hex
    resolved_device_uid = device_uid or uuid4().hex
    node_id = mint_node_id(resolved_home)
    settings_file = write_settings_identity(
        owner_value,
        mode=DEFAULT_MODE,
        issuer=issuer,
        user_id=resolved_user_id,
        username=owner_value,
        environ={},
        hyprial_home=resolved_home,
    )
    write_device_record(
        resolved_home,
        DeviceRecord(
            device_id=node_id,
            owner=owner_value,
            server_public=DEFAULT_SERVER_PUBLIC,
            client_public=DEFAULT_CLIENT_PUBLIC,
            key_generation=1,
            device_uid=resolved_device_uid,
            name=device_name,
        ),
    )

    home_binding = str(resolved_home)
    if len(home_binding) > MAX_SURFACE_TEXT:
        home_binding = "home-binding"
    service = OperationService.open(operations_root)
    context = CallContext(
        surface=Surface(home_binding, "host-control", "view", generation),
        phase="bootstrap",
        caller="local-bootstrap",
        owner=owner_value,
    )
    record = service.accept_initialize(
        context,
        initialize_key,
        f"op-init-{uuid4().hex[:12]}",
        f"rcpt-init-{uuid4().hex[:12]}",
        promotion=lambda _record: None,
    )
    service.mark_initialize(context, record.operation_id, "completed")

    marker_path = resolved_home / MARKER_FILENAME
    _write_marker(marker_path, owner=owner_value, node_id=node_id)

    written_files = (
        node_id_path(resolved_home),
        settings_file,
        device_record_path(resolved_home),
        resolved_home / DEVICE_IDENTITY_RELPATH,
        journal.journal_path(operations_root),
        marker_path,
    )
    return ReadyHome(
        home=resolved_home,
        state_dir=resolved_state,
        operations_root=operations_root,
        owner=owner_value,
        node_id=node_id,
        user_id=resolved_user_id,
        device_uid=resolved_device_uid,
        issuer=issuer,
        mode=DEFAULT_MODE,
        marker_path=marker_path,
        written_files=written_files,
    )
