"""Same-source fixtures must be observations of the actual read producers."""

from __future__ import annotations

import hashlib

import pytest

from hyprial.daemon.impl.request_catalog.org.orgfs.read_examples import ORGFS_READ_EXAMPLES
from hyprial.kernel import DaemonRequestError, parse_orgfs_uri
from hyprial.daemon.impl.orgfs.api import OrgFsError

from .producers import PLAN, PLAN_UNSIGNED, observation, plain

CASES = [
    (example_set.method, case)
    for example_set in ORGFS_READ_EXAMPLES
    for case in example_set.cases
    if case.kind != "invalid-params"
]


@pytest.mark.parametrize("method,case", CASES, ids=[f"{method}:{case.name}" for method, case in CASES])
def test_examples_replay_real_producer(method, case, tmp_path):
    with observation(tmp_path / "runtime", method, case.name) as observed:
        if case.error is None:
            assert observed.call(method, case.params) == plain(case.result)
        else:
            with pytest.raises(DaemonRequestError) as raised:
                observed.call(method, case.params)
            assert raised.value.code == case.error.code
            assert observed.transform(raised.value.data) == plain(case.error.data)
            assert str(raised.value) == case.error.message


def test_spaces_have_no_fabricated_domain_error():
    example_set = next(item for item in ORGFS_READ_EXAMPLES if item.method == "orgfs.spaces")
    assert example_set.domain_errors_not_applicable
    assert all(case.kind not in ("domain-error", "semantic-param-error") for case in example_set.cases)


def test_empty_gaps_follow_actual_path_and_membership_rules(tmp_path):
    with observation(tmp_path / "runtime", "orgfs.resolve", "producer-success") as observed:
        space = observed.runtime.facade.spaces()[0]
        with pytest.raises(OrgFsError) as missing:
            observed.runtime.facade.resolve(space.space_id, "does-not-exist")
        assert missing.value.code == "unknown-doc"
        assert missing.value.details == {"node": "does-not-exist"}
        with pytest.raises(OrgFsError) as owner_removal:
            observed.runtime.facade.remove_member(space.space_id, space.owner)
        assert owner_removal.value.code == "invalid-argument"
        assert observed.runtime.facade.members(space.space_id)[0].user == space.owner


def test_doc_projection_invariants_are_preserved():
    def check(value):
        if isinstance(value, dict):
            if "uri" in value:
                owner, space, node = parse_orgfs_uri(value["uri"])
                assert owner == "example"
                assert space == value["space_id"]
                assert node == value["node_id"]
                assert not value["path"].startswith("/")
                assert value["kind"] == "doc"
                assert value["blob_hash"] is None
                assert value["doc_id"].startswith("doc-")
            for item in value.values():
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)
    for _method, case in CASES:
        if case.result:
            check(plain(case.result))


def test_fixed_plan_id_is_a_real_synthetic_plan_digest():
    from hyprial.daemon.impl.orgfs.projection.purge import canonical_bytes

    assert hashlib.sha256(canonical_bytes(PLAN_UNSIGNED)).hexdigest() == PLAN
    example_set = next(item for item in ORGFS_READ_EXAMPLES if item.method == "orgfs.purge_status")
    assert example_set.cases[0].params["planId"] == PLAN
    assert example_set.cases[0].result["plan_id"] == PLAN
