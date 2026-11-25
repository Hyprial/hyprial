# Common HTTP parameter constraints handoff

This common package supports chat contract preparation without changing existing
IPC behavior or activating a product route. It follows common-interface-v1 and
the staged M1/M2 gates.

## Contract author interface

Keep the existing legacy parameter schema and authoritative handler error order.
For HTTP-only requirements, declare for example:

```python
HttpBinding(
    identity=existing_identity_binding,
    params_constraints={
        "type": "object",
        "properties": {"idempotencyKey": {
            "type": "string", "minLength": 1, "pattern": r"\S",
        }},
        "required": ["idempotencyKey"],
    },
)
```

The property must already exist in the IPC schema and cannot be host-only.
This extension does not select caller identity, invent keys, reorder daemon
handlers, retry writes, or validate message.send business semantics itself.
Chat owns the real handler compatibility matrix, examples and negative tests;
common owns registry/transport assembly and generated output integration.

The common projection rejects empty/annotation-only declarations and obvious
duplicates or weaker type/bound rules unless a newly required field supplies the
restriction. It does not solve arbitrary JSON Schema implication. Non-widening
is guaranteed by intersection, while the method's negative examples must prove
its intended HTTP-only restriction. Formats known to the default
`jsonschema.FormatChecker()` count as executable validation, matching HTTP,
Contract Mock and exported HTTP example validation. Unknown formats remain
annotations and do not supply a restriction on their own.

## Owned files

- `src/hyprial/kernel/impl/contracts/daemon/http_params/__init__.py`: pure narrowing projection.
- `src/hyprial/kernel/impl/contracts/daemon/method_spec.py`: immutable binding,
  declaration validation and OpenAPI export; public exports through kernel interface.
- `src/hyprial/shell/impl/request_http/catalog.py`: consumes the same projection,
  fail-closed malformed declarations and digest pinning.
- `contract/daemon-request/README.md`: wire extension and compatibility contract.

The HTTP listener, generated client and Contract Mock already consume the
projected schema. No per-feature transport or second business entry is added.
`message.send` now opts in and is registered with the other declared GUI write
methods; that registration intentionally changes OpenAPI, generated clients and
catalog digests. Methods using the legacy handler admission policy keep their
IPC schemas unchanged while their HTTP bodies remain strict.

## Verification boundary

Focused probes cover optional IPC key versus required/nonblank HTTP key,
unchanged handler error, immutable metadata, non-relaxation, host-only exclusion,
invalid reference rejection, digest change, real loopback HTTP refusal with zero
backend dispatch, unknown writes without retry, and Mock parity. The listener
test injects a test backend callback; it is not real message.send/daemon evidence.
Chat registration is present in this change; ordinary GUI assembly, first-run,
real daemon/browser acceptance and wheel remain pending.
