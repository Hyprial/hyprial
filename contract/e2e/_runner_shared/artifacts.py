"""Disposable E2E filesystem roots and failure-preserving artifacts.

The failure artifact boundary is an allowlist, not a credential-name denylist.
Both entries are daemon-owned diagnostics used directly by E2E assertions:

- Logger.route_path selects state/logs/daemon.jsonl and Logger.redact sanitizes
  every structured field (src/hyprial/kernel/impl/log/__init__.py:105-157).
- daemon startup redirects its output to state/daemon-launch.log
  (src/hyprial/shell/impl/cli/commands/common/daemon_start.py:375,414-460).

The paths below are exact and closed. Root-level daemons are used by
run-daemon-restart-survival.py:23-24 and run-first-run-no-agent.py. Nested
daemon roots are limited to the layouts used by
scenario_tempdir/preserve_failure_state callers: a/b
(run-orgfs-forwarding-redial.py:509-517), d (_group_conversation/phases.py),
worker/sender (run-cc-tui-worker.py:707-721),
and case-1..4/a,b (run-peer-restart-relearn.py:634-641,896-905). Adding a new
nested daemon layout requires adding its exact root here.

Scope: this boundary decides WHICH files are copied, not WHAT they contain.
Content safety rests on the producers: daemon.jsonl relies on Logger.redact,
while daemon-launch.log is the daemon's raw redirected output and is not
redacted by that logger. Passing this allowlist does not prove a file is free
of secrets; any change that lets credentials reach either log must be fixed
at its source.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

_DIAGNOSTIC_FILE_SUFFIXES = (
    ("state", "daemon-launch.log"),
    ("state", "logs", "daemon.jsonl"),
)
_DAEMON_ARTIFACT_ROOTS = frozenset(
    {
        (),
        ("a",),
        ("b",),
        ("d",),
        ("sender",),
        ("worker",),
        ("case-1", "a"),
        ("case-1", "b"),
        ("case-2", "a"),
        ("case-2", "b"),
        ("case-3", "a"),
        ("case-3", "b"),
        ("case-4", "a"),
        ("case-4", "b"),
    }
)
_DIAGNOSTIC_ALLOWLIST = frozenset(
    root + suffix
    for root in _DAEMON_ARTIFACT_ROOTS
    for suffix in _DIAGNOSTIC_FILE_SUFFIXES
)
_NOT_ALLOWLISTED_REASON = "not in diagnostic artifact allowlist"


def artifacts_root() -> Path:
    """Where scenario failure evidence is preserved for CI collection.

    The runner host persists /tmp across runs; the e2e workflow sets
    HYPRIAL_E2E_ROOT per run, so artifacts land in a run-scoped directory and
    the always() collection step lists them into the job log.  (This
    Forgejo instance mirrors no upload-artifact action -- `uses:` is allowed
    only with a pinned ref, tests/support/ci/ci_workflow_policy.py -- so host-persistent
    run-scoped directories are the artifact store.)
    """

    configured = os.environ.get("HYPRIAL_E2E_ARTIFACTS") or (
        os.environ.get("HYPRIAL_E2E_ROOT") + "/artifacts"
        if os.environ.get("HYPRIAL_E2E_ROOT")
        else None
    )
    return Path(configured) if configured else Path.cwd() / "e2e-artifacts"


def _is_allowlisted_diagnostic(relative: Path) -> bool:
    return relative.parts in _DIAGNOSTIC_ALLOWLIST


def _copy_regular_file(
    source: Path,
    destination: Path,
    expected: os.stat_result,
) -> None:
    """Copy one already-classified file without following a replaced symlink."""

    source_fd = -1
    destination_fd = -1
    try:
        source_fd = os.open(
            source,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(source_fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino)
        ):
            raise OSError("artifact source changed during classification")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(source_fd, "rb") as source_stream:
            source_fd = -1
            with os.fdopen(destination_fd, "wb") as destination_stream:
                destination_fd = -1
                shutil.copyfileobj(source_stream, destination_stream)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        if destination_fd >= 0:
            os.close(destination_fd)


def _copy_allowlisted_diagnostics(
    root: Path,
    destination: Path,
    skipped: list[tuple[str, str]],
) -> None:
    """Walk without links and copy only the two documented diagnostic paths."""

    directories: set[Path] = set()
    retained_directories: set[Path] = set()

    def on_walk_error(error: OSError) -> None:
        candidate = Path(error.filename) if error.filename else root
        try:
            relative = candidate.relative_to(root).as_posix()
        except ValueError:
            relative = os.path.relpath(candidate, root)
        skipped.append((relative, f"unclassifiable: {type(error).__name__}"))

    for directory, dirnames, filenames in os.walk(
        root,
        topdown=True,
        onerror=on_walk_error,
        followlinks=False,
    ):
        current = Path(directory)
        for name in tuple(dirnames):
            candidate = current / name
            try:
                relative = candidate.relative_to(root)
                mode = candidate.lstat().st_mode
            except (OSError, ValueError) as error:
                skipped.append(
                    (
                        os.path.relpath(candidate, root),
                        f"unclassifiable: {type(error).__name__}",
                    )
                )
                dirnames.remove(name)
                continue
            if stat.S_ISLNK(mode):
                skipped.append((relative.as_posix(), "unsafe symlink"))
                dirnames.remove(name)
            elif not stat.S_ISDIR(mode):
                skipped.append((relative.as_posix(), "unsafe non-directory path"))
                dirnames.remove(name)
            else:
                directories.add(relative)

        for name in filenames:
            candidate = current / name
            try:
                relative = candidate.relative_to(root)
                metadata = candidate.lstat()
            except (OSError, ValueError) as error:
                skipped.append(
                    (
                        os.path.relpath(candidate, root),
                        f"unclassifiable: {type(error).__name__}",
                    )
                )
                continue
            if stat.S_ISLNK(metadata.st_mode):
                skipped.append((relative.as_posix(), "unsafe symlink"))
                continue
            if not stat.S_ISREG(metadata.st_mode):
                skipped.append((relative.as_posix(), "unsafe non-regular path"))
                continue
            if metadata.st_nlink != 1:
                skipped.append((relative.as_posix(), "unsafe multiply-linked file"))
                continue
            if not _is_allowlisted_diagnostic(relative):
                skipped.append((relative.as_posix(), _NOT_ALLOWLISTED_REASON))
                continue
            try:
                _copy_regular_file(candidate, destination / relative, metadata)
            except OSError as error:
                skipped.append(
                    (relative.as_posix(), f"unclassifiable: {type(error).__name__}")
                )
                continue
            retained_directories.update(
                parent for parent in relative.parents if parent != Path(".")
            )

    for relative in sorted(directories - retained_directories):
        skipped.append((relative.as_posix(), _NOT_ALLOWLISTED_REASON))


def _report_skipped_paths(skipped: list[tuple[str, str]]) -> None:
    if not skipped:
        return
    print(f"failure state preservation skipped {len(skipped)} path(s):")
    for relative, reason in skipped:
        print(f"  - {relative} ({reason})")


def preserve_failure_state(root: Path, scenario: str) -> Path | None:
    """Copy safe post-mortem evidence from a failed scenario.

    TemporaryDirectory cleanup deleting daemon.jsonl/daemon-launch.log made the
    E2E-002 gate flake undiagnosable (CI run 857). Only documented exact
    diagnostic paths are copied; every other path fails closed without
    inspecting its contents. Every skipped path is named in stdout. Returns the
    destination, or None when preservation itself failed; preservation errors
    never mask the original scenario failure.
    """

    root = Path(root)
    skipped: list[tuple[str, str]] = []
    try:
        root_mode = root.lstat().st_mode
        if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
            print("WARNING: could not preserve failure state: unsafe source root")
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        destination = artifacts_root() / f"{scenario}-{stamp}-{uuid4().hex[:8]}"
        resolved_root = root.resolve(strict=True)
        resolved_destination = destination.resolve(strict=False)
        if (
            resolved_destination == resolved_root
            or resolved_root in resolved_destination.parents
        ):
            raise OSError("artifact destination must be outside the source root")
        destination.mkdir(parents=True, mode=0o700)
        _copy_allowlisted_diagnostics(root, destination, skipped)
        _report_skipped_paths(skipped)
        print(f"failure state preserved at {destination}")
        return destination
    except Exception as error:  # noqa: BLE001 - never mask the scenario failure
        try:
            _report_skipped_paths(skipped)
            print(f"WARNING: could not preserve failure state: {error}")
        except Exception:  # noqa: BLE001 - reporting is also best-effort
            pass
        return None


@contextmanager
def scenario_tempdir(
    scenario: str,
    *,
    directory: Path | None = None,
) -> Iterator[Path]:
    """TemporaryDirectory that preserves its contents on scenario failure."""

    with tempfile.TemporaryDirectory(
        prefix=f"hyprial-{scenario}-",
        dir=directory,
    ) as temporary:
        try:
            yield Path(temporary)
        except BaseException:
            try:
                preserve_failure_state(Path(temporary), scenario)
            except BaseException:
                # Preservation is best-effort even if an unexpected reporting
                # failure escapes its own guard. Never replace the scenario error.
                pass
            raise


def short_temp_root() -> Path:
    """Return a writable short temp root suitable for Unix daemon sockets."""

    configured = os.environ.get("HYPRIAL_TEST_TMP_ROOT")
    if configured:
        # CI points this at the /ci-tmpfs RAM disk: daemons started on the
        # shared btrfs stall on its commits during startup (infra-ops,
        # 2026-09-28).  Locally it is unset and the lookup below is unchanged.
        return Path(configured).resolve()
    for candidate in (Path("/private/tmp"), Path("/tmp")):
        if candidate.is_dir() and os.access(candidate, os.W_OK | os.X_OK):
            return candidate
    return Path(tempfile.gettempdir())
