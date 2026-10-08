"""Official ``lark-oapi`` SDK boundary used by the adapter."""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, uuid5

from hyprial.daemon.impl.adapters.lark.contracts.wire import (
    LarkBotInfo,
    LarkChatMember,
    LarkChatSummary,
    LarkHistoryBatch,
    LarkInboundMessage,
    QuotedMessage,
    encode_lark_text_content,
)
from hyprial.daemon.impl.adapters.lark.contracts.endpoint import lark_base_url
from hyprial.daemon.impl.adapters.lark.credentials.scopes import (
    parse_permission_violation,
    runtime_permission_url,
)

from hyprial.daemon.impl.adapters.lark.outbound.sdk_base import (
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
    LarkApiError,
    LarkSdkError,
    ListChatRequest,
    ListMessageReactionRequest,
    ListMessageRequest,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
    _normalized_code,
    lark,
    lark_file_type,
)
from hyprial.daemon.impl.adapters.lark.outbound.sdk_translate import (
    _inbound_from_rest_message,
    _normalize_rest_body,
    _permission_payload,
)
if TYPE_CHECKING:
    from hyprial.kernel import Logger

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
        name: str,
        data: bytes,
        *,
        media_type: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Upload authorized file bytes and send them as an IM ``file`` message.

        The bytes were copied by ``path_authz``; this never opens a path.
        """

        file_type = lark_file_type(Path(name))
        size_bytes = len(data)
        upload_body = (
            CreateFileRequestBody.builder()
            .file_type(file_type)
            .file_name(name)
            .file(io.BytesIO(data))
            .build()
        )
        upload_request = CreateFileRequest.builder().request_body(upload_body).build()
        upload_response = self._client.im.v1.file.create(upload_request)
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
            "fileName": name,
            "mediaType": media_type,
            "sizeBytes": size_bytes,
            "fileType": file_type,
            "fileKey": message_file_key,
            "nativeMessageId": native_message_id,
        }

    def send_chat_image(
        self,
        chat_id: str,
        name: str,
        data: bytes,
        *,
        media_type: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Upload authorized image bytes and send them as an IM ``image`` message.

        The bytes were copied by ``path_authz``; this never opens a path.
        """

        size_bytes = len(data)
        upload_body = (
            CreateImageRequestBody.builder()
            .image_type("message")
            .image(io.BytesIO(data))
            .build()
        )
        upload_request = CreateImageRequest.builder().request_body(upload_body).build()
        upload_response = self._client.im.v1.image.create(upload_request)
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
            "fileName": name,
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
            message,
            items,
            resolve_sender=self._sender_resolver,
            logger=self._logger,
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
            logger=self._logger,
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
                    logger=self._logger,
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
