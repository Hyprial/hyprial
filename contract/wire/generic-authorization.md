# Generic authorization mechanism fixtures (M1, 0.5.5.2)

Approved source: `docs/design/zenoh-generic-authorization-contract-0552.md`
at `12051df301d245fcf4a69408ee3fe26b64ca8270`, file SHA256
`848bc476cb1035dae0fe469e8bed22dc2b1c2f4e0f676275bfea11c2fa681420`.
This document records mechanism fixture conventions. It defines no business
denial envelope, invite command, move flow, or session enforcement claim.

The shared directory `contract/fixtures/generic-authorization/` contains:

| File | Input and oracle |
| --- | --- |
| `subjects.json` | Strict three-segment stable IDs, literal/full-segment wildcard matching, invalid encodings and concrete subjects |
| `rules.json` | Raw JSON token streams, atomic schema acceptance/rejection and immutable decoded rule counts |
| `decisions.json` | Trusted synthetic observations, P0–P11 outcomes/reasons and exact resource/action intersections |
| `lifecycle.json` | All fourteen approved runtime event scenarios, including owner proof distinct from host residency |

Each file has `fixtureVersion: 1`, `kind`, and uniquely named `cases`.
The original M1 examples remain in these files. Additional cases exercise
strict parsing, limits, matching boundaries, and priority combinations.

Subjects and patterns use the kernel's position-specific `stable-id-v1`
predicates. Patterns accept exactly three segments. The two-segment author
shorthand is rejected by this mechanism. JSON escapes are decoded before
subject validation; there is no percent/backslash decoding, trim, case fold,
Unicode normalization, partial glob, or regex engine.

`rules.rawJson` must reach the decoder as UTF-8 bytes. Parsing into an ordinary
dictionary first loses duplicate-key evidence and is not conformant. One
invalid item invalidates the complete authorized ruleset. Empty rules or allow
arrays are valid and grant no authorization. Resource/action strings compare
exactly; subject wildcards never widen their boundaries.

`decisions.base` is copied, then each case's `set` replaces only named top-level
fields. Nested merging and unknown overrides are invalid fixture consumption.
The test adapter builds typed internal observations; this input is never a
request schema and its state fields prove no production identity. The adapter
constructs the complete authoritative reference from scope, declaration
location, opaque version, SHA256 of the original fixture bytes, schema and
profile. A requested version is a pin only. Every decision compares the full
authoritative reference, including mismatched-pin cases.
`observedVersion/Scope/DeclarationId/Digest/Profile` change only the rule
observation reference; the resource's
`authority_ref` remains authoritative, so a cached rule cannot select its own
authority. A missing request pin is passed as `None`.

Age/watch/epoch changes in these decision cases concern the rules observation.
Subject and resource observations are fresh; runtime freshness coverage remains
separate. `outcome` and `reason` are internal fixture oracles, not public wire
fields or business error codes. Only explicit `allow` authorizes an operation.
Expired or discontinuously observed `invalid/absent` facts are unknown/stale;
their former deny result is not an indefinitely current authorization fact.

Lifecycle events describe isolated, registered source/verifier results. Host
residency alone cannot assert another owner's agent. An owner's signature must
cover the owner/host/agent tuple with independently trusted owner-key provenance.
Each case starts from its own runtime and may not auto-repair a negative input.
`internal_activate` is a test-only internal step, never a public load API.

Initial Python consumers are `tests/identity/authorization/`. The lifecycle file
is a shared planned runtime oracle in this batch; fixture integrity checks do
not claim its runtime events have executed. Actual OrgFS/UUID/session/signature
integration remains separately evidenced or BLOCKED.

Missing API/import/setup is capability evidence and is not behavior RED. The
adapter deliberately delays missing API resolution until each test call, so
all cases collect before the implementation exists. Behavior RED must be
recorded after callable product capabilities exist, using the same fixtures.
