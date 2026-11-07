# PAC admission gate: launch workers only when the host and daemon can take them

Date: 2026-10-22. Status: **WIP design** (Allen approved the project on 2026-10-21). Owner: R&D (emergent-org-structure-refactor).

## 1. Problem

- A ready PAC node launches its worker immediately. Nothing checks host load, daemon health or how many workers are already running.
- The coordinating agent applies an operating rule by hand: pause new dispatch when the 1-minute load average is above 20 (this host has 28 cores), or when the daemon process is sustained above 2 cores.
- The daemon is a single process that every agent's inbox and every PAC event pass through. When it is busy, inbox IPC misses its 2-second budget. On 2026-10-21 we saw repeated `inbox command timed out ... queued=27` and `SUBMIT_OUTCOME_UNKNOWN`.
- A manual pause holds back whole graphs, depends on someone remembering to resume, and is invisible in `workflow status`. Dispatching anyway makes the overload worse.

## 2. Goal

Graphs can be dispatched at any time. Each ready worker node waits in an explicit, visible **admission queue** until the configured conditions hold, then launches automatically. Queued is a normal state, never a failure.

## 3. Design

### 3.1 Conditions (node-local evaluation, inside the daemon)

| Signal | Source | Default threshold |
|---|---|---|
| Host load | 1-minute load average | ≤ 0.7 × logical CPUs (20 on this 28-core host) |
| Daemon CPU | the daemon's own CPU usage over a sliding 60 s window | ≤ 2 cores sustained |
| Daemon responsiveness | p95 inbox IPC latency over 60 s | ≤ 1 s |
| Concurrency | running PAC workers on this node | ≤ a configurable cap (default 8) |
| Per-graph fairness | running workers of one graph | ≤ a configurable cap (default 3) |

- Each signal is measured locally. Missing measurement support (for example on another OS) counts as "unknown → pass", and is shown as such. The gate never deadlocks on an unmeasurable signal.
- Thresholds live in the daemon's operator config (`hyprial config`), not in graph YAML, so one rogue graph cannot opt out of the gate.

### 3.2 Behaviour

- A node whose dependencies are satisfied enters `admission-queued` instead of launching.
- An existing GenerationScheduler entry re-evaluates the gate with jittered backoff, starting at 15 s and capped at 2 min, one arm per outcome. There is no busy loop.
- Admission is FIFO by ready time, with the per-graph cap preventing starvation. Review and end nodes that run no worker are never queued.
- Node timeouts start at launch, not while queued. A separate queue budget (default 6 h) moves a node to `admission-expired` and applies the graph's `on_failure` policy.
- `workflow status` / `inspect` show `admission: queued (reason: host-load 23.4 > 20; since 12:01; next check 12:03)`. The 480 s no-progress watchdog does not fire for queued nodes.
- An operator override, `hyprial workflow admit <graph> <node>`, admits once and is audited. It is for a critical-path node; this is the formal version of the manual exception used on 2026-10-21.

### 3.3 Layering

- The gate sits in the PAC dispatcher (daemon workflow domain), before the worker launch effect.
- It reads host and daemon metrics through a small local metrics port, with no network and no IPC on the measuring path. Network/daemon decoupling rule: no IPC handler waits on the gate.

## 4. Tests

- With load forced high, a ready node queues with its reason, and launches when the load drops.
- The per-graph cap is enforced.
- Queue expiry applies `on_failure`.
- The watchdog stays silent while a node is queued.
- An audited override admits exactly once.
- An unmeasurable signal passes and is shown as unknown.
- Restarting the daemon preserves queue order and reasons.

## 5. Open questions

- Whether to measure the remote worker's host for remote/hosted workers, or only the local launch host.
- Default caps per node, and whether harness type (pi, codex, claude) should weigh differently.
