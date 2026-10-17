"""AT06: agent-home bundle definition, export and pre-receive validation.

This module is the transport unit used by cross-machine agent transfer.  A
bundle is a directory containing:

    bundle/
      manifest.json          # frozen schema, written by export_bundle()
      payload/               # agent-home files, one entry per manifest record

The manifest is the single source of truth: every file under ``payload/``
must be registered, and every registered entry must match on size, sha256 and
mode.  Anything else is refused.  The manifest also carries
``manifest_digest``, a self-checksum over its own body, so editing the
identity fields after export is refused too -- schema version 2, because
schema 1 manifests have no seal.

Scope note (AT06): this slice defines, exports and validates the bundle.  It
does not move a bundle across machines, does not touch desired-state and does
not lift the P2 transfer refusal.

Split note (structural limits): this half carries the manifest model and the
pre-receive validation; ``export_bundle`` and the source-unchanged witness
live in ``._export``, and ``bundle/__init__.py`` re-exports both halves under
the original ``archive.bundle`` import path.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping

BUNDLE_SCHEMA_VERSION = 2
BUNDLE_PROTOCOL_VERSION = 1
SUPPORTED_PROTOCOL_VERSIONS = (1,)

MANIFEST_NAME = "manifest.json"
PAYLOAD_DIRNAME = "payload"

#: Field that seals the manifest body.  ``validate_bundle`` recomputes it, so
#: editing ``owner``/``actor``/``entries``/``identity`` without re-sealing is
#: refused -- before this, only the payload was bound to the manifest, and the
#: identity fields could be edited freely.  This is a self-checksum, not a
#: keyed signature: a keyed one needs the credential-key ruling the sealed
#: envelope prototype is still waiting on (card 355 item 2).
MANIFEST_DIGEST_FIELD = "manifest_digest"

#: Directories never exported, even when they live inside the agent home.
EXCLUDED_DIR_NAMES = frozenset({"__pycache__", ".pytest_cache"})

#: File names never exported: derived projections and host model-vendor
#: secrets are properties of the machine, not of the agent.
EXCLUDED_FILE_NAMES = frozenset(
    {
        ".DS_Store",
        "user-provider.json",
        "providers.json",
        "login.keychain-db",
    }
)

#: Paths, relative to the agent home, that belong to the machine rather than
#: the agent and are never exported -- a bundle that carried the operator's
#: Keychain would hand another host the operator's credentials (card 355
#: item 1).  Matched on whole path components, never as a substring.
EXCLUDED_RELATIVE_PREFIXES = (
    "Library/Keychains",
)

_CHUNK = 1024 * 256


class BundleError(Exception):
    """Base class for bundle failures."""

    code = "bundle_error"


class BundleManifestError(BundleError):
    """The manifest is missing, malformed or internally inconsistent."""

    code = "bundle_manifest_invalid"


class BundlePathError(BundleError):
    """A path escapes the bundle, or is a symlink/unsafe entry."""

    code = "bundle_path_unsafe"


class BundleProtocolMismatch(BundleError):
    """The bundle was produced by a protocol version we do not speak."""

    code = "bundle_protocol_mismatch"


class BundleTamperError(BundleError):
    """Payload bytes do not match the manifest."""

    code = "bundle_tampered"


class BundleDestinationNotEmpty(BundleError):
    """The destination already holds something; export never deletes it."""

    code = "bundle_destination_not_empty"


class BundleDestinationOverlapsSource(BundleError):
    """The destination is the source, or contains/inside it; refuse outright."""

    code = "bundle_destination_overlaps_source"


class BundleSourceLive(BundleError):
    """The source agent home still has a live daemon; a stop-write is required."""

    code = "bundle_source_live"


class BundleSourceChanged(BundleError):
    """The source tree changed while it was being packed; the copy is refused."""

    code = "bundle_source_changed"


class BundleEpochMismatch(BundleError):
    """The bundle was exported from a different incarnation than expected.

    Card 355 item 2 asks the consistency payload to carry the source agent's
    owner/epoch.  ``source_epoch`` is a digest, never the token itself, so a
    receiver can say "this is generation N" without ever holding the secret.
    """

    code = "bundle_epoch_mismatch"


class BundleOwnerMismatch(BundleError):
    """The bundle claims a different owner than the caller asserted."""

    code = "bundle_owner_mismatch"


class BundleActorMismatch(BundleError):
    """The bundle claims a different actor than the caller asserted."""

    code = "bundle_actor_mismatch"


class BundleDeclarationMissing(BundleError):
    """The manifest declares a file the bundle does not actually carry.

    Card 355 item 4 asks the pre-receive gate to refuse a bundle whose
    *declared* dependencies (a credential envelope, an external config, a cwd
    snapshot) are missing.  A declaration that cannot be honoured must not
    degrade silently into "this bundle simply has no credentials".
    """

    code = "bundle_declaration_missing"


def _epoch_digest(value: str) -> str:
    """Hash a source incarnation identifier: the manifest keeps no token."""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _manifest_digest(raw: Mapping[str, object]) -> str:
    """Seal the manifest body: canonical JSON of every field but the digest."""

    body = {
        key: value for key, value in raw.items() if key != MANIFEST_DIGEST_FIELD
    }
    payload = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(relative: str) -> str:
    """Normalise and vet a bundle-relative path; raise when unsafe."""

    if not relative or relative.strip() != relative:
        raise BundlePathError(f"empty or padded path: {relative!r}")
    pure = PurePosixPath(relative)
    if pure.is_absolute():
        raise BundlePathError(f"absolute path is not allowed: {relative!r}")
    if "\\" in relative:
        raise BundlePathError(f"backslash is not allowed: {relative!r}")
    parts = pure.parts
    if any(part in ("", ".", "..") for part in parts):
        raise BundlePathError(f"path escapes the bundle: {relative!r}")
    if relative.startswith("/"):
        raise BundlePathError(f"absolute path is not allowed: {relative!r}")
    return pure.as_posix()


def _excluded_relative(relative: str) -> bool:
    """True when ``relative`` is (under) a host-owned path we never export."""

    parts = PurePosixPath(relative).parts
    for prefix in EXCLUDED_RELATIVE_PREFIXES:
        wanted = PurePosixPath(prefix).parts
        if parts[: len(wanted)] == wanted:
            return True
    return False


def _iter_payload_files(root: Path) -> Iterable[tuple[str, Path]]:
    for current, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in EXCLUDED_DIR_NAMES
            and not _excluded_relative(
                (Path(current) / name).relative_to(root).as_posix()
            )
        )
        for filename in sorted(filenames):
            if filename in EXCLUDED_FILE_NAMES:
                continue
            absolute = Path(current) / filename
            relative = absolute.relative_to(root).as_posix()
            if _excluded_relative(relative):
                continue
            yield _safe_relative(relative), absolute

def _normalise_dependencies(
    raw: Iterable[Mapping[str, object]] | None,
) -> tuple[dict[str, object], ...]:
    """Vet declared dependencies; keep the manifest's meaning up front."""

    normalised: list[dict[str, object]] = []
    seen: dict[tuple[str, str], bool] = {}
    for item in raw or ():
        if not isinstance(item, Mapping):
            raise BundleManifestError("dependency must be an object")
        kind = item.get("kind")
        if not isinstance(kind, str) or not kind:
            raise BundleManifestError("dependency needs a non-empty kind")
        present = item.get("present", False)
        if not isinstance(present, bool):
            raise BundleManifestError(
                f"dependency {kind!r} needs a boolean present"
            )
        path = item.get("path", "")
        note = item.get("note", "")
        if path and not isinstance(path, str):
            raise BundleManifestError(f"dependency {kind!r} path must be a string")
        if note and not isinstance(note, str):
            raise BundleManifestError(f"dependency {kind!r} note must be a string")
        if present and not path:
            raise BundleManifestError(
                f"dependency {kind!r} claims present=true but names no path"
            )
        if not present and not (path or note):
            raise BundleManifestError(
                f"dependency {kind!r} must name a path or a note"
            )
        if path:
            path = _safe_relative(path)
        record: dict[str, object] = {"kind": kind, "present": present}
        if path:
            record["path"] = path
        if note:
            record["note"] = note
        key = (kind, path or note)
        if key in seen:
            if seen[key] is not present:
                raise BundleManifestError(
                    f"dependency {kind!r} has conflicting present declarations for {path or note!r}"
                )
            continue
        seen[key] = present
        normalised.append(record)
    return tuple(normalised)


def _resolve_declared_path(
    root: Path,
    relative: str,
    *,
    label: str,
    registered: "set[str] | None" = None,
) -> Path:
    """Resolve a bundle-relative declared path, tolerating the payload prefix.

    A declaration may name ``payload/state/config.json`` or the friendly
    ``state/config.json``; both resolve to the same file.  Anything that
    escapes the bundle, is a symlink, or simply is not there is refused.
    """

    safe = _safe_relative(relative)
    # Only files under ``payload/`` are hashed and registered.  A declaration
    # that reached a bundle-root file would be a path nothing verified, so the
    # friendly form is the payload-relative one and everything else refuses.
    parts = PurePosixPath(safe).parts
    if parts[0] == PAYLOAD_DIRNAME:
        payload_relative = PurePosixPath(*parts[1:]).as_posix()
    else:
        payload_relative = safe
    candidate = root / PAYLOAD_DIRNAME / payload_relative
    if registered is not None and payload_relative not in registered:
        raise BundleDeclarationMissing(
            f"{label} {relative!r} is not a registered payload entry; a "
            "declaration must name a file the manifest already accounts for"
        )
    if candidate.is_symlink():
        raise BundleDeclarationMissing(
            f"{label} {relative!r} is a symlink; declarations must name a "
            "regular file the bundle actually carries"
        )
    if candidate.is_file() and _within(candidate.resolve(), root.resolve()):
        return candidate
    raise BundleDeclarationMissing(
        f"{label} {relative!r} is declared but the bundle does not carry it"
    )


#: Magic numbers of platform binaries: these belong to the machine, not to the
#: agent, so the export neither copies them nor pretends the target has them.
_BINARY_MAGIC = (
    b"\x7fELF",
    b"\xcf\xfa\xed\xfe",
    b"\xce\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"MZ",
)


def _is_platform_binary(path: Path) -> bool:
    """True for ELF / Mach-O / PE files (AT06 item 1's platform-binary rule)."""

    try:
        with open(path, "rb") as handle:
            head = handle.read(4)
    except OSError:
        # Unreadable is not "binary": the caller's own identity check decides.
        return False
    return any(head.startswith(magic) for magic in _BINARY_MAGIC)


@dataclass(frozen=True)
class BundleEntry:
    """One payload file recorded in the manifest."""

    path: str
    size: int
    sha256: str
    mode: int
    role: str = "agent-home"

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
            "mode": self.mode,
            "role": self.role,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "BundleEntry":
        missing = {"path", "size", "sha256", "mode"} - set(raw)
        if missing:
            raise BundleManifestError(
                f"entry is missing fields: {sorted(missing)}"
            )
        path = _safe_relative(str(raw["path"]))
        size = int(raw["size"])
        if size < 0:
            raise BundleManifestError(f"negative size for {path!r}")
        digest = str(raw["sha256"])
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise BundleManifestError(f"bad sha256 for {path!r}: {digest!r}")
        mode = int(raw["mode"])
        if mode < 0 or mode > 0o7777:
            raise BundleManifestError(f"bad mode for {path!r}: {mode!r}")
        role = str(raw.get("role", "agent-home"))
        return cls(path=path, size=size, sha256=digest, mode=mode, role=role)


@dataclass(frozen=True)
class BundleManifest:
    """Frozen transport-unit manifest (schema version 1)."""

    owner: str
    actor: str
    source_machine: str
    created_at: str
    protocol_version: int = BUNDLE_PROTOCOL_VERSION
    schema_version: int = BUNDLE_SCHEMA_VERSION
    entries: tuple[BundleEntry, ...] = ()
    identity: Mapping[str, object] = field(default_factory=dict)
    session_refs: tuple[str, ...] = ()
    grants: tuple[str, ...] = ()
    #: Bundle-relative path of the sealed credential envelope, when the
    #: bundle carries one (card 355 item 1/2).  Empty means "none declared".
    credential_envelope: str = ""
    #: Digest of the source agent's incarnation at export time.  Empty means
    #: the export had no incarnation to read (offline export); an
    #: ``--expect-epoch`` check never passes on an empty value.
    source_epoch: str = ""
    #: Declared dependencies: external config, cwd snapshot, platform binary.
    #: ``present=False`` records "the target must provide this"; a missing
    #: ``present=True`` file refuses the bundle (card 355 item 4).
    dependencies: tuple[Mapping[str, object], ...] = ()

    def target_must_provide(self) -> tuple[Mapping[str, object], ...]:
        """Declarations the target side has to satisfy itself."""

        return tuple(item for item in self.dependencies if not item.get("present"))

    def as_dict(self) -> dict[str, object]:
        body: dict[str, object] = {
            "schema_version": self.schema_version,
            "protocol_version": self.protocol_version,
            "owner": self.owner,
            "actor": self.actor,
            "source_machine": self.source_machine,
            "created_at": self.created_at,
            "identity": dict(self.identity),
            "session_refs": list(self.session_refs),
            "grants": list(self.grants),
            "entries": [entry.as_dict() for entry in self.entries],
        }
        # Only written when set: a manifest that declares nothing keeps the
        # v2 body it had before this field existed (the seal covers whatever
        # body is written, so old bundles still verify).
        if self.source_epoch:
            body["source_epoch"] = self.source_epoch
        if self.credential_envelope:
            body["credential_envelope"] = self.credential_envelope
        if self.dependencies:
            body["dependencies"] = [dict(item) for item in self.dependencies]
        body[MANIFEST_DIGEST_FIELD] = _manifest_digest(body)
        return body

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "BundleManifest":
        if not isinstance(raw, Mapping):
            raise BundleManifestError("manifest must be an object")
        schema_version = int(raw.get("schema_version", -1))
        if schema_version != BUNDLE_SCHEMA_VERSION:
            raise BundleManifestError(
                f"unsupported schema_version: {schema_version}"
            )
        protocol_version = int(raw.get("protocol_version", -1))
        if protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
            raise BundleProtocolMismatch(
                "bundle protocol "
                f"{protocol_version} is not supported by this target "
                f"(supported: {list(SUPPORTED_PROTOCOL_VERSIONS)})"
            )
        for required in ("owner", "actor", "source_machine", "created_at"):
            value = raw.get(required)
            if not isinstance(value, str) or not value:
                raise BundleManifestError(f"{required} must be a non-empty string")
        entries_raw = raw.get("entries")
        if not isinstance(entries_raw, list):
            raise BundleManifestError("entries must be a list")
        entries: list[BundleEntry] = []
        seen: set[str] = set()
        for item in entries_raw:
            if not isinstance(item, Mapping):
                raise BundleManifestError("entry must be an object")
            entry = BundleEntry.from_dict(item)
            if entry.path in seen:
                raise BundleManifestError(f"duplicate entry: {entry.path!r}")
            seen.add(entry.path)
            entries.append(entry)
        identity_raw = raw.get("identity") or {}
        if not isinstance(identity_raw, Mapping):
            raise BundleManifestError("identity must be an object")
        # The identity bag is free-form, but it may not contradict the sealed
        # envelope fields: a bundle whose identity says another owner/actor is
        # a different agent wearing this one's manifest.
        for field_name, top_level in (
            ("owner", raw["owner"]),
            ("actor", raw["actor"]),
        ):
            claimed = identity_raw.get(field_name)
            if claimed is not None and str(claimed) != str(top_level):
                raise BundleManifestError(
                    f"identity.{field_name} ({claimed!r}) contradicts the "
                    f"manifest's own {field_name} ({top_level!r})"
                )
        source_epoch = raw.get("source_epoch") or ""
        if not isinstance(source_epoch, str):
            raise BundleManifestError("source_epoch must be a string digest")
        session_refs = raw.get("session_refs") or []
        grants = raw.get("grants") or []
        if not isinstance(session_refs, list) or not isinstance(grants, list):
            raise BundleManifestError("session_refs and grants must be lists")
        credential_envelope = raw.get("credential_envelope") or ""
        if not isinstance(credential_envelope, str):
            raise BundleManifestError("credential_envelope must be a string path")
        if credential_envelope:
            credential_envelope = _safe_relative(credential_envelope)
        dependencies_raw = raw.get("dependencies") or []
        if not isinstance(dependencies_raw, list):
            raise BundleManifestError("dependencies must be a list")
        # The seal is checked last: a structurally invalid manifest is a
        # malformed file, and saying so beats calling it "edited".  A manifest
        # that parses but no longer matches its seal was rewritten after
        # export -- including its ``owner``/``actor``, which nothing else
        # binds to the payload.
        sealed = raw.get(MANIFEST_DIGEST_FIELD)
        if not isinstance(sealed, str) or not sealed:
            raise BundleManifestError(
                f"manifest is not sealed: {MANIFEST_DIGEST_FIELD} is missing"
            )
        if sealed != _manifest_digest(raw):
            raise BundleTamperError(
                "manifest seal does not match its contents: the manifest was "
                "edited after export"
            )
        return cls(
            owner=str(raw["owner"]),
            actor=str(raw["actor"]),
            source_machine=str(raw["source_machine"]),
            created_at=str(raw["created_at"]),
            protocol_version=protocol_version,
            schema_version=schema_version,
            entries=tuple(entries),
            identity=dict(identity_raw),
            session_refs=tuple(str(item) for item in session_refs),
            grants=tuple(str(item) for item in grants),
            source_epoch=source_epoch,
            credential_envelope=credential_envelope,
            dependencies=_normalise_dependencies(dependencies_raw),
        )


def _payload_path(bundle_dir: Path, relative: str) -> Path:
    safe = _safe_relative(relative)
    payload_root = (bundle_dir / PAYLOAD_DIRNAME).resolve()
    candidate = payload_root / safe
    resolved = candidate.resolve()
    if resolved != payload_root and payload_root not in resolved.parents:
        raise BundlePathError(f"entry escapes the payload root: {relative!r}")
    if candidate.is_symlink() or resolved.is_symlink():
        raise BundlePathError(f"symlink entry is not allowed: {relative!r}")
    return candidate


def write_manifest(bundle_dir: os.PathLike[str] | str, manifest: BundleManifest) -> Path:
    dest = Path(bundle_dir) / MANIFEST_NAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        manifest.as_dict(), indent=2, sort_keys=True, ensure_ascii=False
    )
    dest.write_text(payload + "\n", encoding="utf-8")
    return dest


def load_manifest(bundle_dir: os.PathLike[str] | str) -> BundleManifest:
    path = Path(bundle_dir) / MANIFEST_NAME
    if not path.is_file():
        raise BundleManifestError(f"manifest not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BundleManifestError(f"manifest is not readable JSON: {error}") from error
    return BundleManifest.from_dict(raw)


def validate_bundle(
    bundle_dir: os.PathLike[str] | str,
    *,
    manifest: BundleManifest | None = None,
) -> BundleManifest:
    """Verify a bundle's payload against its manifest.

    Returns the parsed manifest on success; raises ``BundleError`` subclasses
    otherwise.  Never writes to the bundle.  ``manifest`` lets a caller that
    already took one sealed reading (``load_sealed_manifest``) validate the
    payload against *that* reading instead of re-opening the file.
    """

    root = Path(bundle_dir)
    if not root.is_dir():
        raise BundleError(f"bundle is not a directory: {root}")
    if manifest is None:
        manifest = load_manifest(root)

    payload_root = root / PAYLOAD_DIRNAME
    if not payload_root.is_dir():
        raise BundleManifestError("payload/ is missing")

    registered = {entry.path: entry for entry in manifest.entries}
    for relative in registered:
        _payload_path(root, relative)

    on_disk: dict[str, Path] = {}
    for relative, absolute in _iter_payload_files(payload_root):
        if absolute.is_symlink():
            raise BundlePathError(f"symlink payload entry: {relative!r}")
        on_disk[relative] = absolute

    extra = sorted(set(on_disk) - set(registered))
    if extra:
        raise BundleManifestError(
            f"payload has unregistered files: {extra[:5]}"
        )

    for relative, entry in registered.items():
        absolute = on_disk.get(relative)
        if absolute is None:
            raise BundleManifestError(f"manifest entry is missing on disk: {relative!r}")
        info = absolute.stat()
        if not stat.S_ISREG(info.st_mode):
            raise BundleManifestError(f"entry is not a regular file: {relative!r}")
        if info.st_size != entry.size:
            raise BundleTamperError(
                f"size mismatch for {relative!r}: "
                f"manifest={entry.size} actual={info.st_size}"
            )
        actual_mode = stat.S_IMODE(info.st_mode)
        if actual_mode != entry.mode:
            raise BundleTamperError(
                f"mode mismatch for {relative!r}: "
                f"manifest={oct(entry.mode)} actual={oct(actual_mode)}"
            )
        actual_digest = sha256_file(absolute)
        if actual_digest != entry.sha256:
            raise BundleTamperError(
                f"sha256 mismatch for {relative!r}: "
                f"manifest={entry.sha256} actual={actual_digest}"
            )

    # Declared files are checked after the payload: a bundle that claims a
    # credential envelope or a snapshotted dependency it does not carry is
    # refused here, so receive/land never has to decide what a missing
    # declaration means (card 355 item 4).
    if manifest.credential_envelope:
        _resolve_declared_path(
            root,
            manifest.credential_envelope,
            label="credential envelope",
            registered=set(registered),
        )
    for item in manifest.dependencies:
        if item.get("present"):
            _resolve_declared_path(
                root,
                str(item["path"]),
                label=f"dependency {item['kind']!r}",
                registered=set(registered),
            )

    return manifest


def assert_bundle_identity(
    manifest: BundleManifest,
    *,
    expect_owner: str | None = None,
    expect_actor: str | None = None,
    expect_epoch: str | None = None,
) -> None:
    """Refuse when the bundle's own claim is not the one the caller asserted.

    The manifest names who a bundle came from, but nothing else in the
    pre-receive gate compares that claim against what the operator expected --
    a wrong-owner bundle is otherwise accepted and landed.  Card 355 item 4
    asks a production entry point to refuse exactly that.
    """

    if expect_owner is not None and manifest.owner != expect_owner:
        raise BundleOwnerMismatch(
            f"bundle carries owner {manifest.owner!r}, expected {expect_owner!r}"
        )
    if expect_actor is not None and manifest.actor != expect_actor:
        raise BundleActorMismatch(
            f"bundle carries actor {manifest.actor!r}, expected {expect_actor!r}"
        )
    if expect_epoch is not None:
        if not manifest.source_epoch:
            raise BundleEpochMismatch(
                "the bundle carries no source epoch (it was exported without "
                "an incarnation); refusing to treat that as a match"
            )
        wanted = _epoch_digest(expect_epoch)
        if manifest.source_epoch != wanted:
            raise BundleEpochMismatch(
                f"bundle came from incarnation {manifest.source_epoch!r}, "
                f"expected {wanted!r}"
            )


# ---------------------------------------------------------------------------
# Path helper shared with the export half (``._export`` imports this one).
# ---------------------------------------------------------------------------


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
