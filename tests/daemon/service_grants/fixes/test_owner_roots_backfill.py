"""An existing org gains ``directory/service-grants`` once its owner's daemon runs.

Decision (service grant S3 fix, 2026-11-26, critic S5 and S7 item 13): the
periodic ``publish_self_for_app`` path creates missing owner directories for
every org whose space this daemon owns. An org is not checked again after one
success in the process; a failure is retried on the next tick and never stops
the device row from being published. Re-running ``org create`` remains an
alternative and is covered here too.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

import hyprial.daemon.impl.org.network.directory as directory_module
from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.org.network.ipc import ipc_org_create, publish_self_for_app
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.identity import ORG_META_DOC

from tests.daemon.org.network.test_org_network_ipc import OWNER, _FakeApp, app

_FIXTURES = (app,)
ORG = "group-acme"
OTHER = "group-other"
GRANTS_DIR = "directory/service-grants"


def _space_id(app: _FakeApp, org: str) -> str:
    return next(space.space_id for space in app._runtime.facade.spaces() if space.name == org)


def _has_grants_dir(app: _FakeApp, org: str = ORG) -> bool:
    try:
        return app._runtime.facade.stat(_space_id(app, org), GRANTS_DIR).kind == "dir"
    except OrgFsError:
        return False


def _create_pre_s3_org(app: _FakeApp, monkeypatch: pytest.MonkeyPatch) -> None:
    old_dirs = tuple(path for path in directory_module._OWNER_CREATED_DIRS if path != GRANTS_DIR)
    with monkeypatch.context() as patched:
        patched.setattr(directory_module, "_OWNER_CREATED_DIRS", old_dirs)
        ipc_org_create(app, {"org": ORG})
    assert not _has_grants_dir(app)


def _spy_roots(monkeypatch: pytest.MonkeyPatch, *, fail_first: int = 0) -> list[str]:
    calls: list[str] = []
    original = OrgFsDirectoryStore.ensure_org_directory_roots

    def spy(self: OrgFsDirectoryStore, org: str) -> None:
        calls.append(org)
        if len(calls) <= fail_first:
            raise OrgFsError("io", {"reason": "synthetic"})
        original(self, org)

    monkeypatch.setattr(OrgFsDirectoryStore, "ensure_org_directory_roots", spy)
    return calls


def test_publish_tick_backfills_the_owner_directory_once(
    app: _FakeApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_pre_s3_org(app, monkeypatch)
    calls = _spy_roots(monkeypatch)

    publish_self_for_app(app)

    assert _has_grants_dir(app)
    assert calls == [ORG]
    publish_self_for_app(app)
    assert calls == [ORG]


def test_backfill_failure_does_not_stop_the_row_and_is_retried(
    app: _FakeApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_pre_s3_org(app, monkeypatch)
    store = OrgFsDirectoryStore(app._runtime.facade)
    [row] = store.list_devices(ORG)
    store.put_device(ORG, dataclasses.replace(row, address="tcp://10.0.0.99:7447#stale"))
    calls = _spy_roots(monkeypatch, fail_first=1)

    publish_self_for_app(app)

    published = [r["fields"]["published"] for r in app.records if r["event"] == "org.publish_self"]
    assert published[-1] == [ORG]
    [republished] = store.list_devices(ORG)
    assert republished.address == row.address
    assert not _has_grants_dir(app)
    publish_self_for_app(app)
    assert calls == [ORG, ORG]
    assert _has_grants_dir(app)


def test_orgs_owned_by_someone_else_are_not_backfilled(
    app: _FakeApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_pre_s3_org(app, monkeypatch)
    facade = app._runtime.facade
    facade.author = "user:carol@example"
    try:
        other = facade.create_space(OTHER)
        facade.invite(other.space_id, f"user:{OWNER}", "rw")
    finally:
        facade.author = f"user:{OWNER}"
    calls = _spy_roots(monkeypatch)

    publish_self_for_app(app)

    assert calls == [ORG]
    assert not _has_grants_dir(app, OTHER)


def test_rerunning_org_create_on_an_existing_org_adds_the_directory(
    app: _FakeApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S7 item 13: the documented alternative, ``org create`` itself, not only the store call."""

    _create_pre_s3_org(app, monkeypatch)
    space_id = _space_id(app, ORG)
    meta_before = app._runtime.facade.read_text(space_id, ORG_META_DOC)[0]

    again: dict[str, Any] = ipc_org_create(app, {"org": ORG})

    assert again["existed"] is True
    assert _has_grants_dir(app)
    assert app._runtime.facade.read_text(space_id, ORG_META_DOC)[0] == meta_before
