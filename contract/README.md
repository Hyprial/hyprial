# Harness Bridge cross-implementation contracts

Phase 0 freezes observable TS Harness Bridge behavior before Python product
code is written. The corpus is deliberately language-neutral.

## 语料地图（2026-10-01 聚类）

契约族按**外部可观察行为面**组织，不随 `src/hyprial` 内部模块重构而变动——
内部搬家时这里是回归验证的支点。"钉住的产品面"列给出五模块视角的对应关系。

| 目录 | 语料 | 钉住的产品面 |
|---|---|---|
| `wire/` | golden/（v1 信封 fixture + 声明式期望）、trajectory.md、trajectory.schema.json、message-sender-auth.md、send-links.md、send-links.json | kernel 的 wire 词汇表 + daemon 消息面（含 send links：summary/body、hold、link/outbox/top、载体与错误码） |
| `cli-blackbox/` | 调用已安装的 `hyprial`，断言 JSON/退出行为 | shell 命令面（`hyprial.cli:main` 对外契约） |
| `mcp-tools/` | 经固定身份 server 调用全部公开 MCP 工具（官方 SDK + fake Unix-daemon） | daemon 的 MCP 工具面 |
| `daemon-lifecycle/` | 证明干净启动与崩溃启动走同一 desired-state 恢复路径 | daemon 生命周期/恢复 |
| `workers/` | python-worker、user-proxy、pi-attach-probe 的进程协议 schema + 帧校验 | daemon 的 harnesses（worker 进程协议） |
| `inbox-watch/` | cases/（双 lane 事件流黄金用例）、README（规格） | 各客户端 harness 共用的收件箱等待状态机（Python 核心 + TS 移植） |
| `pac/` | context、events、remote——派单协议族 | daemon 的 PAC 派单域 |
| `e2e/` | 可执行端到端流程与证据闸门（E2E-001…，ROADMAP.md G1 的载体） | 全链路（跨模块验收） |
| `migration/` | 迁移切换演练（drill） | daemon/identity 的迁移路径 |

根部文件：`run-all.sh`（总 runner）、`python-resolver.sh`（解释器解析）。

## Run against the TS reference

With the globally installed/source-built TS `hyprial` and its daemon running:

```sh
./contract/run-all.sh
```

`run-all.sh` resolves one Python interpreter for every Python-backed corpus:
an explicitly set `HYPRIAL_PYTHON`, this checkout's `.venv/bin/python`, a non-empty
`VIRTUAL_ENV`, then `python3` on `PATH`. Each selected source is authoritative:
an empty explicit override, broken link, directory, non-executable, invalid
Python identity, Python older than 3.12, or missing project dependency fails
before any child corpus runs instead of falling through to a different Python.
The error includes the selected source and path plus actionable
`uv sync --extra test` guidance for dependency failures.

The aggregate runner resolves the CLI independently, in strict order:
`--hyprial`, `HYPRIAL_BIN`, this checkout's executable `.venv/bin/hyprial`, then `hyprial`
on `PATH`. An explicitly selected empty, missing, broken, directory, or
non-executable CLI fails with its source and path instead of silently falling
through. The standalone
`cli-blackbox/run.sh` deliberately omits the repository default so a copied
language-neutral corpus can continue to test a TS CLI from `HYPRIAL_BIN` or PATH.

For the strict sealed-source baseline (`dev@71ea88c`):

```sh
./contract/run-all.sh --hyprial /path/to/71ea88c/dist/cli.js --doctor-schema sealed
```

This runs read-only live `ps`, `doctor`, and `message targets` checks. All write-shaped
CLI probes use temporary isolated state and stop before daemon delivery. See
`cli-blackbox/README.md` for the exact safety boundary.

To select a particular TS binary:

```sh
./contract/run-all.sh --hyprial ../harness-bridge-ts/dist/cli.js
```

The selected path must itself be executable. If invoking TS through Node is
required, provide a small executable wrapper as `--hyprial`.

## Run against the Python implementation

After installing the Python CLI into an isolated uv environment:

```sh
uv sync --extra test
uv run --project . python -m hyprial.cli version --json
./contract/run-all.sh --hyprial "$(uv run --project . which hyprial)" --doctor-schema sealed
```

To select a different interpreter explicitly, including a path containing
spaces:

```sh
HYPRIAL_PYTHON=/path/to/python ./contract/run-all.sh --isolated-only
```

Alternatively, install it as a uv tool and select that executable explicitly:

```sh
uv tool install --force .
./contract/run-all.sh --hyprial "$(uv tool dir --bin)/hyprial" --doctor-schema sealed
```

The CLI runner now starts a test-owned daemon after completing its stopped-state
checks, so the full 11-case gate is hermetic and expected to be green.

## Hermetic gate

CI jobs without a live reference daemon/network can still validate the local
CLI surface and both corpora:

```sh
./contract/run-all.sh --isolated-only
```

The CLI black-box runner calls `init` only with disposable `HYPRIAL_HOME` and
`HARNESS_STATE_DIR` roots, then gracefully stops that exact daemon. It never
calls `start`, `down`, `upgrade`, or the ambient daemon socket.

The daemon lifecycle runner uses only temporary state and fake provider,
transport, and inbox ports. Durable state is the inbox/SQLite responsibility;
there is no second storage-supervision port. It never starts the ambient daemon
or touches the user's service manager.

The MCP runner drives the fixed-identity server shared by the stdio Channel and
`agent-channel` entry points through the SDK's in-memory client transport. Its
stateless daemon proxy uses a fake connection; it binds no host port or ambient
Unix socket and retains no inbox data.

The executable daemon and uv scenarios are fully isolated and safe to run
locally:

```sh
uv run python contract/e2e/run-daemon-restart-survival.py
uv run python contract/e2e/run-pin-restart-survival.py
uv run python contract/e2e/run-dual-instance-roundtrip.py
uv run python contract/e2e/run-uv-install-flow.py
```

They use temporary state/tool/bin/cache/home directories, runner-owned daemon
PIDs and sockets, and a disposable local Git remote; they never push the real
`dev` branch or change ambient uv tools.
