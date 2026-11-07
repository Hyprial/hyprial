from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError
from hyprial.daemon.impl.org.mesh import OrgFetchResult


from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _app_injections


class _Presence:
    def online_actors(self) -> tuple[str, ...]:
        return ("local-node", "node-a", "agent:owner:host:worker")

    def raw_online_actors(self) -> tuple[str, ...]:
        # 桩无 interactive 层:raw 集合与过滤后集合同义。
        return ("local-node", "node-a", "agent:owner:host:worker")

    def liveness_keeps(self, actor: str) -> bool:
        # 桩无 interactive 层:复核一律保留(与 raw 同义,不改变测试语义)。
        return True


class _Endpoint:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def fetch(self, **kwargs: Any) -> OrgFetchResult:
        self.calls.append(kwargs)
        return OrgFetchResult(tuple(sorted(kwargs.get("allowed_sources") or ())), 0, ())

    def publish_accepted(self) -> bool:
        return True


def _application(
    tmp_path: Path, request: pytest.FixtureRequest
) -> tuple[DaemonApplication, _Endpoint]:
    application = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "state" / "daemon.sock",
        hyprial_home=tmp_path / "home",
        node_id="local-node",
        owner="owner",
        **_app_injections(),
    )
    application.network_isolated = True
    request.addfinalizer(application._close)
    endpoint = _Endpoint()
    application._inbox = object()  # type: ignore[assignment]
    application._harnesses = object()  # type: ignore[assignment]
    application._presence = _Presence()  # type: ignore[assignment]
    application._org_endpoint = endpoint  # type: ignore[assignment]
    return application, endpoint


def test_daemon_org_fetch_enumerates_online_targets_and_normalizes_selected_source(
    tmp_path: Path, request: pytest.FixtureRequest,
) -> None:
    application, endpoint = _application(tmp_path, request)

    result = application.handle(
        "org.fetch", {"from": "agent:node-a", "timeoutSeconds": 0.5}
    )

    assert result["requestedSources"] == ["node-a"]
    assert endpoint.calls == [
        {
            "source": "node-a",
            "allowed_sources": frozenset({"node-a"}),
            "timeout": 0.5,
        }
    ]


def test_daemon_org_fetch_without_source_queries_all_online_targets(
    tmp_path: Path, request: pytest.FixtureRequest,
) -> None:
    application, endpoint = _application(tmp_path, request)

    result = application.handle("org.fetch", {"timeoutSeconds": 0.75})

    assert result["requestedSources"] == ["node-a"]
    assert endpoint.calls == [
        {
            "source": None,
            "allowed_sources": frozenset({"node-a"}),
            "timeout": 0.75,
        }
    ]


def test_daemon_org_fetch_rejects_source_absent_from_targets(
    tmp_path: Path, request: pytest.FixtureRequest,
) -> None:
    application, endpoint = _application(tmp_path, request)

    with pytest.raises(DaemonRequestError) as failure:
        application.handle("org.fetch", {"from": "agent:offline"})

    assert failure.value.code == "ORG_SOURCE_UNREACHABLE"
    # Verbatim, never re-wrapped as agent:<node> — node-a is a node.
    assert failure.value.data == {"availableSources": ["node-a"]}
    assert endpoint.calls == []


def test_daemon_org_fetch_rejects_resident_actor_without_node_queryable(
    tmp_path: Path, request: pytest.FixtureRequest,
) -> None:
    application, endpoint = _application(tmp_path, request)

    with pytest.raises(DaemonRequestError) as failure:
        application.handle("org.fetch", {"from": "agent:owner:host:worker"})

    assert failure.value.code == "ORG_SOURCE_UNREACHABLE"
    # Verbatim, never re-wrapped as agent:<node> — node-a is a node.
    assert failure.value.data == {"availableSources": ["node-a"]}
    assert endpoint.calls == []


def test_daemon_org_neighbor_request_bypasses_target_filter_once(
    tmp_path: Path, request: pytest.FixtureRequest,
) -> None:
    application, endpoint = _application(tmp_path, request)

    result = application.handle(
        "org.fetch", {"requestMode": "neighbors", "timeoutSeconds": 0.25}
    )

    assert result["responseCount"] == 0
    assert endpoint.calls == [{"timeout": 0.25}]


def test_daemon_org_publish_delegates_to_accepted_only_endpoint(
    tmp_path: Path, request: pytest.FixtureRequest,
) -> None:
    application, endpoint = _application(tmp_path, request)

    assert application.handle("org.publish", {}) == {
        "published": True,
        "orgfsPublished": False,
    }
    assert endpoint.calls == []
