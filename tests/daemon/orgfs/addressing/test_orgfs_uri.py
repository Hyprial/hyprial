"orgfs node URIs through the production entries (design §9).\n\n``DaemonApplication._handle_orgfs`` is the seam both the CLI and the MCP\nserver call into, so these tests cover every surface at once (the CLI's own\nforms live in tests/shell/cli/orgfs/test_orgfs_uri_cli.py).  Each test names the\nkill-mutation that must turn it red.\n"

from __future__ import annotations

import uuid

import pytest

from hyprial.kernel.impl.contracts.daemon.ipc_errors import DaemonRequestError
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.orgfs.api import ORGFS_ERROR_CODES, OrgFsError
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.kernel.impl.primitives.uri import canonical_orgfs_uri, parse_orgfs_uri
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies

OWNER = "owner@example.com"


class _FakeTransport:
    def close(self) -> None:
        return None


def _application(tmp_path) -> DaemonApplication:
    application = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id="node-a",
        hyprial_home=tmp_path / "home",
        owner=OWNER,
    **_deep_module_daemon_dependencies())
    application._transport = _FakeTransport()  # type: ignore[assignment]
    return application


def _close(application: DaemonApplication) -> None:
    application._close()
    application._close_home_lease()


def _create_doc(application: DaemonApplication, name: str = "team") -> tuple[str, dict]:
    space_id = application._handle_orgfs("orgfs.create", {"name": name})["space_id"]
    written = application._handle_orgfs(
        "orgfs.write",
        {"spaceId": space_id, "path": "readme.md", "text": "hello"},
    )
    return space_id, written


def test_stat_and_resolve_round_trip_through_daemon(tmp_path) -> None:
    """Kill-mutation 3: a ``user:``-prefixed owner in _node_uri mints a
    five-segment string that parse_orgfs_uri rejects → this test goes red."""

    application = _application(tmp_path)
    try:
        space_id, written = _create_doc(application)
        expected = canonical_orgfs_uri(OWNER, space_id, written["node_id"])
        assert written["uri"] == expected

        by_uri = application._handle_orgfs(
            "orgfs.stat", {"spaceId": space_id, "node": expected}
        )
        assert by_uri["node_id"] == written["node_id"]
        assert by_uri["uri"] == expected

        resolved = application._handle_orgfs(
            "orgfs.resolve", {"spaceId": space_id, "path": expected}
        )
        assert [node["uri"] for node in resolved["nodes"]] == [expected]
    finally:
        _close(application)


def test_cross_space_and_malformed_uris_are_typed_errors(tmp_path) -> None:
    """Kill-mutation 2: dropping the owner/spaceId equality check in
    _resolve_ids turns the cross-space assertions red."""

    application = _application(tmp_path)
    try:
        space_a, written = _create_doc(application, "a")
        space_b = application._handle_orgfs("orgfs.create", {"name": "b"})["space_id"]

        with pytest.raises(DaemonRequestError) as cross:
            application._handle_orgfs(
                "orgfs.stat", {"spaceId": space_b, "node": written["uri"]}
            )
        assert cross.value.code == "cross-space-uri"
        assert cross.value.data["expectedSpaceId"] == space_b
        assert cross.value.data["uriSpaceId"] == space_a

        # An owner mismatch is a forged/corrupted URI: loud, never retargeted.
        forged = canonical_orgfs_uri(
            "someone-else@example.com", space_a, written["node_id"]
        )
        with pytest.raises(DaemonRequestError) as owner_mismatch:
            application._handle_orgfs(
                "orgfs.stat", {"spaceId": space_a, "node": forged}
            )
        assert owner_mismatch.value.code == "cross-space-uri"
        assert owner_mismatch.value.data["expectedOwner"] == f"user:{OWNER}"
        assert owner_mismatch.value.data["uriOwner"] == "someone-else@example.com"

        with pytest.raises(DaemonRequestError) as malformed:
            application._handle_orgfs(
                "orgfs.stat", {"spaceId": space_a, "node": "orgfs:not-a-uri"}
            )
        assert malformed.value.code == "invalid-uri"

        # A URI for a space not joined on this node fails as unknown-space;
        # a URI never triggers a join.
        ghost = canonical_orgfs_uri(OWNER, str(uuid.uuid4()), "root")
        with pytest.raises(DaemonRequestError) as unjoined:
            application._handle_orgfs(
                "orgfs.stat",
                {"spaceId": parse_orgfs_uri(ghost)[1], "node": ghost},
            )
        assert unjoined.value.code == "unknown-space"
    finally:
        _close(application)


def test_write_with_uri_of_nonexistent_node_creates_nothing(tmp_path) -> None:
    """Kill-mutation 4: a _parent_for_new that stops rejecting orgfs: refs
    creates a doc literally named ``orgfs:…`` → the listdir assertion fails."""

    application = _application(tmp_path)
    try:
        space_id, _written = _create_doc(application)
        ghost_uri = canonical_orgfs_uri(OWNER, space_id, "0" * 32)
        with pytest.raises(DaemonRequestError) as rejected:
            application._handle_orgfs(
                "orgfs.write",
                {"spaceId": space_id, "path": ghost_uri, "text": "sneaky"},
            )
        assert rejected.value.code == "unknown-doc"
        listing = application._handle_orgfs("orgfs.ls", {"spaceId": space_id, "path": ""})
        assert [node["name"] for node in listing["nodes"]] == ["readme.md"]
    finally:
        _close(application)


def test_uri_is_stable_across_move_trash_and_restore(tmp_path) -> None:
    application = _application(tmp_path)
    try:
        space_id, written = _create_doc(application)
        uri = written["uri"]

        moved = application._handle_orgfs(
            "orgfs.move",
            {"spaceId": space_id, "sourcePath": uri, "destinationPath": "guide.md"},
        )
        assert moved["uri"] == uri

        application._handle_orgfs("orgfs.remove", {"spaceId": space_id, "node": uri})
        # Trashed node on live surfaces: unknown-doc, exactly the id: contract.
        with pytest.raises(DaemonRequestError) as trashed:
            application._handle_orgfs("orgfs.stat", {"spaceId": space_id, "node": uri})
        assert trashed.value.code == "unknown-doc"

        trash = application._handle_orgfs("orgfs.trash", {"spaceId": space_id})
        assert [node["uri"] for node in trash["nodes"]] == [uri]

        restored = application._handle_orgfs(
            "orgfs.restore",
            {"spaceId": space_id, "node": uri, "version": moved["version"]},
        )
        assert restored["uri"] == uri
        assert (
            application._handle_orgfs("orgfs.stat", {"spaceId": space_id, "node": uri})[
                "uri"
            ]
            == uri
        )

        root = application._handle_orgfs(
            "orgfs.stat",
            {"spaceId": space_id, "node": canonical_orgfs_uri(OWNER, space_id, "root")},
        )
        assert root["node_id"] == "root"
    finally:
        _close(application)


def test_history_and_stat_at_carry_the_uri(tmp_path) -> None:
    """Kill-mutation 5: a _snapshot_info that stops populating ``uri`` raises
    TypeError (the field is keyword-only and required) → this test goes red."""

    application = _application(tmp_path)
    try:
        space_id, written = _create_doc(application)
        uri = written["uri"]
        rewritten = application._handle_orgfs(
            "orgfs.write", {"spaceId": space_id, "path": uri, "text": "v2"}
        )
        assert rewritten["uri"] == uri

        history = application._handle_orgfs(
            "orgfs.history", {"spaceId": space_id, "node": uri}
        )
        assert history["history"]
        assert {entry["node"]["uri"] for entry in history["history"]} == {uri}

        at = application._handle_orgfs(
            "orgfs.stat_at",
            {"spaceId": space_id, "node": uri, "version": written["version"]},
        )
        assert at["uri"] == uri
    finally:
        _close(application)


def test_non_user_space_owner_fails_with_a_typed_error(tmp_path) -> None:
    """Q6=A: a legacy/invalid (non ``user:``) owner never silently mints a
    URI — _node_uri raises the typed invalid-argument error."""

    runtime = OrgFsRuntime(tmp_path / "runtime", node_id="node-a", author=OWNER)
    try:
        space = runtime.facade.create_space("legacy")
        with pytest.raises(OrgFsError) as rejected:
            runtime.facade.write_text(space.space_id, "a.md", "hi")
        assert rejected.value.code == "invalid-argument"
    finally:
        runtime.close()


def test_orgfs_error_code_registry_covers_every_raised_code() -> None:
    """The §5 registry gate: every OrgFsError(<code>, ...) raised in orgfs,
    whether a string literal or an ipc_errors constant, and however it is
    wrapped across lines, must be registered in ORGFS_ERROR_CODES."""

    import ast
    from pathlib import Path

    from hyprial.kernel.impl.contracts.daemon import ipc_errors

    src = Path(__file__).resolve().parents[4] / 'src/hyprial/daemon/impl/orgfs'
    raised: set[str] = set()
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "OrgFsError"
                and node.args
            ):
                continue
            code = node.args[0]
            if isinstance(code, ast.Constant) and isinstance(code.value, str):
                raised.add(code.value)
            elif (
                isinstance(code, ast.Attribute)
                and isinstance(code.value, ast.Name)
                and code.value.id == "ipc_errors"
            ):
                raised.add(getattr(ipc_errors, code.attr))
    assert "cross-space-uri" in raised, "the multi-line cross-space-uri raise must be seen"
    unregistered = raised - ORGFS_ERROR_CODES
    assert not unregistered, (
        f"orgfs error codes raised but not registered in ORGFS_ERROR_CODES: "
        f"{sorted(unregistered)}"
    )
    assert {"invalid-uri", "cross-space-uri"} <= ORGFS_ERROR_CODES
