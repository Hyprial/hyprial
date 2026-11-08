# Network isolation for test daemons (`HYPRIAL_NETWORK_ISOLATED`)

Status: implemented on `feat/daemon-network-isolated`. It lifts the 2026-09-16 ban on
"isolated" test daemons once hq-adjutant's criteria are met; the evidence is in the PR.

## Why

Isolating a test daemon by setting `HYPRIAL_HOME`/`HARNESS_STATE_DIR` and
`HYPRIAL_PEER_DISCOVERY=0` isolated its **files**, not its **network**:

- Forwarding runs before the discovery switch is read, and joins the tailnet
  through a sidecar.
- Zenoh may add its own listener on all interfaces when `listen` is empty.
- The usage fetcher calls model vendors over HTTPS.

## The switch

`HYPRIAL_NETWORK_ISOLATED=1` is read by `DaemonApplication.from_environment`.

### How the value is parsed (fails closed)

| Value | Result |
|---|---|
| unset | not isolated (this is what production runs) |
| `1`/`true`/`yes`/`on` | isolated |
| `0`/`false`/`no`/`off` | not isolated, on purpose |
| empty, whitespace, or anything else | the daemon **refuses to start** and names the variable |

The shared zenoh flag parser treats an empty value as unset. `export
HYPRIAL_NETWORK_ISOLATED=` would therefore leak a networked daemon, so this
switch does not use that rule for empty values.

### What changes when it is on

- **Listen address.** Listen defaults to an explicit `tcp/127.0.0.1:0`, so zenoh never falls back to its own default listener.
- **Non-loopback endpoints are refused.** Any listen or connect endpoint that is not loopback stops the daemon at startup. That includes a tailnet address, `0.0.0.0` and `[::]`.
- **Forwarding is refused.** If any `HYPRIAL_FORWARDING_*` variable is present, the daemon stops at startup. The check is on the variable's presence, not on an address, because the sidecar's endpoints are loopback and tunnel out.
- **Paths that are skipped:**
  - listen derivation;
  - peer discovery (the tailscale backend and the command backend);
  - zenoh gossip;
  - the usage fetcher.

### Reporting

`ping` and `ps` report `zenoh.isolated` and `zenoh.isolation`. The value is
computed from what startup actually did, **not** echoed from the variable.
Each check is listed separately:

- `endpointsLoopbackOnly`
- `listenNotDerived`
- `discoveryNotConsulted`
- `gossipOff`
- `forwardingOff`
- `usageFetchOff`

`effective` is true only when every check passes.

### Entry points

- **pytest.** `tests/conftest.py` (an autouse fixture) sets the switch for every test, and spawned daemons inherit it.
- **Contract harness.** `contract/e2e/_daemon_runner.py`'s `IsolatedDaemon` sets it, and `start()` **refuses to continue** unless `ps` reports `zenoh.isolated: true`. 20 scenarios use this runner.
- **Scripts that build their own environment** set it explicitly:
  - `contract/cli-blackbox/run.py`
  - `run-codex-app-server.py`
  - `run-inprocess-autoupdate.py`
  - `run-tui-attach-manual.py`
  - `run-upgrade-continuity.py`
  - `run-uv-install-flow.py`
  - `scripts/archived/rehearse_login_switch.py`
- **`scripts/archived/timeout_candidate_measurement.py`** inherits `os.environ`.
- **Deliberate opt-outs,** which set `=0` or unset it:
  - `tests/test_daemon_zenoh_failloud.py` tests a production-shaped daemon with no endpoints. Its closed state was measured on 2026-09-23: a daemon of this exact shape (discovery off, usage off, no `HYPRIAL_FORWARDING_*`, no listen or connect) holds **zero TCP/UDP sockets**, so zenoh opens no default listener when `listen` is empty. The test strips any `HYPRIAL_FORWARDING_*` and sets `HYPRIAL_USAGE_DISABLE=1`.
  - `tests/test_forwarding_config.py::test_direct_daemon_refuses_forwarding_without_explicit_listen` tests the production forwarding refusal. It **never starts a daemon**: it only calls `DaemonApplication.from_environment` inside `pytest.raises`.
- **The conditional paths the switch does not close are refused by the runner.** `IsolatedDaemon.require_no_external_triggers` runs before `init` launches the daemon and again after it. It refuses a home that contains any of these:
  - Feishu gateways in `channels.json`, or `secrets/lark-*.json` (Feishu adapters);
  - `settings.json` `autoUpgrade: true` (autoupdate);
  - no `HYPRIAL_OWNER` (the owner identity would come from `tailscale whoami`).

  **Feishu gateways at boot (ruled by Allen, 2026-09-24).** The guard allows Feishu gateways in an `IsolatedDaemon` home in exactly two cases. A production bot (`PRODUCTION_FEISHU_APPS`: `hq-adjutant-feishu`, `hyprial-kanban`, `mac-studio-feishu`) refuses `start()` in every case.
  - **The Lark worker is replaced.** `HYPRIAL_LARK_WORKER_COMMAND` is set, so no Feishu WebSocket or HTTPS client runs. `run-four-lark-adapters.py` (E2E-007) is this case: its `--fake-worker` only signals ready and then idles. It needs no declaration. Its adapters are named `e2e007-fake-lark-1` … `-4` for that role, not after the production bots, so the production-bot refusal does not apply to it. `run-pin-restart-survival.py` (E2E-003) is the same case: its two synthetic adapters exist only for pin semantics and run on `tests/fixtures/fake_lark_worker.py`.
  - **The scenario is on the exempt list and declared itself.** It calls `IsolatedDaemon.allow_feishu_test_apps(Path(__file__).name)`. Every gateway must then be one of that scenario's listed test Apps (by `appId`), and every `secrets/lark-*.json` must belong to one of those gateways. The list lives in one place only, `_daemon_runner.FEISHU_EXEMPT_SCENARIOS`. `tests/test_network_isolated.py` fails if the table below and that constant name different scenarios, or if either is empty.

  A declared scenario is **not** counted as isolated for Feishu: it deliberately reaches Feishu with its test Apps. Autoupdate and owner identity are still refused for it, and the zenoh mesh must still report `isolated: true`.

  Residual: a route send that the scenario itself issues (`route:<adapter>:<route>`) goes from the daemon to Feishu through `_route_lark_gateway`, not through the Lark worker. A replaced worker therefore does not close route sends. E2E-007 issues none.

  <!-- feishu-exempt-scenarios:start -->
  | Scenario | Test Apps it may load at boot | Reaches Feishu? |
  |---|---|---|
  | `run-squire-setup.py` (E2E-009) | The dedicated test App `cli_<e2e-test-app>`, added through `adapter add` before `start()` | Yes, real Feishu |
  | `run-all-harness-lifecycle.py` (E2E-013) | The fabricated pin probe `isolated_e2e013_probe` (never started), and the dedicated test App when the Lark prerequisites hold | Yes, when the test App is included |
  | `run-lark-app-onboarding.py` (E2E-011) | The synthetic Apps `cli_env_app`, `cli_e2e011_created_app` and `cli_existing_app`, registered through its loopback stub and loaded at boot by step A8 | Their adapters load at boot with synthetic credentials |
  <!-- feishu-exempt-scenarios:end -->

  `run-feishu-roundtrip.py` launches its own daemon, not an `IsolatedDaemon`, so this guard does not run for it.
- **Refused on production nodes.** `run-upgrade-continuity.py` (E2E-010) and `run-uv-install-flow.py` (E2E-005) start older released builds that predate the flag, so their daemons cannot be isolated. `_daemon_runner.refuse_unisolatable_daemon` makes both exit 3 (`RESULT REFUSED`):
  - whenever a **live production daemon answers** on this machine, meaning the login user's `~/.hyprial/state/daemon.sock` is connectable. **No environment variable can override this.**
  - otherwise, unless `HYPRIAL_E2E_ALLOW_UNISOLATED_DAEMON=1` is set.

  Every bypass variable of this kind is registered in `_daemon_runner.BYPASS_ENVIRONMENT`. The gate strips all of them before running a scenario, whatever put them in its environment (workflow `env`, runner, launchd, a wrapper). A test fails if a scenario reads an unregistered `HYPRIAL_E2E_ALLOW_*` name.

  In `run-isolated-gate.py`, exit code 3 is reported as `REFUSED`. That is neither PASS nor FAIL. If anything was refused and nothing failed, the gate ends with `RESULT INCOMPLETE GATE ... (REFUSED on this machine, not run: ...)` and exit code **4**, so "not run" and "ran and broke" stay distinct. Whoever reports a gate result must quote that parenthesis: an INCOMPLETE gate is not a full pass.

### Known coverage gap

The E2E workflow (`.forgejo/workflows/e2e.yml`) runs on `macos-host`, directly on this Mac, which is a production node. So from now on these two scenarios **run nowhere**, until they can be isolated or a runner with no production mesh exists. hq-adjutant is collecting the options for Allen. What goes unverified in the meantime:

- **E2E-005 (uv install / upgrade / uninstall).** Install with pre-rename PEP 610 metadata; prove that version and `hyprial daemon upgrade` use the canonical `HyprialOS/harness-bridge`; observe a newer dev commit through a disposable local bare remote; uninstall.
- **E2E-010 (running upgrade continuity).** Upgrade a live disposable deployment across a real package/commit boundary, restart, and prove:
  - adapter recovery;
  - session re-registration;
  - FIFO message continuity;
  - fresh delivery ids;
  - no replay of acknowledged messages.

- **Not covered.** Any entry point added later that builds its own environment without the flag. Such a daemon still reports `zenoh.isolated: false` in `ps`, so a runner that reads the flag back can catch it.

How the entry points were found:

```text
git grep -l -E 'subprocess\.(run|Popen|check_output|call)|pexpect|os\.spawn' -- tests contract scripts e2e \
  | xargs git grep -l -E '"init"|daemon run|"daemon", *"run"|_REAL_LAUNCH_DAEMON_PROCESS|IsolatedDaemon' --
```

That search returns 60 files. Each contract and script file was then sorted into
`IsolatedDaemon` / own environment / inherits. The test files inherit the
conftest switch.

## Inventory of network paths

Search, over the daemon source:

```text
git grep -c -E 'urlopen|urllib\.request|requests\.(get|post|put|Session)|httpx\.|http\.client|socket\.(create_connection|socket\()|zenoh\.open|asyncio\.open_connection|websocket|lark\.ws|ws\.Client|"git", *"(fetch|ls-remote|clone|push)"|ls-remote|"uv", *"tool"|tailscale|hyprial-tsnet|"forward"\]|LarkSdkGateway|lark_oapi|api\.anthropic|chatgpt\.com|api\.kimi|typesafe_sdk|forgejo|/api/v1' -- src/hyprial
```

It hits 44 files. Each path is marked **Blocked** (with the line that closes
it), **Allowed** (with the reason), or **Not covered**. Line numbers refer to
`src/hyprial/daemon/application.py` on this branch unless stated otherwise.

| # | Path | Where | Status |
|---|---|---|---|
| 1 | The zenoh session, which carries all mesh traffic: inbox, liveliness, route registration, org-context publish, pull | `transport/zenoh.py:226` `zenoh.open` | **Blocked.** Endpoints are loopback only; non-loopback ones are refused (`_isolated_endpoints`); gossip is off (`_gossip_for_startup`); listen is an explicit loopback address. |
| 2 | Deriving a listen address from `tailscale status` | `_derive_listen_endpoint`, `discovery.local_tailnet_endpoint` | **Blocked.** Skipped when isolated. |
| 3 | Peer discovery: `tailscale status --json`, then dialing peers on :7447; or the `HYPRIAL_PEER_DISCOVERY_COMMAND` backend | `_discover_peer_endpoints`, `discovery.py` | **Blocked.** Skipped when isolated. The evidence run's mock `tailscale` recorded 0 calls. |
| 4 | Forwarding sidecar (tsnet / headscale / official tailnet) and its periodic relaunch | `_discover_peer_endpoints` (forwarding half), `daemon/forwarding.py`, `forwarding.timer` | **Blocked.** Startup is refused if any `HYPRIAL_FORWARDING_*` variable is present, so no sidecar exists and the timer has nothing to relaunch. |
| 5 | Usage fetch (HTTPS to api.anthropic.com, chatgpt.com, api.kimi.com) | `usage.py`, the `UsageCache` thread | **Blocked.** `usage_cache=None`. |
| 6 | Feishu adapters: WebSocket and HTTPS, user adapters, route sends, owner alerts | `adapters/lark/*`, `configure_user_adapters`, `_restore_adapters`, `_route_lark_gateway`, `autoupdate/alert.py` | **Not closed by the switch; the runner refuses the trigger.** `IsolatedDaemon` refuses a home with Feishu gateways or `secrets/lark-*.json`. Exceptions: a replaced Lark worker (E2E-007), or a declared scenario from `FEISHU_EXEMPT_SCENARIOS` loading only its listed test Apps (E2E-009, 011, 013). Production bots are always refused. See **Feishu gateways at boot** above. `run-feishu-roundtrip.py` uses real Feishu on its own daemon, outside this guard. |
| 7 | Autoupdate: `git ls-remote` and `uv tool install` in a child process | daemon scheduler, `updates.py` | **Not closed by the switch; the runner refuses the trigger.** `IsolatedDaemon` refuses a home with `autoUpgrade: true`. `run-inprocess-autoupdate.py` **deliberately** exercises it. |
| 8 | Harness workers (claude, codex, pi, dsh, jev) calling model providers or TypeSafe | `harnesses/*` child processes | **Allowed.** A worker that talks to its model is what these tests run. |
| 9 | `tailscale whoami` for owner identity in `tailscale-selfhost` mode | `daemon/identity.py`, `tailnet_identity.py` | **Not closed by the switch; the runner refuses the trigger.** `IsolatedDaemon` refuses to start without `HYPRIAL_OWNER`. It is a local query to tailscaled, not a daemon connection (0 mock calls in the evidence run). |
| 10 | Login, tsnet join, installers, desktop network, network profile, Forgejo calls | `login.py`, `tsnet_join.py`, `tsnet_sidecar.py`, `installers/*`, `desktop_network.py`, `network_profile.py`, `cli.py` | **Outside the daemon.** These are CLI or desktop commands a test runs explicitly; the daemon does not start them. |
| 11 | Local sockets: the IPC server, the MCP client, the exec-env socket, the GUI probe | `socket.AF_UNIX` at `application.py` 3246, `mcp/unix.py`, `harnesses/_exec_env_socket.py`; `gui_runtime.py` (a 127.0.0.1 probe) | **Not a network path.** |

### How this maps to infra-ops' four points

| infra-ops point | Status |
|---|---|
| Active discovery | Rows 2 and 3 |
| The forwarding branch runs before the switch | Row 4. Refused by variable presence, before anything starts. |
| Configured connect endpoints are always kept | Row 1. They are **refused** unless loopback, never silently kept. |
| The listen side | Row 1. An explicit loopback listener; wildcard and tailnet addresses are refused. |
