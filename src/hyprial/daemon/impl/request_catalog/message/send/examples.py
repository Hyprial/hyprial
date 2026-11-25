"""Synthetic message.send outcomes for declaration and Mock checks."""

from hyprial.kernel import MethodExample, MethodExampleError, MethodExampleSet, ipc_errors

_REQUEST = {
    "to": ["agent:alice:node:worker"],
    "message": "hello",
    "summary": "hello",
    "conversationId": "conversation-1",
    "idempotencyKey": "frozen-operation-1-extended",
}

MESSAGE_SEND_EXAMPLES = MethodExampleSet(
    method="message.send",
    cases=(
        MethodExample(
            name="non-object-params",
            kind="invalid-params",
            description="the registry rejects non-object params before legacy admission",
            params=[],
            error=MethodExampleError(
                ipc_errors.INVALID_ARGUMENT,
                "message.send params must be an object",
            ),
        ),
        MethodExample(
            name="accepted-agent-send",
            kind="success",
            description="one durable agent message is accepted",
            params=_REQUEST,
            result={
                "ok": True,
                "actingPrincipal": "user:alice",
                "operationId": "frozen-operation-1-extended",
                "conversationId": "conversation-1",
                "messageId": "message-1",
                "deliveries": [{
                    "target": "agent:alice:node:worker",
                    "messageId": "message-1",
                    "conversationId": "conversation-1",
                    "accepted": True,
                    "queued": False,
                }],
            },
        ),
        MethodExample(
            name="accepted-send-without-conversation-id",
            kind="success",
            description=(
                "an HTTP send may omit conversationId; it joins the default "
                "conversation of the owner and target and the response returns that id"
            ),
            params={key: value for key, value in _REQUEST.items() if key != "conversationId"},
            result={
                "ok": True,
                "actingPrincipal": "user:alice",
                "operationId": "frozen-operation-1-extended",
                "conversationId": "dc.example-default-conversation",
                "messageId": "message-1",
                "deliveries": [{
                    "target": "agent:alice:node:worker",
                    "messageId": "message-1",
                    "conversationId": "dc.example-default-conversation",
                    "accepted": True,
                    "queued": False,
                }],
            },
        ),
        MethodExample(
            name="summary-too-long",
            kind="domain-error",
            description="the existing send boundary caps summaries at 512 UTF-8 bytes",
            params={**_REQUEST, "summary": "界" * 171},
            error=MethodExampleError(
                ipc_errors.SUMMARY_REQUIRED, "summary exceeds the byte limit",
                {"summaryBytes": 513, "maxBytes": 512},
            ),
        ),
    ),
    empty_not_applicable="a send produces deliveries or a domain error",
)

_KEY_REQUEST = {"operationKey": "a" * 43}

MESSAGE_SEND_KEY_LOOKUP_EXAMPLES = MethodExampleSet(
    method="message.send.key_lookup",
    cases=(
        MethodExample(
            name="accepted-durable-copy", kind="success",
            description="one durable sender copy exists for the authenticated sender",
            params=_KEY_REQUEST,
            result={
                "status": "accepted", "commandId": _KEY_REQUEST["operationKey"],
                "conversationId": "conversation-1", "messageId": "message-1",
            },
        ),
        MethodExample(
            name="not-found", kind="success",
            description="absence does not prove the original send was refused",
            params=_KEY_REQUEST,
            result={"status": "unknown", "reason": "not-found"},
        ),
        MethodExample(
            name="invalid-record", kind="success",
            description="ambiguous or damaged sender copies stay unknown",
            params=_KEY_REQUEST,
            result={"status": "unknown", "reason": "invalid-record"},
        ),
        MethodExample(
            name="unavailable", kind="success",
            description="a read failure does not permit a resend",
            params=_KEY_REQUEST,
            result={"status": "unknown", "reason": "unavailable"},
        ),
        MethodExample(
            name="non-object-params", kind="invalid-params",
            description="the registry rejects non-object params before legacy admission",
            params=[],
            error=MethodExampleError(
                ipc_errors.INVALID_ARGUMENT,
                "message.send.key_lookup params must be an object",
            ),
        ),
    ),
    empty_not_applicable="every query returns accepted or unknown",
    domain_errors_not_applicable="identity refusals require a live authenticated session",
)
