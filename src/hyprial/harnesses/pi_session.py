"""The pi ``--session-id`` boundary translation, shared by every pi launch.

Pi >=0.76 validates ``--session-id`` against ``[A-Za-z0-9._-]`` and rejects
any other character (colons included) at launch.  The session ref is OUR
identity key -- daemon state, fencing and desired-state all key on the raw
value -- so pi's charset rule must not back-pollute our key space.  The
boundary translation happens only when the ref becomes a pi argument: every
disallowed character maps deterministically to '.' (squire:alw:zyli ->
squire.alw.zyli).  Allowed characters pass through byte-for-byte, so a clean
ref is never rewritten and the sanitized value is still stable across
reconnects.

Introduced at the RPC boundary (#192); the interactive pty connector and the
interactive attach launcher share the same boundary, so the translation
lives here exactly once.
"""

from __future__ import annotations

import re

_PI_SESSION_ID_DISALLOWED = re.compile(r"[^A-Za-z0-9._-]")


def pi_session_id(session_ref: str) -> str:
    """Translate a session ref into a pi ``--session-id`` value."""

    return _PI_SESSION_ID_DISALLOWED.sub(".", session_ref)
