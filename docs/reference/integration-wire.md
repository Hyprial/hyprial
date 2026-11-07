# T2 wire-contract integration

`hyprial.contracts.wire.evaluate_case` is the Python execution adapter for the
frozen, language-neutral corpus (27 cases as of 2026-08-25; three IRC-only
cases retired, see below). It is test infrastructure shipped with the package
so the same public gate can run against every integration SHA.

## Real module path

⚠️ **Updated 2026-08-25 (PAC NAMT3 664a7d314947, IRC codec removal — see
`docs/reference/proto.md` §6):** the IRC line codec (`hyprial.proto`) this section used to
describe has been deleted entirely; it had zero production callers (confirmed
before deletion). Envelope fixtures are now consumed directly as JSON — no
wire-format conversion step. `_message()` in `contracts/wire.py` reads
`messageId`/`conversationId`/`from.actorId`/`intent`/etc. straight off the
fixture dict and builds `InboxMessage` from them; `payload` is the envelope's
own canonical JSON, not IRC line bytes. Delivery cases still use
`InboxService` with its SQLite outbox/inbox, deduplication, FIFO, retry, DLQ
and durable ACK state — this part is unaffected, since it never depended on
IRC in the first place. The online/offline network boundary still uses
`MemoryDeliveryTransport`; live Zenoh transport behavior remains covered by
the existing isolated loopback tests. Three cases (formerly WG-026/027/030)
were retired because they existed solely to pin guards (a ChannelGateway
target guard and a hop-limit guard) that lived only inside the deleted IRC
codec and were never wired into any production send/receive path — retiring
them removed no production protection, since none existed.

The evaluator dispatches each declared behavior explicitly but does not read
`case["expect"]`. A regression test poisons that field and proves the result is
derived independently.

## Integration seams found

1. The runner existed but the `hyprial.contracts.wire` package did not. T2 adds the
   executable adapter and a full-corpus pytest gate.
2. No target-registry or Lark adapter module is present on current `dev`.
   WG-017..WG-025 therefore use deterministic registry inputs and a persistent
   SQLite reply-index fixture at the adapter boundary. This preserves #21/#32
   behavior without leaking future adapter policy into proto or inbox.
3. The merged proto slice carried repository-wide Ruff drift (imports, unused
   names and formatting). T2 applies only mechanical cleanup with no mapping
   behavior change.
4. The test dependency is an optional extra. The reproducible full gate is
   `uv run --locked --extra test pytest`, not bare `uv run pytest`.
5. E2E-005 publishes old and new version tags into a disposable bare remote,
   installs the old tag, then proves `hyprial daemon upgrade` selects the highest PEP 440
   tag and reports its exact tag plus commit. No branch participates.

## Gates

```sh
UV_CACHE_DIR=/tmp/hyprial-t2-uv-cache uv run --locked python contract/wire/golden/run-python.py
UV_CACHE_DIR=/tmp/hyprial-t2-uv-cache uv run --locked --extra test pytest
UV_CACHE_DIR=/tmp/hyprial-t2-uv-cache uv run --locked --extra test \
  ./contract/run-all.sh --hyprial .venv/bin/hyprial --doctor-schema sealed --isolated-only
uv run python contract/e2e/run-uv-install-flow.py
uv run python contract/e2e/run-daemon-restart-survival.py
uv run python contract/e2e/run-pin-restart-survival.py
uv run python contract/e2e/run-dual-instance-roundtrip.py
```
