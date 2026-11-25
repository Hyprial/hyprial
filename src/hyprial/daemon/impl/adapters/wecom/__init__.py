"""WeCom bot and application providers as closed, non-substitutable variants."""

from .application import (
    WeComApplicationCredentials,
    WeComApplicationRuntime,
    WeComApplicationTransport,
    classify_wecom_application_send,
)
from .bot import WeComBotCredentials, WeComBotRuntime, WeComBotTransport
from .codec import (
    DecodedWeComApplicationEvent,
    DecodedWeComBotEvent,
    WeComApplicationCodec,
    WeComBotCodec,
    decode_wecom_application_plaintext,
    decode_wecom_bot_frame,
)
from .crypto import WeComCallbackCrypto
from .errors import WeComProtocolError
from .transport import (
    HttpxWeComApplicationTransport,
    WeComCredentialAuthority,
    WeComMedia,
    WeComOnboarding,
    WeComSdkBotTransport,
)

__all__ = [
    "DecodedWeComBotEvent",
    "DecodedWeComApplicationEvent",
    "HttpxWeComApplicationTransport",
    "WeComApplicationCredentials",
    "WeComApplicationCodec",
    "WeComApplicationRuntime",
    "WeComApplicationTransport",
    "WeComBotCredentials",
    "WeComBotCodec",
    "WeComBotRuntime",
    "WeComBotTransport",
    "WeComCallbackCrypto",
    "WeComCredentialAuthority",
    "WeComMedia",
    "WeComOnboarding",
    "WeComProtocolError",
    "WeComSdkBotTransport",
    "classify_wecom_application_send",
    "decode_wecom_bot_frame",
    "decode_wecom_application_plaintext",
]
