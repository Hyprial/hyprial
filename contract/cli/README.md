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

## Implicit daemon start

Before the single request of a business command, the CLI tries to connect to
the home's socket (no frame is sent). Only when that connect fails (no socket
file, connection refused) it starts the daemon once through the launch
primitive and then sends the request once; nothing is ever retried. A daemon
that accepts the connection and answers `DAEMON_UNAVAILABLE` is reported as
before and never restarted. The start is implicit: it never installs an OS
service unit (only `init`, `daemon start` and `daemon service install`
register one), and it prints one notice on stderr, so stdout under `--json`
stays exactly one JSON object.

- Never started implicitly: `daemon status|start|run|stop|restart`,
  `agent top`, `daemon service *`, `daemon upgrade *`, `doctor`, `profile *`,
  `login`, `logout`, `init`, `version`, `help`,
  `socialware`, and the commands that work with no daemon: `adapter add`,
  `adapter remove`, `agent tiers` (including `probe`), `org context import`.
  `ping` and `shutdown` never start one either.
- A command run inside an agent session (a complete or partial worker binding
  in the environment) never starts the daemon.
- `HYPRIAL_DAEMON_AUTOSTART=0`, those commands and agent sessions leave
  stdout and stderr byte-identical to a CLI without this feature: the decision
  is made before any output.
- On a home with no owner the daemon runs in bootstrap and answers a method it
  declares but cannot serve yet with `ACCOUNT_REQUIRED` itself; the CLI keeps
  that text and adds `data.nextSteps: ["hyprial login"]`. A method name the
  bootstrap daemon does not know stays `METHOD_NOT_FOUND`.
- `daemon restart` on a bootstrap daemon restarts it; `before` and `after`
  carry `phase: "bootstrap"`.
- `hyprial init` never logs in. On a home with no owner it starts the bootstrap
  daemon and prints the launch result with `phase: "bootstrap"`, `owner: null`
  and `nextSteps: ["hyprial login"]` (exit 0); it has no `identity`, `device`,
  `verificationUri` or `userCode`. A bootstrap daemon that is already running
  gets the same `nextSteps`.
- `hyprial login` on a home with no owner (a bootstrap daemon answers, or no daemon
  answers and the home has no owner) is one daemon operation: one
  `auth.login.begin` with `until: "serving"`, the verification URL and user code
  on stderr (the browser opens unless `--no-open`), one `auth.operation.get`
  with `waitSeconds`, and on Ctrl-C one `auth.operation.cancel`. The result keeps
  the keys `ok, identity, device, daemon, migration, pending, verificationUri,
  userCode` (plus `nextSteps`); a device that cannot be registered is exit 0 with
  `device.ready: false` and `device.retry: "serving-start"`; Ctrl-C after the
  identity is committed exits 130. A serving daemon on a home with no owner
  refuses with the daemon's own `IDENTITY_CONFLICT`. `login --no-daemon` does not
  exist.

## 0.5.5.2 group and migration behavior

All groups, including the root, print help and exit 2 on a bare invocation.
The exact exceptions are `init` (initialization), `daemon upgrade` (upgrade
report), `agent tiers` (tier report), and `routine templates` (template list).
Each exception has an owning decision and reason in
`tests/shell/cli/surface/test_cli_group_help.py`; traversal asserts equality,
not just membership.

| Removed top-level group | Registered path |
| --- | --- |
| `service` | `org service` |
| `fs` | `org fs` |

`host` remains top-level. `daemon service` remains the supervisor group, not
the destination of `service`. Removed paths have no alias and do no work.
They exit 2 with empty stdout, including under `--json`, and exactly one stderr
line: `moved: hyprial <old argv>  →  hyprial <new argv>` plus a newline.
There are two spaces on each side of the arrow; remaining arguments are kept.
No fabricated JSON result is emitted for these usage errors.

## Adapter route configuration

`hyprial adapter route list`, `candidates`, `add`, and `remove` operate through
the local daemon. They require the daemon to be reachable and do not fall back
to direct local configuration or observation-store access when it is unavailable.
The existing adapter operator policy applies: worker requests, explicit
actor/session claims, and matching interactive process identities are refused.
Successful output retains the existing route configuration and reload receipt.
Configuration-domain error codes and messages are preserved; unexpected local
exceptions now cross the standard IPC error boundary and may receive a
`DAEMON_ERROR` code.

## Routine plan validation

`hyprial routine plan <file>` reads the named UTF-8 file on the CLI host and
sends its contents and diagnostic label to the local daemon for validation and
plan projection. A readable file therefore requires host daemon IPC that
supports `routine.plan` and is subject to the daemon's restore gate and
ordinary IPC errors; there is no local-validation fallback. Managed workers can
use their restricted relay, which retains `ROUTINE_SCHEMA_ERROR` while replacing
detailed error messages and data with a generic message. Worker relay frames have
a strict 8 MiB limit including their JSON envelope; host IPC has its own buffer
limit. Failure to read the file remains a local
`ROUTINE_SCHEMA_ERROR` before a daemon request. Successful text and JSON plan
shapes retain the existing fields and formatting.

The IPC request and response readers use an approximately 8 MiB buffer bound.
JSON encoding and escaping count toward the request frame; a read can cross the
boundary in its final chunk. Large files or plan responses that previously fit
local validation can therefore fail at the IPC boundary, so 8 MiB is not a
promised exact file-size cutoff. Oversized requests can close the connection;
oversized responses can produce `IPC_RESPONSE_TOO_LARGE`. No automatic fallback
or retry bypasses that boundary.

`hyprial agent top` obtains the human-readable process metrics from the daemon
using `top.inspect`; this requires a daemon that supports that method. Text mode
adds five seconds for sampling to the configured IPC round-trip timeout. The
combined request has one timeout budget rather than the former separate IPC
and local sampling budgets. Sampling failures display unavailable metrics. The
`--json` form continues to use `top.snapshot` and does not sample processes.
## Grant simulations

`hyprial agent grant <actor> check` and `visibility` evaluate hypothetical
inputs through the daemon. A readable command therefore requires a serving
daemon, is subject to its restore gate and ordinary IPC limits, and has no
local simulation fallback. The supplied caller and flags describe the scenario;
they do not authenticate a caller, read the grant ledger, or authorize an
operation. The actor argument remains unused by these two simulations.

The host CLI keeps its existing success shapes and error formatting. The new
IPC methods use canonical `ENFORCEMENT_ERROR`, `ENFORCEMENT_SCOPE_INVALID`,
`VISIBILITY_ERROR`, and `VISIBILITY_SCOPE_INVALID` wire codes. The CLI maps
the scope errors to their previous lowercase codes and retains the prior
uncoded constructor-error display. Managed workers may use these methods via
their bound relay. The relay preserves the wire error code but replaces error
messages and data with its generic failure, so guest error details are not
identical to host diagnostics. The relay's frame limit includes its envelope.
`workflow inspect --node NODE --context` reads the graph snapshot from the
running daemon. The daemon's configured state directory, owner and node identity
are used for actor association. This command requires the daemon; the prior local
snapshot path could succeed while it was unavailable. Requests during daemon
restore receive the standard restoring error, and the restricted worker relay
does not expose this method. A guest's earlier local database may differ from
the daemon's graph. Malformed or unreadable store failures cross the IPC
boundary as `DAEMON_ERROR`.

## Business commands on a daemon without an account

A daemon running in the bootstrap phase (a home without an owner) refuses every
method the serving daemon answers with the one code `ACCOUNT_REQUIRED` and
`data {reason, phase, method}`, whether the method is a declaration or is still
served only by the router's own branches (`ps`, `agent grants`, `message ack`,
`workflow ...`, `routine add`, `outbox prune`, and the worker and session
methods). The CLI translates nothing. A method name no daemon answers stays
`METHOD_NOT_FOUND`. Before this change the router-only names answered
`METHOD_NOT_FOUND` in the bootstrap phase.

## `hyprial gui --json` startup stream

`gui-startup-record.v1.json` is the machine contract for the stdout of
`hyprial gui [dev] --json`: JSON Schemas for the startup record and the failure
object, the exit statuses, the consumer checks and a corpus of examples. It is
the stable surface a launcher (a native shell, a script) may rely on; keys outside
`startupRecord.required` are informational.

- Line 1 of stdout is the startup record (`ok: true`, `record_version: 1`) or a
  failure (`ok: false`, optional `code`, `error`, optional `data`). After a
  startup record at most one more line follows, a failure that says why the
  session ended (for example `GUI_ASSETS_EXITED`, or `GUI_BACKEND_EXITED` with the `contract-mock` driver). Progress and warnings go to
  stderr.
- `--port 0` asks the host to bind a free loopback port: `port` and `url` always
  name the port the host bound (never 0). In `dev` mode `url` is the host's
  origin, not the Vite server's.
- `--stdin-stop` makes EOF or a `shutdown` line on stdin stop the GUI like
  SIGTERM but with exit status 0 (POSIX only; refused with `INVALID_ARGUMENT`
  elsewhere). Without the flag stdin is not read. Children are reclaimed; a
  daemon the launcher started or borrowed is left running. A stop that arrives
  before the startup record is written exits 0 with NO stdout line: a consumer
  must treat "exit 0 and no line" as a clean stop, not as a protocol error. A
  stdin that is non-blocking is not EOF; only real EOF or a `shutdown` line
  stops the GUI. A stop that arrives while the command is already cleaning up
  after another outcome is ignored, so that first outcome's status and failure
  line stand.
- With `--no-open`, `handoff_path` names a private one-time file holding
  `<origin>/#grant=<grant>`. The path is absolute in the host platform's form:
  POSIX `/...`, or on Windows a drive-letter (`C:\...`, `C:/...`) or UNC
  (`\\server\share\...`) path; relative, drive-relative and rootless paths are
  refused. On POSIX the file is mode 0600; Windows has no mode bit, so its
  privacy rests on account-private directory access, not on 0600. The record,
  the failure and stderr never contain the grant or the upstream bearer.
- `backend_pid` is `null` and `backend_endpoint` is `""` for the daemon driver
  (2026-11-19, owner-approved): the host serves the request-http dispatch inside
  its own process, so the host (`frontend_pid`) is the only child besides the
  optional Vite server (`assets_pid`) and no backend process or address exists.
  A consumer must read a null `backend_pid` as "no separate backend process"
  and must not treat a missing pid as a failed start. The `contract-mock` driver
  keeps an integer `backend_pid` (the Node mock) and a loopback
  `backend_endpoint`. No key was added or removed and `record_version` stays 1,
  because `backend_pid` already allowed `null`.
- `backend_owned` is `false` for the daemon driver (2026-11-20, owner-approved;
  it was `true`): the launcher starts no backend child and does not own the
  daemon, which outlives the GUI and is never stopped by closing it. The
  `contract-mock` driver launches its own loopback child and keeps `true`. No
  key was added or removed and `record_version` stays 1.
- A failed start carries `data.stages_seconds` and, per child, `data.children.<name>`
  with `pid`, `exit_code` and a redacted `log_tail`.
- `business`, `note` and `handoff_note` are human-readable text and may be
  reworded without a version bump: one launch path, one Python session host, the
  daemon as the only real backend (`hyprial gui` ensures it; `contract-mock`
  remains a source-only test driver). The former
  informational key `m1_source` was removed; `record_version` stays 1 because it
  was never required.
- `dev_tools` and `api_docs` are optional booleans (added 2026-11-17; a consumer
  must read an absent key as `false`). `dev_tools` is `true` when the launch asked
  for the developer tool layer (`--with-dev-tools`, or `dev`, which implies it).
  `api_docs` is `true` only when the session host confirmed that it serves the API
  docs page: tools on, the daemon driver, and the docs files present. Neither key
  is an address: the docs page is opened from the tool panel of the GUI.
- `live_source` is an optional boolean (added 2026-11-17; absent means `false`):
  the page is served from the GUI source through the Vite dev server behind the
  host (`--live-source`, or `dev`). `hot_reload` equals it. The Vite port is not in
  the record: `url` and `port` are the host's, the only public ones, and
  `assets_pid` identifies the dev server child. `mode` is `dev` exactly when both
  `live_source` and `dev_tools` are on; it is informational.

`tests/tooling/contracts/cli_startup/` validates the examples and the record the
real `startup_payload` builds. A change to a required field or its meaning bumps
`version` and `record_version` together. Widening the `handoff_path` pattern to
Windows absolute paths (2026-11-23, owner-approved) keeps version 1: no host
changes what it writes. A Windows host already wrote these paths and the pattern
now admits them; a POSIX host still writes `/...`. A consumer that validates
with the old pattern refuses only records it already refused.
