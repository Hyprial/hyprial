"""Org lifecycle helpers: deletion journal flow, D5 binding, authority gating."""

from hyprial.daemon.impl.org.network.lifecycle.authority import (
    authority_devices,
    require_roster_write_authority,
)
from hyprial.daemon.impl.org.network.lifecycle.binding import (
    annotate_deleted_orgs,
    annotate_unbound_org_cells,
    refuse_deleted_or_unbound,
    require_bound_org_cell,
    skip_unbound_leave_processing,
)
from hyprial.daemon.impl.org.network.lifecycle.deletion import (
    DELETED_ORG_RECORD_RELPATH,
    DELETION_JOURNAL_RELPATH,
    MIGRATION_DOC,
    deletion_journal_path,
    deleted_org_record_path,
    delete_org,
    depart_deleted_org,
    refuse_resurrected_org,
)

__all__ = [
    "DELETED_ORG_RECORD_RELPATH",
    "DELETION_JOURNAL_RELPATH",
    "MIGRATION_DOC",
    "annotate_deleted_orgs",
    "annotate_unbound_org_cells",
    "authority_devices",
    "deletion_journal_path",
    "deleted_org_record_path",
    "delete_org",
    "depart_deleted_org",
    "refuse_deleted_or_unbound",
    "refuse_resurrected_org",
    "require_bound_org_cell",
    "require_roster_write_authority",
    "skip_unbound_leave_processing",
]
