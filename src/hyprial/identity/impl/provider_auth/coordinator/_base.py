from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hyprial.identity.impl.agents.runtime.context import AgentRuntimeContext

from typing import Any
from typing import Callable
from hyprial.identity.impl.provider_auth.helper import DeviceCodeAnnouncement
from hyprial.identity.impl.provider_auth.helper import HelperOutcome
from pathlib import Path
from typing import Protocol
from hyprial.identity.impl.provider_auth.classification import ProviderFailureClass
from datetime import UTC
from dataclasses import dataclass
from datetime import datetime
from dataclasses import field
import threading

"""Provider-auth coordinator: failure in, one login flow and one alert out.

Spec: ``spec-provider-oauth-relogin-alert-2026-09-14.md`` (+ 追加 1).  The
whole feature lives behind four injected seams, so tests run the real state
machine against a real ``UserProfileStore`` on a tmp file, and only stub the
two process boundaries (helper runner, owner notifier):

* classification — ``classification.py``, closed tables;
* marking — the existing ``UserProfile.runtime_capability`` slot, now a
  **diagnostic record only** (2026-09-21): ``dispatch.matrix`` no longer
  filters on it and no dispatch decision reads it, so a marked provider is
  still selected.  It is kept because the restore scan, the doctor view and
  the recovery notification all key on it;
* relogin — ``helper.DeviceLoginRunner`` spawning pi's own login code;
* alerting — ``autoupdate.alert.notify_owner``, the existing owner channel.

Never raises
------------
Same contract as ``autoupdate.alert``: this runs inside the delivery pump's
failure path.  A raise there would turn "a worker's provider auth died" into
"the pump died", which is strictly worse.  Every public method captures and
logs instead.

Episodes and rounds (追加 1)
----------------------------
One *episode* per provider = one broken stretch.  An episode issues at most
``MAX_ROUNDS`` device codes; each expiry starts the next round; after the
last one, exactly one closing notice is sent and the episode goes quiet.
A new episode is started only by (a) the next A-class failure or (b) daemon
restore finding a provider still marked broken with no flow in flight — the
restore re-trigger exists because a broken provider keeps producing failures
and the episode record is what bounds login rounds; without (b) a daemon
restart mid-episode would leave the record behind (2026-09-21: the mark no
longer suppresses dispatch, so it is not what makes failures stop arriving).

Recovery has two detectors, because the owner can fix this two ways:
the helper's own ``login()`` resolving (auto flow), or a manual
``pi → /login`` landing a fresh credential in auth.json while we were not
looking — reconciled on the next trigger before any new flow is started.
"""
REASON_PROVIDER_AUTH_INVALID = "provider-auth-invalid"
REASON_PROVIDER_AUTH_ACCOUNT = "provider-auth-account"
MAX_ROUNDS = 3
RELOGIN_COOLDOWN_SECONDS = 2 * 3600
_CREDENTIAL_LIVE_FLOOR_SECONDS = 300
_NOTICE_HISTORY_CAPACITY = 256
class OwnerNotifier(Protocol):
    def __call__(self, text: str, *, idempotency_key: str) -> Any: ...
class HelperRunner(Protocol):
    def __call__(
        self,
        provider: str,
        on_device_code: Callable[[DeviceCodeAnnouncement], None],
        *,
        stop: threading.Event | None = None,
    ) -> HelperOutcome: ...
class RuntimeHelperFactory(Protocol):
    def __call__(self, context: "AgentRuntimeContext") -> HelperRunner: ...
class RuntimeContextValidator(Protocol):
    def __call__(self, context: "AgentRuntimeContext") -> bool: ...
@dataclass(frozen=True, slots=True)
class AuthNoticeIntent:
    episode_key: str
    episode_id: str
    round_no: int
    kind: str
    text: str
    idempotency_key: str
@dataclass
class _Episode:
    """One provider's broken stretch.  ``round`` counts issued codes."""

    provider: str
    key: str
    episode_id: str
    kind: ProviderFailureClass
    auth_path: Path
    runner: HelperRunner
    mark_capabilities: bool = True
    workers: set[str] = field(default_factory=set)
    round: int = 0
    inflight: bool = False
    closing_sent: bool = False
    closing_pending: bool = False
    failure_alerted: bool = False  # helper-machinery failure, once per episode
    failure_alert_pending: bool = False
    sample: str = ""  # verbatim cause text (追加 2: alerts carry the cause)
    exhausted_at: float | None = None  # when the closing notice went out (S2)
def _utc_now() -> datetime:
    return datetime.now(UTC)
def _iso_now() -> str:
    return _utc_now().isoformat().replace("+00:00", "Z")
