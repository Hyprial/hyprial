# HTTP request contract (H-01)

`http-contract.v1.json` is the single frozen corpus for the local HTTP request
entry and the future TypeScript client. It contains the closed error/status
map, the three-state `dispatch` rules, the request/response headers, the
endpoint inventory, JSON Schemas for the envelopes and catalog, and the
selector-binding semantics.

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
