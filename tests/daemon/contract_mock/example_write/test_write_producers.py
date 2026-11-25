# Decision record (OrgFS package S, 2026-11-25): an OrgFS version is opaque
# (a digest), so the projection check only requires a non-empty string.  The
# two examples whose behaviour changes with D4b (join of a non-UUID id, restore
# with a version) are strict xfails named in ``D4B_PENDING`` until D4b
# re-records them.
"""Example values must be obtainable from real write producers."""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.request_catalog.org.orgfs.write_examples import ORGFS_WRITE_EXAMPLES
from hyprial.kernel import DaemonRequestError

from .producers import observation, plain

CASES = [(item.method, case) for item in ORGFS_WRITE_EXAMPLES for case in item.cases if case.kind != "invalid-params"]

D4B_PENDING = {
    "orgfs.join:missing-space": (
        "D4b: the state runtime answers a join of a space id that is not a UUID with "
        "unknown-space (reason not-a-uuid); the example still records the journal "
        "runtime's no-holder-online"
    ),
    "orgfs.restore:missing-version": (
        "D4b: restore loses its version argument; the state host ignores it and "
        "restores the node, so the journal-era unknown-doc example no longer replays"
    ),
}


def _replay_params(cases):
    return [
        pytest.param(
            method,
            case,
            id=f"{method}:{case.name}",
            marks=(
                [pytest.mark.xfail(strict=True, reason=D4B_PENDING[f"{method}:{case.name}"])]
                if f"{method}:{case.name}" in D4B_PENDING
                else []
            ),
        )
        for method, case in cases
    ]


@pytest.mark.parametrize("method,case", _replay_params(CASES))
def test_examples_replay_real_write_producer(method, case, tmp_path):
    with observation(tmp_path / "isolated", method, case.name) as observed:
        if case.error is None:
            assert observed.call(method, case.params) == plain(case.result)
        else:
            with pytest.raises(DaemonRequestError) as raised:
                observed.call(method, case.params)
            assert raised.value.code == case.error.code
            assert observed.transform(raised.value.data) == plain(case.error.data)
            assert observed.transform(str(raised.value)) == case.error.message


def test_binary_digest_has_a_public_reproducible_input():
    import hashlib

    from .producers import CONTENT, SHA

    assert hashlib.sha256(CONTENT).hexdigest() == SHA


def test_node_uri_and_content_projection_invariants():
    from hyprial.kernel import parse_orgfs_uri
    from .producers import SHA

    for _method, case in CASES:
        value = plain(case.result)
        if value is None or "node_id" not in value:
            continue
        owner, space, node = parse_orgfs_uri(value["uri"])
        assert (owner, space, node) == ("example", value["space_id"], value["node_id"])
        assert not value["path"].startswith("/")
        assert value["modified_via"] is None
        assert isinstance(value["version"], str) and value["version"]
        if value["kind"] == "doc":
            assert value["doc_id"].startswith("doc-") and value["blob_hash"] is None
        elif value["kind"] == "blob":
            assert value["doc_id"] is None and value["blob_hash"] == SHA
        else:
            assert value["kind"] == "dir"
            assert value["doc_id"] is None and value["blob_hash"] is None
            assert value["contentState"] is None and value["size"] is None


SUCCESS_CASES = [(method, case) for method, case in CASES if case.error is None]


@pytest.mark.parametrize("method,case", SUCCESS_CASES, ids=[f"{method}:{case.name}" for method, case in SUCCESS_CASES])
def test_success_examples_change_actual_local_state(method, case, tmp_path):
    from pathlib import Path

    from hyprial.daemon.impl.orgfs.api import OrgFsError
    from .producers import SPACE, CONTENT

    with observation(tmp_path / "isolated", method, case.name) as observed:
        outcome = observed.call(method, case.params)
        fs = observed.runtime.facade
        space = observed.transform(SPACE, inverse=True)
        if method in ("orgfs.write", "orgfs.import", "orgfs.restore", "orgfs.move"):
            actual = fs.read_bytes(space, outcome["path"])
            expected = CONTENT if outcome["kind"] == "blob" else ("" if case.name in ("empty-text", "empty-document") else "hello world!").encode()
            assert actual == expected
        elif method == "orgfs.export":
            target = Path(observed.transform(case.params["destination"], inverse=True))
            assert target.read_bytes() == (b"" if case.name == "empty-export" else b"hello world!")
        elif method == "orgfs.mkdir":
            assert fs.stat(space, "docs").kind == "dir" and fs.listdir(space, "docs") == ()
        elif method == "orgfs.remove":
            assert len(fs.trash(space)) == 1 and fs.trash(space)[0].deleted
            with pytest.raises(OrgFsError, match="unknown-doc"):
                fs.stat(space, "readme.txt")
        elif method in ("orgfs.create", "orgfs.join"):
            assert observed.transform(fs.spaces()[0].space_id) == SPACE
        elif method == "orgfs.checkout":
            path = Path(observed.transform(outcome["path"], inverse=True))
            assert path.exists() is outcome["enabled"]
        elif method == "orgfs.invite":
            assert "user:reader" in {member.user for member in fs.members(space)}
        elif method == "orgfs.remove_member":
            assert "user:reader" not in {member.user for member in fs.members(space)}
        elif method == "orgfs.space_purge" and case.name == "producer-success":
            assert fs.spaces() == ()
