"""P3 selection readers: authoritative Agent/config/grant metadata and send proof."""

from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

from hyprial.identity import AgentRegistry, SecretResolver, SecretSource

from hyprial.daemon.impl.application.onboarding.first_run.selection import (
    agent_selection_reader,
    current_attempt_reader,
    reply_reader,
    send_binding_reader,
)

OWNER = "owner"
MACHINE = "node"
CONVERSATION = "c.0123456789abcdef"
REQUEST = "m.request-0123456789"
SENDER = "user:owner"
AGENT = "agent:owner@node:first-run"


def _registry(tmp_path: Path) -> tuple[AgentRegistry, SecretResolver]:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    registry = AgentRegistry(
        tmp_path / "state" / "agents.sqlite3",
        owner=OWNER,
        machine=MACHINE,
        hyprial_home=home,
    )
    return registry, SecretResolver(home, registry)


def _grant(resolver: SecretResolver, registry: AgentRegistry, actor: str, revision: int) -> None:
    resolver.write_user_provider("deepseek", {"apiKey": "sentinel"})
    registry.grant_secret(
        actor,
        grant_id="model-deepseek",
        source=SecretSource.USER_PROVIDER,
        entry_id="deepseek",
        field_name="apiKey",
        environment_names=("DEEPSEEK_API_KEY",),
        revision=revision,
    )


def test_the_selection_reads_agent_incarnation_config_and_grant(tmp_path):
    registry, resolver = _registry(tmp_path)
    try:
        agent = registry.create(
            "first-run",
            preferred_harness="pi",
            provider="deepseek",
            model="deepseek-flash",
        )
        _grant(resolver, registry, agent.actor, 1)
        uri = registry.uri_for(agent.actor)

        selection = agent_selection_reader(registry)(uri)
        assert selection is not None
        assert selection.agent_uri == agent.uri
        assert selection.agent_created_at_ms == agent.created_at_ms
        assert selection.provider == "deepseek"
        assert selection.model == "deepseek-flash"
        assert selection.harness == "pi"
        assert selection.credential.kind == "key"
        assert selection.credential.grant_id == "model-deepseek"
        assert selection.credential.entity_token == agent.entity_token
        assert selection.credential.revision == 1

        _grant(resolver, registry, agent.actor, 2)
        changed = agent_selection_reader(registry)(uri)
        assert changed is not None
        assert changed.credential.revision == 2
        assert changed.credential != selection.credential
    finally:
        registry.close()


def test_no_credential_foreign_uri_or_ambiguous_grant_is_not_current(tmp_path):
    registry, resolver = _registry(tmp_path)
    try:
        agent = registry.create("first-run", preferred_harness="pi")
        reader = agent_selection_reader(registry)
        assert reader(registry.uri_for(agent.actor)) is None
        assert reader("agent:someone@elsewhere:x:y") is None
        _grant(resolver, registry, agent.actor, 1)
        registry.grant_secret(
            agent.actor,
            grant_id="second-grant",
            source=SecretSource.USER_PROVIDER,
            entry_id="deepseek",
            field_name="apiKey",
            environment_names=("SECOND_API_KEY",),
            revision=1,
        )
        assert reader(registry.uri_for(agent.actor)) is None
    finally:
        registry.close()


class _Inbox:
    def __init__(self, request=None, reply=None):
        self.request = request
        self.reply = reply
        self.calls = []

    def correlated_request(self, **selectors):
        self.calls.append(("correlated_request", selectors))
        return self.request

    def correlated_reply(self, **selectors):
        self.calls.append(("correlated_reply", selectors))
        return self.reply


def _request_row(**overrides) -> SimpleNamespace:
    values = dict(
        message_id=REQUEST,
        sender=SENDER,
        recipient=AGENT,
        conversation_id=CONVERSATION,
        operation_key="frs-key",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_send_binding_requires_the_exact_request_sender_recipient_and_key():
    inbox = _Inbox(request=_request_row())
    reader = send_binding_reader(inbox)
    binding = reader(SENDER, AGENT, CONVERSATION, "frs-key")
    assert binding is not None
    assert binding.message_id == REQUEST
    assert binding.idempotency_key == "frs-key"
    assert inbox.calls == [("correlated_request", {
        "sender": SENDER, "recipient": AGENT, "operation_key": "frs-key",
    })]
    assert reader(SENDER, AGENT, "c.other", "frs-key") is None


def test_send_binding_refuses_every_unverified_or_missing_answer():
    assert send_binding_reader(_Inbox(None))(SENDER, AGENT, CONVERSATION, "frs-key") is None

    class Broken(_Inbox):
        def correlated_request(self, **selectors):
            raise RuntimeError("inbox unavailable")

    assert send_binding_reader(Broken())(SENDER, AGENT, CONVERSATION, "frs-key") is None


def _reply_row(*, message_id: str = "m.reply-0123456789") -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        request_message_id=REQUEST,
        sender=AGENT,
        recipient=SENDER,
        conversation_id=CONVERSATION,
        received_at_ms=1_760_000_000_100,
    )


def test_reply_reader_maps_the_exact_durable_reply():
    inbox = _Inbox(reply=_reply_row())
    reply = reply_reader(inbox)(AGENT, SENDER, REQUEST, CONVERSATION)
    assert reply is not None
    assert reply.reply_to == REQUEST
    assert reply.message_id == "m.reply-0123456789"
    assert reply.sender == AGENT
    assert reply.recipient == SENDER
    assert reply.conversation_id == CONVERSATION
    assert reply.intent == "reply"
    assert inbox.calls == [("correlated_reply", {
        "sender": AGENT,
        "recipient": SENDER,
        "conversation_id": CONVERSATION,
        "request_message_id": REQUEST,
    })]


def test_reply_reader_refuses_missing_or_unreadable_reply():
    assert reply_reader(_Inbox())(AGENT, SENDER, REQUEST, CONVERSATION) is None

    class Broken(_Inbox):
        def correlated_reply(self, **selectors):
            raise RuntimeError("inbox unavailable")

    assert reply_reader(Broken())(AGENT, SENDER, REQUEST, CONVERSATION) is None


def _attempt(**overrides):
    values = dict(
        login_epoch=3,
        agent_uri="agent:owner@node:first-run",
        agent_created_at_ms=100,
        provider="deepseek",
        model="deepseek-flash",
        harness="pi",
        config_revision=None,
        credential=SimpleNamespace(kind="key", grant_id="model-deepseek", revision=1),
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _selection(**overrides):
    values = dict(
        agent_uri="agent:owner@node:first-run",
        agent_created_at_ms=100,
        provider="deepseek",
        model="deepseek-flash",
        harness="pi",
        config_revision=None,
        credential=SimpleNamespace(kind="key", grant_id="model-deepseek", revision=1),
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _attempts(attempt):
    return SimpleNamespace(read=lambda subject: attempt)


def test_a_current_attempt_needs_the_same_login_and_selection():
    attempt = _attempt()
    selection = _selection()
    read = current_attempt_reader(
        _attempts(attempt), lambda: 3, lambda uri: selection if uri == attempt.agent_uri else None,
    )
    assert read("subject-1") is attempt

    assert current_attempt_reader(_attempts(attempt), lambda: 4, lambda uri: selection)("subject-1") is None
    assert current_attempt_reader(_attempts(attempt), lambda: 3, lambda uri: None)("subject-1") is None
    changed = _selection(model="deepseek-pro")
    assert current_attempt_reader(_attempts(attempt), lambda: 3, lambda uri: changed)("subject-1") is None
    rotated = _selection(credential=SimpleNamespace(kind="key", grant_id="model-deepseek", revision=2))
    assert current_attempt_reader(_attempts(attempt), lambda: 3, lambda uri: rotated)("subject-1") is None
    assert current_attempt_reader(_attempts(None), lambda: 3, lambda uri: selection)("subject-1") is None


def test_a_failing_attempt_or_selection_read_projects_nothing():
    class Broken:
        def read(self, subject):
            raise RuntimeError("attempt store unavailable")

    assert current_attempt_reader(Broken(), lambda: 3, lambda uri: _selection())("subject-1") is None

    def broken_selection(uri):
        raise RuntimeError("registry unavailable")

    assert current_attempt_reader(
        _attempts(_attempt()), lambda: 3, broken_selection,
    )("subject-1") is None
