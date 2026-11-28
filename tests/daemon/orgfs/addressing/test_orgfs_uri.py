# Decisions (phase D, package D4b, 2026-11-25):
# - The error-code gate covers the state host's packages (state, membership,
#   sync, document/coordinator, composition/state): besides ``OrgFsError`` it
#   reads ``SyncError`` and ``StoreError``, whose codes reach the surface
#   through the join, blob, commit and startup-refusal relays, and resolves
#   bare kernel constants and both arms of a conditional.  Non-literal codes
#   are relays and are pinned site by site in ``_RELAYS``, so a new dynamic
#   producer is red; each relays a code already produced (and scanned)
#   elsewhere, or a peer's code over the sync wire.
# - Upper-case codes are pinned in their own set (``_UPPER_CODES``): the
#   kernel registry declares them by shape and ``ORGFS_ERROR_CODES`` must stay
#   a subset of the frozen kebab list (test_legacy_error_codes).
# - ``orgfs.restore`` takes no ``version`` (design §1.1).
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


def test_orgfs_ls_recursive_returns_descendants_through_the_daemon_entry(
    tmp_path,
) -> None:
    application = _application(tmp_path)
    try:
        space_id, _written = _create_doc(application)
        application._handle_orgfs(
            "orgfs.mkdir", {"spaceId": space_id, "path": "docs"}
        )
        application._handle_orgfs(
            "orgfs.mkdir", {"spaceId": space_id, "path": "docs/nested"}
        )
        application._handle_orgfs(
            "orgfs.write",
            {"spaceId": space_id, "path": "docs/nested/guide.md", "text": "guide"},
        )

        direct = application._handle_orgfs(
            "orgfs.ls", {"spaceId": space_id, "path": ""}
        )
        assert [node["path"] for node in direct["nodes"]] == ["docs", "readme.md"]

        recursive = application._handle_orgfs(
            "orgfs.ls", {"spaceId": space_id, "path": "", "recursive": True}
        )
        assert [(node["path"], node["kind"]) for node in recursive["nodes"]] == [
            ("docs", "dir"),
            ("docs/nested", "dir"),
            ("docs/nested/guide.md", "doc"),
            ("readme.md", "doc"),
        ]
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
            {"spaceId": space_id, "node": uri},
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


_SCANNED_ERRORS = ("OrgFsError", "SyncError", "StoreError")

_RELAYS = {
    ("composition/lifecycle.py", "OrgFsError", "error.code"),
    ("composition/state/sync.py", "OrgFsError", "code"),
    ("document/coordinator/commit.py", "OrgFsError", "str(code)"),
    ("document/coordinator/spaces.py", "OrgFsError", "refused[0]"),
    ("document/space_state.py", "OrgFsError", "error.code"),
    ("document/space_state.py", "StoreError", "error.code"),
    ("sync/blobs.py", "SyncError", "code"),
    ("sync/pull.py", "SyncError", "reply.code"),
}

_UPPER_CODES = {"ORGFS_LAYOUT_UNSUPPORTED", "ORGFS_INVALID_SPACE_DIRECTORY"}

_NEW_PACKAGES = ("state", "membership", "sync", "document/coordinator", "composition/state")


def _scan_error_codes(src):
    """Codes, relay sites and producing files of every scanned error call."""

    import ast

    from hyprial.kernel.impl.contracts.daemon import ipc_errors

    codes: set[str] = set()
    relays: set[tuple[str, str, str]] = set()
    files: set[str] = set()

    def resolve(node) -> list[str] | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "ipc_errors"
        ):
            return [getattr(ipc_errors, node.attr)]
        if isinstance(node, ast.Name) and node.id.isupper() and hasattr(ipc_errors, node.id):
            return [getattr(ipc_errors, node.id)]
        if isinstance(node, ast.IfExp):
            body, orelse = resolve(node.body), resolve(node.orelse)
            return None if body is None or orelse is None else body + orelse
        return None

    for path in sorted(src.rglob("*.py")):
        relative = path.relative_to(src).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in _SCANNED_ERRORS
                and node.args
            ):
                continue
            found = resolve(node.args[0])
            if found is None:
                relays.add((relative, node.func.id, ast.unparse(node.args[0])))
            else:
                codes.update(found)
                files.add(relative)
    return codes, relays, files


def test_orgfs_error_code_registry_covers_every_raised_code() -> None:
    """The §5 registry gate over the whole package, the D4b packages included.

    Every code given to ``OrgFsError``, ``SyncError`` (relayed by the join and
    blob paths) or ``StoreError`` (relayed by the commit path and the startup
    refusal) as a literal, an ``ipc_errors`` attribute, a bare kernel constant
    or either arm of a conditional must be registered: kebab codes in
    ``ORGFS_ERROR_CODES``, upper-case codes in ``_UPPER_CODES``.  Any other
    expression is a relay and must be listed in ``_RELAYS``."""

    from pathlib import Path

    src = Path(__file__).resolve().parents[4] / "src/hyprial/daemon/impl/orgfs"
    codes, relays, files = _scan_error_codes(src)

    assert "cross-space-uri" in codes, "the multi-line cross-space-uri raise must be seen"
    for package in _NEW_PACKAGES:
        assert any(name.startswith(package + "/") for name in files), package
    assert relays == _RELAYS
    upper = {code for code in codes if code.isupper()}
    assert upper <= _UPPER_CODES, sorted(upper - _UPPER_CODES)
    unregistered = codes - upper - ORGFS_ERROR_CODES
    assert not unregistered, (
        f"orgfs error codes raised but not registered in ORGFS_ERROR_CODES: "
        f"{sorted(unregistered)}"
    )
    assert {"invalid-uri", "cross-space-uri"} <= ORGFS_ERROR_CODES


def test_the_error_code_scan_sees_each_producer_shape(tmp_path) -> None:
    """The scan itself: a new code in any shape, or a new relay, is visible."""

    source = tmp_path / "sync"
    source.mkdir()
    (source / "probe.py").write_text(
        "from hyprial.kernel import ORGFS_LAYOUT_UNSUPPORTED, ipc_errors\n"
        "SyncError('brand-new-code', 'x')\n"
        "StoreError(ORGFS_LAYOUT_UNSUPPORTED, 'x')\n"
        "OrgFsError('a' if flag else ipc_errors.ORGFS_INVALID_URI)\n"
        "OrgFsError(other.code)\n",
        encoding="utf-8",
    )

    codes, relays, files = _scan_error_codes(tmp_path)

    assert codes == {"brand-new-code", "ORGFS_LAYOUT_UNSUPPORTED", "a", "invalid-uri"}
    assert relays == {("sync/probe.py", "OrgFsError", "other.code")}
    assert files == {"sync/probe.py"}
