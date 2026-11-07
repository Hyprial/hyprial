# host 术语映射（A-04 prepare）

日期：2026-10-28。基线：`intg/cli-command-surface@1f2cfc631`（A-04 prepare）。
来源：`handoff-2026-10-27.md` §7 的 host 术语约定、`cli-surface.md` §4 的用户零兼容要求。

本文件只记录**目标映射与归属**，A-04 不执行整仓改名。实际改名随 B/C 各命令组迁移
同批完成；每一组都必须同步自己的帮助文字、参数、输出字段、`contract/` 快照和消费者。
A-04 的生成清单 `docs/reference/cli-command-inventory.json` 只记录当前真实注册，
不把本表的目标名当成已实现名称。

## 规则

1. host 是名词：运行 hyprial daemon 的环境。用户可见的 CLI 名词、参数名、帮助文字、
   输出字段和文档统一用 host。
2. `cli-surface.md` §4 的用户零兼容要求优先于交接文档的“旧参数保留一版别名”。
   迁移时直接使用新名；不新增旧 `--machine`/`--node`/`--device` alias。
3. 已发布 JSON 字段或参数名改名必须同步 `contract/`，并在 PR 写 `Contract-Impact`。
4. 不把产品领域自己的 node 改成 host；不为改名单独动未迁移模块。
5. 归属以命令族所在迁移组为准；B 的 `agent move` 由 B 提供 MethodSpec/handler/薄 CLI，
   A 只提供共享注册与清单核对。

## 不映射：同名但不同领域

| 当前用词 | 领域含义 | 处置 | 归属 |
| --- | --- | --- | --- |
| `fs ls/read/mv/rm/history/export/trash/restore/import` 的 `node`、`id:<nodeId>` | OrgFS 文件/目录节点 | 保留；不是 host | C DR-14 |
| `work show/resolve` 的 `id:<nodeId>` | OrgFS 冲突副本节点 | 保留；不是 host | C DR-15 |
| `workflow inspect --node`、`workflow reset/complete/fail NODE_ID`、`graph` 的 `nodeId` | PAC 图节点 | 保留；不是 host | C DR-16 |
| `workflow overview` 的 `nodes/` 目录与 graph node 文案 | PAC 图节点 | 保留；不是 host | C DR-16 |
| `login` 的 `device` 阶段、`adapter scopes/user-auth` 的 `device_authorization` | OAuth 2.0 device authorization flow | 保留；不是 host | C DR-23 / DR-19 |
| `routine templates` 的 “Machine-readable …” | machine-readable（形容词） | 保留 | C DR-17 |
| `service whois` 的 “node key” 需在迁移时核对是否 host 身份密钥 | 待权威模块确认 | 迁移时判定，不在 A 预改 | B04 / DR-06 |

## 逐项映射

| 当前用词（真实注册/源码） | 目标 | 所属迁移组 | Contract-Impact |
| --- | --- | --- | --- |
| `host` 组 help “register machines as move targets”；`host list` 的 “nodes/NODE/nodeId” | host / hosts / hostId | C DR-07 | 帮助文字与文本表头；JSON `nodeId` 改名需同步 contract |
| `agent create/list/stop/grants`、`agent home/config` help 的 “this machine” | this host | C DR-18 | 仅帮助文字 |
| `agent` 规范地址 `agent:<owner>:<machine>:<actor>`、`squire` 的 `channel:<owner>:<machine>:<adapter>` | `<host>` 段 | C DR-18 / DR-20 | 地址语法与解析消费者；迁移组同步 contract |
| `agent runtime interactive` 读取的 `nodeId`、canonical URI 错误文案 | `hostId` / host | C DR-18 | 输出字段与错误文案 |
| `adapter pin` 的 “on this machine”、URI `<machine>` 段 | host | C DR-19 | 参数帮助、地址语法 |
| `adapter route candidates` 的 “machine-side answer” | 本地 daemon/host 侧 | C DR-19 | 帮助文字 |
| `doctor` 的 `device-key` 检查名、device record/device id 文案 | `host-key` / host record / host id | B06 / C DR-24 | 诊断字段名与 `doctor --json` 需同步 contract |
| `doctor` 的 “node identity”/“another node id”/`nodeId` | host identity / host id | B06 / C DR-24 | 诊断文案；字段改名同步 contract |
| `daemon top` 的 `node`/`nodeId` 列与文案 | host/hostId | C DR-21 | 文本表头与 JSON 字段 |
| `init`/`onboarding` 的 “machine state/current read-only state”、孤立 node 文案 | host state / isolated host | C DR-22 | 帮助与输出文案 |
| `login` 的 `device key`、`deviceId`、`DEVICE_KEY_*`（不含 OAuth `device` 阶段） | host key / hostId / `HOST_KEY_*` | C DR-23 | 状态码/字段改名需同步 contract；OAuth device 不动 |
| `message` 规范地址 `<node>` 段、trajectory 的 “cross-node” | host / cross-host | C DR-13 | 地址语法、轨迹文案/字段 |
| `org` 的 `devicePublished`、`deviceId`、devices 列、“this node belongs to” | hostPublished / hostId / hosts / this host | C DR-27 | JSON 字段与文本输出；同步 contract |
| `org network` 的 “members/devices, mapped peers”、`org fetch --node` | hosts / host target | C DR-27 | JSON/帮助 |
| `profile list` 的 “this machine's network profiles” | this host's | C DR-12 | 帮助文字 |
| `service` 的 “this machine publishes”、`deviceId` 表头/指南 | this host publishes / hostId | B04 / C DR-06 | 文本渲染；JSON 字段同步 contract |
| `squire setup --machine/--machine-key`、错误 “node” | `--host/--host-key`、host | C DR-20 | 参数名改名；同步 contract 与消费者（按零兼容，不保 alias） |
| `agent transfer bundle-export --machine`、`trust-source-add --source-machine`、`source_machine`/`sourceMachine` | `--host/--source-host`、`source_host`/`sourceHost` | C DR-28 | CLI、bundle manifest、trust 记录；迁移时同步 contract |
| `agent transfer credential-*`、`recipient-key` 的 “target machine/this machine” | target host / this host | C DR-28 | 帮助与记录字段 |
| 旧 `agent move --to` 的 “exact nodeId from this home's hyprial host list”、`another machine` | host/hostId（`--to` 仍是 agent 目标，不改 `--host`） | B move | 旧命令由 B 的 8 动作面替换；输入契约以 B 为准 |
| `upgrade` 的 “this machine's desired state/fleet” | this host's | C DR-25 | 帮助/通知文案 |
| `user` 的 “node owner” | host owner | C DR-08 | 帮助文字；字段改名同步 contract |
| `fs space list`/`daemon top` 的 “this node” 若指运行环境 | this host | C DR-14 / DR-21 | 仅当语义是 daemon 环境；OrgFS 节点保留 |
| `fs web` 的 “nodekey” | 迁移时确认是 host 密钥还是 OrgFS 节点键 | C DR-14 | 判定后再改；不在 A 预改 |

## 与 A-04 清单的关系

- `docs/reference/cli-command-inventory.json` 是当前注册树（含 `agent move` 旧命令、
  `agent transfer` 隐藏协议面、`daemon upgrade` 隐藏入口）的唯一生成快照。
- 本表中的目标名尚未进入注册树；未实现的新 `agent move` 8 动作不会因为本表出现在
  可用命令目录。
- A-03 接入真实 MethodSpec/OpenAPI 后，a04-build 再核对参数名与导出；本 prepare
  节点不伪造 schema。
