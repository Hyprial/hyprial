"""P3 first-run attempt binding: durable reply_to and current configuration.

The verifier is pure: the service injects the trusted sender, the current
configuration read from the agent authority and the durable reply row selected
by ``replyTo``.  Conversation plus time is never an association.
"""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.onboarding.first_run import (
    FIRST_RUN_CONFIGURATION_CHANGED,
    FIRST_RUN_REPLY_UNVERIFIED,
    FirstRunAttempt,
    FirstRunConfiguration,
    FirstRunReply,
    FirstRunVerificationError,
    verify_first_run_attempt,
)

AGENT = "agent:owner@node:squire"
SENDER = "user:owner"
CONVERSATION = "c.0123456789abcdef"
REQUEST = "m.request-0123456789"
REPLY = "m.reply-0123456789"
GRANT = "model-deepseek"
KEY = "opkey-0123456789abcdef"


def _configuration(**overrides) -> FirstRunConfiguration:
    values = {
        "provider": "deepseek",
        "model": "deepseek-flash",
        "harness": "pi",
        "grant_id": GRANT,
        "grant_revision": 1,
    }
    values.update(overrides)
    return FirstRunConfiguration(**values)


def _attempt(**overrides) -> FirstRunAttempt:
    values = {
        "agent_uri": AGENT,
        "conversation_id": CONVERSATION,
        "request_message_id": REQUEST,
        "configuration": _configuration(),
    }
    values.update(overrides)
    return FirstRunAttempt(**values)


def _reply(**overrides) -> FirstRunReply:
    values = {
        "message_id": REPLY,
        "reply_to": REQUEST,
        "sender": AGENT,
        "recipient": SENDER,
        "conversation_id": CONVERSATION,
        "intent": "reply",
        "received_at_ms": 1_760_000_000_000,
    }
    values.update(overrides)
    return FirstRunReply(**values)


def test_accepts_a_reply_whose_durable_reply_to_names_the_request():
    reply = verify_first_run_attempt(
        attempt=_attempt(),
        reply=_reply(),
        current=_configuration(),
        trusted_sender=SENDER,
    )
    assert reply.message_id == REPLY
    assert reply.reply_to == REQUEST


def test_rejects_an_old_reply_that_answers_another_request():
    with pytest.raises(FirstRunVerificationError) as raised:
        verify_first_run_attempt(
            attempt=_attempt(),
            reply=_reply(reply_to="m.older-request-0123456789"),
            current=_configuration(),
            trusted_sender=SENDER,
        )
    assert raised.value.code == FIRST_RUN_REPLY_UNVERIFIED


def test_rejects_a_reply_from_another_agent():
    with pytest.raises(FirstRunVerificationError) as raised:
        verify_first_run_attempt(
            attempt=_attempt(),
            reply=_reply(sender="agent:owner@node:other"),
            current=_configuration(),
            trusted_sender=SENDER,
        )
    assert raised.value.code == FIRST_RUN_REPLY_UNVERIFIED


def test_rejects_a_reply_for_another_trusted_sender():
    with pytest.raises(FirstRunVerificationError) as raised:
        verify_first_run_attempt(
            attempt=_attempt(),
            reply=_reply(recipient="user:other"),
            current=_configuration(),
            trusted_sender=SENDER,
        )
    assert raised.value.code == FIRST_RUN_REPLY_UNVERIFIED


def test_rejects_a_reply_from_another_conversation():
    with pytest.raises(FirstRunVerificationError) as raised:
        verify_first_run_attempt(
            attempt=_attempt(),
            reply=_reply(conversation_id="c.fedcba9876543210"),
            current=_configuration(),
            trusted_sender=SENDER,
        )
    assert raised.value.code == FIRST_RUN_REPLY_UNVERIFIED


def test_rejects_a_request_row_as_the_reply():
    with pytest.raises(FirstRunVerificationError) as raised:
        verify_first_run_attempt(
            attempt=_attempt(),
            reply=_reply(intent="request"),
            current=_configuration(),
            trusted_sender=SENDER,
        )
    assert raised.value.code == FIRST_RUN_REPLY_UNVERIFIED


def test_timeout_or_unknown_has_no_reply_to_verify():
    with pytest.raises(FirstRunVerificationError) as raised:
        verify_first_run_attempt(
            attempt=_attempt(),
            reply=None,
            current=_configuration(),
            trusted_sender=SENDER,
        )
    assert raised.value.code == FIRST_RUN_REPLY_UNVERIFIED


@pytest.mark.parametrize(
    "current",
    [
        _configuration(provider="anthropic"),
        _configuration(model="deepseek-chat"),
        _configuration(harness="claude"),
        _configuration(grant_id="model-other"),
        _configuration(grant_revision=2),
    ],
)
def test_rejects_a_configuration_or_grant_that_changed_since_the_attempt(current):
    with pytest.raises(FirstRunVerificationError) as raised:
        verify_first_run_attempt(
            attempt=_attempt(),
            reply=_reply(),
            current=current,
            trusted_sender=SENDER,
        )
    assert raised.value.code == FIRST_RUN_CONFIGURATION_CHANGED
