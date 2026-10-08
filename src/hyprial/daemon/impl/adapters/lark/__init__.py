"""Lark adapter public API (facade over the semantic subpackages).

Subpackages: contracts (pure wire vocabulary), ports (actor command and
projection contracts), credentials (identity, scopes, onboarding, reauth),
state (SQLite correlation custody), inbound (message pipeline, commands,
reconcile, dead letters), outbound (SDK connection, gateway sends, ack
reactions), media (media fetch), runtime (daemon-side adapter actor domain,
worker process supervision, stream health) and worker (the per-gateway worker
process: entrypoint, process handles, daemon link, reply bridge, control IO).
"""

from hyprial.daemon.impl.adapters.lark.contracts.endpoint import (
    FEISHU_BASE_URL,
    LARK_BASE_URL_ENV,
)
from hyprial.daemon.impl.adapters.lark.contracts.lifecycle import (
    ADAPTER_START_TIMEOUT,
    START_DEADLINE_SECONDS,
)
from hyprial.daemon.impl.adapters.lark.contracts.messages import (
    MERGE_FORWARD_LABEL,
    canonical_lark_message_type,
    LarkForwardedItem,
    media_ref,
    normalize_lark_message_content,
    normalize_merge_forward,
)
from hyprial.daemon.impl.adapters.lark.contracts.reply_bridge import (
    lark_reply_adapter,
)
from hyprial.daemon.impl.adapters.lark.contracts.wire import (
    ActorTarget,
    AgentDirectoryEntry,
    DeliveryOutcome,
    encode_lark_text_content,
    HarnessDelivery,
    HarnessPort,
    HarnessReceipt,
    HarnessRequest,
    InboundOutcome,
    LarkApiPort,
    LarkBotInfo,
    LarkChatMember,
    LarkChatSummary,
    LarkHistoryBatch,
    LarkInboundMessage,
    PendingCommandCapacityStatus,
    QuotedMessage,
    ReconcileReport,
    RouteDirectory,
    RuntimeTarget,
)
from hyprial.daemon.impl.adapters.lark.credentials.identities import (
    IdentityCollectionPort,
    sync_identities,
)
from hyprial.daemon.impl.adapters.lark.inbound.adapter import (
    LarkAdapter,
    normalize_sdk_event,
)
from hyprial.daemon.impl.adapters.lark.media.media import (
    MediaDownloadPort,
    MediaFetchError,
    MediaFetchResult,
    MediaRef,
    fetch_media,
    parse_media_ref,
)
from hyprial.daemon.impl.adapters.lark.outbound.reactions import (
    ACK_EMOJI,
    ReactionEffectAdmission,
    ReactionEffectKind,
    ReactionEffectRecord,
    ReactionEffectsRuntime,
    ReactionEffectsSnapshot,
)
from hyprial.daemon.impl.adapters.lark.outbound.sdk import LarkSdkGateway
from hyprial.daemon.impl.adapters.lark.outbound.sdk_base import (
    LarkApiError,
    LarkSdkError,
)
from hyprial.daemon.impl.adapters.lark.outbound.stream import LarkEventStream
from hyprial.daemon.impl.adapters.lark.ports.ports import LarkProjectionPort
from hyprial.daemon.impl.adapters.lark.runtime.runtime import AdapterRuntime
from hyprial.daemon.impl.adapters.lark.state.records import (
    DeadLetter,
    Identity,
    PendingCommandCapacityError,
    PendingCommandResponse,
    ReplyRoute,
    RequestCorrelation,
)
from hyprial.daemon.impl.adapters.lark.state.store import LarkStateStore

# Module aliases for ``from hyprial.daemon.impl.adapters.lark import X``
# consumers (cli.py, application.py and tests import these modules as
# package attributes).  These are the real modules, not re-implementations.
from hyprial.daemon.impl.adapters.lark.contracts import lifecycle
from hyprial.daemon.impl.adapters.lark.credentials import reauth
from hyprial.daemon.impl.adapters.lark.outbound import sdk

__all__ = [
    "ACK_EMOJI",
    "ADAPTER_START_TIMEOUT",
    "AgentDirectoryEntry",
    "ActorTarget",
    "AdapterRuntime",
    "DeadLetter",
    "DeliveryOutcome",
    "FEISHU_BASE_URL",
    "HarnessDelivery",
    "HarnessPort",
    "HarnessReceipt",
    "HarnessRequest",
    "Identity",
    "IdentityCollectionPort",
    "InboundOutcome",
    "LARK_BASE_URL_ENV",
    "LarkAdapter",
    "LarkApiError",
    "LarkApiPort",
    "LarkBotInfo",
    "LarkChatMember",
    "LarkChatSummary",
    "LarkEventStream",
    "LarkForwardedItem",
    "LarkHistoryBatch",
    "LarkInboundMessage",
    "LarkProjectionPort",
    "LarkSdkError",
    "LarkSdkGateway",
    "LarkStateStore",
    "MERGE_FORWARD_LABEL",
    "MediaDownloadPort",
    "MediaFetchError",
    "MediaFetchResult",
    "MediaRef",
    "PendingCommandCapacityError",
    "PendingCommandCapacityStatus",
    "PendingCommandResponse",
    "QuotedMessage",
    "ReactionEffectAdmission",
    "ReactionEffectKind",
    "ReactionEffectRecord",
    "ReactionEffectsRuntime",
    "ReactionEffectsSnapshot",
    "ReconcileReport",
    "ReplyRoute",
    "RequestCorrelation",
    "RouteDirectory",
    "RuntimeTarget",
    "START_DEADLINE_SECONDS",
    "canonical_lark_message_type",
    "encode_lark_text_content",
    "fetch_media",
    "lifecycle",
    "media_ref",
    "lark_reply_adapter",
    "normalize_lark_message_content",
    "normalize_merge_forward",
    "normalize_sdk_event",
    "parse_media_ref",
    "reauth",
    "sdk",
    "sync_identities",
]
