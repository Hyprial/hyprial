# 迁移：`adapter.route-config.*` 退役（F1a，2026-11-23）

`adapter.route-config.list|candidates|add|remove` 是只走本机 IPC 的兼容孪生请求，
只给 `hyprial adapter route` 使用。它已删除，`adapter.routes.*` 是唯一的路由配置请求面。
没有迁移代码；按下面的说明改调用方即可。

## daemon 请求

| 已删除 | 改用 | 差异 |
|---|---|---|
| `adapter.route-config.list {name?}` | `adapter.routes.list {name}` | `name` 必填；一次只返回一个 `adapter`。列全部 adapter 时先 `adapter.list` 再逐个调用。 |
| `adapter.route-config.candidates {name}` | `adapter.routes.candidates {name}` | 未配置的 adapter 返回 `ADAPTER_NOT_FOUND`（原来返回空列表）；不可读的观察库返回 `DAEMON_ERROR`（原来当作空）。行多一个 `chatName`，结果多一个 `namesStatus`。 |
| `adapter.route-config.add {name, route, chatId, makeDefault, force}` | `adapter.routes.add {name, route, chatId, makeDefault, expectedRevision?}` | `expectedRevision` 可选（2026-11-24 起）：传入时与当前 `revision` 不符返回 `ADAPTER_CONFIG_CONFLICT`；不传则不做该检查，路由名已存在仍返回 `ROUTE_EXISTS`。不再支持 `force` 改绑已有路由名，见下文。 |
| `adapter.route-config.remove {name, route, force}` | `adapter.routes.remove {name, route, clearDefault, expectedRevision?}` | `force` 改名为 `clearDefault`；`expectedRevision` 同样可选，未绑定返回 `ROUTE_NOT_FOUND`，默认路由不带 `clearDefault` 返回 `ROUTE_IN_USE`。 |

调用已删除的方法名，得到注册表对未知方法的标准错误 `METHOD_NOT_FOUND`。

## `hyprial adapter route` CLI

每个子命令只发一个 daemon 请求。命令名基本不变，下面几项有变化：

- **`route add --force` 已删除。** 路由名已存在时返回 `ROUTE_EXISTS`，提示先执行
  `hyprial adapter route remove <adapter> <route>`，再重新 `add`。如果它是默认路由，
  `remove` 需要 `--force`，重新添加时加 `--default`。两步分开执行：中途失败不会悄悄丢掉路由。
- **`--json` 输出改为 `adapter.routes.*` 的结果：**
  - `list`：`{"ok": true, "adapters": [...]}`。每个 adapter 带 `revision`、`namesStatus`，
    每条路由带 `chatName`；去掉 `channelsPath` 与 `credentialRef`；`defaultRoute` 未设置时为 `null`，不再省略。
  - `add` / `remove`：`{"ok", "adapter", "configured", "reloaded", "nextAction"}`；去掉
    `channelsPath`、`daemonReload`。`reloaded: false` 时 `nextAction` 为 `"reload"`。
  - `candidates`：同 `adapter.routes.candidates`（见上表）。
- **`route list` 必须给出 adapter 名**（`hyprial adapter route list <adapter>`）。daemon 没有一次返回全部
  adapter 路由的请求，CLI 不再逐个 adapter 循环查询。要列全部，先 `hyprial adapter list` 再逐个查询。
  不带名字时是用法错误（非零退出）。
- `route add` / `route remove` 只发一个写请求，不带 `expectedRevision`；路由名已占用、未绑定、
  默认路由未加 `--force` 仍由 daemon 在写锁内拒绝。
- `list` 与 `candidates` 会尝试从平台读取会话名（`chatName`），读不到时 `namesStatus`
  为 `partial` 或 `unavailable`，路由本身照常返回。`add` 与 `remove` 不做会话名读取。
- `ROUTE_NOT_FOUND` / `ROUTE_IN_USE` 的错误码不变；`ROUTE_NOT_FOUND` 的提示改为指向
  `hyprial adapter route list <adapter>`（不再列出已绑定的路由名），`ROUTE_IN_USE` 提示 `--force`。
- **adapter 必须已被运行中的 daemon 加载。** 路由命令按 daemon 当前的 adapter 注册表校验名字，
  不再直接读 `channels.json`。如果 `channels.json` 里有 adapter 但 daemon 还没加载（例如
  `hyprial adapter add` 写入后热加载失败），`route list|add|remove|candidates` 返回 `ADAPTER_NOT_FOUND`；
  先执行 `hyprial adapter reload`。

## 不变的部分

`route_registration`、`channels.json` 的格式，以及 `adapter.routes.*` 的结果都没有改动。
`adapter.routes.*` 的请求只有一处放宽：`add` / `remove` 的 `expectedRevision` 改为可选，传入时的冲突检查与之前完全一样。
