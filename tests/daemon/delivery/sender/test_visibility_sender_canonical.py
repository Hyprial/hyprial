"""Canonical send authorization over fake ledgers/session authority only."""
from __future__ import annotations

import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from hyprial.identity.impl.agents.runtime.grants import CapabilityGrant
from hyprial.identity.impl.agents.runtime.visibility import visible_targets
from hyprial.kernel import ipc_errors
from hyprial.daemon.impl.alias_resolver import daemon_alias_resolver
from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError
from hyprial.daemon.impl.inbox import SubmissionResult
from hyprial.daemon.impl.inbox.authority.errors import InboxSubmissionOutcomeUnknown

VISITOR = "agent:guest:node-1:visitor"
NATIVE = "agent:op:node-1:native"
OTHER = "agent:op:node-1:other"


class FakeApp(DaemonApplication):
    """Use real handle/auth/visibility methods without constructing any Agent."""

    def __init__(self):
        self.owner = "op"
        self.node_id = "node-1"
        self.submitted = []
        self.rows = {
            VISITOR: SimpleNamespace(hosted_by="op", capabilities={}),
            NATIVE: SimpleNamespace(hosted_by=None, capabilities={}),
            OTHER: SimpleNamespace(hosted_by=None, capabilities={}),
        }
        resolver_rows = tuple(
            SimpleNamespace(actor=name, agent_id=f"a.{name}")
            for name in ("visitor", "native", "other")
        )
        self.agents = SimpleNamespace(
            get=lambda actor: self.rows.get(actor),
            list=lambda: resolver_rows,
        )
        self.aliases = {"native": NATIVE, "visitor": VISITOR}
        self.resolutions = []
        self.grants = []
        self._agent_session_domains = SimpleNamespace(agent=SimpleNamespace(
            read_capability_grants=lambda actor: tuple(self.grants) if actor == VISITOR else (),
        ))
        self._inbox = SimpleNamespace(
            submit=self.submit,
            # WP7: message.send reads pending requests for its advisory
            # openRequestsFrom field; this fake has none.
            pending_messages=lambda *args, **kwargs: (),
        )
        self._harnesses = self._presence = object()
        self._restore_done = threading.Event()
        self._restore_done.set()
        self._dispatch_diagnostics = None
        self._dispatch_without_pac_lock = threading.Lock()
        self._dispatch_gate_conversations = {}
        self._sender_auth_enforced = True
        self.fences = []
        self.sessions = {VISITOR: "visitor-session", NATIVE: "native-session"}
        # #1753 requires live interactive bindings, not registration alone.
        self._agent_liveness = SimpleNamespace(
            binding=lambda actor: SimpleNamespace(
                runtime="interactive", session_id=self.sessions.get(actor)
            ),
            verdict=lambda actor: actor in self.sessions,
        )
        self.submit_unknown = False
        self._alias_resolver = daemon_alias_resolver(
            home=Path(__file__).parent / "_missing-home",
            agents=self.agents,
            owner=self.owner,
            node_id=self.node_id,
            known_agent=lambda alias: alias in {"visitor", "native", "other"},
            log=lambda event, **fields: self._log(
                "warn", "daemon", event, **fields
            ),
        )

    def submit(self, message):
        self.submitted.append(message)
        if self.submit_unknown:
            raise InboxSubmissionOutcomeUnknown(message, "inbox:submit:fake-only")
        return SubmissionResult(message.message_id, True)

    def _worker_session_ref(self, actor):
        return None  # This rig has interactive sessions only.

    def _fence_interactive_session(self, actor, params):
        self.fences.append((actor, params.get("sessionRef")))
        if self.sessions.get(actor) != params.get("sessionRef"):
            raise DaemonRequestError(ipc_errors.SESSION_SUPERSEDED, "not the fake session owner")
        return actor

    def _resolve_send_sender(self, actor):
        return actor

    def _resolve_agent_alias(self, target):
        self.resolutions.append(target)
        return self.aliases.get(target, target)

    def _record_unauthenticated_sender(self, *args):
        pass

    def _queue_agent_activity(self, actor):
        pass

    def _wake_dormant_agent(self, actor, **kwargs):
        pass

    def _is_dispatch_candidate(self, **kwargs):
        return False

    def _is_unresolved_local_identity(self, target):
        return False

    def _reply_path_unavailable(self, source):
        return False

    def _log(self, *args, **kwargs):
        pass

    def grant_both(self):
        self.grants = [CapabilityGrant(
            "visitor", "entity-visitor", capability, capability,
            json.dumps([NATIVE]), "user:op", 1,
        ) for capability in ("see-actors", "send-to")]

    def send(self, targets, **extra):
        return self.handle("message.send", {
            "actor": VISITOR, "sessionRef": "visitor-session",
            "to": targets, "summary": "fake-only", "message": "fake-only", **extra,
        }, _trusted_message_origin="ipc")


@pytest.mark.parametrize("target", [NATIVE, "native"], ids=["canonical", "alias"])
def test_visible_granted_target_preserves_address_equivalence(target):
    app = FakeApp()
    app.grant_both()
    caller = app._visibility_identity(VISITOR)
    assert visible_targets([{"targetUri": NATIVE}], caller=caller,
                           grants=app._visibility_grants(VISITOR)) == [{"targetUri": NATIVE}]
    assert app.send([target])["ok"] is True
    assert len(app.submitted) == 1 and app.submitted[0].recipient == NATIVE
    assert app.resolutions == [target]


def test_self_alias_needs_no_new_grant():
    app = FakeApp()
    assert app.send(["visitor"])["ok"] is True
    assert app.submitted[0].recipient == VISITOR


def test_authorized_alias_cannot_change_target_before_delivery():
    app = FakeApp()
    app.grant_both()
    authorize = app._enforce_send_to

    def authorize_then_replace(caller, targets):
        authorize(caller, targets)
        app.aliases["native"] = OTHER

    app._enforce_send_to = authorize_then_replace
    assert app.send(["native"])["ok"] is True
    assert [message.recipient for message in app.submitted] == [NATIVE]
    assert app.resolutions == ["native"]


def test_scope_mismatch_refuses_whole_batch_before_any_send():
    app = FakeApp()
    app.grant_both()
    with pytest.raises(DaemonRequestError) as error:
        app.send(["native", OTHER], hosted=False, verified=True, scope=json.dumps([OTHER]))
    assert error.value.code == ipc_errors.TARGET_NOT_AUTHORIZED
    assert not app.submitted


@pytest.mark.parametrize("target", ["user:guest", "user:op"])
def test_no_implicit_owner_or_host_owner_channel_grant(target):
    app = FakeApp()
    with pytest.raises(DaemonRequestError) as error:
        app.send([target])
    assert error.value.code == ipc_errors.TARGET_NOT_AUTHORIZED
    assert not app.submitted


def test_revoked_grant_is_not_reused_for_an_alias():
    app = FakeApp()
    app.grant_both()
    assert app.send(["native"])["ok"] is True
    app.grants.clear()
    with pytest.raises(DaemonRequestError) as error:
        app.send(["native"])
    assert error.value.code == ipc_errors.TARGET_NOT_AUTHORIZED
    assert len(app.submitted) == 1


def test_payload_cannot_mint_trusted_origin_or_native_authority():
    app = FakeApp()
    with pytest.raises(DaemonRequestError) as error:
        app.handle("message.send", {"actor": NATIVE, "to": [OTHER], "message": "fake-only",
                   "_trusted_message_origin": "daemon", "verified": True},
                   _trusted_message_origin="ipc")
    assert error.value.code == ipc_errors.SENDER_NOT_AUTHENTICATED
    assert not app.submitted


def test_superseded_binding_cannot_borrow_hosted_grants():
    app = FakeApp()
    app.grant_both()
    with pytest.raises(DaemonRequestError) as error:
        app.send(["native"], sessionRef="superseded")
    assert error.value.code == ipc_errors.SESSION_SUPERSEDED
    assert not app.resolutions and not app.submitted


def test_hosted_flag_is_registry_owned_and_unknown_submission_is_not_success():
    app = FakeApp()
    with pytest.raises(DaemonRequestError) as denied:
        app.send([NATIVE], hosted=False, verified=True)
    assert denied.value.code == ipc_errors.TARGET_NOT_AUTHORIZED
    app.grant_both()
    app.submit_unknown = True
    with pytest.raises(InboxSubmissionOutcomeUnknown) as unknown:
        app.send(["native"])
    assert unknown.value.code == ipc_errors.SUBMIT_OUTCOME_UNKNOWN
    assert unknown.value.data["outcomeKnown"] is False
    assert unknown.value.data["messageId"] == app.submitted[0].message_id


@pytest.mark.parametrize("session_ref", ["fabricated", "old-native-session"])
def test_bound_native_org_content_read_cannot_skip_session_fence(session_ref):
    app = FakeApp()
    reads = []
    app._require_orgfs_runtime = lambda: SimpleNamespace(facade=SimpleNamespace(
        stat=lambda *args: SimpleNamespace(kind="doc"),
        read_text=lambda *args: reads.append(args) or ("fake-org-content", "v1"),
    ))
    with pytest.raises(DaemonRequestError) as error:
        app.handle("orgfs.read", {"actor": NATIVE, "sessionRef": session_ref,
                                 "spaceId": "space", "path": "readme.md"})
    assert error.value.code == ipc_errors.SESSION_SUPERSEDED
    assert reads == []


def test_verified_native_and_local_org_content_reads_keep_their_exemption():
    app = FakeApp()
    app._require_orgfs_runtime = lambda: SimpleNamespace(facade=SimpleNamespace(
        stat=lambda *args: SimpleNamespace(kind="doc"),
        read_text=lambda *args: ("fake-org-content", "v1"),
    ))
    read = {"spaceId": "space", "path": "readme.md"}
    assert app.handle("orgfs.read", {**read, "actor": NATIVE,
                                   "sessionRef": "native-session"})["text"] == "fake-org-content"
    assert app.fences == [(NATIVE, "native-session")]
    assert app.handle("orgfs.read", read)["text"] == "fake-org-content"


def test_content_fencing_does_not_change_the_disclosed_native_targets_boundary():
    app = FakeApp()
    assert app._visibility_caller({"actor": NATIVE, "sessionRef": "fabricated"}) is None
    assert app.fences == []
