from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.daemon.impl.adapters.lark.inbound.commands import _org_context_summary
from hyprial.kernel.impl.contracts.daemon import ipc_errors
from hyprial.kernel.impl.contracts.daemon.ipc_errors import DaemonRequestError
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.org.document import parse_document, serialize_document, validate_document
from hyprial.daemon.impl.org.mesh import OrgFetchResult
from hyprial.daemon.impl.org.orgfs_migration import ORG_CONTEXT_SPACE_NAME, write_org_fetch_source
from hyprial.daemon.impl.org.store import OrgContextStore
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.kernel.impl.primitives.uri import parse_orgfs_uri
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


CORRUPT_ESCAPED = (
    Path(__file__).resolve().parents[1]
    / "document"
    / "fixtures"
    / "org-context-corrupt-escaped-brace.md"
)


class _Endpoint:
    def __init__(self, result: OrgFetchResult | None = None) -> None:
        self.result = result or OrgFetchResult((), 0, ())
        self.calls: list[dict[str, Any]] = []

    def publish_accepted(self) -> bool:
        return True

    def fetch(self, **kwargs: Any) -> OrgFetchResult:
        self.calls.append(kwargs)
        return self.result

    def close(self) -> None:
        return None


class _FakeTransport:
    def close(self) -> None:
        return None


class _Presence:
    def __init__(self, *nodes: str) -> None:
        self.nodes = nodes

    def raw_online_actors(self) -> tuple[str, ...]:
        return self.nodes

    def liveness_keeps(self, _actor: str) -> bool:
        return True


def _document(version: int, publisher: str = "alice") -> str:
    return serialize_document(
        parse_document(
            f"""# Harness Bridge organization

Candidate version {version}.

```yaml
meta:
  version: {version}
  issued_at: 2026-09-27
  publisher: {publisher}
lines:
  - {{line: orgfs, owner: {publisher}}}
members: []
routing: []
norms:
  autonomy: [safe]
  escalation: ask
  etiquette: kind
residents: []
```
"""
        )
    )


def _application(tmp_path: Path) -> DaemonApplication:
    (tmp_path / "home").mkdir(mode=0o700)
    application = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "state" / "daemon.sock",
        hyprial_home=tmp_path / "home",
        node_id="node-a",
        owner="owner",
    **_deep_module_daemon_dependencies())
    application._inbox = object()  # type: ignore[assignment]
    application._harnesses = object()  # type: ignore[assignment]
    application._org_endpoint = _Endpoint()  # type: ignore[assignment]
    application._presence = _Presence("node-b")  # type: ignore[assignment]
    application._transport = _FakeTransport()  # type: ignore[assignment]
    return application


def _space(application: DaemonApplication) -> str:
    runtime = application._require_orgfs_runtime()
    space = runtime.facade.create_space(ORG_CONTEXT_SPACE_NAME)
    runtime.facade.mkdir(space.space_id, "candidates")
    return space.space_id


def _close(application: DaemonApplication) -> None:
    application._close()
    application._close_home_lease()


def _enable_m1(application: DaemonApplication) -> None:
    write_org_fetch_source("orgfs", application.hyprial_home)


def test_member_controlled_invalid_candidate_name_is_skipped_with_one_warning(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    try:
        space_id = _space(application)
        bridge = application._require_org_context_bridge()
        events: list[tuple[str, str, dict[str, object]]] = []
        bridge.logger = lambda level, event, **fields: events.append(
            (level, event, fields)
        )
        fs = application._require_orgfs_runtime().facade
        fs.write_text(space_id, "candidates/a:b.md", _document(2, "invalid"))
        fs.write_text(space_id, "candidates/bob.md", _document(3, "bob"))

        assert bridge._candidate_users(space_id) == ("user:bob",)
        assert events == [
            (
                "warn",
                "org.context.candidate_name_invalid",
                {
                    "candidate": "a:b.md",
                    "detail": "user identity owner must not contain ':'",
                },
            )
        ]
    finally:
        _close(application)


def test_morg_1_bad_candidates_never_change_accepted_slot(tmp_path: Path) -> None:
    application = _application(tmp_path)
    try:
        space_id = _space(application)
        store = OrgContextStore(application.hyprial_home)
        store.adopt(parse_document(_document(1)))
        accepted_before = store.accepted_path.read_bytes()
        fs = application._require_orgfs_runtime().facade
        fs.write_text(space_id, "candidates/schema.md", "not an org document")
        fs.write_text(
            space_id,
            "candidates/merged.md",
            _document(2, "merged-a") + _document(3, "merged-b"),
        )
        fs.write_text(space_id, "candidates/old.md", _document(1, "old"))
        fs.write_text(space_id, "candidates/good.md", _document(2, "good"))
        _enable_m1(application)

        result = application.handle("org.fetch", {"timeoutSeconds": 0.2})

        assert result["responseCount"] == 4
        assert result["receivedCount"] == 1
        assert result["rejectedCount"] == 3
        assert store.accepted_path.read_bytes() == accepted_before
        pending = store.list_pending()
        assert len(pending) == 1
        assert pending[0].publisher == "good"
        # Provenance is the canonical node URI plus @<version> (Q4=A).
        expected_uri = fs.stat(space_id, "candidates/good.md").uri
        assert pending[0].source.startswith(f"{expected_uri}@")
        assert parse_orgfs_uri(pending[0].source.rsplit("@", 1)[0]) is not None
    finally:
        _close(application)


def test_candidate_provenance_uses_same_node_snapshot_as_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    application = _application(tmp_path)
    try:
        space_id = _space(application)
        runtime = application._require_orgfs_runtime()
        fs = runtime.facade
        path = "candidates/alice.md"
        original = fs.write_text(space_id, path, _document(2))
        original_read_text = fs.read_text
        replaced = False

        def replace_candidate() -> None:
            nonlocal replaced
            if replaced:
                return
            replaced = True
            fs.move(space_id, f"id:{original.node_id}", "candidates/alice-old.md")
            fs.write_text(space_id, path, _document(3, "replacement"))

        def read_then_replace(space: str, node: str):
            result = original_read_text(space, node)
            replace_candidate()
            return result

        monkeypatch.setattr(fs, "read_text", read_then_replace)
        original_snapshot = getattr(fs, "read_text_snapshot", None)
        if original_snapshot is not None:

            def snapshot_then_replace(space: str, node: str):
                result = original_snapshot(space, node)
                replace_candidate()
                return result

            monkeypatch.setattr(fs, "read_text_snapshot", snapshot_then_replace)
        pending = application._require_org_context_bridge()._stage_candidate(
            space_id, "user:alice"
        )

        assert pending is not None
        assert replaced is True
        assert pending.source.startswith(f"{original.uri}@")
        assert not pending.source.startswith(f"{fs.stat(space_id, path).uri}@")
    finally:
        _close(application)


def test_morg_2_publish_failure_keeps_adoption_and_restart_retry_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    application = _application(tmp_path)
    try:
        space_id = _space(application)
        runtime = application._require_orgfs_runtime()
        incoming = tmp_path / "incoming.md"
        incoming.write_text(_document(2), encoding="utf-8")
        monkeypatch.setenv("HYPRIAL_HOME", str(application.hyprial_home))
        monkeypatch.setenv("HARNESS_STATE_DIR", str(application.state_dir))
        original_write = runtime.facade.write_text

        def fail_candidate(space: str, node: str, content: str, **kwargs: Any):
            if node == "candidates/owner.md":
                raise OrgFsError(
                    "invalid-argument", {"message": "injected orgfs write failure"}
                )
            return original_write(space, node, content, **kwargs)

        monkeypatch.setattr(runtime.facade, "write_text", fail_candidate)
        monkeypatch.setattr(
            _cli_support,
            "_daemon_request",
            lambda method, params=None, **_kwargs: application.handle(
                method, params or {}
            ),
        )

        imported = CliRunner().invoke(
            _cli_root_app,
            ["org", "context", "import", str(incoming), "--force", "--json"],
        )

        assert imported.exit_code == 0, imported.output
        payload = json.loads(imported.stdout)
        assert payload["adopted"] is True
        assert payload["published"] is True
        assert payload["orgfsPublished"] is False
        store = OrgContextStore(application.hyprial_home)
        assert store.load_accepted().meta.version == 2
        assert store.publish_pending_path.is_file()
        assert "version=2" in _org_context_summary(store.accepted_path)

        monkeypatch.setattr(runtime.facade, "write_text", original_write)
        runtime.close()
        application._orgfs_runtime = None
        application._org_context_bridge = None
        first_retry = application.handle("org.publish", {})
        assert first_retry["orgfsPublished"] is True
        assert not store.publish_pending_path.exists()
        restarted = application._require_orgfs_runtime()
        first_history = restarted.facade.history(space_id, "candidates/owner.md")
        assert application.handle("org.publish", {})["orgfsPublished"] is True
        assert (
            restarted.facade.history(space_id, "candidates/owner.md") == first_history
        )

        class _FailedMesh(_Endpoint):
            def publish_accepted(self) -> bool:
                raise RuntimeError("mesh unavailable")

        application._org_endpoint = _FailedMesh()  # type: ignore[assignment]
        independent = application.handle("org.publish", {})
        assert independent == {"published": False, "orgfsPublished": True}
    finally:
        _close(application)


def test_morg_3_interrupted_fetch_restart_has_no_duplicate_pending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    application = _application(tmp_path)
    try:
        space_id = _space(application)
        runtime = application._require_orgfs_runtime()
        runtime.facade.write_text(space_id, "candidates/alice.md", _document(2))
        runtime.facade.write_text(space_id, "candidates/bob.md", _document(3, "bob"))
        _enable_m1(application)
        bridge = application._require_org_context_bridge()
        original_stage = bridge._stage_candidate
        calls = 0

        def interrupted(space: str, source: str):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("simulated daemon death")
            return original_stage(space, source)

        monkeypatch.setattr(bridge, "_stage_candidate", interrupted)
        with pytest.raises(RuntimeError, match="simulated daemon death"):
            application.handle("org.fetch", {"timeoutSeconds": 0.2})
        assert len(OrgContextStore(application.hyprial_home).list_pending()) == 1

        runtime.close()
        application._orgfs_runtime = None
        application._org_context_bridge = None
        result = application.handle("org.fetch", {"timeoutSeconds": 0.2})
        assert result["receivedCount"] == 2
        assert len(OrgContextStore(application.hyprial_home).list_pending()) == 2
    finally:
        _close(application)


def test_morg_4_lark_summary_bytes_are_phase_invariant(tmp_path: Path) -> None:
    application = _application(tmp_path)
    try:
        _space(application)
        store = OrgContextStore(application.hyprial_home)
        store.adopt(parse_document(_document(4)))
        accepted_bytes = store.accepted_path.read_bytes()
        before = _org_context_summary(store.accepted_path).encode()

        assert application.handle("org.publish", {})["orgfsPublished"] is True
        m0 = _org_context_summary(store.accepted_path).encode()
        _enable_m1(application)
        application.handle("org.fetch", {"timeoutSeconds": 0.2})
        m1 = _org_context_summary(store.accepted_path).encode()

        assert before == m0 == m1
        assert store.accepted_path.read_bytes() == accepted_bytes
        # M2 is deliberately not represented: it requires separate approval.
        assert b"M2" not in (before + m0 + m1)
    finally:
        _close(application)


def test_morg_5_m1_to_m0_switch_preserves_dual_path_and_no_duplicates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    application = _application(tmp_path)
    try:
        space_id = _space(application)
        runtime = application._require_orgfs_runtime()
        runtime.facade.write_text(space_id, "candidates/alice.md", _document(2))
        runtime.facade.write_text(space_id, "candidates/bob.md", _document(3, "bob"))
        _enable_m1(application)
        assert application.handle("org.fetch", {})["receivedCount"] == 2
        store = OrgContextStore(application.hyprial_home)

        class _MeshEndpoint(_Endpoint):
            def fetch(self, **kwargs: Any) -> OrgFetchResult:
                self.calls.append(kwargs)
                records = (
                    store.stage(parse_document(_document(2)), source="node-b"),
                    store.stage(parse_document(_document(3, "bob")), source="node-c"),
                )
                return OrgFetchResult(("node-b", "node-c"), 2, records)

        endpoint = _MeshEndpoint()
        application._org_endpoint = endpoint  # type: ignore[assignment]
        application._presence = _Presence("node-b", "node-c")  # type: ignore[assignment]
        write_org_fetch_source("mesh", application.hyprial_home)
        assert application.handle("org.fetch", {})["receivedCount"] == 2
        assert len(endpoint.calls) == 1
        write_org_fetch_source("orgfs", application.hyprial_home)
        assert application.handle("org.fetch", {})["receivedCount"] == 2
        assert len(store.list_pending()) == 2

        monkeypatch.setenv("HYPRIAL_HOME", str(application.hyprial_home))
        refused_m2 = CliRunner().invoke(
            _cli_root_app,
            ["config", "set", "org.fetchSource", "m2", "--json"],
        )
        assert refused_m2.exit_code == 1
    finally:
        _close(application)


def test_morg_6_node_and_user_sources_keep_result_keys_and_offline_resolution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    try:
        space_id = _space(application)
        runtime = application._require_orgfs_runtime()
        runtime.facade.write_text(space_id, "candidates/owner.md", _document(2))
        application._presence = _Presence("node-b")  # type: ignore[assignment]
        m0 = application.handle("org.fetch", {"from": "agent:node-b"})
        core = {
            "requestedSources",
            "responseCount",
            "receivedCount",
            "rejectedCount",
            "candidates",
        }
        assert core <= m0.keys()
        with pytest.raises(DaemonRequestError) as offline_m0:
            application.handle("org.fetch", {"from": "agent:offline"})
        assert offline_m0.value.code == ipc_errors.ORG_SOURCE_UNREACHABLE

        _enable_m1(application)
        application._presence = _Presence()  # type: ignore[assignment]
        monkeypatch.setattr(
            runtime, "writer_attributions", lambda _space: {"node-b": ("user:owner",)}
        )
        by_node = application.handle("org.fetch", {"from": "agent:node-b"})
        by_user = application.handle("org.fetch", {"from": "user:owner"})
        assert core <= by_node.keys()
        assert core <= by_user.keys()
        assert by_node["requestedSources"] == ["user:owner"]
        assert by_node["deprecatedSourceTarget"] is True
        assert by_user["requestedSources"] == ["user:owner"]
        assert by_user["source"] == "orgfs"
    finally:
        _close(application)


def test_morg_7_timeout_returns_local_candidates_with_refreshed_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    application = _application(tmp_path)
    try:
        space_id = _space(application)
        runtime = application._require_orgfs_runtime()
        runtime.facade.write_text(space_id, "candidates/alice.md", _document(2))
        _enable_m1(application)
        monkeypatch.setattr(runtime, "refresh_space", lambda *_args, **_kwargs: False)

        result = application.handle("org.fetch", {"timeoutSeconds": 0.01})

        assert result["refreshed"] is False
        assert result["receivedCount"] == 1
        assert result["responseCount"] == 1
    finally:
        _close(application)


def test_m0_publish_pending_digest_matches_accepted_bytes(tmp_path: Path) -> None:
    """The retry marker names accepted bytes and never rewrites the slot."""

    application = _application(tmp_path)
    try:
        store = OrgContextStore(application.hyprial_home)
        store.adopt(parse_document(_document(9)))
        before = store.accepted_path.read_bytes()
        assert application.handle("org.publish", {})["orgfsPublished"] is False
        assert store.orgfs_publish_pending() == hashlib.sha256(before).hexdigest()
        assert store.accepted_path.read_bytes() == before
    finally:
        _close(application)


def test_m1_requires_joined_space_and_config_switch_is_public_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    application = _application(tmp_path)
    try:
        monkeypatch.setenv("HYPRIAL_HOME", str(application.hyprial_home))
        monkeypatch.setenv("HARNESS_STATE_DIR", str(application.state_dir))
        configured = CliRunner().invoke(
            _cli_root_app,
            ["config", "set", "org.fetchSource", "orgfs", "--json"],
        )
        assert configured.exit_code == 0, configured.output
        assert json.loads(configured.stdout)["value"] == "orgfs"
        assert json.loads(configured.stdout)["appliesOn"] == "next org fetch"

        with pytest.raises(DaemonRequestError) as failure:
            application.handle("org.fetch", {"timeoutSeconds": 0.2})
        assert failure.value.code == ipc_errors.ORG_SPACE_NOT_JOINED
        assert failure.value.data == {"hint": "hyprial org fs space join <spaceId>"}

        rolled_back = CliRunner().invoke(
            _cli_root_app,
            ["config", "set", "org.fetchSource", "mesh", "--json"],
        )
        assert rolled_back.exit_code == 0, rolled_back.output
        assert application.handle("org.fetch", {})["candidates"] == []
    finally:
        _close(application)


def test_morg_provenance_names_the_node_whose_text_was_staged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A move/recreate between reading a candidate and naming it must not pair
    the old text's version with the successor node's URI."""

    application = _application(tmp_path)
    try:
        space_id = _space(application)
        fs = application._require_orgfs_runtime().facade
        fs.write_text(space_id, "candidates/good.md", _document(2, "good"))
        original = fs.stat(space_id, "candidates/good.md")
        _enable_m1(application)
        original_read = fs.read_text_snapshot
        swapped: list[str] = []

        def read_then_swap(space: str, node: str):
            result = original_read(space, node)
            if not swapped:
                swapped.append(node)
                fs.move(space, f"id:{original.node_id}", "candidates/moved.md")
                fs.write_text(space, "candidates/good.md", _document(2, "successor"))
            return result

        monkeypatch.setattr(fs, "read_text_snapshot", read_then_swap)

        application.handle("org.fetch", {"timeoutSeconds": 0.2})

        pending = OrgContextStore(application.hyprial_home).list_pending()
        assert swapped, "the swap must have happened during staging"
        staged = [record for record in pending if record.publisher == "good"]
        assert len(staged) == 1
        assert staged[0].source.rsplit("@", 1)[0] == original.uri
    finally:
        _close(application)


@pytest.mark.parametrize(
    ("name", "mirrored"), [("context", True), ("org-context", False), ("team", False)]
)
def test_creating_the_context_space_mirrors_accepted_bytes(
    tmp_path: Path, name: str, mirrored: bool
) -> None:
    """Allen 2026-09-29: the org-context space is named exactly ``context``.

    Creating it through the daemon mirrors this node's accepted bytes into
    ``candidates/<user>.md``; any other name, including the old one, does not.
    """

    application = _application(tmp_path)
    try:
        store = OrgContextStore(application.hyprial_home)
        store.adopt(parse_document(_document(8)))
        accepted = store.accepted_path.read_bytes()
        created = application.handle("orgfs.create", {"name": name})
        runtime = application._require_orgfs_runtime()
        username = runtime.author.removeprefix("user:")
        path = f"candidates/{username}.md"
        if mirrored:
            text, _version = runtime.facade.read_text(created["space_id"], path)
            assert text.encode("utf-8") == accepted
        else:
            with pytest.raises(OrgFsError):
                runtime.facade.read_text(created["space_id"], path)
    finally:
        _close(application)


def test_orgfs_publish_refuses_corrupt_bytes_for_a_new_candidate(
    tmp_path: Path,
) -> None:
    """The candidate publish path is a writer: it validates before any write."""

    application = _application(tmp_path)
    try:
        space_id = _space(application)
        bridge = application._require_org_context_bridge()
        store = bridge.store
        corrupt = CORRUPT_ESCAPED.read_text(encoding="utf-8")
        store.ensure_layout()
        store.accepted_path.write_text(corrupt, encoding="utf-8")
        events: list[tuple[str, str, dict[str, object]]] = []
        bridge.logger = lambda level, event, **fields: events.append(
            (level, event, fields)
        )

        assert bridge.publish_accepted() is False

        runtime = application._require_orgfs_runtime()
        path = "candidates/" + runtime.author.removeprefix("user:") + ".md"
        with pytest.raises(OrgFsError):
            runtime.facade.read_text(space_id, path)
        assert not store.publish_pending_path.exists()
        assert validate_document(corrupt).ok is False
        assert events and events[0][0] == "warn"
    finally:
        _close(application)


def test_orgfs_publish_refuses_corrupt_bytes_over_an_existing_candidate(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    try:
        space_id = _space(application)
        bridge = application._require_org_context_bridge()
        store = bridge.store
        store.adopt(parse_document(_document(1)))
        assert bridge.publish_accepted() is True
        runtime = application._require_orgfs_runtime()
        path = "candidates/" + runtime.author.removeprefix("user:") + ".md"
        published, _version = runtime.facade.read_text(space_id, path)
        store.accepted_path.write_text(
            CORRUPT_ESCAPED.read_text(encoding="utf-8"), encoding="utf-8"
        )

        assert bridge.publish_accepted() is False

        current, _version = runtime.facade.read_text(space_id, path)
        assert current == published
        assert not store.publish_pending_path.exists()
    finally:
        _close(application)
