"""Synchronous pure evaluation of trusted observations in approved priority order.

All time values are injected observations. This module never obtains identity,
reads a source, verifies signatures or grants a reusable permit.
"""

import math

from .ports import (
    Decision, Reason, ResourceObservation, RuleSetObservation, RuleSetRef,
    SubjectObservation,
)
from .rules import valid_token
from .subjects import matches


def _finite_number(value: object) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _fresh(*, observed_at, now, max_observation_age, valid_until) -> bool:
    if not all(_finite_number(value) for value in (observed_at, now, max_observation_age)):
        return False
    if not 0 < max_observation_age <= 30 or not 0 <= now - observed_at < max_observation_age:
        return False
    return valid_until is None or (_finite_number(valid_until) and now < valid_until)


def _optional_fresh(observation) -> bool:
    return _fresh(
        observed_at=observation.observed_at, now=observation.now,
        max_observation_age=observation.max_observation_age,
        valid_until=observation.valid_until,
    )


def _host_rejected(subject: SubjectObservation) -> bool:
    if subject.connection_host is None:
        return False
    return (
        subject.attribution_host is not None
        and subject.connection_host != subject.attribution_host
    ) or (
        subject.subject is not None and subject.connection_host != subject.subject.host
    )


class AuthorizationEvaluator:
    def evaluate(
        self, *, subject: SubjectObservation, resource: ResourceObservation,
        action: str, ruleset_ref: RuleSetRef | None, rules: RuleSetObservation,
    ) -> Decision:
        authoritative = resource.authority_ref

        def result(outcome, reason):
            return Decision(outcome, reason, authoritative, rules.epoch)

        # P0-P4: explicit rejection takes precedence over missing prerequisites.
        if not valid_token(action, limit=128):
            return result("deny", Reason.REQUEST_INVALID)
        if subject.state == "rejected" or _host_rejected(subject):
            return result("deny", Reason.SUBJECT_REJECTED)
        if resource.state == "rejected" or (
            resource.resource is not None and not valid_token(resource.resource, limit=2048)
        ):
            return result("deny", Reason.RESOURCE_REJECTED)
        if subject.state != "verified" or subject.subject is None or not _optional_fresh(subject):
            return result("unknown", Reason.SUBJECT_UNAVAILABLE)
        if any(
            epoch is not None and epoch != subject.epoch
            for epoch in (subject.connection_epoch, subject.attribution_epoch)
        ):
            return result("unknown", Reason.SUBJECT_UNAVAILABLE)
        if (
            resource.state != "verified" or resource.resource is None
            or not resource.mapping_revision or not resource.scope
            or not _optional_fresh(resource)
            or (authoritative is not None and authoritative.scope != resource.scope)
        ):
            return result("unknown", Reason.RESOURCE_UNAVAILABLE)

        # P5: a caller's pin cannot choose the authority or its statement.
        if authoritative is not None and (
            (ruleset_ref is not None and ruleset_ref != authoritative)
            or (rules.ref is not None and rules.ref != authoritative)
        ):
            return result("unknown", Reason.REF_MISMATCH)

        fresh = (
            rules.watch_continuous is True and rules.epoch_current is True
            and _fresh(
                observed_at=rules.observed_at, now=rules.now,
                max_observation_age=rules.max_observation_age,
                valid_until=rules.valid_until,
            )
        )
        # P6 and P9 describe current authoritative facts. Old invalid/absent
        # observations lose that status when stale, just like old active ones.
        if authoritative is not None and rules.ref is not None and rules.state == "invalid" and fresh:
            return result("deny", Reason.RULESET_INVALID)
        if authoritative is None or rules.ref is None or rules.state not in {"active", "absent", "invalid", "stale"}:
            return result("unknown", Reason.RULESET_UNAVAILABLE)
        if rules.state == "stale" or not fresh:
            return result("unknown", Reason.RULESET_STALE)
        if rules.state == "absent" or not any(rule.allow for rule in rules.rules):
            return result("deny", Reason.NO_AUTHORIZATION)
        if any(
            rule.resource == resource.resource and rule.action == action
            and any(matches(pattern, subject.subject) for pattern in rule.allow)
            for rule in rules.rules
        ):
            return result("allow", Reason.MATCH)
        return result("deny", Reason.NO_MATCH)
