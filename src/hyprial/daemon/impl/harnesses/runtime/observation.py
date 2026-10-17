"""Restore-only capability for publishing fresh native runtime facts.

``RefreshHarnessProjectionsCommand`` may republish the cached harness
projection.  It must not be able to make the runtime owner read native process
state: a generic switch would hand every caller a second liveness authority and
a "read native facts" primitive outside the owner's convergence path.

The only consumer allowed to ask for a native read is the restore path.  The
composition layer mints a :class:`RuntimeFactsGrant` from the daemon-owned
:class:`RuntimeFactsAuthority` and injects that authority into the harness port
client; the runtime owner validates the same grant against the same authority.
There is deliberately no boolean parameter: a caller cannot express "observe
native" by passing ``True``.  A grant that the authority did not mint -- a bare
``None``, a foreign object, a token from another authority -- is rejected with a
distinct code by the owner, never trusted because it is non-empty.
"""

from __future__ import annotations

import secrets
from collections import deque
from dataclasses import dataclass, field

RESTORE_PURPOSE = "restore"

# Grants are short-lived single-use facts, but a long-running daemon mints one
# per legacy wake, so the issued set is bounded.  Eviction only ever forgets an
# already-delivered grant; it never widens acceptance.
_ISSUED_CAPACITY = 256


@dataclass(frozen=True, slots=True)
class RuntimeFactsGrant:
    """Opaque capability value; only a ``RuntimeFactsAuthority`` mints one."""

    purpose: str
    token: str = field(repr=False)


class RuntimeFactsAuthority:
    """Daemon-owned minter and validator for native-observation capabilities."""

    __slots__ = ("_issued", "_order")

    def __init__(self) -> None:
        self._issued: set[str] = set()
        self._order: deque[str] = deque()

    def mint(self, *, purpose: str = RESTORE_PURPOSE) -> RuntimeFactsGrant:
        token = secrets.token_hex(16)
        self._issued.add(token)
        self._order.append(token)
        while len(self._order) > _ISSUED_CAPACITY:
            self._issued.discard(self._order.popleft())
        return RuntimeFactsGrant(purpose=purpose, token=token)

    def validate(self, grant: object) -> bool:
        """True only for a grant this authority minted for the restore purpose."""

        return (
            isinstance(grant, RuntimeFactsGrant)
            and grant.purpose == RESTORE_PURPOSE
            and grant.token in self._issued
        )
