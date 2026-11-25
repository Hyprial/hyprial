"""P3 first-run attempt service over injected narrow authority reads.

Decision record (web-auth-r3-p3-20261118):
- ``begin`` runs before the send and snapshots only non-secret facts the server
  reads itself: login epoch, agent incarnation/config, and Key/native metadata.
  It assigns the attempt's own send idempotency key; the GUI sends with that
  key through the existing send authority.
- ``bind`` accepts a request only when that authority returns the same sender,
  Agent recipient and conversation for the attempt's send key, so an older or
  differently addressed request cannot be rebound.
- ``confirm`` takes only attempt identity, revisions and the operation key.
  It re-reads the current epoch/configuration/credential, requires them to
  equal the begin snapshot, proves the reply by the exact Agent sender,
  trusted recipient, conversation and ``replyTo`` naming the bound request,
  then writes the receipt and marks the attempt.  A repeat after a
  partial two-file commit reads the receipt first and completes idempotently;
  a read never repairs or resends.
- Serving request threads share one service settlement lock.  Verification
  rechecks the attempt and current identity after reading the reply; a settled
  cancellation cannot leave a new confirmation receipt.  A receipt already
  committed by this attempt cannot be cancelled, and recovery must complete
  the receipt directory flush before reporting confirmation.
- Confirmation also holds the existing home identity transaction lock across
  the final generation check and both durable writes.  An active login/logout
  writer refuses confirmation; a later writer cannot cross its commit window.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import threading
from typing import Iterator, Protocol

from hyprial.daemon.impl.onboarding.first_run import (
    FIRST_RUN_CONFIGURATION_CHANGED,
    FIRST_RUN_REPLY_UNVERIFIED,
    FIRST_RUN_SUBJECT_REQUIRED,
    FirstRunAttempt,
    FirstRunConfiguration,
    FirstRunReply,
    FirstRunStore,
    FirstRunStoreError,
    FirstRunVerificationError,
    verify_first_run_attempt,
)
from hyprial.daemon.impl.onboarding.first_run_attempt import (
    FIRST_RUN_ATTEMPT_CONFLICT,
    FIRST_RUN_ATTEMPT_NOT_FOUND,
    FirstRunAttemptSelection,
    FirstRunAttemptSnapshot,
    FirstRunAttemptStore,
    FirstRunCredentialBinding,
)
from hyprial.daemon.impl.request_catalog.onboarding import FirstRunSnapshot
from hyprial.identity import IdentityTransactionBusy, IdentityTransactionLock, OnboardingSubject

CONFIRMED_STEPS = ("configuration", "reply")


class FirstRunAuthorityError(RuntimeError):
    """A narrow authority read could not prove the attempt."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class RequestBinding:
    message_id: str
    conversation_id: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class AgentSelection:
    agent_uri: str
    agent_created_at_ms: int
    provider: str | None
    model: str | None
    harness: str
    config_revision: str | None
    credential: FirstRunCredentialBinding


@dataclass(frozen=True, slots=True)
class FirstRunConfirmation:
    first_run: FirstRunSnapshot
    attempt: FirstRunAttemptSnapshot
    request_message_id: str
    reply_message_id: str


class LoginEpochReader(Protocol):
    def __call__(self, home: Path) -> int: ...


class AgentSelectionReader(Protocol):
    def __call__(self, agent_uri: str) -> AgentSelection | None: ...


class SendBindingReader(Protocol):
    def __call__(
        self,
        sender: str,
        recipient: str,
        conversation_id: str,
        idempotency_key: str,
    ) -> RequestBinding | None: ...


class ReplyReader(Protocol):
    def __call__(
        self,
        sender: str,
        recipient: str,
        request_message_id: str,
        conversation_id: str,
    ) -> FirstRunReply | None: ...


class FirstRunAttemptService:
    def __init__(
        self,
        *,
        home: Path,
        onboarding_root: Path,
        attempts: FirstRunAttemptStore,
        receipts: FirstRunStore,
        login_epoch: LoginEpochReader,
        select_agent: AgentSelectionReader,
        send_binding: SendBindingReader,
        reply_for: ReplyReader,
    ) -> None:
        self._guard = threading.RLock()
        self._home = Path(home)
        self._onboarding_root = Path(onboarding_root)
        self._attempts = attempts
        self._receipts = receipts
        self._login_epoch = login_epoch
        self._select_agent = select_agent
        self._send_binding = send_binding
        self._reply_for = reply_for

    def begin(
        self,
        subject: OnboardingSubject | None,
        *,
        operation_key: str,
        agent_uri: str,
    ) -> FirstRunAttemptSnapshot:
        with self._guard:
            if subject is None:
                raise FirstRunStoreError(FIRST_RUN_SUBJECT_REQUIRED, "a stable subject is required")
            selection = self._select_agent(agent_uri)
            if selection is None:
                raise FirstRunAuthorityError(
                    FIRST_RUN_CONFIGURATION_CHANGED,
                    "the selected agent, configuration or credential is not current",
                )
            epoch = self._login_epoch(self._home)
            if epoch < 1:
                raise FirstRunAuthorityError(
                    FIRST_RUN_CONFIGURATION_CHANGED,
                    "the current login generation is not available",
                )
            receipt = self._receipts.read(subject)
            return self._attempts.begin(
                subject,
                FirstRunAttemptSelection(
                    login_epoch=epoch,
                    agent_uri=selection.agent_uri,
                    agent_created_at_ms=selection.agent_created_at_ms,
                    provider=selection.provider,
                    model=selection.model,
                    harness=selection.harness,
                    config_revision=selection.config_revision,
                    credential=selection.credential,
                ),
                operation_key=operation_key,
                receipt_revision=receipt.revision,
            )

    def bind(
        self,
        subject: OnboardingSubject | None,
        *,
        trusted_sender: str,
        attempt_id: str,
        expected_revision: int,
        operation_key: str,
        conversation_id: str,
        request_message_id: str,
    ) -> FirstRunAttemptSnapshot:
        with self._guard:
            attempt = self._require_attempt(subject)
            if attempt.attempt_id != attempt_id or attempt.operation_key != operation_key:
                raise FirstRunStoreError(FIRST_RUN_ATTEMPT_CONFLICT, "the attempt does not match")
            binding = self._send_binding(
                trusted_sender, attempt.agent_uri, conversation_id, attempt.send_key,
            )
            if (
                binding is None
                or binding.message_id != request_message_id
                or binding.conversation_id != conversation_id
                or binding.idempotency_key != attempt.send_key
            ):
                raise FirstRunAuthorityError(
                    FIRST_RUN_REPLY_UNVERIFIED,
                    "the request does not carry this attempt's send association",
                )
            return self._attempts.bind(
                subject,
                attempt_id=attempt_id,
                expected_revision=expected_revision,
                operation_key=operation_key,
                request_message_id=request_message_id,
                conversation_id=conversation_id,
            )

    def confirm(
        self,
        subject: OnboardingSubject | None,
        *,
        trusted_sender: str,
        attempt_id: str,
        expected_attempt_revision: int,
        expected_receipt_revision: int,
        operation_key: str,
    ) -> FirstRunConfirmation:
        with self._guard, self._identity_fence():
            attempt = self._require_attempt(subject)
            if attempt.attempt_id != attempt_id or attempt.operation_key != operation_key:
                raise FirstRunStoreError(FIRST_RUN_ATTEMPT_CONFLICT, "the attempt does not match")
            receipt, last_operation_key = self._receipts.read_internal(subject)
            if all(step in receipt.confirmed for step in CONFIRMED_STEPS):
                return self._complete_existing(
                    subject, attempt, trusted_sender, last_operation_key, receipt,
                )

            if attempt.state != "bound":
                raise FirstRunStoreError(FIRST_RUN_ATTEMPT_NOT_FOUND, "the attempt is not bound")
            if (
                attempt.revision != expected_attempt_revision
                or attempt.receipt_revision != expected_receipt_revision
                or receipt.revision != expected_receipt_revision
            ):
                raise FirstRunStoreError(FIRST_RUN_ATTEMPT_CONFLICT, "the attempt or receipt revision changed")
            if attempt.request_message_id is None or attempt.conversation_id is None:
                raise FirstRunAuthorityError(FIRST_RUN_REPLY_UNVERIFIED, "the attempt has no request")

            reply = self._reply_for(
                attempt.agent_uri,
                trusted_sender,
                attempt.request_message_id,
                attempt.conversation_id,
            )
            self._require_current_attempt(subject, attempt)
            current = self._current_selection(attempt)
            verified = verify_first_run_attempt(
                attempt=FirstRunAttempt(
                    agent_uri=attempt.agent_uri,
                    conversation_id=attempt.conversation_id,
                    request_message_id=attempt.request_message_id,
                    configuration=_configuration_of(attempt),
                ),
                reply=reply,
                current=_configuration_of(current),
                trusted_sender=trusted_sender,
            )
            if current.credential != attempt.credential:
                raise FirstRunVerificationError(
                    FIRST_RUN_CONFIGURATION_CHANGED,
                    "the selected credential metadata changed",
                )
            completed = self._receipts.confirm_many(
                subject,
                steps=CONFIRMED_STEPS,
                expected_revision=expected_receipt_revision,
                operation_key=operation_key,
            )
            marked = self._attempts.mark_confirmed(
                subject,
                attempt_id=attempt.attempt_id,
                expected_revision=attempt.revision,
                operation_key=operation_key,
                reply_message_id=verified.message_id,
            )
            return FirstRunConfirmation(
                first_run=completed,
                attempt=marked,
                request_message_id=attempt.request_message_id,
                reply_message_id=verified.message_id,
            )

    def cancel(
        self,
        subject: OnboardingSubject | None,
        *,
        attempt_id: str,
        expected_revision: int,
        operation_key: str,
    ) -> FirstRunAttemptSnapshot:
        with self._guard:
            receipt, receipt_operation = self._receipts.read_internal(subject)
            if receipt_operation == operation_key and all(
                step in receipt.confirmed for step in CONFIRMED_STEPS
            ):
                raise FirstRunStoreError(
                    FIRST_RUN_ATTEMPT_CONFLICT,
                    "the attempt already committed its confirmation",
                )
            return self._attempts.cancel(
                subject,
                attempt_id=attempt_id,
                expected_revision=expected_revision,
                operation_key=operation_key,
            )

    def skip(
        self,
        subject: OnboardingSubject | None,
        *,
        expected_revision: int,
        operation_key: str,
    ) -> FirstRunSnapshot:
        with self._guard, self._identity_fence():
            return self._receipts.skip(
                subject,
                expected_revision=expected_revision,
                operation_key=operation_key,
            )

    def read(
        self,
        subject: OnboardingSubject | None,
    ) -> FirstRunAttemptSnapshot | None:
        with self._guard:
            return self._attempts.read(subject)

    def _require_attempt(self, subject: OnboardingSubject | None) -> FirstRunAttemptSnapshot:
        attempt = self._attempts.read(subject)
        if attempt is None:
            raise FirstRunStoreError(FIRST_RUN_ATTEMPT_NOT_FOUND, "no attempt exists")
        return attempt

    @contextmanager
    def _identity_fence(self) -> Iterator[None]:
        try:
            lock = IdentityTransactionLock.acquire(self._home, timeout=0)
        except IdentityTransactionBusy as error:
            raise FirstRunVerificationError(
                FIRST_RUN_CONFIGURATION_CHANGED,
                "an identity transaction is changing the current login generation",
            ) from error
        with lock:
            yield

    def _require_current_attempt(
        self, subject: OnboardingSubject | None, expected: FirstRunAttemptSnapshot,
    ) -> None:
        if self._require_attempt(subject) != expected:
            raise FirstRunStoreError(
                FIRST_RUN_ATTEMPT_CONFLICT, "the attempt changed during verification",
            )

    def _current_selection(self, attempt: FirstRunAttemptSnapshot) -> AgentSelection:
        current = self._select_agent(attempt.agent_uri)
        if current is None:
            raise FirstRunVerificationError(
                FIRST_RUN_CONFIGURATION_CHANGED,
                "the selected agent or credential is no longer current",
            )
        epoch = self._login_epoch(self._home)
        if (
            epoch != attempt.login_epoch
            or current.agent_created_at_ms != attempt.agent_created_at_ms
            or current.config_revision != attempt.config_revision
        ):
            raise FirstRunVerificationError(
                FIRST_RUN_CONFIGURATION_CHANGED,
                "the login generation or agent incarnation changed",
            )
        return current

    def _complete_existing(
        self,
        subject: OnboardingSubject | None,
        attempt: FirstRunAttemptSnapshot,
        trusted_sender: str,
        last_operation_key: str | None,
        receipt: FirstRunSnapshot,
    ) -> FirstRunConfirmation:
        if (
            last_operation_key != attempt.operation_key
            or receipt.revision != attempt.receipt_revision + 1
            or attempt.state not in ("bound", "confirmed")
            or attempt.request_message_id is None
            or attempt.conversation_id is None
        ):
            raise FirstRunStoreError(
                FIRST_RUN_ATTEMPT_CONFLICT,
                "the completed receipt was not written by this attempt",
            )
        reply = self._reply_for(
            attempt.agent_uri,
            trusted_sender,
            attempt.request_message_id,
            attempt.conversation_id,
        )
        self._require_current_attempt(subject, attempt)
        current = self._current_selection(attempt)
        if current.credential != attempt.credential:
            raise FirstRunVerificationError(
                FIRST_RUN_CONFIGURATION_CHANGED,
                "the selected credential metadata changed",
            )
        verified = verify_first_run_attempt(
            attempt=FirstRunAttempt(
                agent_uri=attempt.agent_uri,
                conversation_id=attempt.conversation_id,
                request_message_id=attempt.request_message_id,
                configuration=_configuration_of(attempt),
            ),
            reply=reply,
            current=_configuration_of(current),
            trusted_sender=trusted_sender,
        )
        receipt = self._receipts.confirm_many(
            subject,
            steps=CONFIRMED_STEPS,
            expected_revision=receipt.revision,
            operation_key=attempt.operation_key,
        )
        marked = (
            attempt
            if attempt.state == "confirmed" and attempt.reply_message_id == verified.message_id
            else self._attempts.mark_confirmed(
                subject,
                attempt_id=attempt.attempt_id,
                expected_revision=attempt.revision,
                operation_key=attempt.operation_key,
                reply_message_id=verified.message_id,
            )
        )
        return FirstRunConfirmation(
            first_run=receipt,
            attempt=marked,
            request_message_id=attempt.request_message_id,
            reply_message_id=verified.message_id,
        )


def _configuration_of(source: FirstRunAttemptSnapshot | AgentSelection) -> FirstRunConfiguration:
    return FirstRunConfiguration(
        provider=source.provider,
        model=source.model,
        harness=source.harness,
        grant_id=source.credential.grant_id,
        grant_revision=source.credential.revision or 0,
    )
