"""Owner migration execution: apply plans, detect previous owners and run the startup migration."""

from __future__ import annotations

from .vocabulary import (  # noqa: F401
    ADDRESS_PREFIXES,
    ALLOWED_RESIDUALS,
    ARCHIVAL_COLUMNS,
    HOST_LOGIN_JSON_KEYS,
    MIGRATION_DATABASES,
    MIGRATION_TEXT_FILES,
    MigrationPlan,
    OWNER_JSON_KEYS,
    OwnerMigrationAborted,
    OwnerMigrationCustodyConflict,
    OwnerMigrationHostedConflict,
    PendingWrite,
    ResidualForm,
    USER_IDENTIFIER_EXTRA_CHARACTERS,
    Unclassified,
    _table_exists,
)
from .planning import (  # noqa: F401
    _check_hosted_owner_collision,
    _hit_context,
    _home_root_residual,
    _is_accounted_for,
    _rewrite_json_owner_keys,
    _text_columns,
    _unaccounted_json_values,
    build_plan,
    plan_database,
    plan_text_file,
    rewrite_value,
)
from .apply import (  # noqa: F401
    OwnerMigrationCustodyUnreadable,
    _count_or_zero_if_absent,
    _live_custody_counts,
    apply_plan,
    detect_previous_owner,
    migrate_owner,
    migrate_owner_if_needed,
)
