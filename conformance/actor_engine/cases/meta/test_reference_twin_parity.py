# Decision record (file header; no inline comments by repository rule):
# - The conformance reference engine (the executable oracle) is a twin copy of
#   the product reference engine. They must stay line-identical in ORDER,
#   after normalizing the product import path, except for an explicit
#   allowlist of consecutive test-injection blocks: the test-only keyed
#   pump-start failure injection (flag init, failure branch) and the
#   test-only registry readout. the fourth review (2026-11-20) verified this by hand;
#   the fifth review required the comparison to be order-sensitive (a multiset diff
#   cannot see swapped lines or swapped logic blocks).
# - The check is a pure function over the two line lists so the self-test
#   can prove it fails on a swapped-line mutation; every opcode must be
#   'equal' or an insert of one allowlisted consecutive block, and each
#   allowlisted block is consumed at most once.
"""Twin parity: conformance reference oracle == product reference engine."""

from __future__ import annotations

import difflib
from pathlib import Path

SUITE = Path(__file__).resolve().parents[2]
ROOT = Path(__file__).resolve().parents[4]
TWIN = SUITE / "backends" / "_reference_engine.py"
PRODUCT = ROOT / "src/hyprial/kernel/impl/actor_runtime/backends/_reference_engine.py"
PRODUCT_IMPORT = "from ._reference_pools import Pool"
TWIN_IMPORT = "from backends._reference_pools import Pool"

ALLOWED_TWIN_ONLY: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("        self._test_pump_start_failure = False",),
        "test-only injection flag initialised in Engine.__init__",
    ),
    (
        (
            "                if self._test_pump_start_failure:",
            "                    self._test_pump_start_failure = False",
            '                    raise RuntimeError("injected pump start failure")',
        ),
        "test-only keyed pump-start failure branch inside the install lock",
    ),
    (
        (
            "",
            "    def _test_registry_counts(self) -> tuple[int, int, int]:",
            "        return len(self._records), len(self._keys), 0",
        ),
        "test-only actor/key/pump registry readout",
    ),
)


def normalized_product_lines(product_text: str) -> list[str]:
    assert PRODUCT_IMPORT in product_text
    return product_text.replace(PRODUCT_IMPORT, TWIN_IMPORT, 1).splitlines()


def parity_violations(product_lines: list[str], twin_lines: list[str]) -> list[str]:
    """Order-sensitive diff: only allowlisted consecutive twin-only blocks."""

    allowed = [list(hunk) for hunk, _ in ALLOWED_TWIN_ONLY]
    violations: list[str] = []
    matcher = difflib.SequenceMatcher(None, product_lines, twin_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        removed = product_lines[i1:i2]
        if removed:
            violations.append(f"product-only lines {i1 + 1}:{i2}: {removed}")
            continue
        inserted = twin_lines[j1:j2]
        match = next((k for k, lines in enumerate(allowed) if lines == inserted), None)
        if match is None:
            violations.append(f"twin-only lines {j1 + 1}:{j2} match no allowlisted block: {inserted}")
        else:
            allowed.pop(match)
    if allowed:
        violations.append(f"allowlisted blocks missing from the twin: {allowed}")
    return violations


def test_conformance_twin_equals_product_reference_except_allowlist() -> None:
    twin_lines = TWIN.read_text(encoding="utf-8").splitlines()
    product_lines = normalized_product_lines(PRODUCT.read_text(encoding="utf-8"))
    assert not parity_violations(product_lines, twin_lines), (
        "twin drifted from the product reference beyond the allowlist:\n"
        + "\n".join(parity_violations(product_lines, twin_lines))
    )


def test_parity_check_fails_when_two_lines_are_swapped() -> None:
    product_lines = normalized_product_lines(PRODUCT.read_text(encoding="utf-8"))
    twin_lines = TWIN.read_text(encoding="utf-8").splitlines()
    assert not parity_violations(product_lines, twin_lines)
    first = product_lines.index('RUNNING = "running"')
    swapped = list(product_lines)
    swapped[first], swapped[first + 1] = swapped[first + 1], swapped[first]
    assert parity_violations(swapped, twin_lines), (
        "a swapped-line mutation must fail the parity check; a multiset diff "
        "would pass it"
    )
