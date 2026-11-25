# Agent line integration handoff

Implementation base: `intg/gui-live-0554@92b9f88b1bfcebb78b73cde0978dbad38bb42b77`.
Merged forward to container `d92f2f07aa677fd9b16379784bc00c6c43a03b9f`,
including the earlier `9a2c8f9f8173d84e07c4e2f7a35c9cc9e7e09d5a` chat C0 snapshot,
including common interface v1 at `546a8346bdf39f51b8f801ec6438cbe98a3f59ae`;
`gui/src/client/generated` exports `GeneratedClient`. The generated HTTP projection
contains 68 methods; this feature consumes the eleven in `AGENTS_CLIENT_METHODS`.

`createAgentsApi(client?: AgentsClient): AgentsApi` accepts
`Pick<GeneratedClient, 'agent' | 'org' | 'routine' | 'workflow'>`. It only converts
UI inputs and projects generated results. The host owns the transport, catalog,
principal, session, endpoint and backend mode. No feature factory creates a
transport/client, reads configuration or chooses a backend.

The method set is agent.list/create/start/stop, agent.secret.list,
agent.provider.default, agent.home-census, routine.list/status and
workflow.list. Provider login/logout remain with the user-setting line; chat API
and controller integration remain with C1. This package adds no backend method.

## Registration patch for the common line

The common owner must replace the legacy agent HTTP factory import in
`app/capabilities.tsx` with `createAgentsApi`, then inject the same generated client
used by the other feature adapters:

```tsx
import { createAgentsApi } from '../features/agents';

const agents = createAgentsApi(client);
const capabilities = { ...otherCapabilities, agents };
```

Pass `connection.client` from the frozen public `app/backend` interface v1.
Construct this adapter once per client, at common composition or in `useMemo`.
On principal, catalog or transport-generation replacement, construct a new client
and adapter. Retire the old connection synchronously through the common owner;
retained adapters preserve BACKEND_CONNECTION_RETIRED and its zero-attempt diagnostics.
`ContactsPage` drops the previous adapter's directory rows immediately
and rebuilds its mutation gateway from the current adapter. Never mutate a client
in place across identities. Both daemon and contract-mock inject this same factory;
neither mode passes a fixture seed or local mutation gateway to the product page.

Keep `ContactsPage`, `AgentList`, `AgentDetail`, `AgentForm` and their existing tabs
as the shared views. Explicit component previews may supply a seed/gateway, while
a product composition never passes one. A method the served catalog does not
offer is absent from the adapter: the entry, control or section that would call
it is not rendered, and a call that is refused at use raises one toast through
`useNotifyUnavailable` with the control's visible label as subject, before any
dialog, file chooser, editor or busy state starts. A directory read failure
is distinct from a confirmed missing agent.

Test reference entrypoints compose an explicit generated client outside this feature.

## Error and runtime semantics

`AgentRequestError` retains code, dispatch, domain data and the original
`RequestDiagnostics` (requestId/catalogDigest/attempts included). Create/runtime
error outcomes also retain code/diagnostics. Missing-capability errors have no
invented daemon receipt or requestId. Unknown writes show the existing unknown
wording, issue no retry, and permit the existing readback callbacks; an accepted
start with `running=false` does not claim running. `accepted=false` is an error
outcome. The common runtime owns receipt recovery and any catalog/phase admission.

Inventory, provider configuration, credential readiness and observed running are
separate facts. Secret grants never establish connection/readiness. A successfully
read null default provider means no default configured, without claiming a usable
credential. Provider failures remain unknown; start refusals preserve backend codes.

## Evidence boundaries

`gui/tests/unified-backend/agents-test-client.ts` contains explicitly labelled
contract-shaped test doubles. Existing agent tests use those doubles; they are
component/adapter evidence, not live evidence. The isolated Python Contract Mock
roundtrip uses the real generated client and HttpTransport against Spec examples,
through the same adapter. It still does not prove a live daemon, authentication,
ordinary common runtime, browser journey, wheel or release acceptance. Those gates
remain with common composition and integrated verification.
