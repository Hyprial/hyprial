# Released DSH history compatibility

## Subagent descriptors

DSH 0.1.5-rc.2 rejects released descriptor version 2 records during Session migration,
then cannot classify them during cold continuation. Descriptor version 3 adds only
an optional `agentReasoningEffort` field. This patch supports descriptor version 2
while retaining the current released-event payload semantic checks: for example,
`agentProvider` and `agentModel` must appear together. The supported historical
exception is an explicitly empty version 2 `persona`, which the released producer
accepted and persisted as a string. Its empty value is preserved; non-string
personas remain invalid and version 3 validation is unchanged. It does not infer a missing
route or admit unknown fields. It keeps the recorded payload unchanged and
normalizes a detached descriptor in memory. Version 2 records containing the
newer field remain invalid; unknown versions are not silently upgraded. No user
histories are rewritten by this installer.

`upstream.patch` records the source change against upstream tag `dsh-v0.1.5-rc.2`
(`fb2c4b9e698e30edb738bca4cf0618587db7d203`). The original MIT license is included.
`patch.json` pins the versions and before/after SHA-256 of the three descriptor
runtime files, including both subagent bundle and internal descriptor entry.

The release verifier applies this patch only to a freshly installed candidate,
after checking its lockfile and before browser validation or runtime promotion.
It validates all input files before writing; reapplication is idempotent. A new
upstream package version or unexpected source hash fails the release check and
requires review. Do not edit a running installation to bypass that check.

The real published-package regression exercises descriptor v2/v3 migration and
folding, records rejected by the current payload rules (including malformed or
unknown versions and unpaired routes), unchanged source payloads, and both module
entry points. It runs in `verify-dsh-latest.mjs`, including installation-time
verification. Additional source tests cover the complete migration and subagent
suites. A private copy of a real historical compressed session was also migrated
successfully; original-file checksums were unchanged. No session prompts or resumes
are part of the tests.

When upstream ships the fix, remove this patch after updating the GUI runtime
lock and retaining the history regression in release verification.

## Durable message occurrences (manifest version 2)

`@deepseek-ai/dsh-client-ui-chat@0.1.5-rc.2/lib/client.js` used transport
`data.id` alone as the `input-message` start key. Re-delivery at another durable
sequence could throw a duplicate-start error and abort an older history page.
The matcher now uses `JSON.stringify([String(event.data.id), event.seq])`:
collision-safe tuple encoding distinguishes occurrences without changing business
`data.id`, content, or steering claim correlation. Same-sequence replay remains
idempotent through the actual assembler's sequence deduplication. Stored history
and transport identities are not rewritten.

The [clean published artifact](https://registry.npmmirror.com/@deepseek-ai/dsh-client-ui-chat/-/dsh-client-ui-chat-0.1.5-rc.2.tgz)
was byte-for-byte equal to the previously hotpatched runtime bundle after
reversing **only** the exact two-comment patch beginning `A durable occurrence
is distinct...` / `Re-delivery may append...` and its ID expression. SHA-256:

- Original: `6527556d25c7b2a27f2e3bfb96cc2ae3d9244f1213147e245b17684ad8d9e396`
- Patched: `adcdbfda652968b65a743d4b646b99cc4656dfd4cb74e1bcd8ab8188cf75f72b`

`message-occurrence.patch` records the matching TypeScript source change against
upstream tag `dsh-v0.1.5-rc.2` (`fb2c4b9e698e30edb738bca4cf0618587db7d203`),
validated with `git apply --check` on that tag's original source.

The existing isolated-candidate patch mechanism and strict guards are unchanged.
`verify-dsh-latest.mjs` already calls `verifyDshHistoryCompatibility` before its
browser/release checks; that verifier now calls `verifyDshHistoryOccurrences`.
It extracts bounded named regions from the candidate's **actual** chat matcher
and conversation assembler bundles, without importing the browser application.
Missing regions fail closed rather than substituting an imitation assembler.
Synthetic fixtures cover replace/prepend/append with repeated IDs, ordinary input
after repeats, unchanged steering identity, and replay of the same sequence.
No production session is read.

History-only verification (no servers started):

```sh
node scripts/verify-dsh-history.mjs /path/to/already-patched-candidate-runtime
DSH_HISTORY_RUNTIME=/path/to/audited-runtime node --test tests/dsh-history-compat.test.mjs tests/dsh-history-occurrences.test.mjs
```

The manifest integration test operates on disposable copies and checks late-file
drift rejection leaves earlier files unchanged. Runtime-dependent unit tests are
optional for portability; real-runtime occurrence checks always run in the
existing release verifier.
