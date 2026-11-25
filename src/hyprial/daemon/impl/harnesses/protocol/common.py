"""Bounded, redacted harness startup diagnostics.

Decision record (2026-11-19, PTY connector removal): the PTY process class,
its POSIX/ConPTY transports, the connector options dataclass and the
session-argument stripper were deleted with the interactive connector
launch path; every managed harness now starts through its headless protocol.
What remains here is the bounded stderr tail (``summarize_stderr``) and the
launch failure type (``HarnessStartError``) shared by those headless paths.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

STDERR_TAIL_BYTES = 4 * 1024
STDERR_TAIL_LINES = 8
STDERR_TRUNCATION_MARKER = "… [truncated]"

_AUTHORIZATION = re.compile(
    r"(\bauthorization\s*:\s*)[^\r\n]+",
    re.IGNORECASE,
)
_BEARER_OR_TOKEN = re.compile(
    r"\b(bearer|token)\s+[^\s,;]+",
    re.IGNORECASE,
)
_NAMED_CREDENTIAL = re.compile(
    r"((?:[\"']?)[a-z0-9_-]*(?:api[_-]?key|access[_-]?key|token|secret|password|"
    r"credential|authorization)[a-z0-9_-]*(?:[\"']?)\s*[:=]\s*)"
    r'(?:"(?:\\.|[^"\\\r\n])*"|\'(?:\\.|[^\'\\\r\n])*\'|[^\s,;]+)',
    re.IGNORECASE,
)


class HarnessStartError(RuntimeError):
    """A launch failure with only bounded, redacted harness diagnostics."""

    def __init__(
        self,
        harness: str,
        argv: Sequence[str],
        reason: str,
        *,
        returncode: int | None = None,
        stderr_tail: str = "",
    ) -> None:
        self.harness = harness
        self.argv = tuple(argv)
        self.returncode = returncode
        self.stderr_tail = stderr_tail
        message = f"{harness} harness failed to start: {reason}"
        if stderr_tail:
            message += f"\nRecent stderr:\n{stderr_tail}"
        super().__init__(message)


def summarize_stderr(
    stderr: bytes | str,
    *,
    max_lines: int = STDERR_TAIL_LINES,
    max_bytes: int = STDERR_TAIL_BYTES,
) -> str:
    """Return the last non-empty redacted lines within a byte budget.

    Defaults keep routine logs tight; startup-critical failure records
    (startup_timeout, unexpected worker exits) pass a larger budget so a
    verbose error body -- e.g. a rate-limit response the CLI retried
    against -- is not truncated away from the one line that explains it.
    """

    if isinstance(stderr, bytes):
        text = stderr.decode("utf-8", errors="replace")
    else:
        text = stderr
    text = _AUTHORIZATION.sub(r"\1[REDACTED]", text)
    text = _BEARER_OR_TOKEN.sub(r"\1 [REDACTED]", text)
    text = _NAMED_CREDENTIAL.sub(r"\1[REDACTED]", text)
    lines = [line.rstrip() for line in text.splitlines() if line.rstrip()]
    if not lines:
        return ""
    recent = "\n".join(lines[-max_lines:])
    encoded = recent.encode("utf-8")
    if len(encoded) <= max_bytes:
        return recent
    # The marker shares the first retained line so the marker itself does not
    # turn an eight-line diagnostic into nine surfaced lines.
    marker = f"{STDERR_TRUNCATION_MARKER} ".encode()
    retained = encoded[-(max_bytes - len(marker)) :]
    while retained and retained[0] & 0xC0 == 0x80:
        retained = retained[1:]
    return marker.decode() + retained.decode("utf-8", errors="replace")
