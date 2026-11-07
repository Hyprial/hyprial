"""orgfs.import / orgfs.export go through path_authz for every caller kind (#1169).

The gap this closes: over MCP or the socket, orgfs.import could copy any file
the daemon can read (``~/.hyprial/secrets`` included) into a space that syncs
to other members, and orgfs.export could write any path the daemon can.
"""

from __future__ import annotations

import os
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import pytest

from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.daemon.impl.application.messaging import orgfs_bridge
from tests.daemon.path_authz.test_send_gate import ACTOR_NAME, _OPERATOR, _SESSION


def _space(gated) -> str:
    return gated["app"].handle(
        "orgfs.create", {"name": "gate"}, _trusted_message_origin="ipc"
    )["space_id"]


def _call(gated, method: str, caller: dict[str, str], **params):
    return gated["app"].handle(
        method, {**caller, **params}, _trusted_message_origin="ipc"
    )


def _refused(gated, method: str, caller: dict[str, str], **params) -> DaemonRequestError:
    with pytest.raises(DaemonRequestError) as failure:
        _call(gated, method, caller, **params)
    assert failure.value.code == "ATTACHMENT_PATH_REFUSED"
    return failure.value


def test_session_imports_from_its_workspace_and_cwd(gated) -> None:
    space = _space(gated)
    (gated["workspace"] / "a.bin").write_bytes(b"workspace bytes")
    (gated["repo"] / "b.bin").write_bytes(b"cwd bytes")

    _call(gated, "orgfs.import", _SESSION, spaceId=space, path="a.bin",
          source=str(gated["workspace"] / "a.bin"))
    _call(gated, "orgfs.import", _SESSION, spaceId=space, path="b.bin",
          source=str(gated["repo"] / "b.bin"))

    fs = gated["app"]._require_orgfs_runtime().facade
    assert fs.read_bytes(space, "a.bin") == b"workspace bytes"
    assert fs.read_bytes(space, "b.bin") == b"cwd bytes"


def test_session_cannot_import_hyprial_secrets_or_outside_files(gated) -> None:
    space = _space(gated)
    secret = gated["home"] / "secrets" / "lark-alicesmith-hyprial-e2e.json"
    (gated["outside"] / "private.txt").write_text("not yours")
    link = gated["repo"] / "creds.json"
    link.symlink_to(secret)

    for source, reason in (
        (secret, "hyprial-home"),
        (link, "hyprial-home"),
        (gated["outside"] / "private.txt", "outside-allowed-roots"),
    ):
        error = _refused(gated, "orgfs.import", _SESSION, spaceId=space,
                         path="x.bin", source=str(source))
        assert error.data["reason"] == reason
        assert "secrets" not in str(error.data)
    fs = gated["app"]._require_orgfs_runtime().facade
    assert [node.name for node in fs.listdir(space, "")] == []


def test_unauthenticated_claim_may_import_and_export_nothing(gated) -> None:
    space = _space(gated)
    (gated["repo"] / "a.bin").write_bytes(b"cwd bytes")

    error = _refused(gated, "orgfs.import", {"from": ACTOR_NAME}, spaceId=space,
                     path="a.bin", source=str(gated["repo"] / "a.bin"))
    assert error.data["reason"] == "caller-not-authenticated"
    _call(gated, "orgfs.write", _OPERATOR, spaceId=space, node="n.md", text="x")
    error = _refused(gated, "orgfs.export", {"from": ACTOR_NAME}, spaceId=space,
                     node="n.md", destination=str(gated["repo"] / "n.md"))
    assert error.data["reason"] == "caller-not-authenticated"
    assert not (gated["repo"] / "n.md").exists()


def test_session_exports_only_into_its_roots(gated) -> None:
    space = _space(gated)
    _call(gated, "orgfs.write", _OPERATOR, spaceId=space, node="n.md", text="hello")

    _call(gated, "orgfs.export", _SESSION, spaceId=space, node="n.md",
          destination=str(gated["workspace"] / "n.md"))
    assert (gated["workspace"] / "n.md").read_text() == "hello"

    for destination, reason in (
        (gated["home"] / "secrets" / "planted.json", "hyprial-home"),
        (gated["home"] / "agents" / "other" / "workspace" / "x.md", "other-agent-home"),
        (gated["outside"] / "n.md", "outside-allowed-roots"),
    ):
        error = _refused(gated, "orgfs.export", _SESSION, spaceId=space, node="n.md",
                         destination=str(destination))
        assert error.data["reason"] == reason
        assert not destination.exists()

    error = _refused(gated, "orgfs.export", _SESSION, spaceId=space, node="n.md",
                     destination=str(gated["workspace"] / "n.md"))
    assert error.data["reason"] == "exists"
    _call(gated, "orgfs.export", _SESSION, spaceId=space, node="n.md",
          destination=str(gated["workspace"] / "n.md"), overwrite=True)


def test_operator_exports_anywhere_but_the_hyprial_home(gated) -> None:
    space = _space(gated)
    _call(gated, "orgfs.write", _OPERATOR, spaceId=space, node="n.md", text="hello")

    _call(gated, "orgfs.export", _OPERATOR, spaceId=space, node="n.md",
          destination=str(gated["outside"] / "n.md"))
    assert (gated["outside"] / "n.md").read_text() == "hello"

    secret = gated["home"] / "secrets" / "lark-alicesmith-hyprial-e2e.json"
    secret_bytes = secret.read_bytes()
    error = _refused(gated, "orgfs.export", _OPERATOR, spaceId=space, node="n.md",
                     destination=str(secret))
    assert error.data["reason"] == "hyprial-home"
    rendered = f"{error} {error.data}"
    assert str(secret) not in rendered
    assert secret_bytes.decode() not in rendered


def test_export_judges_a_refused_destination_before_reading_node_content(
    gated, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m2: a denied local path is rejected before orgfs content is touched."""

    space = _space(gated)
    _call(gated, "orgfs.write", _OPERATOR, spaceId=space, node="n.md", text="secret bytes")
    fs = gated["app"]._require_orgfs_runtime().facade

    def content_read_too_early(*_args, **_kwargs):
        raise AssertionError("orgfs content was read before destination authorization")

    monkeypatch.setattr(fs, "read_bytes", content_read_too_early)
    destination = gated["home"] / "secrets" / "must-not-exist.txt"

    error = _refused(
        gated,
        "orgfs.export",
        _OPERATOR,
        spaceId=space,
        node="n.md",
        destination=str(destination),
    )

    assert error.data == {"reason": "hyprial-home"}
    assert not destination.exists()


def test_export_reuses_the_registered_import_size_limit_before_content_read(
    gated, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m2: the export cap is the registered import limit, checked from stat."""

    space = _space(gated)
    _call(gated, "orgfs.write", _OPERATOR, spaceId=space, node="large.bin", text="12345")
    fs = gated["app"]._require_orgfs_runtime().facade
    monkeypatch.setattr(orgfs_bridge, "ORGFS_EXPORT_MAX_BYTES", 4, raising=False)

    def content_read_too_early(*_args, **_kwargs):
        raise AssertionError("oversized orgfs content was read")

    monkeypatch.setattr(fs, "read_bytes", content_read_too_early)
    destination = gated["outside"] / "large.bin"

    with pytest.raises(DaemonRequestError) as failure:
        _call(
            gated,
            "orgfs.export",
            _OPERATOR,
            spaceId=space,
            node="large.bin",
            destination=str(destination),
        )

    assert failure.value.code == "too-large"
    assert failure.value.data == {"maxBytes": 4, "use": "export"}
    assert not destination.exists()


def test_session_import_refuses_a_hardlinked_source(gated) -> None:
    """#1205: orgfs.import shares the read-side multiply-linked refusal."""

    space = _space(gated)
    secret = gated["home"] / "secrets" / "lark-alicesmith-hyprial-e2e.json"
    link = gated["repo"] / "innocent.bin"
    os.link(secret, link)

    error = _refused(gated, "orgfs.import", _SESSION, spaceId=space,
                     path="x.bin", source=str(link))

    assert error.data["reason"] == "hardlinked"
    assert "path" not in error.data
    assert str(link) not in str(error) and "secrets" not in str(error.data)
    fs = gated["app"]._require_orgfs_runtime().facade
    assert [node.name for node in fs.listdir(space, "")] == []
