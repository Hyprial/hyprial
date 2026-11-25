"""S3c: probing records into runtime-capabilities.json; no users.json exists anywhere."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.daemon.impl.dispatch import matrix
from hyprial.daemon.impl.harnesses import probe as probe_module
from hyprial.daemon.impl.harnesses.probe import ProbeRun
from hyprial.identity import RuntimeCapability, RuntimeCapabilityStore
from hyprial.kernel import TIERS

OWNER = "alicesmith"
RECEIPT_KEYS = {
    "ok", "probe", "schemaVersion", "ownerKey", "dryRun",
    "changed", "results", "capabilities",
}
CLAUDE = ("claude", None, "sonnet")
PI = ("pi", "kimi-coding", "k3")


@pytest.fixture()
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "state"
    directory.mkdir()
    monkeypatch.setenv("HARNESS_STATE_DIR", str(directory))
    monkeypatch.setenv("HYPRIAL_OWNER", OWNER)
    return directory


@pytest.fixture()
def runs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    def runner(command: tuple[str, ...], timeout: float) -> ProbeRun:
        calls.append(command)
        if "k3" in command:
            return ProbeRun(1, "You're out of extra usage")
        return ProbeRun(0, "hello\n")

    monkeypatch.setattr(probe_module, "_subprocess_runner", runner)
    return calls


def cache(state: Path) -> RuntimeCapabilityStore:
    return RuntimeCapabilityStore(state / "runtime-capabilities.json")


def probe(state: Path, owner: str | None, combos, **kwargs):
    return matrix.probe_capabilities(
        state, owner, tuple(combos), timeout=5.0, dry_run=kwargs.pop("dry_run", False)
    )


def test_receipt_has_exactly_the_spec_keys_and_no_profile(state, runs) -> None:
    assert not (state / "users.json").exists()

    receipt = probe(state, OWNER, [CLAUDE, PI])

    assert set(receipt) == RECEIPT_KEYS
    assert receipt["ok"] is True
    assert receipt["probe"] == "user-runtime-capability"
    assert receipt["schemaVersion"] == 1
    assert receipt["ownerKey"] == OWNER
    assert receipt["dryRun"] is False
    assert receipt["changed"] == ["runtimeCapabilities", "runtimeCapabilities"]
    assert [row["model"] for row in receipt["results"]] == ["sonnet", "k3"]
    assert [row["status"] for row in receipt["capabilities"]] == [
        "available", "unavailable",
    ]
    assert "profile" not in receipt
    assert "deliveryAgentLive" not in receipt
    assert "squire" not in json.dumps(receipt)
    assert not (state / "users.json").exists()
    assert len(runs) == 2


def test_receipt_capabilities_are_what_the_cache_holds(state, runs) -> None:
    receipt = probe(state, OWNER, [CLAUDE, PI])

    stored = [item.to_json() for item in cache(state).list(OWNER)]
    assert receipt["capabilities"] == stored
    assert cache(state).get(OWNER, "pi", "k3", "kimi-coding").reason == "usage-limit"


def test_dry_run_runs_probes_but_writes_nothing(state, runs) -> None:
    receipt = probe(state, OWNER, [CLAUDE], dry_run=True)

    assert receipt["dryRun"] is True
    assert receipt["changed"] == []
    assert len(receipt["results"]) == 1
    assert len(runs) == 1
    assert not (state / "runtime-capabilities.json").exists()
    assert not (state / "users.json").exists()


def test_reprobe_without_combo_reprobes_the_recorded_combos(state, runs) -> None:
    probe(state, OWNER, [CLAUDE, PI])
    runs.clear()

    receipt = probe(state, OWNER, [])

    assert len(runs) == 2
    assert {(row["harness"], row["model"]) for row in receipt["results"]} == {
        ("claude", "sonnet"), ("pi", "k3"),
    }
    assert receipt["probe"] == "user-runtime-capability"


def test_nothing_recorded_and_no_combo_is_a_value_error(state, runs) -> None:
    with pytest.raises(ValueError, match="pass --combo harness:provider:model"):
        probe(state, OWNER, [])

    assert runs == []
    assert not (state / "runtime-capabilities.json").exists()


def test_owner_key_none_means_the_node_owner(state, runs) -> None:
    receipt = probe(state, None, [CLAUDE])

    assert receipt["ownerKey"] == OWNER
    assert cache(state).get(OWNER, "claude", "sonnet") is not None


def test_corrupt_cache_fails_loud_instead_of_probing_over_it(state, runs) -> None:
    (state / "runtime-capabilities.json").write_text("{broken", encoding="utf-8")

    with pytest.raises(ValueError):
        probe(state, OWNER, [CLAUDE])

    assert (state / "runtime-capabilities.json").read_text(encoding="utf-8") == "{broken"


def test_recorded_capabilities_reads_the_active_owners_cache(state) -> None:
    mine = RuntimeCapability(
        harness="claude", model="sonnet", status="available",
        probed_at="2026-11-24T00:00:00Z",
    )
    theirs = RuntimeCapability(
        harness="codex", model="gpt-x", status="available",
        probed_at="2026-11-24T00:00:00Z",
    )
    cache(state).set(OWNER, mine)
    cache(state).set("someone-else", theirs)

    assert matrix.recorded_capabilities() == (mine,)


def test_recorded_capabilities_is_empty_without_a_cache(state) -> None:
    assert matrix.recorded_capabilities() == ()


def test_recorded_candidate_json_reads_the_new_cache(state) -> None:
    candidate = TIERS["fast"][0]
    capability = RuntimeCapability(
        harness=candidate.harness, provider=candidate.provider,
        model=candidate.model, status="unavailable", reason="usage-limit",
        probed_at="2026-11-24T00:00:00Z",
    )

    marked = matrix.recorded_candidate_json(candidate, (capability,))
    unmarked = matrix.recorded_candidate_json(TIERS["fast"][1], (capability,))

    assert marked["availability"] == capability.to_json()
    assert unmarked["availability"] is None


def test_tier_report_shows_recorded_availability_without_probing(
    state, monkeypatch
) -> None:
    from hyprial.shell.impl.cli.commands.workflow.internal.dispatch import tier_report

    selected = TIERS["super"][0]
    cache(state).set(OWNER, RuntimeCapability(
        harness=selected.harness, provider=selected.provider, model=selected.model,
        status="unavailable", reason="usage-limit", probed_at="2026-11-24T00:00:00Z",
    ))
    monkeypatch.setattr(
        probe_module, "_subprocess_runner",
        lambda *_: pytest.fail("tier_report without probe must not run a probe"),
    )

    report = tier_report("super")

    assert report["tiers"]["super"][0]["availability"]["reason"] == "usage-limit"
    assert report["tiers"]["super"][1]["availability"] is None
    assert not (state / "users.json").exists()


def test_the_profile_helpers_are_gone() -> None:
    assert not hasattr(matrix, "active_profile")
    assert not hasattr(matrix, "probe_profile")


def test_tier_view_on_a_home_without_an_owner_reads_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / "state").mkdir(parents=True)
    monkeypatch.setenv("HARNESS_STATE_DIR", str(home / "state"))
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)

    assert matrix.recorded_capabilities() == ()
    candidate = TIERS[next(iter(TIERS))][0]
    assert matrix.recorded_candidate_json(candidate)["availability"] is None
