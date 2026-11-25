from __future__ import annotations

import hashlib
from dataclasses import FrozenInstanceError, replace

import pytest

from hyprial.daemon.impl.adapters.contracts import (
    AgentOperationalAuthorizationRefV1,
    ChatAuthorizationResourceV1,
    InstanceAuthorizationResourceV1,
    NativeOperationDag,
    OnboardingAttempt,
    OperationNode,
    RequiredAuthorizationV1,
    ServiceAuthorizationRefV1,
    authorization_required_digest_v1,
    frozen_json_digest_v1,
)


LEASE_BYTES = b'{"lease":"sealed"}'
LEASE_DIGEST = hashlib.sha256(LEASE_BYTES).hexdigest()


def test_authorization_refs_match_frozen_r15_shape() -> None:
    resource = ChatAuthorizationResourceV1(
        "chat", "org-1", "channel-1", "chat-ref:v1:one", 4, "audience-1"
    )
    required_fields = (
        "service",
        "service:daemon:node-1:epoch-1",
        "chat.submit.text",
        resource,
        "service-row-1",
        3,
        "use-1",
    )
    required = RequiredAuthorizationV1(
        *required_fields, authorization_required_digest_v1(*required_fields)
    )
    agent = AgentOperationalAuthorizationRefV1(
        "hyprial.identity.agent-operational-authorization-ref/v1",
        "agent-lease-1",
        2,
        "org-1",
        "agent:one",
        "entity-1",
        4,
        "use-1",
    )
    service = ServiceAuthorizationRefV1(
        "hyprial.identity.service-authorization-ref/v1",
        "service-lease-1",
        3,
        "plan-1",
        "daemon-chat-outbox",
        "use-1",
        "a" * 64,
    )
    assert required.resource is resource
    assert agent.association_generation == 4
    assert service.use_owner == "daemon-chat-outbox"


def _authorization_refs() -> tuple[
    AgentOperationalAuthorizationRefV1,
    ServiceAuthorizationRefV1,
    tuple[RequiredAuthorizationV1, ...],
]:
    agent = AgentOperationalAuthorizationRefV1(
        "hyprial.identity.agent-operational-authorization-ref/v1",
        "agent-lease-1",
        2,
        "org-1",
        "agent:one",
        "entity-1",
        4,
        "use-1",
    )
    service = ServiceAuthorizationRefV1(
        "hyprial.identity.service-authorization-ref/v1",
        "service-lease-1",
        3,
        "plan-1",
        "daemon-chat-outbox",
        "use-1",
        LEASE_DIGEST,
    )
    resources = (
        InstanceAuthorizationResourceV1("instance", "org-1", "instance-1", 3, "fp-1"),
        ChatAuthorizationResourceV1(
            "chat", "org-1", "channel-1", "chat-ref:v1:one", 4, "audience-1"
        ),
    )
    rows = []
    for kind, subject, source, generation in (
        ("agent-operational", "agent:one", "agent-lease-1", 2),
        ("service", "service:daemon:node-1:epoch-1", "service-lease-1", 3),
    ):
        for resource in resources:
            fields = (
                kind,
                subject,
                "chat.submit.text",
                resource,
                source,
                generation,
                "use-1",
            )
            rows.append(RequiredAuthorizationV1(*fields, authorization_required_digest_v1(*fields)))
    required = tuple(rows)
    return agent, service, required


def _node(template: object | None = None, sealed: object | None = None) -> OperationNode:
    agent, service, required = _authorization_refs()
    template_value = {"text": "hello"} if template is None else template
    sealed_value = {"request": "hello"} if sealed is None else sealed
    return OperationNode(
        "node-1",
        "text-send",
        (),
        template_value,
        frozen_json_digest_v1(template_value),
        sealed_value,
        frozen_json_digest_v1(sealed_value),
        "echoed-operation-id",
        "cap-1",
        "evidence-1",
        agent,
        service,
        required,
        "use-1",
        "idem-1",
        "submitting",
    )


def _dag(node: OperationNode | None = None, lease: bytes = LEASE_BYTES) -> NativeOperationDag:
    return NativeOperationDag(
        "plan-1",
        "target-1",
        "renderer-1",
        "cap-1",
        "credential-ref-1",
        "content-digest-1",
        lease,
        hashlib.sha256(lease).hexdigest(),
        (_node() if node is None else node,),
    )


def test_authorization_refs_are_typed_frozen_and_revision_bound() -> None:
    agent, service, required = _authorization_refs()
    with pytest.raises(FrozenInstanceError):
        agent.lease_generation = 9  # type: ignore[misc]
    with pytest.raises(TypeError):
        OperationNode(
            "node-1", "text-send", (), {}, frozen_json_digest_v1({}), {},
            frozen_json_digest_v1({}), "echoed-operation-id", "cap-1", "evidence-1",
            {"lease_id": "mutable"}, service, required, "use-1", "idem-1", "submitting",
        )
    with pytest.raises(ValueError):
        replace(_node(), authorization_use_nonce="use-drift")


def test_operation_node_deep_freezes_and_checks_template_and_sealed_digests() -> None:
    template = {"body": ["before"]}
    node = _node(template=template)
    template["body"].append("after")
    assert node.template == (("body", ("before",)),)
    with pytest.raises(ValueError):
        replace(node, template_digest="wrong")
    with pytest.raises(ValueError):
        replace(node, sealed_digest="wrong")


def test_dag_checks_service_lease_digest_bytes_and_node_revisions() -> None:
    with pytest.raises(ValueError):
        replace(_dag(), service_authorization_lease_digest="wrong")
    with pytest.raises(ValueError):
        _dag(lease=b"x" * (64 * 1024 + 1))
    with pytest.raises(ValueError):
        replace(_dag(), capability_revision="cap-drift")
    with pytest.raises(TypeError):
        replace(_dag(), service_authorization_lease_bytes=1)  # type: ignore[arg-type]


def test_operation_cross_binds_exact_authorization_rows_and_refs() -> None:
    node = _node()
    wrong_subject_rows = []
    for row in node.required_authorizations:
        subject = (
            "agent:other"
            if row.authorization_kind == "agent-operational"
            else row.expected_subject_principal
        )
        fields = (
            row.authorization_kind,
            subject,
            row.action,
            row.resource,
            row.source_record_ref,
            row.source_generation,
            row.use_nonce,
        )
        wrong_subject_rows.append(
            RequiredAuthorizationV1(*fields, authorization_required_digest_v1(*fields))
        )
    with pytest.raises(ValueError):
        replace(node, required_authorizations=tuple(wrong_subject_rows))
    with pytest.raises(ValueError):
        replace(
            node,
            required_authorizations=node.required_authorizations
            + (node.required_authorizations[0],),
        )
    with pytest.raises(ValueError):
        replace(
            node,
            service_authorization_ref=replace(
                node.service_authorization_ref, use_owner="identity-agent-actor"
            ),
        )
    with pytest.raises(ValueError):
        replace(
            _dag(),
            nodes=(
                replace(
                    node,
                    service_authorization_ref=replace(
                        node.service_authorization_ref, operation_id="other-plan"
                    ),
                ),
            ),
        )


def test_operation_literals_and_state_sealing_are_closed() -> None:
    with pytest.raises(ValueError):
        replace(_node(), state="invented")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        replace(_node(), kind="shell-command")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        replace(_node(), state="accepted", sealed_request=None, sealed_digest=None)
    with pytest.raises(ValueError):
        replace(
            _node(),
            echo_strategy="ambiguous-self-quarantine",
            echo_evidence_revision="evidence-1",
        )


def test_onboarding_config_is_deep_frozen_and_secret_free() -> None:
    config = {"region": "cn", "options": ["safe"]}
    attempt = OnboardingAttempt("attempt-1", config, "credential-ref-1")
    config["options"].append("mutated")
    assert attempt.config == (("options", ("safe",)), ("region", "cn"))
    with pytest.raises(ValueError):
        OnboardingAttempt("attempt-2", {"appSecret": "cleartext"}, "credential-ref-2")
