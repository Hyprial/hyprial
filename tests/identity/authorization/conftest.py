"""One adapter from versioned mechanism goldens to internal typed inputs.

No matcher, schema validator, or decision oracle is implemented here. Private
identity imports are intentional for identity's own unit tests. Construction
details live in this adapter so they do not become extra public API.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path

import pytest

import hyprial.identity as identity


FIXTURES = (
    Path(__file__).resolve().parents[3]
    / "contract" / "fixtures" / "generic-authorization"
)


def load_golden(kind: str) -> dict:
    return json.loads((FIXTURES / f"{kind}.json").read_text(encoding="utf-8"))


def enum_value(value):
    return getattr(value, "value", value)


class ContractAdapter:
    """Translate fixtures only; missing capability is never behavior RED."""

    @property
    def mechanism(self):
        mechanism = getattr(identity, "AuthorizationMechanism", None)
        if mechanism is None:
            pytest.fail(
                "API_UNAVAILABLE: identity.AuthorizationMechanism is absent; "
                "this failure is capability evidence, not behavior RED",
                pytrace=False,
            )
        return mechanism

    def _internal(self, leaf: str):
        self.mechanism
        try:
            return importlib.import_module(
                f"hyprial.identity.impl.authorization.{leaf}"
            )
        except ModuleNotFoundError as exc:
            pytest.fail(
                f"API_UNAVAILABLE: {exc.name}; not behavior RED", pytrace=False
            )

    def decode_pattern(self, raw: str):
        return self._internal("subjects").decode_pattern(raw)

    def decode_subject(self, raw: str):
        return self._internal("subjects").decode_subject(raw)

    def matches(self, pattern, subject) -> bool:
        return self._internal("subjects").matches(pattern, subject)

    def decode_rules(self, raw: bytes):
        return self._internal("rules").decode_rules(raw)

    def decision(self, case: dict):
        golden = load_golden("decisions")
        base = golden["base"]
        assert not (set(case["set"]) - set(base)), "unknown golden override"
        inputs = {**base, **case["set"]}
        rule_case = next(
            row for row in load_golden("rules")["cases"]
            if row["id"] == inputs["rulesCase"]
        )
        raw = rule_case["rawJson"].encode("utf-8")
        m = self.mechanism

        def ref(version):
            return m.RuleSetRef(
                scope="fixture-tailnet/scope-1",
                declaration_id="fixture-space/statement-1",
                version=version,
                digest=hashlib.sha256(raw).hexdigest(),
                schema_version=1,
                subject_profile="stable-id-v1",
            )

        authoritative = ref(inputs["authoritativeVersion"])
        observed = m.RuleSetRef(
            scope=inputs["observedScope"] or authoritative.scope,
            declaration_id=inputs["observedDeclarationId"] or authoritative.declaration_id,
            version=inputs["observedVersion"] or authoritative.version,
            digest=inputs["observedDigest"] or authoritative.digest,
            schema_version=authoritative.schema_version,
            subject_profile=inputs["observedProfile"] or authoritative.subject_profile,
        )
        requested = (
            None if inputs["requestedVersion"] is None
            else ref(inputs["requestedVersion"])
        )
        subject = m.SubjectObservation(
            state=inputs["subjectState"],
            subject=self.decode_subject(inputs["subject"]),
            epoch=1,
            observed_at=inputs["now"], now=inputs["now"], max_observation_age=30.0,
        )
        resource = m.ResourceObservation(
            state=inputs["resourceState"], resource=inputs["resource"],
            mapping_revision="fixture-mapping-v1", scope=authoritative.scope,
            observed_at=inputs["now"], now=inputs["now"], max_observation_age=30.0,
            authority_ref=authoritative,
        )
        rules = m.RuleSetObservation(
            state=inputs["rulesState"], ref=observed,
            rules=self.decode_rules(raw), epoch=1,
            observed_at=inputs["observedAt"], now=inputs["now"],
            max_observation_age=inputs["maxObservationAge"],
            watch_continuous=inputs["watchContinuous"],
            epoch_current=inputs["epochCurrent"],
        )
        decision = m.compose_evaluator().evaluate(
            subject=subject, resource=resource, action=inputs["action"],
            ruleset_ref=requested, rules=rules,
        )
        return decision, authoritative


@pytest.fixture
def mechanism_api() -> ContractAdapter:
    # Do not resolve product capability in fixture setup: every test collects,
    # and absent API is an explicit assertion in the call phase.
    return ContractAdapter()
