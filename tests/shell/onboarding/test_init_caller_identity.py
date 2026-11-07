"""Isolation callers that run init must not inherit a developer's owner."""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    ("relative", "assignment"),
    [
        ("contract/cli-blackbox/run.py", 'env["HYPRIAL_OWNER"] = "e2e"'),
        (
            "contract/e2e/run-tui-attach-manual.py",
            '"HYPRIAL_OWNER": "e2e",',
        ),
        (
            "contract/e2e/run-upgrade-continuity.py",
            '"HYPRIAL_OWNER": "e2e",',
        ),
    ],
)
def test_fresh_home_init_callers_assign_the_ci_owner(
    relative: str, assignment: str
) -> None:
    source = (_ROOT / relative).read_text(encoding="utf-8")
    if relative == "contract/e2e/run-upgrade-continuity.py":
        assert "return _scenario_impl.main(" in source
        source = (_ROOT / "contract/e2e/_upgrade_continuity/scenario.py").read_text(encoding="utf-8")

    assert source.count(assignment) == 1
    assert 'setdefault("HYPRIAL_OWNER"' not in source
