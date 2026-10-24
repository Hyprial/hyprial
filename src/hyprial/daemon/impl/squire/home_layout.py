"""Squire's role files inside the ordinary, registry-owned agent home."""

from __future__ import annotations

import os
import stat
from hashlib import sha256
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SquireHomeLayout:
    home: Path

    @classmethod
    def within(cls, hyprial_home: Path) -> SquireHomeLayout:
        root = Path(hyprial_home).resolve()
        home = root / "agents" / "squire"
        if not home.resolve(strict=False).is_relative_to(root):
            raise ValueError("squire agent home escapes HYPRIAL_HOME")
        return cls(home)

    @property
    def workspace(self) -> Path:
        return self.home / "workspace"

    @property
    def config(self) -> Path:
        return self.home / "config"

    @property
    def skill_dir(self) -> Path:
        return self.config / "shared" / "skills" / "squire"

    def legacy_targets(self) -> tuple[tuple[Path, Path], ...]:
        return (
            (Path("skills/squire/SKILL.md"), self.skill_dir / "SKILL.md"),
            (Path("profile.md"), self.workspace / "profile.md"),
            (Path("backlog.md"), self.workspace / "backlog.md"),
        )

    def validate_write_targets(self) -> None:
        root = self.home.parent.parent
        for _, target in self.legacy_targets():
            current = target
            while current != root:
                if current.is_symlink():
                    raise ValueError(f"squire agent home contains a symlink: {current}")
                current = current.parent
            if not target.resolve(strict=False).is_relative_to(root.resolve()):
                raise ValueError(f"squire agent home escapes HYPRIAL_HOME: {target}")


def plan_legacy_copy(
    legacy_home: Path, layout: SquireHomeLayout
) -> tuple[tuple[Path, bytes], ...]:
    """Read legacy files and reject every conflict before any target mutation."""

    legacy = Path(legacy_home).expanduser()
    if legacy.resolve(strict=False) == layout.home.resolve(strict=False):
        return ()
    if legacy.is_symlink():
        raise ValueError(f"legacy squire home is a symlink: {legacy}")
    if not legacy.exists():
        return ()
    if not legacy.is_dir():
        raise ValueError(f"legacy squire home is not a directory: {legacy}")
    planned: list[tuple[Path, bytes]] = []
    for relative, target in layout.legacy_targets():
        source = legacy / relative
        current = source
        while current != legacy:
            if current.is_symlink():
                raise ValueError(f"legacy squire file is a symlink: {current}")
            current = current.parent
        if not source.exists():
            continue
        if not stat.S_ISREG(source.stat().st_mode):
            raise ValueError(f"legacy squire file is not regular: {source}")
        data = source.read_bytes()
        if relative == Path("skills/squire/SKILL.md"):
            data = _relocate_legacy_skill_paths(data, legacy)
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ValueError(f"legacy squire file conflicts with target: {target}")
        if target.is_file():
            if target.read_bytes() != data:
                raise ValueError(f"legacy squire file conflicts with target: {target}")
            continue
        planned.append((target, data))
    return tuple(planned)


def _relocate_legacy_skill_paths(data: bytes, legacy: Path) -> bytes:
    """Keep custom instructions while moving known Squire-owned data paths."""

    workspace = b"$HYPRIAL_HOME/agents/squire/workspace"
    skill = b"$HYPRIAL_HOME/agents/squire/config/shared/skills/squire/SKILL.md"
    old_root = os.fsencode(str(legacy.resolve(strict=False)))
    for old in (old_root, b"~/squire"):
        data = data.replace(old + b"/skills/squire/SKILL.md", skill)
        data = data.replace(old + b"/", workspace + b"/")
        data = data.replace(old, workspace)
    return data.replace(b"~/.hyprial/org-context.md", b"$HYPRIAL_HOME/org-context.md")


def create_private_file(
    layout: SquireHomeLayout,
    target: Path,
    data: bytes,
    *,
    keep_existing: bool = False,
) -> bool:
    """Create a file beneath HYPRIAL_HOME without following linked parents."""

    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise ValueError("safe Squire file creation is unsupported on this platform")
    root = layout.home.parent.parent
    relative = target.relative_to(root)
    if ".." in relative.parts or not relative.parts:
        raise ValueError(f"Squire file escapes HYPRIAL_HOME: {target}")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            try:
                os.mkdir(part, 0o700, dir_fd=directory)
            except FileExistsError:
                pass
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = child
        try:
            descriptor = os.open(
                relative.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
        except FileExistsError:
            if keep_existing and stat.S_ISREG(
                os.stat(relative.name, dir_fd=directory, follow_symlinks=False).st_mode
            ):
                return False
            raise ValueError(f"Squire file is not available for creation: {target}") from None
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return True
    finally:
        os.close(directory)


def copy_legacy_files(
    planned: tuple[tuple[Path, bytes], ...], layout: SquireHomeLayout
) -> list[str]:
    changed: list[str] = []
    for target, data in planned:
        create_private_file(layout, target, data)
        changed.append(f"legacy.{target.name}")
    return changed


def legacy_import_marker(state_dir: Path, legacy_home: Path) -> Path:
    source = str(Path(legacy_home).expanduser().resolve(strict=False))
    digest = sha256(os.fsencode(source)).hexdigest()[:24]
    return state_dir / f"squire-legacy-{digest}.done"


def record_legacy_import(layout: SquireHomeLayout, marker: Path) -> None:
    create_private_file(layout, marker, b"migrated\n")
