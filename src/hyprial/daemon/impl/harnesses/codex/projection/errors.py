"""Codex projection refusal shared by preparation and native validation."""


class CodexAgentHomeError(ValueError):
    """The resolved Codex native root failed its P2 loading contract."""

    permanent_start_failure = True
