"""Desired-state projection reads copy only the requested boundary."""

from pathlib import Path

from hyprial.daemon.impl.desired_state import DesiredState, DesiredStateStore
from hyprial.daemon.impl.state_persistence import StatePersistenceAuthority
from hyprial.kernel import HarnessLaunchSpec


def _authority(tmp_path: Path) -> StatePersistenceAuthority:
    return StatePersistenceAuthority(DesiredStateStore(tmp_path / "desired.json"))


def test_desired_load_has_one_full_copy_and_keeps_mutation_isolated(
    tmp_path: Path, monkeypatch
) -> None:
    authority = _authority(tmp_path)
    try:
        authority.desired.upsert_harness(
            HarnessLaunchSpec(
                harness="pi", name="worker", headless=True, nickname="original"
            )
        )
        from_json_calls = 0
        real_from_json = DesiredState.from_json

        def counting_from_json(cls: type[DesiredState], value: object) -> DesiredState:
            nonlocal from_json_calls
            from_json_calls += 1
            return real_from_json(value)

        monkeypatch.setattr(
            DesiredState, "from_json", classmethod(counting_from_json)
        )
        first = authority.desired.load()

        assert from_json_calls == 1
        object.__setattr__(first.harnesses[0], "nickname", "caller-mutated")
        assert authority.desired.load().harnesses[0].nickname == "original"
    finally:
        authority.close()


def test_single_harness_read_isolated_from_caller_mutation(tmp_path: Path) -> None:
    authority = _authority(tmp_path)
    try:
        authority.desired.upsert_harness(
            HarnessLaunchSpec(
                harness="pi", name="worker", headless=True, nickname="original"
            )
        )

        first = authority.desired.harness_spec("worker")
        assert first is not None
        object.__setattr__(first, "nickname", "caller-mutated")

        second = authority.desired.harness_spec("worker")
        assert second is not None
        assert second.nickname == "original"
        assert authority.desired.harness_spec("missing") is None
    finally:
        authority.close()


def test_actor_spec_skips_a_same_named_lark_row_while_searching(tmp_path: Path) -> None:
    """Review 930: (lark, foo) sorts before (pi, foo); the worker must win."""

    from types import SimpleNamespace

    from hyprial.daemon.impl.pac.actors.daemon import DaemonActorRuntime

    authority = _authority(tmp_path)
    try:
        authority.desired.set_adapter("foo", True)
        authority.desired.upsert_harness(
            HarnessLaunchSpec(harness="pi", name="foo", headless=True)
        )
        assert [h.harness for h in authority.desired.load().harnesses] == ["pi"]
        assert [a.provider for a in authority.desired.load().adapters] == ["lark"]

        runtime = DaemonActorRuntime(SimpleNamespace(desired_state=authority.desired))
        spec = runtime._spec("foo")
        assert spec is not None and spec.harness == "pi"

        excluded = authority.desired.harness_spec("foo", excluding="lark")
        assert excluded is not None and excluded.harness == "pi"
        assert authority.desired.harness_spec("foo").harness == "pi"
    finally:
        authority.close()

    store = DesiredStateStore(tmp_path / "store" / "desired-state.json")
    store.set_adapter("foo", True)
    store.upsert_harness(HarnessLaunchSpec(harness="pi", name="foo", headless=True))
    found = store.harness_spec("foo", excluding="lark")
    assert found is not None and found.harness == "pi"
