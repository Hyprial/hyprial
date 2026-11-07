import hashlib
import json
from pathlib import Path

import pytest

from hyprial.daemon.impl.pac.contracts.expansion.document import expansion_digest
from hyprial.daemon.impl.pac.contracts.expansion.policy import (
    effective_limits,
    load_expansion_policy,
)
from hyprial.daemon.impl.pac.contracts.expansion.validation import (
    validate_expansion,
    validate_parent,
)
from hyprial.daemon.impl.pac.contracts.workflow import load_workflow_text
from hyprial.identity import (
    PAC_EXPANSION_INVALID,
    PAC_EXPANSION_POLICY_UNAVAILABLE,
    PacError,
)


def policy(root: Path, **limits):
    payload = {"version": 1, "workRoot": str(root)}
    if limits:
        payload["limits"] = limits
    (root / "pac-expansion.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    return load_expansion_policy(root)


def parent_text(*, placeholder_limits=None, extra="", include_end=False):
    limits = (
        "\n    limits: " + json.dumps(placeholder_limits)
        if placeholder_limits is not None
        else ""
    )
    end = """
  - id: finish
    kind: end
    owner: user:alice
    task: finish
    after: [join]
""" if include_end else ""
    return f"""version: 2
name: parent
nodes:
  - id: planner
    kind: task
    role: plan
    expands: join
    task: plan the child
  - id: join
    kind: expansion
    task: child placeholder{limits}
    after: [planner]
{end}
{extra}"""


def child_text(body="""  - id: build
    task: build it
    role: execute
    timeout: 50ms
"""):
    return "version: 2\nnodes:\n" + body


def test_policy_defaults_and_parent_snapshot_are_trusted(tmp_path):
    loaded = policy(tmp_path)
    assert loaded["limits"] == {
        "maxNodes": 12,
        "tiers": ["fast", "strong"],
        "owners": [],
        "cwdUnder": [str(tmp_path.resolve())],
    }
    spec = validate_parent(
        load_workflow_text(parent_text()),
        policy=loaded,
    )
    assert spec.expansion_policy == loaded
    assert spec.nodes[1].to_json()["ownership"] == "system"


def test_limits_only_tighten_and_empty_lists_mean_none(tmp_path):
    loaded = policy(tmp_path, maxNodes=4, tiers=["fast", "strong"])
    assert effective_limits(
        {"maxNodes": 2, "tiers": ["fast"]}, policy=loaded
    )["maxNodes"] == 2
    for override in (
        {"maxNodes": 5},
        {"tiers": ["super"]},
        {"owners": ["user:alice"]},
        {"cwdUnder": [str(tmp_path.parent)]},
        {"workRoot": "/other"},
    ):
        with pytest.raises(PacError) as caught:
            effective_limits(override, policy=loaded)
        assert caught.value.code == PAC_EXPANSION_INVALID


def test_policy_errors_are_only_relevant_to_expansion(tmp_path):
    ordinary = load_workflow_text("version: 2\nname: work\nnodes:\n- id: a\n  task: a\n")
    assert validate_parent(ordinary, policy={}) is ordinary
    (tmp_path / "pac-expansion.json").write_text("{bad", encoding="utf-8")
    with pytest.raises(PacError) as caught:
        load_expansion_policy(tmp_path)
    assert caught.value.code == PAC_EXPANSION_POLICY_UNAVAILABLE


def test_digest_is_format_stable_but_rejects_duplicates_aliases_and_depth():
    left = "version: 2\nnodes:\n  - id: a\n    task: x\n"
    right = '{"nodes":[{"task":"x","id":"a"}],"version":2}'
    assert expansion_digest(left) == expansion_digest(right)
    assert expansion_digest(left) == hashlib.sha256(
        json.dumps(
            {"version": 2, "nodes": [{"id": "a", "task": "x"}]},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    for raw in (
        "version: 2\nversion: 3\n",
        "base: &x {id: a}\ncopy: *x\n",
        "{1: value}\n",
    ):
        with pytest.raises(PacError) as caught:
            expansion_digest(raw)
        assert caught.value.code == PAC_EXPANSION_INVALID


def test_child_validation_enforces_count_tier_owner_args_and_deadline(tmp_path):
    root = tmp_path.resolve()
    loaded = policy(root, maxNodes=1, tiers=["fast"], owners=["user:alice"])
    limits = effective_limits(None, policy=loaded)
    spec = validate_expansion(
        child_text(),
        limits=limits,
        child_graph_id="wf-child",
        deadline_ms=200,
        at=100,
        name="parent",
        escalate_to="user:alice",
    )
    assert len(spec.nodes) == 1
    assert spec.nodes[0].launch["tier"] == "fast"
    assert spec.on_failure == "terminate"
    assert spec.escalate_to == "user:alice"

    bads = (
        child_text("  - id: a\n    task: a\n    timeout: 50ms\n  - id: b\n    task: b\n    timeout: 50ms\n"),
        child_text("  - id: a\n    task: a\n    timeout: 50ms\n    launch: {model: fable, harness: claude}\n"),
        child_text("  - id: a\n    task: a\n    timeout: 50ms\n    owner: user:bob\n"),
        child_text("  - id: a\n    task: a\n    timeout: 50ms\n    launch: {tier: fast, args: [--x]}\n"),
    )
    for raw in bads:
        with pytest.raises(PacError) as caught:
            validate_expansion(
                raw,
                limits=limits,
                child_graph_id="wf-child",
                deadline_ms=200,
                at=100,
                name="parent",
                escalate_to=None,
            )
        assert caught.value.code == PAC_EXPANSION_INVALID


def test_child_validation_accepts_immutable_id_in_borrowed_owner(tmp_path):
    loaded = policy(tmp_path, owners=["user:u.alice"])
    limits = effective_limits(None, policy=loaded)
    raw = child_text(
        "  - id: review\n"
        "    task: review\n"
        "    timeout: 50ms\n"
        "    owner: user:u.alice\n"
    )
    spec = validate_expansion(
        raw,
        limits=limits,
        child_graph_id="wf-child",
        deadline_ms=200,
        at=100,
        name="parent",
        escalate_to=None,
    )
    assert spec.nodes[0].owner == "user:u.alice"


def test_child_cwd_is_canonical_and_authorized(tmp_path):
    root = tmp_path.resolve()
    allowed = root / "allowed"
    allowed.mkdir()
    outside = root / "outside"
    outside.mkdir()
    workspace = allowed / "x"
    workspace.mkdir()
    loaded = policy(root, cwdUnder=[str(allowed)])
    limits = effective_limits(None, policy=loaded)
    valid = child_text(
        f"  - id: review\n    role: review\n    task: review\n    timeout: 50ms\n    launch: {{tier: fast, cwd: {workspace}}}\n"
    )
    validate_expansion(
        valid,
        limits=limits,
        child_graph_id="wf-child",
        deadline_ms=200,
        at=100,
        name="parent",
        escalate_to=None,
    )
    for cwd in (outside / "x", allowed / ".." / "outside" / "x"):
        with pytest.raises(PacError):
            validate_expansion(
                child_text(
                    f"  - id: review\n    role: review\n    task: review\n    timeout: 50ms\n    launch: {{tier: fast, cwd: {cwd}}}\n"
                ),
                limits=limits,
                child_graph_id="wf-child",
                deadline_ms=200,
                at=100,
                name="parent",
                escalate_to=None,
            )


def test_parent_requires_one_planner_binding_without_endpoint_back_edges(tmp_path):
    loaded = policy(tmp_path)
    with pytest.raises(PacError) as caught:
        validate_parent(
            load_workflow_text(
                parent_text(
                    extra="""edges:
  - {from: join, to: planner, kind: back}
""",
                    include_end=True,
                )
            ),
            policy=loaded,
        )
    assert caught.value.code == PAC_EXPANSION_INVALID


@pytest.mark.parametrize("node_id", ["a..b", "x.lock"])
def test_child_node_id_rejects_invalid_git_branch_components(tmp_path, node_id):
    limits = effective_limits(None, policy=policy(tmp_path))
    with pytest.raises(PacError) as caught:
        validate_expansion(
            child_text(
                f"  - id: {node_id}\n    task: invalid branch component\n"
            ),
            limits=limits,
            child_graph_id="wf-child",
            deadline_ms=10_000,
            at=1,
            name="child",
            escalate_to=None,
        )
    assert caught.value.code == PAC_EXPANSION_INVALID
    assert caught.value.data == {"field": "nodes[0].id", "reason": "syntax"}
