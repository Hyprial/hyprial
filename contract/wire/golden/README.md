# Wire golden corpus

This directory freezes TS Harness Bridge behavior as language-neutral JSON:

- `envelopes.json` contains complete version-1 wire envelope fixtures.
- `expectations.json` describes routing, receipt, acknowledgement, retry,
  deduplication, queueing, correlation, and error outcomes.
- Every expectation names its TS source test and/or specification section.
- `validate.py` checks corpus integrity without importing either implementation.

Run the structural gate:

```sh
uv run python contract/wire/golden/validate.py
```

Run the same cases against the Python implementation adapter:

```sh
PYTHONPATH=src uv run python contract/wire/golden/run-python.py
```

The T2 integration adapter executes all 35 cases through the Python proto and
inbox stack. Registry and Lark-specific route state remain explicit environment
fixtures until their owning product modules land. The adapter never reads a
case's `expect` value when producing its actual result.

The corpus intentionally separates wire data from the transport technology.
TS executes these semantics over NATS/JetStream; Python over zenoh-backed
inboxes. Tests compare declared observable outcomes, not private storage.

`ack-control` records the future IRC `TAGMSG +hyprial/ack` mapping from
`docs/design/design.md` while its expectations remain the current public
`message.ack`/`hyprial message ack` behavior. Adapter context under `content.metadata` is
golden input for #21/#32 routing; it is not an invitation to put adapter policy
inside the transport layer.

WG-030..WG-037 (see `contract/wire/send-links.md` §2/§4/§5/§8) freeze the
summary/body split, hold results, read_body, settle_hint and ingress refusal.
WG-030/WG-031 are `validate.py`'s `NEGATIVE_SUMMARY_FIXTURES`; WG-001/002/004/005
renamed `autoReply` to `replyOwed`. Those and the new cases are
`hyprialOnly: true` and cite the design document, not the TS reference.
