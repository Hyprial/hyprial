"""Installer error-code registry for ``hyprial install`` failures.

One named constant per installer error code -- every code that reaches the
public CLI error envelope (``--json`` prints it as the top-level ``code``
field) and that tests, the e2e runner, or operator tooling compare against
(``json.loads(stdout)["code"] == "INSTALL_CATALOG_BEHIND"`` is already
load-bearing in the suite).  Sender and comparison sites import the SAME
constant, following the ``ipc_errors`` precedent: a code a caller branches
on must never be typed twice as a string literal.

Rules:

- Values are frozen once shipped.  Old receipts, captured logs and pinned
  e2e expectations carry these exact strings, so renaming a value is a
  break; add a new code instead and keep recognising the old one.
- Membership line: an INSTALL_* code enters this registry the moment it
  crosses into a machine-readable decision surface -- the ``--json``
  envelope another process parses.  Codes minted by the installers are
  born across that line, so every one of them lives here.
- ``tests/test_install_error_code_registry.py`` enforces both directions:
  the registry matches a frozen membership fixture, and no file under
  ``src/hyprial`` spells ``INSTALL_*`` as a string literal outside this
  module (docstrings excepted -- history notes may name retired codes such
  as ``INSTALL_UPDATE_UNSUPPORTED`` without importing them).
"""

from __future__ import annotations

# -- release artifact pipeline -------------------------------------------
INSTALL_ARTIFACT_UNAVAILABLE = "INSTALL_ARTIFACT_UNAVAILABLE"
INSTALL_ARTIFACT_DIGEST_MISMATCH = "INSTALL_ARTIFACT_DIGEST_MISMATCH"
INSTALL_ARTIFACT_MALFORMED = "INSTALL_ARTIFACT_MALFORMED"
INSTALL_ARTIFACT_CHANGED = "INSTALL_ARTIFACT_CHANGED"

# -- catalog / registry document -----------------------------------------
INSTALL_REGISTRY_INVALID = "INSTALL_REGISTRY_INVALID"
INSTALL_REGISTRY_UNAVAILABLE = "INSTALL_REGISTRY_UNAVAILABLE"
INSTALL_CATALOG_INVALID = "INSTALL_CATALOG_INVALID"
INSTALL_CATALOG_BEHIND = "INSTALL_CATALOG_BEHIND"

# -- app install manifest (hyprial.install/v1|v2) -------------------------
INSTALL_MANIFEST_INVALID = "INSTALL_MANIFEST_INVALID"

# -- installed state (receipt / source manifest) --------------------------
INSTALL_STATE_INVALID = "INSTALL_STATE_INVALID"
INSTALL_SOURCE_MISSING = "INSTALL_SOURCE_MISSING"
INSTALL_SOURCE_DIRTY = "INSTALL_SOURCE_DIRTY"
INSTALL_SOURCE_CONFLICT = "INSTALL_SOURCE_CONFLICT"

# -- upstream / execution --------------------------------------------------
INSTALL_GIT_FAILED = "INSTALL_GIT_FAILED"
INSTALL_SCRIPT_FAILED = "INSTALL_SCRIPT_FAILED"
INSTALL_DECLINED = "INSTALL_DECLINED"
INSTALL_UPDATE_UNSUPPORTED = "INSTALL_UPDATE_UNSUPPORTED"

#: Every installer error code.  Membership is frozen by test; a change here
#: must be deliberate and reviewed as a compatibility change.
ALL_CODES: frozenset[str] = frozenset(
    value
    for name, value in globals().items()
    if name.isupper() and isinstance(value, str)
)
