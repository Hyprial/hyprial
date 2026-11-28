# dev E2E readiness — work in progress

Scope baseline: `284dcf72942b1b1b2495c9bbddc1e646cfc386a3`.
Isolated verification candidate: `39cf24a0918d64c4b1e1fd124aa2f91e400d67ff`.
Formal release candidate remains **TBD**. This WIP does not approve a release.
The measured dev baseline is 5 PASS / 13 FAIL; the fixed WIP source
`41e3de2a1b0f332c8816298675b258496bd311ae` measured 15 PASS / 3 FAIL;
`93992a93313e315edddbb9cb205ac3d4a41ae935` measured 16 PASS / 2 FAIL;
`9c8eb2676822775f8ab7dbda34aa5c58309250c7` and
`a10857afad464a8d1428f32a3f00c19f92c2a41a` each measured17 PASS /1 FAIL,
at different later001 boundaries. The OS-identity repair passes phases1–6
in focused Mac42009. The bounded mailbox precondition repair then passes
all seven phases in Mac42040. The normal full18-case Mac42047 then completes
**18 PASS /0 FAIL /0 NOT_RUN** on the isolated verification candidate.
Current dev has not acquired these WIP results.

All fixes target `dev`. The short-lived E2E branch collects test repairs and
evidence; it is not a second release integration lane. Publication holds and
version tags remain under the [release rules](release-rules.md).

## Evidence and issue analysis

- [#2425](https://code.hyprial.com/HyprialOS/hyprial/issues/2425): retain
  startup diagnostics before cleanup and account for nested startup budgets.
  The current mock-model gate has progressed since the historical issue;
  recheck current source/results before attributing its old timeout.
- [#1759](https://code.hyprial.com/HyprialOS/hyprial/issues/1759): isolated
  test daemons must stop on every exit path and must not escape isolation.
- [#1778](https://code.hyprial.com/HyprialOS/hyprial/issues/1778): failure
  artifacts must preserve diagnostic evidence without copying credentials.
- [#1746](https://code.hyprial.com/HyprialOS/hyprial/issues/1746): a passing
  test gate and the publication writer are different checks; never relax the
  gate or its mutation control to obtain a green result.
- [#9](https://code.hyprial.com/HyprialOS/hyprial/issues/9) is an early
  historical matrix. Its old missing-runner conclusions are superseded; its
  restart, delivery/ACK consistency and attribution requirements remain useful.

Current dev's mock-model workflow run [52154 / page 41786](https://code.hyprial.com/HyprialOS/hyprial/actions/runs/41786)
is successful. This does not certify the isolated macOS or controlled live
provider suites. Their previous runs used older source `6d94236bc93156be1345447fb7415a536333729a`
and failed. The current isolated macOS baseline run
[52166 / page 41797](https://code.hyprial.com/HyprialOS/hyprial/actions/runs/41797)
finished FAIL after 889 seconds: **5 PASS / 13 FAIL** across 18 scenarios.
PASS rows are E2E-002/011/020/029/030. E2E-020 covers its declared first wave;
029/030 prove transport only and do not certify the unrun CLI/Casdoor/OrgFS
layers. No global zero-residue verdict was emitted by that job.

## Initial local observations

The baseline was installed into a dedicated editable venv and run with
disposable HOME/XDG/state/socket paths. Structural and module-boundary checks
passed. Scenario inventory validation passed. Runner/control tests passed
321 cases; these are tests of the E2E infrastructure, not scenario acceptance.

The selected ten-scenario baseline is red: E2E-002/011/020 passed;
E2E-003/007/015/016/017/018/026 failed. The initial concrete causes include:

| Area | Observed mismatch or prerequisite |
| --- | --- |
| E2E-003 | Pin expectation constructs the retired name URI instead of the published stable agent ID |
| E2E-007 | Desired adapters are read from the old harness collection |
| E2E-015 | Runner calls retired `workflow status` |
| E2E-016 | This Linux test environment lacks Taskwarrior; retain the prerequisite failure until provisioned |
| E2E-017 | Runner calls `message outbox --json` instead of the current subcommand |
| E2E-018/026 | Fixture constructs retired agent-name addresses |

E2E-003/007/017 were repeated unchanged and reproduced the same failures.
The initial reading is **NO-GO**, not a whole-suite pass or release approval.

## Candidate repair progress

The following includes earlier separate local records and the subsequent
fixed-source macOS observations. The next repaired candidate still needs its
own complete gate; neither a partial pass nor local controls certify dev.

| Scenario | Current observation |
| --- | --- |
| 003/007/017 | Real isolated runs pass after official IDs, frozen-name handling, desired adapters and outbox-list corrections |
| 016 | Two complete passes with real Taskwarrior 2.6.2, official actors and live channel leases |
| 018 | Two complete passes, 13 assertions each, preserving graph ownership/fence/reset checks |
| 021 | Strict repeat passes in 142.28 s after public session replay, actual migration authority and cleanup corrections; 240 s/20 s budgets retained |
| 028 | Frozen clean 41e3 passes locally in 61.60 s and on macOS in 35 s, including real Tailcat, fixed address/endpoints, zero redial/reconfigure, delivery and cleanup |
| 015 | Two complete passes, 280.48 s and 289.49 s, of the original unified+cross entry within 600 s. Product fixes cover managed-owner identity, ordinary remote `expands` and receipt/binding-fenced routine-owned publication; fixtures retain logical stored owners and use official IDs for public calls |
| 024 | Local four-mode PASS240.58s; Mac41e3 FAIL, then939/9c8/a108 each full4-mode/8-direction delivery+ack PASS with the original budgets. All8 submissions on each later run used0 lookups; recovery controls remain separate |
| 026 | Complete 13-step enforced-profile pass in 146.85 s. Real channel leases, restart and known-request reply settlement preserve the original 10 s/30 s/5 s assertions, with owned daemon cleanup confirmed |
| 001 | Maca108 failed original90s offline delivery with B copied-home reissuance. Independent persistent OS IDs fix that boundary while preserving product guards. The public view retains withdrawn hosts as unknown; the composite absence fix preserves it and proves targets absent/container stopped. Focused Mac42040 passes all seven phases in233s; normal full Mac42047 repeats them in224s, with original custody/TTL/terminal requirements and owned Docker cleanup |
| 005 | Frozen Mac41e3 PASS277s; latesta108 PASS194s, including actual native/GUI wheel build, install, upgrade and uninstall. Linux still refuses GLIBC2.34 at the declared manylinux2.17 floor; this is not five-platform release acceptance |
| 010 | Full same-interpreter continuity passed on9c8/192s anda108/198s, all17 checks: independently preserved public IDs, actual old-version auto-restart, held old MCP/current epoch, connectors/session, new fenced child/restarts, exact old bodies/FIFO/fresh delivery IDs/no replay, post-upgrade send/ack. Explicit forwarding-target compatibility remains KP039 |

The next fixes are documented separately: [001 managed fixture](../notes/e2e001-managed-readiness-2026-11-08/README.md),
[010 old-launcher handoff](../notes/e2e-upgrade-legacy-tail-2026-11-08/README.md)
and [024 read-only submission reconciliation](../notes/e2e024-macos-ambiguous-submit-2026-11-08.md).
Their local controls do not replace an exact-source macOS gate. Explicit
forwarding-target upgrade compatibility remains [KP-039](../reference/known-problems.md#kp-039-显式转发目标的旧进程原位升级兼容尚未验收).

The product fixes preserve official caller identities, current entity-token
fences and stored PAC owner/receipt/schema bytes. Fixtures must use current
contracts; genuine product failures remain failures. No native-build skip,
raised ABI floor, relaxed deadline or omitted mode constitutes acceptance.

The first assembled candidate `732d22fe34116139c16184f33f56dc8ec7b072cd`
ran on macOS in [run 52295 / page 41919](https://code.hyprial.com/HyprialOS/hyprial/actions/runs/41919).
It stopped after 702 s: the Rust preparation step hit its 10-minute deadline
while downloading rustc. All 18 scenarios were **NOT_RUN/BLOCKED**, and cleanup
evidence was UNKNOWN. The underlying download cause remains unknown.

The follow-up prepares checksum-verified official archives before rustup,
caches complete archives and retains private installation homes. Real Linux
cold/warm installation and offline compilation pass; only the tool preparation
budget becomes 20 minutes. The job remains 90 minutes and every scenario budget
is unchanged. The new helper is mapped to E2E-005/010, whose native-wheel builds
need this toolchain. On real macOS, 41e3 successfully prepared Rust/Cargo
1.93.1 and completed the actual E2E-005 native wheel flow.

The second assembled candidate 41e3 ran in
[run 52319 / page 41940](https://code.hyprial.com/HyprialOS/hyprial/actions/runs/41940)
for 1813 s (gate 1630 s), finishing **15 PASS / 3 FAIL / 0 NOT_RUN**. All 18
selected cases have actual rc and duration records. Only 001, 010 and 024
failed, at the later stages described above. The
[fixed-source report](../notes/e2e-macos-41e3-2026-11-08/README.md) retains
the complete matrix, source attribution and raw-log digest. Overall host
cleanup is UNKNOWN; explicit per-case cleanup receipts cover only their
respective resources. Three giant 024 records returned incomplete at 65,565
characters, so omitted diagnostics cannot be inferred.

020 covers only its declared first wave. 029/030 cover real v7 transport,
with CLI/Casdoor/OrgFS explicitly NOT_RUN. Controlled credentials, live
platform actions and the five-platform release matrix remain separate.

The third fixed source939 completed
[run52340 / page41957](https://code.hyprial.com/HyprialOS/hyprial/actions/runs/41957)
in1725 s: **16 PASS / 2 FAIL / 0 NOT_RUN**. Only001 and010 remain red,
at the later fixture boundaries above. The
[939 matrix](../notes/e2e-macos-939-2026-11-08/README.md) preserves actual
rc/durations and the distinction between024 normal sends and recovery controls.
The subsequent [relay IPv4 repair](../notes/e2e001-relay-ipv4-2026-11-08/README.md)
and [canonical upgrade identity repair](../notes/e2e-upgrade-canonical-identity-2026-11-08/README.md)
are independently controlled changes, not retroactive passes for939.

The fourth fixed source9c8 completed
[run52366 / page41979](https://code.hyprial.com/HyprialOS/hyprial/actions/runs/41979)
in1412 s (gate1268 s): **17 PASS / 1 FAIL / 0 NOT_RUN**. Only001 failed,
after actual join and phase1, at the old diagnostic CLI in phase2. 010 now
completed all17 checks; 024 again completed all4 modes and8 delivery/ack
directions, with0 lookup attempts and the original budgets. The
[9c8 matrix](../notes/e2e-macos-9c8-2026-11-08/README.md) preserves its exact
source, actual checkout, timings and raw digest. Global host cleanup remains
UNKNOWN; the scope limits above still apply. The bounded
[current CLI repair](../notes/e2e001-current-cli-2026-11-08/README.md) changes
only that observation command, retains all7 phases and30/90/90 s waits,
and audits the remaining current command families. It awaits the next Mac
candidate; local CLI/control tests are not full Docker acceptance.

The fifth fixed sourcea108 completed
[run52381 / page41991](https://code.hyprial.com/HyprialOS/hyprial/actions/runs/41991)
in1555s (gate1359s): **17 PASS /1 FAIL /0 NOT_RUN**. The
[a108 report](../notes/e2e-macos-a108-2026-11-08/README.md) retains the whole
matrix and bounded owned-artifact diagnosis. 001 now reaches the original
90s offline wait. B's saved daemon log records `tailcat.identity-reissued`,
`reason=copied-home` and an identity-hash change after its container restart.
Stable-host real Go controls, including a verified cold sidecar and the
public entrypoint bootstrap, pass the original limits. They do not prove
Docker's OS fingerprint stayed stable; which fallback component changed is
still unknown.

The [fixture OS-identity repair](../notes/e2e001-container-machine-id-2026-11-08/README.md)
removes the shared image template and creates a separate persistent ID per
container before public device-key provisioning. Stable IDs preserve real
device records; a changed OS ID still triggers copied-home rotation. All
seven phases and business budgets remain. A temporary manual-only workflow
ran the original complete001 in
[Mac42009](https://code.hyprial.com/HyprialOS/hyprial/actions/runs/42009)
on `cebced995cefa0c110a01d77929f3b79096cf79b`, whose only difference from
the tested active-code source6c82 is that focused workflow. It has no
schedule, publication marker or secret writer and must never be merged.
It finished failure,418s total/273s selected001. The
[focused report](../notes/e2e001-focused-42009-2026-11-08/README.md)
retains phase1–6 passes and the exact phase7 failure. D advertised its role
and took the first ghost; B was stopped, but the old helper required its
whole host row to vanish. The public projection retains withdrawn peers as
unknown, so the90s wait could not pass. Held-mailbox delivery and final
expiry were not executed. The next control jointly requires a valid
absent/unknown host view, all that peer's canonical targets absent and its
exact owned container stopped, under one original90s deadline. It does not
rename unknown to offline or weaken later requirements. This focused
result cannot replace the full18-case a108 matrix.

The [mailbox presence repair](../notes/e2e001-mailbox-presence-2026-11-08/README.md)
implements those three observations without changing the public projection.
Its initial21 controls reproduce17 FAIL/4 PASS before and21 PASS after;
29 new boundary cases are part of the final144 passing combination. Positive
readiness requires online; invalid shapes, a remaining target, a running
container or late result cannot pass. Structure/module boundaries/inventory
and final Ruff pass. Full pytest on clean6496 again exits2 for the same3
KP037 collection errors/2 skips. Impact maps only001, unknown=[] and
execution=NOT_EVALUATED. The large raw boundary graph is losslessly archived
with its original-byte hash, not expanded into250,000 lines of PR noise.

The next original001 seven-phase run was
[Mac42040](https://code.hyprial.com/HyprialOS/hyprial/actions/runs/42040)
(API52434/job135546), fixed source
`500d2ea0e133f44de73756a0492a8034e3ecc0bc`. It differs from the WIP's active
code only by its temporary manual-only workflow and has the same Docker
input hash. It completed **success**,387s total/233s selected001, including
mailbox custody transfer, delivery after B returns, D's own TTL eviction
and non-holder verdict pulls. The
[complete focused evidence](../notes/e2e001-focused-42040-2026-11-08/README.md)
retains those original assertions. No unselected case or whole-suite pass
is inferred. The temporary focused remote was backed up and deleted with its
exact500 SHA lease and API404 readback; its workflow is never merged into the
full gate.

A stale final summary literal still said absentRecipientUnconfirmed even
though phase6e already verified TTL_EXPIRED. The report-only correction
now names absentRecipientExpired; both phase and public terminal report
are checked. Final clean30d6 passes145 combined tests, structure and module
boundaries. Full pytest on that exact clean code again stops at the same3
KP037 collection errors/2 skips (20.46s), not a full-suite pass. Source
metadata and lossless raw logs, including the intermediate wiring-control
failure, are in the focused report.

The final normal full gate completed on the frozen WIP source
`39cf24a0918d64c4b1e1fd124aa2f91e400d67ff`:
[Mac42047](https://code.hyprial.com/HyprialOS/hyprial/actions/runs/42047),
API52442/job135566, **success,18 PASS/0 FAIL/0 NOT_RUN**. Total1568s,
gate1417s. Every selected scenario has an actual rc/duration and matching
checkout/installed-SUT source anchor in the
[complete final matrix](../notes/e2e-macos-39cf-2026-11-08/README.md).
001 repeats all seven original phases in224s;010 again passes all17
same-interpreter upgrade checks in186s;005 performs its actual native/GUI/wheel
installation flow in182s. 024 passes four modes/eight delivery+ack directions
in130s, all relearn31.581–31.607s within38s and zero lookups. The original
business budgets and terminal assertions remain intact. Overall unrelated
host cleanup stays UNKNOWN;020 is first-wave,029/030 are transport-only,
and controlled/attended obligations are not part of these18 passes.
This manual run does not write a scheduled green marker or release a version.
These results belong to the fixed WIP candidate, not automatically to its later
merge into dev or a formal candidate; publication decisions remain separate.

Full `pytest tests/` with the pinned LibCST overlay was attempted on clean732
and939.
It stopped during collection with the three remaining KP-037 classes: old
interpreter path, duplicate `test_migration` import and missing link-fixture
`_ipc`. It reported two skips and no whole-suite pass. Separate composed focus
checks had 514 PASS and one actual Docker-freezer environment failure. On clean
939 the combined repair focus passed175 tests; Ruff, structural limits, module
boundaries and27-row inventory passed. Full pytest939 still exited2 during
collection with the same3 KP037 errors and2 skipped; it is not a full-suite pass.
The next active code passed250 focused tests at930fcba57 and full Ruff at
9c8 (a lint-scope-only descendant). The new001 CLI repair passed94 related
checks and Ruff/structure/boundaries/inventory at0f1d682d0; full pytest again
stopped with the same3 KP037 collection errors and2 skips. No collection or
behavior assertion was weakened to produce these results.
The machine-ID repair passed117 combined tests on clean6c82; a real pinned
Go/public-key control passed0.57s. Full pytest6c82 again stopped at the same
3 KP037 collection errors/2 skips, exit2. Full Ruff passed at6c82 and the
docs-only ae5 descendant without new excludes. Active contract/test diff
checks pass; two frozen raw pytest logs retain their original whitespace,
as explained in [the verification record](../notes/e2e001-container-machine-id-2026-11-08/verification.md).

## Preventing the same drift

The [E2E impact mechanism](e2e-impact.md) adds implementation/contract coverage
to the existing scenario registry. It computes affected scenarios from the
complete PR diff, requires individual dispositions and reports unmapped inputs
for review. A contract-preserving implementation change need not edit a test;
its original scenario still needs verification. Declaration completeness always
reports `execution=NOT_EVALUATED` and does not prove an E2E pass.

The checker, mapping, template and developer instructions are prepared here.
Enforcement is a separate adoption step after the reviewed checker exists on
dev; the prepared base-owned workflow wiring must otherwise fail explicitly
with `BOOTSTRAP_REVIEW_REQUIRED`. Preparing the checker here does not itself
enforce the new policy. Adoption is tracked in
[#2588](https://code.hyprial.com/HyprialOS/hyprial/pulls/2588); that subsequent
PR must verify that the base owns the checker and must retain the shared
registry/declaration semantics reviewed with the owner
[#2587](https://code.hyprial.com/HyprialOS/hyprial/pulls/2587) proposal.

This repair also found obsolete prose assertions in the registry: old TCP
endpoints for 001, a nightly baseline for 010 and a v5 fixture for 024. Their
facts and dependency coverage are corrected with their runners. Inventory
shape validation alone cannot prove that prose matches implementation;
declarations still need semantic review and execution evidence.

## Completion record

- [x] Align fixtures with current public CLI, agent IDs, frozen names and
  desired-state contracts while preserving the behavioral assertions.
- [x] Reproduce and classify remaining isolated scenarios, including actual
  Docker mesh, Tailcat transport and installation/upgrade boundaries.
- [x] Preserve red/green commands, final RESULT lines, tested SHA, duration,
  prerequisite failures and owned cleanup evidence.
- [x] Repeat affected baseline/candidate checks to distinguish a stable
  mismatch from intermittent behavior.
- [x] Run the final isolated macOS gate on the fixed candidate and record its
  actual completion; a dispatch acknowledgement is not execution evidence.
- [ ] Assess controlled provider/platform rows separately. Missing accounts,
  attended actions or live capabilities remain BLOCKED; do not replace them
  with a mock-model or unit-test result.
- [x] Record source candidate, final matrix and any remaining blockers for
  the separate `release/v0.5.5.4` preparation WIP.
