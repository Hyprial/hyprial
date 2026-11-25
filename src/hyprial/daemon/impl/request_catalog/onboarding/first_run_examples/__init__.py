# Decision record (2026-11-19, split at the repository line limit):
# - These first-run confirmation examples were split out of ``onboarding/examples.py``
#   because that file passed the 1000-line limit after dev's first-run block and
#   the first-login examples met in one file.  The cases are unchanged: the block
#   is moved verbatim, in the same order, with no rewording.
# - From ``examples.py`` this module takes the shared helper ``_case`` (private by
#   name, used across this package boundary on purpose) and the constants ``KEY``
#   and ``KEY_2``; nothing is renamed.  Everything else the block uses is its own.
# - ``examples.py`` imports nothing from this module.  The aggregate
#   ``ONBOARDING_EXAMPLES`` (eight account sets, then the four sets below, in that
#   order, which is observable in the OpenAPI export and the contract mock) is
#   assembled in ``onboarding/__init__.py``, the one place that can import both
#   without a cycle; ``request_catalog/registry.py`` takes it from there.
"""First-run confirmation examples of the M2 onboarding MethodSpecs."""

from __future__ import annotations

from hyprial.kernel import MethodExampleError, MethodExampleSet, ipc_errors

from ..examples import KEY, KEY_2, _case

ATTEMPT = "att-0123456789abcdef"
SEND_KEY = "frs-0123456789abcdef0123456789abcdef"
AGENT = "agent:alice@node-1:assistant"
CONV = "c.0123456789abcdef"
REQUEST = "m.request-0123456789"
REPLY = "m.reply-0123456789"

PENDING_ATTEMPT = {
    "attemptId": ATTEMPT,
    "revision": 1,
    "state": "pending",
    "operationKey": KEY,
    "sendKey": SEND_KEY,
    "receiptRevision": 0,
    "loginEpoch": 3,
    "agentUri": AGENT,
    "agentCreatedAtMs": 1760000000000,
    "provider": "deepseek",
    "model": "deepseek-flash",
    "harness": "pi",
    "configRevision": None,
    "credentialKind": "key",
    "requestMessageId": None,
    "conversationId": None,
    "replyMessageId": None,
}

BOUND_ATTEMPT = {
    **PENDING_ATTEMPT,
    "revision": 2,
    "state": "bound",
    "requestMessageId": REQUEST,
    "conversationId": CONV,
}

CONFIRMED_ATTEMPT = {
    **BOUND_ATTEMPT,
    "revision": 3,
    "state": "confirmed",
    "receiptRevision": 1,
    "replyMessageId": REPLY,
}

RECORDED_FIRST_RUN = {
    "state": "recorded",
    "revision": 1,
    "confirmed": ["configuration", "reply"],
    "updatedAt": "2026-11-18T00:00:00Z",
    "skipped": False,
}

FIRST_RUN_BEGIN_EXAMPLES = MethodExampleSet(
    method="onboarding.firstRun.begin",
    cases=(
        _case(
            "begin-first-run-attempt",
            "success",
            {
                "operationKey": KEY,
                "agentUri": AGENT,
            },
            result={"ok": True, "attempt": PENDING_ATTEMPT},
        ),
        _case(
            "begin-native-attempt",
            "success",
            {
                "operationKey": KEY_2,
                "agentUri": AGENT,
            },
            result={
                "ok": True,
                "attempt": {**PENDING_ATTEMPT, "credentialKind": "native"},
            },
        ),
        _case(
            "begin-missing-agent",
            "invalid-params",
            {"operationKey": KEY},
            error=MethodExampleError(
                ipc_errors.INVALID_ARGUMENT, "agentUri is required"
            ),
        ),
        _case(
            "begin-configuration-changed",
            "domain-error",
            {
                "operationKey": KEY,
                "agentUri": AGENT,
            },
            error=MethodExampleError(
                "FIRST_RUN_CONFIGURATION_CHANGED",
                "the selected agent, configuration or credential is not current",
            ),
        ),
    ),
    empty_not_applicable="begin always returns the durable attempt",
)

FIRST_RUN_BIND_EXAMPLES = MethodExampleSet(
    method="onboarding.firstRun.bind",
    cases=(
        _case(
            "bind-authorized-request",
            "success",
            {
                "attemptId": ATTEMPT,
                "expectedRevision": 1,
                "operationKey": KEY,
                "conversationId": CONV,
                "requestMessageId": REQUEST,
            },
            result={"ok": True, "attempt": BOUND_ATTEMPT},
        ),
        _case(
            "bind-missing-attempt",
            "invalid-params",
            {
                "operationKey": KEY,
                "conversationId": CONV,
                "requestMessageId": REQUEST,
            },
            error=MethodExampleError(
                ipc_errors.INVALID_ARGUMENT, "attemptId is required"
            ),
        ),
        _case(
            "bind-unverified-request",
            "domain-error",
            {
                "attemptId": ATTEMPT,
                "expectedRevision": 1,
                "operationKey": KEY,
                "conversationId": CONV,
                "requestMessageId": REQUEST,
            },
            error=MethodExampleError(
                "FIRST_RUN_REPLY_UNVERIFIED",
                "the request does not carry this attempt's send association",
            ),
        ),
    ),
    empty_not_applicable="bind always returns the durable attempt",
)

FIRST_RUN_CONFIRM_EXAMPLES = MethodExampleSet(
    method="onboarding.firstRun.confirm",
    cases=(
        _case(
            "confirm-durable-reply",
            "success",
            {
                "attemptId": ATTEMPT,
                "expectedAttemptRevision": 2,
                "expectedReceiptRevision": 0,
                "operationKey": KEY,
            },
            result={
                "ok": True,
                "attempt": CONFIRMED_ATTEMPT,
                "firstRun": RECORDED_FIRST_RUN,
                "requestMessageId": REQUEST,
                "replyMessageId": REPLY,
            },
        ),
        _case(
            "confirm-missing-receipt-revision",
            "invalid-params",
            {
                "attemptId": ATTEMPT,
                "expectedAttemptRevision": 2,
                "operationKey": KEY,
            },
            error=MethodExampleError(
                ipc_errors.INVALID_ARGUMENT,
                "expectedReceiptRevision is required",
            ),
        ),
        _case(
            "confirm-attempt-conflict",
            "domain-error",
            {
                "attemptId": ATTEMPT,
                "expectedAttemptRevision": 2,
                "expectedReceiptRevision": 0,
                "operationKey": KEY,
            },
            error=MethodExampleError(
                "FIRST_RUN_ATTEMPT_CONFLICT",
                "the attempt or receipt revision changed",
            ),
        ),
    ),
    empty_not_applicable="confirm always returns the durable attempt",
)

FIRST_RUN_CANCEL_EXAMPLES = MethodExampleSet(
    method="onboarding.firstRun.cancel",
    cases=(
        _case(
            "cancel-attempt",
            "success",
            {
                "attemptId": ATTEMPT,
                "expectedRevision": 2,
                "operationKey": KEY,
            },
            result={
                "ok": True,
                "attempt": {**BOUND_ATTEMPT, "revision": 3, "state": "cancelled"},
            },
        ),
        _case(
            "cancel-stale-attempt",
            "domain-error",
            {
                "attemptId": ATTEMPT,
                "expectedRevision": 1,
                "operationKey": KEY,
            },
            error=MethodExampleError(
                "FIRST_RUN_ATTEMPT_STALE", "the attempt revision is stale"
            ),
        ),
        _case(
            "cancel-missing-operation-key",
            "invalid-params",
            {"attemptId": ATTEMPT, "expectedRevision": 2},
            error=MethodExampleError(
                ipc_errors.INVALID_ARGUMENT, "operationKey is required"
            ),
        ),
    ),
    empty_not_applicable="cancel always returns the durable attempt",
)

FIRST_RUN_SKIP_EXAMPLES = MethodExampleSet(
    method="onboarding.firstRun.skip",
    cases=(
        _case(
            "skip-first-run",
            "success",
            {"expectedRevision": 0, "operationKey": KEY},
            result={
                "ok": True,
                "firstRun": {
                    "state": "recorded",
                    "revision": 1,
                    "confirmed": [],
                    "updatedAt": "2026-11-18T00:00:00Z",
                    "skipped": True,
                },
            },
        ),
        _case(
            "skip-stale-revision",
            "domain-error",
            {"expectedRevision": 2, "operationKey": KEY},
            error=MethodExampleError(
                "FIRST_RUN_REVISION_CONFLICT", "the first-run revision changed"
            ),
        ),
        _case(
            "skip-missing-operation-key",
            "invalid-params",
            {"expectedRevision": 0},
            error=MethodExampleError(
                ipc_errors.INVALID_ARGUMENT, "operationKey is required"
            ),
        ),
    ),
    empty_not_applicable="skip records a durable user choice",
)
