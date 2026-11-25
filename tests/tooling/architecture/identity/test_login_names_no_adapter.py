"""Login-like flows and their settings writers do not name an adapter or its binding.

Owner decision 2026-11-18: an adapter's binding must not appear in the code
of login or similar flows.  The scanned roots are login, the CLI login and
``init`` commands, the CLI ``org`` commands (join), the invite helpers and the
identity onboarding package.  Each file is scanned for the binding vocabulary
and, case-insensitively, for the adapter name.  Exemptions are exact literals
in exact files and nothing wider; no file is exempt today.  The self-tests run
the scanner on synthetic source in a temporary directory; the exemption
mechanism itself is exercised with a table built inside the test, so no test
depends on a production exemption existing.

Widened 2026-11-18 (tier 1 of the user-to-IM binding cleanup): the daemon,
identity and shell roots that already contained none of the tokens below are
scanned too, so the binding and the adapter name cannot come back there.  Only
roots that are clean under exactly these tokens and need no exemption were
added; the roots that still carry the binding (daemon identity, application,
org, orgfs, request catalog, agent templates, autoupdate, identity org
directory and users, the CLI user, squire and common commands) wait for the
behaviour-changing removal tracked in PR #2950.

Widened again by R3 of that removal (the identity resolver and the binding
proof chain are deleted): the daemon identity package, the identity request
catalog, the CLI ``user`` commands, the identity org directory model, the org
network package and OrgFS storage are scanned, and the two ``"identityBinding",``
residue exemptions are gone because logout and the identity record no longer
name the key.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SOURCE = ROOT / "src" / "hyprial"
LOGIN_ROOTS = (
    "shell/impl/login",
    "shell/impl/cli/commands/login",
    "shell/impl/cli/commands/init",
    "shell/impl/cli/commands/org",
    "shell/impl/invites",
    "identity/impl/onboarding",
)
WIDENED_ROOTS = (
    "daemon/impl/lifecycle",
    "daemon/impl/lifecycle_manager",
    "daemon/impl/onboarding",
    "daemon/impl/conversations",
    "daemon/impl/delivery",
    "daemon/impl/session_actor",
    "daemon/impl/transport",
    "daemon/impl/dispatch",
    "daemon/impl/forwarding",
    "identity/impl/org_membership",
    "identity/impl/device",
    "identity/impl/authorization",
    "identity/impl/user_key",
    "shell/impl/gui",
    "shell/impl/request_http",
    "shell/impl/http_contract",
    "shell/impl/daemon_wiring",
    "shell/impl/cli/commands/daemon",
    "shell/impl/cli/commands/message",
    "shell/impl/cli/commands/gui",
    "shell/impl/cli/commands/config",
    "shell/impl/cli/commands/host",
    "daemon/impl/identity",
    "daemon/impl/request_catalog/identity",
    "shell/impl/cli/commands/user",
    "identity/impl/org_directory",
    "daemon/impl/org/network",
    "daemon/impl/orgfs/storage",
)
SCANNED_ROOTS = LOGIN_ROOTS + WIDENED_ROOTS
BINDING_TOKENS = (
    "binding_proof",
    "identity_binding",
    "identityBinding",
    "binding-device",
    "binding-proof-failed",
    "BINDING_ASSERTION",
    "binding_profile",
    "BINDING_PROOF",
    "BINDING_ACCOUNT_MISMATCH",
)
ADAPTER_TOKENS = ("lark", "Lark", "LARK", "oauth_Lark_unionId")
EXEMPT_LITERALS: Mapping[str, tuple[str, ...]] = {
}


def scan(
    base: Path, roots: tuple[str, ...], exempt: Mapping[str, tuple[str, ...]]
) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    for root in roots:
        for path in sorted((base / root).rglob("*.py")):
            relative = path.relative_to(base).as_posix()
            text = path.read_text(encoding="utf-8")
            for literal in exempt.get(relative, ()):
                text = text.replace(literal, "")
            hits.extend((relative, token) for token in BINDING_TOKENS if token in text)
            if "lark" in text.lower():
                hits.append((relative, "lark"))
    return hits


def test_login_like_flows_name_neither_binding_nor_adapter() -> None:
    assert scan(SOURCE, SCANNED_ROOTS, EXEMPT_LITERALS) == []


def test_every_scanned_root_exists_and_has_sources() -> None:
    for root in SCANNED_ROOTS:
        assert list((SOURCE / root).rglob("*.py")), root


@pytest.mark.parametrize(("relative", "literals"), sorted(EXEMPT_LITERALS.items()))
def test_each_exemption_is_used_exactly_once_per_literal(
    relative: str, literals: tuple[str, ...]
) -> None:
    text = (SOURCE / relative).read_text(encoding="utf-8")
    for literal in literals:
        assert text.count(literal) == 1, (relative, literal)


def _plant(base: Path, root: str, name: str, content: str) -> None:
    target = base / root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


@pytest.mark.parametrize("root", SCANNED_ROOTS)
@pytest.mark.parametrize("token", BINDING_TOKENS + ADAPTER_TOKENS)
def test_the_scanner_fails_on_each_forbidden_term_in_each_root(
    tmp_path: Path, root: str, token: str
) -> None:
    _plant(tmp_path, root, "planted.py", f'value = "{token}"\n')
    hits = scan(tmp_path, SCANNED_ROOTS, {})
    assert (f"{root}/planted.py", token if token in BINDING_TOKENS else "lark") in hits


def test_the_widened_roots_are_scanned_and_each_detects_a_planted_term(
    tmp_path: Path,
) -> None:
    assert set(WIDENED_ROOTS) <= set(SCANNED_ROOTS)
    assert len(set(SCANNED_ROOTS)) == len(SCANNED_ROOTS)
    for index, root in enumerate(WIDENED_ROOTS):
        token = BINDING_TOKENS[index % len(BINDING_TOKENS)]
        _plant(tmp_path, root, "planted.py", f'value = "{token}"\n')
        _plant(tmp_path, root, "planted_adapter.py", 'value = "Lark"\n')
    hits = scan(tmp_path, SCANNED_ROOTS, {})
    for index, root in enumerate(WIDENED_ROOTS):
        token = BINDING_TOKENS[index % len(BINDING_TOKENS)]
        assert (f"{root}/planted.py", token) in hits
        assert (f"{root}/planted_adapter.py", "lark") in hits


def test_the_scanner_passes_clean_source(tmp_path: Path) -> None:
    for root in SCANNED_ROOTS:
        _plant(tmp_path, root, "clean.py", 'value = "login"\n')
    assert scan(tmp_path, SCANNED_ROOTS, {}) == []


def test_the_production_exemption_table_is_empty() -> None:
    assert EXEMPT_LITERALS == {}


def test_exemptions_cover_only_their_exact_literals_in_their_exact_files(
    tmp_path: Path,
) -> None:
    exempt_file = "identity/impl/onboarding/logout.py"
    literal = '"identityBinding",'
    synthetic: Mapping[str, tuple[str, ...]] = {exempt_file: (literal,)}
    _plant(tmp_path, "identity/impl/onboarding", "logout.py",
           'a = "identityBinding",\n')
    assert scan(tmp_path, SCANNED_ROOTS, synthetic) == []
    _plant(tmp_path, "identity/impl/onboarding", "logout.py",
           'a = "identityBinding",\nb = "identityBinding"\n')
    assert scan(tmp_path, SCANNED_ROOTS, synthetic) == [
        (exempt_file, "identityBinding")
    ]
    _plant(tmp_path, "identity/impl/onboarding", "logout.py",
           'a = "identityBinding",\n')
    _plant(tmp_path, "identity/impl/onboarding", "other.py",
           'a = "identityBinding",\n')
    assert scan(tmp_path, SCANNED_ROOTS, synthetic) == [
        ("identity/impl/onboarding/other.py", "identityBinding")
    ]


def test_the_residue_lists_have_no_binding_exemption_any_more(
    tmp_path: Path,
) -> None:
    for name in ("logout.py", "identity_record.py"):
        _plant(tmp_path, "identity/impl/onboarding", name, 'FIELDS = (\n    "identityBinding",\n)\n')
    assert scan(tmp_path, SCANNED_ROOTS, EXEMPT_LITERALS) == [
        ("identity/impl/onboarding/identity_record.py", "identityBinding"),
        ("identity/impl/onboarding/logout.py", "identityBinding"),
    ]
