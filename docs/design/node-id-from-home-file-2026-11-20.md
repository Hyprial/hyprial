# Node id from a home file (2026-11-20)

Owner decision (Allen, 2026-11-20). This note covers the implementation; the decision itself is final.

## Rule

- The node id comes from `HYPRIAL_NODE_ID` when it is set and not blank. Otherwise it comes from
  the home's node-id file. The host name is never read for identity.
- Every reader goes through `resolve_node_id` in
  `src/hyprial/kernel/impl/primitives/node_identity.py`.

## File

- Location: `<HYPRIAL_HOME>/node-id`, so `~/.hyprial/node-id` by default.
  The home is the unit that owns stored addresses, so the id lives with it rather than in `state/`.
  `HARNESS_STATE_DIR` can point outside the home.
- Format: one line of UTF-8 text. The reader strips surrounding whitespace. A missing file and an
  empty file are both refused.
- A new home gets `d.` followed by 32 lowercase hex characters (`secrets.token_hex(16)`).
  This is exactly the existing device-id address segment (`_DEVICE_ID_SEGMENT` in `kernel/impl/primitives/uri.py`),
  which is also the host shape `identity/impl/authorization/ports.py` already requires.
- An existing install's file holds whatever string it already used, which today is its host name.
  From then on that string is opaque and is never compared with the real host name.

## Mint point

- Only one: the kernel's `initialize_hyprial_home`, which `hyprial init` uses. `hyprial login` also
  uses it when the home directory does not exist yet.
- It mints only when the home is new: the directory did not exist, or existed and was empty.
  Re-running `hyprial init` on an existing home never mints. If it did, a home without the file would
  silently get a new node id, and every address already stored would point at a node that no longer exists.
- Writing uses exclusive create, so an existing file is never replaced.
- Nothing else mints: not the daemon, not the GUI, not the container entrypoints.
  The containers set `HYPRIAL_NODE_ID`, and a test home gets its file through the same `mint_node_id`.

## Failure behaviour

- `resolve_node_id` raises `NodeIdNotConfigured`. It is a `ValueError`, carries the code
  `NODE_ID_NOT_CONFIGURED` and the data `{path, guide}`, and the message names the migration guide.
- The code is an ordinary class attribute, like `HYPRIAL_HOME_NOT_INITIALIZED`. It is not a registered
  wire code and not a FROZEN entry. The CLI renders it through `json_failure`'s generic `code` + `data` path.
- `hyprial daemon start` fails before spawning anything, because it resolves the node id for the forwarding environment.
- `hyprial daemon run` fails in `DaemonApplication.from_environment`. The ownerless bootstrap daemon
  fails in its dispatcher.
- CLI commands that need the node id without a daemon fail the same way: offline `adapter remove`,
  the derived machine and cross-check in `squire setup`, `login` device provisioning, and interactive
  `agent start`. None of them falls back to the host name.
- GUI admission already maps `ValueError` from `resolve_node_id` to `GUI_IDENTITY_INVALID`.
- If `HYPRIAL_NODE_ID` is set and the file is missing, everything starts, because the override wins.

## Override kept: `HYPRIAL_NODE_ID`

It is kept because deployments and tests rely on it:

- Both docker entrypoints set it: `contract/e2e/docker/entrypoint.sh` and `scripts/build/docker/gui-debug-container-entrypoint.sh`.
- The two-machine image and `packaging/clients/verify_runtime.py` set it.
- The e2e safety guard in `contract/e2e/_daemon_runner.py` keys on it.
- The daemon passes it to managed workers (`worker_channel.py`) and to GUI children (`gui/backend/source.py`, `gui/host/config.py`).
- About 60 test files set it.

A blank value is ignored with one warning, and the file is read instead.

## Deliberately not handled

- No conversion, migration or fallback code. Stored data is not rewritten.
- Copied homes: two machines with the same file share one node id. Nothing detects this.
- The node id and the tailcat `deviceUid` (`daemon/impl/network/tailcat/keys.py`, a UUID4 hex in
  `device.json`) are not merged. On a new home the node segment is `d.<hex>` and `deviceUid` is a
  different 32-hex value: same shape, different value. Nothing derives one from the other.
- The host name is still read for display and fingerprinting, which this change leaves alone:
  - the tailcat device display name;
  - the `machine_binding.py` fallback fingerprint;
  - the pending-restart and startup log `host` field.

## Touched call sites

All of these go through `resolve_node_id`. None of the callers changed; only the source behind it did.

- `daemon/impl/application/__init__.py` (`from_environment`)
- `daemon/impl/application/onboarding/bootstrap/dispatcher.py`
- `daemon/impl/application/onboarding/provisioning.py` (device id for a new device record)
- `daemon/impl/bootstrap/adapter_registration.py` (offline remove)
- `daemon/impl/pac/views/context.py`
- `daemon/impl/agent_templates/setup.py` (`node_machine_id`, `_cross_checks`)
- `shell/impl/cli/commands/common/daemon_start.py`
- `shell/impl/cli/commands/login/flow.py`
- `shell/impl/cli/commands/agent/runtime/interactive.py`
- `shell/impl/gui/backend/admission.py`

Changed in product code:

- `node_identity.py`: reads the file, mints, defines the error.
- `kernel/impl/configuration/home.py`: the mint point.
- `identity/impl/users/profile.py`: `machine` and `machineKey` now accept an exact lowercase device id.
  Before this, a minted node id was refused as an "identity id shape". All other shapes, and
  uppercase variants, are still refused.
- Help text and docstrings that said "else hostname".

## For the org line and the cell-model line

- Existing installs keep their node value byte for byte, because the operator writes the old string
  into the file. Agent URIs `agent:<owner>:<node>:a.<id>`, cell members, org receiver records and
  stored addresses therefore keep working unchanged.
- Mixed-version machines are unaffected. An old peer still reads the host name, a new peer reads the
  same string from the file, so the wire value is identical.
- Only new homes produce `d.<hex>` node segments. Code that parses or validates a node segment must
  accept a device-id shape. The user-profile validator was the only such check found, and it is fixed here.
- Upgrade step for operators: `docs/guides/migration-im-push-by-agent-binding.md`, section 13.
