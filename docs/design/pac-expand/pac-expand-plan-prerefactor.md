# PAC expand: code-verified plan (astra design node, wf-b590cd23, 2026-09-30)

PAC EXPAND v1 — REVIEW / PARALLEL IMPLEMENTATION PLAN
Baseline: feat/pac-expand, a01c6da1. Read-only static review: no repository files edited, no tests executed. Line references are baseline lines. Approved decisions retained: org-default ceilings, optional per-node git worktrees in v1, parent on_failure for a failed expansion. Resolutions below explicitly fill gaps; they are not claims of existing functionality.

1. CORRECTIONS

C01 TRANSACTION COMPILATION. Design :16 says compile_workflow is reusable “as is”; :65-70/:97 requires it inside the flag transaction. workflow_graph.py:95-155,302-308 opens/commits/rolls back its own transaction; store.py:314-318 always issues BEGIN IMMEDIATE; reactor.py:493 already holds one. Nested invocation fails; another connection is not atomic and contends with the writer. Extract compile_workflow_in_transaction, with no BEGIN/commit/rollback, and retain the existing wrapper for ordinary callers. One transaction must publish parent flag, child graph/structure/activation/spec references/roster/receipts, binding/deadlines/worktree intents, completion receipt and acceptance alert. Immutable files must be finished/digest-verified before references publish; filesystem writes cannot be rolled back, so orphan unreferenced artifacts are possible. No git/network effect in that transaction.

C02 PRODUCTION AUTHORITY/ADMISSION. Design implementation outline omits daemon/application.py:8174-8242 and pac/authority.py:51-60,1099-1146,1333-1341 (typed SetFlag -> reactor); cadence runs through authority.py:901-906 too. Carry expansion through all layers, never introduce a second writer in IPC or a lifecycle thread. application.py:10431-10470 _workflow_admit checks role, launch and remote-owner admission; direct child compile must not bypass it. Perform external admission/base resolution before the transaction on bounded effect/preflight lanes, then recheck authoritative request, binding, time, policy and prepared facts under lock. Dry run has no artifacts, git add, notification, launch or DB write.

C03 YAML AND RELATIONSHIP. workflow_schema.py:210-239 requires version/name; :282-313 rejects expands/limits/expansion; :328 requires task. Design :40-42 is missing integrate.task. “Normal format-2 containing only nodes/edges” (:48) is contradictory. Freeze a special envelope {version:2,nodes:[...],edges?:[...]} and generate the child name. Reject user name/defaults/workers/on_failure/escalate_to in it. Require exactly one role:plan source per placeholder, a matching planner -> placeholder forward edge, no orphan/multiple targets, no worker/launch/owner on placeholder. Recommended v1 clarification: placeholder has only that incoming edge, avoiding immediate child activation while other prerequisites are unready. Child: task/report only, no expands, limits, expansion/approval/end, shared workers or back edges. Current back-edge semantics explicitly require end (schema.py:396-415), so “DAG” cannot silently include rework edges. Ordinary workflows remain compatible.

C04 SYSTEM OWNERSHIP/STORAGE. principal.py:1-25,71-126 accepts only canonical user:/agent:, not system:pac. nodes.kind only task/clock/actor/end (store.py:72-86; migrations.py:97-115). Use physical task plus workflow_nodes.node_kind='expansion'; public projection reports expansion. Owner is a reserved canonical local agent URI such as canonical_agent_uri(local_owner,machine,'pac-expansion'), never a real launched actor. Roster ownership='system'; no _actor node/receipt. Public complete/fail/reset must explicitly refuse it even if caller equals that URI; internal completion is separate. _workflow_admit skips system placeholders instead of resolving them as borrowed agents.

C05 PLACEHOLDER STATE MACHINE. _request (workflow_runtime.py:580-660) emits turn/inbox/delivery records; _project (:717-891) would invoke it for an actorless placeholder. Branch before generic dispatch/failure logic. Unbound placeholder stays pending. Accepted binding atomically makes it requested with internal request id, input_token, generation=1, fixed D, NO inbox turn. While child running it remains requested. Child completed -> genuine flag plus done; failed/cancelled child -> _failure, not success; parent closure -> cancelled placeholder/cascade. Repeated projection is idempotent. Planner done without required binding is an invariant error, not ordinary dispatch. Internal requests must not be delivered or exposed as human-completable.

C06 FLAGS/TOKENS. workflow_runtime.py:752-787 verifies flagged-node input tokens; workflow_graph.py:333-381 hashes predecessor flag-event sequences. Merely updating workflow_nodes to done cannot release integrate. System completion must write flag_events, nodes and journal like reactor.py:555-604 within the SAME caller transaction, with current captured token validation; public set_flag from _project would nest BEGIN. Managed forward joins are requested by projection, not _plan_set (reactor.py:250-264). If ancestor reset invalidates a running expansion, cancel child and fail placeholder rather than re-request/create another child.

C07 DEADLINES. workflow_runtime.py:594-621 starts relative timeout at first request and does not refresh it; workflow_graph.py:413-424 rejects only at > deadline. Define D=min(existing placeholder absolute deadline if present, acceptance_at+placeholder.timeout_ms). Persist D on binding/child. Checking child timeout <= D-acceptance_at alone is insufficient: later sequential requests can exceed it. Child fixed request deadline = min(D, first_request_at+timeout_ms, authored absolute deadline if present). Enforce D for pending nodes and startup too; keep existing at==D acceptance, at>D expiration. At expiry cancel child and fail placeholder pac:deadline-expired. No recomputation on retry/restart/hold. Clocks alert; projector must do actual close/failure. Accepted completion replay still succeeds after deadline.

C08 FAILURE POLICY. Design :69 child on_failure=terminate is compatible with :73/:106 PARENT policy, but must be distinguished. Keep child fail-fast so it becomes terminal; then _failure (workflow_runtime.py:662-715) applies parent's terminate/hold/continue. Do not inherit hold into child then wait forever for close. Parent hold is held/open, not in-place re-plan/resume. Already-requested work can still expire. Continue blocks failed placeholder descendants and runs independent branches (:840-888); it must not dispatch integrate as success. Child cancellation maps to placeholder failure unless parent closure already controls it. Error suffix needs deterministic failed node OR graph-level reason when cancellation has no failed node. Replanning a held failed expansion needs a new workflow in v1.

C09 ROSTER ASSERTION IS OVERSTATED. workflow_roster.py:27-81 hashes graphId and ordered business-node projection including ownership, not the generated actor/clock list. store.py:543-618 checks canonical roster/digest, actor-node/receipt-set equality, assigned owner, launch and roster digests; lifecycle.py:706-729 delegates there. Adding an arbitrary borrowed business node does not itself rebuild/check the full roster against every business node as the design implies. Preserve intended immutability nonetheless: system placeholder goes in ORIGINAL parent roster; child gets independent graphId/spec/roster/receipts. Never amend parent roster on expansion. Extend integrity validation for system entries; do not fabricate worker receipts. Historical both-NULL roster exception stays historical, never a fallback for new children.

C10 WORKTREE CWD MUST BE PINNED BEFORE COMPILE. workflow_graph.py:135-153,185-246 pins launch artifacts/receipts; lifecycle.py:731-757 caches resolved launch; migrations.py:859-880 makes launch/roster receipt identity immutable. Rewriting cwd at reconcile breaks digest identity or makes actual launch disagree with the receipt. Allocate deterministic child id/path first; put effective cwd in canonical child spec AND launch artifact; reconciliation creates that already-declared path. Keep an authored-expansion digest separately from derived spec digest. Pin base to commit OID, not a branch resolved later at launch.

C11 COMPLETION RETRY IS NOT GRAPH REPLAY. workflow_graph.py:63-92 compares creator/spec/routine/task for workflow:<operation_key>. Reactor checks closed/state/flag before storing receipts (reactor.py:493-550,646-676): local repeated completion currently yields stale/already-set/closed. Remote _receipt (dispatch/remote_workflow.py:674-706,946-983) replays actor/action/reason/output only; it would ignore expansion. Under the writer lock, BEFORE closed/time/current-request checks, compare accepted receipt's graphId,nodeId,requestId,verified actor,action,reasonRef,outputText,canonical AUTHOR expansion digest. Exact replay returns original event/child even after close/restart; no flag, child or alert duplicated. Changed payload -> WORKFLOW_OUTCOME_CONFLICT. Unaccepted stale/deadline -> existing errors. Invalid expansion writes no receipt and leaves requested. Never re-evaluate accepted identity using new time, generated UUID, current model matrix or policy.

C12 DURABLE BINDING/GENERATION. Two parent columns in design :67-68 are not enough. Persist UNIQUE(parent_graph_id,parent_node_id), child id UNIQUE, planner node/request, generation=1, author digest, captured effective limits, acceptance time and D. Child parent columns need paired-null consistency/composite parent-node FK/index. workflow_nodes.generation increments on requests and is not an expansion retry counter. Reject planner/placeholder reset after binding with PAC_EXPANSION_ALREADY_BOUND; no generation 2. Other ancestor invalidation follows C06. The binding and completion receipt are written with child and parent flag.

C13 ORG DEFAULTS HAVE NO EXISTING CONFIGURATION. workflow_schema.py:24 is generic MAX_NODES=100; :67-115 ordinary launch validation; dispatch/matrix.py:54-81 model choices; persistent_config.py:203-228 loads channels/users. None is an org expansion policy/work root. Add trusted daemon policy, not hard-coded HQ paths or defaults supplied by planner YAML. Proposed <HYPRIAL_HOME>/pac-expansion.json with version/workRoot/optional limits; missing limits defaults to 12,fast+strong,no owners,[workRoot]. Missing/invalid workRoot fails closed for expansion-enabled runs only. Graph overrides must tighten; empty list means none, not unlimited; wider parent limits -> WORKFLOW_SCHEMA_ERROR. Snapshot effective limits in immutable parent spec/binding. Later widening cannot widen that parent; later tightening also constrains NEW admissions, not mutation of running children. This is a node-local trusted copy of org policy, not an existing distributed org-policy service.

C14 VALIDATION DETAIL. Validate every effective launch/default; explicit unknown model rejected; actual resolved tier must be allowed. tier_for_model returns HIGHEST tier (matrix.py:60-81): explicit astra/fable are super despite appearing in strong; document rejection under fast+strong. Check resolved models for tier launches too. Enforce canonical Path.resolve containment, not startswith; reject '..'/symlink escape, omitted cwd except an authorized managed default, relative paths, model/cwd override args. Existing schema.py:67-115 filters only some model args and is not a sandbox; recommended v1 expansions reject all nonempty native args. Count business nodes, not generated actors/clocks. Owner allowlists are exact canonical URIs; borrowed ownership cannot change cwd/model or authorize cleanup. Empty org owners means remote borrowed nodes unavailable until admin grants them. Bound YAML bytes/depth/aliases and use duplicate-key protection.

C15 PER-NODE VS PER-ACTOR WORKTREES. lifecycle.py:582 reconciles actor nodes; schema.py:269-327,418-426 allows shared workers; compiler creates one actor per worker. A worktree-enabled workflow-owned role:execute node needs its own worker/path. Restricted child envelope can disallow shared workers entirely. Borrowed/local/remote actors are not launched/relocated; don't create unused managed worktrees for them. Non-execute roles do not get worktrees implicitly. Design :76 allows explicit cwd override: if retained, prepare/track the promised managed worktree but preserve authorized explicit effectiveCwd; report managedPath/effectiveCwd and warn that isolation is bypassed. Do not claim explicit shared cwd is isolated. repo/managed paths must satisfy tightened cwdUnder too.

C16 EFFECT LANE, NOT BLOCKING RECONCILE. daemon/pac_actor.py:589-645 delegates production reconciliation to authority; authority.py:344-412,724-782,1433-1471 runs observe/resolve/start/stop outside graph writer with generation fences. Direct git inside ActorCoordinator blocks that writer. Add durable prepare/remove intents and bounded effects; verify existing repo/path/branch ownership, no unrelated adoption. Handle crash after git succeeds before receipt; prep failure must fail child before launch. Use argv/no shell; no fetch/install/push/PR. Base resolution also belongs outside the SQL lock.

C17 CASCADE AND CLEANUP. close_workflow (runtime.py:106-193) closes only one graph and creates intents from its receipts. Extend transactionally to open bound children, preserving already-terminal child states, each with its own receipt-owned cleanup intents. No runtime stops in close. lifecycle.py:590-694,892-1014 deliberately records identity/entity mismatch/stop-timeout attention; never bypass with destructive cleanup. daemon/pac_actor.py:424-438 stops scheduling a closed actor after cleanup complete, so worktree cleanup needs a separate pending-worktree scan even after worker cleanup settles. Remove only after proven worker absence/complete; retain on attention, dirty/untracked/locked/mismatched paths or failure. git worktree remove without force, no rm -rf or branch -D. Preserve branches/commits for integration. Business result and cleanup debt remain separate.

C18 REMOTE “UNCHANGED” IS LIMITED TO ORDINARY CHILD TASKS. remote_workflow.py:451-503,986-1016 drops expansion; authority.py:972-1000 and migrations.py:593-603,646-650 lack expansion custody. Remote planner requires payload/digest through outbox/wire/origin/receipt. MAX_FRAME=256KiB (:35,113,140), smaller than ordinary 1MiB YAML; bound expansion and actual encoded frame, never truncate/raise global limit casually. Rejected remote result is cached, and changed payload conflicts: to support invalid-then-correct on same requested node, permit replacement ONLY after definitive PAC_EXPANSION_INVALID with no accepted receipt and request still current. Use durable attempt number/CAS so an old reply cannot overwrite a corrected attempt. Pending/accepted outcomes immutable. Child graphs stay on origin. Mixed-version inability must fail explicitly, not silently drop expansion.

C19 OUTPUT DOES NOT REACH JOIN TODAY. WorkflowSender at runtime.py:255-280 constructs input evidence from NodeRow.to_json(); store.py:133-170 has flagReasonRef but not workflow_nodes.output_text. Placeholder output alone therefore does not put slice results in integrate brief. Add bounded predecessor output (or explicit inspect references/capability), and planner brief must expose expands target, effective limits and required completion field. Define summary PLUS hash, not an opaque “digest” masquerading as prose. Sort child nodes; include nodeId/state/reasonRef/output head/branch/path; hash full canonical result document; bound summary to 64KiB UTF-8 bytes (workflow_output.py:8-25). Full results remain inspectable in child. Only report branches from durable worktree facts, not inferred worker prose.

C20 SURFACE MODEL. runtime.py:1121-1293 has no relation fields; physical task kind needs public expansion override. overview.py:69-127 must retain new data; :144-245 builds graph-level name links, not per-node links. Use separate nodeLinks and update overview_page.html. Explicit mission grouping already exists (:247-257), so “only inferred from names” is incomplete. Child follows parent's task line/mission, not name heuristic; tolerate filtered/missing parent. Preserve unrelated legacy heuristics. Child nodes are display facts, never insert them into parent DAG/roster. workflow/cli.py:165-170 status is generic IPC output; :338-381 list has custom formatter; human rendering and additive JSON need separate tests.

C21 JOURNAL/ALERTS/ERROR DATA. journal.py:20-35 has closed event types and DB CHECK; :102-121 append requires active transaction. Use workflow_changed {expansionAccepted,...}, normal flag_set/graph_closed and durable actor_alert; a new event type otherwise requires journal migration/consumers. One acceptance means one notification identity, not exactly-once network delivery. Ordinary completion-output/child-failure alerts are distinct. Retry cannot insert acceptance again. Preserve PacError.data through service/application/remote so offending field is machine-readable (application.py:8240-8242 currently drops it).

C22 OUTLINE/DOC ACCURACY. Include authority.py, application.py, dispatch/remote_workflow.py, dispatch/workflow_observation.py, overview_page.html and trusted policy wiring in integration. Correct design :16 v0.1 wording, :109-112 former questions, example missing task and unauthorized paths. No schema-only patch or two-column migration suffices. No activated-graph structural mutation, nested expansion, automatic push/PR or generation-2 replanning is introduced.

2. PLAN — FOUR FILE-DISJOINT PARALLEL SLICES

Freeze the following proposed interfaces before dispatch:

F1 CONFIG/YAML:
Trusted <HYPRIAL_HOME>/pac-expansion.json:
 {"version":1,"workRoot":"/absolute/root","limits":{"maxNodes":12,"tiers":["fast","strong"],"owners":[],"cwdUnder":["/absolute/root"]}}
Only version/workRoot required; absent individual limits get shown defaults. maxNodes strict int 1..100, unique lists; cwdUnder contained in workRoot. Graph limits same four fields plus optional worktree:{repo:<absolute>,base:<git-ref>}; graph cannot change workRoot. No policy fallback to process cwd/HQ path. Managed path = <workRoot>/.pac-worktrees/<childGraphId>/<nodeId>, and must fit any narrower cwdUnder too. Org admins may alter their own ceilings, graphs may only tighten.
Parent additions: planner task/role:plan/expands:<placeholder>; placeholder {id,kind:expansion,after:[planner],timeout,limits?}, optional task generated if absent. No placeholder owner/worker/launch/expands; exactly its planner incoming edge; no back edges touching planner/placeholder. Wrong-kind expands/limits invalid.
Child root exactly {version:2,nodes:[...],edges?:[{from,to,kind?:forward}]}. Child ordinary task/report supports id,task,role,owner OR launch,after,timeout,deadline_ms,first_output_eta,human_gates. No shared worker, defaults, workers, name, summary, on_failure, escalate_to, expands, limits or nested kinds. Owned launch defaults fast; cwd explicit except generated execute worktree default; no nonempty args. Borrowed nodes have no launch. Internal child name generated/bounded, creator/escalation inherited, internal on_failure=terminate.

F2 ERRORS (PAC-domain definitions in pac/errors.py, consistent with that file's documented boundary):
 WORKFLOW_SCHEMA_ERROR: bad parent syntax or widened authorization.
 PAC_EXPANSION_INVALID: bad/missing/unexpected expansion, data {field:<JSON-style path>,reason:<stable tag>}; tags syntax,unknown-field,required,unexpected,kind,nested,cycle,count,tier,owner,cwd,timeout,args,worker-sharing,transport-size.
 PAC_EXPANSION_POLICY_UNAVAILABLE: trusted config unavailable/invalid.
 PAC_EXPANSION_ALREADY_BOUND: post-binding planner/placeholder reset or new generation.
 PAC_EXPANSION_SYSTEM_NODE: external placeholder complete/fail/reset.
 WORKFLOW_OUTCOME_CONFLICT: different accepted outcome payload.
 WORKFLOW_REQUEST_STALE / WORKFLOW_DEADLINE_EXPIRED: existing unaccepted request fences.
 PAC_WORKTREE_PREPARE_FAILED / PAC_WORKTREE_IDENTITY_MISMATCH / PAC_WORKTREE_CLEANUP_FAILED: bounded {graphId,nodeId,path,stage}, no environment/secrets in errors.

F3 PUBLIC CONTRACT:
 workflow.complete existing params + optional expansion:<YAML/JSON string>, <=65536 UTF-8 bytes, NEVER origin-side filename. CLI --expansion reads locally; MCP expansion:str|None. No expansion on fail.
 Read-only workflow.expansion.plan {graphId,nodeId,yaml,actor,sessionRef?}; nodeId placeholder. Authenticate creator or expanding planner. Return {ok:true,graphId,nodeId,expansionDigest,deadlineMs,effectiveLimits,plan:<normalized preview>,warnings:[]}. Before acceptance D is prospective and labelled provisional; no lease/reservation. Origin owns validation.
 Read-only workflow.plan {yaml,actor,sessionRef?} for policy-aware parent preview. Existing ordinary offline plan stays; parent containing expansion uses authoritative daemon preview. run revalidates.
 Completion success {ok:true,event:{...existing fields...,childGraphId,expansionDigest}}. Durable receipt preserves original child/event; ordinary nodes unchanged. Remote returnState pending/accepted/rejected unchanged.
 Child status/list adds parentGraphId,parentNodeId. Placeholder adds kind:'expansion',systemOwned:true,childGraphId,childState,expansionDigest,deadlineMs,childNodes:[{nodeId,state,reasonRef}],worktreeCleanup?. States remain pending/requested/done/failed/blocked/cancelled. One-level bounded inline summary from pinned snapshot.
 Overview nodeLinks distinct from graph links: {from:{graphId,nodeId},to:{graphId,nodeId},kind:'expand'}, planner to each child node, child sinks to placeholder successors.

F4 INTERNAL SIGNATURES (new keyword-only additions, backward-compatible ordinary behavior):
 expansion_policy.load_expansion_policy(home:Path)->dict
 expansion_policy.effective_limits(overrides:dict|None,*,policy:dict)->dict
 workflow_expansion.expansion_digest(raw:str)->str
 workflow_expansion.validate_parent(spec:WorkflowSpec,*,policy:dict)->WorkflowSpec
 workflow_expansion.validate_expansion(raw:str,*,limits:dict,child_graph_id:str,deadline_ms:int,at:int,name:str,escalate_to:str|None)->WorkflowSpec
Digest hashes canonical parsed AUTHOR input, not current model resolution/derived cwd/time. Duplicate-key/non-JSON/deep/aliased payloads bounded/rejected. Pure validation: no git/network/DB writes. Parent validator snapshots effective policy; absent additions preserve ordinary canonical bytes.
 workflow_graph.compile_workflow_in_transaction(store:PacGraphStore,spec:WorkflowSpec,*,sender:str,machine:str,local_owner:str,operation_key:str,at:int,graph_id:str|None=None,parent_graph_id:str|None=None,parent_node_id:str|None=None,expansion_deadline_ms:int|None=None,routine_name:str|None=None,task_key:str|None=None)->str
Requires active transaction, no commit/rollback. Existing compile_workflow wrapper signature unchanged. Child id='wf-'+uuid5(NAMESPACE_URL,'pac-expand:<parent>:<placeholder>:1').hex; operation key passed expand:<parent>:<placeholder>:1, compiler retains workflow: storage prefix.
 PacReactor.set_flag(...existing...,expansion:str|None=None)->FlagEventOutcome
Trusted injected preparation/validation callback supplies machine/local identity, policy and pre-resolved base; never client params. Reactor independently enforces request/binding/receipt fences. Isolated engine tests inject callback; integrator attaches real validator/admission.
 Worktree plan value = {graphId,nodeId,actorNode,repo,baseRef,baseOid,branch,path,effectiveCwd,operationId}; branch='pac/<childGraphId>/<nodeId>'; operationId='pac-worktree:<childGraphId>:<nodeId>'; paths canonical; OID pinned before publication.

Parallel rule: no worker edits shared integration seams (authority/application/lifecycle/daemon), common conftest, deps or lockfiles. New cross-slice imports can wait for integration: B uses injected validator/value fixtures, C pure git effects, D mocked IPC. Do not copy another worker's implementation or touch their worktree. Only listed files may be edited; existing focused tests may be read/run by any slice.

S1 POLICY — Goal: grammar, trusted ceilings and pure validators.
EXACT OWNED FILES:
 src/hyprial/pac/workflow_schema.py
 src/hyprial/pac/errors.py
 src/hyprial/pac/expansion_policy.py (new)
 src/hyprial/pac/workflow_expansion.py (new)
 tests/test_pac_expansion_schema.py (new)
 tests/test_pac_expansion_policy.py (new)
Implement F1/F2/F4, additive WorkflowNode expands/limits defaulted fields and system ownership serialization; no DB/daemon effects. Preserve ordinary canonical shape when fields absent.
TEST CASES: current YAML unchanged; corrected example; all wrong/missing/duplicate fields; unique planner binding/orphan/multiple sources; placeholder ownership forbidden; child forbidden roots/kinds/back edges/nesting; each ceiling plus combinations; bool/count/zero; exact owners; absent vs empty lists; strict tightening; missing config only affects expansion; symlink/'..'/sibling-prefix cwd; default tier and explicit astra highest-tier/unknown model; args bypass; timeout/D boundary; source digest formatting-stable/content-sensitive; pathological YAML limits.
ISOLATED CHECK:
 $PY -m pytest -q tests/test_pac_expansion_schema.py tests/test_pac_expansion_policy.py tests/test_pac_workflow_schema.py

S2 ENGINE — Goal: atomic binding, retry receipts, placeholder projection, cascade/query facts.
EXACT OWNED FILES:
 src/hyprial/pac/migrations.py
 src/hyprial/pac/store.py
 src/hyprial/pac/workflow_graph.py
 src/hyprial/pac/workflow_runtime.py
 src/hyprial/pac/reactor.py
 src/hyprial/pac/workflow_roster.py
 src/hyprial/pac/expansion_runtime.py (new helper/value seam)
 tests/test_pac_expansion_transactions.py (new)
 tests/test_pac_expansion_projection.py (new)
 tests/test_pac_expansion_migrations.py (new)
Implement caller-owned compiler, one binding, exact replay, system flag helper, D cap, all parent policies, child cancellation, additive status/list, bounded predecessor output delivery and durable git plans. Keep graph.py freeze and journal event types unchanged. Refuse expansion without trusted injected context, never bypass policy. No production authority/lifecycle/git effects here.
EXCLUSIVE MIGRATION OWNER: reserve v16->v17, renumber if dev advances. Frozen schema additions:
 workflow_graphs parent_graph_id TEXT NULL,parent_node_id TEXT NULL,expansion_deadline_ms INTEGER NULL; paired-null consistency/composite parent-node FK/index.
 workflow_nodes node_kind TEXT NOT NULL DEFAULT 'task'; recognize expansion, preserve legacy end projection.
 workflow_expansions parent_graph_id,parent_node_id PK; child_graph_id UNIQUE; planner_node_id,planner_request_id,generation CHECK=1,expansion_digest,limits_json,accepted_at,deadline_ms; graph/node FKs, immutable identity/policy/time fields.
 workflow_outcome_receipts graph_id,node_id,expansion_digest nullable legacy columns; new rows populate identity; original child/event in result_json. Do not invent legacy expansion metadata.
 remote_workflow_outbox expansion_text TEXT NULL,expansion_digest TEXT NULL,attempt_no INTEGER NOT NULL DEFAULT 0; integrator wires CAS.
 workflow_worktrees graph_id,node_id PK,actor_node,plan_json,state CHECK in planned/prepared/cleanup_pending/removed/retained/attention,reason TEXT NULL,updated_at INTEGER. Immutable plan shape F4, unique managed path/operation identity, separate from worker cleanup states. No borrowed-worker plans.
Use explicit column lists; do not alter legacy store.SCHEMA snapshot as a parallel migration path.
TEST CASES: fault injection through publication proves all-or-nothing flag/child/receipt/alert; wrapper compatibility; same completion twice/concurrently/after close/reopen -> same event/child; payload/identity conflict; invalid expansion unchanged requested/no receipt; stale/deadline no child; no placeholder delivery/actor receipt; actual flag releases join once; terminate/hold/continue; manual child cancel; >D cap for sequential pending work; parent cancel/terminate/end cascades and never-launched cleanup intents; input invalidation no generation2; bound reset forbidden/system spoof rejected; parent spec/roster immutable; child digest integrity; sorted bounded output reaches join; fresh/v16 upgrade/idempotent migration/FKs/rollback/legacy preservation.
ISOLATED CHECK:
 $PY -m pytest -q tests/test_pac_expansion_transactions.py tests/test_pac_expansion_projection.py tests/test_pac_expansion_migrations.py tests/test_pac_workflow_runtime.py tests/test_pac_workflow_roster.py tests/test_pac_worker_terminal_cleanup.py

S3 GIT EFFECTS — Goal: idempotent prepare and non-destructive removal without PAC/daemon writer dependencies.
EXACT OWNED FILES:
 src/hyprial/pac/workflow_worktrees.py (new)
 tests/test_pac_expansion_worktrees.py (new)
FROZEN FUNCTIONS:
 resolve_base(repo:Path,base_ref:str)->str
 prepare_worktree(plan:dict,*,timeout_s:float=30.0)->dict
 cleanup_worktree(plan:dict,*,timeout_s:float=30.0)->dict
Result {state:'prepared'|'removed'|'retained'|'attention',path,branch,baseOid,reason:<str|null>}. Preparation exceptions use F2 errors. resolve_base verifies commit with option-safe argv; no write. prepare verifies registration/common-dir/branch/path ownership; first creation uses pinned OID. Retry MUST permit worker commits on owned branch (do not require HEAD==base after work). No unrelated path adoption. Cleanup only called after integrator proves worker absence; never force/delete branch/remove explicitCwd. A replaced directory is not authorized by an old receipt.
TEST CASES in local temp git repo, no remotes: distinct node paths/branches/shared objects; moving base ref still pinned OID; retry after git success/before receipt; replay after worker commit; wrong branch/repo/path/symlink/noncommit; argv/no shell/timeouts; partial add; clean removal preserves branch/commit; dirty/untracked/locked retained; missing path idempotent; explicit cwd untouched; no fetch/push/PR commands.
ISOLATED CHECK:
 $PY -m pytest -q tests/test_pac_expansion_worktrees.py

S4 SURFACES — Goal: CLI/MCP/overview against frozen JSON.
EXACT OWNED FILES:
 src/hyprial/workflow/cli.py
 src/hyprial/mcp/server.py
 src/hyprial/pac/overview.py
 src/hyprial/pac/overview_page.html
 tests/test_cli_workflow_expansion.py (new)
 tests/test_mcp_workflow_expansion.py (new)
 tests/test_pac_expansion_overview.py (new)
Implement --expansion bounded local UTF-8 read; MCP optional expansion string; plan --expansion-for graph:placeholder with special envelope, not ordinary name-required parser; policy-aware parent preview routing. Preserve actor/session identity and old complete/fail behavior. Human status child states/list parent marker; additive JSON only. Explicit parent task-line/mission and nodeLinks; missing parent fallback, preserve unrelated heuristics/redaction. Child display never changes parent roster.
TEST CASES: exact IPC omission/presence fields; file read/encoding/size errors; invalid expansion-for syntax; option help; plan routing; MCP tools flag/session binding; no expansion on fail; replay response shape; human+JSON rendering; misleading names cannot override relation; mission inheritance; filtered parent; duplicate links; redacted/escaped bounded summaries; ordinary behavior unchanged.
ISOLATED CHECK:
 $PY -m pytest -q tests/test_cli_workflow_expansion.py tests/test_mcp_workflow_expansion.py tests/test_pac_expansion_overview.py tests/test_cli_workflow.py tests/test_mcp_worker_workflow_tools.py tests/test_pac_overview.py tests/test_pac_list_overview.py

$PY is the worker's exact PRE-BUILT interpreter provided by supervisor. This planning worktree has no .venv, so do not assume one. No uv sync/pip install/network/full suite. Tests are self-contained or use read-only existing helpers; do not edit common conftest. Checks above are proposed, NOT reported passes.

3. INTEGRATION — SINGLE OWNER AFTER MERGES

I1 Merge/review S1,S2,S3,S4 in dependency order; builds remain parallel. After workers stop editing, integrator owns wiring in src/hyprial/pac/authority.py, src/hyprial/pac/lifecycle.py, src/hyprial/daemon/application.py, src/hyprial/daemon/pac_actor.py, src/hyprial/dispatch/remote_workflow.py, src/hyprial/dispatch/workflow_observation.py, policy startup, and necessary amendments to merged slice files. Confirm current dev migration version; fix every positional remote INSERT affected by added columns.

I2 Load trusted daemon-home policy; snapshot parent effective defaults at preview/start. Extend typed SetFlag and handler/auth/error data forwarding, attach validators/admission. Preserve graph-authority timeout semantics (authority.py:1681-1701: timeout doesn't cancel accepted mutation). Never trust client-supplied machine/policy/base. Preflight remote-owner admission and base resolution on bounded lanes; under lock validate current request/policy/binding and consume prepared facts. Parent operation-key replay uses original accepted snapshot, not new policy. Dry run never reserves id or creates worktree/artifact. Ordinary workflow run remains compatible.

I3 Allocate paths before launch/spec digest pinning. Add prepare/remove effects with durable intent/result writes, generation/activation fences, startup gating, prep failure projection, and separate closed-worktree cleanup scheduling. Cancel during prepare/start must settle into cleanup, not launch/relaunch; late old-generation results cannot mutate new state. Direct test coordinator and actorized production paths match. Never remove before confirmed stop; clean removal preserves branches for manual integration. No automatic merges, pushes, PRs or branch deletion.

I4 Remote custody: local completion -> outbox text/digest/attempt -> bounded wire -> origin validation/authority -> receipt -> return. Reuse pending/accepted identity. Corrected payload allowed only after definitive PAC_EXPANSION_INVALID/no receipt/still-current request; compare-and-set results by attempt_no to fence late replies. Other terminal outcomes immutable. Origin validates remote dry run. Mixed-version inability explicitly refuses expansion; ordinary remote child tasks unchanged. Test wire-size errors and invalid-then-correct same request, not just happy remote completion.

I5 END-TO-END TEST: new tests/test_pac_expansion_e2e.py, isolated temp daemon/application+PacGraphAuthority, fake runtime/sender/remote wire, real temporary git repo, deterministic clock. Parent design -> build -> integrate. Invalid design completion leaves requested and zero flags/child/alert/worktrees. Correct completion creates two parallel execute nodes with distinct pinned cwd/branches. Retry before/after daemon restart/graph close returns same event/child with one durable acceptance alert. Worktrees prepared before fake launch; commit independent edits; complete slices; child closes; placeholder real flag; join requested exactly once WITH summaries/branch refs. Check clean worktree removal preserves commits/branches and dirty worktree retained; parent spec/roster unchanged; child receipts/digests and journal transaction order correct.
Parametrized companions: all parent policies; D cap/sequential work; child cancel; parent cancel during prepare/start; borrowed never stopped; cleanup identity mismatch; ancestor invalidation; bad policy; remote invalid/correct/pending/late-attempt fence. No actual agents/providers/network.
FOCUSED INTEGRATION CHECK:
 $PY -m pytest -q tests/test_pac_expansion_e2e.py tests/test_pac_graph_authority.py tests/test_pac_scan_actor_boundary.py tests/test_pac_remote_workflow.py tests/test_pac_request_inbox_ttl.py tests/test_pac_actor_lifecycle.py tests/test_pac_worker_terminal_cleanup.py
Then each slice's focused command; never full pytest suite.

I6 DOCS owned by integrator: docs/design/pac-expand/design-pac-expand.md (corrected approved design/v1 restrictions); docs/design-pac-workflow.md and docs/spec-pac-workflow-unification.md (state/deadline/receipt contracts); docs/workflow-node-observation.md (additive facts/output); docs/proto.md (IPC/remote payloads/errors/size). Include executable parent+child examples, trusted policy path/schema, defaults only tighten, rejection vs accepted replay, hold != replanning, dirty retention/cleanup attention, branch-based manual integration and no push. Distinguish business completion from cleanup completion and durable alert identity from network redelivery.

I7 CLI HELP INVENTORY: actual Typer tree, not invented top-level snapshot. New leaf options workflow complete --expansion and workflow plan --expansion-for; no new top-level command. Review workflow --help, plan --help, complete --help, status --help, list --help, inspect --help, cancel --help, worker --help and MCP workflow_complete schema/description. cli_inventory.py:31-59 generates root inventory, so no fake root-list edit. Update leaf-help tests and descriptions.
FOCUSED GUARDS:
 $PY -m pytest -q tests/test_cli_help_inventory.py tests/test_ci_cli_help_inventory.py tests/test_no_pac_cli_spellings.py tests/test_ipc_error_code_registry.py
This planning node did not execute help commands: only inspect/complete are permitted here.

I8 SKILL-IMPACT: updated. Integrator edits .agents/skills/hyprial-workflow/SKILL.md for planner completion field, origin dry run, request-bound retry/inspect, tightening defaults, per-node worktrees, dirty retention/cancellation and manual integration. No .agents/skills/h2b-workflow in this checkout; do not edit host-installed skill as project deliverable. Review dispatch/template guidance for false mutation/automatic PR claims. Final integration report includes literal “Skill-Impact: updated” plus focused evidence. No push/PR needed for acceptance.

4. RISKS / STOP CONDITIONS

R1 FILE OWNERSHIP: S1 schema/errors; S2 engine quartet/store/roster/migration; S3 new git module; S4 surfaces; integrator alone authority/lifecycle/application/daemon/remote. Function-level split in same file is NOT isolation. Report seam changes instead of editing another slice. Cross-slice imports are integration work; isolated tests use injected/value seams.

R2 MIGRATIONS: baseline SCHEMA_VERSION=16 (migrations.py:22); one owner and one atomic upgrade. Version number is not branch-local; rebase before merge. Preserve v15/v16 immutable receipt identity, cleanup FKs/triggers, legacy NULL exceptions and old data. Explicit SQL columns, especially remote inserts. Test fresh/upgraded/reopen/rollback. Avoid nodes/journal rebuild by physical task plus existing event types. Never bypass roster validation to make new child compile pass.

R3 AUTHORIZATION IS NOT SANDBOX: allowed owners/models/cwds constrain dispatch, not arbitrary actions of a launched model. Protect trusted config/repo at host level. Explicit cwd override/borrowed environment cannot honestly promise isolated worktrees. Arg rejection/canonical paths/identity fences prevent parameter escapes but do not replace OS isolation. Ensure selected repo and generated root fit narrowed cwdUnder.

R4 DATA LOSS/RACES: no force remove/rm-rf/branch deletion. Cancel vs prepare/start and old-generation completions require fences. Worker attention means preserve worktree. Dirty/untracked/locked/mismatched paths become visible retained/attention; don't rewrite business outcome to hide debt. Pin base OID. Shared-object-store manual integration depends on actual sandbox mounts; verify supported launch setup rather than assume access.

R5 IDENTITY: graph operation key, node request, authority call correlation, binding generation, remote attempt number have different lifetimes. Invalid validation must not consume request; accepted replay ignores elapsed deadlines/config changes but compares exact author identity/payload. Remote size/cached-rejection issues are blockers, not optional polish.

R6 OFFLINE: prebuilt Python per worktree, temp DB/git fixtures, no network/package install/provider probes/live daemon reconfiguration/full suite. Missing interpreter dependency or git support -> report blocked command/error, no downloading repairs. This worktree lacks .venv; supervisor supplies path. Static review is not test evidence.

R7 SCOPE: no mutation of activated parent node/edge set, nested expansion, generation2 reset/replan, automatic push/PR, or borrowed-worker cleanup. If implementation needs these to pass an example, correct the example or escalate instead of silently widening v1.

