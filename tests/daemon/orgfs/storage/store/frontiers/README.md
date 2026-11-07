OrgFS meta frontier cache: implementation and evidence

FROZEN_BASE=89de5d013fbcd28e0521ff25dd7589c5ae36703f
IMPLEMENTATION=506d81aa8b5b8c5493456347466c8dd74fc7f376

All data below comes from disposable synthetic spaces, CPython 3.12.13 and
pycrdt 0.14.9. No production space, daemon, credentials, user configuration,
release branch or counterpart host was read or changed. Raw timing rounds and
both literal membership_json strings per token are in [evidence.json](evidence.json).

**C1 PASS: scope.** Product changes are confined to
`src/hyprial/daemon/impl/orgfs/storage/store/`; all other changes are under
`tests/daemon/orgfs/storage/store/`. Check with
`git diff --name-only 89de5d013fbcd28e0521ff25dd7589c5ae36703f HEAD`.

**C2 PASS: reproduce, then measure the second meta commit twice.**

The first regression run on the frozen base was 2 failed / 2 passed: an owner
append erased unrelated cached tokens, and a simulated 0.5-second maintenance
budget performed 73 apply_update calls inside one pending row. A further RED
test proved that the current token still replayed history after two commits;
the final implementation passes it without invoking the replay routine.

The delete-set workload keeps a live cell between overwritten cells (width 64),
using only 50/100/200 meta commits. Fixture setup defers eager materialization
while preserving invalidation. It primes the current token, makes two ordinary
meta commits, and times the current _membership_at call and the entire commit.
The table shows the second commit for each of two fresh runs. All first-commit
readings are also retained in evidence.json. Milliseconds:

| Meta commits | Run | Before single membership ms | After single membership ms | Before commit ms | After commit ms |
| --- | --- | --- | --- | --- | --- |
| 50 | 1 | 27.770 | 12.728 | 123.487 | 119.287 |
| 100 | 1 | 75.259 | 25.974 | 268.157 | 240.675 |
| 200 | 1 | 241.165 | 56.585 | 647.911 | 496.926 |
| 50 | 2 | 29.804 | 13.916 | 128.116 | 122.037 |
| 100 | 2 | 76.152 | 27.940 | 281.623 | 245.373 |
| 200 | 2 | 238.405 | 54.284 | 647.900 | 509.018 |

The single-call baseline grows superlinearly; the candidate grows approximately
linearly in this bounded workload. The complete commit remains more expensive
than membership alone, as the table explicitly shows.

The separate multiple-token workload has N=M initial meta commits and cached
historical tokens. After each of two new commits it reads the same M tokens.
The next table shows the second round. Membership total includes the current
call plus those historical reads; commit + reads includes invalidation and
validation work too. This avoids attributing shifted work to an improvement:

| M | Run | Before membership total ms | After membership total ms | Before commit + reads ms | After commit + reads ms |
| --- | --- | --- | --- | --- | --- |
| 50 | 1 | 39.791 | 0.794 | 41.090 | 2.253 |
| 100 | 1 | 121.354 | 1.569 | 122.946 | 3.201 |
| 200 | 1 | 445.746 | 3.437 | 448.415 | 5.468 |
| 50 | 2 | 39.047 | 0.770 | 40.469 | 2.183 |
| 100 | 2 | 124.700 | 1.683 | 126.396 | 3.566 |
| 200 | 2 | 448.647 | 3.442 | 451.505 | 5.529 |

Reproduce either curve offline in a checkout with dependencies already present:

```sh
UV_CACHE_DIR=/tmp/orgfs-uv uv run --offline --python 3.12 python tests/daemon/orgfs/storage/store/frontiers/probe_delete_sets.py
UV_CACHE_DIR=/tmp/orgfs-uv uv run --offline --python 3.12 python tests/daemon/orgfs/storage/store/frontiers/benchmark.py
```

Run the same script with `PYTHONPATH=<frozen-checkout>/src` for the baseline.
No 50,000-commit experiment was used.

The counterpart probe is [probe_scale.py](probe_scale.py). It creates exactly N
initial meta commits and M distinct owner writers with one cached token per
writer, in a fresh HYPRIAL_HOME/state. It prints N/M/commits/elapsed_ms,
current_recompute_ms, full commit/read timings, and an explicit arrival interval:

```sh
UV_CACHE_DIR=/tmp/orgfs-uv uv run --offline --python 3.12 python tests/daemon/orgfs/storage/store/frontiers/probe_scale.py --sizes 100 300 668 --writers 8 --arrival-seconds 90
```

Local second-round smoke: N=100,M=8 current 3.453 -> 0.601 ms, full path
36.474 -> 5.143 ms; N=300,M=8 current 9.152 -> 0.748 ms, full path
90.926 -> 8.046 ms. N=668 on the counterpart is UNKNOWN, pending its own run.

The dispatch supplied a production recompute range of 60-93 seconds and an
arrival interval of about 90 seconds (about 0.0111 envelopes/second), a ratio of 0.67-1.03. The upper end
exceeds the interval; that range alone does not prove every occurrence loses
ground. The probe's 90-second interval is an input, not a locally measured
arrival rate. Recovery of the live incident remains UNKNOWN.

**C3 PASS: same space, same journal, same token, equal membership_json.**

Frozen code created one synthetic space with 31 meta commits (owner changes
and member suffixes) and captured 25 tokens. The candidate reopened that same
space and compared the literal persisted JSON strings. 25/25 are equal.
The SHA256 of ordered immutable journal envelopes stayed
`244a2a2161067d5e35ab2d9678845c3de728f192976ed0fba0f9fd8fe53a4834`.
The table uses SHA256 prefixes for readability; evidence.json contains both
complete strings, every full token, and the equality booleans:

| Token | Membership SHA256 prefix before | After | Equal |
| --- | --- | --- | --- |
| `Abik25mSopQKDQ==` | `105c974c5df4` | `105c974c5df4` | True |
| `Abik25mSopQKEQ==` | `044f1591f76f` | `044f1591f76f` | True |
| `Abik25mSopQKFQ==` | `9678e31f5809` | `9678e31f5809` | True |
| `Abik25mSopQKGQ==` | `1b59d1294c1d` | `1b59d1294c1d` | True |
| `Abik25mSopQKHQ==` | `f82a1c3a7be1` | `f82a1c3a7be1` | True |
| `Abik25mSopQKIQ==` | `10e8e0966e02` | `10e8e0966e02` | True |
| `Abik25mSopQKJQ==` | `950984a0d4b3` | `950984a0d4b3` | True |
| `Abik25mSopQKKQ==` | `12c2075fb47a` | `12c2075fb47a` | True |
| `Abik25mSopQKLQ==` | `869b4cc79629` | `869b4cc79629` | True |
| `Abik25mSopQKMQ==` | `0e4b34de1f13` | `0e4b34de1f13` | True |
| `Abik25mSopQKNQ==` | `44a100615654` | `44a100615654` | True |
| `Abik25mSopQKOQ==` | `559cbc2f967d` | `559cbc2f967d` | True |
| `Abik25mSopQKPQ==` | `91f266e7cb69` | `91f266e7cb69` | True |
| `Abik25mSopQKQQ==` | `287ed1b2ecea` | `287ed1b2ecea` | True |
| `Abik25mSopQKRQ==` | `89f08561cd17` | `89f08561cd17` | True |
| `Abik25mSopQKSQ==` | `dd5408a333f9` | `dd5408a333f9` | True |
| `Abik25mSopQKTQ==` | `fe4a97bef192` | `fe4a97bef192` | True |
| `Abik25mSopQKUQ==` | `2dbe5fe24add` | `2dbe5fe24add` | True |
| `Abik25mSopQKVQ==` | `1d2f907daf94` | `1d2f907daf94` | True |
| `Abik25mSopQKWQ==` | `466ce660bcd7` | `466ce660bcd7` | True |
| `Abik25mSopQKXQ==` | `7bfa7dfbbd76` | `7bfa7dfbbd76` | True |
| `Abik25mSopQKYQ==` | `3f40fef7e131` | `3f40fef7e131` | True |
| `Abik25mSopQKZQ==` | `24e380a41fbb` | `24e380a41fbb` | True |
| `Abik25mSopQKaQ==` | `b13635ecb012` | `b13635ecb012` | True |
| `Abik25mSopQKbQ==` | `2d8f9d05fb1b` | `2d8f9d05fb1b` | True |


Recreate a new isolated fixture with the frozen implementation, then compare
with the candidate, retaining the same directory:

```sh
PYTHONPATH=<frozen-checkout>/src UV_CACHE_DIR=/tmp/orgfs-uv uv run --offline --python 3.12 python tests/daemon/orgfs/storage/store/frontiers/benchmark.py --capture /tmp/orgfs-json-comparison
UV_CACHE_DIR=/tmp/orgfs-uv uv run --offline --python 3.12 python tests/daemon/orgfs/storage/store/frontiers/benchmark.py --compare /tmp/orgfs-json-comparison
```

The mixed-writer regression additionally compares every retained token against
the original independent replay algorithm after each suffix. Private baseline
mutation, a missing incremental prefix, restart before invalidation, expired
pending rows, and budget resumption each have behavioral regressions.

**C4 PASS: member suffix negative controls.**

`test_member_suffix_invalidates_old_and_current_owner_tokens` imports a member
suffix based on an older owner frontier without advancing the receiver's owner
projection. The old cached row disappears and its lazy reconstruction equals a
forced journal rebuild, including meta_update bytes. A second control proves a
member suffix may affect a token older than its stamped owner frontier; a pure
state-vector filter would miss that effect. Budgeted invalidation never returns
a stale row and retains already applied drain notifications.

**C5: measured verification and limits.**

- Focused final run: `uv run --python 3.12 pytest tests/daemon/orgfs/storage/store/frontiers tests/daemon/orgfs/storage/store/pending_replay tests/daemon/orgfs/storage/store/test_member_add_frontier_defer.py -q`: rc=0, 18 passed, 5.93 s.
- Entire OrgFS subtree: `uv run --python 3.12 pytest tests/daemon/orgfs/ -q -n 4 --dist worksteal --tb=short`: rc=1, 776 passed, 3 xfailed, 1 failed, 80.53 s. The xfails are reported separately, not counted as passes.
- Failure: `test_fs_web_check_cli` invokes `org fs space web --check` with synthetic TLS files; expected exit 0 and ok, actual `RuntimeError: Could not get a command for this Typer instance`. The exact test fails the same way on the frozen base and candidate. Its CLI code is outside this task's whitelist.
- Structural limits, module boundaries, `uv run ruff check .`, `git diff --check`, and strict added-comment check: PASS.
- FULL_SUITE_STATUS: FAIL, rc=1, 45 failed, 18417 passed, 39 skipped, 7 xfailed, 2 xpassed, 24 warnings, 2 errors in 1574.61s (0:26:14). 18,510 unique test IDs; 18,512 JUnit records include two teardown error reports. Proxy variables were removed and a short isolated AF_UNIX basetemp was used; skipped/xfail/xpass outcomes are separate from passes. Full failure attribution is in [validation.json](validation.json). Unrestricted validation is NOT_RUN because the escalation request was denied.
- Remote CI at daecf37d5d8887c56256ae21cdbc7b253b512ef7: BLOCKED. [Run 39677 / pr-hygiene](https://code.hyprial.com/HyprialOS/harness-bridge/actions/runs/39677/jobs/2) passed Node policy tests, then uv failed fetching pytest with `failed to lookup address information: nodename nor servname provided, or not known`. The four pytest shards were skipped after that prerequisite failed. The same merge tree's pytest pair passed locally: 237 passed in 0.39 s. This is not a remote CI pass. Production/counterpart execution: NOT_RUN by task scope.


Full-suite comparison: the 45 failed test IDs were rerun on the frozen base
with the same interpreter/dependencies and sandbox. That run produced 41 failed,
4 passed and 1 teardown error in 38.15 s. The four initially passing controls
were then interleaved base -> candidate -> base -> candidate: 4 passed;
4 passed; 1 failed / 3 passed; 4 passed. The base's intermittent failure was
`test_stalled_renewal_fences_lease_at_existing_stale_threshold`.

Thus 42/45 primary failure IDs also failed on the frozen base at least once.
The two drained-read-back tests and the sampler-cap test passed both targeted
candidate runs and both targeted base runs but failed in the full candidate
suite. Their full-suite mechanisms remain UNKNOWN, including the drained
test's extra teardown error. Shared failing IDs do not establish complete
causal attribution. The full suite remains FAIL, not a release/product pass.
`validation.json` retains every control outcome and error message.

Full-suite failures (raw pytest outcomes retained):

| Test | Attribution | First error |
| --- | --- | --- |
| `tests/daemon/upgrade/test_steps.py::test_tmp_or_var_symlink_dotdot_cancellation_is_refused[/var/tmp]` | BLOCKED(sandbox) | PermissionError: [Errno 1] Operation not permitted: '/var/tmp/hf-home-cancel-hejdf5_h' |
| `tests/daemon/ipc/error/test_ipc_error_code_registry.py::test_no_literal_proto_code_outside_the_registry` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | AssertionError: proto-facing error codes must come from hyprial.contracts.ipc_errors, never a string literal: |
| `tests/daemon/links/composition/test_drained_read_back.py::test_r23_a_drained_request_that_was_never_injected_is_not_readable` | FAIL(UNACCOUNTED); full-suite only, targeted base/candidate pass | hyprial.kernel.impl.contracts.daemon.ipc_errors.DaemonRequestError: domain command session:register:35ce5054deb444d0b246cb6e75e21e09 did not settle |
| `tests/daemon/links/composition/test_drained_read_back.py::test_r23_a_drained_request_that_was_never_injected_is_not_readable` | FAIL(UNACCOUNTED); full-suite only, targeted base/candidate pass | failed on teardown with "RuntimeError: daemon log writer did not drain" |
| `tests/daemon/links/composition/test_drained_read_back.py::test_r23_a_processed_settled_row_stays_readable` | FAIL(UNACCOUNTED); full-suite only, targeted base/candidate pass | hyprial.kernel.impl.contracts.daemon.ipc_errors.DaemonRequestError: domain command session:register:909bb4f64ae148ca9c89c206d467c20f did not settle |
| `tests/integration/hosted/readiness/test_hosted_readiness_timing.py::test_spawn_error_is_recorded_and_reraised` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | subprocess.SubprocessError: Exception occurred in preexec_fn. |
| `tests/integration/hosted/readiness/test_hosted_start_probe.py::test_sampler_stops_at_cap` | FAIL(UNACCOUNTED); full-suite only, targeted base/candidate pass | AssertionError: assert ([{'hookCalls': {'get_data': 63, 'read_text': 0}, 'inflight': [], 'kind': 'sample', 'loadavg': [9.40087890625, 9.998046... ...}, {'hookCalls': {'get_data': 63, 'read_text': 0}, 'inflight': [], 'kin |
| `tests/integration/hosted/runtime/test_hosted_suite_precompile.py::test_probe_without_bytecode_reports_source_compiles` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | AssertionError: assert 'reap-failed' == 'ok' |
| `tests/integration/hosted/runtime/test_hosted_suite_precompile.py::test_warm_tree_makes_the_daemon_path_adopt_bytecode` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | AssertionError: assert ('reap-failed' == 'ok' |
| `tests/integration/hosted/runtime/test_hosted_suite_precompile.py::test_stale_bytecode_is_detected_as_source_compile` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | KeyError: 'bootstrap' |
| `tests/integration/hosted/runtime/test_hosted_suite_precompile.py::test_wrong_magic_bytecode_is_detected_as_source_compile` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | KeyError: 'cachePresent' |
| `tests/integration/hosted/runtime/test_hosted_suite_precompile.py::test_runtime_compile_of_a_module_file_is_not_a_source_fallback` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | KeyError: 'import' |
| `tests/integration/hosted/runtime/test_hosted_suite_precompile.py::test_inherited_cache_prefix_and_optimize_do_not_leak` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | KeyError: 'import' |
| `tests/integration/hosted/runtime/test_hosted_suite_precompile.py::test_run_bounded_timeout_kills_the_whole_group` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | AssertionError: assert ('reap-failed' == 'timeout' |
| `tests/integration/hosted/runtime/test_hosted_suite_precompile.py::test_run_bounded_spawn_error_and_nonzero` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | AssertionError: assert ('reap-failed' == 'nonzero' |
| `tests/integration/hosted/runtime/test_hosted_suite_precompile.py::test_compile_timeout_is_recorded_and_not_effective` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | AssertionError: assert ('reap-failed' == 'timeout' |
| `tests/integration/hosted/runtime/test_hosted_suite_precompile.py::test_warm_trees_writes_evidence_with_verdict` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | AssertionError: assert {'effective':...unconfirmed']} == {'effective':...problems': []} |
| `tests/integration/hosted/runtime/test_hosted_suite_precompile.py::test_modules_imported_before_the_audit_hook_are_not_counted_as_bytecode` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | AssertionError: assert 'reap-failed' == 'ok' |
| `tests/daemon/channels/identity_cache/test_native.py::test_real_owned_births_keep_full_markers_without_repeated_ps` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | AssertionError: assert ('darwin-starttime:1793945339:353018;darwin-startsec:1793945339' is not None and 'darwin-starttime:' in 'darwin-starttime:1793945339:353018;darwin-startsec:1793945339' and ';ps-lstart:' in 'darwin- |
| `tests/daemon/channels/mcp/lifecycle/supersession/handoff/test_handoff.py::test_orphaned_channel_subprocess_exits_despite_inherited_stdin` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | PermissionError: [Errno 1] Operation not permitted: 'ps' |
| `tests/daemon/channels/mcp/lifecycle/supersession/handoff/test_handoff.py::test_orphaned_channel_subprocess_exits_despite_inherited_stdin` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | failed on teardown with "AssertionError: test leaked 1 file descriptor(s): |
| `tests/shell/cli/surface/test_cli_surface_snapshot.py::test_cli_surface_matches_committed_snapshot` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | AssertionError: CLI command surface differs from contract/cli/surface.json. |
| `tests/integration/request_http/test_gui_reference.py::test_shared_app_against_live_and_contract_mock` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | AssertionError: BLOCKED: DEV01_CHROMIUM_EXECUTABLE required |
| `tests/daemon/harnesses/codex/native_home/home_contracts/test_fresh_launch_validation.py::test_real_codex_validates_fresh_daemon_agent_home[openai]` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | ConnectionError: Codex app-server exited before its Unix socket became ready |
| `tests/daemon/harnesses/codex/native_home/home_contracts/test_fresh_launch_validation.py::test_real_codex_validates_fresh_daemon_agent_home[deepseek]` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | ConnectionError: Codex app-server exited before its Unix socket became ready |
| `tests/daemon/harnesses/codex/native_home/home_contracts/test_home_contracts.py::test_t04_t05_t07_t13_real_codex_load_and_resume_failure` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | ConnectionError: Codex app-server exited before its Unix socket became ready |
| `tests/integration/transfer/p2/e2e/test_codex_two_daemon_chain.py::test_codex_chain_lands_on_the_target_and_retires_the_source` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | AssertionError: daemon rejected transfer.receive: {'code': 'SANDBOX_SMOKE_FAILED', 'message': 'sandbox-smoke-failed: codex: the target refused the sandbox smoke: codex sandbox smoke exited 71: sandbox-exec: sandbox_apply |
| `tests/integration/transfer/p2/e2e/test_codex_two_daemon_chain.py::test_codex_tampered_bundle_refuses_and_leaves_the_source_held` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | AssertionError: assert 'SANDBOX_SMOKE_FAILED' == 'TRANSFER_LANDING_FAILED' |
| `tests/integration/transfer/p2/e2e/test_codex_two_daemon_chain.py::test_codex_w2_w3_the_export_boundary_re_enters_after_the_first_step` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | AssertionError: daemon rejected transfer.receive: {'code': 'SANDBOX_SMOKE_FAILED', 'message': 'sandbox-smoke-failed: codex: the target refused the sandbox smoke: codex sandbox smoke exited 71: sandbox-exec: sandbox_apply |
| `tests/integration/transfer/p2/e2e/test_codex_two_daemon_chain.py::test_codex_w5_after_the_target_answered_both_daemons_restart_and_the_report_holds` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | AssertionError: daemon rejected transfer.receive: {'code': 'SANDBOX_SMOKE_FAILED', 'message': 'sandbox-smoke-failed: codex: the target refused the sandbox smoke: codex sandbox smoke exited 71: sandbox-exec: sandbox_apply |
| `tests/integration/transfer/p2/e2e/test_codex_two_daemon_chain.py::test_codex_w6_before_the_complete_the_chain_closes_on_the_report` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | AssertionError: daemon rejected transfer.receive: {'code': 'SANDBOX_SMOKE_FAILED', 'message': 'sandbox-smoke-failed: codex: the target refused the sandbox smoke: codex sandbox smoke exited 71: sandbox-exec: sandbox_apply |
| `tests/shell/squire/setup/test_squire_setup_scaffold.py::test_setup_scaffolds_template_and_preserves_every_file_byte_for_byte` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | ValueError: --machine 'mac-studio' does not match the daemon node id HYPRIAL_NODE_ID='node-hq-01.example-tailnet.ts.net'; user delivery is owned by the profile's preferredReceiver.machine and would never be claimed by this da |
| `tests/shell/squire/setup/test_squire_setup_scaffold.py::test_setup_does_not_overwrite_a_custom_skill` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | ValueError: --machine 'mac-studio' does not match the daemon node id HYPRIAL_NODE_ID='node-hq-01.example-tailnet.ts.net'; user delivery is owned by the profile's preferredReceiver.machine and would never be claimed by this da |
| `tests/shell/squire/setup/test_squire_setup_scaffold.py::test_setup_creates_agent_and_pins_only_once_through_registry_port` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | ValueError: --machine 'mac-studio' does not match the daemon node id HYPRIAL_NODE_ID='node-hq-01.example-tailnet.ts.net'; user delivery is owned by the profile's preferredReceiver.machine and would never be claimed by this da |
| `tests/shell/squire/setup/test_squire_setup_scaffold.py::test_setup_without_adapter_returns_explicit_deferred_onboarding` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | ValueError: --machine 'mac-studio' does not match the daemon node id HYPRIAL_NODE_ID='node-hq-01.example-tailnet.ts.net'; user delivery is owned by the profile's preferredReceiver.machine and would never be claimed by this da |
| `tests/shell/squire/setup/test_squire_setup_scaffold.py::test_setup_start_uses_configured_harness_model_provider_and_skill` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | ValueError: --machine 'mac-studio' does not match the daemon node id HYPRIAL_NODE_ID='node-hq-01.example-tailnet.ts.net'; user delivery is owned by the profile's preferredReceiver.machine and would never be claimed by this da |
| `tests/tooling/ci/acquisition/test_ci_dependency_caches.py::test_real_tree_denominator_and_family_witnesses` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | assert not ['unaccounted current image contexts'] |
| `tests/tooling/ci/execution/test_ci_conditions.py::test_reader_emits_exactly_one_platform_scoped_line` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | AssertionError: assert 'unavailable' != 'unavailable' |
| `tests/integration/transfer/runtime/test_transfer_sandbox_smoke.py::test_a_real_codex_run_passes_or_is_explicitly_skipped` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | AssertionError: codex sandbox smoke exited 71: sandbox-exec: sandbox_apply: Operation not permitted |
| `tests/tooling/ci/hygiene/test_pr_hygiene_git.py::test_git_env_scopes_the_token_to_one_hardened_command` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | AssertionError: assert 'warning: ign...CONFIG_HOME\n' == 'warning: ign...CONFIG_HOME\n' |
| `tests/daemon/home/test_home_lease_actor.py::test_stalled_renewal_fences_lease_at_existing_stale_threshold` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | assert False |
| `tests/kernel/processes/test_owned_process.py::test_darwin_native_birth_identity_matches_ps_without_forking[self]` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | PermissionError: [Errno 1] Operation not permitted: 'ps' |
| `tests/kernel/processes/test_owned_process.py::test_darwin_native_birth_identity_matches_ps_without_forking[child]` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | PermissionError: [Errno 1] Operation not permitted: 'ps' |
| `tests/tooling/containers/test_worker_image_build_context.py::test_the_context_really_carries_every_copy_source` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | AssertionError: build context rejects a COPY source: |
| `tests/tooling/release/homepage/test_public_changelog_sync.py::test_the_candidate_token_travels_only_in_the_hardened_child_git_environment` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | AssertionError: assert 'warning: ign...CONFIG_HOME\n' == 'warning: ign...CONFIG_HOME\n' |
| `tests/tooling/release/publish/github/test_publication.py::test_publish_token_never_reaches_the_log` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | AssertionError: assert 'ignored inh... environment' not in 'warning: ig... GIT_PAGER\n' |
| `tests/tooling/release/publish/github/test_publication.py::test_publish_names_ignored_parent_git_variables_without_values` | FAIL(COUNTEREXAMPLE); same test also fails on frozen base | AssertionError: assert 0 == 2 |

**C6 PASS: exact invalidation and yielding mechanism.**

`StoreSnapshots._frontier_invalidations` checks only journal rows after a durable
cache watermark. Owner candidates obey the same before/target coverage and
trial-frontier limits as the canonical replay. Member suffixes are applied as
CRDT updates, including delete sets and earlier member dependencies. A row is
deleted by token only if its materialized baseline changes. Uncovered owner
tokens do not load large snapshots. Token deletions and watermark advancement
commit together; a missing watermark verifies legacy rows by full replay.

`StoreMembership._replay_membership` may seed one private native baseline only
when every journal row was selected/accepted. Its pending CRDT dependencies
remain in that native document. `_advance_meta_baseline` accepts only the next
committed meta append; a count/rowid gap discards the cache. Public results get
independent serialized copies. The current owner token can thus reuse a proven
journal prefix without deriving authority from the live projection or a state
vector alone. Older tokens still use the canonical replay when required.

`expire_pending`'s existing per-space 5-second monotonic deadline now reaches
`_resume_meta_work`, the invalidation loop and individual replay checkpoints.
Exceeding it suspends work, returns the maintenance turn, and resumes only at
the same immutable journal head. It neither negative-memoizes unfinished work
nor drops an aged row. A changed head resets the continuation. Already durable
imports retain their drain notifications even if cache work is deferred.

The budget is cooperative between Python/native operations. One pycrdt apply,
merge, serialization or SQLite operation cannot be interrupted by this patch;
this is not a hard wall-clock bound on a single native call. A cold restart or
unaccounted multi-row prefix may still require one authoritative replay. The
additional native Doc is a bounded derived cache, not another durable authority.

Skill-Impact: not-needed: internal store caching and maintenance only; agent operations unchanged.
Contract-Impact: internal derived SQLite cache and scheduling only; no published JSON keys, errors or event semantics change.
