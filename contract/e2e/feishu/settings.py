"""Immutable E2E-004 vocabulary and frozen default values; not credentials."""

from __future__ import annotations
from hyprial.daemon.impl.adapters.lark import ActorTarget

APP_NAME = "allenwoods-hyprial-e2e"

APP_ID = "cli_aaf9ea19d4b8dbe4"

CHAT_ID_ENV_VAR = "HYPRIAL_E2E_LARK_CHAT_ID"

APP_ID_ENV_VAR = "HYPRIAL_E2E_LARK_APP_ID"

APP_SECRET_ENV_VAR = "HYPRIAL_E2E_LARK_APP_SECRET"

TEST_ROUTE_NAME = "py-test"

ACTOR = ActorTarget(
    actor_id="agent:e2e:codex:lark-contract",
    actor_key="e2e/codex/lark-contract",
    display_name="e2e/codex/lark-contract",
)

OUTBOUND_SENDER = "e2e004-outbound-driver"

POLL_SECONDS = 0.25

EVENT_TIMEOUT_SECONDS = 45.0

REACTION_TIMEOUT_SECONDS = 20.0

ASSERTIONS = (
    "A1-preflight",
    "A2-user-request-forwarded",
    "A3-onit-after-harness-accept",
    "A4-native-adapter-reply",
    "A5-onit-cleared-after-reply",
    "A6-native-followup-route",
    "A7-duplicate-event-silent",
    "A8-reply-route-survives-restart",
    "A9-provider-failure-retains-onit",
    "A11-richtext-post-roundtrip",
    "A12-file-attachment-roundtrip",
    "A13-image-attachment-roundtrip",
    "A16-outbound-file-route-roundtrip",
    "A14-merge-forward-not-dropped",
    "A15-unsupported-type-loud",
    # Listed in execution order, not numeric order: the sanitization sweep must
    # run last so it also covers the evidence the non-text assertions produce.
    "A10-evidence-sanitized",
)

ATTACHMENT_FILE_NAME = "hyprial-contract-note.txt"

TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)
