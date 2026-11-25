"""Adapter safety: serialized check-then-act, total error mapping, interactive-session refusal."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.daemon.impl.provider_keys import legacy
from hyprial.kernel import DaemonRequestError, ipc_errors

SENTINEL = "sk-legacy-sentinel-value-000"
WRITE = {"entryId": "deepseek", "fieldName": "key", "value": SENTINEL}
GRANT = {
    "actor": "alpha", "grantId": "slot", "source": "user-provider", "entryId": "deepseek",
    "fieldName": "key", "environmentNames": ["DEEPSEEK_API_KEY"], "revision": 1,
}
WAIT = 0.5


def _run(*targets: Any) -> list[BaseException | Any]:
    outcomes: list[BaseException | Any] = [None] * len(targets)

    def call(index: int, target: Any) -> None:
        try:
            outcomes[index] = target()
        except BaseException as error:
            outcomes[index] = error

    threads = [threading.Thread(target=call, args=(index, target)) for index, target in enumerate(targets)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    return outcomes


def _met(barrier: threading.Barrier) -> None:
    try:
        barrier.wait(timeout=WAIT)
    except threading.BrokenBarrierError:
        pass


def test_concurrent_first_writes_of_one_entry_log_in_exactly_one_key(app, service) -> None:
    barrier = threading.Barrier(2)
    find = service.find_key_by_legacy_ref

    def racing_find(**kwargs: Any) -> Any:
        found = find(**kwargs)
        _met(barrier)
        return found

    service.find_key_by_legacy_ref = racing_find

    outcomes = _run(*(lambda: legacy.dispatch(app, "agent.secret.provider-write", dict(WRITE)),) * 2)

    assert outcomes == [{"ok": True, "entryId": "deepseek", "fieldNames": ["key"]}] * 2
    assert service.names().count("login") == 1
    assert service.names().count("rotate") == 1
    assert len(service.keys) == 1


def test_a_lower_revision_racing_a_higher_one_is_rejected(app, service, registry) -> None:
    ref = registry.add("alpha")
    legacy.dispatch(app, "agent.secret.grant", dict(GRANT))
    barrier = threading.Barrier(2)
    higher_entered, higher_written = threading.Event(), threading.Event()
    grants_for, put_awaiting = service.grants_for, service.put_awaiting

    def racing_grants_for(agent: Any) -> Any:
        records = grants_for(agent)
        higher_entered.set()
        _met(barrier)
        return records

    def ordered_put_awaiting(**kwargs: Any) -> Any:
        if kwargs["legacy_ref"]["revision"] == 2:
            higher_written.wait(timeout=WAIT)
        result = put_awaiting(**kwargs)
        if kwargs["legacy_ref"]["revision"] == 3:
            higher_written.set()
        return result

    service.grants_for, service.put_awaiting = racing_grants_for, ordered_put_awaiting
    outcomes: list[Any] = []
    higher = threading.Thread(target=lambda: outcomes.append(
        legacy.dispatch(app, "agent.secret.grant", {**GRANT, "revision": 3, "environmentNames": ["A"]})))
    higher.start()
    assert higher_entered.wait(timeout=5)
    lower = _run(lambda: legacy.dispatch(app, "agent.secret.grant",
                                         {**GRANT, "revision": 2, "environmentNames": ["B"]}))
    higher.join(timeout=10)

    assert service.records[(ref.agent, "slot")]["legacyRef"]["revision"] == 3
    assert isinstance(lower[0], DaemonRequestError)
    assert lower[0].code == legacy.AGENT_ERROR
    assert outcomes[0]["grant"]["revision"] == 3


class _StoreError(Exception):
    def __init__(self) -> None:
        super().__init__("/var/hyprial/orgfs/space/directory/provider-keys/meta.json stale")
        self.code = "stale-write"


@pytest.mark.parametrize("error", [OSError("/home/owner/.hyprial/secret path"), ValueError("bad record"),
                                   _StoreError()])
@pytest.mark.parametrize("step", ["find_key_by_legacy_ref", "login"])
def test_raw_write_failures_map_to_the_fixed_refusal(app, service, error, step) -> None:
    service.fail[step] = error

    with pytest.raises(DaemonRequestError) as refused:
        legacy.dispatch(app, "agent.secret.provider-write", dict(WRITE))

    assert refused.value.code == ipc_errors.AGENT_SECRET_RESOLUTION_FAILED
    assert str(refused.value) == legacy.REFUSED


@pytest.mark.parametrize("error", [OSError("/home/owner/.hyprial/x"), _StoreError()])
def test_raw_grant_and_list_failures_map_to_the_fixed_codes(app, service, registry, error) -> None:
    registry.add("alpha")
    service.fail["grants_for"] = error

    with pytest.raises(DaemonRequestError) as granted:
        legacy.dispatch(app, "agent.secret.grant", dict(GRANT))
    with pytest.raises(DaemonRequestError) as listed:
        legacy.dispatch(app, "agent.secret.list", {"actor": "alpha"})

    assert (granted.value.code, str(granted.value)) == (ipc_errors.AGENT_SECRET_RESOLUTION_FAILED, legacy.REFUSED)
    assert (listed.value.code, str(listed.value)) == (legacy.AGENT_ERROR, legacy.LIST_UNAVAILABLE)


def test_a_raw_owner_context_failure_is_a_fixed_refusal(app, monkeypatch: pytest.MonkeyPatch) -> None:
    from hyprial.daemon.impl.provider_keys import context

    def broken(app: Any) -> Any:
        raise RuntimeError("/home/owner/.hyprial/settings.json")

    monkeypatch.setattr(context, "service_for", broken)

    with pytest.raises(DaemonRequestError) as refused:
        legacy.dispatch(app, "agent.secret.provider-write", dict(WRITE))

    assert (refused.value.code, str(refused.value)) == (ipc_errors.AGENT_SECRET_RESOLUTION_FAILED, legacy.REFUSED)


def _interactive(app: Any, matched: bool) -> list[Any]:
    seen: list[Any] = []
    app._ipc_peer = SimpleNamespace(pid=4242)
    app._interactive_identity_binding_peer = lambda pid: seen.append(pid) or matched
    return seen


@pytest.mark.parametrize(("method", "params"), [
    ("agent.secret.provider-write", dict(WRITE)),
    ("agent.secret.grant", dict(GRANT)),
    ("agent.secret.revoke", {"actor": "alpha", "grantId": "slot"}),
])
def test_an_interactive_session_peer_cannot_sign_through_legacy_names(
    app, service, registry, method: str, params: dict,
) -> None:
    registry.add("alpha")
    legacy.dispatch(app, "agent.secret.grant", dict(GRANT))
    before = list(service.names())
    seen = _interactive(app, True)

    with pytest.raises(DaemonRequestError) as refused:
        legacy.dispatch(app, method, params)

    assert (refused.value.code, str(refused.value)) == (ipc_errors.AGENT_SECRET_RESOLUTION_FAILED, legacy.REFUSED)
    assert seen == [4242]
    assert not {"login", "rotate", "allow", "put_awaiting", "revoke"} & set(service.names()[len(before):])


def test_an_interactive_session_peer_keeps_reads_and_legacy_sources(app, service, registry) -> None:
    registry.add("alpha")
    legacy.dispatch(app, "agent.secret.grant", dict(GRANT))
    _interactive(app, True)

    listed = legacy.dispatch(app, "agent.secret.list", {"actor": "alpha"})

    assert [row["grantId"] for row in listed["grants"]] == ["slot"]
    assert legacy.dispatch(app, "agent.secret.grant", {**GRANT, "source": "agent-private"}) is legacy.NOT_HANDLED
    assert legacy.dispatch(app, "agent.secret.revoke", {"actor": "alpha", "grantId": "other"}) \
        is legacy.NOT_HANDLED


def test_a_non_interactive_peer_reaches_the_authority(app, service) -> None:
    seen = _interactive(app, False)

    assert legacy.dispatch(app, "agent.secret.provider-write", dict(WRITE))["ok"] is True
    assert seen == [4242]
    assert "login" in service.names()


def test_a_peer_without_a_session_matcher_is_refused(app, service) -> None:
    app._ipc_peer = SimpleNamespace(pid=4242)

    with pytest.raises(DaemonRequestError) as refused:
        legacy.dispatch(app, "agent.secret.provider-write", dict(WRITE))

    assert refused.value.code == ipc_errors.AGENT_SECRET_RESOLUTION_FAILED
    assert "login" not in service.names()
