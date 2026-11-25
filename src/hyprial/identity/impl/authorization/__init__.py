"""The sole identity authorization namespace, for trusted daemon composition.

The pure mechanism consumes verified typed observations. Production producers,
owner signature verification and daemon runtime integration are not wired here.
"""

from .evaluator import AuthorizationEvaluator
from .ports import (
    Decision, EvaluatorPort, OwnerAuthorization, ResourceObservation,
    RuleSetObservation, RuleSetRef, Subject, SubjectObservation, SubjectPattern,
)


class AuthorizationMechanism:
    SubjectPattern = SubjectPattern
    Subject = Subject
    SubjectObservation = SubjectObservation
    OwnerAuthorization = OwnerAuthorization
    ResourceObservation = ResourceObservation
    RuleSetRef = RuleSetRef
    RuleSetObservation = RuleSetObservation
    Decision = Decision

    @staticmethod
    def compose_evaluator() -> EvaluatorPort:
        return AuthorizationEvaluator()


__all__ = ["AuthorizationMechanism"]
