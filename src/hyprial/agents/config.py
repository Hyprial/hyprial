"""Single-source agent config and content-addressed native projections.

The public identity contract stops at :class:`AgentConfig`: one explicit C
directory and no resolution chain.  Manifests, native projection plans and
receipts are internal launch metadata.  They deliberately contain no file
bodies or credential values.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Self

from hyprial.contracts import ipc_errors

__all__ = [
    "NATIVE_CONFIG_MAPPING_VERSION",
    "AgentConfig",
    "AgentConfigError",
    "ConfigManifest",
    "ConfigManifestItem",
    "ConfigProjectionReceipt",
    "NativeConfigProjection",
    "NativeProjectionItem",
    "build_native_projection",
    "materialize_native_projection",
    "normalize_agent_config",
    "require_agent_config",
    "validate_agent_config_location",
    "verify_native_projection",
]

NATIVE_CONFIG_MAPPING_VERSION = "agent-home-p2-native-v1"
_SUPPORTED_HARNESSES = frozenset({"claude", "codex", "pi"})
_PROJECTION_RECEIPT_SCHEMA = 1
_DIGEST_LENGTH = hashlib.sha256().digest_size * 2
_KNOWN_NATIVE_CREDENTIAL_PATHS = {
    "claude": frozenset({".credentials.json"}),
    "codex": frozenset({"auth.json"}),
    "pi": frozenset({"auth.json", "models.json", "models-store.json"}),
}


class AgentConfigError(ValueError):
    """A config shape, source, or frozen-projection violation."""

    code = ipc_errors.INVALID_ARGUMENT


@dataclass(frozen=True, slots=True)
class ConfigManifestItem:
    """One regular file frozen relative to the explicit config root."""

    path: str
    digest: str
    size: int

    def __post_init__(self) -> None:
        _validate_relative_path(self.path, "manifest item path")
        _validate_digest(self.digest, "manifest item digest")
        if not isinstance(self.size, int) or isinstance(self.size, bool) or self.size < 0:
            raise AgentConfigError("manifest item size must be a non-negative integer")

    def to_json(self) -> dict[str, object]:
        return {"path": self.path, "digest": self.digest, "size": self.size}

    @classmethod
    def from_json(cls, value: object, label: str = "manifest.items[]") -> Self:
        item = _exact_object(value, {"path", "digest", "size"}, label)
        return cls(
            path=_nonempty_string(item.get("path"), f"{label}.path"),
            digest=_nonempty_string(item.get("digest"), f"{label}.digest"),
            size=_integer(item.get("size"), f"{label}.size"),
        )


@dataclass(frozen=True, slots=True)
class ConfigManifest:
    """Pre-consumption snapshot of the complete explicit config root."""

    source: str
    items: tuple[ConfigManifestItem, ...]
    digest: str
    missing: bool = False

    def __post_init__(self) -> None:
        _validate_absolute_path(self.source, "manifest source")
        _validate_digest(self.digest, "manifest digest")
        if self.missing and self.items:
            raise AgentConfigError("a missing config manifest cannot contain items")
        if tuple(sorted(self.items, key=lambda item: item.path)) != self.items:
            raise AgentConfigError("config manifest items must be sorted by path")
        if len({item.path for item in self.items}) != len(self.items):
            raise AgentConfigError("config manifest item paths must be unique")
        expected = _manifest_digest(self.source, self.items, self.missing)
        if self.digest != expected:
            raise AgentConfigError("config manifest digest does not match its fields")

    @property
    def revision(self) -> str:
        return self.digest

    def to_json(self) -> dict[str, object]:
        return {
            "source": self.source,
            "items": [item.to_json() for item in self.items],
            "digest": self.digest,
            "missing": self.missing,
        }

    @classmethod
    def from_json(cls, value: object, label: str = "manifest") -> Self:
        raw = _exact_object(value, {"source", "items", "digest", "missing"}, label)
        raw_items = raw.get("items")
        if not isinstance(raw_items, list):
            raise AgentConfigError(f"{label}.items must be a list")
        missing = raw.get("missing")
        if not isinstance(missing, bool):
            raise AgentConfigError(f"{label}.missing must be a boolean")
        return cls(
            source=_nonempty_string(raw.get("source"), f"{label}.source"),
            items=tuple(
                ConfigManifestItem.from_json(item, f"{label}.items[{index}]")
                for index, item in enumerate(raw_items)
            ),
            digest=_nonempty_string(raw.get("digest"), f"{label}.digest"),
            missing=missing,
        )


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """The sole public config shape: one C and explicit-only discovery."""

    source: str
    required: bool = True
    discovery: str = "explicit-only"

    def __post_init__(self) -> None:
        _validate_absolute_path(self.source, "config.sources[0].path")
        if not isinstance(self.required, bool):
            raise AgentConfigError("config.sources[0].required must be a boolean")
        if self.discovery != "explicit-only":
            raise AgentConfigError(
                "config.discovery must be 'explicit-only'; implicit discovery is forbidden"
            )

    def to_json(self) -> dict[str, object]:
        return {
            "sources": [{"path": self.source, "required": self.required}],
            "discovery": self.discovery,
        }

    @classmethod
    def from_json(cls, value: object, label: str = "config") -> Self:
        if not isinstance(value, dict):
            raise AgentConfigError(
                f"{label} must be an object, not {type(value).__name__}; "
                "list and resolution-chain config shapes are not supported"
            )
        allowed = {"sources", "discovery"}
        extras = [key for key in value if key not in allowed]
        if extras:
            key = extras[0]
            detail = (
                "resolution chains are not supported"
                if key in {"precedence", "chain", "resolutionChain"}
                else "only sources and discovery are allowed"
            )
            raise AgentConfigError(f"{label}.{key} is not allowed: {detail}")
        sources = value.get("sources")
        if not isinstance(sources, list):
            raise AgentConfigError(
                f"{label}.sources must be a list containing exactly one source"
            )
        if len(sources) != 1:
            violating = f"{label}.sources[1]" if len(sources) > 1 else f"{label}.sources"
            raise AgentConfigError(
                f"{violating} violates the single-path constraint: "
                f"{label}.sources must contain exactly one source"
            )
        source = sources[0]
        if not isinstance(source, dict):
            raise AgentConfigError(f"{label}.sources[0] must be an object with path")
        source_extras = [key for key in source if key not in {"path", "required"}]
        if source_extras:
            key = source_extras[0]
            raise AgentConfigError(
                f"{label}.sources[0].{key} is not allowed; "
                "a source contains only path and required"
            )
        path = source.get("path")
        if not isinstance(path, str) or not path:
            raise AgentConfigError(f"{label}.sources[0].path must be a non-empty string")
        source_path = Path(path).expanduser()
        if not source_path.is_absolute():
            raise AgentConfigError(f"{label}.sources[0].path must be absolute")
        required = source.get("required", True)
        if not isinstance(required, bool):
            raise AgentConfigError(f"{label}.sources[0].required must be a boolean")
        discovery = value.get("discovery")
        if discovery != "explicit-only":
            raise AgentConfigError(
                f"{label}.discovery must be 'explicit-only'; implicit discovery is forbidden"
            )
        return cls(str(source_path), required=required)

    def freeze_manifest(self) -> ConfigManifest:
        """Freeze every regular C file before any native projection is consumed."""

        root = Path(self.source)
        try:
            root_metadata = root.lstat()
        except FileNotFoundError:
            if self.required:
                raise AgentConfigError(
                    f"config.sources[0].path does not exist: {root}"
                ) from None
            return _new_manifest(self.source, (), missing=True)
        except OSError as error:
            raise AgentConfigError(
                f"cannot inspect explicit config source {root}: {error}"
            ) from error
        if stat.S_ISLNK(root_metadata.st_mode):
            raise AgentConfigError("config.sources[0].path must not be a symbolic link")
        if not stat.S_ISDIR(root_metadata.st_mode):
            raise AgentConfigError(f"config.sources[0].path must be a directory: {root}")
        try:
            items = tuple(sorted(_scan_config_tree(root, root), key=lambda item: item.path))
        except AgentConfigError:
            raise
        except OSError as error:
            raise AgentConfigError(
                f"cannot read explicit config source {root}: {error}"
            ) from error
        return _new_manifest(self.source, items, missing=False)

    def verify_manifest(self, expected: ConfigManifest) -> None:
        """Reject source, item, or content drift against a pre-fixed manifest."""

        if expected.source != self.source:
            raise AgentConfigError("config manifest source does not match the explicit C")
        actual = self.freeze_manifest()
        if actual == expected:
            return
        expected_items = {item.path: item for item in expected.items}
        actual_items = {item.path: item for item in actual.items}
        changed = sorted(
            path
            for path in expected_items.keys() | actual_items.keys()
            if expected_items.get(path) != actual_items.get(path)
        )
        item = changed[0] if changed else "manifest root"
        raise AgentConfigError(
            f"config changed after its manifest was fixed: {item}; start a new run"
        )


@dataclass(frozen=True, slots=True)
class NativeProjectionItem:
    """One frozen C item and its native-relative destination."""

    source_path: str
    native_path: str
    digest: str
    size: int

    def __post_init__(self) -> None:
        _validate_relative_path(self.source_path, "projection source path")
        _validate_relative_path(self.native_path, "projection native path")
        _validate_digest(self.digest, "projection item digest")
        if not isinstance(self.size, int) or isinstance(self.size, bool) or self.size < 0:
            raise AgentConfigError("projection item size must be a non-negative integer")

    def to_json(self) -> dict[str, object]:
        return {
            "sourcePath": self.source_path,
            "nativePath": self.native_path,
            "digest": self.digest,
            "size": self.size,
        }

    @classmethod
    def from_json(cls, value: object, label: str = "projection.items[]") -> Self:
        raw = _exact_object(
            value, {"sourcePath", "nativePath", "digest", "size"}, label
        )
        return cls(
            source_path=_nonempty_string(raw.get("sourcePath"), f"{label}.sourcePath"),
            native_path=_nonempty_string(raw.get("nativePath"), f"{label}.nativePath"),
            digest=_nonempty_string(raw.get("digest"), f"{label}.digest"),
            size=_integer(raw.get("size"), f"{label}.size"),
        )


@dataclass(frozen=True, slots=True)
class NativeConfigProjection:
    """Deterministic C-to-native mapping plan, before filesystem publication."""

    harness: str
    source_digest: str
    mapping_version: str
    items: tuple[NativeProjectionItem, ...]
    digest: str

    def __post_init__(self) -> None:
        _validate_harness(self.harness)
        _validate_digest(self.source_digest, "projection source digest")
        if self.mapping_version != NATIVE_CONFIG_MAPPING_VERSION:
            raise AgentConfigError("unsupported native config mapping version")
        if tuple(sorted(self.items, key=lambda item: item.native_path)) != self.items:
            raise AgentConfigError("native projection items must be sorted by native path")
        if len({item.native_path for item in self.items}) != len(self.items):
            raise AgentConfigError("native projection paths must be unique")
        _validate_digest(self.digest, "projection digest")
        if self.digest != _projection_digest(
            self.harness, self.source_digest, self.mapping_version, self.items
        ):
            raise AgentConfigError("native projection digest does not match its fields")

    def to_json(self) -> dict[str, object]:
        return {
            "harness": self.harness,
            "sourceDigest": self.source_digest,
            "mappingVersion": self.mapping_version,
            "items": [item.to_json() for item in self.items],
            "digest": self.digest,
        }

    @classmethod
    def from_json(cls, value: object, label: str = "projection") -> Self:
        raw = _exact_object(
            value,
            {"harness", "sourceDigest", "mappingVersion", "items", "digest"},
            label,
        )
        raw_items = raw.get("items")
        if not isinstance(raw_items, list):
            raise AgentConfigError(f"{label}.items must be a list")
        return cls(
            harness=_nonempty_string(raw.get("harness"), f"{label}.harness"),
            source_digest=_nonempty_string(
                raw.get("sourceDigest"), f"{label}.sourceDigest"
            ),
            mapping_version=_nonempty_string(
                raw.get("mappingVersion"), f"{label}.mappingVersion"
            ),
            items=tuple(
                NativeProjectionItem.from_json(item, f"{label}.items[{index}]")
                for index, item in enumerate(raw_items)
            ),
            digest=_nonempty_string(raw.get("digest"), f"{label}.digest"),
        )


@dataclass(frozen=True, slots=True)
class ConfigProjectionReceipt:
    """Internal incarnation-bound receipt for one atomic projection publish."""

    actor: str
    entity_token: str
    source_digest: str
    harness: str
    mapping_version: str
    projection_digest: str
    projection_root: str
    items: tuple[NativeProjectionItem, ...]

    def __post_init__(self) -> None:
        if not self.actor or not self.entity_token:
            raise AgentConfigError("projection receipt identity must not be blank")
        _validate_digest(self.source_digest, "receipt source digest")
        _validate_harness(self.harness)
        if self.mapping_version != NATIVE_CONFIG_MAPPING_VERSION:
            raise AgentConfigError("unsupported projection receipt mapping version")
        _validate_digest(self.projection_digest, "receipt projection digest")
        _validate_absolute_path(self.projection_root, "receipt projection root")
        if tuple(sorted(self.items, key=lambda item: item.native_path)) != self.items:
            raise AgentConfigError("projection receipt items must be sorted")
        if self.projection_digest != _projection_digest(
            self.harness, self.source_digest, self.mapping_version, self.items
        ):
            raise AgentConfigError("projection receipt digest does not match its items")

    def to_json(self) -> dict[str, object]:
        return {
            "schemaVersion": _PROJECTION_RECEIPT_SCHEMA,
            "actor": self.actor,
            "entityToken": self.entity_token,
            "sourceDigest": self.source_digest,
            "harness": self.harness,
            "mappingVersion": self.mapping_version,
            "projectionDigest": self.projection_digest,
            "projectionRoot": self.projection_root,
            "items": [item.to_json() for item in self.items],
        }

    @classmethod
    def from_json(cls, value: object, label: str = "projectionReceipt") -> Self:
        raw = _exact_object(
            value,
            {
                "schemaVersion",
                "actor",
                "entityToken",
                "sourceDigest",
                "harness",
                "mappingVersion",
                "projectionDigest",
                "projectionRoot",
                "items",
            },
            label,
        )
        if raw.get("schemaVersion") != _PROJECTION_RECEIPT_SCHEMA:
            raise AgentConfigError("unsupported config projection receipt")
        raw_items = raw.get("items")
        if not isinstance(raw_items, list):
            raise AgentConfigError(f"{label}.items must be a list")
        return cls(
            actor=_nonempty_string(raw.get("actor"), f"{label}.actor"),
            entity_token=_nonempty_string(
                raw.get("entityToken"), f"{label}.entityToken"
            ),
            source_digest=_nonempty_string(
                raw.get("sourceDigest"), f"{label}.sourceDigest"
            ),
            harness=_nonempty_string(raw.get("harness"), f"{label}.harness"),
            mapping_version=_nonempty_string(
                raw.get("mappingVersion"), f"{label}.mappingVersion"
            ),
            projection_digest=_nonempty_string(
                raw.get("projectionDigest"), f"{label}.projectionDigest"
            ),
            projection_root=_nonempty_string(
                raw.get("projectionRoot"), f"{label}.projectionRoot"
            ),
            items=tuple(
                NativeProjectionItem.from_json(item, f"{label}.items[{index}]")
                for index, item in enumerate(raw_items)
            ),
        )


def normalize_agent_config(value: object) -> AgentConfig | None:
    """Validate one public config value; ``None`` preserves legacy entities."""

    if value is None or isinstance(value, AgentConfig):
        return value
    return AgentConfig.from_json(value)


def require_agent_config(
    value: AgentConfig | None, *, actor: str = "agent"
) -> AgentConfig:
    """Return an explicit C or reject a P2 launch without inventing an empty one."""

    if value is None:
        raise AgentConfigError(
            f"{actor} has no explicit personality config C; P2 launch is unsupported"
        )
    return value


def validate_agent_config_location(
    config: AgentConfig | None,
    *,
    agent_home: Path | None,
    cwd: str | None,
) -> None:
    """Reject C aliases/containment with runtime, secret, state, or cwd roots."""

    if config is None:
        return
    source = _resolved_for_overlap(Path(config.source))
    protected: list[tuple[str, Path]] = []
    if agent_home is not None:
        home = _resolved_for_overlap(agent_home)
        if source == home:
            raise AgentConfigError("config C must not be the whole agent home")
        protected.extend((name, home / name) for name in ("state", "secrets"))
    if cwd is not None:
        protected.append(("cwd", _resolved_for_overlap(Path(cwd).expanduser())))
    for label, path in protected:
        resolved = _resolved_for_overlap(path)
        if _paths_overlap(source, resolved):
            raise AgentConfigError(
                f"config C must not contain, be contained by, or alias agent {label}"
            )


def build_native_projection(
    manifest: ConfigManifest, harness: str
) -> NativeConfigProjection:
    """Map one complete C revision to a provider-native relative file set."""

    _validate_harness(harness)
    selected: list[NativeProjectionItem] = []
    occupied: dict[str, str] = {}
    for item in manifest.items:
        parts = PurePosixPath(item.path).parts
        native_path: str | None
        if len(parts) >= 3 and parts[:2] == ("shared", "skills"):
            native_path = PurePosixPath("skills", *parts[2:]).as_posix()
        elif len(parts) >= 3 and parts[0] == "native" and parts[1] in _SUPPORTED_HARNESSES:
            provider = parts[1]
            provider_path = PurePosixPath(*parts[2:]).as_posix()
            if provider_path in _KNOWN_NATIVE_CREDENTIAL_PATHS[provider]:
                raise AgentConfigError(
                    f"config item {item.path} maps to credential-bearing native path "
                    f"{provider_path}; authentication must remain outside C"
                )
            native_path = provider_path if provider == harness else None
        else:
            raise AgentConfigError(
                f"config item {item.path} is outside the frozen native/shared mapping"
            )
        if native_path is None:
            continue
        prior = occupied.get(native_path)
        if prior is not None:
            raise AgentConfigError(
                f"config items {prior} and {item.path} both map to {native_path}; "
                "native projection has no implicit precedence"
            )
        occupied[native_path] = item.path
        selected.append(
            NativeProjectionItem(item.path, native_path, item.digest, item.size)
        )
    ordered = tuple(sorted(selected, key=lambda item: item.native_path))
    digest = _projection_digest(
        harness, manifest.digest, NATIVE_CONFIG_MAPPING_VERSION, ordered
    )
    return NativeConfigProjection(
        harness=harness,
        source_digest=manifest.digest,
        mapping_version=NATIVE_CONFIG_MAPPING_VERSION,
        items=ordered,
        digest=digest,
    )


def materialize_native_projection(
    config: AgentConfig,
    manifest: ConfigManifest,
    projection: NativeConfigProjection,
    *,
    actor: str,
    entity_token: str,
    projection_root: Path,
    incumbent: ConfigProjectionReceipt | None = None,
) -> ConfigProjectionReceipt:
    """Atomically publish one immutable native projection under an empty path."""

    if projection.source_digest != manifest.digest:
        raise AgentConfigError("native projection was built for another C revision")
    if projection != build_native_projection(manifest, projection.harness):
        raise AgentConfigError("native projection does not match the frozen mapping")
    config.verify_manifest(manifest)
    target = Path(projection_root)
    if not target.is_absolute():
        raise AgentConfigError("native projection root must be absolute")
    parent = target.parent
    _validate_publish_parent(parent)
    if target.exists() or target.is_symlink():
        expected_receipt = _projection_receipt(actor, entity_token, projection, target)
        if incumbent != expected_receipt:
            raise AgentConfigError(
                "native projection root already exists without a matching "
                "incarnation-bound receipt"
            )
        verify_native_projection(projection, target)
        return incumbent
    if incumbent is not None:
        raise AgentConfigError(
            "native projection receipt exists but its projection root is missing"
        )

    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=parent))
    os.chmod(stage, 0o700)
    published = False
    try:
        source_root = Path(config.source)
        expected = {item.path: item for item in manifest.items}
        for item in projection.items:
            manifest_item = expected[item.source_path]
            body = _read_regular_file(source_root, item.source_path)
            if len(body) != manifest_item.size or _sha256(body) != manifest_item.digest:
                raise AgentConfigError(
                    f"config changed after its manifest was fixed: {item.source_path}; "
                    "start a new run"
                )
            destination = stage.joinpath(*PurePosixPath(item.native_path).parts)
            _mkdir_projection_parents(stage, destination.parent)
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                with os.fdopen(descriptor, "wb", closefd=False) as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                os.close(descriptor)
        config.verify_manifest(manifest)
        try:
            os.rename(stage, target)
        except FileExistsError as error:
            raise AgentConfigError(
                "native projection root appeared before atomic publication"
            ) from error
        else:
            published = True
        verify_native_projection(projection, target)
        return _projection_receipt(actor, entity_token, projection, target)
    finally:
        if not published and stage.exists():
            shutil.rmtree(stage)


def verify_native_projection(
    projection: NativeConfigProjection, projection_root: Path
) -> None:
    """Verify the complete published tree and its private permissions."""

    root = Path(projection_root)
    try:
        metadata = root.lstat()
    except OSError as error:
        raise AgentConfigError(f"cannot inspect native projection root: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise AgentConfigError("native projection root must be a real directory")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise AgentConfigError("native projection root mode must be 0700")
    actual = tuple(sorted(_scan_projection_tree(root, root), key=lambda item: item.path))
    expected = tuple(
        ConfigManifestItem(item.native_path, item.digest, item.size)
        for item in projection.items
    )
    if actual == expected:
        return
    expected_items = {item.path: item for item in expected}
    actual_items = {item.path: item for item in actual}
    changed = sorted(
        path
        for path in expected_items.keys() | actual_items.keys()
        if expected_items.get(path) != actual_items.get(path)
    )
    item = changed[0] if changed else "projection root"
    raise AgentConfigError(f"native projection drifted after publication: {item}")


def _scan_config_tree(root: Path, directory: Path) -> list[ConfigManifestItem]:
    items: list[ConfigManifestItem] = []
    with os.scandir(directory) as entries:
        for entry in entries:
            candidate = Path(entry.path)
            relative = candidate.relative_to(root).as_posix()
            metadata = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                raise AgentConfigError(f"config item {relative} must not be a symbolic link")
            if stat.S_ISDIR(metadata.st_mode):
                items.extend(_scan_config_tree(root, candidate))
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise AgentConfigError(f"config item {relative} must be a regular file")
            credential = _known_credential_native_path(relative)
            if credential is not None:
                raise AgentConfigError(
                    f"config item {relative} maps to credential-bearing native path "
                    f"{credential}; authentication must remain outside C"
                )
            body = _read_regular_file(root, relative)
            items.append(ConfigManifestItem(relative, _sha256(body), len(body)))
    return items


def _scan_projection_tree(
    root: Path, directory: Path
) -> list[ConfigManifestItem]:
    items: list[ConfigManifestItem] = []
    with os.scandir(directory) as entries:
        for entry in entries:
            candidate = Path(entry.path)
            relative = candidate.relative_to(root).as_posix()
            metadata = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                raise AgentConfigError(
                    f"native projection item {relative} must not be a symbolic link"
                )
            if stat.S_ISDIR(metadata.st_mode):
                if stat.S_IMODE(metadata.st_mode) != 0o700:
                    raise AgentConfigError(
                        f"native projection directory {relative} mode must be 0700"
                    )
                items.extend(_scan_projection_tree(root, candidate))
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise AgentConfigError(
                    f"native projection item {relative} must be a regular file"
                )
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                raise AgentConfigError(
                    f"native projection item {relative} mode must be 0600"
                )
            body = _read_regular_file(root, relative)
            items.append(ConfigManifestItem(relative, _sha256(body), len(body)))
    return items


def _read_regular_file(root: Path, relative: str) -> bytes:
    _validate_relative_path(relative, "config item path")
    candidate = root.joinpath(*PurePosixPath(relative).parts)
    _validate_no_symlink_components(root, candidate.parent)
    try:
        before = candidate.lstat()
    except OSError as error:
        raise AgentConfigError(f"cannot inspect config item {relative}: {error}") from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise AgentConfigError(f"config item {relative} must be a regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as error:
        raise AgentConfigError(f"cannot read config item {relative}: {error}") from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
            opened.st_dev,
            opened.st_ino,
        ) != (before.st_dev, before.st_ino):
            raise AgentConfigError(f"config item {relative} changed while being opened")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    body = b"".join(chunks)
    if (
        (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        or len(body) != after.st_size
    ):
        raise AgentConfigError(f"config item {relative} changed while being read")
    return body


def _validate_no_symlink_components(root: Path, directory: Path) -> None:
    current = root
    try:
        root_metadata = current.lstat()
    except OSError as error:
        raise AgentConfigError(f"cannot inspect config root: {error}") from error
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise AgentConfigError("config root must be a real directory")
    for part in directory.relative_to(root).parts:
        current = current / part
        try:
            metadata = current.lstat()
        except OSError as error:
            raise AgentConfigError(f"cannot inspect config directory {part}: {error}") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise AgentConfigError(f"config directory {part} must be a real directory")


def _validate_publish_parent(parent: Path) -> None:
    try:
        metadata = parent.lstat()
    except OSError as error:
        raise AgentConfigError(f"cannot inspect projection parent: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise AgentConfigError("projection parent must be a real directory")


def _mkdir_projection_parents(root: Path, directory: Path) -> None:
    current = root
    for part in directory.relative_to(root).parts:
        current = current / part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise AgentConfigError(
                    f"native projection parent {current.name} must be a real directory"
                ) from None
        os.chmod(current, 0o700)


def _projection_receipt(
    actor: str,
    entity_token: str,
    projection: NativeConfigProjection,
    root: Path,
) -> ConfigProjectionReceipt:
    return ConfigProjectionReceipt(
        actor=actor,
        entity_token=entity_token,
        source_digest=projection.source_digest,
        harness=projection.harness,
        mapping_version=projection.mapping_version,
        projection_digest=projection.digest,
        projection_root=str(root),
        items=projection.items,
    )


def _new_manifest(
    source: str, items: tuple[ConfigManifestItem, ...], *, missing: bool
) -> ConfigManifest:
    return ConfigManifest(
        source=source,
        items=items,
        digest=_manifest_digest(source, items, missing),
        missing=missing,
    )


def _manifest_digest(
    source: str, items: tuple[ConfigManifestItem, ...], missing: bool
) -> str:
    return _digest_json(
        {
            "source": source,
            "items": [item.to_json() for item in items],
            "missing": missing,
        }
    )


def _projection_digest(
    harness: str,
    source_digest: str,
    mapping_version: str,
    items: tuple[NativeProjectionItem, ...],
) -> str:
    return _digest_json(
        {
            "harness": harness,
            "sourceDigest": source_digest,
            "mappingVersion": mapping_version,
            "items": [item.to_json() for item in items],
        }
    )


def _digest_json(value: object) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return _sha256(body)


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _exact_object(value: object, keys: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AgentConfigError(f"{label} must be an object")
    extras = [key for key in value if key not in keys]
    missing = [key for key in keys if key not in value]
    if extras:
        raise AgentConfigError(f"{label}.{extras[0]} is not allowed")
    if missing:
        raise AgentConfigError(f"{label}.{missing[0]} is required")
    return value


def _validate_harness(value: str) -> None:
    if value not in _SUPPORTED_HARNESSES:
        raise AgentConfigError(
            f"unsupported native projection harness {value!r}; "
            f"expected one of {sorted(_SUPPORTED_HARNESSES)}"
        )


def _known_credential_native_path(relative: str) -> str | None:
    parts = PurePosixPath(relative).parts
    if len(parts) < 3 or parts[0] != "native":
        return None
    provider = parts[1]
    if provider not in _SUPPORTED_HARNESSES:
        return None
    native_path = PurePosixPath(*parts[2:]).as_posix()
    return (
        native_path
        if native_path in _KNOWN_NATIVE_CREDENTIAL_PATHS[provider]
        else None
    )


def _validate_absolute_path(value: str, label: str) -> None:
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise AgentConfigError(f"{label} must be an absolute path")


def _validate_relative_path(value: str, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise AgentConfigError(f"{label} must be a non-empty relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise AgentConfigError(f"{label} must be a normalized relative path")
    if path.as_posix() != value:
        raise AgentConfigError(f"{label} must use normalized POSIX separators")


def _validate_digest(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != _DIGEST_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise AgentConfigError(f"{label} must be a lowercase SHA-256 hex digest")


def _nonempty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise AgentConfigError(f"{label} must be a non-empty string")
    return value


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise AgentConfigError(f"{label} must be an integer")
    return value


def _resolved_for_overlap(path: Path) -> Path:
    return Path(os.path.realpath(path))


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents
