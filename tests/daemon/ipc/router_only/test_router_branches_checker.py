"""The branch reader itself: what it collects, what it ignores, what it refuses.

Decision: daemon-first-z4b-router-only-refusal-20261117.  Synthetic source only.
"""

from __future__ import annotations

import pytest

from .router_branches import UnresolvedOperand, branch_names, disagreement

SOURCE = '''
class Mixin:
    def handle(self, method, params):
        if method == "a.one":
            return 1
        if method in {"b.one", "b.two"}:
            return 2
        if method in ("c.one", LISTED):
            return 3
        if "d." + "one" == method:
            return 4
        if method == ONE:
            return 5
        if method != "never.collected":
            pass
        if method not in {"also.never"}:
            pass
        if method in REGISTRY:
            return 6
        if method.startswith("e."):
            return 7
        if other == "not.a.method":
            return 8
'''
NAMESPACE = {"LISTED": ("c.two",), "ONE": "f.one"}


def _names(source: str = SOURCE) -> set[str]:
    return branch_names(source, "handle", NAMESPACE, gates={"REGISTRY"})


def test_every_positive_comparison_form_is_collected() -> None:
    assert _names() == {
        "a.one", "b.one", "b.two", "c.one", "c.two", "d.one", "f.one",
    }


def test_a_gained_branch_is_collected() -> None:
    gained = SOURCE + '        if method == "g.new":\n            return 9\n'
    assert _names(gained) - _names() == {"g.new"}


def test_a_lost_branch_is_no_longer_collected() -> None:
    lost = SOURCE.replace('if method == "a.one":', "if method == ONE:")
    assert _names() - _names(lost) == {"a.one"}


@pytest.mark.parametrize(
    "line",
    [
        "if method == compute():",
        "if method in UNKNOWN_NAME:",
        "if 1 < method == 'x':",
    ],
)
def test_an_operand_it_cannot_resolve_is_an_error_not_a_skip(line: str) -> None:
    with pytest.raises(UnresolvedOperand):
        _names(SOURCE + f"        {line}\n            return 0\n")


def test_a_missing_function_is_an_error() -> None:
    with pytest.raises(UnresolvedOperand):
        branch_names(SOURCE, "absent", NAMESPACE)


def test_disagreement_reports_both_directions() -> None:
    assert disagreement({"x", "y", "ping"}, {"x"}, {"y"}) == ([], [])
    assert disagreement({"x", "y"}, set(), {"y", "z"}) == (["x"], ["z"])
