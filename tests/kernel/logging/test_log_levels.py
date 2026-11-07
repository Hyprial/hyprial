"""``warn`` is the level name; ``"warning"`` raises at the call site.

``hyprial.log`` accepts ``debug`` / ``info`` / ``warn`` / ``error``.  Three
daemon call sites spelled it ``"warning"``, which turns the *error-handling*
path that was supposed to log (an unusable capability grant, a container
cleanup leftover) into a ``ValueError``.  Guard both halves: the level set
the logger actually enforces, and every literal level handed to a bound
``_log``.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / 'src/hyprial'
LEVELS = {"debug", "info", "warn", "error"}


def test_the_logger_does_not_know_a_warning_level() -> None:
    from hyprial.kernel.impl import log

    assert "warn" in log._LEVELS
    assert "warning" not in log._LEVELS


def test_every_bound_log_call_uses_a_real_level() -> None:
    """``X._log("warning", ...)`` is a crash, not a typo that stays quiet."""

    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            # ``DaemonApplication._log(level, component, event)`` is the
            # level-carrying one; ``self._core._log(event)`` elsewhere takes
            # only an event name, so arity separates them.
            if not isinstance(node, ast.Call) or len(node.args) < 3:
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "_log":
                continue
            first = node.args[0]
            if (
                isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and first.value not in LEVELS
            ):
                offenders.append(
                    f"{path.relative_to(SRC.parents[1])}:{node.lineno}: "
                    f"_log({first.value!r})"
                )

    assert not offenders, offenders
