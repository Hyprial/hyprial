"""Old top-level command paths fail loud; they are not silent aliases.

When a family moves under its owning group, the old path stops existing.  A
caller who still types it is making a usage error: the CLI prints one
greppable line that names the new path and exits 2, and it writes nothing to
stdout — not even under ``--json``, because the command was never dispatched
and no business result exists to report.

The line is frozen as ``moved: hyprial <old argv>  →  hyprial <new argv>`` with
two spaces on each side of the arrow.  ``tests/shell/cli/surface/
test_cli_tree_regroup.py`` pins both the prefix and the arrow spacing, so a
format drift fails a test instead of silently changing operator guidance.

Whole-argv rewrite, not token replacement: ``service connect --json`` becomes
``org service connect --json``.  A naive ``str.replace`` would also rewrite a
newer path that merely starts with the old name, so matching is prefix-based
and the remainder is carried over untouched.
"""

from __future__ import annotations

from collections.abc import Sequence

from typer.core import TyperGroup

from hyprial.shell.impl.cli.output import notice

MOVED_PREFIX = "moved: "
MOVED_ARROW = "  \u2192  "

#: Old argv prefix → the path it now lives under.
MOVED_PREFIXES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("service",), ("org", "service")),
    (("fs",), ("org", "fs")),
    (("user-proxy",), ("user", "proxy")),
    (("link",), ("agent", "link")),
    (("onboarding",), ("init", "onboarding")),
)


def render_moved(
    old: Sequence[str], new: Sequence[str]
) -> str:
    """Render the one-line operator guidance for a moved path."""

    literal = MOVED_PREFIX
    literal += "hyprial " + " ".join(old)
    literal += MOVED_ARROW
    literal += "hyprial " + " ".join(new)
    return literal


def resolve_moved(
    argv: Sequence[str],
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """Return ``(old argv, new argv)`` when ``argv`` starts with a moved path.

    Both sides carry the untouched remainder, so ``fs space list`` reports the
    whole path it was given rather than only the moved prefix.
    """

    for old, new in MOVED_PREFIXES:
        if tuple(argv[: len(old)]) != old:
            continue
        remainder = tuple(argv[len(old) :])
        return old + remainder, new + remainder
    return None


class MovedPathGroup(TyperGroup):
    """Refuse a moved top-level path before any command or callback runs."""

    def resolve_command(self, ctx, args):  # type: ignore[override]
        moved = resolve_moved(args)
        if moved is not None:
            old, new = moved
            notice(render_moved(old, new))
            ctx.exit(2)
        return super().resolve_command(ctx, args)
