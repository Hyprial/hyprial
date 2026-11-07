# Expected top-level commands: Allen rulings, 2026-10-06 (Feishu)

The top level is final at 22 commands, grouped by domain noun. Commands are removed outright; there are no aliases.

| Top-level | Absorbs | Note |
|---|---|---|
| help, version, doctor, log, config | | Observation |
| login | | |
| init | `onboarding plan/apply`, which become `init --plan` / `init` | Covers the whole machine's first-run setup. Desktop callers (contract §11) are updated in the same PR. |
| profile, gui | | |
| squire | `user-proxy`, which becomes `squire proxy` | Personal assistant |
| socialware | `install`, which becomes `socialware install` | Placeholder entry only, not implemented yet |
| user, adapter | adapter absorbs `lark-auth` | |
| agent | `start`/`stop` (formerly the connector's start/down), `move` plus a `transfer …` subgroup (the 14 transfer-* commands), `top`, the connector/session part of `ps` (folded into `agent list`), `grant` (the old grants-check/visibility) | An agent's whole life cycle |
| message | send/ack/reply/targets, plus status/trajectory/outbox/inbox (the old delivery/query) | |
| org | `fs` | OrgFS belongs to the org |
| network | `hosts`, which becomes `network peers` | This host's Tailcat network; `org network` stays under org |
| service | | Standalone service access (sub2api and similar): connect/disconnect/list/guide |
| workflow | `dispatch` and `worktrees` | PAC |
| routine | | PAC |
| daemon | `restart` and the daemon part of `ps`, which becomes `daemon status` | |
| upgrade | `autoupdate` | Upgrades the whole install, not just the daemon |

Removed with no replacement as a top-level command: ps, top, targets, hosts, onboarding, install, restart, autoupdate, dispatch, user-proxy, delivery, outbox, query, trajectory, grants-check, grants-visibility, lark-auth, mcp (moved out of the CLI tree), the transfer-* commands, start, down, fs, worktrees, send, ack, reply.

Next: go through the subcommands group by group, starting with agent. Write this into docs/design/cli-surface/expected-commands.md on PR #1126; waiting on hq to confirm who owns that branch.
