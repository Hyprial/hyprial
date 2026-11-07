# CLI command-surface contract

`surface.json` is the canonical inventory of the Typer/Click application used by
the installed `hyprial` console entry point. It includes every command path,
including groups and hidden commands, plus arguments, option spellings, stable
literal defaults, choices, deprecation/visibility flags, and first-line help.

Regenerate it from the repository root:

```sh
uv run --python 3.12 python scripts/verify/update_cli_surface_snapshot.py
```

The generator selects fresh private `HOME`, `XDG_CONFIG_HOME`, `HYPRIAL_HOME`,
and `HARNESS_STATE_DIR` directories before importing the application. It only
walks registration metadata: it does not invoke commands, start a daemon, or
contact one.

A PR that changes the command surface must update `surface.json` in the same PR
and explain why in the PR body. A removal or rename of a command introduced
after CLI refresh #1830 requires an explicit reason. The main pytest suite shows
removed, added, and changed command paths and options, then prints the exact
regeneration command.

## 0.5.5.2 group and migration behavior

All groups, including the root, print help and exit 2 on a bare invocation.
The exact exceptions are `init` (initialization), `daemon upgrade` (upgrade
report), `agent tiers` (tier report), and `routine templates` (template list).
Each exception has an owning decision and reason in
`tests/shell/cli/surface/test_cli_group_help.py`; traversal asserts equality,
not just membership. An onboarding subcommand does not initialize its parent.

| Removed top-level group | Registered path |
| --- | --- |
| `service` | `org service` |
| `fs` | `org fs` |
| `user-proxy` | `user proxy` |
| `link` | `agent link` |
| `onboarding` | `init onboarding` |

`host` remains top-level. `daemon service` remains the supervisor group, not
the destination of `service`. Removed paths have no alias and do no work.
They exit 2 with empty stdout, including under `--json`, and exactly one stderr
line: `moved: hyprial <old argv>  →  hyprial <new argv>` plus a newline.
There are two spaces on each side of the arrow; remaining arguments are kept.
No fabricated JSON result is emitted for these usage errors.
