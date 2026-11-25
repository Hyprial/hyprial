from __future__ import annotations

import secrets
from collections.abc import Mapping
from urllib.parse import quote

from .errors import WeixinProtocolError, WeixinRequestNotSentError
from .runtime import WeixinCredentials


class HttpxWeixinTransport:
    """Direct iLink HTTPS transport; `httpx` is imported only in the worker."""

    def __init__(self, client: object | None = None, *, timeout_seconds: float = 35.0) -> None:
        if client is None:
            try:
                import httpx
            except ImportError as exc:
                raise WeixinProtocolError("httpx-dependency-missing") from exc
            client = httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False)
        self._client = client

    @staticmethod
    def _headers(credentials: WeixinCredentials) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {credentials.token}",
            "AuthorizationType": "ilink_bot_token",
            "content-type": "application/json",
            "iLink-App-Id": "bot",
            "iLink-App-ClientVersion": str((2 << 16) | (4 << 8) | 6),
            "X-WECHAT-UIN": secrets.token_hex(4),
        }

    async def _post(
        self, credentials: WeixinCredentials, endpoint: str, body: Mapping[str, object]
    ) -> dict[str, object]:
        response = await self._client.post(  # type: ignore[attr-defined]
            credentials.api_base_url + endpoint,
            headers=self._headers(credentials),
            json=dict(body),
        )
        if response.status_code != 200:
            raise WeixinProtocolError(f"http-{response.status_code}")
        value = response.json()
        if not isinstance(value, dict):
            raise WeixinProtocolError("invalid-response")
        return value

    async def begin_login(
        self, *, local_tokens: tuple[str, ...] = (), bot_type: str = "3"
    ) -> Mapping[str, object]:
        tokens = tuple(dict.fromkeys(token for token in local_tokens if token))[-10:]
        response = await self._client.post(  # type: ignore[attr-defined]
            "https://ilinkai.weixin.qq.com/ilink/bot/get_bot_qrcode"
            f"?bot_type={quote(bot_type, safe='')}",
            headers={
                "iLink-App-Id": "bot",
                "iLink-App-ClientVersion": str((2 << 16) | (4 << 8) | 6),
            },
            json={"local_token_list": list(tokens)},
        )
        if response.status_code != 200:
            raise WeixinProtocolError(f"login-http-{response.status_code}")
        value = response.json()
        if not isinstance(value, dict) or not isinstance(value.get("qrcode"), str):
            raise WeixinProtocolError("login-invalid-response")
        from .codec import normalize_weixin_api_base_url

        qr_url = value.get("qrcode_img_content")
        if not isinstance(qr_url, str):
            raise WeixinProtocolError("login-qr-url-missing")
        # Reuse the strict Weixin host validator without allowing credentials,
        # query stripping is acceptable because the original QR URL is returned.
        normalize_weixin_api_base_url(qr_url)
        return value

    async def poll_login(
        self,
        qrcode: str,
        *,
        verify_code: str | None = None,
        base_url: str = "https://ilinkai.weixin.qq.com/",
    ) -> Mapping[str, object]:
        from .codec import normalize_weixin_api_base_url

        trusted = normalize_weixin_api_base_url(base_url)
        suffix = f"?qrcode={quote(qrcode, safe='')}"
        if verify_code:
            suffix += f"&verify_code={quote(verify_code, safe='')}"
        response = await self._client.get(  # type: ignore[attr-defined]
            trusted + "ilink/bot/get_qrcode_status" + suffix,
            headers={
                "iLink-App-Id": "bot",
                "iLink-App-ClientVersion": str((2 << 16) | (4 << 8) | 6),
            },
        )
        if response.status_code != 200:
            raise WeixinProtocolError(f"login-http-{response.status_code}")
        value = response.json()
        statuses = {
            "wait",
            "scaned",
            "confirmed",
            "expired",
            "scaned_but_redirect",
            "need_verifycode",
            "verify_code_blocked",
            "binded_redirect",
        }
        if not isinstance(value, dict) or value.get("status") not in statuses:
            raise WeixinProtocolError("login-status-invalid")
        if value.get("status") == "confirmed":
            if not all(
                isinstance(value.get(field), str) and value.get(field)
                for field in ("bot_token", "ilink_user_id", "baseurl")
            ):
                raise WeixinProtocolError("login-confirmation-incomplete")
            normalize_weixin_api_base_url(str(value["baseurl"]))
        return value

    async def notify_start(self, credentials: WeixinCredentials) -> None:
        result = await self._post(credentials, "ilink/bot/msg/notifystart", {"base_info": {"channel_version": "2.4.6"}})
        if result.get("ret", result.get("errcode", 0)) not in (0, "0", None):
            raise WeixinProtocolError("start-rejected")

    async def notify_stop(self, credentials: WeixinCredentials) -> None:
        result = await self._post(credentials, "ilink/bot/msg/notifystop", {"base_info": {"channel_version": "2.4.6"}})
        if result.get("ret", result.get("errcode", 0)) not in (0, "0", None):
            raise WeixinProtocolError("stop-rejected")

    async def get_updates(
        self, credentials: WeixinCredentials, cursor: str, *, limit: int
    ) -> Mapping[str, object]:
        return await self._post(
            credentials,
            "ilink/bot/getupdates",
            {"get_updates_buf": cursor, "base_info": {"channel_version": "2.4.6"}},
        )

    async def send_message(
        self, credentials: WeixinCredentials, request: Mapping[str, object]
    ) -> Mapping[str, object]:
        client_id = request.get("idempotencyKey")
        to_user = request.get("toUserId")
        text = request.get("text")
        if not all(isinstance(value, str) and value for value in (client_id, to_user, text)):
            raise WeixinRequestNotSentError("invalid-send-request")
        result = await self._post(
            credentials,
            "ilink/bot/sendmessage",
            {
                "msg": {
                    "from_user_id": "",
                    "to_user_id": to_user,
                    "client_id": client_id,
                    "message_type": 2,
                    "message_state": 2,
                    "item_list": [{"type": 1, "text_item": {"text": text}}],
                },
                "base_info": {"channel_version": "2.4.6"},
            },
        )
        return result


__all__ = ["HttpxWeixinTransport"]
