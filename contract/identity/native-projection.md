# P2 native projection privacy and Pi settings

Publication and validation use the same required regular-file mode (0600).
Directories remain 0700. Source modes are not copied into native projections.
Manifest items contain content digests and sizes; they do not contain modes.
Changing mode alone cannot cause a content-drift error after 0600 is restored.
Adding/removing a file or changing its bytes remains a refused drift, with one
harness exception: Claude may rewrite the bytes of its seeded `.claude.json`,
and a matching private atomic-write temp file (`.claude.json.tmp.<pid>.<id>`)
is ignored when the projected file set is compared. The seeded file must still
exist and still be 0600; removing it or loosening its mode is refused.

The controlled Pi loader must not pass its frozen projection as the storage
location of a file-backed SettingsManager. It uses Pi's storage interface with
memory-only writes and read-only inputs from the frozen projection and trusted
project. Settings setters are effective for the current session. Resource reload
and session replacement read those same approved inputs again; memory-only Pi
preferences do not become a new config source. Pi's session transcript
persistence is unaffected.

The readiness `config` check verifies an existing current-revision projection
before reporting pass. Privacy or content violations produce `blocked`, the
specific validation reason in `detail`, and null `reloadRequired`. A stopped
binding is not an exemption. Readiness does not repair or publish files.
Missing projections keep the existing next-start/revision semantics.

Explicit recovery uses [config rebind](config-binding.md); it does not weaken
start's drift refusal or erase the original projection. Operator instructions:
[`docs/guides/pi-projection-recovery.md`](../../docs/guides/pi-projection-recovery.md).

## Frozen mapping and legacy-item refusal

The mapping version `agent-home-p2-native-v1` accepts exactly two source shapes:

- `native/<harness>/<rest>` projects to native relative path `<rest>`, and only
  for the harness named in the path.
- `shared/skills/<rest>` projects to `skills/<rest>` for every supported
  harness.

Any other config item is refused before publication. The refusal is
item-specific: it names the offending item and the destination the frozen
mapping assigns to it, so a legacy root config such as `config/AGENTS.md` or
`config/skills/review/SKILL.md` produces an actionable message instead of a
bare mapping failure.

- A root-level `skills/<rest>` item is told to move to
  `config/shared/skills/<rest>`; this is the shared-skills special case.
- Every other unmapped item is told to move to
  `config/native/<requested-harness>/<item>`.

The message is always `config item <path> is outside the frozen native/shared
mapping; move it to config/<destination>`. For example, requesting harness `pi`
for an item `AGENTS.md` yields `move it to config/native/pi/AGENTS.md`, and the
same request for `skills/review/SKILL.md` yields
`move it to config/shared/skills/review/SKILL.md`.

The suggested destination is the mapping slot, not a promise that the item is
acceptable there. Following the hint never bypasses the other refusals:

- a root `auth.json` for `pi` is told `move it to config/native/pi/auth.json`,
  and that same path is then refused as credential-bearing
  (`authentication must remain outside C`);
- two source items that would land on one native path still fail as a mapping
  collision;
- privacy and mode checks still apply, and the authentication exclusion is
  unchanged in every case;
- the secret-like path and content scan (`AGENT_CONFIG_REBIND_SECRET_CONTENT`)
  runs only when a rebind is staged, not during ordinary runtime preparation,
  so a moved `.env` or an `AGENTS.md` holding an API key is not caught on a
  plain start; the operator must not rely on the scan there.

Codes are unchanged. `build_native_projection` still raises `AgentConfigError`,
whose code is `INVALID_ARGUMENT`. The P2 home-access boundary carries the same
text into `AgentHomeAccessError`, so the public `agent.runtime-context`
response reports `AGENT_HOME_ACCESS_FAILED` with that message. No new error
code, wire field, or projection mapping version is introduced.

The hint is guidance only. It introduces no automatic migration: the frozen
source manifest is unchanged, no destination directory or file is created, no
item is renamed or copied, and the start still fails. The operator or dispatcher
must move the item explicitly and rebind or restart with the corrected source,
and must first confirm that the item is allowed in C at all; an advised path
does not make a credential-bearing or otherwise forbidden item acceptable.

## Executable controls

- `tests/identity/agents/configuration/test_agent_config_loading.py`
  (`test_t03_unmapped_c_item_names_the_item_and_its_exact_move_target`,
  `test_t03_rejects_unmapped_or_credential_bearing_c_items`)
- `tests/identity/agents/home/migration/p2/test_projection_modes.py`
- `tests/daemon/harnesses/pi/configuration/test_pi_controlled_loader.py`

The real-SDK cases cover both absent and seeded settings, a session setter,
reload, re-resolution, and explicit rebind after an actual legacy SDK write.
They use isolated homes and do not need a model call or production credentials.
