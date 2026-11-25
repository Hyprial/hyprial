"""Bounded, secret-conscious diagnostics used by the Codex attach gate."""

from __future__ import annotations

import re
from pathlib import Path


class Blocked(RuntimeError):
    """A missing real-environment prerequisite."""


class ManualWindowStop(RuntimeError):
    """Stop after the human-review window so normal cleanup still runs."""


def emit_stage(name: str, status: str, detail: str | None = None) -> None:
    suffix = f" {detail}" if detail else ""
    print(f"STAGE={name} {status}{suffix}", flush=True)


def quota_hint(text: str) -> str | None:
    for line in text.splitlines():
        if re.search(r"rate limit|approaching|weekly limit|quota|usage limit|429", line, re.I):
            return " ".join(line.split())[:300]
    return None


def quota_blocking_hint(text: str) -> str | None:
    for line in text.splitlines():
        if re.search(r"rate limit|exhausted|exceeded|429|usage limit", line, re.I):
            return " ".join(line.split())[:300]
    return None


def environment_block_hint(text: str) -> str | None:
    for line in text.splitlines():
        if re.search(r"failed to refresh available models|timeout waiting for child process to exit", line, re.I):
            return " ".join(line.split())[:300]
    return None


def diagnostic_tail(path: Path | None, lines: int = 20) -> list[str]:
    if path is None or not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    except OSError:
        return []
    redacted: list[str] = []
    for line in raw:
        line = re.sub(r"(?i)(authorization\s*[:=]\s*)\S+", r"\1[REDACTED]", line)
        line = re.sub(r"(?i)(bearer|token)\s+\S+", r"\1 [REDACTED]", line)
        redacted.append(line[:500])
    return redacted


def print_diagnostic_tail(path: Path | None) -> list[str]:
    """Print a bounded log snapshot with an explicit causality disclaimer."""
    tail = diagnostic_tail(path)
    if not tail:
        return []
    print(
        "DIAGNOSTIC_LOG_TAIL_NOTE=log-tail snapshot; "
        "not necessarily the failure cause",
        flush=True,
    )
    print("DIAGNOSTIC_LOG_TAIL_BEGIN", flush=True)
    for line in tail:
        print(line, flush=True)
    print("DIAGNOSTIC_LOG_TAIL_END", flush=True)
    return tail
