"""One-time rename from ``~/.h2b`` to ``~/.hyprial``.

Allen's migration rule is intentionally small: when the new default is absent,
rename the old default into place; once the new default exists, do nothing
beyond a best-effort completion of the sidecar binary rename (a retry of the
step whose one-time failure is ``sidecar-bin-rename-failed``).
There is no rollback compatibility path, duplicate copy, space preflight, or
cross-copy verification because the same-filesystem directory rename is the
migration itself.
"""

from __future__ import annotations

import fcntl
import json
import os
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hyprial.contracts import ipc_errors

_STATE_DATABASES = (
    "agents.sqlite3",
    "inbox.sqlite3",
    "workflows.sqlite3",
    "pac-graph.sqlite3",
    "lifecycle-operations.sqlite3",
)

_LEGACY_SIDECAR_BIN_NAME = "h2b-tsnet"

#: Every state file whose ``schema`` value the rename leaves stale, as an
#: explicit ``(path relative to the home, old value, new value)`` row.
#:
#: A table rather than a pattern, because three neighbouring classes look
#: identical to any regex written against the old product token, and must not
#: be touched:
#:
#: * prose and retired shapes, where a rewrite edits the record of a decision;
#: * names that only *look* stale and are in fact current --
#:   ``h2b-agent-task/v1`` is still what dev emits, in ten files;
#: * copies whose content belongs to an upstream git checkout, where a rewrite
#:   is undone by the next pull while review sees it as done.
#:
#: So the two members here are exactly the files this host generates itself.
#: ``apps/gui/source/**`` is a clone of ``HyprialOS/dsh-h2b-talk``, whose
#: content is fixed upstream rather than here.
#:
#: An earlier version of this comment named ``start-web.sh`` as that clone's
#: launch-info producer. That was wrong, and the way it was wrong is the point:
#: it was read out of a local checkout 155 commits behind upstream and reported
#: as an upstream fact. On ``origin/main`` that file contains no ``schema`` at
#: all, and there are *two* producers, not one. The correction is deliberately
#: left without their paths: naming them here would put a third copy of a
#: fast-moving upstream fact in a file that cannot verify it.
_SCHEMA_REWRITES: tuple[tuple[str, str, str], ...] = (
    (
        "state/orphan-processes.json",
        "h2b.orphan-processes/v1",
        "hyprial.orphan-processes/v1",
    ),
    (
        "apps/gui/install.json",
        "h2b.install-state/v1",
        "hyprial.install-state/v1",
    ),
)

#: Values a row's ``schema`` may legitimately already hold that are *newer* than
#: this rename's target -- so they are skipped, not refused.  ⚠️ Explicitly
#: enumerated per file, NOT matched by family prefix (fable R4): a
#: ``startswith(new_family)`` test skipped **any** suffix in the family (e.g.
#: ``hyprial.install-state/v99-garbage``) and did so for every rewritten key,
#: quietly widening the "a third value must be refused" invariant into a
#: whole-family pass.  The only real newer value is the release install receipt
#: (design §3 touch-point: an app upgraded to a v2 install receipt), so that one
#: exact value is the only thing listed here.
_ALREADY_MIGRATED_AHEAD: dict[str, frozenset[str]] = {
    "apps/gui/install.json": frozenset({"hyprial.install-state/v2"}),
}


def _rewrite_schema_names(home: Path) -> tuple[str, ...]:
    """Point every table row at its new ``schema`` value; return what changed.

    Runs *before* the rename, which is what makes it re-entrant without a
    second call site: a failure here leaves the rename undone, so the next
    ``migrate_legacy_default_home`` passes the same existence checks and
    resumes. Placing it after the rename would instead need a retry hung off
    the ``destination-exists`` early return -- a branch that executes on every
    later start, forever.

    Each row is read as JSON and rewritten through a temporary file in the same
    directory, so a crash mid-write cannot leave a truncated state file. A row
    whose file is absent, or already carries the new value, is skipped: that is
    the property the re-entrancy argument rests on.
    """

    rewritten: list[str] = []
    for relative, old_value, new_value in _SCHEMA_REWRITES:
        path = home / relative
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, json.JSONDecodeError) as error:
            # An unreadable row used to escape as a bare JSONDecodeError: no
            # code, no data, and -- the part that matters -- no file name. By
            # this function's own principle an unnamed failure is the sibling of
            # a silent pass: both leave the next reader unable to act.
            raise _failure(
                "schema-rewrite-unreadable",
                f"cannot read {relative} while rewriting its schema: {error}",
                source=str(home),
                path=relative,
            ) from error
        if not isinstance(raw, dict):
            raise _failure(
                "schema-rewrite-refused",
                f"{relative} is not a JSON object; refusing to rewrite its schema",
                source=str(home),
                path=relative,
            )
        current = raw.get("schema")
        if current == new_value:
            # Already migrated. Skipping here rather than failing is what makes a
            # resumed run finish the job instead of rejecting its own earlier work.
            continue
        # Already hyprial-native and legitimately *newer* than this rename's
        # target -- e.g. an app upgraded to a v2 install receipt (design §3
        # touch-point).  The legacy project rename only ever produced the v1
        # name, so there is nothing to rename; refusing it would block startup
        # on a node that legitimately moved an app to the release path.  ⚠️ Only
        # the exact values listed for this file are skipped (fable R4); any other
        # third value falls through to the loud refusal below.
        if isinstance(current, str) and current in _ALREADY_MIGRATED_AHEAD.get(relative, frozenset()):
            continue
        if current != old_value:
            # A third value is neither ours to migrate nor ours to ignore: some
            # other writer owns this key now. Refusing is the only branch that
            # stays visible -- an `else: skip` would pass it over in silence, and
            # in a rewriting tool a silent pass is as invisible as a bad write.
            raise _failure(
                "schema-rewrite-refused",
                f"{relative} carries an unexpected schema {current!r}; "
                f"expected {old_value!r} or {new_value!r}",
                source=str(home),
                path=relative,
                found=current if isinstance(current, str) else None,
            )
        raw["schema"] = new_value
        temporary = path.parent / f".{path.name}.rewrite-{os.getpid()}-{uuid.uuid4().hex}"
        try:
            temporary.write_text(
                json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            os.replace(temporary, path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise
        rewritten.append(relative)
    return tuple(rewritten)
_SIDECAR_BIN_NAME = "hyprial-tsnet"


class LegacyHomeMigrationError(RuntimeError):
    """The legacy home could not be moved while its state was safe."""

    code = ipc_errors.HYPRIAL_HOME_MIGRATION_FAILED

    def __init__(self, message: str, data: dict[str, Any]) -> None:
        self.data = data
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class LegacyHomeMigrationResult:
    """Observable outcome of one migration check."""

    migrated: bool
    reason: str
    source: Path
    destination: Path

    def to_json(self) -> dict[str, Any]:
        return {
            "migrated": self.migrated,
            "reason": self.reason,
            "source": str(self.source),
            "destination": str(self.destination),
        }


def _failure(reason: str, message: str, **data: Any) -> LegacyHomeMigrationError:
    return LegacyHomeMigrationError(message, {"reason": reason, **data})


def _migrate_sidecar_binary_name(destination: Path) -> None:
    """Finish the sidecar rename inside an already-migrated default home.

    Three call sites, two policies.  The one-time migrating path calls it
    right after the directory rename and stays loud: the home moved and the
    caller must learn the step did not complete
    (``sidecar-bin-rename-failed``).  Both ``destination-exists`` returns
    call it best-effort with ``OSError`` swallowed: they are retries of that
    same unfinished step, and raising on a retry would turn a persistent
    cause into a permanent daemon-start failure (see the branch comments).
    Idempotent by construction: an absent legacy name is a no-op, and when
    both names exist the current name wins and the legacy file is removed.
    """

    bin_directory = destination / "bin"
    legacy = bin_directory / _LEGACY_SIDECAR_BIN_NAME
    current = bin_directory / _SIDECAR_BIN_NAME
    if not os.path.lexists(legacy):
        return
    if os.path.lexists(current):
        # The current product name wins explicitly; never leave both names behind.
        legacy.unlink()
        return
    os.rename(legacy, current)


@contextmanager
def _legacy_daemon_stopped(source: Path) -> Iterator[None]:
    """Hold the legacy daemon lock while its home is renamed."""

    state = source / "state"
    lock_path = state / "daemon.lock"
    if not lock_path.exists():
        databases = [
            str(state / name)
            for name in _STATE_DATABASES
            if (state / name).exists()
        ]
        if databases:
            raise _failure(
                "daemon-state-unfenced",
                "legacy state databases exist but daemon.lock is absent; "
                "cannot prove the daemon is stopped",
                paths=databases,
            )
        yield
        return

    try:
        stream = lock_path.open("r+b")
    except OSError as error:
        raise _failure(
            "daemon-lock-unreadable",
            f"cannot open legacy daemon lock {lock_path}: {error}",
            path=str(lock_path),
        ) from error
    try:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise _failure(
                "daemon-running",
                "legacy HYPRIAL daemon still owns its state; stop it before migration",
                path=str(lock_path),
            ) from error

        from hyprial.daemon.home_guard import live_daemon_pid

        pid = live_daemon_pid(source)
        if pid is not None:
            raise _failure(
                "daemon-running",
                f"legacy HYPRIAL daemon is still running (pid {pid}); stop it before migration",
                path=str(source),
                pid=pid,
            )
        yield
    finally:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


def migrate_legacy_default_home(
    environ: Mapping[str, str] | None = None,
) -> LegacyHomeMigrationResult:
    """Rename the implicit legacy home once; explicit homes are never migrated."""

    env = os.environ if environ is None else environ
    parent = Path.home().resolve()
    source = parent / ".h2b"
    destination = parent / ".hyprial"
    if "HYPRIAL_HOME" in env:
        return LegacyHomeMigrationResult(False, "explicit-home", source, destination)
    if os.path.lexists(destination):
        # Best-effort retry of the sidecar binary rename that the one-time
        # migrating path may have left unfinished (its loud failure is
        # ``sidecar-bin-rename-failed``: home moved, binary did not).  This
        # branch is a RETRY, not the migration, so ``OSError`` is swallowed
        # deliberately: a persistent cause (a root-owned bin/, a read-only
        # mount) must not become a permanent daemon-start failure on every
        # already-migrated machine, where the residue is one orphan file on
        # a working machine.  The daemon fence is deliberately NOT taken:
        # it flocks the SOURCE home's state, while this call touches only
        # destination/bin -- a machine with a still-installed legacy
        # distribution (source present, a live legacy daemon or bare state
        # databases) would be refused for an operation that never reads
        # source.  Accepted cost: one extra stat() of the legacy name per
        # start; on a hung mount that stat can block, which try/except
        # cannot catch -- bounding it is out of scope by design.
        try:
            _migrate_sidecar_binary_name(destination)
        except OSError:
            pass
        return LegacyHomeMigrationResult(
            False, "destination-exists", source, destination
        )
    if not os.path.lexists(source):
        return LegacyHomeMigrationResult(False, "source-absent", source, destination)

    with _legacy_daemon_stopped(source):
        if os.path.lexists(destination):
            # The TOCTOU re-check mirrors the outer destination-exists
            # return's best-effort retry.  Defensive only: reaching here
            # needs destination absent at the outer check and present at
            # this one, which is a concurrent creation, not the retry path
            # the outer call serves.
            try:
                _migrate_sidecar_binary_name(destination)
            except OSError:
                pass
            return LegacyHomeMigrationResult(
                False, "destination-exists", source, destination
            )
        if not os.path.lexists(source):
            return LegacyHomeMigrationResult(False, "source-absent", source, destination)
        try:
            _rewrite_schema_names(source)
        except OSError as error:
            raise _failure(
                "schema-rewrite-failed",
                f"cannot rewrite a stale schema name in the legacy home: {error}",
                source=str(source),
                destination=str(destination),
            ) from error
        try:
            os.rename(source, destination)
        except OSError as error:
            raise _failure(
                "rename-failed",
                f"cannot rename legacy HYPRIAL home: {error}",
                source=str(source),
                destination=str(destination),
            ) from error
        try:
            _migrate_sidecar_binary_name(destination)
        except OSError as error:
            # The home is already migrated at this point; saying "rename-failed"
            # here would report a failure that did not happen.
            raise _failure(
                "sidecar-bin-rename-failed",
                "legacy HYPRIAL home migrated, but the sidecar binary rename "
                f"failed: {error}",
                source=str(source),
                destination=str(destination),
            ) from error

    return LegacyHomeMigrationResult(True, "moved", source, destination)
