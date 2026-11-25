"""Licensed QQ official bot adapter: native python wire port."""

from .codec import CAPABILITY_REVISION, QQEventCodec
from .components import (
    QQOperationStore,
    QQWireComponents,
    QQLoopbackOverrides,
    build_default_components,
)
from .errors import (
    QQFatalGatewayClose,
    QQGatewayClosedClean,
    QQMsgSeqLimit,
    QQProtocolError,
    QQTokenUnavailable,
)
from .media import QQMedia
from .provider import (
    QQCheckpointLedger,
    QQCredentialResolver,
    QQCredentials,
    QQOfficialBotOnboarding,
    QQOfficialBotRuntime,
)
from .transport import (
    QQCloseAction,
    QQGatewaySocket,
    QQRestResult,
    QQRestWire,
    QQTokenManager,
    close_action,
)

__all__ = [
    "CAPABILITY_REVISION",
    "QQCheckpointLedger",
    "QQCloseAction",
    "QQCredentialResolver",
    "QQCredentials",
    "QQEventCodec",
    "QQFatalGatewayClose",
    "QQGatewayClosedClean",
    "QQGatewaySocket",
    "QQMedia",
    "QQMsgSeqLimit",
    "QQOfficialBotOnboarding",
    "QQOfficialBotRuntime",
    "QQOperationStore",
    "QQProtocolError",
    "QQRestResult",
    "QQRestWire",
    "QQTokenManager",
    "QQTokenUnavailable",
    "QQWireComponents",
    "QQLoopbackOverrides",
    "build_default_components",
    "close_action",
]
