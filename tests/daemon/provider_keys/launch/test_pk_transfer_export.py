"""Transfer export under the owner-key authority (contract §3.4, §8.2; spec C1).

Slots the new authority holds bind to the incarnation UUID and stay in the
owner store: the export must never carry a plaintext for them, only a
``requiresReauth`` record. Under the owner-key gate no user-sourced plaintext
leaves the node at all, because a slot whose record fails verification is
invisible to the grant listing (§7.3, §8.4 fail closed). The legacy gate keeps
today's export unchanged.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from hyprial.daemon.impl.transfer.archive.env_credentials import env_credentials_for_quiesce
from hyprial.identity import ProviderKeys
from hyprial.kernel import ipc_errors

LEGACY_VALUE = "LEGACY-SENTINEL-0123456789"
PRIVATE_VALUE = "PRIVATE-SENTINEL-5555"
SPEC = SimpleNamespace(name="worker", harness="pi")


def _export(harness, *, app=None):
    kwargs = {} if app is None else {"app": app}
    rows = env_credentials_for_quiesce(registry=harness.registry, hyprial_home=harness.home, spec=SPEC, **kwargs)
    return {row["entryId"]: row for row in rows}, json.dumps(rows)


def _seed(harness):
    harness.legacy_user_grant("openai", "openai", "OPENAI_API_KEY")
    harness.legacy_user_grant("anthropic", "anthropic", "ANTHROPIC_API_KEY")
    harness.legacy_private_grant("deepseek", "DEEPSEEK_API_KEY")


def test_the_legacy_gate_exports_exactly_as_before(harness, gate):
    gate("legacy")
    _seed(harness)
    harness.service.answer = RuntimeError("must not be called")
    before, _ = _export(harness)
    after, _ = _export(harness, app=harness.app)
    assert after == before
    assert after["openai"]["value"] == LEGACY_VALUE
    assert harness.service.calls == [] and harness.resolved == []


def test_a_held_slot_is_exported_as_requires_reauth_without_value(harness, gate):
    gate("owner-keys")
    _seed(harness)
    harness.service.grant_states = [{"grantId": "openai", "state": "active"}]
    rows, text = _export(harness, app=harness.app)
    assert rows["openai"]["requiresReauth"] is True
    assert "value" not in rows["openai"]
    assert rows["anthropic"]["requiresReauth"] is True and "value" not in rows["anthropic"]
    assert rows["deepseek.json"]["value"] == PRIVATE_VALUE
    assert LEGACY_VALUE not in text
    assert [call[0] for call in harness.service.calls] == ["grants_for"]


def test_a_revoked_held_slot_is_still_never_exported(harness, gate):
    gate("owner-keys")
    _seed(harness)
    harness.service.grant_states = [{"grantId": "openai", "state": "revoked"}]
    rows, _ = _export(harness, app=harness.app)
    assert rows["openai"]["requiresReauth"] is True and "value" not in rows["openai"]


def test_an_unknowable_authority_withholds_every_user_sourced_value(harness, gate):
    gate("owner-keys")
    _seed(harness)
    harness.service.answer = ProviderKeys.Error(ipc_errors.PROVIDER_STORE_UNAVAILABLE)
    rows, text = _export(harness, app=harness.app)
    assert rows["openai"]["requiresReauth"] is True and "value" not in rows["openai"]
    assert rows["anthropic"]["requiresReauth"] is True and "value" not in rows["anthropic"]
    assert rows["deepseek.json"]["value"] == PRIVATE_VALUE
    assert LEGACY_VALUE not in text


def test_a_damaged_gate_withholds_every_user_sourced_value(harness, gate):
    gate(ProviderKeys.Error(ipc_errors.PROVIDER_STORE_UNAVAILABLE))
    _seed(harness)
    rows, text = _export(harness, app=harness.app)
    assert rows["openai"]["requiresReauth"] is True
    assert LEGACY_VALUE not in text
    assert harness.service.calls == []


def test_a_slot_whose_record_fails_verification_still_withholds_the_legacy_value(harness, gate):
    gate("owner-keys")
    _seed(harness)
    harness.service.grant_states = []
    rows, text = _export(harness, app=harness.app)
    assert rows["openai"]["requiresReauth"] is True and "value" not in rows["openai"]
    assert rows["anthropic"]["requiresReauth"] is True and "value" not in rows["anthropic"]
    assert rows["deepseek.json"]["value"] == PRIVATE_VALUE
    assert LEGACY_VALUE not in text


def test_an_unverifiable_slot_withholds_a_private_legacy_value_of_the_same_id(harness, gate):
    gate("owner-keys")
    _seed(harness)
    harness.service.grant_states = []
    harness.service.slot_files = {"deepseek"}
    rows, text = _export(harness, app=harness.app)
    assert rows["deepseek.json"]["requiresReauth"] is True and "value" not in rows["deepseek.json"]
    assert PRIVATE_VALUE not in text and LEGACY_VALUE not in text


def test_a_slot_only_the_new_authority_holds_is_announced_for_reauth(harness, gate):
    gate("owner-keys")
    _seed(harness)
    harness.service.grant_states = [
        harness.grant_state("gemini", "GEMINI_API_KEY"),
        harness.grant_state("mistral", "MISTRAL_API_KEY", state="awaiting-key"),
        harness.grant_state("cohere", "COHERE_API_KEY", state="revoked"),
    ]
    rows, text = _export(harness, app=harness.app)
    assert rows["gemini"]["requiresReauth"] is True and "value" not in rows["gemini"]
    assert rows["gemini"]["environment"] == ["GEMINI_API_KEY"] and rows["gemini"]["field"] is None
    assert rows["mistral"]["requiresReauth"] is True
    assert "cohere" not in rows
    assert LEGACY_VALUE not in text


def test_a_held_slot_with_a_private_legacy_row_is_not_announced_twice(harness, gate):
    gate("owner-keys")
    harness.legacy_private_grant("deepseek", "DEEPSEEK_API_KEY")
    harness.service.grant_states = [harness.grant_state("deepseek", "DEEPSEEK_API_KEY")]
    rows = env_credentials_for_quiesce(registry=harness.registry, hyprial_home=harness.home, spec=SPEC, app=harness.app)
    assert [(row["entryId"], row["requiresReauth"]) for row in rows] == [("deepseek.json", True)]
    assert all("value" not in row for row in rows)


def test_a_foreign_hosted_agent_exports_as_under_the_legacy_gate(harness, gate):
    gate("owner-keys")
    guest = harness.foreign_hosted()
    harness.service.answer = RuntimeError("must not be called")
    rows = env_credentials_for_quiesce(registry=harness.registry, hyprial_home=harness.home,
                                       spec=SimpleNamespace(name=guest.actor, harness="pi"), app=harness.app)
    assert rows == []
    assert harness.service.calls == []
