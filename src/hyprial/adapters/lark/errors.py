"""Stable Lark adapter domain errors shared by process and actor facades."""

from __future__ import annotations


class AdapterStartError(RuntimeError):
    """A Lark adapter start was refused or failed.

    ``code`` carries the stable wire code (``ADAPTER_ALREADY_RUNNING`` /
    ``ADAPTER_START_TIMEOUT``) when the refusal is a lifecycle verdict;
    ``None`` keeps the generic ``ADAPTER_START_FAILED`` mapping.
    """

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


__all__ = ["AdapterStartError"]
