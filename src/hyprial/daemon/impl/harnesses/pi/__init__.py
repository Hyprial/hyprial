"""Pi harness package: RPC worker clients and the interactive attach carrier.

Decision record (2026-11-19, PTY connector removal): the interactive
connector class was deleted with the PTY launch path; managed pi workers start
through the RPC process (``rpc_process``) and interactive pi sessions are
started by the CLI, which ships the attach extension below.
"""

from __future__ import annotations

from pathlib import Path

# The interactive attach carrier: a TUI launched with this extension
# registers itself with the daemon and relays Harness messages into the
# user's session (see pi_harness_attach.ts).  Shipped as package data next to
# this module.
PI_HARNESS_ATTACH_EXTENSION = Path(__file__).parent / "resources" / "pi_harness_attach.ts"
