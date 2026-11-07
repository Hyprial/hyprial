# P3 Lark event-dispatch evidence

This note records the offline dispatcher checks and attended Python Lark
WebSocket validation. The final attempt received a real p2p message through
the approved E2E App; no production credential was printed or committed.

## Registration comparison

| Concern | TypeScript gateway | Python `lark-oapi==1.7.2` | Result |
|---|---|---|---|
| Event key | `im.message.receive_v1` | `register_p2_im_message_receive_v1` registers `p2.im.message.receive_v1` | Match |
| Dispatcher wiring | channel registers the event map, then calls `WSClient.start({ eventDispatcher })` | `EventDispatcherHandler` is passed as `ws.Client(..., event_handler=...)` | SDK-equivalent |
| Domain | `Domain.Feishu` -> `https://open.feishu.cn` | default `FEISHU_DOMAIN` -> `https://open.feishu.cn` | Match |
| App type | no app-type argument | no app-type argument | Match |
| Extra registration set | channel registers message plus optional card/reaction events | the adapter needs and registers the message event | No missing message registration |

The installed `ws/client.py` and `event/dispatcher_handler.py` hashes exactly
match the official `v1.7.2` tag. The adapter construction also matches the
[official WebSocket sample](https://github.com/larksuite/oapi-sdk-python/blob/v1.7.2/samples/ws/sample.py): build an empty-key dispatcher, register
`p2_im_message_receive_v1`, pass it as `event_handler`, then call `start()`.

## TS log golden

The approved read-only source was
`~/.hyprial/state/logs/lark-gateway.jsonl`. Rows matching the supplied chat report:

- `event=im.event` and `msg_type=text`;
- `chat_type=group`, not `p2p`;
- `mentioned_bot=false`;
- `route=silent:not-addressed`.

The JSONL row is normalized observability metadata, not the complete raw P2
payload: it does not retain event headers, sender, content, mentions, or the
identity of the App/gateway that observed the event. A row in this shared log
therefore does not prove that the App under test belongs to that chat. The
committed fixture preserves the observed event/chat/message shape but replaces
identifiers and supplies synthetic missing P2 fields. Feeding it through the
actual `LarkEventStream` dispatcher invokes the callback and normalizes
successfully. Adapter policy then intentionally ignores an unmentioned group
message, matching the TS route; no Harness envelope should be emitted for that
golden.

## Troubleshooting successful handshakes with zero events

The official SDK handshake passed. A later attended attempt also aligned a
main-thread Python listener (ready at 21:03:56 local time) with a human send at
21:04, but received no event frame. The supplied chat was subsequently shown
to be `chat_type=group`, not a p2p DM, and the test did not first establish that
the exact App under test was a member of that group. Zero event frames are the
expected result when an App is not in the target chat, so this attempt does not
establish a Python SDK subscription defect.

Before debugging SDK registration after a successful handshake with zero
events, validate the test itself in this order:

1. verify the exact App identity used by the listener;
2. verify the target chat's actual `chat_type` instead of relying on a human
   description such as "DM";
3. prove that this exact App is a member of or otherwise eligible to receive
   events from the target chat (a shared gateway-log row is insufficient);
4. use a human/user-originated message: a message sent by the App itself is not
   evidence for the `im.message.receive_v1` inbound subscription;
5. stop competing connections for the same App and verify there is one
   connection owner;
6. start the listener without a short callback timeout, confirm readiness,
   then send a unique nonce and capture
   one callback before restoring the other gateway.

The failed windows were invalidated by timing mismatch, App-originated sends,
or a group containing a different bot. Those zero-frame results did not
implicate the SDK.

## Attended real-environment result

With the exact `alicesmith-hyprial-e2e` App and a verified p2p chat, the main-thread
listener received `im.message.receive_v1` and the production normalizer built
the expected Harness request envelope:

- SDK WebSocket handshake: PASS;
- real inbound callback: PASS at `2026-08-07T13:49:02.554Z`;
- event shape: `msg_type=text`, `chat_type=p2p`;
- DM identity: `lark:<chat_id>:<chat_id>`;
- sender and provider metadata: preserved;
- message text: represented in committed evidence only by a 12-character
  SHA-256 prefix and length, not plaintext.

The one-shot listener exited after allowing the SDK event acknowledgement to
complete. This clears the P3 real-environment inbound-event gate; mock-driven
tests continue to cover outbound reply, reaction cleanup, and durable routing.
