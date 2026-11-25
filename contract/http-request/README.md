# HTTP request contract (H-01)

`http-contract.v1.json` is the single frozen corpus for the local HTTP request
entry and the future TypeScript client. It contains the closed error/status
map, the three-state `dispatch` rules, the request/response headers, the
endpoint inventory, JSON Schemas for the envelopes and catalog, and the
selector-binding semantics.

The error-envelope `code` field accepts the uppercase error-code grammar or the
frozen historical lowercase-kebab set shared with MethodSpec. The list and its
uppercase compatibility names are registered in
[`contract/daemon-request/README.md`](../daemon-request/README.md#historical-lowercase-error-codes);
`validate.py` fails if this corpus and the kernel set drift.

It is not a second method list: the method catalog and its JSON Schemas come
from the running daemon's `daemon.catalog` (`httpBinding` is a transport
binding, never an authorization claim). `generatedFrom` in the served catalog
records that authority. The production listener is FastAPI served by Uvicorn;
`GET /openapi.json` (Bearer-protected) describes the actual transport routes and
mirrors the daemon-declared request/result schemas per method.

HTTP/1.1 header names are case-insensitive. The server emits the documented
header names in lower case because Uvicorn normalizes ASGI response headers;
clients must compare them case-insensitively.

Validate the corpus (offline, no daemon/home/socket):

```sh
uv run python contract/http-request/validate.py
```

Captured real documents can be checked against the same corpus:

```sh
uv run python contract/http-request/validate.py --catalog catalog.json
uv run python contract/http-request/validate.py --response response.json
```

`contract/run-all.sh` runs the offline corpus check. Real daemon + HTTP
acceptance stays in `tests/integration/request_http/`; a corpus check is not a
substitute for that live evidence.

## Preview confirmation profile

HTTP preserves daemon-declared `confirmation=preview` / `execute` pairs. A
preview is a safe read with `preview` metadata (operation/expiry/summary fields,
bindings and a bounded TTL). Execute is a never-retried write, accepts only the
closed `{operationId}` body and cannot inject a caller selector or replacement
parameters. An execute route is omitted unless its declared `previewOf` names
an admitted preview route. Ordinary `confirmation=none` methods are unchanged.

The public catalog carries `preview` / `previewOf`; OpenAPI carries
`x-hyprial-preview` / `x-hyprial-preview-of`. These facts participate in both
declaration and HTTP contract digests. They describe the protocol, not an
authorization decision or an automatic confirmation. The daemon domain must
bind the trusted caller/session and resource version, atomically claim execution
and retain or reject uncertain outcomes according to its existing authority.
The HTTP entry creates no confirmation store and never retries an execute.

No product deletion method is exposed by this transport change alone. Synthetic
real HTTP/IPC acceptance is in
`tests/integration/request_http/confirmation/test_confirmation_http.py`; actual
Agent/platform deletion needs separate domain and GUI acceptance.

## Principal change in place (`local-gui`)

The `local-gui` principal is served by the GUI host inside its own process; the
standalone `python -m hyprial.shell.impl.request_http serve` entry refuses a
`local-gui` config (`CONFIG_INVALID`, exit 2) and serves `local-operator` only.
The config's former `transport` block is retired: a `transport` key in a config
file is refused as an unknown key (`CONFIG_INVALID`, exit 2), and `server.json`
carries no `transport` document.

When the daemon changes phase or account (initialize, logout, a restart under
another account) the entry keeps its process, endpoint and port. The bearer is re-issued in the
same step that installs the new principal, `server.json` (when the entry has a
listener) is replaced atomically (mode 0600) and the previous bearer is refused with 401 on every authenticated route from that step on. A
request admitted before the change is never dispatched under the new principal:
it is refused `DAEMON_UNAVAILABLE`, `not-dispatched`. While no daemon answers the
entry stays up and answers `DAEMON_UNAVAILABLE`. `HTTP_NOT_READY` with a phase
reason (`ACCOUNT_REQUIRED`, `BOOTSTRAP_ONLY`) is not transient: do not retry it.
