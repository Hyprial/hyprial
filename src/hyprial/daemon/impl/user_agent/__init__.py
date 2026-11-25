"""The owner's user agent: one registered agent with no harness per machine (R20, R21)."""

from hyprial.daemon.impl.user_agent.admin import (
    refuse_user_agent_method,
    refused_target_param,
    user_agent_row_fields,
)

from hyprial.daemon.impl.user_agent.ensure import (
    CREATED,
    DEFERRED,
    NAME_TAKEN,
    NO_OWNER,
    PRESENT,
    ensure_user_agent,
    local_user_agent,
)
from hyprial.daemon.impl.user_agent.lookup import local_user_agent_address
from hyprial.daemon.impl.user_agent.naming import (
    USER_AGENT_NAME,
    USER_AGENT_TEMPLATE,
    is_user_agent,
    is_user_agent_address,
    refuse_user_agent_change,
    refuse_user_agent_name,
    refuse_user_agent_target,
    refuse_user_agent_work,
    refuse_user_template_capability,
    user_agent_capabilities,
)
from hyprial.daemon.impl.user_agent.runtime import (
    run_user_agent_ensure,
    schedule_user_agent_ensure,
    user_agent_status,
)

__all__ = [
    "CREATED",
    "DEFERRED",
    "NAME_TAKEN",
    "NO_OWNER",
    "PRESENT",
    "USER_AGENT_NAME",
    "USER_AGENT_TEMPLATE",
    "ensure_user_agent",
    "is_user_agent",
    "is_user_agent_address",
    "local_user_agent",
    "local_user_agent_address",
    "refuse_user_agent_change",
    "refuse_user_agent_method",
    "refuse_user_agent_name",
    "refuse_user_agent_target",
    "refuse_user_agent_work",
    "refuse_user_template_capability",
    "refused_target_param",
    "run_user_agent_ensure",
    "schedule_user_agent_ensure",
    "user_agent_capabilities",
    "user_agent_row_fields",
    "user_agent_status",
]
