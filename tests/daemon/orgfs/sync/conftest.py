"""Close every sync node and blob store a test opened, even when it fails."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.daemon.orgfs.sync.support.nodes import OPEN_NODES


@pytest.fixture(autouse=True)
def close_open_nodes() -> Iterator[None]:
    yield
    nodes = list(OPEN_NODES)
    OPEN_NODES.clear()
    for node in nodes:
        try:
            node.close()
        except Exception:  # noqa: BLE001 - teardown must close the rest
            pass
