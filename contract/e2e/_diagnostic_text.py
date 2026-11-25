"""Render a failure detail for a RESULT line without losing the error.

The scenario runners used to print `" ".join(detail.split())[:1000]`, which
keeps the HEAD of the text. For a failed subprocess the head is progress output
("Updating ...", "Downloading ...") and the actual error is at the END, so the
reason was cut away (E2E-010 upgrade-continuity, baseline task 40654: uv exit 2
with only the download progress visible). Keep both ends instead.

Redact before clipping: callers pass the full one-line text through their
secret/native-id redaction first and only then call `clip`, so no cut point can
split a secret into an unredacted fragment.
"""

from __future__ import annotations

HEAD_CHARS = 1000
TAIL_CHARS = 6000


def one_line(value: object) -> str:
    """Collapse whitespace and drop NULs; never truncates."""

    return " ".join(str(value).replace("\x00", "").split())


def clip(text: str, head: int = HEAD_CHARS, tail: int = TAIL_CHARS) -> str:
    """Keep the first `head` and the last `tail` characters, marking the gap."""

    if len(text) <= head + tail:
        return text
    elided = len(text) - head - tail
    return f"{text[:head]} …[{elided} chars elided]… {text[-tail:]}"
