"""ACL-space entry-gate acceptance over the daemon IPC surface.

Design: ``docs/design/design-org-orgfs-usage-for-socialware-2026-10-06.md``
§3.1 (owner 本人入口闸) and §8.3 (Allen's ruling: the ACL space admits the
owner's terminal and user-proxy by default, plus explicit ``org.acl.grant``
authorizations recorded in owner-node local state).  Interface frozen by the
dispatching node: ``org.acl.grant <org> <agent> [--until <moment>]`` /
``org.acl.revoke <org> <agent>`` / ``org.acl.ls <org>`` with error codes
``ORG_ACL_WRITE_DENIED``, ``ORG_ACL_GRANT_FORBIDDEN`` and
``ORG_ACL_GRANT_NOT_OWNED``.

Every endpoint here is the daemon's IPC surface — ``app.handle(method,
params, _trusted_message_origin="ipc")`` is exactly the call the socket
server makes (``ipc/server.py`` ``_response``) — never an internal helper.
The orgfs facade is read only to verify what a call wrote.

Red against base 381acfa10 (readings recorded with the dispatch):
- writes: the bridge still blanket-refuses every ACL-space mutation with
  ``ORGFS_ACL_SPACE_EXTERNAL_MUTATION``, for every caller including the
  operator, so ①②③⑥ cannot pass;
- grants: ``org.acl.*`` does not exist, so every grant/revoke/ls call dies
  with ``METHOD_NOT_FOUND``, so ④⑤⑦ cannot pass.

Identity model used here (the design's「已认证调用方」):
- the owner terminal is the operator identity ``user:<owner>``;
- a normal agent session is a fenced interactive registration
  (``agent.create`` + ``session.register``) or a daemon-managed worker
  (desired-state ``HarnessLaunchSpec`` + the harness supervisor's session
  projection — the only place the daemon itself registers a harness kind);
- the real user-proxy is therefore simulated the way production registers
  it: a desired-state harness spec with ``harness="user-proxy"``, and its
  worker session ref.  The liar in ② is an interactive registration that
  merely carries the NAME ``user-proxy`` — the daemon-registered kind stays
  ``claude``, which is exactly the「不认自报名」rule.

The ``--until`` deadline is itself the injected clock: absolute moments far
in the past and far in the future make the expiry comparison deterministic
without sleeping or patching a wall clock.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from tests.support.tailnet_daemon import membership_services
from hyprial.kernel import DaemonRequestError, HarnessLaunchSpec
from hyprial.shell.impl.daemon_wiring import (
    daemon_dependencies as _deep_module_daemon_dependencies,
)

ORG = "group-acme"
OWNER = "alice"
NODE = "node-a"
MEMBER = "bob"
MEMBER_NODE = "node-b"

# Error codes of the frozen contract.  Spelled as literals because base has
# no ``ipc_errors`` attributes for them yet (importing them would crash
# collection on the base this module must be red against).
WRITE_DENIED = "ORG_ACL_WRITE_DENIED"
GRANT_FORBIDDEN = "ORG_ACL_GRANT_FORBIDDEN"
GRANT_NOT_OWNED = "ORG_ACL_GRANT_NOT_OWNED"
# On a non-owner node the gate adds no second refusal code: the pre-existing
# daemon-owned-ACL-space ruling stays authoritative there (PR #1305), so a
# member replica's refusal carries the flat external-mutation code.
EXTERNAL_MUTATION = "ORGFS_ACL_SPACE_EXTERNAL_MUTATION"

# Absolute moments: the deadline parameter is the injected clock.  The
# daemon IPC freezes the deadline as epoch milliseconds (``untilMs``, PR
# #1305), so each moment is derived from its ISO spelling — the ms value
# on the wire is exactly that instant.
PAST_MOMENT = "2001-09-09T01:46:40+00:00"
PAST_MOMENT_MS = int(datetime.fromisoformat(PAST_MOMENT).timestamp() * 1000)
FUTURE_MOMENT = "2100-01-01T00:00:00+00:00"
FUTURE_MOMENT_MS = int(datetime.fromisoformat(FUTURE_MOMENT).timestamp() * 1000)

_OPERATOR = {"from": f"user:{OWNER}"}


class _FakeInbox:
    def pending_messages(self, _actor: str) -> tuple[()]:
        return ()


class _FakePresence:
    def online_actors(self) -> tuple[()]:
        return ()


class _FakeHarnesses:
    """The harness-supervisor projection seam (see tests/daemon/ipc/work)."""

    def __init__(self) -> None:
        self.refs: dict[tuple[str, str], str | None] = {}

    def projected_worker_session_refs(self) -> dict[tuple[str, str], str | None]:
        return dict(self.refs)

    def status(self) -> tuple[()]:
        return ()


def _ipc(app: DaemonApplication, method: str, params: dict[str, Any]) -> Any:
    return app.handle(method, params, _trusted_message_origin="ipc")


def _build_app(
    own, *, state: Path, home: Path, node_id: str, owner: str
) -> tuple[DaemonApplication, _FakeHarnesses]:
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    app = own(
        DaemonApplication(
            state_dir=state,
            socket_path=state / "daemon.sock",
            node_id=node_id,
            hyprial_home=home,
            owner=owner,
            **_deep_module_daemon_dependencies(),
        )
    )
    app._inbox = _FakeInbox()  # type: ignore[assignment]
    harnesses = _FakeHarnesses()
    app._harnesses = harnesses  # type: ignore[assignment]
    app._presence = _FakePresence()  # type: ignore[assignment]
    app._restore_done.set()
    return app, harnesses


def _interactive_agent(
    app: DaemonApplication, root: Path, name: str, session_ref: str
) -> dict[str, str]:
    """A fenced interactive registration: the ordinary agent session of ①."""

    cwd = root / "workspaces" / name
    cwd.mkdir(parents=True, exist_ok=True)
    _ipc(app, "agent.create", {"name": name})
    _ipc(
        app,
        "session.register",
        {
            "actor": name,
            "sessionRef": session_ref,
            "cwd": str(cwd),
            "command": ["hyprial-entry-gate-carrier"],
            "source": "claude-channel",
            "runtime": "claude_interactive",
        },
    )
    return {"from": name, "sessionRef": session_ref}


def _managed_worker(
    app: DaemonApplication,
    harnesses: _FakeHarnesses,
    *,
    harness_kind: str,
    name: str,
    session_ref: str,
) -> dict[str, str]:
    """A daemon-managed worker: the only registration the daemon itself makes."""

    app.desired_state.upsert_harness(
        HarnessLaunchSpec(
            harness=harness_kind, name=name, headless=True, session_ref=session_ref
        )
    )
    harnesses.refs[(harness_kind, name)] = session_ref
    return {"from": f"agent:{OWNER}:{NODE}:{name}", "sessionRef": session_ref}


def _acl_write(gate, caller: dict[str, str], *, node: str, text: str = "entry") -> Any:
    return _ipc(
        gate["app"],
        "orgfs.write",
        {**caller, "spaceId": gate["acl"], "node": node, "text": text},
    )


def _write_denied(gate, caller: dict[str, str], *, node: str) -> DaemonRequestError:
    with pytest.raises(DaemonRequestError) as denied:
        _acl_write(gate, caller, node=node)
    assert denied.value.code == WRITE_DENIED
    return denied.value


def _walk_space(fs, space_id: str) -> list[tuple[str, bytes]]:
    """Every live node's bytes in one space (verification reads only)."""

    collected: list[tuple[str, bytes]] = []

    def visit(prefix: str) -> None:
        for node in fs.listdir(space_id, prefix):
            if node.kind == "dir":
                visit(node.path)
            elif node.kind == "doc":
                text, _version = fs.read_text(space_id, node.path)
                collected.append((node.path, text.encode()))
            else:
                collected.append((node.path, fs.read_bytes(space_id, node.path)))

    visit("")
    return collected


@pytest.fixture
def local_membership():
    # Keep management/ACL gates real; substitute only the membership port.
    with membership_services() as sidecars:
        yield sidecars


@pytest.fixture
def gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, own_daemon_application, local_membership):
    """One owner node: the org exists and every caller identity is registered."""

    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    root = Path(os.path.realpath(tmp_path))
    monkeypatch.setenv("HYPRIAL_HOME", str(root / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(root / "state"))
    app, harnesses = _build_app(
        own_daemon_application,
        state=root / "state",
        home=root / "home",
        node_id=NODE,
        owner=OWNER,
    )
    created = _ipc(app, "org.create", {"org": ORG})
    gate = {
        "app": app,
        "root": root,
        "directory": created["spaceId"],
        "acl": created["aclSpaceId"],
        "deputy_uri": f"agent:{OWNER}:{NODE}:gate-deputy",
        "stale_uri": f"agent:{OWNER}:{NODE}:stale-deputy",
        "lasting_uri": f"agent:{OWNER}:{NODE}:lasting-deputy",
        "runner_uri": f"agent:{OWNER}:{NODE}:runner",
    }
    gate["deputy"] = _interactive_agent(app, root, "gate-deputy", "session-deputy")
    gate["stale"] = _interactive_agent(app, root, "stale-deputy", "session-stale")
    gate["lasting"] = _interactive_agent(app, root, "lasting-deputy", "session-lasting")
    # ②: the NAME says user-proxy; the daemon-registered kind says claude.
    gate["liar"] = _interactive_agent(app, root, "user-proxy", "session-liar")
    gate["runner"] = _managed_worker(
        app,
        harnesses,
        harness_kind="claude",
        name="runner",
        session_ref="runner-session",
    )
    # ③: the real user-proxy — registered by the daemon as a user-proxy harness.
    gate["proxy"] = _managed_worker(
        app,
        harnesses,
        harness_kind="user-proxy",
        name="proxy",
        session_ref="proxy-session",
    )
    return gate


# -- ① ordinary agent sessions are refused by the entry gate ----------------


def test_owner_node_agent_sessions_are_refused_by_the_entry_gate(gate) -> None:
    """① On the owner node an ordinary agent session — interactive or a
    daemon-managed worker of another harness kind — must not write the ACL
    space, whichever mutating orgfs method it uses.

    Red on base: both callers get ORGFS_ACL_SPACE_EXTERNAL_MUTATION instead
    of ORG_ACL_WRITE_DENIED.
    """

    callers = (gate["deputy"], gate["runner"])
    mutations = (
        ("orgfs.write", {"node": "deputy-attempt.md", "text": "no"}),
        ("orgfs.remove", {"node": "role-grants.yaml"}),
    )
    for caller in callers:
        for method, extra in mutations:
            with pytest.raises(DaemonRequestError) as denied:
                _ipc(
                    gate["app"],
                    method,
                    {**caller, "spaceId": gate["acl"], **extra},
                )
            assert denied.value.code == WRITE_DENIED, (method, caller["from"])


# -- ② a self-reported user-proxy name is not the registered kind ----------


def test_a_self_reported_user_proxy_name_does_not_open_the_gate(gate) -> None:
    """② An agent that merely NAMES itself ``user-proxy`` — while the
    daemon-registered harness kind for its session is ``claude`` — is an
    ordinary agent session and must be refused (「不认自报名」).

    Red on base: the refusal is the blanket ORGFS_ACL_SPACE_EXTERNAL_MUTATION,
    not the entry gate's own code.
    """

    _write_denied(gate, gate["liar"], node="liar-attempt.md")


# -- ③ the owner terminal and the real user-proxy session pass --------------


def test_owner_terminal_and_real_user_proxy_session_pass_the_gate(gate) -> None:
    """③ The two identities equivalent to the owner themself — the local
    operator terminal and the owner's user-proxy (identified by the
    daemon-registered harness kind) — must be let through.

    Red on base: both writes die with ORGFS_ACL_SPACE_EXTERNAL_MUTATION.
    """

    app = gate["app"]
    _acl_write(gate, _OPERATOR, node="terminal-note.md", text="from the terminal")
    _acl_write(gate, gate["proxy"], node="relay-note.md", text="from the user proxy")

    fs = app._require_orgfs_runtime().facade
    assert fs.read_text(gate["acl"], "terminal-note.md")[0] == "from the terminal"
    assert fs.read_text(gate["acl"], "relay-note.md")[0] == "from the user proxy"


# -- ④ grant / revoke / ls and --until expiry drive the gate ----------------


def test_grant_revoke_ls_and_until_expiry_drive_the_gate(gate) -> None:
    """④ An explicit authorization opens the gate for exactly that agent;
    revoke closes it; ``ls`` observes it; an already-expired ``--until``
    never opens it, a future one does.  No sleep: the deadline moments are
    absolute past/future instants, so the expiry comparison is deterministic.

    Red on base: ``org.acl.grant`` raises METHOD_NOT_FOUND.
    """

    app = gate["app"]
    granted = _ipc(
        app,
        "org.acl.grant",
        {**_OPERATOR, "org": ORG, "agent": gate["deputy_uri"]},
    )
    assert isinstance(granted, dict)
    _acl_write(gate, gate["deputy"], node="deputy-1.md")

    listing = json.dumps(_ipc(app, "org.acl.ls", {**_OPERATOR, "org": ORG}))
    assert "gate-deputy" in listing

    _ipc(app, "org.acl.revoke", {**_OPERATOR, "org": ORG, "agent": gate["deputy_uri"]})
    _write_denied(gate, gate["deputy"], node="deputy-2.md")
    assert "gate-deputy" not in json.dumps(
        _ipc(app, "org.acl.ls", {**_OPERATOR, "org": ORG})
    )

    # An already-past deadline must not open the gate.  Whether the daemon
    # stores or rejects a dead grant is its choice; either way no write.
    try:
        _ipc(
            app,
            "org.acl.grant",
            {
                **_OPERATOR,
                "org": ORG,
                "agent": gate["stale_uri"],
                "untilMs": PAST_MOMENT_MS,
            },
        )
    except DaemonRequestError:
        pass
    _write_denied(gate, gate["stale"], node="stale-1.md")

    _ipc(
        app,
        "org.acl.grant",
        {
            **_OPERATOR,
            "org": ORG,
            "agent": gate["lasting_uri"],
            "untilMs": FUTURE_MOMENT_MS,
        },
    )
    _acl_write(gate, gate["lasting"], node="lasting-1.md")


# -- ⑤ who may grant, and to whom -------------------------------------------


def test_agent_sessions_cannot_grant_or_revoke(gate) -> None:
    """⑤a grant/revoke are reserved for the owner terminal and the
    user-proxy; an ordinary agent session gets ORG_ACL_GRANT_FORBIDDEN.

    Red on base: METHOD_NOT_FOUND (the code never matches).
    """

    for method in ("org.acl.grant", "org.acl.revoke"):
        with pytest.raises(DaemonRequestError) as refused:
            _ipc(
                gate["app"],
                method,
                {**gate["deputy"], "org": ORG, "agent": gate["runner_uri"]},
            )
        assert refused.value.code == GRANT_FORBIDDEN, method


def test_granting_an_agent_owned_by_someone_else_is_refused(gate) -> None:
    """⑤b A grant may only target agents under the owner's own registration;
    an agent URI belonging to another owner gets ORG_ACL_GRANT_NOT_OWNED.

    Red on base: METHOD_NOT_FOUND.
    """

    with pytest.raises(DaemonRequestError) as refused:
        _ipc(
            gate["app"],
            "org.acl.grant",
            {
                **_OPERATOR,
                "org": ORG,
                "agent": f"agent:{MEMBER}:{MEMBER_NODE}:helper",
            },
        )
    assert refused.value.code == GRANT_NOT_OWNED


def test_the_user_proxy_session_may_grant(gate) -> None:
    """⑤c The other admitted grant executor: the owner's user-proxy session
    issues a grant and the grant takes effect.

    Red on base: METHOD_NOT_FOUND at the grant call.
    """

    app = gate["app"]
    _ipc(app, "org.acl.grant", {**gate["proxy"], "org": ORG, "agent": gate["deputy_uri"]})
    _acl_write(gate, gate["deputy"], node="deputy-by-proxy.md")


# -- ⑥ a non-owner node keeps refusing --------------------------------------


@pytest.fixture
def member_node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, own_daemon_application, local_membership):
    """An owner node that invites a member, then a member node holding a
    replica of the org's spaces (the orgfs storage is copied once the owner
    phase is closed; the member daemon reopens it as ``user:bob``).
    """

    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    root = Path(os.path.realpath(tmp_path))
    owner_state = root / "owner-state"
    owner_home = root / "owner-home"
    owner_app, _harnesses = _build_app(
        own_daemon_application,
        state=owner_state,
        home=owner_home,
        node_id=NODE,
        owner=OWNER,
    )
    # No finalizer on the owner app: the fixture closes it inline so the
    # orgfs journals are quiet before the copy below.
    try:
        created = _ipc(owner_app, "org.create", {"org": ORG})
        _ipc(owner_app, "org.invite", {"org": ORG, "user": f"user:{MEMBER}"})
    finally:
        owner_app._close()
        owner_app._close_home_lease()

    member_state = root / "member-state"
    member_state.mkdir(parents=True, mode=0o700)
    shutil.copytree(owner_state / "orgfs", member_state / "orgfs")
    app, _member_harnesses = _build_app(
        own_daemon_application,
        state=member_state,
        home=root / "member-home",
        node_id=MEMBER_NODE,
        owner=MEMBER,
    )
    monkeypatch.setenv("HYPRIAL_HOME", str(root / "member-home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(member_state))
    return {
        "app": app,
        "root": root,
        "acl": created["aclSpaceId"],
        "directory": created["spaceId"],
        "helper": _interactive_agent(app, root, "helper", "session-helper"),
    }


def test_the_entry_gate_keeps_refusing_on_a_non_owner_node(member_node) -> None:
    """⑥ The entry gate is an owner-node facility; on a member node nothing
    it added may open the ACL space — the member's session AND the member
    node's own terminal stay refused — while the member's ordinary rights
    (writing the directory space) are untouched.

    The refusal code is the flat external-mutation one the gate kept on
    non-owner nodes (its space-owner check short-circuits before the entry
    gate, PR #1305): the member replica never reaches a gate verdict at all.
    """

    app = member_node["app"]
    with pytest.raises(DaemonRequestError) as denied:
        _ipc(
            app,
            "orgfs.write",
            {
                **member_node["helper"],
                "spaceId": member_node["acl"],
                "node": "member-attempt.md",
                "text": "no",
            },
        )
    assert denied.value.code == EXTERNAL_MUTATION

    # The member node's terminal is not the ACL custodian either: the same
    # flat refusal, not the owner-node gate codes.
    with pytest.raises(DaemonRequestError) as terminal_denied:
        _ipc(
            app,
            "orgfs.write",
            {
                "from": f"user:{MEMBER}",
                "spaceId": member_node["acl"],
                "node": "member-terminal-attempt.md",
                "text": "no",
            },
        )
    assert terminal_denied.value.code == EXTERNAL_MUTATION

    # Unchanged behavior: an rw member still writes the DIRECTORY space.
    _ipc(
        app,
        "orgfs.write",
        {
            **member_node["helper"],
            "spaceId": member_node["directory"],
            "node": "member-note.md",
            "text": "directory stays collaborative",
        },
    )
    fs = app._require_orgfs_runtime().facade
    assert (
        fs.read_text(member_node["directory"], "member-note.md")[0]
        == "directory stays collaborative"
    )


# -- ⑦ grants live in owner local state, never in orgfs ----------------------


def test_grants_stay_in_owner_local_state_and_out_of_orgfs(gate) -> None:
    """⑦ An effective grant (the deputy's write succeeds) must not appear in
    ANY orgfs space's content or trash — the authorization is owner-node
    local state, not a replicated document.

    Red on base: METHOD_NOT_FOUND at the grant call.
    """

    app = gate["app"]
    _ipc(app, "org.acl.grant", {**_OPERATOR, "org": ORG, "agent": gate["deputy_uri"]})
    _acl_write(gate, gate["deputy"], node="deputy-effect.md")

    fs = app._require_orgfs_runtime().facade
    spaces = fs.spaces()
    assert {"group-acme", "group-acme-acl"} <= {space.name for space in spaces}
    for space in spaces:
        for path, blob in _walk_space(fs, space.space_id):
            assert b"gate-deputy" not in blob, (space.name, path)
        for node in fs.trash(space.space_id, limit=1000):
            assert "gate-deputy" not in node.name, (space.name, node.name)
