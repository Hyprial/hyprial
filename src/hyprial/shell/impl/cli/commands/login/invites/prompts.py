"""Per-invite confirmation for ``hyprial login`` and ``org pending`` (§3 O2c).

One rule, one place: a human at a TTY answers each invite separately; a
machine gets no prompt at all.  ``--json`` or a non-TTY stdin therefore
yields no confirmer, which the pickup reads as "join nothing, report what
is pending" (A2).  This is the whole D6 replacement: login has no
accept-everything default any more.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from hyprial.shell.impl.cli.output import confirm as ask

__all__ = ["invite_confirmer", "invite_question"]


def invite_question(org: str, inviter: str) -> str:
    """The one-line question a TTY shows for a single invite."""

    return f"join org {org!r} (invited by {inviter})?"


def invite_confirmer(
    services: Any, *, json_output: bool
) -> Callable[[str, str], bool] | None:
    """A per-invite confirmer, or None when this invocation may not ask.

    ``services`` is the CLI service bundle: it owns the stdin TTY check and
    the prompt sink, so both ``login`` and ``org pending`` ask identically.
    """

    if json_output or not services._stdin_isatty():
        return None

    def confirm(org: str, inviter: str) -> bool:
        return ask(invite_question(org, inviter))

    return confirm
