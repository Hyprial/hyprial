# HTTP request client and grouped TS generator (H-02)

Status: prepared against the synthetic HTTP projection fixture. The final Agent
client, the served-OpenAPI regeneration and the real HTTP/daemon consumer are
still blocked on accepted H-01 (HTTP entry) and H-03 (write profile) in
`intg/gui-request-http`. This guide describes the implemented mechanism, not a
finished live integration.

## What exists

| Path | Role |
| --- | --- |
| `gui/src/client/transport.ts` | `HttpTransport`: one-attempt POST to `/daemon/request/<method>`, Bearer injection, budget/abort, envelope validation, typed errors, catalog digest pinning |
| `gui/src/client/errors.ts` | `TransportError`, `HttpError`, `DomainError`, `EnvelopeError`, `CatalogDriftError` |
| `gui/src/client/types.ts` | transport config, diagnostics, catalog and request option types |
| `gui/scripts/generate-client.mjs` | deterministic JSON-Schema → TypeScript generator and `--check` mode |
| `gui/tests/client/fixtures/http-openapi.generic.json` | synthetic HTTP projection (transport envelope, six methods, read and write profiles) |
| `gui/tests/client/fixtures/generated/` | committed generator output for the fixture; `client:fixture:check` proves re-generation has no diff |
| `gui/tests/client/agent-provider-feature.ts` | feature-api example: generated types in, view model out, mock/live switch |
| `gui/tests/client/*.test.*` | transport, generator and independent consumer tests (no React, no CLI text) |

The wire surface stays `POST /daemon/request/<canonical-method>`; the client
does not introduce a second REST route per capability.

## Generate from the served HTTP contract

The input must be the HTTP projection served by the request entry, not the
business MethodSpec OpenAPI. The generator accepts either the raw OpenAPI
document or `/openapi.json` wrapped in a transport envelope.

```bash
# Bearer comes from the local capability file; never paste it into a command
# that is logged, committed or sent to another agent.
curl -sS -H "Authorization: Bearer $HYPRIAL_HTTP_TOKEN" \
  "$HYPRIAL_HTTP_ENDPOINT/openapi.json" > /tmp/http-openapi.json

node gui/scripts/generate-client.mjs \
  --input /tmp/http-openapi.json \
  --out gui/src/client/generated \
  --source http-openapi.json

# CI / review: fail when the checked-in output would change
node gui/scripts/generate-client.mjs \
  --input /tmp/http-openapi.json \
  --out gui/src/client/generated \
  --source http-openapi.json --check
```

Rules enforced by the generator:

- Methods come from `POST /daemon/request/<method>` paths (or their
  `x-hyprial-method`), sorted deterministically. There is no five-method
  whitelist: a new path becomes `client.<namespace>.<method>` automatically.
- A method mounted elsewhere, a path/method mismatch, duplicate methods, a
  namespace that is also a method, a generated-identifier collision across
  namespaces, a non-identifier segment, a dangerous property name
  (`__proto__`, `prototype`, `constructor`), or a contract with no methods is
  rejected.
- `x-hyprial-mutates: true` plus `x-hyprial-retry: safe` is rejected; writes
  default to `retry: never`.
- Object schemas preserve `additionalProperties`: absent/`true` is open
  (`[key: string]: unknown`), a schema becomes a typed index, and `false` stays
  closed. Typed indexes are unioned with the named property types (plus
  `undefined` for optional properties) because TypeScript requires named
  properties to satisfy the index signature; this is a TS limitation, not a
  reason to tighten the business schema.
- Business types come from the 200 envelope's `result` schema; transport types
  stay owned by `gui/src/client/types.ts` and are not re-generated.
- Identity metadata is read from `x-hyprial-identity`, falling back to
  `x-hyprial-http-binding.identity` for projections that carry the binding
  object instead of the flat extension.
- Output is byte-stable for any JSON key order and is guarded by `--check`.

## Transport behaviour

```ts
import { HttpTransport } from '@/client';
import { createGeneratedClient } from '@/client/generated';

const transport = new HttpTransport({
  baseUrl: endpoint,                    // e.g. http://127.0.0.1:47831
  token: async () => readCapabilityToken(), // Bearer is injected here, never in a component
  requestBudgetMs: 10_000,
  expectedCatalogDigest: pinnedDigest,  // optional but recommended after catalog load
});
const client = createGeneratedClient(transport);

const agents = await client.agent.list({});
const grants = await client.agent.secret.list({ actor: 'agent:alpha' });
await client.agent.provider.logout({ actor: 'agent:alpha', provider: 'claude' });
```

- Auth is a transport concern. Components and the feature adapter pass business
  parameters only; the caller cannot override `Authorization`.
- The budget covers waiting for headers and reading the body. Timeout and caller
  abort share one `AbortSignal`; the signal is cleaned up on every path.
- Exactly one network attempt is made. Writes are never retried automatically,
  including on timeout, disconnect, catalog refresh or cancellation.
- Envelopes are validated (`schemaVersion`, `ok`, `method`, `requestId`,
  `dispatch`, `result`). A success envelope must report `dispatch: dispatched`
  and carry `result`; a malformed, mismatched or `unknown`-dispatch success is
  an `EnvelopeError`, never a business result.
- `dispatch` is reported, never guessed: `not-dispatched` before sending,
  `dispatched` when the server says the handler ran, `unknown` after a
  post-send failure. A timeout or disconnect after dispatch therefore reports
  `unknown`; the client never claims rollback or "definitely not sent".
- A pinned `X-Hyprial-Catalog-Digest` that differs from the response header, or
  a `409 CATALOG_MISMATCH`, raises `CatalogDriftError`; the browser cannot
  self-report a different catalog version.

| Failure | Type | Notes |
| --- | --- | --- |
| invalid method, missing token | `TransportError` (`configuration`) | nothing sent |
| budget exhausted / abort / disconnect | `TransportError` (`timeout`, `aborted`, `network`) | `dispatch: unknown` after send |
| 401, pre-dispatch 403/404/413/415/429 | `HttpError` | transport refusal; `status` retained |
| 400 and post-dispatch domain errors | `DomainError` | `code`, `data`, `dispatch` retained |
| invalid JSON/envelope/method/status | `EnvelopeError` | `dispatch: unknown` |
| digest drift / 409 catalog mismatch | `CatalogDriftError` | `expected`/`actual` retained |

## Feature api injection and mock/live switch

The reference frontend adapter keeps its business-result port and replaces the
temporary DTO mapping with generated types:

```ts
import { createLiveAgentProviderPort, createAgentProviderFeature }
  from '../path/to/agent-provider-feature';

const feature = createAgentProviderFeature({
  mode: 'live',          // or 'mock' with fixture data
  client: generatedClient,
});
const view = await feature.load('agent:alpha');
```

`gui/tests/client/agent-provider-feature.ts` is the executable example. It keeps
the HTTP envelope out of components, maps generated results to the view model,
and lets tests/dev choose a mock port without changing the feature contract.
Until the accepted agent projection exists, the example runs on the fixture
types; it does not add a second hand-maintained DTO source.

## Verification

```bash
cd gui
npm ci                                # Node 24 in CI
npx vitest run tests/client           # 51 focused tests
npm run typecheck
npm run client:fixture:check          # generator output has no diff
npm test                              # whole GUI unit suite
```

Isolated-shell note: the harness machine's default `node` may be newer than the
CI image. These checks were also run with `node@24.22.2`:

```bash
npx -y node@24 ./node_modules/vitest/vitest.mjs run tests/client
```

## Explicitly not proven

- Real served `/openapi.json` generation and no-diff against the H-03
  projection (H-01/H-03 are not accepted yet).
- A consumer over real Uvicorn HTTP plus an isolated daemon, including real
  bearer refusal, write side effects, CORS and browser behaviour.
- Browser/usrb component integration and Swagger UI. Those belong to H-01/H-03
  and HM-01.
