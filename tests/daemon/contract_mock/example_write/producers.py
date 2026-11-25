"""Replay actual write handlers with isolated durable OrgFS and local files.

The bridge receives an already admitted operator with a real operator path
policy. Daemon authentication is not simulated; HTTP projection/auth boundaries
are exercised separately. Only opaque IDs, clock/frontier values and the
isolated filesystem root are normalized, never content, versions or receipts.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from contextlib import contextmanager
import hashlib
from pathlib import Path
import re
from uuid import UUID

from hyprial.daemon.impl.application.messaging.orgfs_bridge import _OrgFsBridgeMixin
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.orgfs.projection.purge import canonical_bytes
from hyprial.daemon.impl.path_authz.policy import operator_policy
from hyprial.daemon.impl.request_catalog.org.orgfs.write_specs import BATCH4_ORGFS_BINDINGS

SPACE = "b363d762-a06e-496d-94d0-e4695b66e206"
NODE = "89fd8adf4f8f43f48f475bef6914c61f"
DOC = "doc-b6fa1d65-6c38-4232-a3ab-15cdc45f8949"
AT = "2026-11-03T09:00:00Z"
EXPIRY = "2026-11-04T09:00:00Z"
FRONTIER = "AvqhrMmIj78FGOCGnpfjt5UJDQ=="
ROOT = "/tmp/hyprial-write-example"
CONTENT = b"example-not-a-credential"
SHA = hashlib.sha256(CONTENT).hexdigest()
VERSION = "eyJjb250ZW50RnJvbnRpZXIiOjEsInRyZWVGcm9udGllciI6MX0"
URI = f"orgfs:example:{SPACE}:{NODE}"
PLAN_UNSIGNED = {
    "spaceId": SPACE, "metaFrontier": FRONTIER, "docs": [],
    "blobs": [{"sha": SHA, "referencedElsewhereLocally": []}],
    "snapshots": [], "writersAffected": [], "authorsBefore": {}, "expiresAt": EXPIRY,
}
PLAN = hashlib.sha256(canonical_bytes(PLAN_UNSIGNED)).hexdigest()
HANDLERS = {spec.name: handler for spec, handler in BATCH4_ORGFS_BINDINGS}


def plain(value):
    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(item) for item in value]
    return value


class WriteApp(_OrgFsBridgeMixin):
    def __init__(self, runtime, root):
        self.runtime = runtime
        self.root = root

    def _require_orgfs_runtime(self):
        return self.runtime

    def _capability_caller(self, params):
        return "user:example"

    def _enforce_org_context(self, caller):
        assert caller == "user:example"

    def _orgfs_path_policy(self, params, method, trusted_origin):
        return operator_policy(self.root / "home", state_dir=self.runtime.state_dir)


class Observation:
    def __init__(self, runtime, root):
        self.runtime = runtime
        self.app = WriteApp(runtime, root)
        self.replacements = {str(root): ROOT}

    def bind(self, actual, fixed):
        if fixed == SPACE:
            assert str(UUID(actual)) == actual
        elif fixed == NODE:
            assert re.fullmatch(r"[0-9a-f]{32}", actual)
        elif fixed == DOC:
            assert actual.startswith("doc-") and str(UUID(actual[4:])) == actual[4:]
        elif fixed == PLAN:
            assert re.fullmatch(r"[0-9a-f]{64}", actual)
        elif fixed in (AT, EXPIRY):
            assert actual.endswith("Z")
        elif fixed == FRONTIER:
            assert base64.b64decode(actual, validate=True)
        else:
            raise AssertionError("only opaque values may be normalized")
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

    def bind_node(self, node):
        self.bind(node.node_id, NODE)
        if node.doc_id:
            self.bind(node.doc_id, DOC)

    def bind_plan(self, plan):
        assert hashlib.sha256(canonical_bytes(plan.unsigned_dict())).hexdigest() == plan.plan_id
        self.bind(plan.meta_frontier, FRONTIER)
        self.bind(plan.expires_at, EXPIRY)
        assert self.transform(plan.unsigned_dict()) == PLAN_UNSIGNED
        self.bind(plan.plan_id, PLAN)

    def call(self, method, params):
        result = HANDLERS[method](self.app, self.transform(params, inverse=True))
        if isinstance(result, dict):
            if method == "orgfs.create":
                self.bind(result["space_id"], SPACE)
            for key in ("created_at", "added_at"):
                if key in result:
                    self.bind(result[key], AT)
            if "node_id" in result:
                self.bind(result["node_id"], NODE)
                if result.get("doc_id"):
                    self.bind(result["doc_id"], DOC)
            if method == "orgfs.purge_plan":
                unsigned = {
                    "spaceId": result["space_id"], "metaFrontier": result["meta_frontier"],
                    "docs": result["docs"], "snapshots": result["snapshots"],
                    "blobs": [{"sha": row["sha"], "referencedElsewhereLocally": row["referenced_elsewhere_locally"]} for row in result["blobs"]],
                    "writersAffected": result["writers_affected"],
                    "authorsBefore": result["authors_before"], "expiresAt": result["expires_at"],
                }
                assert unsigned["docs"] == [] and unsigned["snapshots"] == []
                assert hashlib.sha256(canonical_bytes(unsigned)).hexdigest() == result["plan_id"]
                self.bind(result["meta_frontier"], FRONTIER)
                self.bind(result["expires_at"], EXPIRY)
                assert self.transform(unsigned) == PLAN_UNSIGNED
                self.bind(result["plan_id"], PLAN)
        return self.transform(result)


@contextmanager
def observation(root: Path, method: str, case_name: str):
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    (root / "home").mkdir()
    (root / "input.bin").write_bytes(b"" if case_name == "empty-file-refused" else CONTENT)
    runtime = OrgFsRuntime(root / "state", node_id="node-example", author="user:example")
    result = Observation(runtime, root)
    try:
        if method == "orgfs.create":
            yield result
            return
        space = runtime.facade.create_space("example-space")
        result.bind(space.space_id, SPACE)
        result.bind(space.created_at, AT)
        if method in ("orgfs.export", "orgfs.move", "orgfs.remove", "orgfs.restore"):
            node = runtime.facade.write_text(space.space_id, "readme.txt", "" if case_name in ("empty-export", "empty-document") else "hello world!")
            result.bind_node(node)
            if method == "orgfs.restore":
                runtime.facade.remove(space.space_id, "readme.txt")
        if method in ("orgfs.purge_plan", "orgfs.purge", "orgfs.unban"):
            node = runtime.facade.write_bytes(space.space_id, "payload.bin", CONTENT)
            result.bind_node(node)
            if method in ("orgfs.purge", "orgfs.unban") and case_name == "producer-success":
                plan = runtime.purge_plan(space.space_id, [{"kind": "blob", "sha": SHA}])
                result.bind_plan(plan)
                if method == "orgfs.unban":
                    runtime.purge(space.space_id, plan.plan_id)
        if method == "orgfs.remove_member" or case_name == "other-members":
            runtime.facade.invite(space.space_id, "user:reader", "ro")
        if method == "orgfs.serve" and case_name == "backend-conflict":
            runtime.serve(space.space_id, "memory")
        yield result
    finally:
        runtime.close()
