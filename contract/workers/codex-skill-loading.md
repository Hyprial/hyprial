# Codex skill-loader warnings

A successful Codex `skills/list` RPC can contain per-skill `errors`. Those errors
mean that the affected skills were not loaded. They do not reject Hyprial agent
startup. All other native-root, config, authentication, and skill-scope validation
remains in force. Malformed RPC results are not converted to warnings.

## Diagnostic surfaces

- Native-load evidence carries an immutable `warnings` tuple. Managed-process
  facts and harness status project these startup warnings without changing
  readiness, failure counters, or persistent desired state.
- A managed `lifecycle.start` success includes optional `warnings: string[]`.
  Harness status rows use the same optional field. The field is absent when the
  current process has no startup warnings.
- Interactive Codex start uses the existing structured warning shape:
  `{"code":"CODEX_SKILL_LOAD_WARNING","message":"..."}` in `warnings`.
- Human-facing headless start prints warnings to stderr. JSON stdout remains one
  result object. Interactive launch also announces warnings on stderr.
- Daemon log event `codex.skills.load_warning` has level `warn`, with the bounded
  diagnostic in `error`. No new error code or failed lifecycle outcome is emitted
  solely for per-skill loader errors.

The diagnostic starts with `Codex skills/list reported loader errors: ` and
contains Codex's error text and path. Shared kernel credential/URL redaction runs
on the structured errors before JSON encoding and truncation. Each combined
loader diagnostic is at most 4096 UTF-8 bytes, including a trailing `...` when
truncated. Logging applies the normal logger redaction again.

## Squire repair

Both Squire templates contain YAML `name: squire` and a nonempty `description`.
No owner placeholders occur in frontmatter. Setup can add that header only when
the existing skill is byte-equal to the prior headerless body rendered for the
same inputs. Any different skill is preserved, with manual frontmatter repair
instructions if its header is absent. Legacy relocation preserves its source;
the copied file uses the same comparison and upgrade rule after the existing
legacy-path replacements. A second setup run must not change the already
upgraded skill.

## Verification

- `tests/shell/squire/setup/test_squire_frontmatter.py`
- `tests/daemon/harnesses/codex/native_home/home_contracts/test_fresh_launch_validation.py`
- `tests/daemon/harnesses/codex/app_server/startup/test_startup.py`
- `tests/daemon/harnesses/headless/test_headless_resume_start.py`
- `tests/shell/cli/agents/test_cli_headless_resume.py`
- `tests/shell/cli/agents/codex/test_discovery_launch.py`
- `uv run python scripts/verify/check_codex_squire_skill.py --codex <isolated-binary> --root /tmp/hr-<new-directory>`

The real probe uses Codex CLI 0.164.0, an empty isolated HOME/CODEX_HOME, and no
credentials. It calls `initialize`, `skills/list`, and `thread/start` for both
headerless and corrected skills. It does not send a model turn. A successful
thread creation proves only this offline startup behavior, not provider login
or full message delivery.
