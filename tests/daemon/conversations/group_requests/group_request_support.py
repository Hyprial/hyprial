"""Harness for the group conversation request tests (slice GC2).

A golden daemon with id-form agents (alpha, beta, gamma) and the owner's
user agent, its conversation placement composed over an isolated
``LocalOrgFs`` (the recorder thread is not started).  The golden clock is
frozen, so every call lands on one millisecond unless a test advances it.

The four group methods are invoked through a ``MethodRegistry`` built from
``GROUP_BINDINGS`` alone, which validates params, declared error codes and
results exactly as the production registry does; ``test_group_wiring``
calls them through the daemon itself.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.daemon.impl.composition.conversations import compose_conversation_recorder
from hyprial.daemon.impl.orgfs import LocalOrgFs
from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY, MethodRegistry
from hyprial.daemon.impl.request_catalog.conversations.groups import GROUP_BINDINGS
from hyprial.daemon.impl.user_agent import USER_AGENT_NAME
from hyprial.kernel import parse_agent_uri
from tests.daemon.conversations.conversation_support import OWNER_PRINCIPAL, FlakyOrgFs
from tests.support.golden.app import SESSIONS, GoldenDaemon

OWNER = OWNER_PRINCIPAL
GROUPS = MethodRegistry.from_bindings(GROUP_BINDINGS)


@dataclass
class GroupDaemon:
    """A golden daemon whose group documents the request methods can use."""

    daemon: GoldenDaemon
    fs: FlakyOrgFs

    @property
    def app(self) -> Any:
        return self.daemon.app

    def address(self, name: str) -> str:
        return str(self.app.agents.require(name).uri)

    def agent_id(self, name: str) -> str:
        parsed = parse_agent_uri(self.address(name))
        assert parsed is not None
        return parsed[2]

    def user_agent(self) -> Any:
        return self.app.agents.require(USER_AGENT_NAME)

    def caller(self, who: str) -> dict[str, str]:
        if who == OWNER:
            return {"from": OWNER}
        return {"from": self.address(who), "sessionRef": SESSIONS[who]}

    def call(self, method: str, who: str = OWNER, **params: Any) -> dict[str, Any]:
        return GROUPS.invoke(
            self.app, method, {**self.caller(who), **params}, trusted_message_origin="ipc"
        )

    def refused(self, method: str, who: str = OWNER, **params: Any) -> DaemonRequestError:
        with pytest.raises(DaemonRequestError) as raised:
            self.call(method, who, **params)
        return raised.value

    def create(self, members: list[str], *, title: str = "release", key: str = "k-create",
               who: str = OWNER) -> dict[str, Any]:
        return self.call("conversations.create", who, title=title, members=members,
                         idempotencyKey=key)

    def listed(self, who: str = OWNER, **params: Any) -> dict[str, Any]:
        return METHOD_REGISTRY.invoke(
            self.app, "conversations.list", {**self.caller(who), **params},
            trusted_message_origin="ipc",
        )

    def advance(self, ms: int = 10) -> None:
        self.daemon.clock.advance(ms)


def group_daemon(
    golden_daemon: Callable[..., GoldenDaemon],
    monkeypatch: pytest.MonkeyPatch,
    *,
    id_form: bool = True,
) -> GroupDaemon:
    daemon = golden_daemon(id_form=id_form, user_agent=True)
    app = daemon.app
    fs = FlakyOrgFs(LocalOrgFs(author=OWNER))
    monkeypatch.setattr(
        app, "_require_orgfs_runtime", lambda: SimpleNamespace(facade=fs), raising=False
    )
    compose_conversation_recorder(app, fs=fs, start=False, clock_ms=daemon.clock.ms)
    assert getattr(app, "_conversation_placement", None) is not None
    return GroupDaemon(daemon, fs)


def member_ids(answer: dict[str, Any]) -> list[str]:
    return [member["memberId"] for member in answer["members"]]


__all__ = ["GROUPS", "OWNER", "GroupDaemon", "group_daemon", "member_ids"]
