"""Nothing under src reaches the removed user-to-IM binding chain (R3, design #2950 8.5).

The identity resolver, its override store, the binding publication and
verification chain, the account half of the user store and the lazy store the
Lark worker once held are deleted.  Two scans keep them deleted:

- an AST scan of every module under ``src/hyprial``: no import names a deleted
  module, so a re-added caller fails here before it fails at import time;
- a text scan for the files and keys only that chain read
  (``identity.sqlite3``, the two org-binding cache files, ``identityClaims``,
  ``identityBinding``, ``user_accounts``) and the vocabulary that named an IM
  account's owner (``hyprialOwner``, ``legacy-identities``).  A home that still
  holds those files is left alone because no code names them.

The self-test runs both scanners on synthetic source in a temporary directory.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SOURCE = ROOT / "src" / "hyprial"

DELETED_MODULES = (
    "hyprial.daemon.impl.identity.resolver",
    "hyprial.daemon.impl.identity.store",
    "hyprial.daemon.impl.identity.models",
    "hyprial.daemon.impl.identity.ipc",
    "hyprial.daemon.impl.identity.bindings",
    "hyprial.daemon.impl.identity.audit",
    "hyprial.daemon.impl.identity.mutation",
    "hyprial.daemon.impl.application.messaging.identity_binding",
    "hyprial.daemon.impl.request_catalog.identity.audit",
    "hyprial.identity.impl.users.store._lazy",
)
DELETED_TOKENS = (
    "identity.sqlite3",
    "verified-org-bindings",
    "identity-binding-suppression",
    "identityClaims",
    "identityBinding",
    "user_accounts",
    "hyprialOwner",
    "legacy-identities",
    "LazyUserStore",
)


def _module_name(path: Path, root: Path) -> str:
    relative = path.relative_to(root.parent).with_suffix("")
    parts = relative.parts[:-1] if relative.name == "__init__" else relative.parts
    return ".".join(parts)


def _imported(path: Path, root: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = _module_name(path, root)
    if path.name != "__init__.py":
        package = package.rpartition(".")[0]
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = package.split(".")
                anchor = anchor[: len(anchor) - (node.level - 1)]
                base = ".".join([*anchor, base] if base else anchor)
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
    return names


def import_findings(root: Path) -> list[str]:
    findings = []
    for path in sorted(root.rglob("*.py")):
        for name in sorted(_imported(path, root)):
            if any(name == gone or name.startswith(gone + ".") for gone in DELETED_MODULES):
                findings.append(f"{path.relative_to(root.parent)} imports {name}")
    return findings


def token_findings(root: Path) -> list[str]:
    findings = []
    for path in sorted(root.rglob("*")):
        if path.suffix not in {".py", ".md", ".ts", ".json"} or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for token in DELETED_TOKENS:
            if token in text:
                findings.append(f"{path.relative_to(root.parent)} names {token}")
    return findings


def test_no_source_module_imports_a_deleted_module() -> None:
    assert import_findings(SOURCE) == []


def test_no_source_file_names_a_removed_binding_file_key_or_owner_field() -> None:
    assert token_findings(SOURCE) == []


def test_scanners_find_imports_and_tokens_in_synthetic_source(tmp_path: Path) -> None:
    root = tmp_path / "hyprial"
    package = root / "daemon" / "impl" / "identity"
    package.mkdir(parents=True)
    (root / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("from .resolver import X\n", encoding="utf-8")
    (package / "clean.py").write_text("import json\n", encoding="utf-8")
    (root / "reader.py").write_text(
        "from hyprial.daemon.impl.identity.mutation.overrides import Y\n"
        "PATH = 'state/identity.sqlite3'\n",
        encoding="utf-8",
    )

    assert import_findings(root) == [
        "hyprial/daemon/impl/identity/__init__.py imports hyprial.daemon.impl.identity.resolver",
        "hyprial/daemon/impl/identity/__init__.py imports hyprial.daemon.impl.identity.resolver.X",
        "hyprial/reader.py imports hyprial.daemon.impl.identity.mutation.overrides",
        "hyprial/reader.py imports hyprial.daemon.impl.identity.mutation.overrides.Y",
    ]
    assert token_findings(root) == ["hyprial/reader.py names identity.sqlite3"]
