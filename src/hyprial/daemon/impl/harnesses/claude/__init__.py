"""Claude harness package: legacy-auth admission lives in ``runtime``.

Decision record (2026-11-19, PTY connector removal): the interactive
connector class and its credential admission were deleted with the PTY
launch path;
managed Claude workers start through the Agent SDK process in
``harnesses.agent_sdk`` and interactive Claude sessions are started by the
CLI.  This package now only hosts ``runtime`` (``validate_legacy_claude_auth``
and friends, still used by the SDK path and the CLI).
"""
