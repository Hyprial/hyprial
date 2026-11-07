# A05 upstream ledger reconciliation — decision pending

The accepted design in `cli-surface.md` §6 permits exact legacy references only
to decrease. A03 accepted548 occurrences at
`20269c960c98ba5def5abe65d3948a60ea8f8550`; A04 anchors those immutable ledger
bytes in the gate. No ledger or gate change is authorized by this proposal.

The required merge of dev `da2c9337d5685b622af07d33c16184af18039bfa` into A05
produced candidate `ce8c172fe96ba8d8e245a4a252ce0a5811e4c8ba`. Its scanner
observes590 occurrences. All42 increases are present in that immutable dev tree
at equal or greater counts. They span seven CLI files, including31 imports in
the upstream post-install primer. The complete proposed allowance is exactly
the42 rows, with path/module/symbol/count, in
`../../evidence/cli-command-surface/a05-dev-import-growth-draft.json`.
There is no wildcard or allowance for future upstream imports.

## Proposed bounded amendment

Approve a one-time reconciliation of those42 pre-existing upstream occurrences
before the first A container landing. Preserve the original548 ledger and its
digest as historical evidence. A reviewed new ledger would contain exactly the
original entries plus the approved occurrence increases (590 for ce8); any
later legitimate removal must be reflected as a decrease. The method ledger,
MethodSpec validation, OpenAPI and public client behavior are unaffected.

The implementation must bind both immutable source anchors, the exact42-row
manifest and the reviewed result digest. It must verify source provenance and
ancestry rather than trust a head-provided JSON claim. A missing source commit,
partial ledger, joint manifest/ledger tampering, any43rd increase, an increased
count beyond the approved row, or a post-landing attempt to reapply this
reconciliation must fail. Real PR base comparisons and all three gates remain
mandatory. The standalone ratchet and the CI wrapper must agree; a wrapper-only
PASS cannot hide a standalone FAIL. No generic rebaseline flag or caller-supplied
production anchor is allowed.

The original DeepSeek worker would implement and test this bounded amendment
only after approval. The coordinator would independently run positive cases,
negative controls, shallow PR/push/manual entry checks and review the exact
diff before accepting it. A05 and container #2015 remain unaccepted until their
other verification obligations are met.

## Alternative: retain the original548 ceiling

Keep the approved rule unchanged. The42 upstream dependencies then need real
migration through the intended shared interfaces before A can pass. Relocating
imports, copying helpers or adding re-export shims is prohibited. This extends
the current A05 work beyond installation/consumer verification into migration
of upstream command/bootstrap implementation, and requires a revised scope and
estimate. An older-dev final artifact is not a substitute for the required
latest-dev integration.

Status: decision pending; both options preserve failure reporting and the
independent final review. No automatic rebaseline has been performed.

## Review clarification after the user's request for rationale

The user requested an explanation and design-based recommendation, not approval
of the amendment above. The coordinator withdraws the recommendation to accept
all42 merely because their provenance is upstream. That proves authorship and
merge origin, not architectural admissibility. The proposed bulk amendment is
not approved and must not be implemented.

The GUI dev-mode fix `92922e526` (#2019, merged as `5f6e4d0ed`) changes four
GUI launcher/test/documentation files and introduces none of the42 listed CLI
edges. These edges arrived via the 0.5.5.1 release integration (#2022), followed
by the structural-root fix (#2023). The original31 new preload edges are
attributable to upgrade commits `935bf6bb2`, `0bc722fed` and `b589f0c1b`.
Their function `_prime_post_install_restart_imports` imports lazy dependencies
before installation replaces the running distribution, so the old process can
finish restart safely. This is a credible bootstrap use, not proof of31 newly
duplicated business operations. Each symbol and the continued need for this
placement still require review.

The remaining11 occurrences are adapter offline downgrade/exception imports(3),
worker identity binding/error vocabulary in common support and message send(4),
name-collision diagnostics(2), launch reasoning-effort handling(1), and offline
org authority re-pin(1). Two of the adapter entries are duplicate occurrences
of already-used exception types. A count increase is therefore not equivalent
to a new business function. Conversely, diagnostic code reads registry data
and cannot be declared bootstrap solely because it comes from dev.

Recommended disposition: hold A05 acceptance and bulk ledger expansion while
classifying the exact uses. Preserve the original ledger; review legitimate
startup/offline bootstrap cases under the existing §6 exception policy, and
adapt genuine new business consumers through the intended public request
boundary. Do not move imports or add shims to make the scanner blind. GUI work
and independent consumer/test repairs continue. Any remaining exception request
must name the particular capability and evidence, not all42 as one package.

At the time of this clarification, remote dev advanced to `98b104257` through
#2026. Its only changes since da2 are R2 anonymous read-back User-Agent handling
and its transport test. No CLI path changed; that update is not the origin of
the42-edge result. The import gate itself is absent from dev@da2: it currently
lives on A's integration branch and evaluates the combined source tree there.
