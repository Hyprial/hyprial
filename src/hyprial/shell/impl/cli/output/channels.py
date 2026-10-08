"""Everything that is not the result: progress, warnings and confirmation.

All of it goes to stderr, so stdout carries only the result (design §4.5).
Under ``--json`` a warning belongs in the result's data, so ``warn`` is
silent there; ``progress`` writes one NDJSON event per line instead.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import typer


def progress(event: str, message: str, *, json_output: bool, **fields: Any) -> None:
    """Report a step of a long command."""
    if json_output:
        sys.stderr.write(json.dumps({"event": event, **fields}, ensure_ascii=False) + "\n")
    else:
        sys.stderr.write(message + "\n")
    sys.stderr.flush()


def warn(message: str, *, json_output: bool) -> None:
    """Tell a human something the result does not say."""
    if not json_output:
        sys.stderr.write(message + "\n")
        sys.stderr.flush()


def notice(message: str) -> None:
    """A handoff a person must act on (an authorization URL), in both modes.

    Unlike ``warn`` it is written under ``--json`` too: the command blocks
    until the person acts, so the text must reach them whatever stdout carries.
    """
    sys.stderr.write(message)
    if not message.endswith("\n"):
        sys.stderr.write("\n")
    sys.stderr.flush()


def confirm(question: str, *, preview: str | None = None) -> bool:
    """Show ``preview`` and ask ``question`` on stderr; True when accepted.

    Interactive only: callers decide when a confirmation applies (``--yes``,
    ``--json``, a non-terminal stdin) and what declining means.
    """
    if preview is not None:
        sys.stderr.write(preview.rstrip("\n") + "\n")
        sys.stderr.flush()
    return typer.confirm(question, err=True)
