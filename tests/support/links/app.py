"""Composition test helpers: an orgfs with metadata and a stub app (WP2-B3).

``OrgFsWithMeta`` adds the ``read_text`` the directory store's ``org_state``
needs.  ``stub_app`` is a ``SimpleNamespace`` carrying exactly the attributes
``compose_link_service`` reads, so a composition test never boots a daemon.
``join_test_org`` gives a real app its writable org directory space.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from hyprial.daemon.impl.dispatch.admission import LinkAdmission
from hyprial.daemon.impl.org.network.directory import (
    InvalidDeviceQuarantine,
    OrgFsDirectoryStore,
)
from hyprial.identity import org_space_name
from tests.support.links.org import FakeOrgFs, LogRecorder

__all__ = ["OrgFsWithMeta", "join_test_org", "stub_app"]


class OrgFsWithMeta(FakeOrgFs):
    """``FakeOrgFs`` plus ``read_text`` for org lifecycle metadata."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.spaces_calls = 0

    def spaces(self):  # type: ignore[no-untyped-def]
        self.spaces_calls += 1
        return super().spaces()

    def read_text(self, space_id: str, node: str) -> tuple[str, str]:
        item = self._node(space_id, node)
        return item.content, str(item.version)

    def space_meta(self, space_id: str) -> dict[str, object]:
        self._node(space_id, "link")
        return {"aclSpaceId": f"{space_id}-acl"}


def join_test_org(app: Any, org: str) -> Any:
    """Create ``org``'s directory space on a real app's orgfs facade."""

    fs = app._require_orgfs_runtime().facade
    name = org_space_name(org)
    if hasattr(fs, "create_space"):
        fs.create_space(name)
    else:  # FakeOrgFs
        fs.add_space(name, name=name)
    OrgFsDirectoryStore(fs).put_org_meta(org, {"state": "active"})
    return fs


def stub_app(
    tmp_path: Path,
    *,
    fs: FakeOrgFs | None = None,
    owner: str = "o",
    node_id: str = "n1",
    bound_gate: Any | None = None,
    **overrides: Any,
) -> SimpleNamespace:
    """A minimal ``DaemonApplication`` stand-in for composition tests."""

    orgfs = fs if fs is not None else FakeOrgFs()
    logs = LogRecorder()
    admission = LinkAdmission()
    if bound_gate is not None:
        admission.bind(bound_gate)
    app = SimpleNamespace(
        owner=owner,
        node_id=node_id,
        hyprial_home=Path(tmp_path) / "home",
        state_dir=Path(tmp_path) / "state",
        link_admission=admission,
        agents=SimpleNamespace(
            local_actors=lambda: (
                f"agent:{owner}:{node_id}:a",
                f"agent:{owner}:{node_id}:b",
                f"agent:{owner}:{node_id}:dispatch-coordinator",
            )
        ),
        _registered_interactive_actors=lambda: frozenset(),
        _presence=None,
        _hook_bus=None,
        _daemon_hooks=None,
        _inbox=None,
        _log=logs,
        _org_directory_quarantine=InvalidDeviceQuarantine(),
        _resolve_agent_alias=lambda value: value,
        _require_orgfs_runtime=lambda: SimpleNamespace(facade=orgfs),
    )
    for key, value in overrides.items():
        setattr(app, key, value)
    return app
