from pathlib import Path

import pytest
import yaml

from hyprial.daemon.impl.pac.contracts.workflow import WorkflowSchemaError, load_workflow_text


def document(**changes):
    return yaml.safe_dump(
        {
            "version": 2,
            "name": "work",
            "nodes": [{"id": "build", "task": "implement"}],
            **changes,
        }
    )


def test_defaults_are_explicit_in_the_plan_and_have_no_effects(tmp_path, monkeypatch):
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path))
    spec = load_workflow_text(document())
    plan = spec.to_json()
    assert plan["onFailure"] == "terminate"
    assert plan["nodes"][0]["ownership"] == "workflow"
    assert plan["nodes"][0]["worker"] == "build"
    assert plan["nodes"][0]["launch"]["tier"] == "fast"
    assert plan["nodes"][0]["timeoutMs"] == 3600000
    assert list(Path(tmp_path).iterdir()) == []


def test_independent_shared_and_borrowed_workers():
    spec = load_workflow_text(
        document(
            workers={"implementation": {"tier": "strong"}},
            nodes=[
                {"id": "build", "worker": "implementation", "task": "build"},
                {
                    "id": "fix",
                    "worker": "implementation",
                    "task": "fix",
                    "after": ["build"],
                },
                {
                    "id": "review",
                    "owner": "agent:test:node:reviewer",
                    "task": "review",
                    "role": "review",
                    "after": ["fix"],
                },
            ],
            on_failure="hold",
        )
    )
    assert spec.nodes[0].launch == spec.nodes[1].launch
    assert spec.nodes[2].launch is None
    assert spec.nodes[2].worker is None
    assert spec.edges == (("build", "fix", "forward"), ("fix", "review", "forward"))
    assert spec.on_failure == "hold"


@pytest.mark.parametrize(
    "change",
    [
        {"version": 1},
        {"on_failure": "retry"},
        {"await": {"match": "DONE"}},
        {"nodes": [{"id": "a", "task": "a"}, {"id": "a", "task": "b"}]},
        {"nodes": [{"id": "a", "task": "a", "owner": "unresolved"}]},
        {
            "nodes": [
                {
                    "id": "a",
                    "task": "a",
                    "owner": "user:test",
                    "launch": {"tier": "fast"},
                }
            ]
        },
        {"nodes": [{"id": "a", "task": "a", "worker": "missing"}]},
        {"nodes": [{"id": "a", "task": "a", "launch": {"tier": "fast", "model": "x"}}]},
        {"nodes": [{"id": "a", "task": "a", "kind": "approval"}]},
        {"nodes": [{"id": "a", "task": "a", "timeout": False}]},
        {"nodes": [{"id": "a", "task": "a", "after": ["missing"]}]},
        {
            "nodes": [
                {"id": "a", "task": "a", "after": ["b"]},
                {"id": "b", "task": "b", "after": ["a"]},
            ]
        },
    ],
)
def test_invalid_inputs_are_refused(change):
    with pytest.raises(WorkflowSchemaError):
        load_workflow_text(document(**change))


def test_duplicate_yaml_key_is_not_silently_overwritten():
    with pytest.raises(WorkflowSchemaError, match="duplicate"):
        load_workflow_text("version: 2\nname: one\nname: two\nnodes: []")


def test_explicit_rework_and_end_are_preserved():
    spec = load_workflow_text(
        document(
            nodes=[
                {"id": "build", "task": "build"},
                {
                    "id": "review",
                    "kind": "approval",
                    "owner": "user:test",
                    "task": "review",
                    "after": ["build"],
                },
                {
                    "id": "finish",
                    "kind": "end",
                    "owner": "user:test",
                    "task": "accept",
                    "after": ["review"],
                },
            ],
            edges=[{"from": "review", "to": "build", "kind": "back"}],
        )
    )
    assert ("review", "build", "back") in spec.edges


@pytest.mark.parametrize(
    "args", [["--model", "different"], ["--provider=other"], ["-c", "model=other"]]
)
def test_native_arguments_cannot_override_a_tier_selection(args):
    with pytest.raises(WorkflowSchemaError, match="cannot override"):
        load_workflow_text(
            document(defaults={"launch": {"tier": "fast", "args": args}})
        )


def test_a_retired_model_is_refused_at_plan_time_with_its_replacement():
    """A retired name used to surface only as a missing activation marker."""

    with pytest.raises(WorkflowSchemaError, match="retired; use 'deepseek-flash'"):
        load_workflow_text(
            document(
                defaults={
                    "launch": {
                        "harness": "pi",
                        "provider": "deepseek",
                        "model": "deepseek-v4-flash",
                    }
                }
            )
        )


def test_the_canonical_model_is_accepted_at_plan_time():
    spec = load_workflow_text(
        document(
            defaults={
                "launch": {"harness": "pi", "provider": "deepseek", "model": "deepseek-flash"}
            }
        )
    )
    assert spec.nodes[0].launch["model"] == "deepseek-flash"


def test_managed_worker_missing_required_cwd_is_rejected_with_path(tmp_path):
    missing = (tmp_path / "missing-worktree").resolve()
    with pytest.raises(WorkflowSchemaError, match=str(missing)):
        load_workflow_text(
            document(defaults={"launch": {"tier": "fast", "cwd": str(missing)}})
        )
    assert not missing.exists()


def test_managed_worker_explicit_create_allows_missing_cwd_without_plan_effects(
    tmp_path,
):
    missing = (tmp_path / "create-at-execution").resolve()
    spec = load_workflow_text(
        document(
            defaults={
                "launch": {
                    "tier": "fast",
                    "cwd": str(missing),
                    "cwdPolicy": "create",
                }
            }
        )
    )
    assert spec.nodes[0].launch["cwdPolicy"] == "create"
    assert not missing.exists(), "workflow plan must remain side-effect free"


def test_managed_worker_required_empty_cwd_is_accepted_and_unchanged(tmp_path):
    cwd = tmp_path / "empty-worktree"
    cwd.mkdir()
    spec = load_workflow_text(
        document(
            defaults={
                "launch": {
                    "tier": "fast",
                    "cwd": str(cwd),
                    "cwdPolicy": "require",
                }
            }
        )
    )
    assert spec.nodes[0].launch["cwdPolicy"] == "require"
    assert list(cwd.iterdir()) == []


def test_borrowed_actor_does_not_validate_dispatch_machine_cwd(tmp_path):
    remote_only = (tmp_path / "remote-machine-only").resolve()
    spec = load_workflow_text(
        document(
            defaults={
                "launch": {
                    "tier": "fast",
                    "cwd": str(remote_only),
                    "cwdPolicy": "require",
                }
            },
            nodes=[
                {
                    "id": "review",
                    "owner": "agent:test:remote:reviewer",
                    "task": "review",
                    "role": "review",
                }
            ],
        )
    )
    assert spec.nodes[0].launch is None
    assert not remote_only.exists()


@pytest.mark.parametrize("policy", ["", "mkdir", True])
def test_invalid_cwd_policy_is_rejected(policy):
    with pytest.raises(WorkflowSchemaError, match="cwdPolicy"):
        load_workflow_text(
            document(
                defaults={
                    "launch": {
                        "tier": "fast",
                        "cwd": "/tmp/worktree",
                        "cwdPolicy": policy,
                    }
                }
            )
        )
