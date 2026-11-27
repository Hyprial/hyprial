"""The service grant files explain themselves without pointing outside the repository.

Decision (service grant integration fix, 2026-11-27): a file header's
decision notes say what was decided and why in their own words. They name no
working document, review, ruling or construction slice; where the design is
needed they cite docs/design/service-grant-2026-11-27.md by section (Data,
Trust, Exposing side, Sealing, Recipient side).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SOURCES = (
    "src/hyprial/daemon/impl/service_grants",
    "src/hyprial/daemon/impl/org/network/grant_docs",
    "src/hyprial/daemon/impl/org/network/grant_roots",
    "src/hyprial/daemon/impl/org/network/device_key",
    "src/hyprial/daemon/impl/forwarding/policy.py",
    "src/hyprial/daemon/impl/forwarding/supervisor.py",
    "src/hyprial/daemon/impl/service_connect/derived.py",
    "src/hyprial/daemon/impl/application/netendpoints",
    "src/hyprial/identity/impl/sealed_envelope",
    "src/hyprial/shell/impl/cli/commands/service/grants.py",
    "tests/daemon/service_grants",
    "tests/identity/sealed_envelope",
    "tests/integration/service_connect",
)
TERMS = re.compile(
    r"plan[- ]?v2|\bbrief\b|\bcoordinator\b|\bcritic|s4py|S4-py|\bS[1-5]\b(?! fix)|\bS[1-5] fix"
)


def _files() -> list[Path]:
    found: list[Path] = []
    for name in SOURCES:
        path = ROOT / name
        found.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path])
    return found


def _header(text: str) -> str:
    lines = text.split("\n")
    index = 0
    while index < len(lines) and (lines[index].startswith("#") or not lines[index].strip()):
        index += 1
    head = "\n".join(lines[:index])
    rest = "\n".join(lines[index:])
    for quote in ('"""', "'''"):
        if rest.startswith(quote):
            end = rest.find(quote, 3)
            return head + "\n" + rest[: end + 3]
    return head


@pytest.mark.parametrize("path", _files(), ids=lambda path: str(path.relative_to(ROOT)))
def test_the_header_names_nothing_outside_the_repository(path: Path) -> None:
    assert TERMS.findall(_header(path.read_text())) == []
