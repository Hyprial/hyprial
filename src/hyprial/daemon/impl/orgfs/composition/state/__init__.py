# Decision record (phase D, package D2, 2026-11-25):
# - Plan §3.3: the state runtime's mixins live here because ``composition/``
#   already holds seven files.  Only ``StateOrgFsRuntime`` is public.
"""The OrgFS runtime over the state store and protocol 3 sync (phase D)."""

from hyprial.daemon.impl.orgfs.composition.state.runtime import StateOrgFsRuntime

__all__ = ["StateOrgFsRuntime"]
