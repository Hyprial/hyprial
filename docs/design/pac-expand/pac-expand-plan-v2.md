<!-- Saved from workflow wf-2d7d9af0df1f46e3ac79201efdee8199, node revise (output:revise); then the re-review C1-C8 (docs/design/pac-expand/pac-expand-plan-v2-review.md) folded in place by user-exp-improver on 2026-10-06. Folds are marked [R2-Cn]. -->

PAC EXPAND v1 — CURRENT-TREE IMPLEMENTATION PLAN

[R2-C4] Branch feat/pac-expand-v2 now merges origin/dev 74c86b5f (merge 4a995627, merge-tree clean); the prerequisite starts from the branch HEAD at dispatch, which includes that merge. Evidence/baseline: read-only verification at feat/pac-expand-v2 HEAD 6005943da0f7ce8c7306ac2e10527134db3c5403; product base remains dev e43b3131afbd7d4bd6cec156b897f29fdc013ba3/schema17. Since cea1475ca03908ecead402148de7afecaf5b67f0 only the saved plan and review changed. Paths, cited source ranges and test IDs checked against this tree; NEW paths below are proposed, not existing. Working tree clean. No tests, installations or source edits; only workflow inspect/complete product commands. All checks below are implementation instructions, NOT reported passes.

Approved scope unchanged: frozen parent/child graphs; one plan completion binds one child beneath an existing placeholder; org ceilings only tighten; optional per-execute-node worktrees; child failure applies the parent's failure policy. No nested expansion, generation-2 replanning, automatic push/PR or adoption of borrowed workers.

Exact path abbreviations used below:
 P = src/hyprial/daemon/impl/pac
 A = src/hyprial/daemon/impl/application
 D = src/hyprial/daemon/impl/dispatch
 T = tests/daemon/pac
 Expanding these prefixes yields each exact owned path or file:line reference; braces enumerate files, not ownership globs.

1. MAPPING

Old product paths (src/hyprial/ prefix implicit on LEFT):
 pac/workflow_schema.py -> P/contracts/workflow.py
 pac/errors.py -> identity/impl/pac_errors.py; consume through hyprial.identity
 pac/principal.py -> identity/impl/principal.py; consume through hyprial.identity
 pac/graph.py -> P/graphs/edits.py
 pac/store.py -> P/storage/store.py
 pac/migrations.py -> P/storage/migrations/__init__.py (latest steps/driver) AND upgrades.py (earlier catalogue and SCHEMA_VERSION)
 pac/journal.py -> P/storage/journal.py
 pac/subscription.py -> P/graphs/subscription.py
 pac/reactor.py -> P/graphs/reactor.py
 pac/workflow_graph.py -> P/workflows/graphs.py
 pac/workflow_roster.py -> P/contracts/roster.py
 pac/workflow_output.py -> P/workflows/outputs.py
 pac/workflow_runtime.py -> P/workflows/runtime/{__init__.py,core.py,operations.py,types.py}; facade; start/request/failure/project; tick/status/list/fail/cancel; close_workflow/WorkflowSender/errors/protocols respectively
 pac/authority.py -> P/graphs/authority/{__init__.py,commands.py,generation.py}; facade/effect lanes, typed commands, writer handler respectively
 pac/lifecycle.py -> P/actors/coordinator/{__init__.py,core.py,start.py,stop.py,types.py}; facade, cleanup/activation, reconcile/up/down, lost/alerts, launch/runtime ports
 daemon/pac_actor.py -> P/actors/daemon.py
 daemon/application.py -> A/__init__.py plus semantic mixins; relevant seams A/workflows.py, A/wiring/services.py, A/shutdown.py; routing moved to daemon/impl/ipc/router.py. No new PAC implementation in application facade.
 dispatch/remote_workflow.py -> D/remote/__init__.py AND D/remote/wire.py
 dispatch/workflow_observation.py -> D/workflow_observation.py
 dispatch/matrix.py -> D/matrix.py for selection; TIERS/Candidate/tier_for_model moved to kernel/impl/facts/tier_vocab.py, publicly hyprial.kernel
 persistent_config.py -> kernel/impl/configuration/persistent_config.py, publicly hyprial.kernel
 workflow/cli.py -> shell/impl/cli/commands/routines/workflow.py; list renderer additionally shell/impl/cli/commands/observe/overview.py
 mcp/server.py -> daemon/impl/mcp/server.py
 pac/overview.py -> P/views/overview.py
 pac/overview_page.html -> P/views/overview_page.html
 cli_inventory.py -> shell/impl/cli/inventory.py
 contracts/lifecycle_budgets.py -> kernel/impl/contracts/daemon/lifecycle_budgets.py; public exports in kernel/interface.py
 contracts/daemon_teardown.py -> kernel/impl/contracts/daemon/daemon_teardown.py
 Related IPC error registry -> kernel/impl/contracts/daemon/ipc_errors.py; consume public hyprial.kernel.ipc_errors
 Bare workflow_schema.py/schema.py, workflow_runtime.py/runtime.py, lifecycle.py, authority.py, application.py, graph.py, reactor.py, store.py, journal.py, migrations.py and overview.py references in the old prose denote the corresponding mappings above, NOT new root modules.

Old proposed modules never existed; do not resurrect top-level hyprial.pac:
 pac/expansion_policy.py -> NEW P/contracts/expansion/policy.py
 pac/workflow_expansion.py -> NEW P/contracts/expansion/{document.py,validation.py}
 pac/expansion_runtime.py -> NEW P/workflows/expansion/{transactions.py,projection.py,results.py,storage.py}
 pac/workflow_worktrees.py -> NEW P/actors/worktrees/git.py
 Extra helper allocations needed for structural limits are listed in the slices.

Every old test filename in the plan/review (LEFT was tests/<filename>):
 test_pac_workflow_schema.py -> T/workflows/runtime/test_pac_workflow_schema.py
 test_pac_workflow_runtime.py -> GONE as one file; T/workflows/runtime/graph_service/{completion/test_completion.py,deadline/test_deadline.py,graph_contracts/test_graph_contracts.py,workers/test_workers.py}; shared graph_service/support.py
 test_pac_workflow_roster.py -> T/workflows/runtime/test_pac_workflow_roster.py
 test_pac_worker_terminal_cleanup.py -> T/lifecycle/test_pac_worker_terminal_cleanup.py
 test_cli_workflow.py -> tests/shell/cli/workflows/test_cli_workflow.py
 test_mcp_worker_workflow_tools.py -> tests/daemon/channels/mcp/contracts/test_mcp_worker_workflow_tools.py
 test_pac_overview.py -> T/projection/test_pac_overview.py
 test_pac_list_overview.py -> T/projection/test_pac_list_overview.py
 test_pac_graph_authority.py -> T/graphs/test_pac_graph_authority.py
 test_pac_scan_actor_boundary.py -> T/events/test_pac_scan_actor_boundary.py
 test_pac_remote_workflow.py -> T/workflows/delivery/test_pac_remote_workflow.py
 test_pac_request_inbox_ttl.py -> T/contracts/test_pac_request_inbox_ttl.py
 test_pac_actor_lifecycle.py -> T/lifecycle/test_pac_actor_lifecycle.py
 test_cli_help_inventory.py -> tests/shell/cli/presentation/test_cli_help_inventory.py
 test_ci_cli_help_inventory.py -> tests/tooling/ci/planning/test_ci_cli_help_inventory.py
 test_no_pac_cli_spellings.py -> tests/shell/compatibility/test_no_pac_cli_spellings.py
 test_ipc_error_code_registry.py -> tests/daemon/ipc/error/test_ipc_error_code_registry.py
 test_supervision_boundary_gate.py -> tests/tooling/architecture/boundary/test_supervision_boundary_gate.py
 test_unreachable_surface_retirement.py -> tests/shell/compatibility/test_unreachable_surface_retirement.py
 test_cli_help_coverage.py -> tests/shell/cli/presentation/test_cli_help_coverage.py
 test_teardown_guarantee.py -> tests/tooling/isolation/test_teardown_guarantee.py
 supervision_exemptions.json -> tests/supervision_exemptions.json (unchanged)
 Old NEW test_pac_expansion_schema.py, test_pac_expansion_policy.py -> NEW T/expansion/policy/test_schema.py, test_policy.py
 Old NEW test_pac_expansion_transactions.py, test_pac_expansion_projection.py, test_pac_expansion_migrations.py -> NEW T/expansion/engine/test_transactions.py, test_projection.py, test_migrations.py
 Old NEW test_pac_expansion_worktrees.py -> NEW T/expansion/worktrees/test_git.py
 Old NEW test_cli_workflow_expansion.py -> NEW tests/shell/cli/workflows/expansion/test_cli.py
 Old NEW test_mcp_workflow_expansion.py -> NEW tests/daemon/channels/mcp/expansion/test_tools.py
 Old NEW test_pac_expansion_overview.py -> NEW T/expansion/views/test_overview.py
 Old NEW test_pac_expansion_e2e.py -> NEW tests/integration/pac/expansion/test_e2e.py, with separately allocated companions below

Docs/skills/operational paths:
 docs/design/pac-expand/design-pac-expand.md -> same carried input path
 docs/design-pac-workflow.md -> docs/design/design-pac-workflow.md
 docs/spec-pac-workflow-unification.md -> docs/design/spec-pac-workflow-unification.md
 docs/workflow-node-observation.md -> docs/reference/workflow-node-observation.md
 docs/proto.md -> docs/reference/proto.md
 docs/design/pac-expand/pac-expand-plan.md -> current plan replaced by this output; current review docs/design/pac-expand/pac-expand-plan-review.md. Earlier inputs remain docs/design/pac-expand/pac-expand-plan-prerefactor.md and docs/design/pac-expand/pac-expand-review-prerefactor.md. Approved design unchanged.
 .agents/skills/hyprial-workflow/SKILL.md -> same
 .agents/skills/h2b-workflow -> absent; no host skill edits
 <HYPRIAL_HOME>/pac-expansion.json -> PROPOSED trusted config, not existing configuration
 <workRoot>/.pac-worktrees/<child>/<node> and workflow-artifacts -> runtime artifacts, not source paths
 sidecar -> actual Go module is sidecar/hyprial-tailcat
 $PY -m pytest -> uv-managed offline focused commands below; no bare python or guessed system interpreter

2. CORRECTIONS — REVERIFIED C01–C22, THEN REFACTOR FINDINGS

C01. Transaction extraction STILL REQUIRED. P/workflows/graphs.py:93–309 opens store.write() at 141 [R2-C8] and commits/rolls back at 305–309; P/storage/store.py:320–323 starts BEGIN IMMEDIATE; P/graphs/reactor.py:552 already owns the flag transaction. Extract caller-owned compiler and flag-write primitive. Publish flag, child, binding, receipts, deadline, durable worktree plans and acceptance notification atomically. Finish immutable artifacts before publication; orphan unreferenced files are tolerable, half-published references are not. No nested BEGIN, second connection workaround, git or network under writer.

C02. Writer/admission STILL REQUIRED but split among semantic owners. A/workflows.py:86–138 handles completion; :198–236 [R2-C8] performs role/launch/remote admission. P/graphs/authority/commands.py:24 defines SetFlag; authority/__init__.py:205 forwards it; generation.py:686 consumes it. WorkflowTick is generation.py:469, not an IPC-thread mutation. Carry expansion through typed production and injected-test paths. External admission/base resolution on bounded lanes; recheck request/token/policy/deadline/binding under writer. New parent/child preview routes register in daemon/impl/ipc/router.py:179–186,377–390. Dry run writes no DB/artifacts/worktrees/alerts/launches.

C03. Grammar contradiction STILL PRESENT. P/contracts/workflow.py:203–236 requires name/version; :267–298 rejects expansion/expands/limits; :327 requires task; :393–421 permits DAG plus explicit end-backed rework. Approved example integrate needs task and every cwd must satisfy policy. Freeze special child envelope {version:2,nodes,edges?}, generate name, forbid root defaults/workers/on_failure/escalate_to/name. Parent source task/role:plan has one target; placeholder has exactly that planner as incoming forward dependency. No orphan/multiple source, worker/owner/launch on placeholder, or back edge touching endpoints. Child task/report only, no nesting/back edges/shared workers. These implement v1, not structural editing.

C04. System identity/storage STILL REQUIRED. identity/impl/principal.py:69–117 accepts user:/agent:, not system:pac. P/storage/store.py:72–86 and migrations/upgrades.py:86–115 restrict physical kinds. Use physical task + workflow_nodes.node_kind='expansion'; public projection expansion. Owner via hyprial.kernel.canonical_agent_uri(local_owner,machine,'pac-expansion'), never handwritten URI. Reserved system marker, not launched/borrowed agent. Parent roster ownership='system', no actorNode/worker receipt. Admission skips placeholder, not borrowed-agent lookup. Public complete/fail/reset refuse even if caller presents reserved URI. Public PAC event/snapshot kind remains physical task; workflow status alone projects expansion (I6 contracts).

C05. Placeholder state STILL REQUIRED. runtime/core.py:288–368 _request emits turn/delivery; :425–599 _project would request an actorless task. Branch before generic dispatch. Unbound pending; acceptance atomically requested with internal request/input token/generation=1/fixed D, no inbox turn/delivery. Child completed -> real flag/done; failed/cancelled -> failure; parent closure -> cancellation/cascade. Planner done without binding is invariant failure. Internal request never human-completable; projection idempotent.

C06. Flag/token correction STILL REQUIRED. P/workflows/graphs.py:333–381 hashes predecessor flag sequences; runtime/core.py:461–531 detects changed inputs. Updating workflow_nodes alone cannot release join. Extract reactor.py:614–670 caller-transaction flag primitive; never public set_flag from _project. Managed _plan_set at reactor.py:262–279 defers forward requests to projection. Ancestor invalidation cancels child/fails placeholder, never generation2. Preserve graphs/edits.py:173–190 freeze.

C07. Fixed graph cap STILL REQUIRED. runtime/core.py:298–327 starts relative timeout on first request; graphs.py:398–424 rejects at>D, not at==D; actor_ready :384–395 also needs cap. D=min(existing/authored placeholder deadline if any, acceptance_at+timeout). Persist binding/child D; declared child timeout <=D-acceptance_at. Each first child request deadline=min(D,first_request_at+timeout,authored absolute deadline); never refresh on retry/restart/hold. Enforce pending/startup too. At>D cancel child/fail placeholder pac:deadline-expired; accepted replay ignores new time. Sequential timeouts cannot cumulatively extend D.

C08. Failure policy STILL REQUIRED. runtime/core.py:370–423 applies terminate/hold; :541–597 blocks descendants/runs independent branches for continue. Child internally fail-fast on_failure=terminate; terminal child then invokes PARENT _failure. Never inherit hold into child and await terminal forever. Cancellation is failure except parent-driven cascade. Deterministic failed node or graph-level suffix. Parent hold is not v1 reset/replan; new workflow required for failed expansion.

C09. Roster claim remains overstated. P/contracts/roster.py:27–81 hashes graph id/ordered business-node ownership; store.py:549–624 validates canonical roster and exact actor receipts/owner/launch sets, not all arbitrary borrowed business-node insertions. Do not depend on that overstatement. Original parent roster contains system placeholder; independent child roster/spec/receipts; never rewrite parent. Validate system marker/node_kind without fabricated receipts. Both-NULL exception historical only.

C10. Cwd pinned BEFORE compile. graphs.py:129–143,175–247 pins spec/launch/receipts; actors/coordinator/start.py:145–196 validates/caches resolved launch; migrations/__init__.py:250–270 protects receipt identity. Deterministic child/path/branch/effective cwd precede hashes. Resolve base to commit OID in bounded preflight, not launch time. Separate author-input digest from derived-spec digest. No cwd rewrite in reconcile.

C11. Exact replay STILL MISSING LOCALLY. reactor.py:552–613 rejects closed/stale/already-set before :712–738 receipt insert. D/remote/__init__.py:480–506 compares only actor/action/reason/output. After caller authentication, BEFORE closed/current-request/deadline checks, under writer compare graph,node,request,verified actor,action,reason,output,canonical author-expansion digest. Exact accepted replay returns original event/child after close/restart/config change; changed payload conflicts. Invalid expansion consumes no receipt. No UUID regeneration, admission/git/model-policy revalidation for accepted replay.

C12. Binding STILL REQUIRED. migrations/upgrades.py:534–560 has no parent/binding; :591–602 original outbox/receipts; :644–649 only output_text. Persist unique parent/placeholder + unique child, planner/request,generation=1,author digest,effective limits,accepted_at,D. workflow_nodes.generation is activation count, not expansion generation. Reject bound planner/placeholder resets. SQLite ADD COLUMN cannot install a two-column FK: authoritative binding has composite parent-node FK; added graph columns have paired-null/parent-existence/identity triggers and indexes, plus agreement checks with binding. Do not pretend ALTER provides composite FK; avoid nodes/journal rebuild.

C13. Trusted policy DOES NOT EXIST; config landscape changed. kernel/impl/configuration/persistent_config.py:203–228 is channels/users. D/matrix.py:38–120 now has operator dispatch-policy.json, but only workflowReminder/advice, not authorization; advice fails open. Add separate trusted HOME/pac-expansion.json, never client cwd/home. Missing/bad policy fails closed for expansion-enabled admission only. Snapshot defaults/ceilings in parent; widening cannot widen it; later tightening constrains NEW acceptance, not running-child mutation. Empty lists mean none. No HQ path constant or distributed-policy-service claim.

C14. Validator detail holds but tier_for_model MOVED. Use public hyprial.kernel TIERS/tier_for_model (kernel/impl/facts/tier_vocab.py:45–72), not matrix.tier_for_model. Highest-tier semantics: fable/astra super even when also in strong. Current fast head codex/gpt-5.6-luna, strong head codex/gpt-5.6-sol; don't freeze old names. Reuse contracts/workflow.py:73–121 launch normalization/static selection, check actual model tier even for tier launch. No probes/fallback/minimum-tier authorization; unknown explicit model rejected. Canonical absolute/symlink/.. containment, repo/generated root within tightened cwdUnder; exact canonical owners; no borrowed launch/nonempty args. Bound bytes/depth/aliases, reject duplicate/non-string keys/non-JSON values; count business nodes only.

C15. Per-node != per-actor. contracts/workflow.py:302–326,409–421 allows shared workers; graphs.py:168–196 creates actor per worker; coordinator/start.py:21 reconciles actors. Restricted child disallows sharing. Only workflow-owned execute gets worktree. Borrowed local/remote never relocated/cleaned. Authorized explicit cwd retains approved override: prepare/track promised managed worktree, preserve effectiveCwd, show both and isolation-bypass warning; never remove explicitCwd. Non-execute needs authorized explicit cwd if no managed default.

C16. No effects under writer STILL REQUIRED. actors/daemon.py:591 delegates reconcile; authority/__init__.py:294–395 runs effects; generation.py:126–202,314–365 fences results. No direct blocking git in coordinator. Durable prepare/remove intent -> bounded effect -> writer-fenced result; prep failure before launch. Verify repo/common-dir/path/branch ownership and safe crash replay. No shell/fetch/install/push. Budget base resolution too. Existing shell worktree inventory is not daemon authority: upward shell import violates layers.

C17. Cascade/cleanup STILL REQUIRED. runtime/types.py:93–179 closes one graph/creates receipt cleanup intents. Extend transactionally to open children, preserve terminal outcomes. coordinator/core.py:159–331 and start.py:21–143,331–542 preserve mismatch/stop-timeout attention. actors/daemon.py:418–439 stops closed actor scanning after cleanup settles; separate pending-worktree scan needed. Only remove after proven worker absence/complete; attention/dirty/untracked/locked/replaced/mismatch retained/attention. No force/rm-rf/branch delete; branch/commits remain for integration. Cleanup debt separate from business result.

C18. Ordinary remote CHILD TASK reusable; remote PLANNER completion NOT unchanged. D/remote/__init__.py:193–306,480–506,735–818 and authority/generation.py:551–586 omit expansion custody. remote/wire.py:16,85–108,131–185 caps frames 256KiB and drops error.data. Carry text/digest/attempt through outbox/wire/origin/receipt/result. Expansion <=65536 UTF-8 bytes AND full encoded frame <=256KiB including grant/reason/output; no truncation/cap increase. Replace rejected only definitive PAC_EXPANSION_INVALID/no accepted receipt/still-current request; attempt CAS fences late result. Pending/accepted immutable. Negotiate expansion-v1 capability BEFORE forwarding, else old origin may ignore new field. Child origin-owned. Remote preview uses scoped origin authority, never remote HOME policy.

C19. Brief gap remains. runtime/types.py:251–261 inputs from NodeRow.to_json; store.py:146–169 lacks outputText. Add bounded predecessor output to immutable request body from same snapshot/input token. Planner brief includes target, limits, required expansion, dry-run/retry. Placeholder output = human summary PLUS sha256 of full canonical child results: sorted nodeId/state/reasonRef/output head/branch/managedPath/effectiveCwd; branch only durable plan facts. Bound aggregate UTF-8 delivery and 64KiB output. Full results remain accessible to authorized child inspectors; don't widen arbitrary node-inspect ACLs (A/workflows.py:174–183). P5 interim: artifact PATH in review task text until predecessor delivery lands.

C20. Surface correction plus moved renderer. runtime/operations.py:278–457 lacks relations; preserve lastProgressAtMs/currentNode/stale/deliveryFailureCode/cleanup. P/views/overview.py:69–127 discards unknown relation fields; :219–253 name links/mission grouping. Add explicit relation/task-line inheritance and separate nodeLinks, never child nodes inside parent DAG/roster. Keep unrelated heuristics and filtered/missing-parent fallback. CLI workflow.py:207 status generic; list renderer now shell/impl/cli/commands/observe/overview.py:18. Workflow CLI alone no longer suffices.

C21. Journal/alerts/errors STILL REQUIRED; preserve #987. P/storage/journal.py:20–35,101–119 closed vocabulary/transaction requirement. Use workflow_changed(expansionAccepted), existing flag_set/graph_closed and durable actor_alert. Exactly one durable acceptance identity != exactly-once network. Creator sender must resolve. #987 terminal delivery failures: migrations/__init__.py:304–318,reactor.py:420–499,runtime/operations.py:24–43; honor failed_at, no hot-loop resurrection. A/workflows.py:137–138,runtime/types.py:64–67,remote wire drop .data in some paths; preserve field/reason end-to-end.

C22. All integration/docs seams still required, now mapped; settled design not reopened. Fix missing task/unauthorized cwd/v0.1 wording/former questions. Published additive fields/errors require contract/; worker procedure needs repository skill. Leaf options need help=; root help generated by shell/impl/cli/inventory.py:33, no invented snapshot/root command.

R23 NEW LAYERS. scripts/ci/check_module_boundaries.py:19–22,459–484 enforces kernel -> identity -> daemon -> biz -> shell; cross-domain only public package/interface.__all__. PAC same-daemon impl imports allowed, no biz/shell. Shell uses hyprial.daemon/biz, never daemon.impl; preserve lazy imports/CLI startup budget. PacError/constants are identity; tier vocabulary/budgets kernel. Shared contracts-first public exports; no copied validator/error class/CLI selection authority. CLI uses shell/impl/cli/output CliResult/render/warn/confirm, never print/typer.echo; tests/shell/cli/output/test_output_discipline.py:61–84.

R24 NEW LIMITS/OWNERSHIP. [R2-C7] P/views/overview_page.html is at 957/1000 lines and S4 owns it: S4 must move the expansion nodeLinks rendering into a NEW sibling template or script file inside P/views/ (keep the directory <=7 direct files) if its edit would take overview_page.html past 990 lines; if no split fits the limits, STOP and return to the integrator, never exceed 1000. contracts already six direct files; use expansion subpackage. runtime/operations.py 748 lines, reactor.py 898, CLI routines/workflow.py 916: helpers, not append-until-overlimit or exemption. Every product/test code directory <=7 direct files and each code file <=1000 lines. T/graphs already seven files; new tests in T/expansion/engine. tests/tooling/architecture/refactor/test_pac_refactor_layout.py:32–78 + structural scripts enforce current packages/retired names.

R25 NEW GUARD HAZARDS. T/architecture/test_pac_tripwire.py:27 still scans absent src/hyprial/pac: source scan VACUOUS though planted-reader test may pass. Own first prerequisite commit repairs root to daemon/impl/pac, current banned imports, nonempty assertion; preserve negative/mutation checks. tests/integration/native_harnesses/test_native_resident_tripwires.py:653–700 finite NATIVE_ORIGINS rejects ANY unclassified new daemon .py, consumed by tests/shell/login/device/test_boundaries.py:28–43. Common base seeds exact modules/registers conservative resident origins with explicit new-module provenance, not invented historical relocation or directory waiver. Retain mutations. tests/identity/providers/lifecycle/availability/support.py:67–85 exact selection-callsite inventory: reuse workflow.launch, no new resolver reader; any necessary added reader is integrator-reviewed/registered BEFORE redispatch.

R26 / P1 RESOLVED. #987 landed as 4b51a309b9c781c0004db5c2287f4dd96b0206ff and is ancestor. Current SCHEMA_VERSION=17 at migrations/upgrades.py:20. S2 alone implements 17->18 on common base from exact HEAD above, never 16->17. Existing assertions: T/graphs/test_pac_migrations.py:44,197,263 and T/workflows/delivery/test_pac_workflow_delivery_failure.py:326 expect17. Synthetic downgrades start from latest (:267–305 delivery; migration helpers :142–181,245–257): assertion-only update wrong. Drop v18 structures/columns/triggers for downgraded fixtures or construct version-specific fixture. Preserve #987 failed_at/failure_code/failure_detail/indexes in genuine v17 case. Never rewrite WORM fixtures. Also S2 owns T/contracts/test_pac_v8_rewrite.py:130,280 and T/workflows/runtime/test_pac_workflow_roster.py:282. The former builds latest via PacGraphStore then _pin_to_v7 (:96–119): remove v18 tables/triggers before dropping their referenced workflow tables; do not leave v18 debris under version7. The latter builds a current workflow then strips roster/receipts/cleanup and #987 additions to label v14 (:241–278): first strip every v18 column/table/index/trigger too. Preserve pre-upgrade node/roster comparison and receipt backfill; assert18 only after real upgrade.

R27 DROPPED BY SCOPE RULING (user-exp-improver, 2026-10-06; see SCOPE SIGN-OFF). No authored_specification_digest column, parent_author_digest API or new parent-operation replay semantics in v1. Existing graphs.py:63–90 resolved-spec replay remains; changed defaults may conflict. Child author-expansion digest versus derived-spec digest and accepted child-completion replay (C10–12) remain required.

SCOPE SIGN-OFF [R2-C2]
 (a) R25 tripwire repair as its own first commit: SIGNED by Allen (林懿伦), 2026-10-06, Feishu.
 (b) R27 authored_specification_digest column and parent replay: DROPPED; ruled by user-exp-improver (feature owner), 2026-10-06.
 (c) remote admit features:['pac-expansion-v1'] with mixed-version refusal, which goes beyond design-pac-expand.md:79 ("Remote owners work unchanged") and changes contract/pac/remote/README.md: SIGNED by Allen (林懿伦), 2026-10-06, Feishu.
 (d) PAC_EXPANSION_PREFLIGHT_TIMEOUT_SECONDS: not added (reuse PAC_WORKTREE_GIT_TIMEOUT_SECONDS); deadlineProvisional: DROPPED; ruled by user-exp-improver, 2026-10-06.

3. PLAN — CONTRACTS-FIRST BASE THEN FOUR FILE-DISJOINT PARALLEL SLICES

0. INTEGRATOR PREREQUISITE (not fifth parallel worker)
[R2-C4] Start from the branch HEAD at dispatch (it contains the merge of origin/dev 74c86b5f, 4a995627; re-verify SCHEMA_VERSION is still 17 and that identity/interface.py and tests/daemon/startup/test_startup_event_reconciliation.py match the merged dev before editing), NOT 6005943d or cea1475c. First make a separate testable conventional R25 tripwire-repair commit owning only T/architecture/test_pac_tripwire.py; run its nonempty scan and planted-reader/mutation tests. Then make the contracts-first commit; record its actual SHA BASE_CONTRACTS in every brief. No invented future SHA: no worker starts before commit exists; all four separate worktrees fork exactly it. S2 migration explicitly v17->v18 on BASE_CONTRACTS. Re-audit base changes.

Exclusive contracts-first files (tripwire file belongs to preceding repair commit):
 P/contracts/workflow.py (additive data contract only, then transfers to S1)
 src/hyprial/identity/impl/pac_errors.py
 src/hyprial/identity/interface.py
 src/hyprial/kernel/impl/contracts/daemon/lifecycle_budgets.py
 src/hyprial/kernel/interface.py
 NEW P/contracts/expansion/types.py
 NEW contract/wire/pac-expansion.md
 tests/integration/native_harnesses/test_native_resident_tripwires.py
 NEW tests/daemon/pac/expansion/__init__.py
 NEW tests/kernel/contracts/test_pac_expansion_contracts.py
 NEW tests/identity/principals/test_pac_expansion_errors.py
 [R2-C5] Seeded shells (exclusive to the prerequisite, docstring/package shell only, then owned by the named slice):
  S1: P/contracts/expansion/{__init__,policy,document,validation}.py
  S2: P/workflows/expansion/{__init__,transactions,projection,results,storage}.py
  S3: P/actors/worktrees/{__init__,git}.py
  integrator: D/remote/expansion.py
  plus any other NEW product path named in S1-S4/I1, enumerated in the prerequisite commit message.
 Also seed ALL exact new PRODUCT paths in S1–S4/integration with harmless module docstrings/package shells, classify them in finite inventory; ownership transfers after prerequisite. No feature stub claiming success. types is data-only, no copied validators. No import of absent implementation from public faces. No parallel worker edits types/exports/inventories/budgets. Before branching, workflow.py:124–180 gains defaulted WorkflowNode.expands/limits and WorkflowSpec.expansion_policy, conditional serialization, and ownership literal system for kind expansion; ordinary canonical bytes unchanged. S1 retains parsing/validation; S2 fixtures can use these values immediately. PreparedExpansion lives in P/contracts/expansion/types.py (frozen data-only dataclass, WorkflowSpec type reference, no validator/DB imports).

P2 resolved here: register/export PAC_WORKTREE_GIT_TIMEOUT_SECONDS=30.0; reuse this sole git budget for base-resolution preflight, not multiplied per subprocess. Do NOT add PAC_EXPANSION_PREFLIGHT_TIMEOUT_SECONDS absent a concrete test requiring a distinct bound; any such need returns to integrator for contract/export/test amendment before redispatch. All resolve_base/prepare_worktree/cleanup_worktree take REQUIRED keyword timeout_s, NO DEFAULT. Integrator feeds named budget capped by remaining operation/placeholder time; helper uses one monotonic deadline across commands. Any additional lane bound must be registered first, not inline/default. No new daemon close step planned; conditional P4 below. Prerequisite tests errors/exports/budget shape/required keywords once functions land, plus common guards; maintain an independently passing base without feature-success stubs.

FREEZE in contract/wire/pac-expansion.md BEFORE dispatch:

F1 POLICY/GRAMMAR
 Trusted HOME/pac-expansion.json={version:1,workRoot:<absolute>,limits?:{maxNodes,tiers,owners,cwdUnder}}. Only version/workRoot required; omitted individual limits default12,[fast,strong],[],[workRoot]. maxNodes strict int1..100, not bool; unique string lists; org cwdUnder within workRoot. Explicit [] means none. Parent same four only tighten, optional worktree:{repo:<absolute>,base:<git ref>}; cannot change workRoot. Canonical/symlink-safe path containment, including generated root under narrowed cwdUnder. Daemon-injected policy, never planner-controlled.
 WorkflowNode new expands:str|None=None,limits:dict|None=None; absent keys omitted. Placeholder ownership system. WorkflowSpec new expansion_policy:dict|None=None, serialized expansionPolicy only for expansion-enabled parents to record org/workRoot snapshot; not accepted as author override. Ordinary canonical bytes unchanged.
 Parent planner task/role plan/expands target. Placeholder {id,kind:expansion,after:[planner],timeout,limits?}, optional/generated task, no owner/worker/launch/expands; one source/target, one incoming forward edge, no endpoint back edge.
 Child root ONLY version2,nodes,optional edges. Node ONLY id,kind(task/report),task,role,owner OR launch,after,timeout,deadline_ms,first_output_eta,human_gates. No shared worker/defaults/workers, nested expands/limits/expansion, approval/end/back edges. Normal role rules apply. Owned default tier fast; authorized explicit cwd except managed execute default. Borrowed no launch; nonempty native args rejected. Generated bounded name; creator/escalation inherited; child on_failure terminate.
 Managed path <workRoot>/.pac-worktrees/<childGraphId>/<nodeId>, branch pac/<childGraphId>/<nodeId>, expansion generation1. Explicit cwd permitted under ceiling, with visible isolation-bypass warning.

F2 ERRORS
 Define in identity/impl/pac_errors.py, export hyprial.identity:
 PAC_EXPANSION_INVALID, PAC_EXPANSION_POLICY_UNAVAILABLE, PAC_EXPANSION_ALREADY_BOUND, PAC_EXPANSION_SYSTEM_NODE, PAC_WORKTREE_PREPARE_FAILED, PAC_WORKTREE_IDENTITY_MISMATCH, PAC_WORKTREE_CLEANUP_FAILED.
 PAC_EXPANSION_INVALID data={field:<JSON-style path>,reason:<stable tag>}; tags syntax,unknown-field,required,unexpected,kind,nested,cycle,count,tier,owner,cwd,timeout,args,worker-sharing,transport-size.
 Retain WORKFLOW_SCHEMA_ERROR (bad/widened parent), WORKFLOW_OUTCOME_CONFLICT, WORKFLOW_REQUEST_STALE, WORKFLOW_DEADLINE_EXPIRED, WORKFLOW_OUTPUT_INVALID. No duplicate IPC literals to game registry; generic registered errors through hyprial.kernel.ipc_errors.
 Worktree error data bounded graphId,nodeId,path,stage; no env/secrets. Unsupported peer -> existing WORKFLOW_REMOTE_INVALID with data {field:'expansion',reason:'unsupported-peer'}, never downgrade. Service/application/remote .data preserved.

F3 PUBLIC/WIRE
 workflow.complete optional expansion:<YAML/JSON string> <=65536 UTF-8 bytes, never origin filename. CLI --expansion bounded local UTF-8 read; MCP workflow_complete expansion:str|None=None. No expansion on fail; reject unexpected, not drop.
 workflow.expansion.plan params {graphId,nodeId,yaml,actor,sessionRef?}, placeholder nodeId. Verified creator OR corresponding planner only. Remote planner uses scoped origin grant, not general proxy. Response {ok:true,graphId,nodeId,expansionDigest,deadlineMs,effectiveLimits,plan,warnings}; before binding deadlineMs is only an estimate, acceptance fixes D. No deadlineProvisional field, lease/artifact/reservation/attempt.
 workflow.plan {yaml,actor,sessionRef?} authoritative parent policy preview. Ordinary offline plan preserved; expansion-enabled plan/run confirmation uses daemon preview; start revalidates.
 [R2-C3] Decision: childGraphId and expansionDigest are carried ONLY in the workflow.complete IPC response and in the new outcome-receipt columns/result_json used for replay. The PAC journal flag_set/flag_reset event data is UNCHANGED, so contract/pac/events/schema.json needs no new data fields (the event/snapshot kind stays task); the expansion linkage is observable through workflow_expansions and the status/inspect projection. Ordinary keys unchanged; replay original response/child. Remote pending/accepted/rejected unchanged.
 Remote admit/capability response adds features:['pac-expansion-v1']; absent refuses expansion-specific forwarding, ordinary remote tasks compatible. Outcome carries expansion,expansionDigest,attemptNo; origin recomputes digest. Full encoded MAX_FRAME=256KiB. CAS attemptNo; corrected only definitive PAC_EXPANSION_INVALID/no receipt/still-current request; pending/accepted immutable, late prior reply cannot overwrite.
 Child status/list adds parentGraphId,parentNodeId. Placeholder kind:'expansion',systemOwned:true,childGraphId,childState,expansionDigest,deadlineMs,childNodes:[{nodeId,state,reasonRef}],optional worktreeCleanup. No system delivery/human action. Preserve stale,lastProgressAtMs,currentNode,deliveryFailureCode,workerCleanup; one-level bounded snapshot-consistent summary. Observation is projection, not second authority.
 Overview nodeLinks=[{from:{graphId,nodeId},to:{graphId,nodeId},kind:'expand'}], planner->each child, child sinks->placeholder successors; distinct from graph links. Explicit parent task-line/mission wins over name guesses; unrelated heuristics preserved.

F4 INTERNAL SIGNATURES/VALUES
 S1:
 policy.load_expansion_policy(home:Path)->dict
 policy.effective_limits(overrides:dict|None,*,policy:dict)->dict
 document.expansion_digest(raw:str)->str
 validation.validate_parent(spec:WorkflowSpec,*,policy:dict)->WorkflowSpec
 validation.validate_expansion(raw:str,*,limits:dict,child_graph_id:str,deadline_ms:int,at:int,name:str,escalate_to:str|None)->WorkflowSpec
 Author canonical parsed digest independent of resolution/cwd/time/policy; duplicate/non-string keys/non-JSON/alias/depth abuse bounded/rejected. Pure validator no DB/network/git writes; existing workflow.launch for normalization, public tier_for_model for ceiling. Effective policy snapshotted only trusted-side.
 S2:
 workflows.graphs.prepare_workflow_artifacts(store,spec)->dict with specificationRef,specificationDigest,launches:{worker:{ref,digest}}; immutable/digest-verified before SQL publication.
 workflows.graphs.compile_workflow_in_transaction(store,spec,*,sender,machine,local_owner,operation_key,at,prepared_artifacts,graph_id=None,parent_graph_id=None,parent_node_id=None,expansion_deadline_ms=None,routine_name=None,task_key=None)->str
 Caller transaction required; no begin/commit/rollback/effect. Existing wrapper signature retained; prepares artifacts then invokes primitive. Child id='wf-'+uuid5(NAMESPACE_URL,'pac-expand:<parent>:<placeholder>:1').hex. operation_key='expand:<parent>:<placeholder>:1', existing workflow: storage prefix retained.
 PacReactor.set_flag(existing args,*,expansion:str|None=None) keeps FlagEventOutcome/existing keywords; constructor gains expansion_context:PreparedExpansion|None=None. Production command preflight passes trusted per-command context; tests inject. Not IPC input, no substitute for writer fences. Expanding planner cannot complete without required payload/trusted validated context; accepted replay is resolved before preflight.
 PreparedExpansion shared frozen dataclass in P/contracts/expansion/types.py: parent_graph_id:str,planner_node_id:str,planner_request_id:str,placeholder_node_id:str,child_graph_id:str,expansion_digest:str,captured_input_token:str,effective_limits:dict,policy_snapshot:dict,spec:WorkflowSpec,prepared_artifacts:dict,worktrees:tuple[dict,...]. Writer computes authoritative at/D, rechecks request/token/binding/current trusted policy and pure validity vs prepared facts/remaining time. Preflight does not mint acceptance timestamp. Derived spec artifacts do not encode preflight-time relative graph D; stored expansion_deadline_ms authoritative. Reject changed/inadequate facts with no publication, retry possible.
 WorktreePlan={graphId,nodeId,actorNode,repo,baseRef,baseOid,branch,path,effectiveCwd,operationId}; canonical paths, operationId='pac-worktree:<child>:<node>', immutable, local workflow-owned execute only. Durable ownership proof distinguishes legitimate post-crash worktree from unrelated pre-existing path; directory name alone never authorizes adopt/remove.
 P/graphs/flags.py (S2-owned) transaction-local internal flag writes nodes,flag_events,journal/projection with captured token; never public set_flag or generic request delivery.
 S3:
 resolve_base(repo:Path,base_ref:str,*,timeout_s:float)->str
 prepare_worktree(plan:dict,*,timeout_s:float)->dict
 cleanup_worktree(plan:dict,*,timeout_s:float)->dict
 Result={state:'prepared'|'removed'|'retained'|'attention',path,branch,baseOid,reason:str|None}; errors F2. Required finite positive timeout, no default, one aggregate operation allowance. Caller proves worker absence for cleanup, helper does not infer lifecycle authority.

F5 STORAGE — S2 ONLY
 v18 workflow_graphs adds nullable parent_graph_id,parent_node_id,expansion_deadline_ms. Paired-null/parent-reference/immutable identity enforcement, lookup/uniqueness indexes; composite parent-node FK on authoritative binding, graph pair checked against binding/existing node.
 workflow_nodes.node_kind TEXT NOT NULL DEFAULT 'task'; compiler fills expansion/end; preserve legacy end projection.
 workflow_expansions: (parent_graph_id,parent_node_id) PK,child_graph_id UNIQUE,planner_node_id,planner_request_id,generation CHECK=1,expansion_digest,limits_json,accepted_at,deadline_ms. Composite node/child FKs; identity/policy/time immutable; graph columns agree.
 workflow_outcome_receipts adds nullable graph_id,node_id,expansion_digest; new rows bind identity, original event/child in result_json; no invented legacy metadata.
 remote_workflow_outbox adds nullable expansion_text,expansion_digest and attempt_no INTEGER NOT NULL DEFAULT0; integrator CAS.
 workflow_worktrees: graph_id,node_id PK,actor_node,plan_json,state(planned/prepared/cleanup_pending/removed/retained/attention),nullable reason,updated_at; unique managed path/operation identity, immutable plan,node/actor references; no borrowed plans; distinct worker cleanup state. Explicit INSERT columns everywhere; no parallel migration via store.SCHEMA.
 Owner migration: src/hyprial/daemon/impl/owner_migration/planning.py:148–200 scans PAC TEXT columns. Integrator updates its sibling vocabulary.py ARCHIVAL_COLUMNS by exact (table,column), never a table wildcard. ARCHIVE byte-identical: workflow_graphs.{parent_graph_id,parent_node_id}; workflow_nodes.node_kind; workflow_expansions.{parent_graph_id,parent_node_id,child_graph_id,planner_node_id,planner_request_id,expansion_digest,limits_json}; workflow_outcome_receipts.{graph_id,node_id,expansion_digest}; remote_workflow_outbox.{expansion_text,expansion_digest}; workflow_worktrees.{graph_id,node_id,actor_node,plan_json,state,reason}. [R2-C6] The two PRE-EXISTING result_json columns (workflow_outcome_receipts.result_json, remote_workflow_outbox.result_json; upgrades.py:593,599) have no immutability trigger and stay on the existing rewrite path, so migration behaviour for existing rows is unchanged; replay compares the stored expansion_digest/child id columns, not result_json bytes. This classifies every new TEXT column in F5. IDs/digests/vocabulary are not principals; limits_json is accepted policy history, expansion_text the authored digest-bound payload, plan_json immutable path/operation proof, result_json the original replay result. Rewriting any breaks immutability/digests/custody. Keep live principal/routing columns on the existing rewrite path; snapshots confer no new routing/authorization after rename. New attempts reauthorize against live identity/policy and may fail closed; never reinterpret old archived owners as new grants. Any extra materialized TEXT path/operation column requires an exact classification before DDL freeze. Integrator test_pac_graph.py tests real v18 preview/apply, archival byte equality/digests, live routing rewrites and fail-closed mismatches; no trigger disabling to make migration pass.

PARALLEL OWNERSHIP: listed files only. New shells transfer from prerequisite; separate test subdirs. No shared conftest/deps/locks/domain faces/registry/integration seams. S2 injected spec/context fixtures, S3 pure temp git, S4 mocked IPC avoid cross-worker unfinished code. Use neutral tmp_path/work/repo fixtures, never copied retired-product path tokens from design examples. Necessary shared guard changes go to integrator before redispatch, never worker exemption.

S1 POLICY
 Goal strict grammar/trusted ceilings/pure validators.
 EXACT OWNED:
 P/contracts/workflow.py
 NEW P/contracts/expansion/__init__.py [R2-C5: seeded shell, absent at HEAD]
 NEW P/contracts/expansion/policy.py [R2-C5: seeded shell, absent at HEAD]
 NEW P/contracts/expansion/document.py [R2-C5: seeded shell, absent at HEAD]
 NEW P/contracts/expansion/validation.py [R2-C5: seeded shell, absent at HEAD]
 NEW T/expansion/policy/__init__.py
 NEW T/expansion/policy/test_schema.py
 NEW T/expansion/policy/test_policy.py
 Implement F1/F2/F4 parsing/validation atop base-owned additive fields/serialization/system literal, existing launch reuse; no DB/config writer/effect lane.
 Tests: ordinary canonical compatibility; corrected examples; required/unexpected expansion; binding uniqueness/orphan/multiple/back-edge; forbidden roots/kinds/sharing; bool/count/zero/empty/omitted limits; tightening; bad/missing policy only expansion; exact owners; symlink/../prefix escapes; repo/managed root; actual default/highest-tier fable/astra/unknown; args; timeout boundaries; formatting-stable digest; alias/depth/duplicate keys; explicit cwd warning.
 CHECK:
 uv run --offline --no-sync pytest -q tests/daemon/pac/expansion/policy/test_schema.py tests/daemon/pac/expansion/policy/test_policy.py tests/daemon/pac/workflows/runtime/test_pac_workflow_schema.py tests/identity/providers/lifecycle/availability/test_selection.py
 Then COMMON GUARDS.

S2 ENGINE
 Goal atomic compiler/binding/outcome, child cap, internal projection/cascade/query/delivery facts.
 EXACT OWNED:
 P/storage/migrations/__init__.py
 P/storage/migrations/upgrades.py
 P/storage/migrations/expansion.py (new shell,17->18)
 P/storage/store.py
 P/workflows/graphs.py
 P/workflows/runtime/core.py
 P/workflows/runtime/operations.py
 P/workflows/runtime/types.py
 P/graphs/reactor.py
 P/graphs/flags.py (new shell)
 P/contracts/roster.py
 NEW P/workflows/expansion/__init__.py [R2-C5: seeded shell, absent at HEAD]
 NEW P/workflows/expansion/transactions.py [R2-C5: seeded shell, absent at HEAD]
 NEW P/workflows/expansion/projection.py [R2-C5: seeded shell, absent at HEAD]
 NEW P/workflows/expansion/results.py [R2-C5: seeded shell, absent at HEAD]
 NEW P/workflows/expansion/storage.py [R2-C5: seeded shell, absent at HEAD]
 NEW T/expansion/engine/__init__.py
 NEW T/expansion/engine/test_transactions.py
 NEW T/expansion/engine/test_projection.py
 NEW T/expansion/engine/test_migrations.py
 NEW T/expansion/engine/support.py (slice-local)
 T/graphs/test_pac_migrations.py
 T/workflows/delivery/test_pac_workflow_delivery_failure.py
 T/contracts/test_pac_v8_rewrite.py
 T/workflows/runtime/test_pac_workflow_roster.py
 F4/F5+C01,C04–12,C17,C19–21; real helpers keep reactor/runtime under limits. No production authority/lifecycle/preflight/git/remote wire ownership. Fail closed without context. Bounded predecessor output/planner brief in WorkflowSender, no inbox reply reads. Acceptance alert creator sender and v17 terminal-delivery semantics. No R27 parent-author replay.
 Tests fault injection every publication step, all-or-nothing flag/child/binding/receipt/alert/intents; ordinary wrapper; concurrent/closed/reopen exact replay; changed identity conflict; missing/invalid/stale/expired no receipt/child; system spoof/reset; no placeholder actor/turn; genuine flag releases join once; child terminal/all parent policies; never-started cleanup cascade; sequential/pending/start/hold D; ancestor invalidation no gen2; parent unchanged; immutable child receipts; bounded sorted summary/durable branches; fresh/genuine #987v17/older upgrade/rollback/reopen/FKs/triggers; failed alert state retained. Repair downgraded fixture construction, not label-only version change.
 CHECK:
 uv run --offline --no-sync pytest -q tests/daemon/pac/expansion/engine/test_transactions.py tests/daemon/pac/expansion/engine/test_projection.py tests/daemon/pac/expansion/engine/test_migrations.py tests/daemon/pac/graphs/test_pac_migrations.py tests/daemon/pac/workflows/delivery/test_pac_workflow_delivery_failure.py tests/daemon/pac/workflows/runtime/graph_service/completion/test_completion.py tests/daemon/pac/workflows/runtime/graph_service/deadline/test_deadline.py tests/daemon/pac/workflows/runtime/graph_service/graph_contracts/test_graph_contracts.py tests/daemon/pac/workflows/runtime/graph_service/workers/test_workers.py tests/daemon/pac/workflows/runtime/test_pac_workflow_roster.py tests/daemon/pac/lifecycle/test_pac_worker_terminal_cleanup.py tests/daemon/pac/contracts/test_pac_v8_rewrite.py tests/daemon/pac/contracts/test_pac_envelope.py tests/daemon/pac/dispatch/test_pac_subscription.py
 Then COMMON GUARDS.

S3 GIT EFFECTS
 Goal bounded replayable prepare/non-destructive cleanup without graph writer dependencies.
 EXACT OWNED:
 NEW P/actors/worktrees/__init__.py [R2-C5: seeded shell, absent at HEAD]
 NEW P/actors/worktrees/git.py [R2-C5: seeded shell, absent at HEAD]
 NEW T/expansion/worktrees/__init__.py
 NEW T/expansion/worktrees/test_git.py
 F4 required-timeout functions; option-safe argv/no shell, local repos/no remotes. OID pin, registration/common-dir/path/branch/operation proof; owned retry permits worker commits, not HEAD==base requirement. Never adopt replaced/symlink/unowned path. Partial add/crash-before-result safe, no force. Cleanup only proven-owned managed path via nonforce git remove, branches/commits preserved. Dirty/untracked/locked retained; ambiguity/failure attention; missing idempotent. Never remove differing effectiveCwd.
 Tests distinct branch/path/shared objects; moved ref retains OID; crash success-before-result/retry-after-commit; mismatch/wrong repo/branch/noncommit/symlink/options; aggregate timeout/no defaults; clean branch retention; dirty/untracked/locked/partial/missing; explicit cwd untouched; no fetch/push/PR.
 CHECK:
 uv run --offline --no-sync pytest -q tests/daemon/pac/expansion/worktrees/test_git.py tests/kernel/contracts/test_pac_expansion_contracts.py
 Then COMMON GUARDS including supervision. No worker budget/exemption edits.

S4 SURFACES
 Goal CLI/MCP/human output/overview against frozen mocked IPC.
 EXACT OWNED:
 src/hyprial/shell/impl/cli/commands/routines/workflow.py
 src/hyprial/shell/impl/cli/commands/routines/workflow_expansion.py (new helper)
 src/hyprial/shell/impl/cli/commands/observe/overview.py
 src/hyprial/daemon/impl/mcp/server.py
 P/views/overview.py
 P/views/overview_page.html
 NEW tests/shell/cli/workflows/expansion/__init__.py
 NEW tests/shell/cli/workflows/expansion/test_cli.py
 NEW tests/daemon/channels/mcp/expansion/__init__.py
 NEW tests/daemon/channels/mcp/expansion/test_tools.py
 NEW T/expansion/views/__init__.py
 NEW T/expansion/views/test_overview.py
 Bounded local --expansion/MCP string; --expansion-for special envelope before ordinary parser; parse target with re.fullmatch(r"(?P<graph>[^:\s]+):(?P<placeholder>[A-Za-z][A-Za-z0-9_.-]{0,63})", value), then named groups and origin lookup. This is a CLI graph-target grammar, not an agent URI; reject empty/multiple-colon/whitespace/invalid-node targets, use public URI parsers for identities. No colon split/rsplit/partition or new address-guard whitelist; expansion parent plan/run confirmation authoritative workflow.plan, ordinary offline/reminders unchanged. Actor/session trust unchanged. Inline human child status/↳ parent list; additive JSON; explicit mission/nodeLinks; escaped/redacted bounded HTML. CliResult/render/warn/confirm only, every option help=.
 Tests exact IPC omissions/presence, UTF-8/size/target, fail rejects expansion, identity binding, preview/run routes, pending/accepted/replay, MCP tool gate/schema, human/JSON, misleading name vs relation, missing/filtered parent, inherited mission, link dedup/escaping, ordinary compatibility.
 CHECK:
 uv run --offline --no-sync pytest -q tests/shell/cli/workflows/expansion/test_cli.py tests/daemon/channels/mcp/expansion/test_tools.py tests/daemon/pac/expansion/views/test_overview.py tests/shell/cli/workflows/test_cli_workflow.py tests/daemon/channels/mcp/contracts/test_mcp_worker_workflow_tools.py tests/daemon/pac/projection/test_pac_overview.py tests/daemon/pac/projection/test_pac_list_overview.py tests/shell/cli/presentation/test_cli_help_coverage.py tests/shell/cli/presentation/test_cli_help_inventory.py tests/tooling/ci/planning/test_ci_cli_help_inventory.py tests/shell/compatibility/test_no_pac_cli_spellings.py tests/shell/cli/output/test_output_discipline.py
 Then COMMON GUARDS.

COMMON GUARDS — EACH SLICE + INTEGRATION, NEVER FULL SUITE
 Before structural edits and after:
 UV_OFFLINE=1 uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py
 UV_OFFLINE=1 uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py
 Missing cached interpreter/dependencies => BLOCKED exact command/error. Supervisor preprovisions isolated environments; no worker installs/downloads, no .venv/$PY assumption. Behavior test first failing then passing, followed by gates. Handoff: uv run --offline --no-sync ruff check .

Exact common pytest command (mixed functional files use exact static guard IDs to avoid unrelated environment scenarios):
 uv run --offline --no-sync pytest -q \
 tests/tooling/architecture/boundary/test_supervision_boundary_gate.py \
 tests/tooling/architecture/boundary/test_actor_boundary_gate.py \
 tests/tooling/architecture/actors/test_cadence_api_boundary.py \
 tests/tooling/architecture/merge/test_hyprial_merge_residuals.py \
 tests/tooling/architecture/boundaries/test_module_boundaries.py \
 tests/tooling/architecture/limits/test_structure_limits.py \
 tests/tooling/architecture/refactor/test_pac_refactor_layout.py \
 tests/tooling/architecture/refactor/test_orgfs_refactor_layout.py \
 tests/tooling/architecture/authority/test_inbox_authority_gate.py \
 tests/tooling/architecture/ref/test_session_ref_authority.py \
 tests/tooling/architecture/workflow/test_workflow_effect_id_gate.py \
 tests/tooling/architecture/ipc/test_ipc_transient_judgment.py \
 tests/tooling/architecture/persistence/test_sqlite_durability_policy.py \
 tests/tooling/architecture/rename/test_hyprial_rename_contract.py \
 tests/tooling/architecture/lint/test_provider_term_lint.py \
 tests/tooling/architecture/repo/test_stale_repo_url_lint.py \
 tests/daemon/ipc/error/test_ipc_error_code_registry.py \
 tests/shell/compatibility/test_unreachable_surface_retirement.py \
 tests/shell/compatibility/test_retired_surface.py \
 tests/kernel/addressing/test_uri_construction_guard.py \
 tests/kernel/addressing/test_address_parsing_guard.py \
 tests/kernel/contracts/test_kernel_refactor_contracts.py \
 tests/kernel/logging/test_log_levels.py \
 tests/kernel/language/test_exception_handler_bindings.py \
 tests/kernel/actors/runtime/test_scheduler_and_boundary.py \
 tests/daemon/channels/adapters/test_channel_lark_prefix_guard.py \
 tests/daemon/composition/boundaries/test_final_composition_boundary.py \
 tests/daemon/lifecycle/journal/test_lifecycle_journal_retention_gate.py \
 tests/daemon/adapters/lark/lifecycle/test_lark_lifecycle_single_writer.py \
 tests/daemon/pac/architecture/test_pac_tripwire.py \
 tests/shell/cli/output/test_output_discipline.py \
 tests/integration/native_harnesses/test_native_resident_tripwires.py \
 tests/shell/login/device/test_boundaries.py::test_t11_daemon_tree_never_imports_login_or_names_the_credential \
 tests/identity/providers/lifecycle/availability/test_selection.py \
 tests/integration/actor_preservation/orgfs/test_actor_preserved_uri_construction_guard.py \
 tests/daemon/orgfs/addressing/test_orgfs_uri.py::test_orgfs_error_code_registry_covers_every_raised_code \
 tests/integration/actor_preservation/orgfs/test_actor_preserved_orgfs_uri.py::test_orgfs_error_code_registry_covers_every_raised_code \
 tests/daemon/harnesses/actors/test_harness_restore_admission.py::test_restore_wait_budget_removed \
 tests/daemon/desired_state/state/store/test_desired_state_store_topology.py \
 tests/daemon/network/test_network_profile.py::test_t9_issuer_literal_only_in_default_profile \
 tests/daemon/adapters/lark/identity/test_lark_endpoint_override.py::test_no_fourth_exit_can_appear_unnoticed \
 tests/shell/login/test_login_u5.py::test_t6_no_getpass_getuser_call_in_src \
 tests/integration/hosted/runtime/test_hosted_scope.py::test_only_receive_chain_calls_hosting_authority \
 tests/tooling/release/publish/github/test_snapshot.py::test_whitelist_constant_has_a_single_home

These are current source-scanning/refactor/authority gates, not stale root filenames. No vacuous scan PASS: tripwire repair prerequisite. Unallocated new file requires fresh inventory/ownership review.

No Go implementation ownership in v1 (PAC/MCP Python, tailcat transport). If integration touches sidecar/wire seam or requests Go guard, ONLY:
 (cd sidecar/hyprial-tailcat && GOPROXY=off GOFLAGS=-mod=mod go test ./internal/...)
 No downloads/go test ./.../imaginary sidecar/internal. Missing cached modules BLOCKED, not skip.

4. INTEGRATION — SINGLE OWNER AFTER WORKERS STOP

I1 Review/cherry-pick S1,S2,S3,S4 onto BASE_CONTRACTS; parallel builds, dependency-order integration. Recheck schema17/sole18 and #987 semantics. Run slice commands/common guards, not full pytest. Shared contracts change after dispatch requires explicit seam agreement, not unilateral worker edit.

Exact production integration files (plus necessary amendments to already-merged slice files):
 P/graphs/authority/__init__.py
 P/graphs/authority/commands.py
 P/graphs/authority/generation.py
 P/graphs/authority/expansion.py (new helper shell)
 P/actors/coordinator/__init__.py
 P/actors/coordinator/core.py
 P/actors/coordinator/start.py
 P/actors/coordinator/stop.py
 P/actors/coordinator/types.py
 P/actors/daemon.py
 A/workflows.py
 A/wiring/services.py
 src/hyprial/daemon/impl/ipc/router.py
 D/remote/__init__.py
 D/remote/wire.py
 D/remote/expansion.py (new custody/preview helper shell)
 D/workflow_observation.py
 src/hyprial/daemon/impl/owner_migration/vocabulary.py
 tests/shell/login/test_login_switch_preview.py [R2-C1: :383 asserts the exact ARCHIVAL_COLUMNS registry; extend its expected set with exactly the F5 archival list]
 tests/daemon/startup/test_startup_event_reconciliation.py [R2-C4: I2 edits DaemonApplication mixins]
 Shared faces/budgets/inventories integrator-only if amendment necessary. No blanket facade/shell-identity/unrelated migration/production-config editing permission.

I2 Inject trusted daemon HOME policy for preview/start/acceptance. Existing parent-operation replay unchanged (R27 dropped); child accepted replay before new deadline/preflight. Register both preview routes in router.py and _IPC_STATS_METHODS (:42), not dispatch alone; reconcile DaemonApplication mixin startup-event inventory. New bounded preflight performs admission/base resolution/path/cwd/artifacts; writer consumes PreparedExpansion and rechecks caller/request/token/policy/D/binding. No writer waiting on itself. Preserve .data. authority/__init__.py:554–574 timeout does NOT cancel accepted mutation: inspect durable result, don't blindly create a second child.

I3 Durable worktree effects in existing authority/lifecycle, no git under writer. Prepare gates start; failure projects child/placeholder failure, never indefinite pending. Effect graph/activation/generation/operation fences; cancel during prepare/start settles cleanup, not resurrection. Late successful start after timeout stays receipt-owned and cleanup/attention-tracked, not discarded orphan. Separate fair/bounded closed-worktree scan after worker cleanup; no hot loop/starvation. Retained/attention stops destructive retry. Stop proof before remove; branches/commits preserved. Production/injected test paths agree.
 P4: keep existing PAC lifecycle, NO new _close attempt planned. If unavoidable, separately update A/shutdown.py + kernel/impl/contracts/daemon/daemon_teardown.py DAEMON_CLOSE_STEP_BUDGETS, derive waiters, run tests/tooling/isolation/test_teardown_guarantee.py. No unregistered close timeout.

I4 Full remote custody: authenticate/scoped grant/capability -> durable text/digest/attempt -> bounded wire -> origin preflight/writer -> receipt -> return. Rejection exact field/reason. CAS corrected invalid only, no pending/accepted/other-error replacement or stale overwrite. Mixed-version no capability refuses expansion, ordinary remote child tasks preserved. Origin-scoped dry run only; no correlation-as-auth or child creation on worker daemon. Update every affected remote SQL INSERT, including direct-test fallback; no filenames over wire.

I5 NEW EXACT INTEGRATION TEST FILES (single owner):
 tests/integration/pac/expansion/__init__.py
 tests/integration/pac/expansion/support.py
 tests/integration/pac/expansion/test_e2e.py
 tests/integration/pac/expansion/test_failures.py
 tests/integration/pac/expansion/test_remote.py
 tests/integration/pac/expansion/test_effect_races.py
 Six direct files; any extra semantic split requires ownership approval; no shared conftest. Existing integrator-owned test: tests/identity/ownership/migration/test_pac_graph.py (F5 archival/rewrite conformance).
 Disposable application+PacGraphAuthority/fake harness/sender/wire, deterministic clock, real temp git, isolated homes/sockets/ports, no provider/network/user-config. Assert:
 - invalid planner completion leaves requested, no flag/child/receipt/alert/worktrees;
 - valid two execute children distinct pinned cwd/branches, one binding/flag/acceptance alert, parent spec/roster/node/edge unchanged;
 - exact replay before/after restart/close same child/event, no fresh admission/git/alert;
 - prepare before fake launch, each commits, child closes, real placeholder flag, join once with bounded output/branch refs;
 - clean removal preserves shared branches/commits; dirty/untracked/locked/mismatch attention retained, explicit cwd/borrowed untouched;
 - all policies, pending/sequential/start D, child manual cancel, parent cancel/terminate/end, bound reset/ancestor invalidation;
 - cancel vs prepare/start, old generation, crash after effect, stop-timeout/late absence;
 - P6 injected DOMAIN_COMMAND_TIMEOUT on child worker start gives visible child/placeholder failure and no orphan launch receipt; late agent/runtime remains receipt-owned and settles cleanup/attention, never untracked/relaunched;
 - bad/missing/new-tightened policy, accepted child replay after changed policy; no new parent-default replay promise;
 - remote invalid->correct same request, pending replay, accepted conflict, full-frame size, late-attempt CAS, mixed-version refusal, scoped preview.
 CHECK:
 uv run --offline --no-sync pytest -q tests/integration/pac/expansion/test_e2e.py tests/integration/pac/expansion/test_failures.py tests/integration/pac/expansion/test_remote.py tests/integration/pac/expansion/test_effect_races.py tests/daemon/pac/graphs/test_pac_graph_authority.py tests/daemon/pac/events/test_pac_scan_actor_boundary.py tests/daemon/pac/workflows/delivery/test_pac_remote_workflow.py tests/daemon/pac/workflows/delivery/test_pac_workflow_delivery_failure.py tests/daemon/pac/contracts/test_pac_request_inbox_ttl.py tests/daemon/pac/lifecycle/test_pac_actor_lifecycle.py tests/daemon/pac/lifecycle/test_pac_worker_terminal_cleanup.py tests/daemon/pac/workflows/runtime/test_pac_workflow_upgrade_safety.py tests/tooling/isolation/test_teardown_guarantee.py tests/tooling/contracts/test_contract_impact.py tests/daemon/pac/contracts/test_pac_envelope.py tests/daemon/pac/dispatch/test_pac_subscription.py tests/daemon/ipc/cost/test_ipc_cost_counters.py::test_method_key_set_covers_every_dispatched_method tests/daemon/startup/test_startup_event_reconciliation.py tests/identity/ownership/migration/test_pac_graph.py tests/shell/login/test_login_switch_preview.py
 Then four slice commands+COMMON GUARDS+ruff, never full suite. Record SHA/command/assertions/PASS/FAIL/BLOCKED; skip not acceptance. Real sandbox/native provider/shared-object mount remains UNKNOWN until separately authorized real acceptance. Fake-runtime/local-git != real-provider/release evidence.

I6 Integrator docs/contracts/help/skill:
 contract/wire/pac-expansion.md (prerequisite/final conformance)
 contract/pac/remote/README.md (I4 admit features, custody, preview and refusal)
 contract/pac/events/schema.json (strict additionalProperties:false; event/snapshot kind stays task, not expansion; enumerate any actual additive fields, no blanket relaxation)
 contract/e2e/15-pac-workflow.md
 contract/e2e/15-pac-workflow.yaml
 contract/mcp-tools/run.py (optional expansion schema/omission assertions)
 docs/design/pac-expand/design-pac-expand.md (correct implementation references/example, retain approved decisions)
 docs/design/design-pac-workflow.md
 docs/design/spec-pac-workflow-unification.md
 docs/reference/workflow-node-observation.md
 docs/reference/proto.md
 .agents/skills/hyprial-workflow/SKILL.md
 No root new md/WORM/host-skill edits/push/PR. Contract examples are requirements, not claimed executed live scenarios.
 Document policy/defaults/tightening, envelope, rejection vs accepted replay, fixed D, hold!=replan, explicit cwd isolation caveat, cleanup debt/dirty retention, manual same-repo branch integration/no push. Until predecessor output lands, review briefs explicitly name artifact PATH; afterwards bounded output and remaining inspect ACLs. Restore old I8 guidance review: check repository dispatch/template guidance for false running-graph mutation or automatic PR claims, including .agents/skills/hyprial-workflow/SKILL.md and src/hyprial/biz/impl/routine/templates/selfdrive.yaml; edit only if misleading. This is integrator guidance review, not automatic PR functionality or host-skill editing.
 Help via isolated CliRunner: workflow --help and plan/complete/status/list/inspect/cancel/worker --help; MCP schema. Generated root inventory no invented command. Every new option help=; one JSON object with ok/exit status/stderr discipline.
 Final literal Skill-Impact: updated; Contract-Impact: additive workflow expansion request/event/status and remote custody contracts updated in contract/wire/pac-expansion.md. No waiver for real published semantic changes. Save/integration owner persists this node output and implementation evidence; this planning node creates no files.

5. RISKS / STOP CONDITIONS

R1 File ownership, not function ownership. Faces/errors/budgets/types/inventories common-base/integrator only. No cross-worker worktree/authority/application/lifecycle/remote edits. Stop for seam mismatch, unlisted file, >7 directory files, >1000 lines, unclassified/vacuous guard or upward/private cross-domain import; no exemptions.
R2 One v17->18 owner, #987 ancestry confirmed, actual v17 fixture/fresh/older/reopen/rollback. Latest-schema synthetic downgrade must remove future additions. Preserve receipt triggers/FKs/NULL legacy exception/terminal-alert semantics. Base/schema drift needs renewed reservation, not silent post-dispatch renumber.
R3 Deadline/request/operation/author digest/correlation/activation generation/expansion generation/remote attempt distinct. Invalid consumes no request; accepted replay independent of new time/policy/model. Preflight no second child/reservation/unverified artifact publication. Timeout means uncertainty, not rollback. D caps pending/start, not only declared durations.
R4 Launch authorization != OS sandbox. Trusted config/repo permissions host responsibility; borrowed/explicit shared cwd cannot promise isolation. Real same-repo sandbox object visibility requires separate acceptance, not local-git assertion.
R5 No force/rm-rf/branch delete/name-only adoption. Preserve dirty/untracked/locked/replaced/attention. Stop timeout not absence proof. Late starts owned/cleaned; cleanup failure does not rewrite successful business result. Bounded scans/effects, no hot loops.
R6 Offline uv preprovisioned dependencies; exact focused pytest/gates only, no full suite/downloads/provider probes/production config/home/credentials/Lark changes. Missing prerequisite BLOCKED exact error. This is static planning, not behavior PASS or discharge of ROADMAP real-release obligations.
R7 v1 only: no live structure mutation/nesting/gen2 reset/automatic merge-push-PR/borrowed relocation-cleanup/broad inspect permission. If an example/test needs those, correct or escalate, never quietly expand scope.


Review resolution map:
1 — R26, S2 ownership/CHECK: both schema17 tests and v7/v14 fixture teardown.
2 — Prerequisite/F1/F4: additive fields/system literal, types.py, S2 flag helper.
3 — C04, S2 CHECK, I5/I6: remote/event contracts and both consumers; physical task.
4 — I2/I5: IPC stats registration/guard and startup-event reconciliation.
5 — COMMON GUARDS: cadence boundary.
6 — S4: anchored named-group target parser, no URI whitelist expansion.
7 — Parallel test discipline/COMMON GUARDS: neutral paths and merge-residual gate.
8 — F5/I5/integration files: per-column archival decisions, vocabulary.py and migration tests.
9 — I6: old I8 dispatch/template guidance review restored.
10 — Baseline/prerequisite: fork actual 6005943d HEAD, then publish BASE_CONTRACTS.
Scope — R25 separate first commit; R27 dropped; features retained, deadlineProvisional dropped; reuse git budget unless a distinct-bound test requires amendment.

