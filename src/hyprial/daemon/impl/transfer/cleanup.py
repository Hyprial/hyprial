"""AT11: transfer-out completion — fence, clean up, and report honestly.

Card 360 (AT11) asks for the *leave* half of a transfer: once the target has
confirmed it holds the agent, the source must clean up its own copy -- agent
home, private grants, container credential volumes and staging -- by identity
fence, with a receipt that lists what went and what did not.

Two things this module deliberately refuses to be:

* **It is not a shredder.**  Removing files does not erase historical bytes
  from SSD snapshots or backups, and the receipt says so.
* **It is not a "target said ok" handshake.**  ``execute_cleanup`` needs the
  caller to present the target's confirmation receipt *and* a bundle that still
  validates, so a source can never complete a transfer that did not land.
* **It does not delete a home it cannot recognise.**  The bundle must name the
  same actor and owner as the plan, and every file the bundle records must still
  be present in the home with the same digest.  A bundle taken from a different
  home is refused rather than treated as cover for deleting this one.  A home
  that only partially matches (a previous removal that failed halfway) is
  refused too: the operator decides how to finish, the tool does not guess.
* **Files the bundle does not hold are not deleted by accident.**  A file the
  manifest never recorded (written after the export, or a bundle that recorded
  nothing at all) makes the removal refuse with ``cleanup_uncovered_files``
  and name the files.  ``allow_uncovered`` -- the CLI's ``--allow-uncovered``
  -- is the explicit "yes, this home moved on; delete them anyway", and a
  bundle with no entries is refused even with it: an empty bundle is evidence
  of nothing and may never stand in for a copy of a home.  A dry run lists the
  same files and removes nothing.

It never touches the P0 inbox: a path whose components contain ``inbox`` is
refused, not silently skipped.
"""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from hyprial.daemon.impl.transfer.archive.bundle import (
    BundleError,
    BundleManifest,
    _iter_payload_files,
    sha256_file,
    validate_bundle)

__all__ = [
    "CleanupError",
    "CleanupFenceError",
    "CleanupNotConfirmed",
    "CleanupPlan",
    "CleanupItem",
    "CleanupReceipt",
    "CleanupUncoveredFiles",
    "plan_cleanup",
    "execute_cleanup",
]

#: Receipt line that must travel with every result: deletion is not erasure.
NOT_A_SHREDDER = (
    "removing these paths does not erase historical bytes from SSD snapshots "
    "or backups"
)


class CleanupError(Exception):
    """Base class for transfer-out cleanup failures."""

    code = "cleanup_error"


class CleanupFenceError(CleanupError):
    """A path is outside the identity fence, or is too broad to delete."""

    code = "cleanup_fence_refused"


class CleanupNotConfirmed(CleanupError):
    """The target has not confirmed, or there is no valid bundle to fall back on."""

    code = "cleanup_not_confirmed"


class CleanupUncoveredFiles(CleanupError):
    """The home holds files the bundle never recorded.

    Deleting them would destroy the only copy: "listed in the receipt" is not
    the same as "safe to lose".
    """

    code = "cleanup_uncovered_files"


@dataclass(frozen=True)
class CleanupItem:
    """One path this transfer-out would remove."""

    path: Path
    kind: str
    exists: bool
    size_bytes: int = 0
    note: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "kind": self.kind,
            "exists": self.exists,
            "size_bytes": self.size_bytes,
            "note": self.note,
        }


@dataclass(frozen=True)
class CleanupPlan:
    """What a transfer-out would clean up, before anything is removed."""

    actor: str
    owner: str
    source_home: Path
    items: tuple[CleanupItem, ...]
    roots: tuple[Path, ...]

    @property
    def byte_total(self) -> int:
        return sum(item.size_bytes for item in self.items if item.exists)

    def as_dict(self) -> dict[str, object]:
        return {
            "actor": self.actor,
            "owner": self.owner,
            "source_home": str(self.source_home),
            "roots": [str(root) for root in self.roots],
            "items": [item.as_dict() for item in self.items],
            "byte_total": self.byte_total,
            "erasure": NOT_A_SHREDDER,
        }


@dataclass(frozen=True)
class CleanupReceipt:
    """What a transfer-out actually did."""

    actor: str
    owner: str
    receipt: str
    dry_run: bool
    removed: tuple[str, ...]
    leftover: tuple[str, ...]
    failed: tuple[tuple[str, str], ...]
    uncovered: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.leftover and not self.failed

    def as_dict(self) -> dict[str, object]:
        return {
            "actor": self.actor,
            "owner": self.owner,
            "receipt": self.receipt,
            "dry_run": self.dry_run,
            "removed": list(self.removed),
            "leftover": list(self.leftover),
            "failed": [
                {"path": path, "reason": reason} for path, reason in self.failed
            ],
            "complete": self.complete,
            "uncovered": list(self.uncovered),
            "erasure": NOT_A_SHREDDER,
        }


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _folded(path: Path) -> str:
    """Case-folded form for the string fallbacks.

    APFS and NTFS are case-insensitive but case-preserving, so two spellings
    can name ONE directory.  ``realpath`` does not normalise case, which is how
    ``--staging <tmp>/FH`` used to pass the fence and then delete ``<tmp>/fh``.
    """

    return str(path).casefold()


def _identity(path: Path) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` of the object this path names, or None if absent."""

    try:
        info = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as error:
        raise CleanupFenceError(f"cannot verify identity of {path}: {error}") from error
    return (info.st_dev, info.st_ino)


def _same_object(a: Path, b: Path) -> bool:
    """True when both names reach the same directory/file on THIS filesystem.

    Identity first (that is what catches case variants and symlinks); a
    case-folded string compare is the fallback when a path does not exist yet,
    where deleting it cannot destroy anything.
    """

    left, right = _identity(a), _identity(b)
    if left is not None and right is not None:
        return left == right
    return _folded(a) == _folded(b)


def _ancestors(path: Path) -> list[Path]:
    chain = [path]
    current = path
    while True:
        parent = current.parent
        if parent == current or str(parent) == "":
            break
        chain.append(parent)
        current = parent
    return chain


def _contains(root: Path, candidate: Path) -> bool:
    """True when ``candidate`` IS ``root`` or lives under it, by identity."""

    for ancestor in _ancestors(candidate):
        if _same_object(root, ancestor):
            return True
    return False


def _actor_owned_roots(actor: str) -> tuple[Path, ...]:
    """The selected state's staging root for this actor alone.

    ``hyprial.transfer.container.staging_dir`` puts credential bundles (and the
    image tar) under ``<state dir>/xfer-cred/<actor digest>``. Anything declared
    as ``--staging`` / ``--credential-volume`` / ``--private-grant`` outside
    these roots is not provably this actor's, so it needs ``--allow-external``.
    """

    from hyprial.kernel import configured_hyprial_home
    from hyprial.daemon.impl.transfer.execution.container import staging_dir

    # Match the CLI's _state_dir precedence, including explicit state outside
    # HYPRIAL_HOME. Other configured/default homes are not fallback authority.
    state = os.environ.get("HARNESS_STATE_DIR")
    if state:
        state_root = _canonical(state)
    else:
        home, _source = configured_hyprial_home()
        state_root = home / "state"
    # Keep the actor component unresolved so a symlink at that boundary cannot
    # confer this actor's ownership on a different actor's directory.
    return (staging_dir(_canonical(state_root), actor),)


def _directory_size(path: Path) -> int:
    if path.is_file() and not path.is_symlink():
        return path.stat().st_size
    if not path.is_dir():
        return 0
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            candidate = Path(root) / name
            try:
                if not candidate.is_symlink():
                    total += candidate.stat().st_size
            except OSError:
                continue
    return total


def _has_inbox_component(path: Path) -> bool:
    # Case-insensitive: ``INBOX`` names the same retained directory on APFS.
    return any(part.casefold() == "inbox" for part in path.parts)


def _canonical(target: os.PathLike[str] | str) -> Path:
    """Absolute, ``..``-free, symlink-resolved form used by the fence."""

    return Path(os.path.realpath(os.path.expanduser(str(target))))


def _fence_path(path: Path, *, kind: str, home: Path) -> None:
    """Refuse a path that is too broad to delete, whatever its label.

    Declaring a path as ``--staging`` (or as a credential volume or a private
    grant) does not make it safe: the operator's own home, the filesystem root
    and any ancestor of the agent home are still off limits.  A typo in one of
    those arguments must not turn into ``rmtree`` on a tree nobody exported.
    """

    target = _canonical(path)
    home_target = _canonical(home)
    operator_home = _canonical(Path.home())

    if _folded(target) == _folded(Path(target.anchor)):
        raise CleanupFenceError(
            f"refusing to clean up {path} ({kind}): that is the filesystem root"
        )
    # Identity, not spelling: on a case-insensitive filesystem ``<tmp>/FH`` and
    # ``<tmp>/fh`` are the same directory, and only stat(2) can say so.
    if _same_object(target, operator_home) or _contains(target, operator_home):
        raise CleanupFenceError(
            f"refusing to clean up {path} ({kind}): it is, or contains, the "
            "operator's home"
        )
    if not _same_object(target, home_target) and _contains(target, home_target):
        raise CleanupFenceError(
            f"refusing to clean up {path} ({kind}): it contains the agent home "
            f"{home} and everything else under it"
        )


def plan_cleanup(
    source_home: os.PathLike[str] | str,
    *,
    actor: str,
    owner: str,
    staging: Iterable[os.PathLike[str] | str] = (),
    credential_volumes: Iterable[os.PathLike[str] | str] = (),
    private_grants: Iterable[os.PathLike[str] | str] = (),
    allow_external: bool = False,
) -> CleanupPlan:
    """Build the fence-checked cleanup plan for one transfer-out.

    Every declared path is vetted before it becomes an item: it must sit inside
    the source home or be exactly one of the explicitly declared external roots,
    it must not be a symlink, it must not be the operator's own home or a broad
    ancestor of it, and no component may be ``inbox`` (P0 retention).

    An "external root" is only accepted when it is provably this actor's, i.e.
    it lives under the actor's root from ``container.staging_dir(state, actor)``.
    ``--staging ~/Documents``, a symlink to it, or another agent's home are not
    this actor's, so they are refused unless the operator passes
    ``allow_external`` -- and then the receipt says so.
    """

    home = Path(source_home).expanduser().absolute()
    external: list[tuple[Path, str]] = []
    for values, kind in (
        (staging, "staging"),
        (credential_volumes, "credential-volume"),
        (private_grants, "private-grant"),
    ):
        for raw in values:
            external.append((Path(raw).expanduser().absolute(), kind))

    roots = (home, *(path for path, _kind in external))

    # The same fence for the agent home and for every declared external root:
    # "declared" is not a licence to delete the operator's home, the filesystem
    # root, or a directory that merely contains other agents.
    _fence_path(home, kind="agent-home", home=home)

    actor_roots = _actor_owned_roots(actor)
    foreign: list[tuple[Path, str]] = []
    for path, kind in external:
        _fence_path(path, kind=kind, home=home)
        # A declared "external root" that IS the agent home is not external at
        # all -- and on a case-insensitive filesystem ``<tmp>/FH`` is the home
        # ``<tmp>/fh``, which is how the reviewer's repro deleted keep.txt.
        # ``--allow-external`` must not be able to release this one.
        if _same_object(_canonical(path), _canonical(home)):
            raise CleanupFenceError(
                f"refusing to clean up {path} ({kind}): that is the agent home "
                "itself, not an external root"
            )
        if any(
            not root.is_symlink() and _contains(_canonical(root), _canonical(path))
            for root in actor_roots
        ):
            continue
        foreign.append((path, kind))
    if foreign and not allow_external:
        listed = "; ".join(str(path) for path, _kind in foreign)
        raise CleanupFenceError(
            "refusing to clean up paths that are not this actor's: "
            f"{listed}. Declaring --staging/--credential-volume/--private-grant "
            "does not make a path safe; only "
            f"{', '.join(str(root) for root in actor_roots)} is created for this "
            "actor. Pass --allow-external to delete them anyway."
        )
    external_notes = {
        path: ("declared external root, released by --allow-external")
        for path, _kind in foreign
    }

    items: list[CleanupItem] = []
    seen: set[Path] = set()

    def add(path: Path, kind: str, note: str = "") -> None:
        if path in seen:
            return
        seen.add(path)
        if _has_inbox_component(path):
            raise CleanupFenceError(
                f"refusing to clean up {path}: P0 inbox rows are retained"
            )
        if path.is_symlink():
            raise CleanupFenceError(f"refusing to clean up symlink {path}")
        if not (_within(path, home) or path in external_declared):
            raise CleanupFenceError(
                f"refusing to clean up {path}: outside the identity fence"
            )
        items.append(
            CleanupItem(
                path=path,
                kind=kind,
                exists=path.exists(),
                size_bytes=_directory_size(path) if path.exists() else 0,
                note=note,
            )
        )

    external_declared = {path for path, _kind in external}

    add(home, "agent-home")
    for path, kind in external:
        add(path, kind, external_notes.get(path, ""))

    return CleanupPlan(
        actor=actor,
        owner=owner,
        source_home=home,
        items=tuple(items),
        roots=roots,
    )


def _home_binding(home: Path, manifest: BundleManifest) -> tuple[str, ...]:
    """Prove the bundle is a copy of ``home``; return files it does not cover.

    Every entry the manifest records must still be in the home with the same
    size and digest -- that is what ties *this* bundle to *this* home.  Files in
    the home that the bundle never recorded are returned so the receipt can say
    what the fallback copy does not hold.
    """

    for entry in manifest.entries:
        candidate = home / entry.path
        if candidate.is_symlink() or not candidate.is_file():
            raise CleanupNotConfirmed(
                f"refusing to clean up {home}: the bundle records {entry.path!r} "
                "but this home does not have it, so the bundle was not taken "
                "from here"
            )
        if (
            candidate.stat().st_size != entry.size
            or sha256_file(candidate) != entry.sha256
        ):
            raise CleanupNotConfirmed(
                f"refusing to clean up {home}: {entry.path!r} no longer matches "
                "the bundle manifest"
            )

    registered = {entry.path for entry in manifest.entries}
    return tuple(
        sorted(
            relative
            for relative, absolute in _iter_payload_files(home)
            if relative not in registered and not absolute.is_symlink()
        )
    )


def _remove(path: Path) -> None:
    info = path.lstat()
    if stat.S_ISDIR(info.st_mode) and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def execute_cleanup(
    plan: CleanupPlan,
    *,
    target_receipt: str,
    bundle_dir: os.PathLike[str] | str,
    dry_run: bool = False,
    allow_uncovered: bool = False,
) -> CleanupReceipt:
    """Remove the planned paths, but only after the target has confirmed.

    ``target_receipt`` is the target side's confirmation id.  ``bundle_dir`` is
    mandatory: the fallback copy has to exist *and* have been taken from this
    home before the source copy is removed.  ``allow_uncovered`` is the
    explicit permission to delete files the bundle does not hold; without it
    they stop the removal.  Failures are collected, not raised: the receipt
    lists what survived so the caller can retry.
    """

    if not target_receipt or not target_receipt.strip():
        raise CleanupNotConfirmed(
            "a transfer-out is only complete once the target confirms; "
            "no confirmation receipt was supplied"
        )
    try:
        manifest = validate_bundle(bundle_dir)
    except BundleError as error:
        raise CleanupNotConfirmed(
            f"refusing to clean up without a valid fallback bundle: {error}"
        ) from error

    if manifest.actor != plan.actor or manifest.owner != plan.owner:
        raise CleanupNotConfirmed(
            "refusing to clean up: the bundle belongs to "
            f"{manifest.owner}/{manifest.actor}, but this plan is "
            f"{plan.owner}/{plan.actor}"
        )

    uncovered = _home_binding(plan.source_home, manifest)
    if not manifest.entries:
        raise CleanupUncoveredFiles(
            f"refusing to clean up {plan.source_home}: the bundle records no "
            "files at all, so it cannot be evidence that this home was copied "
            "anywhere (an empty bundle is refused even with allow_uncovered)"
        )
    if uncovered and not allow_uncovered and not dry_run:
        raise CleanupUncoveredFiles(
            f"refusing to clean up {plan.source_home}: {len(uncovered)} file(s) "
            "in this home are not in the bundle, so removing the home would "
            f"delete the only copy: {list(uncovered)}; pass allow_uncovered to "
            "delete them anyway (a dry run lists them without removing)"
        )

    removed: list[str] = []
    leftover: list[str] = []
    failed: list[tuple[str, str]] = []

    for item in sorted(plan.items, key=lambda entry: len(entry.path.parts), reverse=True):
        if not item.path.exists() and not item.path.is_symlink():
            continue
        if dry_run:
            leftover.append(str(item.path))
            continue
        try:
            _remove(item.path)
        except OSError as error:
            failed.append((str(item.path), str(error)))
            leftover.append(str(item.path))
        else:
            removed.append(str(item.path))

    return CleanupReceipt(
        actor=plan.actor,
        owner=plan.owner,
        receipt=target_receipt,
        dry_run=dry_run,
        removed=tuple(removed),
        leftover=tuple(leftover),
        failed=tuple(failed),
        uncovered=uncovered,
    )


def cleanup_summary(plan: CleanupPlan) -> Mapping[str, object]:
    """Small helper for operator-facing callers (CLI, runbooks)."""

    return {
        "source_home": str(plan.source_home),
        "items": len(plan.items),
        "byte_total": plan.byte_total,
        "erasure": NOT_A_SHREDDER,
    }
