"""The real ``TargetHost`` adapter against a real ``AgentRegistry`` in a temp home.

Contract pinned here (module ``hyprial.daemon.impl.teleport.wiring.target``):

- ``RegistryTargetHost(*, node_id, owner, registry, hyprial_home,
  runtime_available)`` implements ``TargetHost``.  Further keyword-only
  collaborators (lifecycle manager, harness port, ...) needed by ``start`` and
  ``stop`` must default to ``None`` so the adapter can be built without a
  daemon; ``start``/``stop`` are not exercised here because no harness binary
  exists on the development machine (protocol §8: real start + strict resume
  on a freshly landed row stays UNKNOWN until a real daemon run).
- ``land`` lands through ``land_bundle_into_registry`` with an unsigned-bundle
  policy (protocol §0), ``home_root = hyprial_home / "agents"``, a fresh
  incarnation and ``cwd = <home>/workspace``.  A taken name is
  ``TELEPORT_AGENT_EXISTS``; every other landing refusal (owner mismatch,
  invalid bundle) is ``TELEPORT_LANDING_FAILED`` and leaves no row.
- ``undo_landing`` settles the incarnation away (name and home are gone).
- ``activate`` / ``deactivate`` use ``RuntimeContextActivation`` for the
  landed harness.
- ``recipient_public_key`` is the public half of
  ``ensure_recipient_key(recipient_key_path(hyprial_home))``.
- ``runtime_supported(h)`` is ``h in {"claude", "codex", "pi"} and
  runtime_available(h)``.
"""

from __future__ import annotations

import importlib
import json
import stat
from pathlib import Path

import pytest

from hyprial.daemon.impl.org.network.member_agents.exclusions import (
    TRANSFER_EXCLUSIONS_RELPATH,
    OrgAgentExclusion,
    OrgAgentExclusionStore,
)
from hyprial.daemon.impl.transfer.archive.bundle import export_bundle
from hyprial.daemon.impl.transfer.archive.envelope import (
    ensure_recipient_key,
    recipient_key_path,
)
from hyprial.identity import agent_id_for_entity_token
from hyprial.identity.impl.agents.registry._core import AgentRegistry

from ..stream.samples import make_home
from ..support import teleport

WIRING = "hyprial.daemon.impl.teleport.wiring.target"
OWNER = "owner-1"
NODE = "node-b"
AGENT = "worker"
HOME_FILES = (
    ("state/agent.json", b"{}\n", 0o600),
    ("config/native/claude/settings.json", b"{}\n", 0o600),
    ("workspace/notes.txt", b"carried across\n", 0o644),
)


def wiring():
    return importlib.import_module(WIRING)


def _bundle(tmp_path: Path, *, owner: str = OWNER, name: str = "bundle") -> Path:
    source = make_home(tmp_path / f"{name}-source", HOME_FILES)
    bundle = tmp_path / name
    export_bundle(
        source,
        bundle,
        owner=owner,
        actor=AGENT,
        source_machine="node-a",
        created_at="2026-11-12T00:00:00Z",
    )
    return bundle


_OPENED: list[AgentRegistry] = []


@pytest.fixture(autouse=True)
def _close_registries():
    yield
    while _OPENED:
        _OPENED.pop().close()


def _host(tmp_path: Path, *, available=lambda _harness: True):
    home = tmp_path / "target-home"
    home.mkdir()
    registry = AgentRegistry(
        home / "agents.sqlite3", owner=OWNER, machine=NODE, hyprial_home=home
    )
    _OPENED.append(registry)
    host = wiring().RegistryTargetHost(
        node_id=NODE,
        owner=OWNER,
        registry=registry,
        hyprial_home=home,
        runtime_available=available,
    )
    return host, registry, home


def _code(callable_, *args, **kwargs) -> str:
    with pytest.raises(teleport().TeleportError) as refused:
        callable_(*args, **kwargs)
    return refused.value.code


def test_identity_fields(tmp_path: Path) -> None:
    host, _registry, _home = _host(tmp_path)
    assert host.node_id == NODE
    assert host.owner == OWNER


def test_land_creates_a_fresh_incarnation_with_workspace_cwd(tmp_path: Path) -> None:
    host, registry, home = _host(tmp_path)
    assert host.agent_exists(AGENT) is False
    landed = host.land(_bundle(tmp_path), name=AGENT, harness="claude")
    row = registry.require(AGENT)
    assert host.agent_exists(AGENT) is True
    assert landed.name == AGENT
    assert landed.harness == "claude"
    assert landed.uri == row.uri
    assert landed.entity_token == row.entity_token
    assert landed.home == home / "agents" / AGENT
    assert landed.cwd == landed.home / "workspace"
    assert Path(row.cwd) == landed.cwd
    assert (landed.cwd / "notes.txt").read_bytes() == b"carried across\n"


def test_taken_name_is_agent_exists(tmp_path: Path) -> None:
    host, registry, _home = _host(tmp_path)
    first = host.land(_bundle(tmp_path), name=AGENT, harness="claude")
    code = _code(host.land, _bundle(tmp_path, name="again"), name=AGENT, harness="claude")
    assert code == "TELEPORT_AGENT_EXISTS"
    assert registry.require(AGENT).entity_token == first.entity_token


def test_other_landing_refusal_is_landing_failed(tmp_path: Path) -> None:
    host, registry, home = _host(tmp_path)
    bundle = _bundle(tmp_path, owner="owner-other")
    assert _code(host.land, bundle, name=AGENT, harness="claude") == "TELEPORT_LANDING_FAILED"
    assert registry.exists(AGENT) is False
    assert not (home / "agents" / AGENT).exists()


def test_undo_landing_removes_row_and_home(tmp_path: Path) -> None:
    host, registry, _home = _host(tmp_path)
    landed = host.land(_bundle(tmp_path), name=AGENT, harness="claude")
    host.undo_landing(landed)
    assert host.agent_exists(AGENT) is False
    assert registry.exists(AGENT) is False
    assert not landed.home.exists()


def test_activate_and_deactivate_a_landed_home(tmp_path: Path) -> None:
    host, registry, _home = _host(tmp_path)
    landed = host.land(_bundle(tmp_path), name=AGENT, harness="claude")
    host.activate(landed)
    host.deactivate(landed)
    assert registry.require(AGENT).entity_token == landed.entity_token
    host.undo_landing(landed)
    assert host.agent_exists(AGENT) is False


def test_recipient_public_key_is_this_homes_key(tmp_path: Path) -> None:
    host, _registry, home = _host(tmp_path)
    first = host.recipient_public_key()
    assert isinstance(first, bytes)
    assert len(first) == 32
    assert host.recipient_public_key() == first
    assert ensure_recipient_key(recipient_key_path(home))[1] == first


def test_runtime_supported_needs_a_declared_and_available_harness(tmp_path: Path) -> None:
    available = {"codex"}
    host, _registry, _home = _host(tmp_path, available=lambda harness: harness in available)
    assert host.runtime_supported("codex") is True
    assert host.runtime_supported("claude") is False
    available.add("lark")
    assert host.runtime_supported("lark") is False


def test_standalone_host_lands_carried_exclusions_with_absent_state_and_undo_cleans_them(
    tmp_path: Path,
) -> None:
    home = tmp_path / "standalone-home"
    home.mkdir(mode=0o700)
    assert not (home / "state").exists()
    registry = AgentRegistry(
        home / "agents.sqlite3", owner=OWNER, machine=NODE, hyprial_home=home
    )
    _OPENED.append(registry)
    host = wiring().RegistryTargetHost(
        node_id=NODE,
        owner=OWNER,
        registry=registry,
        hyprial_home=home,
        runtime_available=lambda _harness: True,
    )
    source_agent_id = "a." + "c" * 26
    carried_bytes = json.dumps(
        [
            {"agentId": source_agent_id, "directorySpace": "space-111"},
            {"agentId": source_agent_id, "directorySpace": "space-222"},
        ]
    ).encode("utf-8")
    source = make_home(
        tmp_path / "carried-source",
        (*HOME_FILES, (TRANSFER_EXCLUSIONS_RELPATH, carried_bytes, 0o600)),
    )
    bundle = tmp_path / "carried-bundle"
    export_bundle(
        source,
        bundle,
        owner=OWNER,
        actor=AGENT,
        source_machine="node-a",
        created_at="2026-11-12T00:00:00Z",
    )

    landed = host.land(bundle, name=AGENT, harness="claude")
    assert stat.S_IMODE((home / "state").stat().st_mode) == 0o700
    assert stat.S_IMODE((home / "state" / "teleport").stat().st_mode) == 0o700
    assert stat.S_IMODE((home / "state" / "teleport" / "landing").stat().st_mode) == 0o700

    landed_agent_id = agent_id_for_entity_token(landed.entity_token)
    store = OrgAgentExclusionStore(home)
    assert store.excluded_spaces(landed_agent_id) == frozenset(
        {"space-111", "space-222"}
    )

    other_agent_id = "a." + "f" * 26
    store.exclude(other_agent_id, "space-keep")

    host.undo_landing(landed)
    assert registry.exists(AGENT) is False
    assert not landed.home.exists()
    assert store.excluded_spaces(landed_agent_id) == frozenset()
    assert store.list() == (OrgAgentExclusion(other_agent_id, "space-keep"),)
