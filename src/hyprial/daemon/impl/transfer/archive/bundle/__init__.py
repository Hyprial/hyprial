"""AT06 bundle package: the manifest model, validation, and the export half.

Split verbatim from the original ``archive/bundle.py`` so both halves stay
inside the structural limits.  The package keeps the module's import path and
re-exports the whole surface callers already relied on, including the private
path helpers other modules import by name.
"""
from __future__ import annotations

from ._base import (
    BUNDLE_PROTOCOL_VERSION,
    BUNDLE_SCHEMA_VERSION,
    MANIFEST_DIGEST_FIELD as MANIFEST_DIGEST_FIELD,
    MANIFEST_NAME as MANIFEST_NAME,
    PAYLOAD_DIRNAME as PAYLOAD_DIRNAME,
    SUPPORTED_PROTOCOL_VERSIONS,
    BundleActorMismatch,
    BundleDeclarationMissing,
    BundleDestinationNotEmpty,
    BundleDestinationOverlapsSource,
    BundleEntry,
    BundleEpochMismatch,
    BundleError,
    BundleManifest,
    BundleManifestError,
    BundleOwnerMismatch,
    BundlePathError,
    BundleProtocolMismatch,
    BundleSourceChanged,
    BundleSourceLive,
    BundleTamperError,
    assert_bundle_identity,
    load_manifest,
    sha256_file,
    validate_bundle,
    write_manifest as write_manifest,
)
from ._base import (
    _CHUNK as _CHUNK,
    _epoch_digest as _epoch_digest,
    _excluded_relative as _excluded_relative,
    _is_platform_binary as _is_platform_binary,
    _iter_payload_files as _iter_payload_files,
    _manifest_digest as _manifest_digest,
    _normalise_dependencies as _normalise_dependencies,
    _payload_path as _payload_path,
    _resolve_declared_path as _resolve_declared_path,
    _safe_relative as _safe_relative,
    _within as _within,
)
from ._export import export_bundle, live_daemon_socket
from ._export import (
    _canonical as _canonical,
    _is_ancestor as _is_ancestor,
    _is_empty_dir as _is_empty_dir,
    _require_unchanged_source as _require_unchanged_source,
    _write_extra_payload_files as _write_extra_payload_files,
)

__all__ = [
    "BUNDLE_SCHEMA_VERSION",
    "BUNDLE_PROTOCOL_VERSION",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "BundleError",
    "BundleManifestError",
    "BundlePathError",
    "BundleDestinationOverlapsSource",
    "BundleDestinationNotEmpty",
    "BundleProtocolMismatch",
    "BundleTamperError",
    "BundleOwnerMismatch",
    "BundleActorMismatch",
    "BundleDeclarationMissing",
    "BundleEpochMismatch",
    "BundleSourceLive",
    "BundleSourceChanged",
    "live_daemon_socket",
    "BundleEntry",
    "BundleManifest",
    "export_bundle",
    "validate_bundle",
    "assert_bundle_identity",
    "load_manifest",
    "sha256_file",
]
