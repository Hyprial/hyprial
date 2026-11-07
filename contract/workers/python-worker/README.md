# Python worker protocol v1

This directory freezes the packaged `jev` child protocol. Frames are UTF-8
JSON objects terminated by one LF; CRLF, arrays/scalars, unknown versions, and
frames larger than 8 MiB are rejected. The parent owns admission and inbox
custody. The child owns only TypeSafe calls and bounded worker execution.

`protocol.schema.json` covers the stable frame envelope and
`fixtures/success.json` is a redacted, credential-free round-trip fixture.
Run `python validate.py` (or `contract/run-all.sh`) to validate the corpus.

Wire state is linear per child generation:

`starting -> ready -> accepting -> draining -> exited`.

Only `call` is accepted in `accepting`; `stop` changes the state to `draining`,
rejects further input, waits for accepted calls, and exits zero. A frame before
`ready`, duplicate ready, duplicate live id, unknown terminal id, duplicate
terminal, CRLF, non-finite JSON, duplicate object keys, or an oversized frame is
a protocol failure. The parent has one stdout reader and correlates every frame
by delivery id; the child never owns an inbox row or a second unbounded queue.

Metric timing is monotonic and segment-local: `decode` covers parent-frame
decode plus payload validation, `call` covers SDK submission through terminal
SDK result/error, and `send` covers placement of the terminal `HarnessResult` in
the parent result queue. Decode failure still emits `call` with `skipped: true`
and `durationMs: 0`, followed by `send`; local log writes remain fail-open.

The state table is normative for the negative corpus:

| state | input | result | stable failure |
| --- | --- | --- | --- |
| `starting` | one `ready` | enter `ready` | any other frame: `HARNESS_START_FAILED` |
| `ready` | fresh `call` | enter `accepting` | duplicate/unknown frame: `HARNESS_TRANSIENT_FAILURE` |
| `ready` or `accepting` | `stop` | enter `draining` | extra fields: `HARNESS_TRANSIENT_FAILURE` |
| `accepting` | fresh `call` with active count `<10` | stay `accepting` | — |
| `accepting` | fresh `call` with active count `==10` | reject and stop generation | `HARNESS_TRANSIENT_FAILURE` |
| `accepting` | terminal for a reserved id | release execution slot; retain id until drain | duplicate/unknown terminal: `HARNESS_TRANSIENT_FAILURE` |
| `draining` | terminal accepted calls then EOF | exit zero after drain | new call/duplicate stop: `HARNESS_TRANSIENT_FAILURE` |
| any live state | CRLF, duplicate key, non-finite number, oversized/no-LF frame | stop generation | `HARNESS_TRANSIENT_FAILURE` |

The negative fixture and validator exercise every row and malformed-frame class:
ready-before-call, duplicate ready, duplicate live id, unknown terminal id,
duplicate terminal, CRLF, non-finite JSON, duplicate object keys, the 8 MiB
byte/LF limit, the eleventh call, stop followed by call, and EOF before ready or
with an active call. This table supplements the missing S:650-654 detail; if a
later spec revision conflicts, the review decision supersedes it.
