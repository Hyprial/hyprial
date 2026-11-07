"""Transform engines for the staged refactor toolchain (S1-S6)."""

from __future__ import annotations

from .common import (
    Diagnostic,
    ERROR,
    ModuleIndex,
    MoveRecord,
    Rule,
    RuleOutcome,
    STAGES,
    ToolError,
    WARNING,
    Workspace,
    decode_source,
    encode_source,
    normalize_rel,
    sha256_hex,
    stable_json_bytes,
    stable_write,
    unified_diff,
)

__all__ = [
    "Diagnostic",
    "ERROR",
    "ModuleIndex",
    "MoveRecord",
    "Rule",
    "RuleOutcome",
    "STAGES",
    "ToolError",
    "WARNING",
    "Workspace",
    "decode_source",
    "encode_source",
    "normalize_rel",
    "sha256_hex",
    "stable_json_bytes",
    "stable_write",
    "unified_diff",
]
