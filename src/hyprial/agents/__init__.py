"""The Agent entity: durable identity/configuration plus one liveness model.

Public seam for the delivery line (design §5.2) is exactly two names —
:func:`local_actors` and :func:`verify_fetch_claim`.  Nothing outside this
package composes an agent identity.
"""

from .liveness import (
    AGENT_HEARTBEAT_TTL_SECONDS,
    RUNTIME_HEADLESS,
    RUNTIME_INTERACTIVE,
    AgentAlreadyRunning,
    AgentBinding,
    AgentLiveness,
)
from .actor import AgentActor, AgentRegistryActor, SenderIdentityError
from .environment import (
    BASE_CHILD_ENVIRONMENT_NAMES,
    GENERATED_CHILD_ENVIRONMENT_NAMES,
    CompleteChildEnvironment,
    build_complete_child_environment,
)
from .home import AgentHomeError, AgentHomeProvisioner, HomeReceipt
from .secrets import (
    ResolvedSecret,
    SECRET_ENVIRONMENT_NAMES,
    SecretCatalogEntry,
    SecretCustody,
    SecretGrant,
    SecretResolver,
    SecretResolutionError,
    SecretSource,
)
from .registry import (
    ACTOR_NAME_PATTERN,
    Agent,
    AgentError,
    AgentExistsError,
    AgentNotFoundError,
    AgentRegistry,
    HandoverNotice,
    InvalidAgentNameError,
    PinConflictError,
    default_registry,
    local_actors,
    normalize_capabilities,
    normalize_harness_args,
    normalize_pinned_adapters,
    set_default_registry,
    verify_fetch_claim,
)

__all__ = [
    "ACTOR_NAME_PATTERN",
    "AGENT_HEARTBEAT_TTL_SECONDS",
    "RUNTIME_HEADLESS",
    "RUNTIME_INTERACTIVE",
    "Agent",
    "AgentActor",
    "AgentAlreadyRunning",
    "AgentBinding",
    "AgentError",
    "AgentExistsError",
    "AgentHomeError",
    "AgentHomeProvisioner",
    "AgentLiveness",
    "AgentNotFoundError",
    "AgentRegistry",
    "AgentRegistryActor",
    "BASE_CHILD_ENVIRONMENT_NAMES",
    "CompleteChildEnvironment",
    "GENERATED_CHILD_ENVIRONMENT_NAMES",
    "HandoverNotice",
    "HomeReceipt",
    "InvalidAgentNameError",
    "PinConflictError",
    "ResolvedSecret",
    "SECRET_ENVIRONMENT_NAMES",
    "SecretCatalogEntry",
    "SecretCustody",
    "SecretGrant",
    "SecretResolutionError",
    "SecretResolver",
    "SecretSource",
    "SenderIdentityError",
    "build_complete_child_environment",
    "default_registry",
    "local_actors",
    "normalize_capabilities",
    "normalize_harness_args",
    "normalize_pinned_adapters",
    "set_default_registry",
    "verify_fetch_claim",
]
