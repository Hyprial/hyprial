# Transport credential storage path audit

This table covers every filesystem read or mutation in
`identity.impl.device.transport_credential` and `transport_storage`. Paths are
relative to the identity home unless stated otherwise.

## Entrypoint read inventory

Source references below are the exact call sites in this audited revision.
Metadata-only inspection is listed as a read because it controls later mutation.

- **Stage** reads `state/tailcat/device.json` and
  `state/tailcat/device-identity.json` through
  `transport_credential.py:177,863-881` and `store.py:77-87`; it inspects/reads
  `state/transport/enrollment.json` at `transport_credential.py:180` and the
  operation's staged `secrets/transport/staged/<operationId>/key.pem` (or
  crash-recovered `secrets/transport/device-key.pem`) at
  `transport_credential.py:179,210,212,218`.
- **Validate** reads the two Tailcat device files at
  `transport_credential.py:287,863-881`. The response, staged projection, and
  pinned trust bundle are values supplied by the caller, not file paths.
- **Commit** reads `state/transport/enrollment.json` at
  `transport_credential.py:328` and the staged or committed key at
  `transport_credential.py:346`, then re-reads both Tailcat device files through
  validation at `transport_credential.py:351-356,287`.
  It also inspects operation-scoped `.credential.<operationId>.tmp` and
  `.trust-bundle.<operationId>.tmp` before replacing them at
  `transport_credential.py:375,384` and `transport_storage.py:280-317`.
  Only regular current-uid, single-link `0600`/`0644` leftovers are removed;
  unsafe ownership, link counts, types, or modes fail before source removal.
- **Retire** metadata-inspects every descendant to be removed below
  `secrets/transport/`, plus `state/transport/credential.json`,
  `state/transport/enrollment.json`, and `state/transport/trust-bundle.pem`, at
  `transport_credential.py:467-478,493-552` before any deletion at lines
  `480-490`.
- Credential content reads use `_read_safe_file` at
  `transport_storage.py:166-187`: `O_NOFOLLOW`, regular file, current uid,
  exactly one hardlink, and exact mode. Optional credential-file presence checks
  use lstat and reject symlinks or multiply linked files before that open
  (`transport_storage.py:126-137,248-252`). Tailcat device reads instead use the
  descriptor-relative primitive at `transport_storage.py:190-245`.

| Operation | Path source | Checks before access | No-follow and mode rule | Evidence |
|---|---|---|---|---|
| Local device read | `state/tailcat/device.json` and `state/tailcat/device-identity.json` via the identity device store (`store.py:77-87`) | Stage, standalone response validation, and commit-time validation open `HYPRIAL_HOME`, `state`, and `tailcat` in sequence using directory fds | Every ancestor uses `O_NOFOLLOW|O_DIRECTORY`; the final name is opened relative to the validated `tailcat` fd with `O_NOFOLLOW` and must be a regular current-uid, single-link `0600` file (`transport_credential.py:863-881`, `transport_storage.py:190-245`) | `test_stage_rejects_symlinked_tailcat_parent_without_creating_material`, `test_stage_rejects_hardlinked_device_files_without_creating_material`, direct-file symlink and local-device validation tests |
| Stage directory preflight | `secrets/transport`, `secrets/transport/staged`, `state/transport`, and `staged/<operationId>` | All paths are checked before any creation | Descriptor-relative walk from the home with `O_NOFOLLOW`; existing final directories must be `0700` | `test_stage_does_not_create_through_symlinked_parent`, `test_stage_preflights_all_parents_before_modifying`, `test_stage_preflights_operation_root_before_other_writes` |
| Stage directory creation | The four preflighted stage directories | Whole-set preflight completed first | Descriptor-relative `mkdir` and open with `O_NOFOLLOW`; final mode `0700` | Same three stage preflight tests |
| Enrollment read | `state/transport/enrollment.json` | All stage directories are preflighted; read happens before directory creation | Final open uses `O_NOFOLLOW`; regular current-uid single-link file, mode `0600` | `test_stage_rejects_unsafe_enrollment_before_creating_secret_roots`, `test_resume_rejects_symlinked_or_permissive_enrollment`, `test_read_rejects_unsafe_staged_enrollment_before_absent_result` |
| Staged key presence/read | `secrets/transport/staged/<operationId>/key.pem` | Operation directory is preflighted before other writes; final path is lstat-checked before creation or read | Symlink is unsafe; read uses `O_NOFOLLOW`; regular current-uid single-link file, mode `0600` | `test_stage_rejects_symlinked_key_before_other_writes`, `test_receipt_loss_resumes_same_operation_and_private_key`, `test_resume_rejects_permissive_operation_directory` |
| Staged key create/chmod/fsync | Same staged key | Whole-set directory and final-path preflight completed; unrecorded regular file causes a typed conflict | `O_CREAT|O_EXCL`; parent is no-follow `0700`; resulting mode `0600` | `test_receipt_loss_resumes_same_operation_and_private_key`, `test_commit_read_status_and_file_modes_do_not_expose_pem` |
| Enrollment atomic write/chmod/fsync | `state/transport/enrollment.json` | Existing enrollment was safely read or proved absent before any creation | Parent is no-follow `0700`; atomic replacement does not follow the final destination; mode `0600` | `test_receipt_loss_resumes_same_operation_and_private_key`, unsafe enrollment tests |
| Stage rollback unlink | Newly created staged key and enrollment | Only paths created by the current failed stage are removed | Final `unlink` does not follow symlinks; validated parents remain inside the home | Failure behavior is exercised by invalid/unsafe stage tests; no independent injected write failure exists because cleanup is best-effort rollback |
| Commit source and destination preflight | Source operation directory when `_key_path` is staged; `secrets/transport` and `state/transport` always | All applicable roots are checked before enrollment/key reads or writes; committed-key recovery omits the obsolete operation root | Descriptor-relative `O_NOFOLLOW`; existing final directories mode `0700` | `test_commit_rejects_symlinked_staged_key_parent_without_external_write`, `test_real_process_abort_is_resumable_or_absent` |
| Commit enrollment/key reads | Enrollment path and staged or committed key | Source and destination roots preflighted first | Enrollment/key final opens use `O_NOFOLLOW`; regular current-uid single-link files mode `0600` | Commit source test, crash recovery test, unsafe mode/symlink tests |
| Credential temporary cleanup/create | `state/transport/.credential.<operationId>.tmp` | Destination root preflighted; existing temporary path is lstat-checked | Only regular current-uid single-link `0600` leftovers are removed; create uses `O_EXCL`; unsafe metadata is typed | `test_real_process_abort_is_resumable_or_absent` |
| Key rename | Staged key to `secrets/transport/device-key.pem` | Source operation root and destination root preflighted; source key safely read and SPKI checked | Source ancestors do not follow symlinks; `os.replace` replaces rather than follows the final destination | `test_commit_rejects_symlinked_staged_key_parent_without_external_write`, crash recovery test |
| Trust-bundle temporary cleanup/create/rename | `state/transport/.trust-bundle.<operationId>.tmp` to `trust-bundle.pem` | State root preflighted | Temporary must be regular current-uid single-link `0644`; create uses `O_EXCL`; replace does not follow final destination; installed mode `0644` | Crash recovery test, `test_read_requires_the_committed_trust_bundle`, bundle identity/hash mutation test |
| Credential rename | Credential temporary path to `credential.json` | State root and temporary path validated | `os.replace` does not follow final destination; installed record mode `0600` | Crash recovery test and committed mode test |
| Commit cleanup unlink/rmdir | Enrollment, credential temporary, and empty operation directory | Their roots were preflighted; operation directory is removed only after its key rename | Final unlink/rmdir does not follow symlinks; failures to remove an already non-empty operation directory are ignored | Crash recovery test |
| Credential read completeness | Secret/state roots, key, credential, bundle | Both roots and their ancestry are preflighted even when the set is incomplete; lstat then checks all five paths before parsing | Roots must be directories `0700`; files must be regular current-uid single-link files with `0600`, `0600`, and `0644`; file opens use `O_NOFOLLOW` | `test_read_rejects_symlinked_parent_even_when_credential_is_incomplete`, `test_read_rejects_symlinked_material`, `test_read_rejects_unsafe_modes` |
| Retirement preflight | Parents and final roots for `secrets/transport` and `state/transport`; all descendants under the secret root; credential, enrollment, and bundle under the state root (`transport_credential.py:467-478,493-552`) | Every deletion target in both roots is inspected before either root is changed | Parent walk is descriptor-relative/no-follow; roots and descendants use lstat; real roots/directories are current-uid `0700`; every regular file must have exactly one hardlink; secret files and state JSON are `0600`, bundle is `0644`; unsafe children are typed and abort with zero writes | `test_retire_rejects_hardlinked_state_file_before_any_deletion`, `test_retire_preflights_directory_credential_before_any_deletion`, other retirement preflight tests |
| Retirement root unlink/rmtree | Both transport roots (`transport_credential.py:480-486`) | Complete two-root child preflight completed first | Root symlink itself is unlinked; directory tree removal occurs only after every descendant was verified non-symlink and safe; no external target is followed | `test_retire_does_not_follow_symlinked_state_root`, retirement preflight tests |
| Retirement state-file unlink | Credential, enrollment, and trust bundle (`transport_credential.py:488-490`) | State root is a real current-uid `0700` directory; all three final targets and the entire secret tree passed inspection | Each present target must be a regular current-uid single-link file with its exact mode before unlink | `test_retire_rejects_hardlinked_state_file_before_any_deletion`, `test_retire_preflights_directory_credential_before_any_deletion`, normal retirement test |
| Directory fsync | Preflighted/created transport directories | Called only after the directory was validated or created inside the home | Opens the already validated directory; it never selects a new storage path | Crash recovery and commit mode tests |

There is no `applied.json` path in this identity transport-credential slice.
The tests cover persisted on-disk states before each call. They do not claim to
model concurrent path replacement after preflight; directory creation and final
file opens nevertheless use descriptor-relative or no-follow operations where
listed above.

## Dev replay verification — 2026-10-25

The #1785 storage chain was replayed onto dev
`198fc2a2b0a494e3c8c2139cd730f4e2a90b9738` as candidate
`a164ac6146c23c9b1b6c86f8380e8d1d2a3c2803`. The `store.py`,
`transport_storage.py`, and `transport_credential.py` blobs are unchanged from
#1785 source `1d7f5c1b19a15b5eac4231b10f57094103fe3537`, so the call-site
references above still identify the same accesses. In particular,
`transport_credential.py:865-868` calls `read_device_record(..., file_reader=...)`;
`store.py:77-99` remains the single device-record JSON parser. The public facade
also retains dev's MachineBinding exports alongside the transport APIs.

Fresh synthetic validation passed all 59 storage tests and all 102 device tests.
The 12 historical hardlink, Tailcat-ancestor, and subprocess-crash/bundle
selectors were rerun after reproducing their historical behavior failures;
their test/helper AST content is identical across the old snapshots and replay.
See [the node-10 receipt](../notes/zenoh-acl-1785-1850-takeover-2026-10-25/runs/10-storage/20261025-attempt1/receipt.md)
for exact snapshots, commands, outcomes, and hashes. This verification retains
the concurrent-replacement and injected-rollback limitations above; it does not
establish production, remote retirement, or real Zenoh session acceptance.

## Correction after independent quality review — 2026-10-25

The earlier inventory and replay addendum describe source/replay snapshots,
not the corrected implementation below. Independent review of
`1218809020e316eb6cfc0be990ec8221146601bf` reproduced two product failures:

- The original claim that unsafe temporary paths fail before source removal
  was false for `.trust-bundle.<operationId>.tmp`. Its check occurred after the
  staged key was installed; rejection could consume that key and invalidate an
  existing ready credential. Final key/credential/bundle destinations also
  needed whole-set file metadata preflight. The earlier 59/102 green tests did
  not cover this boundary.
- No-follow blocking file opens could hang on an existing `0600` FIFO before
  reaching the descriptor's non-regular-file check. A static FIFO was not
  covered by the earlier type-rejection evidence.

The correction consists of these current call sites (code candidate
`106b09d8339c2e402bb0e74d20473b6f2bba73c1`):

| Boundary | Current enforcement | New behavioral evidence |
| --- | --- | --- |
| Commit roots, source, enrollment and identity | `transport_credential.py:328-356`: roots preflight, safe enrollment/key reads and repeated validation, before any mutation | Existing unsafe-root/source, identity, commit revalidation and crash tests |
| All commit destination/temporary paths | `transport_credential.py:358-369`: inspect the committed key, credential, bundle, credential temporary and bundle temporary as a complete set; `_preflight_file` at `transport_storage.py:304-319` only lstat-checks current uid, regular type, exactly one hardlink and exact `0600`/`0644` mode; absence is allowed | 25 cases: five paths crossed with wrong mode, symlink, hardlink, directory and real FIFO; unchanged full metadata/content inventory, intact staged key, and existing ready state for unsafe temporaries |
| Cleanup/install after the entire set is safe | Credential cleanup at `transport_credential.py:384`, key install at `:389`, bundle write at `:393`, credential install at `:395`; temporary removal at `transport_storage.py:322-326` repeats the metadata check before unlink/fsync | All unsafe cases include another safe temporary sentinel to catch premature cleanup; safe-temporary recovery positive test and four existing subprocess crash checkpoints |
| Transport key/enrollment safe reads | `transport_storage.py:166-189`: `O_RDONLY|O_NOFOLLOW|O_NONBLOCK`, followed by descriptor regular-file/current-uid/single-link/exact-mode checks before content reads | Bounded owned subprocesses with real FIFOs at enrollment, staged key and committed-key recovery paths |
| Tailcat device-record safe reads | `transport_storage.py:192-247`: descriptor-relative no-follow ancestor walk; final `O_RDONLY|O_NOFOLLOW|O_NONBLOCK`, then the same descriptor metadata checks | Bounded owned subprocesses with real FIFOs at device and device-identity JSON paths |

All five FIFO cases require prompt `TRANSPORT_STORAGE_UNSAFE`, clean child exit
and identical inventory; the regression owner kills, waits and reaps any child
that exceeds its stage bound. File types are checked without reading special
files in the inventory helper. Commit tests preserve file hashes, type, mode,
uid/gid, link count, inode and symlink target for every inventory entry.
The single `read_device_record(..., file_reader=...)` JSON parser remains.

Exact RED/GREEN commands, source/test identities, complete current AST call-site
inventory and final verification are in
[attempt2](../notes/zenoh-acl-1785-1850-takeover-2026-10-25/runs/10-storage/20261025-attempt2/receipt.md).
Post-preflight concurrent replacement and independent rollback-failure injection
remain unproven. The corrections do not broaden those guarantees or establish
production/remote/session acceptance. Previous WORM receipts remain unchanged.

## Client replay and binding boundary audit — 2026-10-25

The #1850 client and binding chain is replayed onto accepted storage
`af148fe65eb58f1cbd3e4885007b8190eafe270f`. The client does not add a durable
writer or a product caller. Source searches for `DeviceTransportClient` and
`load_client_tls_material` find definitions and identity public exports only;
there is no daemon/shell/session invocation. The credential module remains the
single M1 writer boundary described above.

| Boundary | Current authority and behavior | Regression evidence |
| --- | --- | --- |
| Binding validation | `transport_credential._validate_certificate_projection` calls `binding.verify_credential_binding`; both response validation and commit reach that certificate projection check | Binding URI/JCS vectors, duplicate/foreign URI and commit revalidation tests |
| Client material reads | `client.mtls.load_client_tls_material` delegates committed-state validation to `read_transport_credential` and reads only the safely opened key, credential and bundle; TLS temporary material is ephemeral outside the credential home | Client material no-write inventory, unsafe key mode and absent commit tests |
| Replay ownership | `client.http.DeviceTransportClient._perform` retains route/body per operation in memory; a canonical `REJECTED` resolves it, while `UNKNOWN` blocks a fresh ID on that route | New rejection/new-operation witnesses for both routes; lost-response and retryable 503 followed by exact replay to rejection |
| Invalid responses | A 3xx retains `TRANSPORT_REDIRECT_REFUSED`, status, operation ID and `retryable=False`; its outcome is now `UNKNOWN`, since it lacks a validated authority result | 301/302/303/307/308 crossed with two noncanonical bodies, fresh-ID refusal and exact replay settlement |
| Previously used operation IDs | Both resolved and unresolved IDs reject changed route/body before opening a connection; unchanged replay remains allowed | Rejected exact replay and body/route mismatch crossed with `UNKNOWN`/`REJECTED` |
| Storage safety inheritance | Whole-set commit preflight and both `O_NONBLOCK` safe-reader implementations from accepted storage remain intact | Full devices regression includes all 31 `transport_safety` cases |

This audit does not establish cross-process UNKNOWN recovery, real authority
deployment, production issuer behavior, or Zenoh session/ACL acceptance.

## Client exception and framing correction — 2026-10-25

Independent quality review of `80df749bfeeba684c8c2707589d9d5f8ca4467e6`
found two omissions in the client regression set. Earlier green evidence did
not establish chain detachment for all typed mappings or strict status-line
reason validation. The old head and attempt1 evidence remain frozen.

`client.errors._public_error_boundary` now protects configuration/material
validation, the client constructor and four request methods, and material
loading. It preserves the same `TransportClientError` object and every public
field after `_perform` restores NOT_SENT replay state, while clearing
`__context__` and `__cause__`. This covers config/CSR Unicode conversion,
connect/timeout/handshake/send/recv, trust/client-chain/temporary-material setup,
and credential/safe-reader/material-encoding conversions. `functools.wraps`
retains callable metadata; control-flow exceptions propagate. This is not a
guarantee about all locals reachable through traceback frames. No M1 safe
reader, credential write, or storage preflight logic changed.

`client.http._parse_response` requires the status-code's following SP and
checks reason bytes before accepting an authority envelope. C0 except HTAB and
DEL produce invalid-response UNKNOWN; legal HTAB/SP/VCHAR/obs-text and an empty
reason with its separator retain canonical rejection semantics. Header-value
ASCII/OWS rules remain unchanged. Both malformed real-loopback cases must block
a fresh ID while retaining an unchanged exact replay to success.

Formal test-only commits and exact RED/GREEN identities are recorded in the
node20 attempt2 receipt. The existing 31 storage-safety and 19 replay/redirect
regressions remain part of the complete devices run; no live-session, remote
authority or durable UNKNOWN guarantee is added.
