"""Write-scope policy for every transform engine.

Defaults (deny by policy, not by accident):

- ``docs/notes/`` (the WORM evidence library) is read-only in full, including
  ``.py`` files. Approved WORM relocation is a byte-preserving move pinned by
  hash, never a content rewrite.
- ``src/gui/`` is the separate GUI product line: its ``.py`` files are not
  rewritten by product import/symbol rules by default, and its non-Python
  files are not claimed by Python rules at all. The IR-6 consumer
  ``src/gui/integration/pac-service.py`` therefore stays untouched unless the
  main model coordinates an explicit consumer rule.
- Everything else defaults to writable, with ``unclassified`` top-level trees
  reported so vendors and third-party code cannot be swept up silently.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass

from .common import sha256_hex

WORM_PREFIXES = ("docs/notes/",)
GUI_PREFIX = "src/gui/"
# Non-overridable protected scopes: tool metadata, product build/lock files,
# third-party trees and the GUI line's own tooling. No policy flag can make
# these writable.
PROTECTED_PREFIXES = (
    ".git/",
    ".forgejo/",
    ".github/",
    ".agents/",
    ".claude/",
    "scripts/verify/gui-refactor/",
)
PROTECTED_EXACT = frozenset(
    {
        "pyproject.toml",
        "uv.lock",
        "uv.toml",
        "VERSION",
        "README.md",
        "AGENTS.md",
        "CLAUDE.md",
        "GLOSSARY.md",
        "ROADMAP.md",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "LICENSE",
    }
)
# Third-party / unknown trees are denied by default (deny-by-default):
# rewriting them requires the main model to extend the allowlist explicitly.
UNKNOWN_TREE_PREFIXES = ("vendor/", "thirdparty/", "external/", "node_modules/")
# The positive allowlist of top-level scopes the Python line may rewrite.
KNOWN_WRITABLE_BUCKETS = {
    "src/hyprial": "product",
    "tests": "tests",
    "scripts": "scripts",
    "contract": "contract",
    "app": "desktop",
    "docs": "docs",
}


@dataclass(frozen=True)
class ScopePolicy:
    writable_extra: tuple[str, ...] = ()
    read_only_extra: tuple[str, ...] = ()

    def fingerprint(self) -> str:
        payload = "\n".join(
            [
                *sorted(self.writable_extra),
                "---",
                *sorted(self.read_only_extra),
            ]
        )
        return sha256_hex(payload.encode("utf-8"))


def default_policy() -> ScopePolicy:
    return ScopePolicy()


def _matches(rel: str, prefixes: tuple[str, ...]) -> bool:
    cleaned = posixpath.normpath(rel)
    return any(
        cleaned == prefix.rstrip("/")
        or cleaned.startswith(prefix if prefix.endswith("/") else prefix + "/")
        for prefix in prefixes
    )


def classify_path(rel: str, policy: ScopePolicy) -> dict:
    """Return {writable, reason, bucket} for a repo-relative path.

    Deny by default: anything outside the explicit writable allowlist is
    refused; protected scopes (tool metadata, product build files, .git,
    WORM, GUI, third-party) cannot be overridden by policy flags.
    """
    cleaned = posixpath.normpath(rel)
    if cleaned in PROTECTED_EXACT or cleaned.startswith(".git"):
        return {"writable": False, "reason": "protected-product-file", "bucket": "protected"}
    if _matches(cleaned, PROTECTED_PREFIXES):
        return {"writable": False, "reason": "protected-scope", "bucket": "protected"}
    if _matches(cleaned, UNKNOWN_TREE_PREFIXES):
        return {
            "writable": False,
            "reason": "unclassified-not-authorized",
            "bucket": "unclassified",
        }
    if _matches(cleaned, policy.read_only_extra):
        return {"writable": False, "reason": "policy-read-only-extra", "bucket": "extra"}
    # non-overridable floor BEFORE any flag: WORM (incl. .py), GUI, protected
    # tooling/product files and unknown trees can never be made writable by
    # writable_extra. A future single-file cross-line exception requires its
    # own explicit main-model decision, not a policy flag.
    if _matches(cleaned, WORM_PREFIXES):
        return {"writable": False, "reason": "worm-read-only", "bucket": "worm"}
    if _matches(cleaned, (GUI_PREFIX,)):
        if cleaned.endswith(".py"):
            return {"writable": False, "reason": "gui-python-frozen", "bucket": "gui"}
        return {"writable": False, "reason": "gui-tree-python-rules-only", "bucket": "gui"}
    if _matches(cleaned, policy.writable_extra):
        # flags may only re-enable paths inside already-writable scopes
        # (e.g. un-freeze one file); they cannot cross the floor above
        return {"writable": True, "reason": "policy-writable-extra", "bucket": "extra"}
    for prefix, bucket in KNOWN_WRITABLE_BUCKETS.items():
        if _matches(cleaned, (prefix,)):
            return {"writable": True, "reason": "default", "bucket": bucket}
    return {"writable": False, "reason": "unclassified-not-authorized", "bucket": "unclassified"}


def scope_report(policy: ScopePolicy, paths: list[str]) -> dict:
    """Inclusion/exclusion decisions for a concrete path set."""
    writable: list[str] = []
    read_only: list[dict] = []
    unclassified: list[str] = []
    for rel in sorted(paths):
        decision = classify_path(rel, policy)
        if decision["bucket"] == "unclassified":
            unclassified.append(rel)
        if decision["writable"]:
            writable.append(rel)
        else:
            read_only.append({"path": rel, "reason": decision["reason"]})
    return {
        "writable": writable,
        "read_only": read_only,
        "unclassified": unclassified,
        "denied_by_default": [
            row["path"] for row in read_only if row["reason"] == "unclassified-not-authorized"
        ],
        "policy_fingerprint": policy.fingerprint(),
    }


__all__ = [
    "GUI_PREFIX",
    "ScopePolicy",
    "WORM_PREFIXES",
    "classify_path",
    "default_policy",
    "scope_report",
]
