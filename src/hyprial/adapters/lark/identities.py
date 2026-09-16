"""Active identity collection for Lark adapters: who is who, on record.

Motivation (real incident): a coordinator once had to @-mention a person in
a group and deduced who they were "by elimination" from a member view that
had been silently truncated mid-page — the pagination was never drained, so
the premise ("the group has 3 humans") was simply false.  Identity questions
must be answered from recorded data with provenance, never from live-chat
deduction.  This module is the recorded-data side: it enumerates what the
platform will state on request (the app's own bot identity, the groups the
app belongs to, and every member of those groups — every page of them) and
persists it into the ``identities`` table.

Namespacing (verified in production): Feishu open_ids are scoped *per App*.
The same human has a different open_id under every App, so enumeration must
use the adapter's own App credential — ids collected through another App can
never match the sender ids this adapter's events carry.  Rows are therefore
keyed by adapter, and only ``union_id`` joins identities across adapters.

p2p conversations expose no member-list API; their human counterparts are
collected passively from live events by the adapter instead.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .api import LarkBotInfo, LarkChatMember, LarkChatSummary
from .state import LarkStateStore

#: Hard bound per pagination loop.  Exhausting it is reported as an explicit
#: incomplete result — never a silently short listing (that silence is the
#: exact failure mode this module exists to kill).
MAX_SYNC_PAGES = 200


@runtime_checkable
class IdentityCollectionPort(Protocol):
    """What the collector needs from the platform, page by page.

    Deliberately page-at-a-time: the ``page_token`` loop and its bounds live
    in :func:`sync_identities`, where tests can prove every page is drained.
    """

    def bot_info(self) -> LarkBotInfo: ...

    def list_chats_page(
        self, *, page_token: str | None = None, page_size: int = 50
    ) -> tuple[tuple[LarkChatSummary, ...], str | None]: ...

    def list_chat_members_page(
        self,
        chat_id: str,
        *,
        page_token: str | None = None,
        page_size: int = 50,
    ) -> tuple[tuple[LarkChatMember, ...], str | None, int | None]: ...


def adapter_namespace(adapter: str) -> str:
    """The identities/state namespace of one configured Lark adapter.

    Must match what the adapter worker passes to :class:`LarkStateStore`
    (``lark:<name>``), or CLI queries would look at an empty namespace.
    """

    return f"lark:{adapter}"


def open_identity_store(state_dir: Path, adapter: str) -> LarkStateStore:
    """Open the shared adapters database in one adapter's namespace."""

    return LarkStateStore(
        state_dir / "adapters.sqlite3", adapter=adapter_namespace(adapter)
    )


def configured_identity_gateway(
    hyprial_home: Path, state_dir: Path, adapter: str
) -> tuple[str, Any]:
    """Resolve one adapter's own App credential into an SDK gateway.

    The adapter's *own* credential is load-bearing, not a convenience: ids
    enumerated through any other App belong to a different open_id namespace
    and would never match this adapter's event traffic.
    """

    from .scopes import _configured_gateway
    from .sdk import LarkSdkGateway

    store, gateway = _configured_gateway(hyprial_home, state_dir, adapter)
    secret = store.lark_app_secret(gateway.credential_ref)
    return gateway.app_id, LarkSdkGateway.from_credentials(gateway.app_id, secret)


def _drain_chat_pages(
    gateway: IdentityCollectionPort, *, max_pages: int
) -> tuple[tuple[LarkChatSummary, ...], bool]:
    chats: list[LarkChatSummary] = []
    page_token: str | None = None
    for _page in range(max_pages):
        page, page_token = gateway.list_chats_page(page_token=page_token)
        chats.extend(page)
        if page_token is None:
            return tuple(chats), True
    return tuple(chats), False


def _drain_member_pages(
    gateway: IdentityCollectionPort, chat_id: str, *, max_pages: int
) -> tuple[tuple[LarkChatMember, ...], bool, int | None]:
    members: list[LarkChatMember] = []
    page_token: str | None = None
    member_total: int | None = None
    for _page in range(max_pages):
        page, page_token, total = gateway.list_chat_members_page(
            chat_id, page_token=page_token
        )
        members.extend(page)
        if total is not None:
            member_total = total
        if page_token is None:
            return tuple(members), True, member_total
    return tuple(members), False, member_total


def sync_identities(
    *,
    state: LarkStateStore,
    gateway: IdentityCollectionPort,
    app_id: str,
    now_ms: int | None = None,
    max_pages: int = MAX_SYNC_PAGES,
) -> dict[str, Any]:
    """Enumerate and record everything this App will state about identities.

    Records, all as ``observed`` (a sync is mechanical, never a human
    confirmation): the App itself (kind ``app``, keyed by app_id), its bot
    presence (kind ``bot``, keyed by the bot's open_id), and every member of
    every group the App belongs to (kind ``user``), draining every
    pagination cursor.  Existing ``verified`` rows keep their standing,
    owner and source; observations only refresh platform facts.

    The report says exactly how far it got: per-chat member totals from the
    platform versus members actually recorded, an explicit ``complete``
    flag per enumeration, and per-chat errors (type names only — platform
    error text can carry URLs or tokens and stays out of reports).
    """

    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    errors: list[str] = []

    info = gateway.bot_info()
    source = f"bot-info:{app_id}"
    state.observe_identity(
        "app", app_id, display_name=info.app_name, source=source, now_ms=now_ms
    )
    state.observe_identity(
        "bot",
        info.open_id,
        display_name=info.app_name,
        source=source,
        now_ms=now_ms,
    )
    recorded = 2

    chats, chats_complete = _drain_chat_pages(gateway, max_pages=max_pages)
    if not chats_complete:
        errors.append(f"chats: enumeration-incomplete (page limit {max_pages})")

    chat_reports: list[dict[str, Any]] = []
    for chat in chats:
        try:
            members, complete, member_total = _drain_member_pages(
                gateway, chat.chat_id, max_pages=max_pages
            )
        except Exception as error:  # noqa: BLE001 - per-chat isolation
            errors.append(
                f"{chat.chat_id}: members-unavailable"
                f" ({type(error).__name__})"
            )
            continue
        if not complete:
            errors.append(
                f"{chat.chat_id}: members-incomplete (page limit {max_pages})"
            )
        for member in members:
            state.observe_identity(
                "user",
                member.open_id,
                display_name=member.name,
                source=f"chat-members:{chat.chat_id}",
                now_ms=now_ms,
            )
        recorded += len(members)
        chat_reports.append(
            {
                "chatId": chat.chat_id,
                "name": chat.name,
                "memberTotal": member_total,
                "membersRecorded": len(members),
                "complete": complete,
            }
        )

    return {
        "ok": not errors,
        "appId": app_id,
        "bot": {"openId": info.open_id, "appName": info.app_name},
        "chats": chat_reports,
        "chatsComplete": chats_complete,
        "identitiesRecorded": recorded,
        "errors": errors,
    }
