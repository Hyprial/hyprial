# Decision record (S3a red tests R1; no inline comments by repository rule):
# - org.receivers.get and org.receivers.set leave every table that names a
#   method: the registry, the migrated record, the live catalog, the live and
#   committed OpenAPI documents and the router, which answers METHOD_NOT_FOUND.
# - The router case goes through DaemonApplication.handle with a real
#   application, so a surviving binding or a surviving legacy branch both fail
#   on the returned code and not on a missing helper.
"""org.receivers.* is not a daemon method any more."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.daemon.impl.request_catalog import daemon_catalog, daemon_openapi
from hyprial.daemon.impl.request_catalog.migration import MIGRATED_METHODS
from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY
from tests.identity.agents.registry.entity.support import (
    _close_test_applications as _close_test_applications,
    application,
)

METHODS = ("org.receivers.get", "org.receivers.set")
SNAPSHOT = Path(__file__).resolve().parents[5] / "contract" / "daemon-request" / "openapi.json"


def test_the_registry_and_the_migrated_record_do_not_name_them() -> None:
    assert [name for name in METHODS if name in METHOD_REGISTRY.names] == []
    assert [name for name in METHODS if name in MIGRATED_METHODS] == []


def test_the_live_catalog_does_not_list_them() -> None:
    names = [entry["name"] for entry in daemon_catalog()["methods"]]
    assert [name for name in METHODS if name in names] == []


def test_the_live_and_committed_openapi_do_not_list_them() -> None:
    for document in (daemon_openapi(), json.loads(SNAPSHOT.read_text(encoding="utf-8"))):
        names = [entry["name"] for entry in document["x-hyprial-methods"]]
        paths = [path.rsplit("/", 1)[-1] for path in document["paths"]]
        assert [name for name in METHODS if name in names] == []
        assert [name for name in METHODS if name in paths] == []


@pytest.mark.parametrize("method", METHODS)
def test_the_router_answers_method_not_found(tmp_path: Path, method: str) -> None:
    app = application(tmp_path)

    with pytest.raises(DaemonRequestError) as raised:
        app.handle(method, {}, _trusted_message_origin="ipc")

    assert raised.value.code == "METHOD_NOT_FOUND"
