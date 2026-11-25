# Feishu live roundtrip evidence

This file retains the first attended inbound-only evidence. The executable
eight-step contract is `run-feishu-roundtrip.py`; a passing inbound callback
alone is not a full roundtrip.

> Note (2026-08-11): the p2p DM chat id originally recorded here was redacted.
> A DM with the test App bot is a per-developer private conversation;
> DM chat ids must never be committed. The shared, reproducible E2E target
> is selected by the configured `py-test` route (see 04-feishu-roundtrip.md).
> Current runner output uses stable digest labels for chat/open-user identity;
> raw native identity values remain only in local configuration and live API
> calls.

On 2026-08-07, the approved `allenwoods-h2b-e2e` test App established an
official `lark-oapi==1.7.2` WebSocket connection and received a real
user-originated `im.message.receive_v1` event from a verified p2p chat.

Result: **[REAL ENV] PASS** for the SDK inbound and Harness-envelope conversion
leg.

```json
{
  "receivedAt": "2026-08-07T13:49:02.554Z",
  "native": {
    "messageId": "om_x100b686a5e30e8acb2be1bf1e6acf89",
    "chatId": "oc_<p2p-dm-redacted>",
    "chatType": "p2p",
    "messageType": "text"
  },
  "harness": {
    "messageId": "om_x100b686a5e30e8acb2be1bf1e6acf89",
    "from": "channel:e2e:lark:allenwoods-h2b-e2e",
    "to": "agent:e2e:codex:lark-live",
    "conversationId": "lark:oc_<p2p-dm-redacted>:oc_<p2p-dm-redacted>",
    "textDigest": "8a2c17401f26",
    "textLength": 8,
    "providerMetadata": {
      "provider": "lark",
      "messageId": "om_x100b686a5e30e8acb2be1bf1e6acf89",
      "chatId": "oc_<p2p-dm-redacted>",
      "chatType": "p2p",
      "messageType": "text",
      "senderType": "user",
      "replyTo": null,
      "rootId": null,
      "threadId": null
    }
  }
}
```

The committed evidence excludes App credentials, sender identity, and message
plaintext. This first capture did not exercise the outbound leg.

## Automated full-roundtrip evidence

The executable runner subsequently completed the full eight-step contract
twice with unique nonces. The final run used nonce
`1786114650-6ee6fa57` and completed at
`2026-08-07T14:58:15.155Z`:

| Assertion | Result | Evidence |
|---|---|---|
| Exact App/user preflight and SDK ready | PASS | ready `2026-08-07T14:57:34.588Z` |
| User request forwarded to target actor | PASS | native `om_x100b686b68cd60a4b28fbc3aa233679`; Harness `harness_1786114650-6ee6fa57_38711_1`; event `31a7a52a01429885521109f5466ffb33` |
| `OnIt` after Harness custody | PASS | Harness accepted `2026-08-07T14:57:36.402Z`; reaction action time `1786114660606` ms |
| Adapter native reply and headers | PASS | native `om_x100b686b6855fca0b2bc5a19d20dae2`; `reply_to=om_x100b686b68cd60a4b28fbc3aa233679`; delivery accepted `2026-08-07T14:57:45.591Z` |
| `OnIt` cleared after reply | PASS | original request reaction list became empty after delivery acceptance |
| Native follow-up route | PASS | native `om_x100b686b69d870a0b492cdc2a779561` routed to `agent:e2e:codex:lark-contract` |
| Duplicate replay | PASS | status `duplicate`; Harness request count remained one; plaintext absent |
| Reply route after gateway restart | PASS | gateway ready `2026-08-07T14:58:04.347Z`; native `om_x100b686b691330a0b3e2affbbd250cf` routed to the same actor |
| Forced provider failure | PASS | delivery rejected; Harness not accepted; `OnIt` retained; cleanup calls zero |
| Evidence sanitization | PASS | App secret absent; access token not logged; message plaintext absent |

The runner used `LarkAdapter.handle_delivery` rather than a raw SDK send. The
observed outbound order was correlation lookup, native reply, durable reply
index write, Harness delivery acceptance, then best-effort `OnIt` cleanup.

## Troubleshooting zero-event handshakes

Validate these conditions before changing SDK registration:

1. the listener uses the exact intended App identity;
2. the target's actual `chat_type` matches the test claim;
3. that exact App belongs to or can receive events from the target chat;
4. the test message is user-originated, not sent by the App itself;
5. no competing connection for the same App can split delivery;
6. listener readiness and the unique test send overlap without a short timeout.
