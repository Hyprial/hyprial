# Cell model phase A — merged implementation plan (2026-11-03)

- Status: executable plan for `intg/cell-model-phase-a` (#2323). It was produced by 7 per-slice planners, one adversarial review and revision per slice, and a merge pass. The revised slice plans were not re-reviewed one by one. Instead, each slice implementer re-checks its section against the code before writing the first test.
- Design source: `docs/design/cell-thread-conversation-model-2026-11-03.md` on `infra-ops/design-cell-thread-conversation`@`8a4887b5f`.
- Executor decisions (cloud-claude, 2026-11-03) are listed first. HQ only intervenes on items that change design conclusions (openapi change, cross-node incompatibility). None of the decisions below needs a ruling before work starts.

## Executor decisions on the plan's questions

| # | Decision |
| --- | --- |
| Q1 wire carve-out | **No wire change in phase A.** `proto.md` §2.4 forbids type-specific fields on frame ① (`hyprial-delivery-frame/v1`). `threadId`, `conversationId` (cell) and `class` are local `InboxMessage` fields. Each daemon derives them from sound local signals (sender `system:`, `workflow-` id prefix, `reply:` key, sender/recipient pair). This narrows design §9.1 for phase A: wire stamping waits for a §2.4 carve-out (Allen) in phase B. Cross-version risk with 0.5.5.3.3 is therefore nil. It is still measured against an isolated 0.5.5.3.3 node: today's decoder ignores unknown keys (`inbox/links/zenoh_link.py` `decode_delivery_frame`). |
| Q2 thread granularity | One thread per (agent, counterpart endpoint). A legacy `conversationId` selects a conversation cell inside the thread. |
| Q3 conversation cells | One cell per owner, in the owner-home OrgFS space; S3 adds scope `owner`. |
| Q4 PAC worker replies | SYSTEM. One rule, revertible. |
| Q5 OrgFS disconnected | Option B: established links keep last-known-good flow parameters while OrgFS is unreachable. Boot without OrgFS stays out of scope. |
| Q6 wake | The human-wake gate ships in observe mode. Each enforce step is a separate revertible commit and needs Allen's go before it is turned on. Class decides storage only. |
| Q7 TTL/terminal Lark leg | Exempt (in-thread feedback). |
| Q8 DLQ bodies | Kept in phase A. |
| Q9 retention override | A single `delivery.bodyRetentionMs` key from owner-authored department/agent cells, combined by `min` and clamped to 1 h..365 d. Default 7 d (HQ Q3). |
| Q10 param trust | Local owner only in phase A. |
| Q11 physical moves | `links`→`threads`, `inbox`→`delivery` and `link_ledger` are physically moved in S9. Phase A adds the new packages as the authorities and keeps the old modules as internal engines and aliases. |
| Q12 golden baseline | A test-only commit G0 records pre-change responses before any behaviour change. |
| Q13 `sessions.*` deprecation log | Phase B. |
| Q14 S7c PAC mirror | Ships default-off. |
| Q15 GLOSSARY org-model rows | Imported from the design branch with S3. |
| Q16 thread kinds | `agent`, `user`, `adapter`; one adapter thread per adapter endpoint. |
| Q17 operator window mode | An explicit operator setting beats cell data. |
| Q18 SYSTEM rows after retention | Residue (the row stays, the body goes). |


Baseline: `intg/cell-model-phase-a` at 3f45e1cbe (dev 18364eb00 plus the kernel `t.`/`c.` grammar). Paths below omit `src/hyprial/`.

## 1. Cross-slice interfaces and commit order

### 1.1 Fixed interfaces (one owner each; later slices consume them and never redefine them)

| Interface | Owner | Consumers |
|---|---|---|
| `kernel.MessageClass` (INTERACTION / SYSTEM) in `kernel/impl/contracts/envelope.py`, optionally also `DISPATCH_MESSAGE_PREFIX` | S2 | all |
| `InboxMessage.thread_id / conversation_cell_id / message_class`; `thread_key -> str\|None`; `with_envelope()` | S2 | S4, S5, S6 |
| `inbox.envelope.message_class_of()` is the only class accessor. Also `derive_message_class` (sound signals only), `inherit_envelope`. `requested_message_class` (honoured only for `_messageClass` when origin is `'daemon'`) is **not delivered by S2** (see §2.1 "S2 as delivered"); it needs an orchestrator reassignment or drop | S2 | S4, S5, S6 |
| The submission digest never includes envelope fields (pinned by `test_envelope_digest.py`) | S2 | S4 |
| `kernel.subject_cell_id`, `kernel.keyed_cell_id`; `Agent.cell_id` | S3 | S4, S6, S7 |
| `cells.ports.CellRuntimePort` (`index`/`lookup`/`ensure`/`update`/`retire`/`kick`); `composition.cells.cell_runtime(app)`; `Cell.params` (first-class top-level map); new scope `owner` that maps to the owner-home space | S3 | S4, S6, S7 |
| `CellParamSnapshot`, `composition.cells.cell_params(app)`; `ParamSpec` with combine modes `unique`, `min`, `any_false` and the new `collect` | S7a | S4 (`threads.policy`), S5/S6 (`delivery.bodyRetentionMs`) |
| `links.ports.{AgentFlow, FlowPolicyPort, LinkSnapshot}`; `threads.ids` (pure); `app._threads`; `app._thread_id_for(sender, recipient)`; `ThreadAdmin` view | S4 | translation, S6 |
| `delivery/ports.py`: `EnvelopeLike`, `InteractionRow`, `BodyGrant`, `BodyRef`, `BodyResolver`, `DEFAULT_BODY_RETENTION_MS = 604_800_000`. Facade: `grant_body_release`, `bind_body_retention(resolver=)`, `interaction_rows_after/row_at`, `retained_body`. Also `HumanWakeGate` | S5 | S6 |
| `conversations.ConversationBodies`, which implements `BodyResolver`; `ConversationRecorder` | S6 | S5 (via the port) |

### 1.2 Commit order

0. **G0, characterization baseline (test-only).** The golden harness, snapshots and coverage manifest from the translation plan (G1–G4). It is recorded before any behaviour change (see Q12).
1. **S2**, all 12 steps.
2. **S3**, steps 1–16. **S5** can start in parallel after S2 (steps 1–9 need only S2), but merges after S3.
3. **S7a**: params, snapshot and source, refreshed from an S3 index-generation hook.
4. **S4**, steps 0–11, plus the `send.py` thread stamping. Step 12 is included only if HQ picks option B (Q5).
5. **S5**, remaining steps 10–14.
6. **S6**, all steps; needs S3, S4 and S5.
7. **S7b** (routine defaults) and **S7c** (PAC mirror, default-off).
8. **T, translation close-out and boundary rule**: the `INTRA_DOMAIN_RULES` table, transitive and runtime isolation tests, the `DIRTY_DELIVERY` allowlist, the retention-time golden scenario (T5), and a full golden re-run.

Each slice also carries its own local AST seam test from its first commit.

## 2. Per-slice package trees and steps

### 2.0 Conflicts resolved

| Conflict | Resolution | Reason |
|---|---|---|
| Three class rulebooks: S2 `rules.py`, S5 `delivery/classes.py`, translation `kernel.classify_origin` | Keep S2's only. S5's `classes.py` and the translation `classify_origin` are dropped. Delivery receives `message_class_of` by injection from the facade, as S5 already does for the decoder. | S2's rules are the only ones proven sound against caller-chosen `conversationId` and `idempotencyKey`. S5's dispatch-actor *recipient* rule and S6's `conversationId`-prefix rule can be forged. S2's exact key match `reply:workflow-<uuid>` already covers PAC replies (`send.py:807` builds `reply:{message_id}`). |
| Frame key: `class` vs `message_class` | `message_class` | It avoids a clash with inbox-batch `items[].class`. Recorded in proto.md §2.2 and §5.4 (S2), not in send-links.md. |
| Thread model: S4 pair, S6 (conversationId + pair), translation pair | One thread per pair (S4 `threads.ids`). A legacy `conversationId` selects a conversation cell inside the thread. S6's `mint_legacy_thread_id` and translation's `kernel.thread_seed` are dropped. S6 imports `threads.ids` (allowed by the boundary rule). | Design §2: flow control is per pair. See Q2. |
| Conversation cell id: S6 `default_conversation_cell`, S3 `ensure(KeyBinding)`, translation `conversation_seed` | `keyed_cell_id('conversation', authority=<local owner>, key=thread_id + '\0' + (conversationId or ''))`. Derived locally on each side. In phase A the envelope `conversation_cell_id` is filled only by reply inheritance. | Under S3's verification only the authority can author the cell, so cross-owner threads get one cell per owner (S6's "one copy per owner"). |
| Link translation: translation's `LegacyLinkService(ThreadGate)` relocation vs S4's "LinkService stays the engine" | Adopt S4. `request_catalog/link.py _service` returns `app._threads.link_admin()`. `ThreadGate`, `build_legacy_link_service` and `request_translation/links.py` are dropped. The `LinkServiceSurface` Protocol plus its AST usage scan stay as a test in `tests/daemon/threads/`, guarding the S9 relocation. | The relocation touches 33 test files and the admission slot pin. |
| Route translation: translation `endpoints.py` (edits `send.py` and `status/views.py`) vs S4 `bind_adapter` | Adopt S4 (`sends.py` / `agent_bot_attempts.py` glue). `endpoints.py` is dropped and `status/views.py` (998 lines) is not touched. | — |
| Body hydration: S5 facade port, S6 `HydratingHistory` + `queries.py`, translation `InboxService(body_source=)` | Hydration lives inside the delivery facade: `conversation_messages` and `retained_body` call `BodyResolver.resolve(BodyRef…)`, which S6's `ConversationBodies` implements. `history/__init__.py` is untouched and `queries.py` changes by 2 lines (S5). | Hydration happens before `_admit_read_body`, which fixes S6's admission-order item. |
| Body release: S5 pull `BodyRecordIndex` keyed by message_id vs S6 push grants keyed by (source, position, payload sha) | S6's push grants. S5 owns the `body_release` table and the `grant_body_release` actor command. `BodyRecordIndex` and the defer/recheck column are dropped. | Message-id reuse (`receive.py:117-123`) makes keying by id unsound. |
| Dropped payload: S5 residue vs S6 `x''` | S5 residue: only `origin` and `replyTo` are kept. S6 hydrates when `body_dropped_at_ms IS NOT NULL`. | Keeps history origin and read_body admission identical. |
| Digests: S5 `body_sha256` (message text), S6 payload sha, translation verdict + text sha | Two columns: `body_payload_sha256` (whole payload, the hydration key) and `body_text_sha256` (message text, or `'!invalid'` for an invalid record). Both come from one `classify_send_body` shared with `send_lookup.py`. | — |
| Retention sources: S4 `threadPolicy.bodyRetentionMs`, S5 `RetentionOverrideSource`, S6 `bodyRetentionAfterAckMs`, S7 `delivery.bodyRetentionMs` | A single key, `delivery.bodyRetentionMs`, read from the S7 snapshot (combine `min`, clamp 1h..365d). S6 reads it at record time into `BodyGrant.retention_ms`. SYSTEM rows always use the code default. | See Q9. |
| Cell policy readers: S3 `policy_int(spec…)`, S4 `CellPolicySource`, S7 resolver | S7's resolver is the only reader. S3 drops `policy_int`/`policy_value` and adds `Cell.params`. S4 declares `ParamSpec('threads.policy', 'object', combine='collect')` and keeps its tighten-only merge in `threads/policy.py`. | — |
| S7 kinds `org` and `workflow-definition` do not exist in S3 | `PARAM_SOURCE_KINDS = {department, agent}`; `org` is added only when S3 adds a kind row. PAC definitions use S3 kind `graph` with id `keyed_cell_id('graph', owner, digest)`. | — |
| S7 writer trust: "S3 permission face or space writers" | Use S3 `codec.verify`: authors must equal the binding authority, so `params_writers = {verified author}`. | Resolves S7's top risk. |
| S7 refresher thread plus a `cell-data` close step | Dropped. The snapshot and the PAC publisher tick run on the S3 `cell-runtime` thread through a guarded index-generation hook. S3 lookups never do OrgFS I/O. | One fewer thread and shutdown step. |
| S6 placement `cells/<c.id>/rec-*` vs S3 `cells/<kind>/<id>.json` | Records go to `cells/conversation/<c.id>/rec-<writer>/*.jsonl` and the doc to `cells/conversation/<c.id>.json`, both in the owner-home space provided by S3 (new scope `owner`). S6's `OwnerHomeCellDocStore` becomes a thin adapter over that. S6 and S7c share the space. | — |
| Nobody edited `send.py` to stamp `thread_id` (S2 deferred to S4, S4 assumed S2) | S4 adds about 4 lines at `send.py:308` and `:428` using `getattr(self, '_thread_id_for', None)` + `with_envelope`. A reply inherits the thread, else it is derived. | — |
| Test directory overflow: `tests/daemon/delivery/message` would go 6→8 | S2's test goes there (7). S6's `test_record_feed.py` goes to `tests/daemon/delivery/bodies/` (6). | — |
| Translation package `request_translation/` | Not created. The translation layer is the seam edits in existing handlers. `DeprecationLog` goes in `threads/service.py` (route:, send-link and legacy-conversationId kinds; once per process per (kind, caller)). | — |

### 2.1 S2 (envelope)

Plan r2 unchanged, except:
- `kernel/impl/contracts/envelope.py` optionally also exports `DISPATCH_MESSAGE_PREFIX`, which `dispatch/identity.py` re-imports.
- Its rules are the single class authority.

Files touched: `inbox/envelope/{__init__,rules,frame,columns}.py`, edits to `api.py`, `zenoh_link.py`, `core.py`, `receive.py`, `retry.py`, `read.py`, the system producers, `send.py:308/:428/:796`, `forwards.py`, `sends.py`, `squire/addressing.py`, `ipc/wire.py`, and the deliberate edit to `test_schema_migration.py`.

S2 as delivered (consumers read this before relying on the rows above):
- No `frame.py` and no edit to `zenoh_link.py` (Q1). Same-node delivery keeps the envelope in process; a cross-node hop drops it and the receiver derives its own class.
- The sender rule is sender `system:hyprial:<node>` **and** `intent == "system"`. The sender alone can be forged: `message.send` accepts a caller-chosen `from` of that shape, and only daemon producers set `intent="system"`.
- `requested_message_class` / `_messageClass` is not implemented: adding it changes a daemon request's params (openapi). Relay shapes (`sends.py` `_deliver_to_live_user_proxy`, `_forward_as_actor`) therefore derive INTERACTION, the safe direction. **Open for the orchestrator:** reassign it (for example to S4, as an internal stamping seam that adds no request param) or drop it.
- The stored `message_class` column holds the explicit stamp only; NULL means "derive". System producers are not edited to stamp, so every unstamped row, including all system notices, stores NULL. A SQL filter on `message_class` alone does not find SYSTEM rows (see §2.5).
- `with_envelope` only sets; it cannot clear a stamp.

### 2.2 S3 (cell runtime)

Plan r2, plus:
- `Cell.params`.
- Scope `owner` with `CellSpaces.private_space(owner)`, tagged `{kind:'hyprial-owner-home', owner}`.
- An `on_index(generation)` hook list, with exceptions fenced.
- `policy_int` dropped.

Tree: `cells/{__init__,model,codec,permissions,ports}.py`, `cells/runtime/{__init__,cache,layout,mirror,service,agents}.py`, `composition/cells/{__init__,wiring,spaces}.py`, kernel helpers, `Agent.cell_id`, the shutdown step `cell-runtime`, and three GLOSSARY rows.

### 2.3 S7a (params)

New files: `cells/params/{__init__,spec,resolve,snapshot,source}.py` and `composition/cells/params.py`, which brings `composition/cells` to 4 files.

### 2.4 S4 (threads)

Plan r2 (`threads/` with 7 files, `composition/threads/`, `links/ports.py` additions, the hooks in `core.py`/`gate.py`, `request_catalog/link.py`, `sends.py`, `agent_bot_attempts.py`), plus:
- `CellPolicySource` reads the S7 snapshot;
- the `send.py` stamping;
- `DeprecationLog`.

### 2.5 S5 (delivery)

Plan r2, with these changes:
- Remove `classes.py` and `BodyRecordIndex`.
- `delivery/ports.py` gains `InteractionRow` / `BodyGrant` / `BodyRef` (typed on `EnvelopeLike`).
- `retention/schema.py` adds `body_release`.
- Candidates: SYSTEM rows by the code-default cutoff; INTERACTION rows only through a JOIN on `body_release` where position and payload sha match. SYSTEM is decided in Python with `message_class_of(row)` over rows past the cutoff, not by `WHERE message_class = 'system'`: S2 stores NULL for every unstamped row (§2.1 "S2 as delivered"). If S5 needs a SQL-selectable class it adds its own derived column, filled through `message_class_of`, never a second SQL rule list.
- `apply` writes `body_ref` and `body_payload_sha256` on the row.
- S5 also extends the `history.py` UNION column lists with the `body_*` columns.

The wake gate is unchanged: observe mode, with three enforcement commits gated on Q6.

### 2.6 S6 (conversations write side)

Plan r2 minus `mint_legacy_thread_id`, `HydratingHistory`, the `queries.py` edits and `inbox/contracts/interactions.py`. Kept:
- `conversations/{__init__,bodies}.py` and `record/{contracts,redaction,store,segments,recorder}.py`;
- `inbox/record_feed.py`;
- the facade passthroughs;
- `composition/conversations/{__init__,wiring,placement}.py`;
- the shutdown step `conversation-recorder`;
- `conversation-records.sqlite3` added to `MIGRATION_DATABASES`.

Changes from r2:
- The thread comes from the envelope, else `app._thread_id_for`. With neither, the job is skipped as `unthreaded`, which keeps the body.
- Classification uses `message_class_of`.

### 2.7 S7b and S7c

Plan r2. `routine_defaults.py` and the `routines.py` fallback are unchanged. The PAC publisher writes `graph`-kind cells through `CellWriter.ensure`, scoped `owner`.

### 2.8 Translation close-out (T)

- **G0 goldens:** harness, three scenario files, coverage manifest, mutation probes.
- **B1:** `INTRA_DOMAIN_RULES` in `check_module_boundaries.py`, decision text in the docstring. Rules:
  - network ⇏ orgfs, cells, conversations, threads;
  - inbox and the future delivery ⇏ orgfs, cells, conversations, threads;
  - `application.messaging.delivery` ⇏ orgfs, cells, conversations;
  - threads ⇏ conversations, orgfs.
- **B2:** transitive closure following package `__init__` edges, with a shrink-only `DIRTY_DELIVERY = {sends, lark, status_queries}` whose witness is the composition hub.
- **B3:** runtime import isolation in a subprocess (`-I`).

## 3. Migration and compatibility

**Wire**
- Frame 1 (`hyprial-delivery-frame/v1`) gains optional keys `thread_id`, `conversation_cell_id` and `message_class`, emitted only when set. Frame 4 gains optional `message_class`.
- Schema strings stay the same. Decoding is lenient: an invalid value becomes None.
- Byte-identical frames for legacy-shaped messages and frozen 0.5.5.3.3 decoders are both pinned.
- If an old hop strips the stamps: direct system producers still derive SYSTEM, and the listed relay shapes degrade to INTERACTION (the safe direction).
- No openapi, IPC, error-code or gui/ changes.

**SQLite (`inbox.sqlite3`; ALTER is guarded by PRAGMA, no backfill; downgrade is safe because 0.5.5.3.3 INSERTs use explicit column lists)**
- outbox / inbox / custody / sender_copies: `thread_id`, `conversation_cell_id`, `message_class` (S2).
- inbox / sender_copies: `body_class`, `body_ref`, `body_dropped_at_ms`, `body_payload_sha256`, `body_text_sha256`; sender_copies also gets `body_settled_at_ms` (S5).
- New table `body_release(source, position, payload_sha256, ref, retention_ms, granted_at_ms, PK(source, position))` (S5).
- dlq and system_notices are unchanged. DLQ bodies are never dropped.
- New databases: `cells.sqlite3` (S3; rehome on owner change; kept out of `MIGRATION_DATABASES`) and `conversation-records.sqlite3` (S6; added to `MIGRATION_DATABASES`). `pac-graph.sqlite3` and `routines.sqlite3` are unchanged.

**OrgFS**
- `cells/<kind>/<id>.json`: eager kinds in org directory spaces, lazy kinds by id.
- An owner-home space holds conversation records and PAC definitions.
- `link/<actor>.json` is unchanged and still written only by `LinkStore`. Thread ids are projected from it, never stored.
- 0.5.5.3.3 peers ignore `cells/`.

**Drop gating**
- An INTERACTION body is dropped only with a matching grant. A grant requires the record to have been written, read back and indexed.
- When OrgFS is down, nothing is granted and nothing is dropped.
- Reverting only the S6 recorder is always safe. A full S6 revert after drops leaves `''` messages; there is a runbook entry for it.

## 4. Test strategy

- **Red to green per slice**, following each plan's steps. Pins that pass on first run (characterization, seam guards, byte pins) are labelled PIN, and each guard carries a seeded synthetic red case.
- **Goldens (G0)**, recorded with `HYPRIAL_GOLDEN_RECORD=1`; snapshot sha256 values are pinned.
  - Real `DaemonApplication`; uuid4 pinned to a seeded sequence (uuid5 and `workflow-` ids compared verbatim); wall clock frozen; `compose_link_service(clock_ms, seed=0)`; only the `link-service` thread is suppressed, with explicit `tick()`.
  - Normalizer allowlist: tmp path, pid, socket path.
  - Coverage assertion: `COVERED ∪ UNPROTECTED == _IPC_STATS_METHODS ∪ METHOD_REGISTRY.names ∪ {message.read_body, message.settle_hint}`, with the two sets disjoint.
  - Mutation probes, which must turn the goldens red: swapped stats keys, a changed uuid5 namespace, a changed `link.add` error code.
  - Allowed re-pins: only S3's first cell write (`orgfs.*` listing values) and the T5 retention-time scenario, each with the reason in the diff.
- **Translation equivalence**: S4 `test_translation.py` (link.* byte-identical through the registry, `DAEMON_NOT_READY` ordering); S5 `test_legacy_reads_after_drop.py` and `test_send_lookup_after_drop.py`; S6 `test_history_after_body_drop.py`, re-run on S5's real sweep.
- **Acceptance**:
  - OrgFS disconnected: S5 and S6 `test_orgfs_disconnected.py`, plus S4 step 12 if option B.
  - Data only: S7 `test_data_only_acceptance` (routine interval and S4 flow value).
  - Legacy link migration: S4's verifier reports `mismatches=0` inside `start()`.
- **Gates every commit**: `dump_daemon_openapi.py --check`, `npm --prefix gui run client:reference:check`, `check_module_boundaries`, `check_structural_limits`, `check_inline_comments --strict` on the diff, `check_unbounded_waits`, `check_method_registration`, `test_teardown_guarantee.py`, `test_whole_store_work.py`, `test_inbox_authority_gate.py`, `test_send_links_contract.py`. Zero diff under `gui/`, `contract/daemon-request/` and `.forgejo/workflows`.

## 5. Risks

- **Line caps.** Projected: `send.py` 970→~980, `facade.py` 817→~895, `shutdown.py` 957→~975, `sends.py` 768→~815, `composition/links.py` 924→~950, `core.py` 935→~943. `application/__init__.py`, `daemon/interface.py`, `status/views.py`, `read.py` (only shrinks), `router.py` and `projection.py` are not edited.
- **File caps.** `composition/` is at 7 direct files, so all new code goes in subpackages. `threads/` is at 7/7 and `retention/` at 7/7.
- **Composition hub.** Importing `composition` reaches `cells.observability`. S5 must move `DomainCommandError` to a leaf before any S9 move of `sends`/`lark`.
- **Merge conflicts.** `api.py`, `core.py`, `facade.py`, `sends.py` and `send.py` are touched by several slices; merge strictly in the order of §1.2.
- **Behaviour changes.** After 7 days, SYSTEM bodies become residue: `pac-<graph>` history shows `''`, and read_body raises the existing `MESSAGE_BODY_UNAVAILABLE`.
- **Mixed versions.** The listed relay shapes can lose their class over a 0.5.5.3.3 hop. Observe-mode `system:` sender forging predates this work.
- **OrgFS.** Snapshot growth from per-version layers. Owner-home spaces are a new user-visible OrgFS space. Two same-owner devices can each create a home space; readers take the union.
- **Owner migration.** Peers see re-minted cells as new cells; only the local `cell_aliases` table links them.
- **S3 runtime thread now runs hook code** (S7 snapshot and PAC publisher). Hooks must be fenced and bounded at 32 rows per tick.

## 6. Design questions and deviations for HQ

| # | Question | Recommendation |
|---|---|---|
| Q1 | proto.md §2.4 carve-out for `message_class` | Approve; Allen review gates the S2 merge. |
| Q2 | Thread granularity versus the glossary line "旧 conversationId 实为 thread id" | A thread is per (agent, counterpart endpoint). A legacy `conversationId` names a conversation cell inside the thread. GUI conversation ids are already per pair, so the glossary holds there. |
| Q3 | Conversation cells per owner in an owner-home OrgFS space; new S3 scope `owner` | Approve. Note it is visible in GUI OrgFS views. |
| Q4 | Class of PAC worker replies: SYSTEM (S2/S5) or interaction (translation) | SYSTEM, per §6 "PAC 内部派单". Results stay in `pac-graph.sqlite3`. Flipping is one rule. |
| Q5 | §10.0 "断开 OrgFS 时消息照常收发" for agent↔agent sends | Option B: last-known-good continuity for established links (S4 step 12). Boot without OrgFS stays out of scope. |
| Q6 | §6 "不唤醒人" versus watchdog, workflow escalation and PAC user notifications | Wake gate in observe mode; three separately revertible enforce commits, each gated on Allen. Class decides storage only. |
| Q7 | TTL/terminal Lark human leg | Exempt as in-thread feedback. |
| Q8 | DLQ bodies | Keep in phase A. |
| Q9 | Retention override | A single `delivery.bodyRetentionMs` from owner-authored department/agent cells. Per-conversation override deferred (conversation cells are not trusted param sources). No shorter SYSTEM default. |
| Q10 | Param trust | Local owner only in phase A; org-admin delegation in phase B. |
| Q11 | Physical moves (`links`→`threads`, `inbox`→`delivery`, `link_ledger`) | Defer to S9. Amend §9.6: `link_ledger` stays in the inbox database for transactional reasons. |
| Q12 | Test-only golden baseline commit G0 before S2 | Approve. It must capture pre-change behaviour. |
| Q13 | `sessions.*` deprecation log | No daemon-side call site in phase A; move it to phase B. |
| Q14 | S7c PAC mirror (write side only) | Keep default-off, or drop from phase A with no knock-on. |
| Q15 | GLOSSARY org-model table is missing on this branch | Import it from the design branch first; S3 adds its 3 rows. |
| Q16 | Thread `user` kind; adapter thread per adapter endpoint | Confirm both before pinning `contract/wire/thread-ids.json`. |
| Q17 | An explicit operator window mode beats cell data | Confirm. |
| Q18 | SYSTEM rows after retention: residue (keeps the row) or whole-row prune | Residue. |

**Slices whose review did not end sound:** none of the r2 plans was re-reviewed after revision.

- **S2:** sound.
- **S3:** sound.
- **Translation r2: unsound as written.** It is built on a `ThreadGate` relocation that S4 rejected, and on a second class rulebook. Replaced by §2.0.
- **S6 r2: unsound until §2.0 is applied.** It used forgeable `conversationId` classification, a thread model that conflicts with S4, its own hydration sites and an `x''` drop.
- **S5 r2: partly unsound.** Release keyed by message_id is exposed to id reuse, and its dispatch-recipient SYSTEM rule can be forged. Both are fixed by §2.0.
- **S4 r2:** cannot be accepted until Q5 is decided, and it had the stamping gap, fixed in §2.0.
- **S7 r2:** relied on S3 kinds and fields that do not exist (fixed in §2.0).

All of these need one re-review pass against this merged plan before implementation starts.