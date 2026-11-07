# PAC CLI acting principal

Issue #1734 defines one identity path for CLI commands executed inside an agent
session. The daemon session registry remains the authority; environment values
are claims that only become authenticated after the daemon matches the exact
`actor + sessionRef` pair.

## Resolution map

1. `src/hyprial/biz/impl/workflow/identity.py:34-101` resolves a CLI binding.
   Headless managed workers use `HYPRIAL_WORKER_ACTOR` plus the existing direct
   `HYPRIAL_WORKER_SESSION_REF`. Interactive carriers use the same actor plus
   `HYPRIAL_WORKER_SESSION_REF_FILE`. The file must be absolute, regular,
   owner-only, owned by the current uid, at most 4096 bytes, and contain one
   token. Partial, conflicting, unreadable, or unsafe context fails with
   `PAC_PRINCIPAL_UNVERIFIED`; it never falls back to `user:<owner>`.
2. `src/hyprial/shell/impl/cli/commands/workflow/run.py:54-84` applies that
   binding to workflow requests. `run`, `complete`, and `fail` therefore send
   `actor + sessionRef`; an explicit `--from` must agree with the resolved
   principal. `src/hyprial/shell/impl/cli/commands/common/support.py` applies
   the same binding to message claims, and
   `src/hyprial/shell/impl/cli/commands/message/send.py` defaults an omitted
   `--from` only when an agent binding is present.
3. `src/hyprial/daemon/impl/application/workflows.py:440-501` accepts the local
   machine owner only as exact `user:<owner>` without a session. Agent writes
   pass through `_pac_bound_caller`.
   `src/hyprial/daemon/impl/application/actors/sessions/registry.py:758-827`
   compares the presented pair with the live interactive binding or the
   daemon-managed worker session. A persisted registration alone is not enough:
   a released or dead interactive binding is rejected. A mismatched, moved,
   retired, absent, or dead pair is rejected. Workflow endpoints expose these
   claim failures as `PAC_PRINCIPAL_UNVERIFIED`.

## Carrier map

- Shared interactive file publication and environment construction:
  `src/hyprial/shell/impl/cli/commands/agent/runtime/interactive.py:48-78`.
- Pi publishes the known ref before spawn and injects the file-backed binding:
  `src/hyprial/shell/impl/cli/commands/agent/runtime/sessions.py:232-242`.
- Claude does the same for its known native session:
  `src/hyprial/shell/impl/cli/commands/agent/runtime/claude.py:164-166,414-418`.
- Codex injects the actor and file path into both app-server and remote-TUI
  environment before spawn, then atomically publishes the discovered thread
  ref after daemon registration:
  `src/hyprial/shell/impl/cli/commands/agent/runtime/sessions.py:634-644,799`.
- Headless Claude, Codex, and Pi retain their direct binding. The common source
  is `WorkerChannel.identity_environment` in
  `src/hyprial/daemon/impl/harnesses/turn_delivery/channel/worker_channel.py:99-121`.

The file-backed form avoids exposing a discovered interactive ref as a process
argument and supports Codex, whose ref is unavailable when its processes are
spawned. Possession of the file does not bypass daemon verification.

## Observable attribution and completion routing

Successful JSON from `hyprial workflow run/complete/fail` and `hyprial message send`
contains `actingPrincipal`. The daemon returns its verified canonical principal,
and the CLI passes that value through unchanged instead of echoing its claim.
It therefore reports owner-only relocation and bare-alias canonicalization, so
scripts can distinguish an interactive agent from the machine owner.

Workflow graph creation stores the daemon-verified caller as `created_by`.
Turn notifications go to each ready node's owner. When a node reports bounded
`outputText`, the completion/output alert goes to the verified graph creator,
not to the machine owner and not implicitly to an end/report node owner; see
`src/hyprial/daemon/impl/pac/graphs/reactor.py:771-790`. Thus a graph started
from an interactive agent routes its completion output back to that agent.
