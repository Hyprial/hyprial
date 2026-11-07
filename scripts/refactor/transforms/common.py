"""Shared infrastructure for the staged refactor toolchain.

Everything in this package is deliberately repository-agnostic: the S1-S6
stages, rule kinds and manifests describe *what* to do, while this module
provides hashing, encoding-safe IO, the module index, the virtual workspace
used for staged simulation, and shared diagnostics types.
"""

from __future__ import annotations

import difflib
import hashlib
import io
import json
import posixpath
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

STAGES = ("S1", "S2", "S3", "S4", "S5", "S6")
RULE_KINDS = (
    "import_module",
    "import_symbol",
    "symbol_extract",
    "class_member_extract",
    "facade_export",
    "reference_rule",
    "layout_move",
)
RULE_STATUSES = ("draft", "approved")


class ToolError(Exception):
    """Fatal tool error with a stable message for CLI reporting."""


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_rel(path: str) -> str:
    """Normalize a repo-relative POSIX path, rejecting escapes."""
    if not isinstance(path, str) or not path or path.startswith("/"):
        raise ToolError(f"path must be a relative POSIX path: {path!r}")
    cleaned = posixpath.normpath(path.replace("\\", "/"))
    if cleaned in (".", "..") or cleaned.startswith("../"):
        raise ToolError(f"path escapes the repository root: {path!r}")
    return cleaned


def stable_json_bytes(data: object) -> bytes:
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def stable_write(path: Path, data: object) -> None:
    path.write_bytes(stable_json_bytes(data))


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    path: str | None = None
    line: int | None = None
    severity: str = "error"


ERROR = "error"
WARNING = "warning"


def decode_source(data: bytes, path: str) -> tuple[str, str]:
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
    except SyntaxError as exc:
        raise ToolError(f"{path}: cannot detect source encoding: {exc}") from exc
    try:
        return data.decode(encoding), encoding
    except (UnicodeDecodeError, LookupError) as exc:
        raise ToolError(f"{path}: cannot decode with {encoding}: {exc}") from exc


def encode_source(text: str, encoding: str) -> bytes:
    return text.encode(encoding)


def unified_diff(path: str, old: bytes, new: bytes) -> str:
    old_text = old.decode("utf-8", "replace").splitlines(keepends=True)
    new_text = new.decode("utf-8", "replace").splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            old_text,
            new_text,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


class ModuleIndex:
    """Module-name <-> repo-path mapping for tracked Python files.

    Namespace aware: every ancestor directory of a tracked module counts as a
    package even without ``__init__.py``. Files under ``src/`` use the
    src-layout package root (``src/hyprial/...`` -> ``hyprial....``); every
    other tracked Python file is indexed by its path-derived module name.
    """

    def __init__(self, python_paths: list[str]) -> None:
        self.module_paths: dict[str, str] = {}
        self.packages: set[str] = set()
        for rel in sorted(python_paths):
            if not rel.endswith(".py"):
                continue
            logical = rel
            if logical.startswith("src/"):
                logical = logical[len("src/") :]
            parts = logical[: -len(".py")].split("/")
            stem = parts[-1]
            dirs = parts[:-1]
            for depth in range(1, len(dirs) + 1):
                self.packages.add(".".join(dirs[:depth]))
            if stem == "__init__":
                name = ".".join(dirs)
            else:
                name = ".".join([*dirs, stem])
            self.module_paths[name] = rel
        self._path_to_module = {v: k for k, v in self.module_paths.items()}

    def module_of(self, rel: str) -> str | None:
        return self._path_to_module.get(rel)

    def path_of(self, module: str) -> str | None:
        return self.module_paths.get(module)

    def is_module(self, name: str) -> bool:
        return name in self.module_paths or name in self.packages

    def is_package(self, name: str) -> bool:
        return name in self.packages

    @staticmethod
    def package_of_module(module: str, is_package_file: bool) -> str:
        return module if is_package_file else module.rpartition(".")[0]

    def file_package(self, rel: str) -> str:
        module = self.module_of(rel)
        if module is None:
            logical = rel[len("src/") :] if rel.startswith("src/") else rel
            parts = logical[: -len(".py")].split("/")
            parts = parts[:-1]
            return ".".join(parts)
        return self.package_of_module(module, rel.endswith("__init__.py"))

    def resolve_relative(self, package: str, level: int, module: str | None) -> str | None:
        if level == 0:
            return module
        parts = package.split(".") if package else []
        if level - 1 > len(parts):
            return None
        base = parts[: len(parts) - (level - 1)] if level > 1 else parts
        pieces = ".".join(base)
        if module:
            pieces = f"{pieces}.{module}" if pieces else module
        return pieces or None

    def relative_form(self, from_package: str, target: str) -> tuple[int, str | None]:
        """Minimal relative import (level, module) from a package to target."""
        if not target:
            raise ToolError("relative_form needs a target module")
        left = from_package.split(".") if from_package else []
        right = target.split(".")
        common = 0
        for a, b in zip(left, right):
            if a != b:
                break
            common += 1
        base_parts = left[:common]
        level = len(left) - len(base_parts) + 1
        rest = right[common:]
        module = ".".join(rest) if rest else None
        if level == 1 and module is None:
            return 1, None
        return level, module


@dataclass
class MoveRecord:
    src: str
    dst: str
    content_sha256: str


class Workspace:
    """Cumulative virtual file state layered over the real checkout."""

    def __init__(self, root: Path, tracked: list[str]) -> None:
        self.root = root
        self.tracked = sorted(tracked)
        self._written: dict[str, bytes] = {}
        self._deleted: set[str] = set()
        self._moves: list[MoveRecord] = []
        self.read_paths: set[str] = set()
        self.index = ModuleIndex([p for p in self.tracked if p.endswith(".py")])

    # -- listing ---------------------------------------------------------
    def paths(self) -> list[str]:
        live = (set(self.tracked) - self._deleted) | set(self._written)
        return sorted(live)

    def iter_python(self) -> list[str]:
        return [p for p in self.paths() if p.endswith(".py")]

    def rebuild_index(self) -> None:
        self.index = ModuleIndex([p for p in self.paths() if p.endswith(".py")])

    # -- reads -----------------------------------------------------------
    def exists(self, rel: str) -> bool:
        rel = normalize_rel(rel)
        if rel in self._deleted:
            return False
        if rel in self._written:
            return True
        return (self.root / rel).is_file()

    def read(self, rel: str) -> bytes:
        rel = normalize_rel(rel)
        self.read_paths.add(rel)
        if rel in self._deleted:
            raise ToolError(f"{rel}: file was removed in simulated state")
        if rel in self._written:
            return self._written[rel]
        data = (self.root / rel).read_bytes()
        return data

    def is_dirty(self, rel: str) -> bool:
        rel = normalize_rel(rel)
        return rel in self._written or rel in self._deleted

    # -- writes ----------------------------------------------------------
    def ensure_real_containment(self, rel: str) -> None:
        """Refuse ALL symlinked write paths on this call path.

        The previous direction check allowed escapes to checkout parents and
        rejected legitimate inward links; the round-4 policy for this pilot's
        writers is simpler and stricter: any symlink ancestor or symlink
        target is refused, and normal paths are unaffected. Unsupported
        layouts must use UNSUPPORTED-refusal, not permissive guesses.
        """
        parts = rel.split("/")
        for depth in range(1, len(parts) + 1):
            candidate = self.root.joinpath(*parts[:depth])
            if candidate.is_symlink():
                raise ToolError(
                    f"{rel}: path component {'/'.join(parts[:depth])} is a symlink; "
                    "this writer refuses all symlinked write paths (UNSUPPORTED)"
                )

    def write(self, rel: str, data: bytes) -> None:
        rel = normalize_rel(rel)
        self.ensure_real_containment(rel)
        if rel in self._deleted:
            self._deleted.discard(rel)
        self._written[rel] = data

    def delete(self, rel: str) -> None:
        rel = normalize_rel(rel)
        self._written.pop(rel, None)
        self._deleted.add(rel)

    def move(self, src: str, dst: str) -> bytes:
        src = normalize_rel(src)
        dst = normalize_rel(dst)
        self.ensure_real_containment(dst)
        if not self.exists(src):
            raise ToolError(f"{src}: cannot move a missing file")
        if self.exists(dst):
            raise ToolError(f"{dst}: move target already exists")
        data = self.read(src)
        self.delete(src)
        self.write(dst, data)
        self._moves.append(MoveRecord(src, dst, sha256_hex(data)))
        if src.endswith(".py") or dst.endswith(".py"):
            self.rebuild_index()
        return data

    def moves(self) -> list[MoveRecord]:
        return list(self._moves)

    def materialize(self) -> None:
        """Write the simulated state to disk: moves first, then contents."""
        import os

        for move in self._moves:
            dst = self.root / move.dst
            dst.parent.mkdir(parents=True, exist_ok=True)
            src = self.root / move.src
            if src.exists():
                src.rename(dst)
            # else: the source only ever existed in this overlay (created by an
            # earlier rule, e.g. a generated facade, then moved by a later one);
            # its content reaches dst through the _written loop below, because
            # move() re-keyed the overlay entry from src to dst.
        for rel, data in sorted(self._written.items()):
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".refactor-tmp")
            tmp.write_bytes(data)
            old_mode = path.stat().st_mode if path.exists() else None
            os.replace(tmp, path)
            if old_mode is not None:
                path.chmod(old_mode)

    # -- directory listings (union of disk and overlay) -------------------
    def list_dir(self, reldir: str) -> set[str]:
        reldir = normalize_rel(reldir)
        names: set[str] = set()
        disk = self.root / reldir
        if disk.is_dir():
            names |= {p.name for p in disk.iterdir()}
        prefix = reldir + "/"
        for rel in list(self._written) + list(self._deleted):
            if rel.startswith(prefix):
                rest = rel[len(prefix) :]
                if "/" not in rest:
                    if rel in self._deleted:
                        names.discard(rest)
                    else:
                        names.add(rest)
                else:
                    names.add(rest.split("/", 1)[0])
        return names


@dataclass
class RuleOutcome:
    rule_id: str
    kind: str
    stage: str
    status: str  # applied | already_done | refused
    matches: int = 0
    expected_matches: int = 0
    changed_paths: list[str] = field(default_factory=list)
    new_form_count: int = 0
    diagnostics: list[Diagnostic] = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    def refused_diagnostic_codes(self) -> list[str]:
        return [d.code for d in self.diagnostics if d.severity == ERROR]


@dataclass
class Rule:
    """A validated manifest rule (loose dict wrapper with typed accessors)."""

    data: dict

    @property
    def rule_id(self) -> str:
        return self.data["rule_id"]

    @property
    def kind(self) -> str:
        return self.data["kind"]

    @property
    def stage(self) -> str:
        return self.data["stage"]

    @property
    def status(self) -> str:
        return self.data["status"]

    @property
    def expected_matches(self) -> int:
        return self.data["expected_matches"]

    def get(self, key: str, default: object = None) -> object:
        return self.data.get(key, default)
