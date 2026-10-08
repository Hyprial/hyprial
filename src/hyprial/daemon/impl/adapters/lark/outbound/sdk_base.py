"""Official ``lark-oapi`` SDK boundary used by the adapter."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    pass


class _Deferred:
    """A ``lark_oapi`` name that is imported when it is first used.

    ``import lark_oapi`` runs the package initialiser, and that imports the
    whole generated API surface: 10,416 modules, measured on 2026-09-24 as
    15.7s of a 17.7s ``import hyprial.daemon.impl.application`` on the packaged
    Windows runtime.  The daemon imported it to reach this module, so every
    launch paid it -- including a client that never signs in to Lark.

    These names instead resolve on first attribute access.  A launch that never
    builds a Lark request never pays for the SDK, and one that does pays at its
    first request instead of before the window can appear.
    """

    __slots__ = ("_module", "_symbol", "_value")

    def __init__(self, module: str, symbol: str | None = None) -> None:
        self._module = module
        self._symbol = symbol
        self._value: Any = None

    def _resolve(self) -> Any:
        if self._value is None:
            # The 19 deferred names below this boundary use exactly these two
            # external SDK modules. Keep lazy loading without an opaque import
            # path that could reach another Hyprial implementation authority.
            if self._module == "lark_oapi":
                imported = importlib.import_module("lark_oapi")
            elif self._module == "lark_oapi.api.im.v1":
                imported = importlib.import_module("lark_oapi.api.im.v1")
            else:
                raise ValueError("unsupported deferred SDK module")
            self._value = imported if self._symbol is None else getattr(imported, self._symbol)
        return self._value

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resolve(), name)


lark = _Deferred("lark_oapi")
CreateFileRequest = _Deferred("lark_oapi.api.im.v1", "CreateFileRequest")
CreateFileRequestBody = _Deferred("lark_oapi.api.im.v1", "CreateFileRequestBody")
CreateImageRequest = _Deferred("lark_oapi.api.im.v1", "CreateImageRequest")
CreateImageRequestBody = _Deferred("lark_oapi.api.im.v1", "CreateImageRequestBody")
CreateMessageReactionRequest = _Deferred("lark_oapi.api.im.v1", "CreateMessageReactionRequest")
CreateMessageReactionRequestBody = _Deferred("lark_oapi.api.im.v1", "CreateMessageReactionRequestBody")
CreateMessageRequest = _Deferred("lark_oapi.api.im.v1", "CreateMessageRequest")
CreateMessageRequestBody = _Deferred("lark_oapi.api.im.v1", "CreateMessageRequestBody")
DeleteMessageReactionRequest = _Deferred("lark_oapi.api.im.v1", "DeleteMessageReactionRequest")
Emoji = _Deferred("lark_oapi.api.im.v1", "Emoji")
GetChatMembersRequest = _Deferred("lark_oapi.api.im.v1", "GetChatMembersRequest")
GetMessageRequest = _Deferred("lark_oapi.api.im.v1", "GetMessageRequest")
GetMessageResourceRequest = _Deferred("lark_oapi.api.im.v1", "GetMessageResourceRequest")
ListChatRequest = _Deferred("lark_oapi.api.im.v1", "ListChatRequest")
ListMessageReactionRequest = _Deferred("lark_oapi.api.im.v1", "ListMessageReactionRequest")
ListMessageRequest = _Deferred("lark_oapi.api.im.v1", "ListMessageRequest")
ReplyMessageRequest = _Deferred("lark_oapi.api.im.v1", "ReplyMessageRequest")
ReplyMessageRequestBody = _Deferred("lark_oapi.api.im.v1", "ReplyMessageRequestBody")


class LarkSdkError(RuntimeError):
    pass


class LarkApiError(LarkSdkError):
    """A Lark OpenAPI rejection carrying the platform error code."""

    def __init__(
        self,
        operation: str,
        code: int | None,
        message: str | None,
        *,
        missing_scopes: tuple[str, ...] = (),
        authorization_url: str | None = None,
        authorization_url_reason: str | None = None,
    ) -> None:
        # ``message=None`` (an unparseable platform body) renders without a
        # literal "None" so operator-facing text stays clean; the structured
        # log keeps the null as a JSON null instead.
        super().__init__(
            f"{operation} failed: code={code if code is not None else '<unknown>'}"
            + (f" message={message}" if message else "")
        )
        self.operation = operation
        self.code = code
        self.lark_message = message
        self.missing_scopes = missing_scopes
        self.authorization_url = authorization_url
        self.authorization_url_reason = authorization_url_reason
        self.recovery: dict[str, Any] | None = None


def _normalized_code(value: object) -> int | None:
    """Best-effort int coercion for a platform response ``code`` field.

    A response that failed to parse (or a mock) can leave ``code`` as
    ``None`` or a non-numeric string.  The send boundary must keep reporting
    that failure through :class:`LarkApiError` instead of dying with a
    ``TypeError`` inside the error path itself.
    """

    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_LARK_FILE_TYPES = {
    ".opus": "opus",
    ".mp4": "mp4",
    ".pdf": "pdf",
    ".doc": "doc",
    ".docx": "doc",
    ".xls": "xls",
    ".xlsx": "xls",
    ".ppt": "ppt",
    ".pptx": "ppt",
}


#: ``im.v1.message.list`` refusals that are permanent for one chat: the
#: platform error-code table of the official "Get chat history" document
#: (https://open.feishu.cn/document/server-docs/im-v1/message/list, mirrored
#: at open.larksuite.com) lists 230002 as "The bot can not be outside the
#: group" -- the app is not a member of the chat, which is also what a
#: disbanded group (or one the bot was removed from) surfaces, since the
#: membership no longer exists.  The same code is live-confirmed for sends
#: in ``hyprial.daemon.impl.route_delivery`` (``LARK_CODE_APP_NOT_IN_CHAT``).
#: Deliberately excluded: 230027 (missing scope -- recoverable by
#: re-authorization), 230001 (ambiguous invalid-parameter), and everything
#: else; any unrecognized code stays fail-closed as transient.
LARK_HISTORY_CODES_PERMANENT_CHAT_GONE = frozenset({230002})


def lark_file_type(path: Path) -> str:
    """Map documented native formats; every other attachment is a stream."""

    return _LARK_FILE_TYPES.get(path.suffix.lower(), "stream")
