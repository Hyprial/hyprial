"""F-a regression: a landed row recovers the source's launch wiring.

The P2 chain's transfer unit is the agent home, and the home does not carry the
source registry row: ``provider``/``model``/``preferredHarness`` live only in
the source daemon's SQLite.  Before this fix a landed row therefore read
``provider=None/model=None`` and a dispatched message could not resolve a
launch.  The source now stamps ``identity.launch`` into the sealed manifest and
the landing authority backfills it.

The negative arm is explicit: a bundle exported without that field (every
bundle before this slice) lands exactly as it did -- all three remain ``None``
and nothing is invented.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.identity.impl.agents.registry._core import AgentRegistry
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.transfer.agent_home_chain import _launch_wiring_identity
from hyprial.daemon.impl.transfer.archive.bundle import export_bundle
from hyprial.daemon.impl.transfer.landing.owner import LandingError
from hyprial.daemon.impl.transfer.landing.registry import land_bundle_into_registry
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from tests.support.transfer.rehearsal import UNSIGNED_REHEARSAL
from tests.support.transfer.signed import sign_bundle_for_target

OWNER = "alice"
MACHINE = "server"
ACTOR = "wk"
CREATED_AT = "2026-09-30T23:30:00+08:00"


def _source_home(tmp_path: Path) -> Path:
    home = tmp_path / "source"
    for name in ("state", "config", "secrets", "workspace"):
        (home / name).mkdir(parents=True)
    (home / "state" / "home-receipt.json").write_text(
        json.dumps({"actor": ACTOR, "path": "/laptop/agents/wk", "resource": "src"})
    )
    (home / "config" / "agent.json").write_text('{"model": "x"}\n')
    return home


def _bundle(tmp_path: Path, home: Path, *, identity: dict | None = None) -> Path:
    bundle_dir = tmp_path / "bundle"
    export_bundle(
        home,
        bundle_dir,
        owner=OWNER,
        actor=ACTOR,
        source_machine="laptop",
        created_at=CREATED_AT,
        identity=identity,
    )
    return bundle_dir


def _registry(tmp_path: Path) -> AgentRegistry:
    return AgentRegistry(
        tmp_path / "agents.sqlite3", owner=OWNER, machine=MACHINE, hyprial_home=tmp_path
    )


def _land(tmp_path: Path, bundle_dir: Path, registry: AgentRegistry, **overrides):
    arguments: dict[str, object] = {
        "registry": registry,
        "home_root": tmp_path / "agents",
        "staging": tmp_path / "staging" / "landing-1",
        "granted_by": f"user:{OWNER}",
        "signature": UNSIGNED_REHEARSAL,
    }
    arguments.update(overrides)
    return land_bundle_into_registry(bundle_dir, **arguments)


def test_landing_backfills_the_bundled_launch_wiring(tmp_path: Path) -> None:
    home = _source_home(tmp_path)
    bundle_dir = _bundle(
        tmp_path,
        home,
        identity={
            "launch": {
                "provider": "deepseek",
                "model": "deepseek-flash",
                "harness": "pi",
            }
        },
    )
    registry = _registry(tmp_path)
    try:
        _land(tmp_path, bundle_dir, registry, harness="pi")

        landed = registry.require(ACTOR)
        assert landed.provider == "deepseek"
        assert landed.model == "deepseek-flash"
        assert landed.preferred_harness == "pi"
    finally:
        registry.close()


def test_a_bundle_without_launch_wiring_lands_exactly_as_before(tmp_path: Path) -> None:
    """The explicit negative arm: the pre-fix bundle shape stays null."""

    home = _source_home(tmp_path)
    bundle_dir = _bundle(tmp_path, home, identity=None)
    registry = _registry(tmp_path)
    try:
        result = _land(tmp_path, bundle_dir, registry, harness="pi")

        assert result.actor == ACTOR
        landed = registry.require(ACTOR)
        assert landed.provider is None
        assert landed.model is None
        assert landed.preferred_harness is None
    finally:
        registry.close()


def test_source_row_launch_wiring_rides_the_identity_bag() -> None:
    """The source side stamps the row fields a landed row cannot reconstruct."""

    assert _launch_wiring_identity(
        {
            "provider": "deepseek",
            "model": "deepseek-flash",
            "preferredHarness": "pi",
        },
        "pi",
    ) == {
        "launch": {"provider": "deepseek", "model": "deepseek-flash", "harness": "pi"}
    }


def test_source_harness_is_the_chain_harness_not_the_rows_preferred() -> None:
    """B1: a swapped agent (preferred != last) must not be refused on landing.

    The row says preferred ``pi`` and last ``codex``; transferring the current
    ``codex`` must stamp ``codex``, or the landing gate would reject a bundle
    the operator legitimately asked to move.
    """

    assert _launch_wiring_identity(
        {
            "provider": "openai",
            "model": "gpt-5.6-sol",
            "preferredHarness": "pi",
            "lastHarness": "codex",
        },
        "codex",
    ) == {
        "launch": {"provider": "openai", "model": "gpt-5.6-sol", "harness": "codex"}
    }


def test_source_row_without_wiring_produces_no_identity_keys() -> None:
    """A source row that never had wiring yields the pre-fix bundle shape."""

    assert _launch_wiring_identity({"provider": None, "model": None}, "pi") == {}
    assert _launch_wiring_identity(None, "pi") == {}


def test_the_landed_row_is_resolved_into_a_launch_without_explicit_flags(
    tmp_path: Path,
) -> None:
    """B2: the backfilled wiring is what the daemon's launch path consumes.

    Landing itself starts nothing.  This drives the next step the operator took
    in the first-hop run -- a start that passes no ``--provider``/``--model`` --
    and proves the row's backfilled values are the ones the daemon resolves,
    via the real ``_ensure_agent`` get-or-create seam (``agents_registry.py``).
    """

    home = _source_home(tmp_path)
    bundle_dir = tmp_path / "bundle"
    export_bundle(
        home,
        bundle_dir,
        owner=OWNER,
        actor=ACTOR,
        source_machine="laptop",
        created_at=CREATED_AT,
        identity={
            "launch": {
                "provider": "deepseek",
                "model": "deepseek-flash",
                "harness": "pi",
            }
        },
    )
    sign_bundle_for_target(
        bundle_dir, actor=ACTOR, source_machine="laptop", target_home=tmp_path
    )
    app = DaemonApplication(
        state_dir=tmp_path,
        socket_path=tmp_path / "daemon.sock",
        node_id=MACHINE,
        owner=OWNER,
        **_deep_module_daemon_dependencies(),
    )
    app._inbox = object()  # type: ignore[assignment]
    app._presence = object()  # type: ignore[assignment]
    app._harnesses = object()  # type: ignore[assignment]
    try:
        assert app.handle("transfer.land", {"bundleDir": str(bundle_dir)})["ok"] is True

        agent = app._ensure_agent(ACTOR, harness="pi", interactive=False)

        assert agent is not None
        assert (agent.provider, agent.model, agent.preferred_harness) == (
            "deepseek",
            "deepseek-flash",
            "pi",
        )
        assert (agent.capabilities.get("provider"), agent.capabilities.get("model")) == (
            "deepseek",
            "deepseek-flash",
        )
    finally:
        app.agents.close()


def test_wiring_for_another_harness_is_refused_before_any_write(tmp_path: Path) -> None:
    home = _source_home(tmp_path)
    bundle_dir = _bundle(
        tmp_path,
        home,
        identity={
            "launch": {
                "provider": "deepseek",
                "model": "deepseek-flash",
                "harness": "codex",
            }
        },
    )
    registry = _registry(tmp_path)
    try:
        with pytest.raises(LandingError):
            _land(tmp_path, bundle_dir, registry, harness="pi")

        assert registry.exists(ACTOR) is False
        assert not (tmp_path / "agents" / ACTOR).exists()
    finally:
        registry.close()
