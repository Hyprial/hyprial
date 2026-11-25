"""Approved identity probes; credentials/profile cache remain entrypoint owned."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lark_oapi.api.im.v1 import GetChatRequest
from feishu.evidence import ContractBlocked, ContractFailure, safe_cli_error


@dataclass(frozen=True)
class IdentityPorts:
    APP_ID: str
    LARK_CLI: Path
    entrypoint: str


def _chat_mode(client: Any, chat_id: str) -> str | None:
    request = GetChatRequest.builder().chat_id(chat_id).build()
    response = client.im.v1.chat.get(request)
    if not response.success():
        return None
    chat = response.data
    return getattr(chat, "chat_mode", None) if chat else None


def _bot_open_id(app_id: str, app_secret: str) -> str:
    """The test App bot's own open_id, for @mentioning it in group mode."""

    import urllib.request

    token_request = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(token_request, timeout=15) as response:
        token = json.loads(response.read().decode(), strict=False).get(
            "tenant_access_token"
        )
    if not token:
        raise ContractBlocked("approved E2E App could not mint a tenant token")
    info_request = urllib.request.Request(
        "https://open.feishu.cn/open-apis/bot/v3/info",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(info_request, timeout=15) as response:
        bot = json.loads(response.read().decode(), strict=False).get("bot") or {}
    open_id = bot.get("open_id")
    if not isinstance(open_id, str) or not open_id:
        raise ContractBlocked("approved E2E App bot open_id is unavailable")
    return open_id


def _identity_criteria(auth: Mapping[str, Any]) -> dict[str, Any]:
    """The exact judged values, for a diagnosable BLOCKED (never a token).

    `verified` is reported for diagnosis only: it reflects the default/auto
    identity (a ready bot makes it True while the user identity is missing),
    so it is never a pass criterion (#141).
    """

    identities = auth.get("identities")
    user = identities.get("user") if isinstance(identities, dict) else None
    user = user if isinstance(user, dict) else {}
    return {
        "verified": auth.get("verified"),
        "user.status": user.get("status"),
        "user.tokenStatus": user.get("tokenStatus"),
        "user.present": bool(user),
    }


def _lark_reauth_check_summary(*, _ports: IdentityPorts) -> str:
    """Best-effort: walk the lark-cli device flow to 'one click away' and
    push the link to the configured route (hyprial adapter user-auth check).  Never
    raises; the summary it returns goes into the BLOCKED message so a red
    gate says '凭据问题,链接已推给 X' instead of looking like a product
    failure."""

    executable = os.environ.get(
        "HYPRIAL_BIN",
        str(Path(_ports.entrypoint).resolve().parents[2] / ".venv/bin/hyprial"),
    )
    try:
        result = subprocess.run(
            [executable, "adapter", "user-auth", "check", "--json"],
            text=True,
            capture_output=True,
            timeout=90,
            check=False,
        )
        payload = json.loads(result.stdout) if result.stdout.strip() else {}
    except Exception as error:  # noqa: BLE001 - preflight aid, never fatal
        return f"adapter user-auth check unavailable ({type(error).__name__})"
    if not isinstance(payload, dict):
        return "adapter user-auth check returned an unparseable result"
    if payload.get("ok") is False:
        return (
            "adapter user-auth check failed "
            f"({payload.get('code')}): {payload.get('error')}"
        )
    status = payload.get("status")
    if status == "awaiting_user":
        if payload.get("notified"):
            return f"re-authorization link pushed to {payload.get('notifyRoute')}"
        return (
            "re-authorization link generated but NOT pushed "
            f"(no route/sender configured); link: {payload.get('verificationUrl')}"
        )
    return f"adapter user-auth check status={status}"


def _lookup_e2e_lark_profile(*, _ports: IdentityPorts) -> str:
    environment = dict(os.environ)
    environment.update(
        {
            "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
            "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
        }
    )
    result = subprocess.run(
        [str(_ports.LARK_CLI), "profile", "list"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ContractBlocked(
            f"lark-cli profile list failed ({result.returncode}): "
            f"{safe_cli_error(result.stderr, result.stdout)}"
        )
    try:
        profiles = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ContractFailure("lark-cli returned non-JSON success output") from error
    for profile in profiles if isinstance(profiles, list) else []:
        if isinstance(profile, dict) and profile.get("appId") == _ports.APP_ID:
            name = profile.get("name")
            if isinstance(name, str) and name:
                return name
    raise ContractBlocked(f"lark-cli has no profile for E2E app {_ports.APP_ID}")
