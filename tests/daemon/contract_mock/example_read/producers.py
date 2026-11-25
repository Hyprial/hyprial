"""Replay real OrgFS read producers in a fresh, isolated local runtime.

Only nondeterministic identifiers and clock values are normalized for comparison.
Org-context admission is outside this read-example test; the bridge is given an
already admitted local caller. The facade, runtime reads and wire serializer run.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
import re
from uuid import UUID

from hyprial.daemon.impl.application.messaging.orgfs_bridge import _OrgFsBridgeMixin
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.request_catalog.org.orgfs.read_specs import BATCH3_ORGFS_BINDINGS

SPACE = "b363d762-a06e-496d-94d0-e4695b66e206"
NODE = "89fd8adf4f8f43f48f475bef6914c61f"
DOC = "doc-b6fa1d65-6c38-4232-a3ab-15cdc45f8949"
PLAN = 'b9d31a5e951e7342d974a2491e41fe4e04624c0a685364d94412e43c4b31f878'
AT = "2026-11-03T09:00:00Z"
PLAN_UNSIGNED = {'spaceId': 'b363d762-a06e-496d-94d0-e4695b66e206',
 'metaFrontier': 'AvqhrMmIj78FGOCGnpfjt5UJDQ==',
 'docs': [{'docId': 'doc-b6fa1d65-6c38-4232-a3ab-15cdc45f8949',
           'from': 'eyJjb250ZW50RnJvbnRpZXIiOjEsInRyZWVGcm9udGllciI6MX0',
           'to': 'eyJjb250ZW50RnJvbnRpZXIiOjEsInRyZWVGcm9udGllciI6MX0',
           'updateBlobs': []}],
 'blobs': [],
 'snapshots': [],
 'writersAffected': ['node-example.17e20830f284842537f9ad2dedee3ecc.lane-0'],
 'authorsBefore': {'user:example': 1},
 'expiresAt': '2026-11-04T00:38:41.608906Z'}
HANDLERS = {spec.name: handler for spec, handler in BATCH3_ORGFS_BINDINGS}


def plain(value):
    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(item) for item in value]
    return value


class ReadApp(_OrgFsBridgeMixin):
    def __init__(self, runtime):
        self.runtime = runtime

    def _require_orgfs_runtime(self):
        return self.runtime

    def _capability_caller(self, params):
        return "user:example"

    def _enforce_org_context(self, caller):
        assert caller == "user:example"


class Observation:
    def __init__(self, runtime):
        self.runtime = runtime
        self.app = ReadApp(runtime)
        self.replacements = {}

    def bind(self, actual, fixed):
        if fixed == SPACE:
            assert str(UUID(actual)) == actual
        elif fixed == NODE:
            assert re.fullmatch(r"[0-9a-f]{32}", actual)
        elif fixed == DOC:
            assert actual.startswith("doc-")
            assert str(UUID(actual[4:])) == actual[4:]
        elif fixed == PLAN:
            assert re.fullmatch(r"[0-9a-f]{64}", actual)
        elif fixed == AT:
            assert actual.endswith("Z")
        else:
            raise AssertionError("normalization is limited to opaque IDs and timestamps")
        self.replacements[actual] = fixed

    def transform(self, value, *, inverse=False):
        mapping = self.replacements
        if inverse:
            mapping = {fixed: actual for actual, fixed in mapping.items()}
        if isinstance(value, str):
            for old, new in sorted(mapping.items(), key=lambda item: -len(item[0])):
                value = value.replace(old, new)
            return value
        if isinstance(value, Mapping):
            return {key: self.transform(item, inverse=inverse) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [self.transform(item, inverse=inverse) for item in value]
        return value

    def call(self, method, params):
        return self.transform(HANDLERS[method](self.app, self.transform(params, inverse=True)))


@contextmanager
def observation(path: Path, method: str, case_name: str):
    runtime = OrgFsRuntime(path, node_id="node-example", author="user:example")
    result = Observation(runtime)
    try:
        if method == "orgfs.spaces" and case_name == "producer-empty":
            yield result
            return
        space = runtime.facade.create_space("example-space")
        result.bind(space.space_id, SPACE)
        result.bind(space.created_at, AT)
        if method == "orgfs.ls" and case_name == "producer-empty":
            yield result
            return
        text = "" if case_name == "producer-empty" and method in ("orgfs.read", "orgfs.read_at") else "hello world!"
        node = runtime.facade.write_text(space.space_id, "readme.txt", text)
        result.bind(node.node_id, NODE)
        result.bind(node.doc_id, DOC)
        for entry in runtime.facade.history(space.space_id, "readme.txt"):
            result.bind(entry.at, AT)
        if method == "orgfs.trash" and case_name == "producer-success":
            runtime.facade.remove(space.space_id, "readme.txt")
        if method == "orgfs.purge_status" and case_name == "producer-success":
            plan = runtime.purge_plan(space.space_id, [{
                "kind": "doc-range", "node": "readme.txt",
                "from": node.version, "to": node.version,
            }])
            result.bind(plan.plan_id, PLAN)
        yield result
    finally:
        runtime.close()
