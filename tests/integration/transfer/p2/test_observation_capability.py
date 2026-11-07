"""The native runtime observation is a restore-only capability, not a switch.

Owner conditions encoded here:

1. the cached refresh command has no boolean native-read opt-in;
2. the runtime owner validates grant provenance, not non-emptiness;
3. an unauthorized caller is rejected with a distinct code and reads nothing;
4. the dedicated command is publish-only: it can never persist session refs.

Plus the convergence ordering the owner asked for: the ``ProcessOwner`` settles
its own facts before the restore path reads them, so there is no second
liveness authority.
"""
from __future__ import annotations

import threading

import pytest

from hyprial.daemon.impl.composition import (
    CorrelatedDomainEvents,
    DomainCommandError,
    HarnessPortClient,
)
from hyprial.daemon.impl.harnesses.actor.actor import HarnessRuntimeActor
from hyprial.daemon.impl.harnesses.runtime.observation import (
    RuntimeFactsAuthority,
    RuntimeFactsGrant,
)
from hyprial.daemon.impl.harnesses.runtime.ports import (
    HarnessProjectionsRefreshed,
    ObserveHarnessRuntimeFactsCommand,
    RefreshHarnessProjectionsCommand,
)
from hyprial.daemon.impl.processes.process_owner import ProcessOwner
from hyprial.kernel import HarnessLaunchSpec
from hyprial.kernel import ipc_errors


class _NativeProcess:
    """Minimal native process face for a real ``ProcessOwner``."""

    def __init__(self, session_ref: str | None = None) -> None:
        self.running = True
        self.pid = None
        self.last_error = None
        self.session_ref = session_ref

    def stop(self) -> None:
        self.running = False


class _OwnerLauncher:
    def __init__(self, process: object) -> None:
        self.process = process
        self.started: list[HarnessLaunchSpec] = []

    def start(self, spec: HarnessLaunchSpec) -> object:
        self.started.append(spec)
        return self.process


def _actor_and_client(launcher: object) -> tuple[HarnessRuntimeActor, HarnessPortClient]:
    events = CorrelatedDomainEvents()
    actor = HarnessRuntimeActor(launcher, event_sink=events)  # type: ignore[arg-type]
    return actor, HarnessPortClient(actor, events)


def test_cached_refresh_command_exposes_no_native_switch() -> None:
    assert [field for field in RefreshHarnessProjectionsCommand.__dataclass_fields__] == [
        "correlation_id"
    ]
    # The dedicated observation command carries a grant and has no default, so
    # a caller cannot ask for a native read by omission either.
    fields = ObserveHarnessRuntimeFactsCommand.__dataclass_fields__
    assert set(fields) == {"correlation_id", "grant"}
    assert fields["grant"].default is not None or fields["grant"].default_factory is not None


@pytest.mark.parametrize(
    "grant",
    [
        None,
        True,
        "restore",
        RuntimeFactsGrant(purpose="restore", token="0" * 32),
        RuntimeFactsGrant(purpose="something-else", token="1" * 32),
    ],
)
def test_unminted_observation_is_rejected_with_distinct_code(
    grant: object, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: list[int] = []
    monkeypatch.setattr(
        ProcessOwner, "observe_facts",
        lambda self: reads.append(threading.get_ident()),  # pragma: no cover
    )
    _actor, harnesses = _actor_and_client(_OwnerLauncher(_NativeProcess()))
    with pytest.raises(DomainCommandError) as rejected:
        harnesses.call(
            ObserveHarnessRuntimeFactsCommand("unauthorized-observe", grant),  # type: ignore[arg-type]
            HarnessProjectionsRefreshed,
            timeout=5.0,
        )
    assert rejected.value.code == ipc_errors.HARNESS_RUNTIME_OBSERVATION_UNAUTHORIZED
    assert reads == [], "an unauthorized observation reached native facts"


def test_grant_from_a_foreign_authority_is_rejected() -> None:
    _actor, harnesses = _actor_and_client(_OwnerLauncher(_NativeProcess()))
    foreign = RuntimeFactsAuthority().mint(purpose="restore")
    with pytest.raises(DomainCommandError) as rejected:
        harnesses.call(
            ObserveHarnessRuntimeFactsCommand("foreign-observe", foreign),
            HarnessProjectionsRefreshed,
            timeout=5.0,
        )
    assert rejected.value.code == ipc_errors.HARNESS_RUNTIME_OBSERVATION_UNAUTHORIZED


def test_restore_grant_publishes_fresh_facts_and_never_persists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = _NativeProcess(session_ref="stale-ref")
    owner = ProcessOwner(native)
    actor, harnesses = _actor_and_client(_OwnerLauncher(owner))
    try:
        harnesses.restore((HarnessLaunchSpec("pi", "worker", True),))
        native.session_ref = "fresh-native-ref"
        # Publish-only is structural: the dedicated command has no persistence
        # branch, so the durable writer must never be reached.
        monkeypatch.setattr(
            actor, "_complete_session_ref_reconcile",
            lambda *_: pytest.fail("native observation persisted session refs"),
        )
        observed_threads: list[int] = []
        original = ProcessOwner.observe_facts

        def observe(process_owner: ProcessOwner) -> object:
            observed_threads.append(threading.get_ident())
            return original(process_owner)

        monkeypatch.setattr(ProcessOwner, "observe_facts", observe)
        harnesses.observe_runtime_facts()
        assert observed_threads and threading.get_ident() not in observed_threads
        refs = actor.read_session_refs().refs
        assert {(item.harness, item.name): item.session_ref for item in refs} == {
            ("pi", "worker"): "fresh-native-ref"
        }
    finally:
        owner.detach(1.0)


def test_owner_settles_its_own_facts_before_the_restore_path_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owner convergence first; the restore path only reads the settled facts."""

    native = _NativeProcess(session_ref="yesterday-ref")
    owner = ProcessOwner(native)
    read_threads: list[int] = []
    original = ProcessOwner._read_facts

    def read_facts(process_owner: ProcessOwner) -> object:
        read_threads.append(threading.get_ident())
        return original(process_owner)

    monkeypatch.setattr(ProcessOwner, "_read_facts", read_facts)
    try:
        assert owner.facts().session_ref == "yesterday-ref"
        native.session_ref = "fresh-native-ref"
        observed = owner.observe_facts()
        assert observed.session_ref == "fresh-native-ref"
        # The owner's own cached projection is already converged when the
        # observation is delivered, so no caller can read a stale cache and no
        # second liveness authority exists.
        assert owner.facts().session_ref == "fresh-native-ref"
        assert read_threads and read_threads[-1] != threading.get_ident()
    finally:
        owner.detach(1.0)
