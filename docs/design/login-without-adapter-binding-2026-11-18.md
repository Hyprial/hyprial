# Login without adapter binding — plan

- Base read: `dev` @ `7bd92d5554` (read-only checkout). Date 2026-11-18. Planning only; no product code changed, no test suite run.
- Owner decision (2026-11-18): login does not need to bind Lark. An adapter's binding must not block any main step and must not appear in the code of login or similar flows at all; adapters come in entirely as plug-ins.
- Consistent with `z-5.md`; its Q2 is closed: the daemon-side login not obtaining the proof is the end state.
- Marks: **R** read from code at the cited line, **U** unverified. `S/` = `src/hyprial/`.

## 0. Status update 2026-11-18: the confirmation step is cancelled; current state of the code

**Cancelled (2026-11-18).** PR B (`hyprial user bindings confirm`, the daemon method `identity.binding.submit`, the neutral `external_identity` assertion helper), the "six security checks PR B must re-create" of §14, and every follow-up that depended on them (PR D as a dependent of Q3, Q5, Q6, the `org join` hint, the closing of KP-105 "when the tests exist") are cancelled. Owner's reason: a hyprial user is never bound to an IM identity (Lark, Feishu and so on); what relates to an IM is the adapter. A step that obtains a user-to-IM proof therefore has no place in the product. PR A (login stops obtaining the proof) stays as merged; PR C (GUI) is carried out in the tier 1 cleanup of 2026-11-18. The sections below are kept as the record of what was planned; cancelled text is marked **[CANCELLED 2026-11-18]** and left readable.

**The code still binds users at runtime.** This document and the cancellation do not change that. Until the behaviour-changing removal (tier 2) lands:

- Stores that tie a hyprial user to a Lark identity still exist: `settings.json` `identityClaims` (`oauth_Lark_unionId`, written on every login) and `identityBinding` (held rows only, nothing writes new ones since #2943); the OrgFS documents `directory/people/<slug>/binding.json`; `state/verified-org-bindings.json` and `state/identity-binding-suppression.json`; `state/identity.sqlite3` (`identity_overrides`, `identity_account_overrides`); `state/users.sqlite3` `user_accounts` with the mirror `users/<key>/profile.json` `accounts[]`; `state/users.json` `ownerOpenId`; the adapter table columns `hyprial_owner`, `standing` in `state/adapters.sqlite3`.
- Two runtime readers depend on them. Inbound: the "who sent this" block of every Lark message is built by `IdentityResolver` from those stores (`daemon/impl/adapters/lark/inbound/adapter.py`, `daemon/impl/identity/resolver.py`). Outbound: the Lark recipient of every `user:<owner>` delivery is chosen from the same stores (`daemon/impl/adapters/user_delivery/receiver.py`, `resolver.py` `profile_open_id`, `owner_open_id`). No code gate refuses a message on the sender's owner; messages are never dropped.
- The behaviour-changing removal (login no longer storing the Lark union id, proof publication and verification, the resolver and overrides, inbound sender attribution, outbound recipient selection, the adapter-table owner column) is tracked in PR #2950 (`docs/design/user-im-binding-removal-tier2-2026-11-18.md` on that PR's branch). Known problem: KP-105.

## 1. Goal and observable behaviour

Goal: no login, logout-adjacent, init or org-join path starts, waits for, validates, stores or reports a Lark binding. ~~Obtaining the proof becomes one explicit, optional, non-blocking step owned by the daemon's Lark identity code, reached through a neutral identity capability ("obtain an assertion for a linked external account").~~ **[CANCELLED 2026-11-18]**: the step is not built; see §0.

| Situation (isolated home, fakes) | Before | After |
|---|---|---|
| `hyprial login`, account has a linked Lark identity | two device confirmations (account, then "binding proof"); the second can wait for two expiries | one confirmation; login never waits for anything adapter-related |
| `login --json` stderr progress | `device`, `authenticated`, `binding-device` (maybe twice, `restarted`), maybe `binding-proof-failed`, `user-key`, `committed` | `device`, `authenticated`, `user-key`, `committed` |
| `login --json` stdout result | keys `ok, identity, device, daemon, migration, pending, verificationUri, userCode` | unchanged (no key ever carried the binding, R `S/shell/impl/login/flow.py:712-724`) |
| `settings.json` after login | `identityBinding {user, larkUnionId, proof, publishedAt}` written, replaced or removed by login | login never reads, writes or removes the key; an existing value stays byte-identical |
| `hyprial org join <link>` | best-effort second device confirmation before the join (can wait for two expiries) | joins immediately; no device confirmation |
| Daemon-side login (`auth.login.begin` → worker commit) | preserves a matching binding, removes any other | does not touch the key |
| Obtaining a proof | side effect of login / org join | ~~`hyprial user bindings confirm` (PR B), optional, never required by another command~~ **[CANCELLED 2026-11-18]**: no command obtains a proof, none will |
| Publication of a held proof to org directories, `user bindings publish|withdraw`, verification of other members' rows | daemon | unchanged |
| `hyprial logout` | removes `settings.identityBinding` when present | unchanged in PR A and B (Q3) |

## 2. Feature map (current state)

### 2.1 Two different things are called "Lark binding" in login

| Thing | What | Produced by | Needs a second confirmation |
|---|---|---|---|
| **Union-id claim** | `settings.identityClaims.properties.oauth_Lark_unionId`, read from the access token / userinfo of the normal login | every login | no |
| **Binding proof** | `settings.identityBinding`: a JWT from a separate Casdoor application (`BINDING_ASSERTION_CLIENT_ID`), published to org directories once it is no longer a bearer | CLI login, `org join` | yes |

Both name Lark inside login code, so both are in scope of "must not appear in login code". They have different consequences when removed (§2.5, Q1).

### 2.2 Producers

| # | Path | Where (R) |
|---|---|---|
| P1 | In-process CLI login: reads previous binding, runs the second device grant, validates, builds the row, passes it to the owner writer | `S/shell/impl/login/flow.py:135-136` (imports), `:441` (`binding_profile`, used only by tests: `tests/shell/login/device/support.py:72`), `:512` (union id from userinfo), `:597-661` (proof), `:686-694` (`lark_union_id=`, `identity_binding=`) |
| P2 | Device grant for the assertion app, event `binding-device`, codes `BINDING_PROOF_EXPIRED`, `BINDING_PROOF_INVALID`, `BINDING_ACCOUNT_MISMATCH` | `S/shell/impl/login/binding.py:1-136` |
| P3 | CLI rendering of `binding-device`, `binding-proof-failed` | `S/shell/impl/cli/commands/login/flow.py:70-81` |
| P4 | `org join` retry: reads settings, runs P2, writes settings from the CLI through `write_settings_identity`, message "will retry on the next login or org join" | `S/shell/impl/cli/commands/org/commands.py:197-323`; called at `S/shell/impl/cli/commands/org/member/commands.py:36-39`, `:104` |
| P5 | Daemon-side login: preserve-only, validator kept "for a future caller" | `S/identity/impl/onboarding/credentials/binding.py:1-89`; `credentials/flow.py:18`, `:158-160`, `:181-182`; exports `credentials/__init__.py:5`, `:44`, `:47` |
| P6 | Union-id claim extraction (two copies) | `S/shell/impl/login/oidc.py:80-100`, `:436`, `:489`; `S/identity/impl/onboarding/credentials/oidc.py:59-78`, `:411`; `S/identity/impl/onboarding/worker.py:65`, `:278`, `:290`; `credentials/flow.py:121`, `:181` |
| P7 | Settings writers that accept, compare, write and **remove** both keys | `S/identity/impl/onboarding/identity_record.py:142-143`, `:170-171`, `:179-180`, `:381-382`, `:417-418`, `:428-429`, `:434-470` |

### 2.3 Storage

`<home>/settings.json`: `identityClaims` and `identityBinding`. Daemon state: `state/verified-org-bindings.json` (cache of verified rows of all members, `S/daemon/impl/identity/bindings.py:74`, `:323-426`), suppression state (`S/daemon/impl/identity/mutation/suppression.py`), org directory rows `people/<user>/binding.json` over OrgFS (`S/daemon/impl/org/network/directory.py:337-411`).

### 2.4 Consumers

| Consumer | Uses | Where (R) |
|---|---|---|
| Publication of the held proof, deferred until non-bearer, retries, withdraw/publish | `settings.identityBinding` | `S/daemon/impl/application/messaging/identity_binding.py:254-281` (drop), `:391-488` (publish); start `S/daemon/impl/application/wiring/transport.py:525`; after join `S/daemon/impl/org/network/ipc.py:245-296` |
| Verification of rows of **other** members (JWKS, membership) and cache projector | org directory rows | `S/daemon/impl/identity/bindings.py:107-320`, `:428-610` |
| Identity resolver, source `org-directory` | cache | `S/daemon/impl/identity/resolver.py:668-689` |
| Identity resolver, source `casdoor-login` (own union id) | `identityClaims` | `resolver.py:75-101`, `:209-222` |
| Lark inbound sender block | resolver | `S/daemon/impl/adapters/lark/inbound/adapter.py:593-646` |
| Outbound user delivery open-id lookup | resolver `bindings(platform="lark")` | `resolver.py:859-906` |
| Daemon methods `identity.binding.publish|withdraw`, `identity.bindings.list|audit`; CLI `hyprial user bindings …` | — | `S/daemon/impl/request_catalog/identity/binding/specs.py:21-33`; `S/daemon/impl/ipc/router.py:114-116`; `S/shell/impl/cli/commands/user/commands.py:257-559` |
| Logout: removes both keys, labels `settings.identityBinding`, `settings.identityClaims` | settings | `S/identity/impl/onboarding/logout.py:53-66`, `:167`, `:186`; `S/daemon/impl/request_catalog/onboarding/schemas.py:55-67`; `contract/daemon-request/openapi.json:23732`, `:25030`, `:109269`, `:110009`; `gui/src/client/generated/types.ts:657`, `:679` |
| GUI | only the two progress events and the logout labels; no screen reads the binding (no `identity.bindings` use outside generated types) | `gui/src/platform/auth/index.ts:14`, `:16`; `gui/src/features/onboarding/impl/types.ts:60`, `:62`; `AccountProgressPanel.tsx:35`, `:53`, `:133`, `:180`, `:243`, `:291-303` |
| Org membership | nothing: the join is never conditional on the proof | `org/commands.py:212`; `org/network/ipc.py:246` |

### 2.5 User-visible effects of having no binding proof

| # | Effect | Mark |
|---|---|---|
| E1 | Login asks for one confirmation and does not wait for a second | R |
| E2 | On the user's own node their Lark messages are still attributed to them (source `casdoor-login`), as long as the union-id claim is stored | R (`resolver.py:209-222`) |
| E3 | On **other** members' nodes the user is not resolved through the org directory: the sender block of their Lark messages is `standing: unresolved`, `owner: null`, unless that node has a local override (`hyprial user bind`) or a legacy verified row | R (`adapter.py:593-646`, `resolver.py:584-620`) |
| E4 | What an agent does differently with an unresolved sender (owner-only behaviour, permissions) | U |
| E5 | Other nodes cannot pick the user's open-id through a union binding for user delivery; override and legacy rows still work; with none, delivery is refused with `IDENTITY_UNBOUND` | lookup order R (`resolver.py:859-906`); end-to-end refusal U |
| E6 | Messages themselves are never dropped or blocked: the lookup "never fails the message" | R (`adapter.py:588-590`) |
| E7 | Org join, membership and invites work | R |
| E8 | `user bindings publish` succeeds and publishes nothing; `withdraw` works; other nodes' `user bindings list` has no `org-directory` row for the user | R (`identity_binding.py:421-425`) |
| E9 | GUI: the "additional authorization" step never appears | source R, runtime U |
| E10 | Logout receipt has no `settings.identityBinding` element | R (`logout.py:167`) |
| E11 | After an issuer key rotation a member can no longer refresh the proof by logging in (the docstring of `bindings.py:1-7` says so today); they use the explicit step | R for the text, U in practice |

### 2.6 What the Lark side already does without the proof (R)

`IdentityResolver` merges five sources (`S/daemon/impl/request_catalog/identity/schemas.py:9`): `casdoor-login` (own union-id claim), `org-directory` (the proof), `local-override` (`hyprial user bind`, `identity.override.set`), `legacy-user-bind` (accounts in the users store), `legacy-identities` (rows the adapter observed in `adapters.sqlite3` with standing `verified`). The adapter also records name/open-id pairs from member-change events (`adapter.py:649-660`). Only `org-directory` depends on the proof.

### 2.7 How adapters plug in today (R)

There is no adapter plug-in registry. `ADAPTER_PROVIDERS = frozenset({"lark"})` (`S/daemon/impl/adapters/spec.py:9`); `hyprial adapter add` imports `add_lark_gateway` directly (`S/shell/impl/cli/commands/adapter/admin.py:285-290`). The driver contract (`S/daemon/impl/adapters/contracts/driver.py:495-504`, `AdapterOnboarding.authenticate → VerifiedPlatformIdentity`) is about the bot/app, not about a person. No extension point for "prove a person's external identity" exists. The proof is also not tied to an adapter instance: the member who must confirm is often someone with no Lark adapter at all, so that other members' adapters recognise them. `adapter add` is therefore the wrong trigger for the confirming user.

## 3. Classification

| Piece | Class | After |
|---|---|---|
| OIDC device grant mechanics (`_fetch_endpoints`, `_request_device_authorization(scope=)`, `_poll_device_token(require_refresh=)`) | identity-core, neutral | stay in `S/shell/impl/login/oidc.py` |
| Assertion helpers `BINDING_ASSERTION_CLIENT_ID`, `BINDING_ASSERTION_OWNER`, `binding_assertion_*` (`S/identity/impl/org_directory/models.py:61-62`) | neutral mechanism: "an assertion for a linked external account" | stay; no adapter name in them |
| Owner, subject, issuer, mode, credential, identity generation, user key | identity-core | stay |
| Projection of allowlisted external-account claims into `identityClaims.properties` | neutral mechanism; the allowlist entry is adapter data (Q1) | stays as a mechanism; the word "Lark" leaves login code |
| Second device grant in login, `binding-device`, `binding-proof-failed`, `binding_profile`, previous-binding logic, `identity_binding=` | adapter-specific | deleted from login |
| `preserved_identity_binding`, both `validate_binding_confirmation` copies | adapter-specific | deleted; ~~validation re-homed in the daemon handler (PR B)~~ **[CANCELLED 2026-11-18]**: not re-homed |
| `identity_binding` parameter and `_write_identity_binding` in the settings writers | adapter-specific | deleted; writers never touch the key |
| `org join` retry | adapter-specific and blocking | deleted |
| Publication, verification, cache, resolver, `user bindings` commands | adapter-specific, already outside login (daemon identity domain, Lark-only by construction: `resolver.py:519-522`) | stay for now; ~~gain the one explicit acquisition step~~ **[CANCELLED 2026-11-18]**; removal is tier 2 (§0) |
| `identityBinding` in the logout field list | adapter-specific residue in a login-like flow | Q3 |
| GUI progress cases | adapter-specific display | deleted in PR C (carried out in the tier 1 cleanup of 2026-11-18) |

"Must not appear in login code" after PR A means: in `S/shell/impl/login/**`, `S/shell/impl/cli/commands/login/**`, `S/shell/impl/cli/commands/init/**`, `S/identity/impl/onboarding/**` (except the logout list, Q3) there is no occurrence of `binding_proof`, `identity_binding`, `identityBinding`, `binding-device`, `binding-proof-failed`, `BINDING_ASSERTION`, `binding_profile`, and (commit 2) no case-insensitive `lark`. A structural test pins this (T3).

## 4. Where the binding lives after the change **[CANCELLED 2026-11-18: this section describes the cancelled PR B; the owner's model is that users are not bound to an IM identity, see §0]**

- **Owner:** the daemon identity domain that already owns publication (`S/daemon/impl/application/messaging/identity_binding.py`, `S/daemon/impl/identity/`).
- **Step (PR B):** `hyprial user bindings confirm [--no-open] [--json]`. The CLI runs one device grant against the assertion application with the neutral helpers and submits the JWT to a new daemon method `identity.binding.submit {proof}`. The daemon validates (audience, issuer organization, `name` equals the daemon's owner, claim equals the stored union id), stores the row, and schedules publication with the existing deferral. Nothing else calls it; it is never a precondition.
- **Hints, not triggers:** `org join` and `user bindings audit` may print one `nextSteps` line produced by the daemon result when no proof is held. No command starts a device grant on the user's behalf.
- **Neutral capability identity keeps:** device grant for a named client id and scope; unverified-claims and audience helpers; JWKS verification. Any future adapter can use them with its own client id and claim key.
- **Storage:** stays `settings.identityBinding` with the frozen four fields (it is also the org-directory wire format, `directory.py:340`). Reason: existing homes keep working with no migration and the publication code is unchanged. Relocation is Q3.

## 5. Pull requests

### PR A — remove the binding from login (before Z-5b)

Branch `refactor/login-without-adapter-binding`. Two commits.

**Commit 1, deletion first:**

1. Delete `S/shell/impl/login/binding.py` (136 lines).
2. `S/shell/impl/login/flow.py`: delete `:135-138` (import), `:441` (`binding_profile`), `:597-661`, the `identity_binding=identity_binding` argument `:691`. About −70 lines.
3. `S/shell/impl/cli/commands/login/flow.py`: delete `:70-81` (two event branches). −12 lines (the file is at 999).
4. Delete `S/identity/impl/onboarding/credentials/binding.py` (89 lines); `credentials/__init__.py`: remove `:5` and the two `__all__` names; `credentials/flow.py`: remove `:18`, `:158-160`, `:182`.
5. `S/identity/impl/onboarding/identity_record.py`: remove the `identity_binding` parameter (`:143`, `:382`), the comparisons (`:171`, `:418`), the calls (`:180`, `:429`) and `_write_identity_binding` (`:454-470`). The writers now leave an existing `identityBinding` key untouched.
6. `S/shell/impl/cli/commands/org/commands.py`: delete `:193-323` (two lazy wrappers and `_retry_binding_proof_before_join`) and the now unused imports (`datetime`, `UTC`, `time`, `webbrowser`, `notice`, `progress` if unused — check each). `org/member/commands.py`: delete `:36-39` and the call `:104`.
7. `S/daemon/impl/application/messaging/identity_binding.py:421-425`: after reading the row, skip publication and log `identity.org-binding.publish-skipped` with `reason="held row belongs to another account"` when `row.get("user") != str(self.owner)`. Needed because login no longer guarantees the held row matches the owner after an account switch. +8 lines (488 → 496).
8. `S/daemon/impl/identity/bindings.py:1-7` docstring: "must log in again" → "must run the explicit confirmation step again".
9. Docs: `CHANGELOG.md` entry; `docs/reference/known-problems.md` new entry "no way to obtain a new binding proof until PR B" (phenomenon, effects E3/E5, workaround `hyprial user bind` on the receiving node, decision, owner); `docs/design/gui-login-progress-status-2026-10-29.md:25-26`, `:68`, `:111` marked as superseded by this decision (one added paragraph, rows kept).

**Commit 2, neutral claim projection (only as answered in Q1; recommended form):**

10. `S/identity/impl/org_directory/models.py`: add `EXTERNAL_ACCOUNT_CLAIM_KEYS: tuple[str, ...] = ("oauth_Lark_unionId",)` beside `BINDING_ASSERTION_*`, exported through `identity/impl/org_directory/__init__.py`, `identity/interface.py`, `identity/impl/facade/types.py`. This is the one table where an adapter contributes data to identity.
11. `S/shell/impl/login/oidc.py:80-100`, `:436`, `:489` and `S/identity/impl/onboarding/credentials/oidc.py:59-78`, `:411`: `_lark_union_id` → `_external_account_claims(payload, access_token) -> dict[str, str]` (same precedence: token claims, then userinfo; only allowlisted keys; non-empty strings).
12. `flow.py:512`, `:690`; `worker.py:65`, `:278`, `:290`; `credentials/flow.py:121`, `:181`; `identity_record.py:142`, `:170`, `:179`, `:381`, `:417`, `:428`, `:434-451`: parameter `lark_union_id: str | None` → `external_claims: Mapping[str, str] | None`; `_write_lark_union_id` → `_write_external_claims` (writes `identityClaims = {"properties": {...}}` or removes the key when empty). On-disk bytes are identical to today for every existing account.
13. `resolver.py:75-98` is unchanged (it is Lark code reading its own key).

Size: product about +45 / −470; tests about +260 / −420.

### PR B — the explicit, optional confirmation step (after PR A; independent of Z-5) **[CANCELLED 2026-11-18]**

> Cancelled by the owner on 2026-11-18: a hyprial user is never bound to an IM identity; the adapter is what relates to an IM. Nothing below is implemented or will be. Kept as the record of what was planned.

1. New `S/shell/impl/external_identity/__init__.py` and `assertion.py` (about 90 lines): `obtain_account_assertion(profile, *, client_id, scope, emit, open_browser, open_url, sleep, clock) -> str`, the body of the deleted `obtain_binding_proof` with the client id as a parameter and neutral event name `assertion-device`. `S/shell/impl` goes from 3 to 3 files plus one directory.
2. `S/daemon/impl/request_catalog/identity/binding/specs.py`: `SUBMIT = MethodSpec(name="identity.binding.submit", owner="daemon.identity", params {proof: string, minLength 1, maxLength 8192}, result {stored: const true, publishAfter: number, orgs: [string], operatorVerified: const false}, errors = the existing `_ERRORS`, `mode="unary"`, `mutates=True`, `retry="never"`, `audience=("local-operator",)`, `http_exposed=False`, `idempotency never`)`; `examples.py`, `__init__.py` accordingly.
3. `S/daemon/impl/ipc/router.py:116`: add `"identity.binding.submit"`. `S/daemon/impl/identity/ipc.py`: route it like publish/withdraw (operator check `:37-43`).
4. `S/daemon/impl/application/messaging/identity_binding.py`: `_submit_identity_binding(proof, …)` (about 60 lines, file stays under 600): validation with `binding_assertion_claims_unverified`, `binding_assertion_audience_matches`, `BINDING_ASSERTION_OWNER`, `claims["name"] == owner`, claim equals `IdentityResolver._owner_union_id()`; refusal is `INVALID_ARGUMENT` with `data.reason` in `{"assertion-invalid", "other-application", "other-organization", "other-account"}` (no new kernel error code); write the row with `atomic_json_write` under the publication lock; call `_publish_identity_binding()`.
5. `S/shell/impl/cli/commands/user/commands.py` (559 lines): `@bindings_app.command("confirm")`, about 55 lines: resolve profile, `obtain_account_assertion(client_id=BINDING_ASSERTION_CLIENT_ID, scope="openid profile")`, `runtime.request("identity.binding.submit", {"proof": …})`, result through `CliResult`; progress through `progress`/`notice`.
6. `org.join` / `org.execute` results: optional `nextSteps: ["hyprial user bindings confirm"]` when casdoor mode, a union-id claim exists and no row is held (daemon side, `org/network/ipc.py:258-296`). Only if the owner wants the hint (Q5); it touches `request_catalog/org/**` result schemas.
7. Docs: `docs/guides/operator-guide.md:389` section gains the command; close the known-problems entry of PR A (update, do not delete); `Skill-Impact:` line (ops skill mentions `user bindings` only if present — U).

Size: product about +260; tests about +420.

### PR C — GUI clean-up (independent; suitable for an external developer)

> Carried out in the tier 1 cleanup of 2026-11-18 (the two progress branches were removed from `gui/`).

Remove `'binding-device'` and `'binding-proof-failed'` from `gui/src/platform/auth/index.ts:14`, `:16` and `gui/src/features/onboarding/impl/types.ts:60`, `:62`; remove the cases in `AccountProgressPanel.tsx`; update `gui/tests/login-progress.test.tsx`, `native-host.test.tsx`, `settings-first-run.test.tsx`, `dev01-auth-actions.test.ts`. Follow the single-GUI rule and the UI hand-off template; both entries (preview, live) share the same component. No backend change. Until it merges the cases are unreachable, not wrong.

### PR D — only if Q3 is answered "relocate" **[CANCELLED 2026-11-18]**

> Dependent on the cancelled PR B line; superseded by the tier 2 removal tracked in PR #2950.

Move the held row to daemon-owned state, read the old key once, remove `identityBinding` from `LOGIN_SETTINGS_FIELDS` and `LOGOUT_REMOVED_LABELS`, regenerate `openapi.json` and the GUI types.

## 6. Coordination-zone files

> The PR B and PR D columns of this section and of §7 are **[CANCELLED 2026-11-18]**; only the PR A column describes work that was done.

| File | PR A | PR B | PR D |
|---|---|---|---|
| kernel `ipc_errors.py` | — | — (reuses `INVALID_ARGUMENT`) | — |
| `S/shell/impl/request_http/**`, `shell/impl/http_contract/**` | — | — (`http_exposed=False`) | — |
| `S/daemon/impl/request_catalog/**` | — | `identity/binding/specs.py`, `examples.py`, `__init__.py`: one method; `org/**` only with the Q5 hint | `onboarding/schemas.py:60`: one label removed |
| `S/shell/impl/cli/commands/common/support.py` | — | — | — |
| `contract/daemon-request/openapi.json` | — | regenerated only if non-HTTP methods appear in it (U) | 4 enum sites |

PR A touches no zone file. PR B needs the zone owners' sign-off before pushing.

## 7. Gates

| Gate | PR A | PR B |
|---|---|---|
| Import ratchet `scripts/ci/import-ratchet-baseline.json` | **remove** line 220: `{"path":"src/hyprial/shell/impl/cli/commands/org/commands.py","module":"hyprial.daemon","symbol":"write_settings_identity","count":1}` | no entry: the CLI imports only `hyprial.shell.impl.external_identity` (not a backend domain) and `BINDING_ASSERTION_CLIENT_ID` is read inside that module. If the constant must be imported in `user/commands.py` instead, that is a new triple and needs approval: `{"path":"src/hyprial/shell/impl/cli/commands/user/commands.py","module":"hyprial.identity","symbol":"BINDING_ASSERTION_CLIENT_ID","count":1}` — avoid it |
| `scripts/ci/import-ratchet-exceptions.json` and its digest | unchanged (no exception names the removed symbol, R `:655-672`, `:825-840`) | unchanged |
| Request-architecture ledger `scripts/ci/daemon-request-architecture-baseline.json` | **remove** two entries: `backend-import` / `org/commands.py` / `hyprial.daemon:write_settings_identity` (`:1194-1199`) and `file-mutation` / `org/commands.py` / `open` (`:2136-2141`, it is the `webbrowser.open` at `org/commands.py:257`) | none added if the request uses a literal method name; U until the gate is run |
| Method registration / `registry-union.json` / legacy-method baseline | — | one method added to whatever those gates enumerate (U: exact files) |
| Module boundaries | fewer cross-domain imports | `shell/impl/external_identity` imports `hyprial.daemon` (`NetworkProfile`) and `hyprial.identity` public faces only |
| Public-seam / facade tests | `credentials/__init__.py` loses 2 names; `write_settings_identity`/`write_settings_owner` lose one keyword; commit 2 adds one exported constant and renames one keyword. U whether a test pins these signatures: run `tests/identity`, `tests/tooling/architecture` | one new shell package |
| Structural limits | files: `shell/impl/login` 7 → 6, `identity/.../credentials` 6 → 5, `tests/shell/login/device` stays 7 (one deleted, one added), `tests/shell/cli/org` stays 7, `tests/tooling/architecture/identity` 5 → 6; `cli/commands/login/flow.py` 999 → 987 | `identity_binding.py` < 600; `user/commands.py` ≈ 615 |
| Post-install surface snapshot `tests/shell/cli/installation/restart/post_install/post_install_surface.json` | no entry changes expected: deleted modules are not listed (R `:903-908`, `:621-623`); run the directory to confirm | no change expected |
| CLI surface `contract/cli/surface.json`, `docs/reference/cli-command-inventory.json` | unchanged (no command or option changes) | regenerate: one command `hyprial user bindings confirm` |
| Contract impact | no file under `contract/` describes the login events (R, grep). PR body: `Contract-Impact: login and org join no longer emit the progress events binding-device and binding-proof-failed (codes BINDING_PROOF_EXPIRED, BINDING_PROOF_INVALID, BINDING_ACCOUNT_MISMATCH no longer appear); login no longer writes or removes settings.identityBinding; login --json stdout keys and logout labels are unchanged.` | `Contract-Impact: new local-operator IPC method identity.binding.submit and CLI command user bindings confirm; no existing field changes.` |
| E2E impact, changelog, inline comments, PR hygiene | declare `E2E-Impact` per `scripts/ci/check_e2e_impact.py` for the login scenarios (U: which ids); changelog entry; deletions only add no inline comments; decisions go in file-header blocks | same |
| GUI gates | not touched | not touched (PR C meets them) |

## 8. Tests

### 8.1 PR A — must fail first, then pass

| # | File | Test | Red today because |
|---|---|---|---|
| T1 | new `tests/shell/login/device/test_no_adapter_binding.py` (replaces `test_binding_proof.py`) | fake issuer with a Lark-linked account: `run_login` completes; the fake's assertion-application authorization counter is 0; emitted event kinds contain no `binding-*`; settings has no `identityBinding` | the second device authorization is requested and the row is written |
| T2 | same | settings already hold a row for the same owner: after login the row is byte-identical | login replaces it (new `proof`, `publishedAt`) |
| T3 | new `tests/tooling/architecture/identity/test_login_names_no_adapter.py` | the forbidden tokens of §3 do not occur in the listed source trees (the logout list is the one named exception while Q3 stands) | they occur |
| T4 | `tests/identity/onboarding/test_identity_record.py:40-75` (changed) | both writers reject an `identity_binding` keyword and leave an existing `identityBinding` key untouched when called with and without external claims | keyword accepted; key removed when `None` |
| T5 | `tests/identity/onboarding` worker/commit test (new case beside `test_worker_negative.py:700`) | `commit_identity` on a home with a held row for another account leaves `settings.identityBinding` untouched | the row is removed |
| T6 | `tests/shell/cli/org/test_cli_org.py:521` (replaced) | `org join --json` on a casdoor home with a union-id claim and no row: the join request is sent; any device-grant helper is patched to fail the test if called; stderr has no `binding-*` event | the retry calls the helper |
| T7 | `tests/daemon/identity/r2g/` new case | held row whose `user` differs from the daemon owner: `put_binding` is never called, `publish-skipped` logged | the row is published |
| T8 | commit 2: `tests/shell/login/device/test_persistence.py:54-80` (kept, renamed) | only allowlisted keys are projected; a second `oauth_*` property in the fake userinfo is not stored; on-disk `identityClaims` equals today's bytes | green today for the Lark key; the extra-key assertion is new |

### 8.2 Tests deleted, with reason

- `tests/shell/login/device/test_binding_proof.py` (11 tests, `:32-325`): they test acquisition inside login, which no longer exists. Six of them pin security properties of the confirmation (`:153`, `:181`, `:208`, `:229`, `:278`, `:307`: other account, other organization, mismatched union id, audience forms). These were to be **re-created in PR B** against `identity.binding.submit` **[CANCELLED 2026-11-18: PR B is not built, nothing is re-created]**; the PR B body lists the six by name. `:129` (refresh token of the assertion grant is discarded) is re-created against `obtain_account_assertion`.
- `test_org_join_retries_binding_proof_before_daemon_publishes`: replaced by T6 (the behaviour is deliberately removed).
- `tests/shell/login/device/support.py:72` `binding_profile` default: removed with the parameter.
- `tests/fixtures/network/fake_oidc.py:125-134`, `:309-398` (assertion application in the fake): kept; PR B uses it.
- Unchanged and must stay green: `tests/daemon/identity/**` (publication, withdraw, cache), the logout tests naming `settings.identityBinding`, `tests/shell/packaging/test_installed_baseline.py:104-116` and `tests/tooling/containers/test_worker_image_gitless_wheel.py:190-199` (they use the old path only as a literal string for a path predicate, R).

### 8.3 PR B — fail first **[CANCELLED 2026-11-18]**

`tests/daemon/identity/r2h` is at 6 files, `r2g` at 2: new `tests/daemon/identity/r2g/test_binding_submit.py` (valid assertion stored and publication deferred; the six refusals; non-operator caller refused; non-casdoor mode refused); `tests/shell/cli/users/` case for `user bindings confirm` with the scripted daemon and `fake_oidc`; a test that `login`, `init` and `org join` never send `identity.binding.submit`.

### 8.4 Focused verification (each PR)

```bash
uv sync --extra test --python 3.12
uv run --python 3.12 pytest -n 2 \
  tests/shell/login tests/shell/cli/org tests/shell/cli/users tests/shell/cli/installation \
  tests/identity/onboarding tests/daemon/identity tests/daemon/onboarding tests/daemon/org \
  tests/daemon/adapters/lark/identity tests/shell/packaging \
  tests/daemon/startup tests/shell/compatibility tests/identity/provider_keys/structure \
  tests/tooling/release tests/daemon/ipc/error tests/tooling/architecture tests/tooling/ci \
  tests/integration/native_harnesses
uv run --python 3.12 ruff check .
uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py
uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py
uv run --no-project --python 3.12 python scripts/ci/check_import_ratchet.py
uv run --no-project --python 3.12 python scripts/ci/check_daemon_request_architecture.py
uv run --no-project --python 3.12 python scripts/ci/check_inline_comments.py
./contract/run-all.sh --isolated-only
uv run --python 3.12 --with libcst==1.9.0 pytest -n 2 tests/
```

PR B adds `tests/daemon/ipc/contracts/identity`, `tests/shell/request_http`, `tests/integration/identity` and the CLI surface regeneration (`scripts/verify/update_cli_surface_snapshot.py`). The exact argument forms of the two ledger checkers are U; read their `--help` first.

## 9. Compatibility

- **Homes that already have `identityBinding`:** nothing is migrated. The row stays, and the daemon keeps publishing it while its `user` is the daemon's owner and its union id is among the account's current stored external claims (§14); every earlier writer stored the claim in the same write as the row, so a home written by an earlier version keeps publishing. Re-login with the same account no longer refreshes it. After an account switch the old row stays on disk and is skipped by step 7 until logout or a new confirmation replaces it.
- **Published `login --json`:** stdout unchanged. stderr events `binding-device` and `binding-proof-failed` disappear, as does the human-mode "binding proof confirmation required" text and the warning "will retry on the next login or org join". `org join --json` loses the same two events.
- **Logout:** unchanged; still removes the key and reports the label (Q3).
- **GUI:** reads nothing about the binding. The generated logout labels are unchanged. The two progress cases become unreachable until PR C.
- **Daemon-side login (Z-2/Z-5 path):** was preserve-or-remove, becomes do-not-touch.
- **Docs:** `gui-login-progress-status-2026-10-29.md`, `bindings.py` docstring, known problems, changelog.

## 10. Sequencing

1. **Z-4 (#2925)** merges first. It changes one keyword in `S/shell/impl/cli/commands/login/commands.py`; no PR here touches that file.
2. **PR A** next, **before Z-5b** (login client). Then Z-5 never carries the binding path. Edits needed in `z-5.md`: drop the "Lark binding proof" item of §2, the "no `binding-device` event on this route (Q2)" clause and the known-problem entry of §3.5, close Q2; line references into `S/shell/impl/login/flow.py` after `:597` shift up by about 67 (the opener `:404-410` that Z-5 moves is unaffected). R1 of Z-5 (golden comparison of the legacy and the daemon route) gets simpler because the two routes now emit the same events.
3. **Z-5a** (init decoupling) is independent of PR A in files; any order.
4. ~~**PR B** after PR A, any time relative to Z-5. It needs zone sign-off.~~ **[CANCELLED 2026-11-18]**
5. **PR C** any time after PR A.

External developer: PR C is independent enough. PR A is mostly deletion with a clear red-first list, but it gates Z-5b and edits identity writers; keep it in this line. PR B touches the zone; keep it with the zone owners.

## 11. Safety constraints for the implementer

- Isolated home, state directory, socket and ports only; never a real `~/.hyprial`, production daemon, service unit, credentials or user configuration.
- Never call a real identity issuer or real Lark: only `tests/fixtures/network/fake_oidc.py`, scripted daemons and recorders. Never run `hyprial login` / `hyprial init` outside those fakes (they reach the public issuer even with `HYPRIAL_NETWORK_ISOLATED=1`, KP-085). Red-first runs on the base use the fake issuer through `run_login`, never the real command.
- No machine user names, host names or e-mail addresses in any new file; reuse the fixture's placeholder names.
- No `skip`/`xfail` beyond platform preconditions, no weakened assertion, no raised timeout to get green. Real-issuer and real-Lark checks are reported BLOCKED, never PASS.
- Assertions, device codes and tokens never appear in CLI output, progress events or logs; `identity.binding.submit` logs only outcome and reason.
- No inline comments; decisions in the file-header block. `uv`, Python 3.12. CLI output only through `shell/impl/cli/output`.
- Ledgers shrink only. If a gate asks for a new entry that is not listed in §7, stop and ask.

## 12. Risks

> Risks K1, K5, K6 mention PR B, which is cancelled (2026-11-18); read them as history.

| # | Risk | Mark | Handling |
|---|---|---|---|
| K1 | ~~Between PR A and PR B~~ From PR A on nobody can obtain a new proof, and **[CANCELLED 2026-11-18]** PR B will not change that (E3, E5, E11 for new accounts) | R | known-problems entry; keep the gap short; Q4 |
| K2 | A held row of a previous account stays in `settings.json` after an account switch | R | step 7 skips it; logout removes it; PR B overwrites it |
| K3 | A test or caller outside the grep set passes `identity_binding=` / `lark_union_id=` | U | full suite; `grep -rn` before pushing |
| K4 | Agents behave differently for an unresolved sender (E4) | U | not changed by this plan for existing users; documented |
| K5 | The assertion travels over the local IPC socket while still a bearer (PR B) | R | local-operator audience only; never logged; today it sits in `settings.json` for the same period |
| K6 | The method-registration gates need files not listed here (PR B) | U | run them before requesting sign-off |
| K7 | Commit 2 changes the keyword of two exported writers | R | all callers are in the grep list of §5 |

## 13. Open questions for the owner (recommended answer first)

- **Q1. The union-id claim that login stores without any extra confirmation.** Recommended: keep the stored claim, make login generic — login projects the keys of a neutral allowlist table; the single Lark key lives in that table in `identity/impl/org_directory/models.py`, not in login code. Own-node attribution (E2) is unchanged. Alternative A: login stores nothing; the union id is learned only from the confirmation step — then a user's own Lark messages are unresolved on their own node until they run it. Alternative B: store every `oauth_*` property — no adapter name anywhere, but it writes display names and e-mail addresses of linked accounts into `settings.json`, reversing a deliberate minimisation (`test_persistence.py:54`).
- **Q2. `org join` retry.** Recommended: delete it in PR A (it blocks a main step for up to two expiries and deleting it shrinks three ledger entries). Alternative: keep it until PR B — then the `identity_binding` keyword must stay in the settings writers for one more PR.
- **Q3. `identityBinding` in logout and in `settings.json`.** Recommended: leave both for now; logout keeps removing the key as account-scoped residue and the published label stays. Alternative (PR D): move the row to daemon-owned state and drop the label — a contract change in `openapi.json` and the GUI types, plus a one-time read of the old key.
- **Q4. The gap of K1.** Recommended: accept it and merge PR A first so Z-5b starts clean. Alternative: hold PR A until PR B is ready — Z-5b then either waits or carries the binding path.
- **Q5 [CANCELLED 2026-11-18: no confirmation step, so no hint]. Hint after `org join`.** Recommended: no hint in PR B (zero change to `org` schemas); document the command. Alternative: one `nextSteps` line from the daemon — touches `request_catalog/org/**`.
- **Q6 [CANCELLED 2026-11-18: the step is not built]. Name and shape of the step.** Recommended: `hyprial user bindings confirm` + `identity.binding.submit` (CLI obtains, daemon validates and stores). Alternative: the CLI writes `settings.json` itself — a new backend import in a shrink-only ledger; or the daemon runs the device grant — a new operation kind in the credential worker, much larger.

## 14. Implementation record of PR A and ~~what PR B must re-create~~ [CANCELLED 2026-11-18]

PR A (branch `refactor/login-without-binding-proof`) implements §5 "PR A" with these differences from the text above:

- The publisher guard (§5 step 7) has two rules, not one. A held row is skipped when its `user` is not the daemon's owner (`reason="held row belongs to another account"`), and when its `larkUnionId` does not equal a current stored external claim of the account from `EXTERNAL_ACCOUNT_CLAIM_KEYS`, including when no claim is stored (`reason="held row does not match the account's current external claim"`). The row is never deleted by the publisher. Tests: `tests/daemon/identity/r2g/test_publication_account_guard.py`.
- The structural test scans login, the CLI login, `init`, `org` and invite roots, with exact-literal exemptions only (`tests/tooling/architecture/identity/test_login_names_no_adapter.py`).
- `identity/interface.py` was at its 900-line facade budget. The public name `binding_assertion_claims_unverified` had no consumer outside `identity/` and was removed from the identity facade (`interface.py`, `facade/types.py`) to make room for `EXTERNAL_ACCOUNT_CLAIM_KEYS`. ~~PR B must import it from a public face again, or reach it inside its own domain, when the confirmation step needs it.~~ **[CANCELLED 2026-11-18]** with PR B.

### Security checks whose tests were deleted with the proof **[CANCELLED 2026-11-18: not to be re-created]**

> Cancelled 2026-11-18. The confirmation step will not be built, so these checks have nothing to protect and are not re-created. The list is kept as the record of what the deleted tests covered.

`tests/shell/login/device/test_binding_proof.py` was deleted because acquisition and validation inside login no longer exist. These six properties are not tested anywhere after PR A. The optional confirmation step (PR B, `identity.binding.submit` and `hyprial user bindings confirm`) must re-create each of them as a failing-first test before it lands:

1. Another account: a proof approved by an account other than the node owner is refused and nothing is stored.
2. Another organization: a proof whose `owner` claim is not `BINDING_ASSERTION_OWNER` is refused.
3. Union-id mismatch: a proof whose external claim differs from the account's stored claim is refused.
4. Mismatch preserves the old row: a refused proof (cases 1 to 3) leaves a previously held row byte-identical.
5. Audience: audience forms the issuer signs (a string, an array with the binding application's client id, an array that also names another client) are accepted; an audience naming another application, an empty array, or none is refused.
6. Refresh token discarded: the refresh token of the assertion grant never reaches state, logs or the stored row, and the stored row has exactly the four frozen fields.

~~The known problem entry KP-105 in `docs/reference/known-problems.md` stays open until these tests exist.~~ **[CANCELLED 2026-11-18]** KP-105 is rewritten instead: no proof can be obtained and none will be; the removal is tier 2 (PR #2950).
