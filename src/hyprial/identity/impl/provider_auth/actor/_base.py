from __future__ import annotations

from hyprial.identity.impl.provider_auth.coordinator._base import AuthNoticeIntent
from hyprial.identity.impl.provider_auth.helper import DeviceCodeAnnouncement
from hyprial.identity.impl.provider_auth.helper import HelperOutcome
from dataclasses import dataclass

"""Typed provider-auth admission with one episode writer and isolated helpers."""
@dataclass(frozen=True, slots=True)
class TurnAuthFailed:
    failure: str
    harness: str
    provider: str | None
    model: str | None
    worker: str
    runtime_context: object = None
@dataclass(frozen=True, slots=True)
class RestoreAuth:
    pass
@dataclass(frozen=True, slots=True)
class RunAuthHelper:
    operation_id: str
    episode_key: str
    episode_id: str
    round_no: int
    provider: str
@dataclass(frozen=True, slots=True)
class AuthCodeObserved:
    helper: RunAuthHelper
    announcement: DeviceCodeAnnouncement
@dataclass(frozen=True, slots=True)
class AuthHelperFinished:
    helper: RunAuthHelper
    outcome: HelperOutcome
@dataclass(frozen=True, slots=True)
class RetryAuthHelper:
    episode_key: str
    episode_id: str
@dataclass(frozen=True, slots=True)
class NotifyOwner:
    intent: AuthNoticeIntent
@dataclass(frozen=True, slots=True)
class NoticeAttemptOutcome:
    intent: AuthNoticeIntent
    delivered: bool
    current: bool = True
@dataclass(frozen=True, slots=True)
class AuthNoticeCompleted:
    intent: AuthNoticeIntent
    delivered: bool
@dataclass(frozen=True, slots=True)
class _DecisionWork:
    command: object
    credit_token: str | None = None
