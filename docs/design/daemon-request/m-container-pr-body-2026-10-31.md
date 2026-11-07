# M container — pull request body (maintained copy) — 2026-10-31

Everything in this file *is* the body to submit for `intg/gui-request-examples`.
The historical drafts stay in `docs/notes/hm-h00-m-container-draft-pr-2026-10-31.md`
so the review trail is not rewritten; this file is the one to keep current.

---

## Why

The five Agent methods this round are `agent.list`, `agent.secret.list`,
`agent.provider.default`, `agent.provider.login` and `agent.provider.logout`.
Org examples and any wider frontend scope stay deferred.

They need an integration target of their own:

- they are declarations of the daemon request catalog, not of the H FastAPI
  transport;
- H's runtime must not be merged into M just to obtain declaration types;
- M must stay usable through CLI/IPC without any HTTP server running.

`hm-pac-activation-2026-10-31.md` therefore splits the work into an H container
(`intg/gui-request-http`, PR #2144) and this M container, with a disposable joint
branch for the combined verification.

## What this pull request contains

Four documentation files and nothing else — no product code, no test, no
`contract/` artifact. The container was published at the reviewed head
`9a370369dc57332074508d2ff158b913fa620c66` (tree
`6da05b4daa51af0aafee63daa0158c8f1ecf3edc`), with one further documentation
commit on top to update this body with the API access and H-line facts:

- `docs/notes/hm-h00-m-container-baseline-2026-10-31.md` — the container opening
  record: base SHA/tree, M task boundaries, "no `request_http`, no private
  declaration ABI" constraints, inherited red test.
- `docs/notes/hm-h00-m-container-draft-pr-2026-10-31.md` — the first draft of
  this body, kept as history.
- `docs/notes/hm-h00-m-container-baseline-correction-2026-10-31.md` — the
  correction that keeps `http_exposed: true` for M declarations (see below).
- `docs/design/daemon-request/m-container-pr-body-2026-10-31.md` — this file,
  the maintained copy of the body.

M-01 (read declarations + the minimal partial-registration hook) and M-02
(provider declarations + tests) open their own pull requests against this
branch; M-03 combines the accepted slices and regenerates examples, the Contract
Mock and the frontend handoff once.

## Baseline facts

- Base: `df9d505029b6002560fa732a2d73d8ac15610b74` (tree
  `921537411c0b02d300f5a62f8936cd721470b521`), i.e. actual dev at container
  creation. A's landing `fd14cf98e1fd3d48ccfd5b31aa80f5777664fd3b` is an
  ancestor of it (`git merge-base --is-ancestor` → 0).
- The five methods are **not** declared entities yet: `METHOD_REGISTRY` binds
  only `LINK_BINDINGS` and `MIGRATED_FAMILIES == {"link"}`, so they still travel
  the legacy string router. Declaring them is M's product work.
- dev's `MethodSpec` carries `http_exposed: bool` and nothing else HTTP-specific.
  `HttpBinding` / `IdentityBinding`, the `daemon.catalog` runtime projection
  (`catalogVersion` / `epoch` / `packageVersion` / `declared` / `state` /
  `callable`) and the `httpBinding` declaration-digest input exist only on the H
  line (`02b9001fae32bd998a75654c0ea259d94276c7c5`, rejected, not merged, not
  treated as passed here).

### `http_exposed: true` is a projection claim, not a live route

M declares the five methods with `http_exposed: true`. On dev's type that flag
means "this method is eligible for the HTTP/OpenAPI projection", which is exactly
what the M task needs: the declarations must reach
`contract/daemon-request/openapi.json` and the generated client surface.

It does **not** claim that a running gateway serves the route. Runtime
availability additionally needs the generic ABI adaptation (identity binding plus
the daemon catalog state projection), which H-01/H-03 own, and the real
five-method joint verification, which HM-01 owns. Until those land, an exported
`/daemon/request/<method>` path describes intent; a request against it is
expected to be unavailable, and M must not present it as working. The
declaration's docstring records that split so no reader has to infer it.

The field-by-field gap table and the three-slice scope of the shared metadata
(kernel declaration metadata → daemon catalog projection → regenerated contract
artifact, explicitly excluding `request_http` runtime) are in
`docs/notes/hm-h00-baseline-sync-2026-10-31.md` on the H source branch
(`fix/http-hm-baseline-sync`). It is deliberately not copied into this container.

## Local gates

Run on this branch, on this host. The exact head these numbers belong to is
recorded in the H-00 PAC handoff; the branch name is used below so this file does
not go stale on every commit.

Local gates:
- `uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py` — PASS (script exit 0; product/test tree unchanged from dev)
- `uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py` — PASS (script exit 0; static AST, same tree as dev)
- `uv run --no-project --python 3.12 python scripts/verify/check_contract_impact.py --base df9d505029b6002560fa732a2d73d8ac15610b74 --head intg/gui-request-examples` — PASS (exit 0; `src_files_scanned=0 candidates=0 impacts=0`)
- `uv run --with libcst==1.9.0 pytest tests/` — NOT_RUN for this pull request (documentation-only delta; a full-suite number here would imply coverage this branch does not have. M-01/M-02 carry their own full-suite evidence.)

## Unknowns and inherited failures

- **Inherited FAIL at this base**: `tests/tooling/contracts/test_active_contract_command_spelling.py::test_retired_top_level_names_are_absent_from_the_registered_root`
  fails with `RuntimeError: Could not get a command for this Typer instance` on
  the unmodified base `df9d505029b6002560fa732a2d73d8ac15610b74` (reproduced in a
  detached worktree of that exact commit). Not caused by this container, not
  fixed here, reported as FAIL.
- **Unknown**: whether the joint H+M composition actually routes the five
  methods with real credentials and CORS. That is HM-01's measurement, not this
  container's.

## H line status at publication

- A landed: `fd14cf98e1fd3d48ccfd5b31aa80f5777664fd3b` is an ancestor of the
  current dev (`git merge-base --is-ancestor` → 0), so "wait for A" is closed by
  ancestry, not by assertion.
- H container `intg/gui-request-http` was fast-forwarded from
  `15db36c0f8049144d0dc0be6d397522b4d66c28b` to
  `ad31e3f5cab89bd2d1e80e072218cf83a0bd84a9` (tree
  `dc913c60056d934d3e7f978b0789f00defa26672`) after this card's review: it now
  sits on actual dev `df9d5050` via a zero-conflict docs-only merge-forward, and
  its non-`docs/` delta against dev is still 0 files.
- H-01 (real entry + runtime catalog), H-02 (generated client) and H-03 (write
  profile) remain the H line's own work. The rejected H-01 candidate
  `02b9001fae32bd998a75654c0ea259d94276c7c5` is still not accepted and is not on
  the container.

## API access

Reading and writing pull requests needs the Forgejo HTTPS API, which this host's
proxy intercepts. Both the failure and the workaround are measured, not guessed:

```
$ fj repo view HyprialOS/harness-bridge            # without NO_PROXY
Error: error sending request for url (https://git.internal.example.com/api/v1/repos/HyprialOS/harness-bridge?)
Caused by: 0: client error (Connect) 1: tls handshake eof

$ curl -sS -m 10 -o /dev/null -w '%{http_code}\n' https://git.internal.example.com/api/v1/version
curl: (35) LibreSSL SSL_connect: SSL_ERROR_SYSCALL ... ; http_code=000

$ NO_PROXY=git.internal.example.com,localhost,127.0.0.1 \
  no_proxy=git.internal.example.com,localhost,127.0.0.1 \
  fj -C /Users/amyws/projects/daemon-request pr -R fj view 2144
feat(http): daemon request entry and reusable client #2144       # exit 0
```

Git transport stays on `ssh://git@git.internal.example.com/HyprialOS/harness-bridge.git`;
`-R fj` only selects the repository for the API call. No credential was extracted
or repointed and the existing `fj` login is untouched. In this CLI version
`fj pr view` takes an integer id (`pr view 2144`) — `2144/status` is rejected as
an invalid digit — and PR state comes from `fj pr status 2146`.

## Submission command

`fj pr create` takes the title as a positional argument and has no `--draft`
flag; a draft is expressed by the `WIP: ` title prefix. This is the command the
H-00 `integrate` step used to publish the branch:

```
export NO_PROXY=git.internal.example.com,localhost,127.0.0.1
export no_proxy=git.internal.example.com,localhost,127.0.0.1
git push origin intg/gui-request-examples
fj -C /Users/amyws/projects/request-h00-sync pr -R fj create \
  --base dev --head intg/gui-request-examples \
  --body-file docs/design/daemon-request/m-container-pr-body-2026-10-31.md \
  'WIP: intg(gui-request-examples): open the M integration container at actual dev'
```

## Submitted-by

Yao Williams <amy.williams@example.com> — H-00 execution worker, PAC
`wf-9641fde49d8c438bad75f22a3778e710`, build request
`workflow-request:e456901e61904263bdb7441eb7200a4b`.

Skill-Impact: not-needed: this pull request touches no `.agents/skills/**`,
no `SKILL*.md` and no dispatch instruction; it only adds documentation under
`docs/notes/` and `docs/design/daemon-request/`.

Contract-Impact: none — the branch increment has `src_files_scanned=0`, so no
published JSON key, diagnostic field, event name or error code changes. The five
declarations that will change `contract/daemon-request/openapi.json` arrive in
M-01/M-02, and those pull requests declare their own Contract-Impact.

## Review checklist

1. Base is `df9d505029b6002560fa732a2d73d8ac15610b74` and the delta is the four
   documentation files listed above.
2. M constraint set holds: no `src/hyprial/shell/impl/request_http/**`, no
   private `HttpBinding`, no second durable inbox, no credential material.
3. The `http_exposed: true` reading is accepted: projection eligibility now,
   live routing only after the shared ABI and HM-01 verification.
4. Decide whether the shared metadata slices get their own reviewed commit
   before M-01, or whether M-01 lands first on dev's declaration type.
