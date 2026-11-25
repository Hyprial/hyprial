"""Matrix client adapter protocol implementation."""

from .codec import CAPABILITY_REVISION, RENDERER_REVISION, MatrixEventCodec
from .errors import (
    MatrixAdapterError,
    MatrixCryptoBlocked,
    MatrixDefinitelyFailed,
    MatrixRateLimited,
)
from .models import (
    MatrixCredentialResolver,
    MatrixCredentials,
    MatrixCryptoBackend,
    MatrixOutboundRecord,
    MatrixStateStore,
    MatrixTransport,
    REVIEWED_MATRIX_CRYPTO_DESCRIPTOR,
    freeze_room_encryption_evidence,
    normalize_homeserver,
    read_room_encryption_evidence,
)
from .onboarding import MatrixOnboarding
from .production import (
    MatrixJsonCredentialResolver,
    MatrixNioCryptoBackend,
    MatrixNioTransport,
    MatrixProductionPaths,
    MatrixProductionUnavailable,
    MatrixSqliteStateStore,
    create_default_matrix_runtime,
)
from .runtime import MAX_SYNC_EVENTS, SYNC_TIMEOUT_MS, MatrixRuntime, matrix_transaction_id

__all__ = [
    "CAPABILITY_REVISION",
    "MAX_SYNC_EVENTS",
    "MatrixAdapterError",
    "MatrixCredentialResolver",
    "MatrixCredentials",
    "MatrixCryptoBackend",
    "MatrixCryptoBlocked",
    "MatrixDefinitelyFailed",
    "MatrixEventCodec",
    "MatrixOnboarding",
    "MatrixJsonCredentialResolver",
    "MatrixNioCryptoBackend",
    "MatrixNioTransport",
    "MatrixOutboundRecord",
    "MatrixProductionPaths",
    "MatrixProductionUnavailable",
    "MatrixRateLimited",
    "MatrixRuntime",
    "MatrixSqliteStateStore",
    "MatrixStateStore",
    "MatrixTransport",
    "REVIEWED_MATRIX_CRYPTO_DESCRIPTOR",
    "RENDERER_REVISION",
    "SYNC_TIMEOUT_MS",
    "matrix_transaction_id",
    "create_default_matrix_runtime",
    "freeze_room_encryption_evidence",
    "normalize_homeserver",
    "read_room_encryption_evidence",
]
