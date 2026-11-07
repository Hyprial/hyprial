# user-proxy protocol v1

This directory freezes the packaged `user-proxy` child protocol
(`hyprial.daemon.impl.harnesses.python_worker._user_proxy_worker`; design:
`docs/design/design-user-proxy-harness.md`). The envelope, framing, and strictness are
the same as `contract/workers/python-worker/`: UTF-8 JSON objects, one LF each; CRLF,
unknown versions or fields, duplicate keys, non-finite numbers, and frames over
8 MiB are rejected.

Differences from jev:

- `ready` carries only `kind`, `pid`, `hyprialCommit`, `scriptSha256`: no model,
  no credential. The parent refuses a ready frame whose `scriptSha256` is not
  the packaged file.
- `call.payload` is `{from, message}`: the original sender and the text.
- An extra terminal frame, `forward {to, message}`, means "send this AS me
  to `to`". The **parent daemon** performs the send and settles the inbox row
  (accepted: ack; unresolvable address: `FORWARD_TARGET_UNKNOWN`, permanent;
  send fault: retried). The child never holds a send path.
- Concurrency is exactly 1 (sequential): a relay must keep order. A second
  `call` while one is live is a protocol failure.

The forwarding rule is a pure function of `from`, `message`, and `--route`:

| `from` | output |
| --- | --- |
| exactly `adapter:lark:<adapter of --route>`, the identity that adapter stamps on the person's messages | `@<recipient> <text>` → `forward {to: recipient, message: text}`; no marker or no text → `result {message: hint}` |
| anyone else | `forward {to: --route, message: unchanged}` |

`validate.py` checks the fixtures against the schema, every negative against the
framing and the state table, and then runs the real packaged worker on the fixture
requests and requires the fixture responses (with `durationMs` and the hint text
normalized), so the corpus cannot drift from the implementation.
