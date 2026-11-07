# Pi P2 settings and projection recovery

P2 uses the explicit config source C as the restart configuration. The controlled
Pi loader reads settings from its frozen native projection and the trusted
project. Pi's settings setters change only in-memory preferences. Reload and
session replacement read the configured inputs again. To persist a preference
across starts, change C through the supported config workflow, not Pi's settings
UI. Session transcript model/thinking entries are unchanged by this rule.

## Recognize the old-loader defect

The old controlled loader gave Pi a file-backed settings manager inside the
frozen projection. A Pi setter could create an unplanned `settings.json` at
0644, or change the contents of a seeded 0600 file. The resolver then reports:

- `native projection item settings.json mode must be 0600`;
- after chmod, `native projection drifted after publication: settings.json`.

The publication manifest does not record mode. Chmod cannot fix an extra file
or changed content. `ensure-home` validates home custody; it does not repair
projections. Start still refuses drift after the loader upgrade.

Readiness now validates the existing projection for the current source revision.
Its `config` member is `blocked` for invalid modes, links, missing/extra files or
changed content, even when the agent is stopped. Pi permissions introspection
can still be `unknown`; inspect the individual checks.

## Explicit operator recovery

Use the upgraded daemon and CLI. Do not run this procedure as an automatic
startup action. An authorized operator must stop the affected agent and clear
its durable desired launch state first. Rebind refuses an active or uncertain
custody state with `AGENT_CONFIG_REBIND_NOT_QUIESCENT`.

1. Identify the agent's current **explicit C path** from its registered config.
   Do not use the corrupt `state/config/.../native/pi` projection as the source.
   Preserve the old projection for diagnosis.
2. Ensure C is complete, valid, and private: directories 0700, files 0600,
   no links, credentials, or unmapped files. If it does not meet staging rules,
   prepare and review a separate private staging tree from C. Do not silently
   accept content that exists only in the corrupt projection.
3. Preview using the exact source selected in step 2:

   ```sh
   hyprial agent config rebind <name> --source <absolute-C-or-staging> --dry-run --json
   ```

   Inspect `projectionChanges`. Each row names `harness`, relative `path`,
   `changes`, and `action`. `publish-from-staging` replaces that input in the
   new projection; `omit-from-new-projection` drops an unexpected file from the
   new projection. `uninspectable` or `current-source-invalid` means the old
   projection could not be fully compared, not that it is safe. Inspect such
   evidence before approving the rebind.
4. Apply the same explicit source after review:

   ```sh
   hyprial agent config rebind <name> --source <absolute-C-or-staging> --json
   ```

   Keep the `receiptId`. The identity owner atomically publishes a new C root
   and changes the binding under quiescence and CAS fences. It does not overwrite
   the corrupt projection. The returned differences are the recorded pre-rebind
   observation; a dry run does not reserve or authorize a later apply.
5. Start the agent normally with the upgraded loader. The resolver publishes
   a fresh private native projection from the new C root. Re-run readiness and
   check that the `config` member passes. This is not proof of provider access.

Rollback remains `hyprial agent config rebind <name> --rollback <receiptId>`.
It restores the old binding, so an old corrupt projection can block start again.

For `cloudflare-worker-manager`, infra-ops must perform these steps after the
upgrade using its actual registered C and approved launch arguments. This change
never inspects or repairs that production home automatically.
