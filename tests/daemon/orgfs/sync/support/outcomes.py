"""C2's admission outcomes (design §3.6), re-exported for the sync test host.

``outcome_kind`` recognises them by type (plan R16), so the test host returns
the real classes rather than look-alikes.
"""

from __future__ import annotations

from hyprial.daemon.impl.orgfs.membership import Accepted, Pending, Rejected

__all__ = ["Accepted", "Pending", "Rejected"]
