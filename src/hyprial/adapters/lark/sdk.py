"""Official ``lark-oapi`` SDK boundary used by the adapter."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, uuid5

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateFileRequest,
    CreateFileRequestBody,
    CreateImageRequest,
    CreateImageRequestBody,
    CreateMessageReactionRequest,
    CreateMessageReactionRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    DeleteMessageReactionRequest,
    Emoji,
    GetChatMembersRequest,
    GetMessageRequest,
    GetMessageResourceRequest,
    ListChatRequest,
    ListMessageReactionRequest,
    ListMessageRequest,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

from .api import (
    LarkBotInfo,
    LarkChatMember,
    LarkChatSummary,
    LarkForwardedItem,
    LarkHistoryBatch,
    LarkInboundMessage,
    NormalizedLarkContent,
    QuotedMessage,
    encode_lark_text_content,
    normalize_lark_message_content,
    normalize_merge_forward,
)
from .endpoint import lark_base_url
from .scopes import parse_permission_violation, runtime_permission_url

if TYPE_CHECKING:
    from hyprial.log import Logger


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
#: in ``hyprial.daemon.route_delivery`` (``LARK_CODE_APP_NOT_IN_CHAT``).
#: Deliberately excluded: 230027 (missing scope -- recoverable by
#: re-authorization), 230001 (ambiguous invalid-parameter), and everything
#: else; any unrecognized code stays fail-closed as transient.
LARK_HISTORY_CODES_PERMANENT_CHAT_GONE = frozenset({230002})


def lark_file_type(path: Path) -> str:
    """Map documented native formats; every other attachment is a stream."""

    return _LARK_FILE_TYPES.get(path.suffix.lower(), "stream")


def _permission_payload(response: Any) -> dict[str, Any]:
    error = getattr(response, "error", None)

    def field(value: object, name: str) -> object:
        if isinstance(value, Mapping):
            return value.get(name)
        return getattr(value, name, None)

    violations = field(error, "permission_violations")
    normalized: list[dict[str, Any]] = []
    for item in (violations if isinstance(violations, (list, tuple)) else ()):
        normalized.append({"subject": field(item, "subject")})
    raw_helps = field(error, "helps")
    helps = [
        {"url": field(item, "url")}
        for item in (raw_helps if isinstance(raw_helps, (list, tuple)) else ())
    ]
    return {
        "code": getattr(response, "code", None),
        "error": {
            "permission_violations": normalized,
            "helps": helps,
        },
    }


def _rest_message_type(message: Any) -> Any:
    # The REST models name the field ``msg_type``; event payloads use
    # ``message_type``.  Accept either so fakes and SDK generations both work.
    return getattr(message, "msg_type", None) or getattr(
        message, "message_type", None
    )


def _forwarded_items(items: Any) -> tuple[LarkForwardedItem, ...]:
    """Project an ``im.v1.message.get`` response onto the neutral item shape."""

    collected: list[LarkForwardedItem] = []
    for item in items or ():
        message_id = getattr(item, "message_id", None)
        body = getattr(item, "body", None)
        if not isinstance(message_id, str) or not message_id or body is None:
            continue
        sender = getattr(item, "sender", None)
        sender_id = getattr(sender, "id", None)
        sender_type = getattr(sender, "sender_type", None)
        collected.append(
            LarkForwardedItem(
                message_id=message_id,
                # ``canonical_lark_message_type`` closes this set downstream;
                # an unknown child becomes an explicit unsupported line.
                message_type=str(_rest_message_type(item) or ""),
                content=getattr(body, "content", None),
                upper_message_id=getattr(item, "upper_message_id", None),
                sender_id=(
                    sender_id
                    if isinstance(sender_id, str) and sender_id
                    else None
                ),
                sender_type=(
                    sender_type
                    if isinstance(sender_type, str) and sender_type
                    else None
                ),
            )
        )
    return tuple(collected)


def _normalize_rest_body(
    message: Any,
    items: Any = (),
    *,
    resolve_sender: Callable[[str], str | None] | None = None,
) -> NormalizedLarkContent:
    """Normalize one REST message, expanding a merge-forward when possible.

    ``im.v1.message.get`` returns the forwarded conversation in the *same*
    response as the shell, so expansion here costs no extra platform call.
    Callers without that list (``im.v1.message.list``) get the label.
    """

    message_type = _rest_message_type(message)
    body = getattr(message, "body", None)
    content = getattr(body, "content", None)
    message_id = getattr(message, "message_id", None)
    if message_type == "merge_forward":
        forwarded = _forwarded_items(items)
        if forwarded:
            return normalize_merge_forward(
                message.message_id, forwarded, resolve_sender=resolve_sender
            )
    return normalize_lark_message_content(
        message_type,
        content,
        message_id=message_id if isinstance(message_id, str) else None,
    )


def _inbound_from_rest_message(
    message: Any,
    *,
    event_id: str,
    chat_type: str | None,
    items: Any = (),
    resolve_sender: Callable[[str], str | None] | None = None,
) -> LarkInboundMessage | None:
    """Normalize a REST ``Message`` model (get/list) into the inbound shape.

    Returns ``None`` only for non-user senders or missing routing fields.
    Message bodies share the live-event normalizer, including explicit
    unsupported/invalid outcomes, so history never silently drops a type.
    """

    sender = message.sender
    body = message.body
    if not message.message_id or not message.chat_id or sender is None or body is None:
        return None
    if sender.sender_type != "user":
        return None
    normalized = _normalize_rest_body(message, items, resolve_sender=resolve_sender)
    text = normalized.text
    mention_names: list[str] = []
    mention_open_ids: list[str] = []
    for mention in message.mentions or ():
        name = getattr(mention, "name", None)
        key = getattr(mention, "key", None)
        if isinstance(name, str) and name:
            mention_names.append(name)
        if isinstance(key, str) and key:
            text = text.replace(key, "")
        # REST ``Mention`` carries ``id`` as a plain string qualified by
        # ``id_type``; only an open_id is comparable to the bot's identity.
        # Tolerate an event-shaped ``id`` object too, so fakes and future SDK
        # generations that reuse the UserId model keep working.
        identity = getattr(mention, "id", None)
        id_type = getattr(mention, "id_type", None)
        nested_open_id = getattr(identity, "open_id", None)
        if isinstance(nested_open_id, str) and nested_open_id:
            mention_open_ids.append(nested_open_id)
        elif (
            isinstance(identity, str)
            and identity
            and (id_type is None or id_type == "open_id")
        ):
            mention_open_ids.append(identity)
    create_time = message.create_time
    return LarkInboundMessage(
        event_id=event_id,
        message_id=message.message_id,
        chat_id=message.chat_id,
        chat_type=chat_type or "unknown",
        text=text.strip(),
        sender_id=sender.id or "unknown",
        sender_type=sender.sender_type,
        root_id=message.root_id,
        thread_id=getattr(message, "thread_id", None),
        reply_to=message.parent_id,
        create_time=str(create_time) if create_time is not None else None,
        message_type=normalized.message_type,
        content_status=normalized.status,
        mentions=tuple(mention_names),
        mention_open_ids=tuple(mention_open_ids),
    )


class LarkSdkGateway:
    """Small synchronous facade over generated Lark IM v1 APIs."""

    def __init__(
        self,
        client: Any,
        *,
        permission_recovery: Callable[[LarkApiError], dict[str, Any] | None]
        | None = None,
        sender_resolver: Callable[[str], str | None] | None = None,
        logger: Logger | None = None,
        gateway_name: str | None = None,
        app_id: str | None = None,
    ) -> None:
        self._client = client
        self._permission_recovery = permission_recovery
        self._bot_open_id: str | None = None
        self._sender_resolver = sender_resolver
        self._logger = logger
        self._gateway_name = gateway_name
        self._app_id = app_id

    @classmethod
    def from_credentials(
        cls,
        app_id: str,
        app_secret: str,
        *,
        logger: Logger | None = None,
        gateway_name: str | None = None,
    ) -> LarkSdkGateway:
        client = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .domain(lark_base_url())
            .build()
        )
        return cls(client, logger=logger, gateway_name=gateway_name, app_id=app_id)

    def set_logger(
        self, logger: Logger | None, *, gateway_name: str | None = None
    ) -> None:
        """Attach the secret-free send-outcome logger (daemon route gateway)."""

        self._logger = logger
        if gateway_name is not None:
            self._gateway_name = gateway_name

    def set_permission_recovery(
        self, recovery: Callable[[LarkApiError], dict[str, Any] | None] | None
    ) -> None:
        self._permission_recovery = recovery

    def set_sender_resolver(
        self, resolver: Callable[[str], str | None] | None
    ) -> None:
        """Install a read-only platform-id -> display-name lookup.

        Used to annotate expanded merge-forward children with who said what.
        The resolver must not raise and must not write anywhere; the worker
        wires it to a recorded-identities query, never a live platform call.
        """

        self._sender_resolver = resolver

    def _require_success(self, response: Any, operation: str) -> None:
        if response.success():
            return
        violation = parse_permission_violation(_permission_payload(response))
        authorization_url = violation.get("consoleUrl") if violation else None
        authorization_url_reason = None
        if violation and self._app_id:
            authorization_url, authorization_url_reason = runtime_permission_url(
                self._app_id, violation["scopes"], violation.get("consoleUrl")
            )
        error = LarkApiError(
            operation,
            _normalized_code(getattr(response, "code", None)),
            getattr(response, "msg", None),
            missing_scopes=tuple(violation["scopes"]) if violation else (),
            authorization_url=authorization_url,
            authorization_url_reason=authorization_url_reason,
        )
        if violation and self._permission_recovery is not None:
            try:
                error.recovery = self._permission_recovery(error)
            except Exception as recovery_error:  # noqa: BLE001 - preserve Lark error
                error.recovery = {
                    "handled": True,
                    "status": "failed",
                    "errorType": type(recovery_error).__name__,
                }
        raise error

    def _record_send(
        self,
        *,
        operation: str,
        target: str,
        response: Any | None = None,
        error: BaseException | None = None,
        native_message_id: str | None = None,
    ) -> None:
        """One secret-free send-outcome line: code, msg, native message id.

        The message body never reaches the log — only what the platform
        answered.  Platform text still passes through the shared logger
        redaction (URLs/tokens stripped), so a misbehaving ``msg`` cannot
        smuggle a credential into the adapter log.  ``msg=None`` stays a
        JSON null rather than the string "None".
        """

        logger = self._logger
        if logger is None:
            return
        if native_message_id is None and response is not None:
            data = getattr(response, "data", None)
            if data is not None:
                candidate = getattr(data, "message_id", None)
                if isinstance(candidate, str):
                    native_message_id = candidate
        if isinstance(error, LarkApiError):
            code = error.code
            msg = error.lark_message
        else:
            code = _normalized_code(getattr(response, "code", None))
            msg = getattr(response, "msg", None)
            if msg is not None and not isinstance(msg, str):
                msg = str(msg)
        fields: dict[str, object] = {
            "operation": operation,
            "code": code,
            "msg": msg,
            "nativeMessageId": (
                native_message_id
                if isinstance(native_message_id, str) and native_message_id
                else None
            ),
            "target": target,
        }
        if self._gateway_name:
            fields["gateway"] = self._gateway_name
        if error is not None and not isinstance(error, LarkApiError):
            fields["errorType"] = type(error).__name__
        try:
            logger.log(
                "warn" if error is not None else "info",
                "lark.send",
                **fields,
            )
        except (NameError, ImportError):
            raise
        except OSError:
            # The platform answer is already recorded in the raised error;
            # losing the local visibility line must not break the send.
            pass

    def bot_open_id(self) -> str | None:
        """This app's own bot open_id, fetched once and cached per process.

        Identity, not name: bot-mention routing compares the mentioned
        open_id against this value and never matches display names (names
        are mutable and collide).  Any platform failure answers ``None`` --
        the caller then declines to treat the message as a bot mention
        instead of guessing -- and is retried on the next call rather than
        cached.
        """

        if self._bot_open_id is not None:
            return self._bot_open_id
        try:
            request = (
                lark.BaseRequest.builder()
                .http_method(lark.HttpMethod.GET)
                .uri("/open-apis/bot/v3/info")
                .token_types({lark.AccessTokenType.TENANT})
                .build()
            )
            response = self._client.request(request)
            raw = getattr(response, "raw", None)
            content = getattr(raw, "content", None)
            payload = json.loads(content) if content else None
        except Exception:  # noqa: BLE001 - never let identity lookup break inbound
            return None
        if not isinstance(payload, dict) or payload.get("code") != 0:
            return None
        bot = payload.get("bot")
        open_id = bot.get("open_id") if isinstance(bot, dict) else None
        if isinstance(open_id, str) and open_id:
            self._bot_open_id = open_id
            return open_id
        return None

    def bot_info(self) -> LarkBotInfo:
        """This app's own bot identity, loudly (``bot/v3/info``).

        Unlike :meth:`bot_open_id` -- which degrades to ``None`` so routing
        can decline instead of guessing -- identity *collection* must not
        silently record nothing, so every failure raises.
        """

        request = (
            lark.BaseRequest.builder()
            .http_method(lark.HttpMethod.GET)
            .uri("/open-apis/bot/v3/info")
            .token_types({lark.AccessTokenType.TENANT})
            .build()
        )
        response = self._client.request(request)
        raw = getattr(response, "raw", None)
        content = getattr(raw, "content", None)
        try:
            payload = json.loads(content) if content else None
        except (ValueError, TypeError) as error:
            raise LarkSdkError("bot info returned an unreadable body") from error
        if not isinstance(payload, dict) or payload.get("code") != 0:
            code = payload.get("code") if isinstance(payload, dict) else None
            raise LarkSdkError(f"bot info failed: code={code}")
        bot = payload.get("bot")
        open_id = bot.get("open_id") if isinstance(bot, dict) else None
        if not isinstance(open_id, str) or not open_id:
            raise LarkSdkError("bot info returned no open_id")
        app_name = bot.get("app_name") if isinstance(bot, dict) else None
        return LarkBotInfo(
            open_id=open_id,
            app_name=(
                app_name if isinstance(app_name, str) and app_name else None
            ),
        )

    def list_chats_page(
        self, *, page_token: str | None = None, page_size: int = 50
    ) -> tuple[tuple[LarkChatSummary, ...], str | None]:
        """One page of the groups this app belongs to (``im/v1/chats``).

        Deliberately page-at-a-time: the caller owns the ``page_token`` loop
        and its bounds, so tests can prove the loop drains every page — a
        truncated member view once produced a confidently wrong who-is-who.
        """

        builder = ListChatRequest.builder().page_size(page_size)
        if page_token:
            builder.page_token(page_token)
        response = self._client.im.v1.chat.list(builder.build())
        self._require_success(response, "list chats")
        data = response.data
        chats = tuple(
            LarkChatSummary(
                chat_id=item.chat_id,
                name=item.name if isinstance(item.name, str) and item.name else None,
            )
            for item in (data.items if data else None) or ()
            if isinstance(item.chat_id, str) and item.chat_id
        )
        next_token = (
            data.page_token if data and data.has_more and data.page_token else None
        )
        return chats, next_token

    def list_chat_members_page(
        self,
        chat_id: str,
        *,
        page_token: str | None = None,
        page_size: int = 50,
    ) -> tuple[tuple[LarkChatMember, ...], str | None, int | None]:
        """One page of a group's members, keyed by open_id.

        Also returns the platform's own ``member_total`` so a caller can
        prove the pagination loop actually collected everyone.
        """

        builder = (
            GetChatMembersRequest.builder()
            .chat_id(chat_id)
            .member_id_type("open_id")
            .page_size(page_size)
        )
        if page_token:
            builder.page_token(page_token)
        response = self._client.im.v1.chat_members.get(builder.build())
        self._require_success(response, "list chat members")
        data = response.data
        members = tuple(
            LarkChatMember(
                open_id=item.member_id,
                name=item.name if isinstance(item.name, str) and item.name else None,
            )
            for item in (data.items if data else None) or ()
            if isinstance(item.member_id, str) and item.member_id
        )
        next_token = (
            data.page_token if data and data.has_more and data.page_token else None
        )
        member_total = (
            data.member_total
            if data and isinstance(data.member_total, int)
            else None
        )
        return members, next_token, member_total

    def add_reaction(self, message_id: str, emoji_type: str) -> None:
        emoji = Emoji.builder().emoji_type(emoji_type).build()
        body = CreateMessageReactionRequestBody.builder().reaction_type(emoji).build()
        request = (
            CreateMessageReactionRequest.builder()
            .message_id(message_id)
            .request_body(body)
            .build()
        )
        response = self._client.im.v1.message_reaction.create(request)
        self._require_success(response, "add reaction")

    def send_owner_dm(
        self, open_id: str, text: str, *, idempotency_key: str
    ) -> str:
        platform_uuid = str(
            uuid5(NAMESPACE_URL, f"hyprial:lark-owner-dm:{idempotency_key}")
        )
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(open_id)
            .msg_type("text")
            .content(encode_lark_text_content(text))
            .uuid(platform_uuid)
            .build()
        )
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(body)
            .build()
        )
        response: Any | None = None
        try:
            response = self._client.im.v1.message.create(request)
            self._require_success(response, "send owner DM")
            native_message_id = response.data.message_id if response.data else None
            if not native_message_id:
                raise LarkSdkError("send owner DM returned no message id")
        except Exception as error:  # noqa: BLE001 - boundary; log then re-raise
            self._record_send(
                operation="send owner DM",
                target=open_id,
                response=response,
                error=error,
            )
            raise
        self._record_send(
            operation="send owner DM",
            target=open_id,
            response=response,
            native_message_id=native_message_id,
        )
        return native_message_id

    def send_chat(
        self, chat_id: str, text: str, *, idempotency_key: str
    ) -> str:
        """Post to a chat the app is a member of (route nativeId target)."""

        platform_uuid = str(
            uuid5(NAMESPACE_URL, f"hyprial:lark-route-send:{idempotency_key}")
        )
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("text")
            .content(encode_lark_text_content(text))
            .uuid(platform_uuid)
            .build()
        )
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(body)
            .build()
        )
        response: Any | None = None
        try:
            response = self._client.im.v1.message.create(request)
            self._require_success(response, "send chat message")
            native_message_id = response.data.message_id if response.data else None
            if not native_message_id:
                raise LarkSdkError("send chat message returned no message id")
        except Exception as error:  # noqa: BLE001 - boundary; log then re-raise
            self._record_send(
                operation="send chat message",
                target=chat_id,
                response=response,
                error=error,
            )
            raise
        self._record_send(
            operation="send chat message",
            target=chat_id,
            response=response,
            native_message_id=native_message_id,
        )
        return native_message_id

    def send_chat_file(
        self,
        chat_id: str,
        path: Path,
        *,
        media_type: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Upload one local file and send it as an IM ``file`` message."""

        file_type = lark_file_type(path)
        try:
            size_bytes = path.stat().st_size
            with path.open("rb") as stream:
                upload_body = (
                    CreateFileRequestBody.builder()
                    .file_type(file_type)
                    .file_name(path.name)
                    .file(stream)
                    .build()
                )
                upload_request = (
                    CreateFileRequest.builder().request_body(upload_body).build()
                )
                upload_response = self._client.im.v1.file.create(upload_request)
        except OSError as error:
            raise LarkSdkError(f"open file for upload failed: {path}: {error}") from error
        self._require_success(upload_response, "upload file")
        upload_file_key = (
            upload_response.data.file_key if upload_response.data else None
        )
        if not isinstance(upload_file_key, str) or not upload_file_key:
            raise LarkSdkError("upload file returned no file key")
        native_message_id, sent_content = self._send_chat_resource(
            chat_id,
            message_type="file",
            content={"file_key": upload_file_key},
            idempotency_key=idempotency_key,
        )
        message_file_key = sent_content.get("file_key")
        if not isinstance(message_file_key, str) or not message_file_key:
            raise LarkSdkError("sent file message returned no file key")
        return {
            "kind": "file",
            "fileName": path.name,
            "mediaType": media_type,
            "sizeBytes": size_bytes,
            "fileType": file_type,
            "fileKey": message_file_key,
            "nativeMessageId": native_message_id,
        }

    def send_chat_image(
        self,
        chat_id: str,
        path: Path,
        *,
        media_type: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Upload one local image and send it as an IM ``image`` message."""

        try:
            size_bytes = path.stat().st_size
            with path.open("rb") as stream:
                upload_body = (
                    CreateImageRequestBody.builder()
                    .image_type("message")
                    .image(stream)
                    .build()
                )
                upload_request = (
                    CreateImageRequest.builder().request_body(upload_body).build()
                )
                upload_response = self._client.im.v1.image.create(upload_request)
        except OSError as error:
            raise LarkSdkError(
                f"open image for upload failed: {path}: {error}"
            ) from error
        self._require_success(upload_response, "upload image")
        upload_image_key = (
            upload_response.data.image_key if upload_response.data else None
        )
        if not isinstance(upload_image_key, str) or not upload_image_key:
            raise LarkSdkError("upload image returned no image key")
        native_message_id, sent_content = self._send_chat_resource(
            chat_id,
            message_type="image",
            content={"image_key": upload_image_key},
            idempotency_key=idempotency_key,
        )
        message_image_key = sent_content.get("image_key")
        if not isinstance(message_image_key, str) or not message_image_key:
            raise LarkSdkError("sent image message returned no image key")
        return {
            "kind": "image",
            "fileName": path.name,
            "mediaType": media_type,
            "sizeBytes": size_bytes,
            "imageKey": message_image_key,
            "nativeMessageId": native_message_id,
        }

    def _send_chat_resource(
        self,
        chat_id: str,
        *,
        message_type: str,
        content: dict[str, str],
        idempotency_key: str,
    ) -> tuple[str, dict[str, Any]]:
        platform_uuid = str(
            uuid5(
                NAMESPACE_URL,
                f"hyprial:lark-route-{message_type}:{idempotency_key}",
            )
        )
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type(message_type)
            .content(json.dumps(content, ensure_ascii=False, separators=(",", ":")))
            .uuid(platform_uuid)
            .build()
        )
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(body)
            .build()
        )
        response: Any | None = None
        try:
            response = self._client.im.v1.message.create(request)
            self._require_success(response, f"send chat {message_type} message")
            response_data = response.data
            native_message_id = response_data.message_id if response_data else None
            if not native_message_id:
                raise LarkSdkError(
                    f"send chat {message_type} message returned no message id"
                )
            response_body = getattr(response_data, "body", None)
            raw_content = getattr(response_body, "content", None)
            try:
                sent_content = json.loads(raw_content)
            except (TypeError, json.JSONDecodeError) as error:
                raise LarkSdkError(
                    f"send chat {message_type} message returned invalid content"
                ) from error
            if not isinstance(sent_content, dict):
                raise LarkSdkError(
                    f"send chat {message_type} message returned non-object content"
                )
        except Exception as error:  # noqa: BLE001 - boundary; log then re-raise
            self._record_send(
                operation=f"send chat {message_type} message",
                target=chat_id,
                response=response,
                error=error,
            )
            raise
        self._record_send(
            operation=f"send chat {message_type} message",
            target=chat_id,
            response=response,
            native_message_id=native_message_id,
        )
        return native_message_id, sent_content

    def clear_reaction(self, message_id: str, emoji_type: str) -> None:
        request = (
            ListMessageReactionRequest.builder()
            .message_id(message_id)
            .reaction_type(emoji_type)
            .page_size(50)
            .build()
        )
        response = self._client.im.v1.message_reaction.list(request)
        self._require_success(response, "list reactions")
        items = response.data.items if response.data else None
        for reaction in items or ():
            operator = reaction.operator
            if operator is None or operator.operator_type != "app":
                continue
            reaction_id = reaction.reaction_id
            if not reaction_id:
                continue
            delete_request = (
                DeleteMessageReactionRequest.builder()
                .message_id(message_id)
                .reaction_id(reaction_id)
                .build()
            )
            delete_response = self._client.im.v1.message_reaction.delete(delete_request)
            self._require_success(delete_response, "delete reaction")

    def reply(
        self, message_id: str, text: str, *, idempotency_key: str
    ) -> str:
        body = (
            ReplyMessageRequestBody.builder()
            .msg_type("text")
            .content(encode_lark_text_content(text))
            .uuid(str(uuid5(NAMESPACE_URL, f"hyprial:lark-reply:{idempotency_key}")))
            .build()
        )
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(body)
            .build()
        )
        response: Any | None = None
        try:
            response = self._client.im.v1.message.reply(request)
            self._require_success(response, "reply message")
            native_message_id = response.data.message_id if response.data else None
            if not native_message_id:
                raise LarkSdkError("reply message returned no message id")
        except Exception as error:  # noqa: BLE001 - boundary; log then re-raise
            self._record_send(
                operation="reply message",
                target=message_id,
                response=response,
                error=error,
            )
            raise
        self._record_send(
            operation="reply message",
            target=message_id,
            response=response,
            native_message_id=native_message_id,
        )
        return native_message_id

    def get_message(self, message_id: str) -> QuotedMessage | None:
        """Fetch one message as quotable context.

        The response is a *list*: index 0 is the requested message and, for a
        merge-forward, every following item is one of its children.  Both the
        shell and each child go through the shared content normalizer, so a
        quoted image/post/file/forward now yields readable context instead of
        raising and collapsing into "could not be resolved".
        """

        request = (
            GetMessageRequest.builder()
            .message_id(message_id)
            .user_id_type("open_id")
            .build()
        )
        response = self._client.im.v1.message.get(request)
        self._require_success(response, "get message")
        items = response.data.items if response.data else None
        if not items:
            return None
        message = items[0]
        sender = message.sender
        body = message.body
        if not message.message_id or sender is None or body is None:
            raise LarkSdkError("get message returned incomplete message data")
        normalized = _normalize_rest_body(
            message, items, resolve_sender=self._sender_resolver
        )
        return QuotedMessage(
            message_id=message.message_id,
            sender_id=sender.id or "unknown",
            sender_type=sender.sender_type or "unknown",
            text=normalized.text,
            message_type=normalized.message_type,
        )

    def get_inbound_message(
        self, message_id: str, *, chat_type: str | None = None
    ) -> LarkInboundMessage | None:
        """Fetch a message by native id for failure recovery (``im.v1.message.get``).

        This is the same endpoint :meth:`get_message` already uses for quoted
        context; here it returns the full inbound shape so a lost event can be
        re-driven through routing and forwarding.
        """

        request = (
            GetMessageRequest.builder()
            .message_id(message_id)
            .user_id_type("open_id")
            .build()
        )
        response = self._client.im.v1.message.get(request)
        self._require_success(response, "get message")
        items = response.data.items if response.data else None
        if not items:
            return None
        return _inbound_from_rest_message(
            items[0],
            event_id=f"recover:{message_id}",
            chat_type=chat_type,
            # A recovered merge-forward expands from this same response; the
            # trailing items are its children, not sibling chat messages.
            items=items,
            resolve_sender=self._sender_resolver,
        )

    def download_message_resource(
        self,
        message_id: str,
        file_key: str,
        *,
        resource_type: str,
    ) -> tuple[bytes, str | None, str | None]:
        """Fetch one media resource carried by a message (``im/v1`` resources).

        ``resource_type`` is the platform's ``image``/``file`` discriminator.
        Returns ``(payload, file_name, content_type)``; the latter two come
        from response headers and may be absent.  Failures raise the shared
        :class:`LarkApiError`/:class:`LarkSdkError` — callers that report
        errors outward must summarize them without the platform's own text.
        """

        request = (
            GetMessageResourceRequest.builder()
            .message_id(message_id)
            .file_key(file_key)
            .type(resource_type)
            .build()
        )
        response = self._client.im.v1.message_resource.get(request)
        self._require_success(response, "get message resource")
        stream = getattr(response, "file", None)
        if stream is None:
            raise LarkSdkError("get message resource returned no payload")
        payload = stream.read()
        if not isinstance(payload, bytes):
            raise LarkSdkError("get message resource returned a non-binary payload")
        file_name = getattr(response, "file_name", None)
        raw = getattr(response, "raw", None)
        headers = getattr(raw, "headers", None) or {}
        content_type = next(
            (
                value
                for key, value in headers.items()
                if isinstance(key, str)
                and key.lower() == "content-type"
                and isinstance(value, str)
            ),
            None,
        )
        return (
            payload,
            file_name if isinstance(file_name, str) and file_name else None,
            content_type,
        )

    def list_chat_messages(
        self,
        chat_id: str,
        *,
        start_time: str | None = None,
        end_time: str | None = None,
        page_size: int = 50,
        max_pages: int = 20,
        chat_type: str | None = None,
    ) -> LarkHistoryBatch:
        """Pull recent chat history (``im.v1.message.list``) for reconciliation.

        ``start_time``/``end_time`` are second-level unix timestamps per the
        platform contract. Newest messages are requested first so a bounded
        sweep prioritizes the outage edge. Exhausting ``max_pages`` is an
        explicit failure rather than a false healthy result.
        """

        messages: list[LarkInboundMessage] = []
        page_token: str | None = None
        truncated = False
        for page_index in range(max_pages):
            builder = (
                ListMessageRequest.builder()
                .container_id_type("chat")
                .container_id(chat_id)
                .sort_type("ByCreateTimeDesc")
                .page_size(page_size)
            )
            if start_time:
                builder.start_time(start_time)
            if end_time:
                builder.end_time(end_time)
            if page_token:
                builder.page_token(page_token)
            response = self._client.im.v1.message.list(builder.build())
            self._require_success(response, "list chat messages")
            data = response.data
            items = data.items if data else None
            for item in items or ():
                inbound = _inbound_from_rest_message(
                    item,
                    event_id=f"reconcile:{item.message_id}",
                    chat_type=chat_type,
                    resolve_sender=self._sender_resolver,
                )
                if inbound is not None:
                    messages.append(inbound)
            if not data or not data.has_more or not data.page_token:
                break
            if page_index + 1 == max_pages:
                truncated = True
                break
            page_token = data.page_token
        return LarkHistoryBatch(messages, complete=not truncated)


EventClientFactory = Callable[[str, str, Any], Any]


class LarkEventStream:
    """Blocking official-SDK WebSocket event source.

    Process supervision belongs to the daemon. ``start`` deliberately preserves
    the SDK's blocking contract so the daemon can own its thread/process policy.

    ⚠️ **One event stream per process.**  ``lark_oapi.ws.client`` holds a
    module-level event loop and drives it with ``run_until_complete`` inside
    ``start``, so a second stream started in the same process raises
    ``RuntimeError: This event loop is already running``.  Two Lark adapters
    therefore cannot both run their inbound streams under one interpreter: a
    multi-adapter topology needs a process per stream, not a thread per
    stream.  There is also no stop -- ``start`` never returns and the SDK
    exposes no shutdown -- so ending a stream means ending its process.

    Both facts were established by running it (see
    ``contract/e2e-scenarios/run-lark-standin-selfcheck.py``, which owns a
    process for exactly this reason), not read off the vendor's
    documentation, which states neither.
    """

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        on_message: Callable[[Any], Any],
        on_ready: Callable[[], None] | None = None,
        on_reconnect: Callable[[], None] | None = None,
        on_transport_activity: Callable[[], None] | None = None,
        on_event_activity: Callable[[], None] | None = None,
        on_member_change: Callable[[Any], Any] | None = None,
        client_factory: EventClientFactory | None = None,
    ) -> None:
        def observed_message(event: Any) -> Any:
            if on_event_activity is not None:
                on_event_activity()
            return on_message(event)

        dispatcher_builder = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(observed_message)
        )
        if on_member_change is not None:
            # Delivered only when the App's platform-side event subscription
            # includes im.chat.member.user.added/deleted_v1; registering the
            # handler alone does not subscribe (harmless when unsubscribed).
            def observed_member_change(event: Any) -> Any:
                if on_event_activity is not None:
                    on_event_activity()
                return on_member_change(event)

            dispatcher_builder = (
                dispatcher_builder.register_p2_im_chat_member_user_added_v1(
                    observed_member_change
                ).register_p2_im_chat_member_user_deleted_v1(
                    observed_member_change
                )
            )
        dispatcher = dispatcher_builder.build()
        if client_factory is not None:
            self._client = client_factory(app_id, app_secret, dispatcher)
        else:
            self._client = self._default_factory(
                app_id,
                app_secret,
                dispatcher,
                on_ready=on_ready,
                on_reconnect=on_reconnect,
                on_transport_activity=on_transport_activity,
            )

    @staticmethod
    def _default_factory(
        app_id: str,
        app_secret: str,
        dispatcher: Any,
        *,
        on_ready: Callable[[], None] | None = None,
        on_reconnect: Callable[[], None] | None = None,
        on_transport_activity: Callable[[], None] | None = None,
    ) -> Any:
        class ReadyClient(lark.ws.Client):
            # Websocket events are never replayed by the platform: messages
            # sent during a disconnect window are lost unless fetched.  The
            # first connect signals readiness; every later (re)connect fires
            # ``on_reconnect`` so the worker can reconcile missed history.
            ever_connected = False

            async def _handle_message(self, message: bytes) -> None:
                # Count control frames (notably PONG) as transport activity.
                # This is deliberately separate from business events so a quiet
                # chat remains healthy while the subscribed websocket is alive.
                if on_transport_activity is not None:
                    on_transport_activity()
                await super()._handle_message(message)

            async def _connect(self) -> None:
                was_connected = self._conn is not None
                await super()._connect()
                if was_connected or self._conn is None:
                    return
                if not self.ever_connected:
                    self.ever_connected = True
                    if on_ready is not None:
                        on_ready()
                elif on_reconnect is not None:
                    on_reconnect()

        # ``domain`` steers two things at once here: the HTTP
        # endpoint-discovery request that precedes every handshake (what
        # ``probe_rest_endpoint`` re-runs) and the websocket the returned URL
        # points at.  Passing it is therefore not optional for a redirected
        # process -- omitting it leaves the inbound half on production while
        # the outbound half has moved.
        return ReadyClient(
            app_id,
            app_secret,
            event_handler=dispatcher,
            domain=lark_base_url(),
            auto_reconnect=True,
        )

    def start(self) -> None:
        self._client.start()

    def probe_rest_endpoint(self) -> None:
        """Actively re-run the SDK's credentialed endpoint-discovery request.

        ``_get_conn_url`` is the HTTP/REST request the official client itself
        uses before every websocket handshake.  It verifies network reachability
        and app credentials without sending a chat message or consuming a
        production conversation.  The returned one-time URL is intentionally
        discarded and never logged.
        """

        self._client._get_conn_url()
