"""Daemon-side narrow authorities for the P3 first-run attempt.

Decision record (web-auth-r3-p3-selection-20261118):
- The caller selects only a credential kind (and one Key grant id); every
  authoritative fact is read here from the identity registry and inbox, never
  from request parameters.  A Key binding carries the grant metadata and the
  Agent incarnation; a native binding carries the import generation and the
  observed manifest.  A grant revision or import id alone never proves that a
  different secret value still exists.
- ``correlated_request`` is the send authority: a request binds only when the
  stored sender copy for the attempt's own send key names the same sender,
  Agent recipient and conversation.  A key or conversation match is not enough.
- ``correlated_reply`` is the reply authority: the Agent sender, trusted
  recipient, conversation and exact payload ``replyTo`` must all match one
  retained durable row, including after ACK; no fetch, ACK or drain.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from hyprial.identity import (
    NativeImportRecord,
    capture_native_auth_launch,
    read_login_generation,
)

from hyprial.daemon.impl.onboarding.first_run import FirstRunReply
from hyprial.daemon.impl.onboarding.first_run_attempt import (
    FirstRunCredentialBinding,
)

from .service import AgentSelection, RequestBinding


def login_epoch_reader(home: Path) -> int:
    """The committed login generation; 0 means no current login exists."""

    value = read_login_generation(Path(home))
    return value or 0


def agent_selection_reader(registry: Any) -> Callable[..., AgentSelection | None]:
    """Read one local Agent's configuration and its single credential binding.

    A first-run Agent carries exactly one credential method.  Zero or more than
    one binding is not "the selected credential", so the reader refuses instead
    of choosing for the caller.
    """

    def read(agent_uri: str) -> AgentSelection | None:
        try:
            agent = registry.get(agent_uri)
        except Exception:
            return None
        if agent is None or not agent.preferred_harness:
            return None
        try:
            grants = registry.secret_inventory(
                agent.actor,
                expected_uri=agent.uri,
                expected_created_at_ms=agent.created_at_ms,
            )
        except Exception:
            return None
        native = _native_binding(registry, agent)
        if len(grants) == 1 and native is None:
            credential = _key_binding(grants[0])
        elif not grants and native is not None:
            credential = native
        else:
            return None
        if credential is None:
            return None
        return AgentSelection(
            agent_uri=agent.uri,
            agent_created_at_ms=agent.created_at_ms,
            provider=agent.provider,
            model=agent.model,
            harness=agent.preferred_harness,
            config_revision=None,
            credential=credential,
        )

    return read


def _key_binding(grant: Any) -> FirstRunCredentialBinding:
    return FirstRunCredentialBinding(
        kind="key",
        grant_id=grant.grant_id,
        entry_id=grant.entry_id,
        entity_token=grant.entity_token,
        revision=grant.revision,
        route_required=grant.route_required,
    )


def _native_binding(registry: Any, agent: Any) -> FirstRunCredentialBinding | None:
    harness = agent.preferred_harness
    if not harness:
        return None
    try:
        record = NativeImportRecord(registry, agent.actor)
        receipt = registry.home_receipt(agent.actor, require_ready=False)
        launch = capture_native_auth_launch(
            receipt.path, harness, import_record=record
        )
    except Exception:
        return None
    snapshot = None if launch is None else launch.snapshot
    if snapshot is None:
        return None
    import_id, manifest = snapshot
    try:
        imported_at_ms = record.read(harness, manifest)
    except Exception:
        return None
    if imported_at_ms is None:
        return None
    return FirstRunCredentialBinding(
        kind="native",
        import_id=import_id,
        imported_at_ms=imported_at_ms,
        manifest=manifest,
    )


def send_binding_reader(inbox: Any) -> Callable[..., RequestBinding | None]:
    """Resolve the stored send by the attempt's own key through the inbox."""

    def read(
        sender: str,
        recipient: str,
        conversation_id: str,
        idempotency_key: str,
    ) -> RequestBinding | None:
        try:
            request = inbox.correlated_request(
                sender=sender,
                recipient=recipient,
                operation_key=idempotency_key,
            )
        except Exception:
            return None
        if request is None or request.conversation_id != conversation_id:
            return None
        return RequestBinding(
            message_id=request.message_id,
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
        )

    return read


def reply_reader(inbox: Any) -> Callable[..., FirstRunReply | None]:
    """Read the one retained reply to the request from its exact sender."""

    def read(
        sender: str,
        recipient: str,
        request_message_id: str,
        conversation_id: str,
    ) -> FirstRunReply | None:
        try:
            reply = inbox.correlated_reply(
                sender=sender,
                recipient=recipient,
                conversation_id=conversation_id,
                request_message_id=request_message_id,
            )
        except Exception:
            return None
        if reply is None:
            return None
        return FirstRunReply(
            message_id=reply.message_id,
            reply_to=reply.request_message_id,
            sender=reply.sender,
            recipient=reply.recipient,
            conversation_id=reply.conversation_id,
            intent="reply",
            received_at_ms=reply.received_at_ms,
        )

    return read


def current_attempt_reader(
    attempts: Any,
    login_epoch: Callable[[], int],
    select_agent: Callable[..., AgentSelection | None],
) -> Callable[[Any], Any]:
    """Project an attempt only while its login and selection are current."""

    def read(subject: Any) -> Any:
        try:
            attempt = attempts.read(subject)
            if attempt is None:
                return None
            selection = select_agent(attempt.agent_uri)
            epoch = login_epoch()
        except Exception:
            return None
        if (
            selection is None
            or attempt.login_epoch != epoch
            or selection.agent_created_at_ms != attempt.agent_created_at_ms
            or selection.provider != attempt.provider
            or selection.model != attempt.model
            or selection.harness != attempt.harness
            or selection.config_revision != attempt.config_revision
            or selection.credential != attempt.credential
        ):
            return None
        return attempt

    return read


__all__ = [
    "agent_selection_reader",
    "current_attempt_reader",
    "login_epoch_reader",
    "reply_reader",
    "send_binding_reader",
]
