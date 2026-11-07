# WP6：CLI 与 MCP 工具面（规格）

- 设计依据：[design-send-links-2026-10-14.md](../design-send-links-2026-10-14.md) §7、§8.2、§8.3、§9、§14
- 缝：`src/hyprial/daemon/impl/links/ports.py`（WP0）
- 分支：`wp6/send-links`，基于集成分支 `refactor/org-structure-constrain`
- 依赖：WP0（已合）。IPC 返回形状依赖 WP2（link.*、top 的 W 与在途）、WP3（message.send/reply/read_body/pending.list）、WP4（top 的入站等待注入）、WP7（openRequestsFrom）。本包对这些形状**按本文 §2 的字段名编码并容忍缺失**，不等它们合并。
- 规则：shell 只经 IPC，不 import `hyprial.daemon.impl.*`；结构门：每目录 ≤7 个直接文件，每文件 ≤1000 行。

## 0. 基线读数（2026-10-14，本 worktree，HEAD 177929d26）

- `PYTHONPATH=$PWD/src uv run --extra test pytest tests/shell/entrypoints tests/shell/cli/presentation tests/shell/cli/output tests/shell/cli/communication tests/shell/cli/lifecycle/test_cli_top.py -q`：105 passed，**1 failed**：`tests/shell/entrypoints/test_shell_cli_entry.py::test_whole_registered_command_inventory_stays_on_native_app`。原因：它要求 `dispatch` 是顶层命令，而 `tests/shell/cli/presentation/test_cli_top_level_concepts.py::test_dispatch_is_not_a_top_level_command` 要求 `dispatch` 不是。这个失败早于 WP6。
- 注：「冻住的 CLI 命令清单」在仓库里**没有**严格的等值快照。最接近的是上面那个测试（子集断言）和 `tests/shell/cli/presentation/test_cli_help_inventory.py`（`help` 列表与注册树相等，部分子组用等值断言）。WP6 更新这两处，见 §1.4。
- 目录现状：`shell/impl/cli/commands/message/` 有 6 个文件；`tests/shell/cli/communication/` 有 7 个（已满）；`tests/shell/cli/lifecycle/` 有 7 个（已满）；`tests/daemon/channels/mcp/contracts/` 有 7 个（已满）；`daemon/impl/ipc/` 有 7 个（已满）。

## 1. 改哪些文件

### 1.1 shell：新命令组 `commands/link/`（新建目录，3 个文件）

| 文件 | 要点 |
| --- | --- |
| `src/hyprial/shell/impl/cli/commands/link/__init__.py` | 一行 docstring。 |
| `src/hyprial/shell/impl/cli/commands/link/commands.py` | `link_app = typer.Typer(help=…, no_args_is_help=True)`；四个子命令 `list`、`stats`、`add`、`drop`，写法照 `commands/agent/grant.py` 与 `commands/message/outbox.py`：`services._execute(operation, json_output=…)`，operation 返回 `CliResult(data, render=…)`。方法名用 `IPC_LINK_LIST/STATS/ADD/DROP`（经 daemon 公共面导入，见 1.5）。`--from` 复用 `commands/message/send.py` 的 `_validate_from_identity` 回调；参数用 `_message_claim_params(source)` 组装，使 managed worker 的 `sessionRef` 照常携带。每个参数都写 `help=`。约 150 行。 |
| `src/hyprial/shell/impl/cli/commands/link/render.py` | 纯函数：`render_link_list(data) -> str`、`render_link_stats(data) -> str`、`render_link_change(data) -> str`。只用 `output.tables.render_table` 与已有的时长格式（可从 `commands/daemon/top.py` 导入 `_format_top_age`）。缺失或类型不对的字段一律渲染为 `—`（U+2014）。不 print。约 150 行。 |

### 1.2 shell：`commands/message/`（6 → 7 个文件，到上限）

| 文件 | 要点 |
| --- | --- |
| `commands/message/send.py` | 1）新增 `--summary`：`typer.Option(None, "--summary", help=…)`。**故意声明为可选**，缺失时由本地校验报 `SUMMARY_REQUIRED`（退出 1），不交给 click 报 usage error（退出 2，码 `INVALID_ARGUMENT`）。2）新增模块级纯函数 `_require_summary(value: str \| None) -> str`：`None`、去空白后为空 → `CliError(SUMMARY_REQUIRED, …)`；UTF-8 字节数 > `SUMMARY_MAX_BYTES` → `CliError(SUMMARY_REQUIRED, …, data={"summaryBytes": n, "maxBytes": 512})`；返回去首尾空白的值。在 `operation()` 里、任何 `_daemon_request` 之前调用，放在空 message 检查之前。3）参数加 `"summary": <值>`。4）新增 `_lift_hold(result) -> dict`：只做投影，不改 daemon 已有的键：若顶层没有 `queued`，置 `queued = any(d.queued for d in deliveries)`；若恰一个 delivery 且它 `queued: true` 且带 `reason`，并且顶层没有 `reason`，把它提到顶层（同 daemon 已对 `messageId` 做的单目标提升）。5）新增人读渲染 `_render_send(data) -> str`（见 §2.2）。6）`openRequestsFrom` 提示：非 `--json` 时，对每条带非空 `openRequestsFrom` 的 delivery（若 delivery 上没有，再看顶层）经 `output.warn` 打一行（见 §2.2）；`--json` 时 `warn` 本身静默，字段已在数据里。现有 `replyPathUnavailable` 的 warn 保留。预计 124 → 约 230 行。 |
| `commands/message/reply.py` | 加 `--summary`（同上，可选声明 + 本地校验，复用 `send._require_summary`）。把调用改为 `def operation()`，在其中先校验 summary 再发 `message.reply`，参数加 `"summary"`。`LINK_RECLAIMED` 等 daemon 拒绝原样透传（`ok:false`，退出 1）。 |
| `commands/message/read.py`（新建） | `@app.command("read")`，函数名 `read_body`（避免遮蔽内建 `read`）。参数：`message_id` 位置参数、`--from`（必填，`_validate_from_identity`）、`--json`。调用 `IPC_MESSAGE_READ_BODY`，参数 `{**_message_claim_params(source), "messageId": message_id}`。人读输出只写正文（§2.3）。help 写明「消费动作：daemon 记录 fetched；消息仍待回复，直到 reply 或 ack」；与 `hyprial query`（只读）分开，`query` 不改。约 70 行。 |

### 1.3 shell：`commands/daemon/top.py`

- `_render_top` 在 `Q` 与 `STATE` 之间加三列：`W`、`OUT`、`INJQ`。取每行 actor 的 `capacityW`、`outboundInFlight`、`inboundAwaitingInjection`；字段缺失、为 `None`、或不是 `int`（`bool` 也算不是）时显示 `—`，不报错、不抛异常。
- `--json` 不加占位：daemon 没给的键在 JSON 里依然没有。
- `top_status` 的 docstring 补一句三列的来源与 `—` 的含义。443 → 约 465 行。
- 字段由 daemon `top.snapshot`（`src/hyprial/daemon/impl/operations/top.py`）提供，**不在本包实现**：`capacityW`、`outboundInFlight` 由 WP2（来自 `LinkQueries.stats` 的 W 与在途出站合计），`inboundAwaitingInjection` 由 WP4（W 准入挡在 inbox、未注入的行数）。见 §6 对缝的建议。

### 1.4 shell：注册与清单

| 文件 | 要点 |
| --- | --- |
| `src/hyprial/shell/impl/cli/runtime/entry.py` | 导入 `from …commands.message.read import read_body`（放在 `reply` 导入之后，使 `read` 在注册顺序上落在 `reply` 与 `query` 之间，不影响 `test_transfer_registration_retains_its_original_slot_before_upgrade` 检查的 `down…gui` 切片）；导入 `link_app` 及四个子命令函数；在 group wiring 区 `app.add_typer(outbox_app, …)` 之后加 `app.add_typer(link_app, name="link")`；`__all__` 加 `link_app`、`link_list`、`link_stats`、`link_add`、`link_drop`、`read_body`。484 → 约 495 行。 |
| `tests/shell/entrypoints/test_shell_cli_entry.py` | `test_whole_registered_command_inventory_stays_on_native_app`：子集加 `"link"`、`"read"`、`"reply"`；**去掉 `"dispatch"`**（它与 `test_dispatch_is_not_a_top_level_command` 矛盾，是 §0 记录的基线红）。这是本包点名要更新的「命令清单」测试。 |
| `tests/shell/cli/presentation/test_cli_help_inventory.py` | `test_help_inventory_matches_registered_top_level_tree` 的可见集合子集加 `"link"`、`"read"`；新增断言 `set(root.commands["link"].commands) == {"list", "stats", "add", "drop"}`（等值，冻住子命令）。 |
| `tests/shell/cli/presentation/test_cli_help_coverage.py` | **不改**。所有新参数都写 `help=`，`_UNDOCUMENTED_BUDGET` 保持 54；若这项测试变红，说明漏写 help，修参数而不是改预算。 |

### 1.5 daemon 公共面：IPC 方法名经 facade 导出

shell 不能 import `hyprial.daemon.impl.links.ports`（`scripts/ci/check_module_boundaries.py` 会拦）。在 `src/hyprial/daemon/interface.py` 的三处（`TYPE_CHECKING` 导入、`_FACADE_EXPORTS`、`__all__`）各加：`IPC_LINK_LIST`、`IPC_LINK_STATS`、`IPC_LINK_ADD`、`IPC_LINK_DROP`、`IPC_MESSAGE_READ_BODY`、`HOLD_REASONS`、`SUMMARY_MAX_BYTES`，都指向 `hyprial.daemon.impl.links.ports`。shell 用 `from hyprial.daemon import IPC_LINK_LIST, …`，在函数体内惰性导入（照 entry.py 对 `hyprial.daemon` 的现有写法），以免拖慢 CLI 启动。只加导出，不改行为。

### 1.6 daemon IPC：注册 `link.*`

ports.py 的注释写明「WP6 registers link.*」。`daemon/impl/ipc/` 已满 7 个文件，不新建文件：

- `src/hyprial/daemon/impl/ipc/router.py`（850 行）：allow-list 加四个方法名（用 ports 常量，不写字面量）；dispatch 加一个分支 `if method in (IPC_LINK_LIST, IPC_LINK_STATS, IPC_LINK_ADD, IPC_LINK_DROP): return self._ipc_link(method, params, _trusted_message_origin=…)`；`_ipc_link` 约 30 行：
  - 链接服务取 `getattr(self, "_link_service", None)`（由 WP2 在 composition 里赋值）；为 `None` 时抛 `DaemonRequestError(DAEMON_NOT_READY, "link service is not wired")`。
  - `link.list`、`link.stats`：只读，与 `top.snapshot` 同级（只有本机 socket 能到）；`from` 只是 actor 选择器，按现有 canonical 解析，调用 `list_links(actor)` / `stats(actor)`，结果 `{"ok": True, **mapping}`。
  - `link.add`、`link.drop`：会改状态，主体用 `self._authenticated_message_caller(params, method=method, trusted_origin=…)` 取 `caller.subject`（与 `message.ack` 同一道门：actor 本人凭 session 围栏，或本机 owner 操作者），`peer` 用 `_required_string`，调用 `add(actor=…, peer=…)` / `drop(actor=…, peer=…)`。
- 增长约 45 行，到 ~895 行，在 1000 行内。add/drop 的协议见 §6 建议 1。

### 1.7 MCP：`src/hyprial/daemon/impl/mcp/server.py` 与 channel adapter

| 文件 | 要点 |
| --- | --- |
| `daemon/impl/mcp/server.py` | `harness_send(to, summary, message)`：`to: NonEmpty \| Annotated[list[NonEmpty], Field(min_length=1)]`；字符串包成 `[to]`，列表原样（不去重、不排序，交给 daemon）；`summary: NonEmpty` 必填（字节上限由 daemon 判，MCP 不重复判）；参数 `{"to": [...], "summary": summary, "message": message}`。description 补「to 可为数组（fan-out）；summary 必填，是对方 context 里唯一能看到的部分，≤512 字节」。`harness_reply(messageId, summary, message)`：summary 必填（WP3 让 `message.reply` 要求 summary，不补会让所有 MCP 回复失败）。新增 `harness_read_body(messageId)`：`invoke(ctx, IPC_MESSAGE_READ_BODY, {"messageId": messageId}, mutation=True)`（消费动作，记录 fetched，所以 mutation=True）。`harness_read`：不改代码，只改 description 为「返回 summary 与 body 字节数；正文用 harness_read_body 取」；返回值原样透传。579 → 约 610 行。 |
| `daemon/impl/mcp/channel/adapter.py` | `harness_reply(message_id, summary, message)` 透传 summary（427 → ~430 行）。`harness_read_body` 不经 channel adapter，与 `harness_send` 一样走 `invoke`。 |
| `daemon/impl/harnesses/turn_delivery/channel/worker_channel.py` | `WORKER_HARNESS_TOOLS` 加 `"harness_read_body"`。不加的话 headless worker 调用它会卡在无人应答的权限确认上。 |
| `shell/impl/cli/commands/agent/runtime/claude.py` | `_CLAUDE_HARNESS_TOOLS` 加 `"harness_read_body"`（interactive 的允许列表）。 |
| `daemon/impl/harnesses/pi/resources/pi_harness_bridge.ts` | pi 的同名工具面与 MCP 对等（R18）：`harness_send` 的 `to` 改为 `Type.Union([Type.String(), Type.Array(Type.String(), {minItems: 1})])`，字符串包成数组；加必填 `summary`；`harness_reply` 加必填 `summary`；新增 `harness_read_body`（`invoke("message.read_body", {messageId}, true)`）。214 → 约 245 行。若评审裁定它归 WP5，本行整体移交，§3 的 T15 随之移交。 |

### 1.8 契约与已有测试的同步（不同步 CI 必红）

| 文件 | 要点 |
| --- | --- |
| `contract/mcp-tools/run.py` | `harness_send` 调用加 `summary`；`harness_reply` 加 `summary`；新增 `("harness_read_body", {"messageId": "m3"})`，`factory.calls` 相应位置加 `"message.read_body"`；输出改为 `mcp-tools: PASS 32/32`。 |
| `tests/shell/compatibility/test_unreachable_surface_retirement.py` | `test_live_mcp_contract_passes_on_the_fixed_identity_server` 断言改为 `"mcp-tools: PASS 32/32"`。 |
| `contract/cli-blackbox/run.py` | `isolated send fails before delivery` 的参数加 `--summary probe`（否则会先报 SUMMARY_REQUIRED，不再是 DAEMON_UNAVAILABLE）；新增检查 `isolated send without summary fails locally`：不带 `--summary`、无 daemon，期望 `ok:false`、`code == "SUMMARY_REQUIRED"`、退出 1。这是「本地即报」在黑盒层的证明。 |
| `tests/shell/cli/communication/test_cli_send_resources.py` | 调用加 `--summary`；期望参数加 `"summary"`。 |
| `tests/shell/cli/communication/test_cli_from_validation.py` | 所有 `send`、`reply` 调用加 `--summary`（第 45、169、170 行附近）。 |
| `tests/daemon/channels/mcp/contracts/test_mcp_daemon_alignment.py` | 三处 `harness_send` 调用加 `summary`（第 210、253、282 行附近）。 |
| `tests/daemon/channels/mcp/contracts/test_mcp_channel.py` | 工具名集合加 `"harness_read_body"`；`harness_reply` 调用补 summary。文件现 901 行，只允许个位数增长。 |
| `tests/daemon/harnesses/pi/extension/test_pi_harness_bridge_extension.py` | 工具清单断言加 `harness_read_body`；send/reply 调用补 summary。 |

不在本包：`src/hyprial/skills/hyprial-ops/SKILL.md`、`src/hyprial/daemon/impl/squire/SKILL-template.md` 里的 `hyprial send` 示例（WP8）；`contract/wire` 的 summary golden（WP8）；`hyprial outbox list` 的原因与等待时长列（design §9 列了，但 outbox.list 是通用渲染，原因字段由 WP2 写进行内即可透传；本包不做专门渲染）。

### 1.9 结构核对（实施后必须成立）

| 目录 | 直接文件数（后） |
| --- | --- |
| `shell/impl/cli/commands/` | 1（`__init__.py`），新增目录不计 |
| `shell/impl/cli/commands/link/` | 3 |
| `shell/impl/cli/commands/message/` | 7（到上限；后续需要再拆时开子目录） |
| `daemon/impl/ipc/` | 7（不变） |
| `tests/shell/cli/` | 4（不变），新测试放新子目录 |
| `tests/shell/cli/links/`（新建） | `__init__.py` + 4 个测试文件 = 5 |
| `tests/daemon/channels/mcp/links/`（新建） | `__init__.py` + 1 = 2 |
| `tests/daemon/ipc/links/`（新建） | `__init__.py` + 1 = 2 |

## 2. 接口

通用约定（`AGENTS.md` CLI 输出一节）：结果只经 `CliResult`；`--json` 时 stdout 恰好一个 JSON 对象且带 `ok`；`ok:false` 退出 1；提示与警告经 `output.warn` 走 stderr，`--json` 下静默。参数缺失、类型错（click usage error）退出 2。daemon 不可达沿用 `DAEMON_UNAVAILABLE`，退出 1。

### 2.1 `hyprial link`

所有子命令的 `--from <actor>` 必填，接受注册别名或 canonical URI（`_validate_from_identity`：裸名先经 `agent.resolve` 核实；daemon 不可达时 fail-open）。

**`hyprial link list --from <actor> [--json]`** → `link.list`，参数 `{"from", "sessionRef"?}`。

期望 daemon 返回（WP2 实现 `LinkQueries.list_links` 的建议形状；CLI 对每个键都容忍缺失）：

```json
{"ok": true, "actor": "agent:op:n1:alice", "k": 4, "W": 6, "inFlight": 3,
 "owner": {"peer": "user:op"},
 "links": [{"peer": "agent:op:n1:bob", "state": "established", "cwnd": 2, "inFlight": 1,
            "lifecycle": "regular", "mintedAtMs": 0, "lastUsedAtMs": 0, "revision": 3}]}
```

人读（`links` 按 state 排序：established、pending、reclaimed，再按 peer 排）：

```
agent:op:n1:alice   k 4   W 6   in-flight 3/6   agent links 1/4
PEER                STATE        CWND  IN-FLIGHT  LIFECYCLE  LAST USED
user:op             owner        —     —          —          —
agent:op:n1:bob     established  2     1          regular    3m
agent:op:n2:carol   pending      —     —          regular    —
```

`agent links a/b`：a 是 state 为 established 的条数，b 是 k。owner 行恒在第一行（design §3.2）；daemon 没给 `owner` 时这一行不画。`--json`：daemon 结果原样，带 `ok`。退出码：0；daemon 拒绝时 1。

**`hyprial link stats --from <actor> [--json]`** → `link.stats`。期望形状（WP2 `LinkQueries.stats`）：

```json
{"ok": true, "actor": "…", "nActive": 23, "k": 4, "W": 6, "wTarget": 8,
 "tMinMs": 42000, "tBarMs": 70000, "muMax": 0.12, "diff": 2.4, "grad": -3000,
 "links": [{"peer": "…", "rttMinMs": 30000, "rttMs": 45000, "cwnd": 2, "inFlight": 1}]}
```

人读：

```
agent:op:n1:alice
N_active 23   k 4   W 6 (target 8)
T_min 42s   T_bar 1m10s   mu_max 0.12/s   diff 2.4   grad -3s
PEER              RTT_MIN  RTT   CWND  IN-FLIGHT
agent:op:n1:bob   30s      45s   2     1
```

缺失的量显示 `—`。退出码同上。

**`hyprial link add <peer> --from <actor> [--json]`** → `link.add`，参数 `{"from", "sessionRef"?, "peer"}`。期望返回 `{"ok": true, "actor", "peer", "state": "pending"|"established", "revision"?}`。人读：`link agent:…:alice -> agent:…:bob: pending (waiting for the peer to reference back)`，或 `…: established`。若 daemon 返回 `queued: true, reason`（例如自身满了），按 hold 渲染（与 §2.2 相同的 hold 文案）。退出码：0（含 pending 与 hold）；`ok:false` 时 1。

**`hyprial link drop <peer> --from <actor> [--json]`** → `link.drop`。期望返回 `{"ok": true, "actor", "peer", "state": "reclaimed", "drained": n}`。人读：`dropped link agent:…:alice -> agent:…:bob; drained 2 pending request(s) as ack`（`drained` 缺失时省略后半句）。退出码同上。

### 2.2 `hyprial send` 与 `hyprial reply`

```
hyprial send --from <actor> --to <target> [--to …] --summary <text> [--topic] [--conversation]
             [--reply-to] [--file …] [--image …] [--on-behalf-of] [--json] <message…>
hyprial reply <messageId> --from <actor> --summary <text> [--on-behalf-of] [--json] <message…>
```

- `--summary`：必填（语义上），≤512 UTF-8 字节，不截断。缺失、全空白、超长 → 本地 `SUMMARY_REQUIRED`，**不发任何 `message.send`/`message.reply` IPC**，退出 1。
  - `--json`：`{"ok": false, "code": "SUMMARY_REQUIRED", "error": "…"}`；超长时多 `"data": {"summaryBytes": n, "maxBytes": 512}`。
  - 人读：stderr 一行 `hyprial: --summary is required (at most 512 UTF-8 bytes)`；超长时 `hyprial: --summary is 731 bytes; at most 512 UTF-8 bytes`。
- IPC 参数：`message.send` 新增 `"summary"`；`message.reply` 新增 `"summary"`。其余不变。

`send` 返回的 `--json`（daemon 结果 + `_lift_hold` 投影）：

| 情形 | stdout JSON | 退出 |
| --- | --- | --- |
| 已投递 | `{"ok": true, "queued": false, "deliveries": [{"target", "messageId", "accepted": true, "queued": false}], "messageId", …}` | 0 |
| 被 hold（单目标） | `{"ok": true, "queued": true, "reason": "LINK_PEER_FULL", "deliveries": [{…, "accepted": true, "queued": true, "reason": "LINK_PEER_FULL", "detail"?: "…"}], …}` | 0 |
| 铸造中（单目标，无 link） | 同上，`reason` 取 daemon 给的值（见 §6 建议 3），`queued: true` | 0 |
| 多目标部分 hold | 顶层 `queued: true`，无顶层 `reason`，原因在各 delivery | 0 |
| 拒绝（如 `LINK_UNKNOWN`、`TARGET_NOT_AUTHORIZED`） | daemon 的 `ok:false` 与 `code` 原样 | 1 |
| 本地缺 summary | 见上 | 1 |

hold 原因只有 `HOLD_REASONS` 三个（另有铸造态，见 §6），**hold 永远不是 `ok:false`**。CLI 不自己判定 hold，只投影 daemon 的 `queued`/`reason`。

`send` 人读（stdout，每个 delivery 一行）：

```
sent    agent:op:n1:bob     3f2a…
held    agent:op:n2:carol   7c1e…   LINK_PEER_FULL: peer has no free link slot
        the daemon retries delivery itself; do not resend
failed  agent:op:n3:dave    —       LINK_UNKNOWN
```

- `held` 行用于任何 `accepted: true, queued: true` 的 delivery；`reason` 缺失时写 `queued`；`detail` 缺失时只写原因码。
- 只要有一条 held，在最后加一行 `the daemon retries delivery itself; do not resend`（即「重试由 daemon 负责」）。测试按这句英文原文断言。
- 原有 `replyPathUnavailable` 的 stderr 行保留不变。

`openRequestsFrom`（WP7 提供）：非 `--json` 时，对每个带非空 `openRequestsFrom` 的 delivery，stderr 打一行：

```
hyprial: you have pending request(s) from agent:op:n1:bob: 9a0b…, 1c2d…; if this is a reply, use 'hyprial reply <messageId>'
```

字段只在顶层时，X 取唯一目标；多目标时 X 写 `the target`。字段缺失或为空列表时什么也不打。消息照发，退出码不受影响。`--json` 下不打（字段在数据里）。

`reply` 人读：沿用通用渲染（不改）；`--json` 原样。`LINK_RECLAIMED` 是 daemon 的 `ok:false`，退出 1。

### 2.3 `hyprial read`

```
hyprial read <messageId> --from <actor> [--json]
```

- IPC：`message.read_body`，参数 `{"from", "sessionRef"?, "messageId"}`。
- 期望返回（WP3）：`{"ok": true, "messageId", "sender", "summary", "body": "<utf-8 text>", "bodyBytes": n, "fetched": true}`。
- 人读：stdout **只有正文**，逐字节写出（`CliResult` 的 render 返回 `body` 字符串；`_print_human` 原样写并补末尾换行）。不加标题、不加 summary，便于管道。正文为空时输出空行。返回里没有 `body` 字符串时（例如 WP3 对二进制正文给的是别的键），stdout 写 `<body not text: N bytes; use --json>`。
- `--json`：daemon 结果原样，带 `ok`。
- 语义：消费动作，daemon 记录 fetched（WP3 负责）；**不 ack、不改变 pending**，可重复读。`hyprial query` 保持只读，不调用 `message.read_body`。
- 退出码：0；消息不存在或无权读时 daemon `ok:false`/错误码，退出 1；缺 messageId 或 `--from` 退出 2。

### 2.4 `hyprial top`

- 表头：`ACTOR RT PID [CPU%] RSS CPUTIME UPTIME TURNS AVG5 IDLE Q W OUT INJQ STATE`。
- `W` ← actor 行的 `capacityW`；`OUT` ← `outboundInFlight`；`INJQ` ← `inboundAwaitingInjection`。不是 int 时显示 `—`。
- `--json`：不变，原样透传（不补键）。退出码不变。

### 2.5 MCP

| 工具 | 参数 | IPC | 返回 |
| --- | --- | --- | --- |
| `harness_send` | `to: str \| list[str]`（非空；列表至少 1 个，元素非空）、`summary: str`（非空）、`message: str`（非空） | `message.send`，`{"to": [..], "summary", "message"}` | daemon 原样（含 `deliveries`、hold 的 `queued`/`reason`、WP7 的 `openRequestsFrom`） |
| `harness_reply` | `messageId`、`summary`、`message` | `message.reply`，加 `"summary"` | 原样 |
| `harness_read_body`（新） | `messageId: str`（非空） | `message.read_body`，`{"messageId"}`，mutation=True | 原样 |
| `harness_read` | 无 | `message.pending.list`（不变） | 原样；summary 与 body 字节数由 WP3 的返回决定，MCP 不增删字段 |

缺 `summary` 在 MCP 层是 schema 校验错误（工具调用 `is_error`），不发 IPC。超长由 daemon 返回 `SUMMARY_REQUIRED`。

### 2.6 daemon IPC（§1.6）

| 方法 | 参数 | 鉴权 | 返回 |
| --- | --- | --- | --- |
| `link.list` | `from` | 本机 socket（同 `top.snapshot`） | `{"ok": true, **list_links(actor)}` |
| `link.stats` | `from` | 同上 | `{"ok": true, **stats(actor)}` |
| `link.add` | `from`、`sessionRef?`、`peer` | `_authenticated_message_caller` | `{"ok": true, **add(...)}` 或错误 |
| `link.drop` | 同上 | 同上 | `{"ok": true, **drop(...)}` 或错误 |
| 任一，服务未接线 | — | — | `DAEMON_NOT_READY` |

## 3. 测试清单

运行方式：`PYTHONPATH=$PWD/src uv run --extra test pytest <路径> -q`。CLI 测试一律 `monkeypatch.setattr(cli, "_daemon_request", fake)` 记录 `(method, params)`；不起真实 daemon，不碰用户 home。「先红形状」指新代码出现之前这条测试的失败方式；实施者在 PR 正文贴出该失败输出。

| # | 测什么 | 先红形状 | 路径 |
| --- | --- | --- | --- |
| T1 | 输出纪律门覆盖新文件：`commands/link/*.py`、`commands/message/read.py` 没有 print、echo、`sys.std*.write`、`Console`。不加新测试，现有门对新文件自动生效；另加一条断言：`COMMANDS.rglob` 结果包含 `link/commands.py`、`link/render.py`、`message/read.py`，防止门因路径变动而漏扫 | 文件不存在时，路径存在断言失败（`AssertionError: link/commands.py not scanned`） | `tests/shell/cli/output/test_output_discipline.py`（加一个测试函数） |
| T2 | `send` 缺 `--summary`：退出 1，`--json` stdout 为 `{"ok": false, "code": "SUMMARY_REQUIRED", …}`，fake daemon 的调用列表为空（`--from` 用 canonical URI，避免 `agent.resolve`） | 现状退出 0，并发出不带 summary 的 `message.send`（`assert calls == []` 失败） | `tests/shell/cli/links/test_cli_send_summary.py` |
| T3 | 空白 summary 与 513 字节 summary（多字节字符拼成，比如 171 个「汉」=513 字节，而字符数只有 171）都报 `SUMMARY_REQUIRED`；超长时 `data == {"summaryBytes": 513, "maxBytes": 512}`；512 字节整放行，参数里 `summary` 原样 | 现状没有 `--summary` 选项，退出 2（`No such option`） | 同上 |
| T4 | `send --summary s` 的 IPC 参数里有 `"summary": "s"`，其余键与现状一致 | 现状退出 2 | 同上 |
| T5 | `reply` 缺 summary 报本地 `SUMMARY_REQUIRED`、零 IPC；带 summary 时 `message.reply` 参数含 `summary` | 现状退出 0，发出 `message.reply` | 同上 |
| T6 | hold 的 JSON 形状：fake 返回单个 delivery `{"accepted": true, "queued": true, "reason": "LINK_PEER_FULL"}`、`ok: true`，顶层无 `queued`/`reason` → stdout JSON 为 `ok is True`、`queued is True`、`reason == "LINK_PEER_FULL"`，退出 0。对 `HOLD_REASONS` 三个码参数化 | 现状顶层没有 `queued`（`KeyError`） | `tests/shell/cli/links/test_cli_send_hold.py` |
| T7 | daemon 已给顶层 `queued`/`reason` 时 CLI 不覆盖（投影是幂等的）；多目标部分 hold → 顶层 `queued: true`，没有顶层 `reason` | 现状没有顶层 `queued` | 同上 |
| T8 | hold 的人读：stdout 含 `held`、目标、`LINK_SELF_FULL`，最后一行是 `the daemon retries delivery itself; do not resend`；全部已投递时没有这行 | 现状是通用 Pretty 渲染，没有这行 | 同上 |
| T9 | `ok:false` + `code: LINK_UNKNOWN` → 退出 1，JSON 原样（hold 与拒绝分开） | 现状已是退出 1；此条作为护栏，开始时可能就是绿的，PR 里注明「护栏，非先红」 | 同上 |
| T10 | `openRequestsFrom`：delivery 上带 `["m1","m2"]` → 非 json 时 stderr 恰好一行，含目标、`m1`、`m2`、`hyprial reply`；`--json` 时 stderr 为空，字段在 stdout JSON 里；字段缺失或为 `[]` → stderr 为空 | 现状 stderr 为空（断言含 `m1` 失败） | 同上 |
| T11 | `read` 的消费语义：`hyprial read m1 --from <canonical>` 只发一次 IPC，方法名 `== IPC_MESSAGE_READ_BODY`，参数 `{"from", "messageId": "m1"}`；人读 stdout 恰好是 `body + "\n"`（含多行与非 ASCII 正文），没有 summary 与标题；`--json` 原样带 `ok`；没有 `body` 键时 stdout 是 `<body not text: N bytes; use --json>` | 现状 `read` 不存在，退出 2（`No such command`） | `tests/shell/cli/links/test_cli_read.py` |
| T12 | `read` 与 `query` 分开：调用 `hyprial query …` 时 fake daemon 从不收到 `message.read_body`；`read --help` 文本含 `fetched` 与 `reply or ack` | 现状 `read --help` 退出 2 | 同上 |
| T13 | `link list/stats/add/drop`：各自发出对应 IPC 常量与参数（`from`、`peer`）；`HYPRIAL_WORKER_ACTOR`/`HYPRIAL_WORKER_SESSION_REF` 与 `--from` 一致时带 `sessionRef`；人读 `list` 含 owner 行、`k 4`、`W 6`、表头；JSON 原样；`add` 的 hold 返回按 hold 渲染、退出 0；`ok:false` 退出 1 | 现状 `link` 不存在，退出 2 | `tests/shell/cli/links/test_cli_link.py` |
| T14 | `link` 渲染对缺失字段的容忍：只给 `{"ok": true}` 时 `list` 与 `stats` 人读都不抛异常、含 `—`；`links` 行缺 `cwnd` 时该格为 `—` | 现状 `render` 模块不存在（`ImportError`） | 同上 |
| T15 | top 字段缺失时的占位：现有 `_snapshot()` 不带三个新字段 → 表头含 `W`、`OUT`、`INJQ`，每行这三格都是 `—`，退出 0；带 `capacityW: 6, outboundInFlight: 2, inboundAwaitingInjection: 0` 时显示 `6 2 0`（0 不能显示成 `—`）；值为 `None`、`"6"` 或 `True` 时显示 `—`；`--json` 不出现这三个键 | 现状表头没有 `W`（断言失败） | `tests/shell/cli/lifecycle/test_cli_top.py`（追加；该目录已满 7 个文件，不能新建） |
| T16 | MCP 多目标包装：用 `contract/mcp-tools/run.py` 同款的 fake factory 与 `InMemoryTransport`：`to="agent:a"` → IPC `to == ["agent:a"]`；`to=["agent:a","agent:b"]` → 原样；`to=[]` 与 `to=[""]` 是工具错误且零 IPC；缺 `summary` 是工具错误且零 IPC；`summary` 出现在 IPC 参数里 | 现状列表 `to` 被 schema 拒绝（`is_error`），缺 summary 照发 | `tests/daemon/channels/mcp/links/test_mcp_send_links.py` |
| T17 | MCP `harness_read_body`：工具存在；调用发出 `message.read_body`、`{"messageId"}`、mutation=True；`harness_reply` 带 summary；`harness_read` 原样透传 fake 返回的 `{"messages": [{"summary": "s", "bodyBytes": 9}]}`，不加 `payload` | 现状工具不存在（`list_tools` 里没有） | 同上 |
| T18 | 允许列表：`"harness_read_body" in WORKER_HARNESS_TOOLS` 且 `in _CLAUDE_HARNESS_TOOLS` | 现状不在 | 同上 |
| T19 | pi 桥工具面对等：注册的工具含 `harness_read_body`；`harness_send` 带数组 `to` 时原样、字符串时包成数组；`summary` 进入 `message.send` 参数 | 现状工具清单断言失败 | `tests/daemon/harnesses/pi/extension/test_pi_harness_bridge_extension.py`（追加） |
| T20 | daemon `link.*` 路由：四个方法在 router allow-list 里，且就是 ports 常量（`assert IPC_LINK_LIST in <allow-list>`）；`_link_service` 为 None → `DAEMON_NOT_READY`；注入假服务（实现 `list_links`/`stats`/`add`/`drop`）后 `link.list` 返回 `{"ok": True, **mapping}`；`link.add` 在 caller 未通过 `_authenticated_message_caller` 时被拒，且不调用 `add` | 现状 `METHOD_NOT_FOUND` | `tests/daemon/ipc/links/test_link_ipc_routes.py` |
| T21 | 命令清单：`test_whole_registered_command_inventory_stays_on_native_app` 子集含 `link`、`read`、`reply`，不含 `dispatch`；`test_help_inventory_matches_registered_top_level_tree` 的 `link` 子命令集合等于 `{"list","stats","add","drop"}` | 前者现状本就红（dispatch，见 §0）；加 `link` 后在实现前仍红；后者 `KeyError: 'link'` | `tests/shell/entrypoints/test_shell_cli_entry.py`、`tests/shell/cli/presentation/test_cli_help_inventory.py` |
| T22 | help 覆盖：`test_undocumented_parameters_do_not_grow` 保持 54 | 新参数漏写 help 时变红 | `tests/shell/cli/presentation/test_cli_help_coverage.py`（不改，只跑） |
| T23 | MCP 契约脚本：`mcp-tools: PASS 32/32` | 现状输出 `31/31` | `contract/mcp-tools/run.py` + `tests/shell/compatibility/test_unreachable_surface_retirement.py` |
| T24 | 模块边界：shell 不 import `hyprial.daemon.impl`（`check_module_boundaries.py` 通过）；`from hyprial.daemon import IPC_LINK_LIST, IPC_MESSAGE_READ_BODY, HOLD_REASONS, SUMMARY_MAX_BYTES` 可用，且与 ports 中的对象是同一个（`is`） | 现状 `AttributeError` | `tests/shell/cli/links/test_cli_link.py`（facade 断言一条） |

已有测试同步（§1.8 所列文件）不算新测试，但必须全绿。

## 4. 验收标准（可机检）

全部在 worktree 根执行，必须全部为真：

1. 焦点测试全绿：
   `PYTHONPATH=$PWD/src uv run --extra test pytest tests/shell/cli/links tests/shell/cli/output tests/shell/cli/presentation tests/shell/cli/communication tests/shell/cli/lifecycle/test_cli_top.py tests/shell/entrypoints tests/shell/compatibility/test_unreachable_surface_retirement.py tests/daemon/channels/mcp tests/daemon/ipc/links tests/daemon/harnesses/pi/extension tests/daemon/links tests/daemon/workers/test_worker_identity.py -q` → 0 failed（含 §0 的基线红一并转绿）。
2. 全量：`uv run --with libcst==1.9.0 pytest tests/` 的失败集合不比集成分支基线多（PR 正文贴两边的失败列表）。
3. `uv run ruff check .` 退出 0。
4. `uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py` 与 `… check_module_boundaries.py` 退出 0。
5. `uv run python contract/mcp-tools/run.py` 输出恰为 `mcp-tools: PASS 32/32`。
6. `./contract/run-all.sh --isolated-only` 退出 0（含 cli-blackbox 新增的 `isolated send without summary fails locally`）。
7. `grep -rn "hyprial.daemon.impl" src/hyprial/shell/impl/cli/commands/link src/hyprial/shell/impl/cli/commands/message/read.py` 无输出。
8. `grep -n '"link\.\(list\|stats\|add\|drop\)"\|"message.read_body"' src/hyprial/shell src/hyprial/daemon/impl/ipc/router.py src/hyprial/daemon/impl/mcp/server.py` 无输出（方法名只用常量；ts 文件除外，它不能导入 Python 常量，由 T19 守）。
9. 结构计数：`find src/hyprial/shell/impl/cli/commands/message -maxdepth 1 -type f | wc -l` ≤ 7；`wc -l` 下 `send.py`、`top.py`、`router.py`、`server.py`、`entry.py` 都 ≤ 1000。
10. 「本地即报」在无 daemon 的已初始化隔离 home 上成立：由第 6 条中 cli-blackbox 的 `isolated send without summary fails locally` 检查判定（`code == "SUMMARY_REQUIRED"`，不是 `DAEMON_UNAVAILABLE`，退出 1）。
11. `hyprial --help`、`hyprial help --json` 列出 `link` 与 `read`；`hyprial link --help` 列出四个子命令。

## 5. 要留的证据

放 PR 正文（必要时附 `docs/notes/send-links-wp6-2026-10-*/`，WORM 只追加）：

1. **先红**：测试先单独成一个提交（`test: WP6 … (red)`），贴该提交上第 1 条焦点命令的输出摘要，逐条对应 §3 的先红形状（T9 标注为护栏）。
2. **后绿**：实现提交上同一命令的输出，以及第 2–6 条命令的尾行（ruff、结构门、边界门、`mcp-tools: PASS 32/32`、`run-all.sh --isolated-only`）。
3. **人读样例**：用 fake daemon（测试里的数据）跑出的 `link list`、`link stats`、hold 情形的 `send`、`read`、缺字段的 `top` 的 stdout 原文各一段，证明 §2 的形状。
4. **基线对照**：§0 的基线红（dispatch）在本 PR 转绿，写明是因为清单测试更新，不是改了 dispatch 行为。
5. PR 正文必须有两行：`Contract-Impact: …`（`harness_send`/`harness_reply` 参数、新 MCP 工具、`message.send` 结果新增顶层 `queued`/`reason` 投影、新 IPC `link.*`；wire golden 与 skill 由 WP8 同步）与 `Skill-Impact: deferred to WP8 (hyprial-ops send section, squire SKILL-template send examples, MCP tool docs)`。
6. 未实测项显式标注：真实 daemon 上 link 与 hold 的端到端行为 **BLOCKED**（等 WP2–WP4 接线；由 WP8 E2E 覆盖）；不能用 fake 结果当 PASS。

## 6. 对缝的建议（评审与 WP0/WP2/WP3/WP7 负责人参考）

1. **add/drop 没有协议**。`LinkQueries` 只有 `list_links`、`stats`，没有 `link.add`/`link.drop` 要调用的方法。建议在 `ports.py` 增加 `LinkAdmin` 协议：`add(*, actor: str, peer: str, now_ms: int) -> Mapping[str, Any]`、`drop(*, actor: str, peer: str, now_ms: int) -> Mapping[str, Any]`（只增不改，`runtime_checkable`），由 WP2 的 service 实现、composition 赋给 `DaemonApplication._link_service`。若 WP0 负责人不加，本包在 ports.py 里加（纯增量，并在 `test_wp0_seams.py` 加一条 isinstance 断言）。
2. **IPC 方法名对 shell 不可达**。ports 常量在 `daemon.impl`，shell 不许 import。本规格在 `daemon/interface.py` 的 facade 里导出（§1.5）。若希望 shell 不经 daemon 公共面，备选方案是把方法名移到 `kernel`（与 `ipc_errors` 并列），ports 再从 kernel 引用。
3. **铸造态没有原因码**。`Mint` 与 `Hold` 分开，但 `send` 的返回里铸造中的消息该写什么 `reason`，ports 没定。建议 WP3 对 Mint 的 delivery 写 `accepted: true, queued: true, reason: "LINK_MINTING"`（或加 `linkMinted: true`），并把这个值加入 kernel `ipc_errors` 和 `contract/`。CLI 按「任何 queued 都是 held」渲染，不依赖具体值。
4. **hold 的字段名要统一**。现有 send 已经对 `queued` 写了 `reason: "target-not-visible"`（kebab 字符串）。hold 复用 `reason` 键但放的是错误码，两种值混在一个键里。建议 WP3 保持 `reason` 放码，并把 `target-not-visible` 迁成码，或为 hold 另用 `holdReason`；无论选哪种，WP3 与本包的 `_lift_hold` 都必须用同一个键，评审时核对。
5. **SUMMARY_REQUIRED 的 data 键**。ports 写「measured size in data」但没给键名。本包用 `{"summaryBytes", "maxBytes"}`；请 WP3 采用同名，使 CLI 本地报错与 daemon 报错形状一致。
6. **`openRequestsFrom` 的位置**。设计写的是 send 返回字段，但没说在顶层还是在每个 delivery。fan-out 时只有放在每个 delivery 上才知道是谁的待回复请求。建议 WP7 放在 delivery 上，值是 messageId 列表；CLI 两处都读。
7. **top.snapshot 的三个字段没有归属**。README 的 WP 表里没有任何包负责改 `daemon/impl/operations/top.py`。建议：WP2 加 `capacityW`、`outboundInFlight`，WP4 加 `inboundAwaitingInjection`，字段名按本文 §2.4。
8. **pi 桥工具面**。`pi_harness_bridge.ts` 的 `harness_send`/`harness_reply` 直接调 `message.send`/`message.reply`。WP3 让 summary 必填后，它们不补 summary 就全部失败。本规格把它放进 WP6（§1.7），评审可改划给 WP5，但必须有人负责，并与 WP3 同批合并。
9. **summary 必填的生效顺序**。WP3 在 daemon 端要求 summary 之后，旧 CLI、旧 MCP、squire 模板里的命令都会失败。建议 WP3 与 WP6 同批合入集成分支，WP8 在同一窗口更新 skill 与 squire SKILL-template（design §11 的硬切）。

## 7. 评审

结论：**BLOCKING:6 ADVISORY:5**。以下修正规格覆盖正文的同主题条款。
评审基线为 `fda6346f9`。依据为主设计、实施目录 README 的裁定及当前 `ports.py`。

### B1｜阻断：hold 键与原因集合不符合已裁定的接口

位置：§1.2、§2.1、§2.2、§2.5、T6–T8、§5.5、§6.3–§6.5。
正文仍把 link hold 码写入 `reason`，并把原因集合描述为 3 个。
主设计 §9 和 README 裁定 14–16 已固定 `holdReason` 与 4 个原因。
`reason` 已承载 `target-not-visible`，不得改其语义。

**修正规格：**

- Python 消费方必须使用公共面的 `HOLD_REASON_KEY` 和 `HOLD_REASONS`。
- link hold 返回必须是 `ok: true, queued: true, holdReason: <码>`。
  4 个码为 `LINK_MINTING`、`LINK_PEER_FULL`、`LINK_SELF_FULL`、`LINK_WINDOW_FULL`。
- `_lift_hold` 仅在顶层缺失时补 `queued`。
  仅有 1 条 delivery 且该条 queued 时，才可补顶层 `holdReason`。
  多目标结果不得把某条 delivery 的原因推广为全部目标的原因。
- CLI 必须保留 daemon 已给的 `reason`、`holdReason`、错误码和结果字段。
  人读 link hold 必须优先显示 `holdReason`。
  普通排队的自由字串 `reason` 可作为兼容回退。
- `link add` 的 hold、MCP 和 pi 桥返回同用此字段。
  MCP 和 pi 桥不得把 hold 改为工具错误。
- T6 必须遍历实际 `HOLD_REASONS`，断言集合长度为 4。
  T7 必须覆盖 `reason: "target-not-visible"` 与 `holdReason` 并存。
  另测多目标各自 hold 原因、顶层已存在值及幂等投影。
  同步 JSON 示例、测试 fixture 和 Contract-Impact 描述。

### B2｜阻断：规格要求重做已经提交的公共面和协议

位置：§1.5、§1.6、T20、T24、§6.1–§6.9。
`LinkAdmin`、方法名、summary 数据键和 top 字段常量已经存在。
`daemon/interface.py` 已导出这些名称。
README 裁定 21 明确要求 WP6 不再添加这些导出。
此外，router 的 `_IPC_STATS_METHODS` 是计费统计清单，不是授权白名单。

**修正规格：**

- §1.5 改为使用现有 facade；不得重复修改 `interface.py` 的这些条目。
  不得新增或改名 `ports.py` 的协议、常量和方法名。
- shell 必须从 `hyprial.daemon` 取方法名、hold 键、summary 上限及 top 字段常量。
  shell 不得从 `daemon.impl` 导入。
- `link.*` handler 必须调用 `LinkQueries` 与 `LinkAdmin` 的现有方法。
  不得让 CLI 直接调用 service、store、orgfs 或 inbox。
  links 不得因本包接入而导入 inbox service。
- T20 改为检查 router 实际 dispatch 到假服务。
  `_IPC_STATS_METHODS` 的成员测试仅检查统计登记，不代表权限检查通过。
- T24 的公共面同一性断言保留为护栏。
  当前基线已经可导入这些名称，不得记录成新功能的先红证据。
- §6 中已裁定的备选方案停止实施。
  pi 桥工具面归 WP6；top 字段由 WP2、WP4 提供。
  WP6 与 WP3-A 可先提供 summary；WP3-B 等 WP3-A、WP5、WP6 合并后硬切。

### B3｜阻断：summary 校验会改变发送方文本并漏掉非法 UTF-8

位置：§1.2 的 `_require_summary`、§2.2、T3。
先去首尾空白再计字节，会把超长输入变成可接受输入。
返回去空白后的值也不符合 T3 的“参数原样”要求。
直接 `encode` 还可能泄漏 `UnicodeEncodeError`。
主设计 §7 的 512 字节限制针对发送方提供的 summary。

**修正规格：**

- `_require_summary` 可用 `value.strip()` 判断是否全空白。
  字节计数必须针对原始 `value`，返回值也必须是原始 `value`。
- 缺失、全空白和不能编码为 UTF-8 的值必须报 `SUMMARY_REQUIRED`。
  不得让编码异常变成 traceback 或其他错误码。
- 超过 `SUMMARY_MAX_BYTES` 时必须返回原始值的 UTF-8 字节数。
  `data` 必须为 `{summaryBytes: n, maxBytes: SUMMARY_MAX_BYTES}`。
  不得截断、去空白后放行或自行生成 agent summary。
- `send` 与 `reply` 都必须在各自 operation 内校验。
  校验失败时不得发 `message.send` 或 `message.reply`。
  身份别名解析的只读 IPC 不属于消息发送。
- 扩展 T3、T5：原始值含首尾空白且恰为 512 字节时，参数逐字相同。
  原始值为 513 字节、去空白后不足 512 字节时，仍必须拒绝。
  非法 surrogate 的纯函数测试必须得到 `SUMMARY_REQUIRED`。

### B4｜阻断：link 管理调用漏掉必填时间，owner 操作没有 actor 选择路径

位置：§1.1、§1.6、§2.1、§2.6、T13、T20。
`LinkAdmin.add/drop` 的 `now_ms` 是必填参数，正文调用未提供。
`_authenticated_message_caller` 对 owner 默认返回 `subject == user:<owner>`。
正文没有 `--on-behalf-of`，owner 因而不能选择要管理的 agent。
直接伪造 `--from <agent>` 也不能代替既有 session 围栏。

**修正规格：**

- `link add/drop` 必须提供可选 `--on-behalf-of <local-agent>`。
  参数必须经 `_message_claim_params(source, on_behalf_of=...)` 传递。
  operator 形式为 `--from user:<owner> --on-behalf-of <local-agent>`。
- handler 必须先调用 `_authenticated_message_caller`。
  operator 使用经核实的 `caller.subject`；session 使用其绑定 actor。
  被管理的 actor 必须是本 daemon 的规范 agent URI。
  不得把 adapter、channel 或未选择 agent 的 owner 写成 agent link 文件。
- 不得从 IPC 参数读取可伪造的 trusted origin。
  不得新增绕过 session 围栏的本机 agent 操作路径。
  CLI 身份核实强度仍沿用主设计 §12 的开放项。
- handler 必须在 daemon 边界获取当前毫秒时间。
  调用必须为 `add/drop(actor=actor, peer=peer, now_ms=now_ms)`。
  控制器和 policy 不得为此读取时钟。
- T20 的假服务必须使用与 `LinkAdmin` 相同的关键字签名。
  必须断言 add、drop 都收到规范 actor、peer 与整数 `now_ms`。
- 增加 owner 代本机 agent、worker 自己管理 link 的成功测试。
  增加无效 session、session 与 actor 不符、foreign actor 的拒绝测试。
  拒绝时不得调用 admin 方法。
  add/drop 的鉴权必须各测一次，不能只测 add。

### B5｜阻断：§4 边界和 §5.6 劣化场景没有工具面的回归测试

位置：§3 的 T6–T19、§4、§5.6。
现有测试主要验证参数和 happy path，没有证明工具面不会替 daemon 授权或重发。
以下测试只验 CLI/MCP/pi 的边界，控制器算法仍由 WP1、WP2 验收。

**修正规格：**

1. **目标与调用路径（设计 §4.1、§4.6）：**
   CLI、MCP、pi 桥必须覆盖 own-owner `user:`、其他 `user:`、`agent:` 和 `route:`。
   工具面必须保留 daemon 对各目标的决策。
   不得根据 native/hosted、目标类型或本地状态自行授权、铸造或豁免。
   假 IPC 每次只收到预期的发送调用，不得收到隐式 `link.add`。
2. **回复与排干（设计 §4.4、§4.5）：**
   分别测试普通 send、`send --reply-to` 和显式 reply。
   普通 send 即使带 `openRequestsFrom`，也不得自动补 `replyTo`。
   `LINK_RECLAIMED` 和 `MESSAGE_REPLY_UNAVAILABLE` 必须原样返回失败。
   失败后不得补发 send、ack 或 link.add。
   MCP 与 pi 桥必须同测 reply summary 的传递。
3. **body 消费（设计 §7）：**
   CLI read 和 harness_read_body 成功时只能调用 `message.read_body`。
   不得调用 ack、reply 或 send；MCP/pi 的调用必须带原绑定身份。
   测试不存在及无权读取时的 `MESSAGE_BODY_UNAVAILABLE` 透传。
   CLI 的 managed worker read 必须携带 sessionRef。
   fetched、pending 是否真的变化由 WP3 的 daemon 测试证明。
4. **分区、过期副本、link 抖动、冷启动（设计 §5.6）：**
   用 4 种 hold 结果分别测试 CLI、MCP、pi 的成功排队形状。
   fake 连接未中断时，一次操作只能发 1 次发送调用。
   收到 hold 后，工具面不得轮询、补发或重新铸造。
   既有传输重连可保留，但必须沿用同一 request ID。
   1 个目标 held、另 1 个已投递时，必须保留两条 delivery 的 messageId 与原因。
5. **队头阻塞与 TTL（设计 §4.3、§5.6）：**
   hold 后退出，再执行只读观察时，工具面不得重发旧请求。
   观察到 expired 必须保留终态，不得显示仍可自动重发。
   send 的人读提示改为 `the daemon re-evaluates this queued message; do not resend`。
   提示必须同时说明等待会受 TTL 限制。
   该提示只用于 queued 消息，不用于已经 expired 的结果。
6. **优先级反转（设计 §3.2、§5.6）：**
   fake 返回容量已满时，工具面仍必须提交 owner 发送和显式回复给 daemon。
   不得让 CLI/MCP/pi 自行读取 k、W 或 cwnd 后拒绝操作。
7. **负载导致服务率下降、非拥塞慢、后来者不公平、审查偏差：**
   stats/top 渲染测试必须保留 daemon 给出的 0、负梯度、收缩值与缺样本。
   shell 不得重新计算 W、cwnd、T 基线或放弃阈值。
   这 4 项的算法实测结果必须引用 WP1、WP2 或 WP8 证据。
   fake 渲染通过不得记成算法或真实网络 PASS。

增加测试可放入 §1.9 的新子目录，但必须保持每目录最多 7 个文件。
单元测试记录必须写明 fake 的边界。
未接线的真实 daemon 场景仍必须记为 BLOCKED。

### B6｜阻断：outbox 的等待时长没有实施归属

位置：§1.8 末段与 §4；主设计 §9。
正文把原因与等待时长交给通用渲染，却没有规定谁提供等待时长。
当前 `_outbox_entry_json` 只给 `createdAtMs` 等时刻，未给等待时长。
通用渲染不会把时刻转换成等待时长。

**修正规格：**

- WP6 必须在现有 `commands/message/outbox.py` 中增加 list 人读渲染。
  必须通过 `CliResult` 输出 messageId、目标、等待原因与等待时长。
  不必新建 `message/` 下的直接文件。
- 原因必须取 daemon 提供的 `holdReason`。
  非 link 排队可回退到既有 `reason`；缺失时显示 `—`。
- 等待时长可用 daemon 提供的时长。
  未提供时，可用当前时刻减 `createdAtMs`，下限为 0。
  无有效时刻时必须显示 `—`，不得显示伪造的 0。
- `--json` 必须原样透传，不补人读计算字段。
  观察不得触发 send、add、drop、prune 或自动重发。
- 增加 fake outbox 测试：LINK_MINTING、LINK_WINDOW_FULL、缺字段和 expired。
  冻结渲染时钟后断言等待时长，并断言只调用一次 `outbox.list`。
  holdReason 的实际数据接入由 WP2 提供；未接入时不得记为端到端 PASS。

### A1｜建议：固定 IPC 返回形状，缺字段容忍不能代替接线验收

§2.1 的 list/stats 与 §2.3 的 body 形状都是本包提出的形状。
`LinkQueries` 只承诺 Mapping，未固定这些 JSON 字段。
宜在接线验收中用 WP2、WP3 的真实 IPC 返回运行渲染测试。
特别核对 `W`、`inFlight`、`nActive`、`body` 及吞吐时间单位。
top 必须使用 ports 已固定的 3 个字段常量。
没有真实数据时，记录 BLOCKED，不能用全 `—` 渲染证明功能完成。

### A2｜建议：扩大既有调用点同步清单

§1.8 漏列了直接调用 adapter 的生命周期测试。
例如 `tests/daemon/channels/mcp/lifecycle/test_channel_waits_on_doorbell.py:314`
仍调用 `adapter.harness_reply("m-1", "done")`。
`test_mcp_delegate_removed.py`、owner migration 及 authenticated sender 测试也有旧参数。
宜检索所有实际 harness_send/harness_reply 调用，补 summary 后跑对应焦点测试。
只描述旧调用的历史证据不得改写。
契约的 `32/32` 必须来自真实断言数量，不能只改输出字符串。

### A3｜建议：正文来自文件的 MCP 等价性尚未说明

主设计 §8.3 写了补齐“多目标、正文来自文件”后 CLI 与 MCP 等价。
本规格只新增多目标与 summary，未说明文件正文如何进入 message。
宜明确 agent 先用本地文件读取工具，再把完整文本传入 message。
若要求独立的文件输入参数，必须先固定其权限和实施归属。
不得为补齐文件输入开放 worker 对 daemon 主机任意 `resourcePaths` 的访问。
当前 worker channel 明确排除了这种主机文件读取能力。

### A4｜建议：link 数显示区分计入 k 的常规边

§2.1 把所有 established 边计入 `agent links a/b`。
README 裁定 7 规定 PAC 任务绑定边不计入 k。
宜把 a 定义为计入 k 的 established 常规边数量。
owner 与任务绑定边可另列数量；在途仍采用 daemon 的 cwnd、W 统计。

### A5｜建议：read 换行测试与输出器行为保持一致

§2.3 说明只在缺换行时补换行，T11 却无条件要求 `body + "\n"`。
当前 `_print_human` 会保留已经存在的末尾换行。
宜分别测试无末尾换行、有 1 个或多个末尾换行、空正文和非文本结果。
不得通过修改全局输出器让 read 的测试成立。

### 评审验证记录

- PASS：`PYTHONPATH=$PWD/src uv run --extra test pytest tests/daemon/links/test_wp0_seams.py tests/daemon/ipc/error -q`，10 passed。
- PASS：结构门，3035 个文件、878 个模块；模块边界门，status 为 PASS。
- ruff：文档路径检查退出 0；Markdown 不在 Python lint 范围内，未检查代码。
- PASS：`git diff --check`；新增评审节的 STC 检查，0 处需修改、0 处待确认。
- 本次仅追加规格评审，未修改产品、测试、协议或公共面。
- 未执行 WP6 实施后的焦点测试和真实网络场景；其结果为 UNKNOWN。
- 依赖方向核查未发现正文要求 links 导入 inbox service。
  shell 经 daemon 公共面和 IPC 的方向正确；实施必须继续保持。
