# Python tests

`kernel/identity/daemon/biz/shell` follow the five product domains. `integration/`
contains cross-domain and isolated runner scenarios; `tooling/` covers CI,
architecture, release and migration tooling. Shared builders live in `support/`,
fake services and data in `fixtures/`. Tests import one canonical `tests.*`
package; there are no legacy path forwarding modules.

The subject's authority, not its legacy filename prefix, selects a domain:
PAC/dispatch/OrgFS/squire, durable state and installer/update implementations
belong to `daemon/`; canonical user/profile stores belong to `identity/`;
logger/hooks/process primitives belong to `kernel/`; routine and usage belong
to `biz/`. CLI-only contracts remain in `shell/`, cross-domain scenarios in
`integration/`, and source-policy/mutation gates in `tooling/`.

The root `conftest.py` still applies home/network isolation and FD ownership to
the whole suite. Existing authority JSON files retain their path and bytes.

Run `uv run pytest tests` or a focused semantic directory. Refactor tool tests
require their independent `libcst==1.9.0` dependency and now live in
`tests/tooling/refactor`; unittest discovery uses `-s tests/tooling/refactor -t .`.
Do not infer an end-to-end PASS from successful collection or static gates.

The 2026-10-03 migration manifest and exact collection comparison are under
`docs/evidence/test-layout-refactor-2026-10-03/`. That receipt also lists the
remaining oversized test files; moving a file does not complete its split.
