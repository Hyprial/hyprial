# Agent 启动前只读就绪检查

`hyprial agent readiness` 在任务启动前，用**该 agent 实际会收到的 HOME/XDG/PATH**
检查工具、命令权限、Forgejo 身份引用和配置版本。它不创建 home、不写 settings 或
投影、不改变权限、不重启 daemon/agent、不中断活动回合。

```sh
hyprial agent readiness <name> --role developer
hyprial agent readiness <name> --role pac --json
hyprial agent readiness <name> --role developer \
  --tool git=/absolute/bin/git --work-path /absolute/workspace \
  --forgejo-identity --expect-login <declared-login> --json
```

`--role` 必填。`--harness` 省略时取该 agent 的首选/最近 harness；`--cwd` 省略时取
注册的 cwd。`--tool NAME` 从实际启动 PATH 解析；`--tool NAME=/absolute/path` 固定
绝对调用形式。`--work-path`、`--tool` 可重复。`--timeout`（0.1–60 秒，默认 8）限制
每个外部探测。stdout 为 JSON；退出码 0 表示 `ready=true`，1 表示未就绪（含任何
UNKNOWN），2 表示参数或命令自身失败。`ok` 在 CLI 中与 `ready` 一致，daemon 返回的
`ok` 只表示 RPC 成功，判断就绪必须看 `ready`/`status`。

## 状态语义

| 状态 | 含义 |
| --- | --- |
| `pass` | 该项声明的启动前提已在实际环境验证 |
| `blocked` | 已知阻塞；`action` 给出可操作修正方向 |
| `unknown` | 无法内省或证据不足；**不是就绪**，不得用工具存在、旧报告或操作者 shell 推断 |

`ready=true` 要求所有检查项均为 `pass`。PASS 只证明声明的工具/权限/身份/配置前提，
不证明模型、provider 或 HTTP 调用一定成功。

## 检查项

- `home`：P2 home 存在、非符号链接、属主为 daemon 用户、mode 0700。预检不
  chmod/chown，也不创建 home。
- `environment`：用 P2 profile 组合实际环境；HOME/XDG/native 根归 agent 所有，
  PATH 来自启动环境。legacy 模式没有 agent-owned 环境，为 UNKNOWN。
- `interop:wsl`：仅当原始启动环境含 WSL interop 标记（`WSL_INTEROP` 或
  `WSL_DISTRO_NAME`）时出现。从实际启动 PATH 解析 `cmd.exe` 后，执行一次有界
  `cmd.exe /d /c exit 0`，固定 5 秒超时；退出 0 才 PASS。非零、超时或无法启动均为
  BLOCKED；超时明确不是路径存在性问题，不做重试，也绝不重启整机。非 WSL 启动不
  出现该项（不适用）。
- `tool:<name>`：在实际环境中解析并有界执行版本命令；失败、超时、无版本输出分别为
  BLOCKED/UNKNOWN，且不回显 stderr。
- `permissions[:<tool>]`：按实际 settings 判定。deny 优先；短命令名与绝对路径形式
  不可互换；无匹配 allow 即 BLOCKED；`bypassPermissions` 不被接受；缺失/畸形/
  不可读 settings、legacy 或非 Claude harness 为 UNKNOWN。
- `permissions:env`：审计 env 包装授权。只接受
  `Bash(env EXACT_KEY=VALUE /exact/declared/tool:*)`，其中 key 必须在该角色的精确
  env-key 白名单内，包装的可执行文件必须与已声明工具的调用形式完全一致，且仍须有
  独立的非 env allow 规则。`Bash(env:*)`、env key 通配、值通配、未列入的 key、
  短/绝对形式错配、仅 env 规则授权、以及被 deny 覆盖的情况均 BLOCKED。env 规则的
  value 不进入报告；env 前缀本身从不构成授权。
- `work-path:cwd`、`work-path:<n>`：绝对路径、存在且为目录；不得与 agent 私有
  home/secrets/state 重叠；cwd 之外还必须在
  `permissions.additionalDirectories` 显式声明。`~` 不按操作者 shell 展开。
- `identity:forgejo`：仅在 `--forgejo-identity` 时出现。执行
  `fj -H <host> whoami`，只报告解析出的 `login@host`；凭据从不读取、复制或打印。
- `config`: compare the source revision with published projections. If the current
  revision has a projection, validate its private modes, file set, and content
  before reporting PASS, including when stopped. Invalid projections are BLOCKED
  with the resolver's reason; `reloadRequired` is null because restart alone does
  not repair drift. With no active binding and no invalid projection, the next
  start can publish the source. A running process with an unpublished source
  revision is BLOCKED with `reloadRequired=true`; multiple published revisions
  remain UNKNOWN. Readiness never repairs, reloads, or restarts.
  See [Pi projection recovery](pi-projection-recovery.md) for explicit recovery.

JSON 顶层还包含 `configRevision`、`publishedRevisions`、`reloadRequired`、
`identity` 和非秘密的 `limits` 清单。

## 角色模板

| 角色 | 必需工具 | env-key 白名单 | 说明 |
| --- | --- | --- | --- |
| `developer` | `git`, `fj` | 空 | 仓库工作区；Forgejo 身份仅在显式 opt-in 时检查 |
| `pac` | `git`, `hyprial` | 空 | PAC 入口/工作流须显式声明；短命令授权不覆盖绝对入口 |

模板只含用户选择的能力，不默认放开所有 Bash，不设置 `bypassPermissions`，不授予
sudo、生产重启、删除或凭据扩权。额外工具只能通过 `--tool` 显式声明。生产角色模板
的 env-key 白名单为空：本小 PR 只增加审计和 fail-closed 语义，不扩大任何权限策略；
增加具体 key 需要另行明确授权的策略变更。

## 身份与凭据边界

身份引用是 operator 批准的 opt-in 配置，不随新 agent 自动共享：

- 不读取、复制、轮换或打印凭据；
- 不把 agent 的 HOME/XDG 指向操作者；
- 引用缺失或 `--expect-login` 不匹配时，以安全原因提前失败，不尝试切换身份；
- 修正动作是“为该 agent 自己的 `XDG_DATA_HOME` 配置 operator 批准的单文件引用”；
- verifier 身份仍独立配置，不复用本检查的引用或权限。

## 配置版本与 reload

运行中的 binding 不持久化启动 revision。若源配置在运行期间变化，预检会把源
revision 与已发布投影比较；只有“未发布”才能证明 stale，多个已发布 revision 并存时
只能报 UNKNOWN。需要新配置时，由授权操作者在空闲时按正常流程 reload/restart；
预检不自动重启、不中断活动回合，也不把未完成的 reload 报告为成功。旧 daemon 不
认识该 RPC 时会安全失败；部署后需正常更新 daemon 才能使用，本功能不包含重启动作。

## 限制与平台

- 只读；不执行 Claude Code 的真实权限执行路径，只读取声明式 settings。env 包装授权
  只是被审计的声明，不是本工具执行权限的路径。
- 仅 Claude 有权限内省；Codex/Pi 等为 UNKNOWN。
- 多 revision 并存时运行 revision 不可证明，报 UNKNOWN。
- WSL 标记来自 daemon 原始启动环境；P2 环境白名单有意不新增 WSL 变量。实际子进程
  只探测一次，不替换为路径存在性检查，不重试、不执行 `wsl --shutdown`、不重启整机。
- 在 Linux 上以临时 agent home 和注入式 runner 做行为测试；WSL 子进程的成功/非零/
  超时/启动失败分支由注入式 runner 覆盖。macOS、Windows、真实新 agent 首次启动、
  真实 Forgejo 服务为 **NOT_RUN**。
- 不修改其他 worker/生产配置、签名、TCC 或不可变配置投影。

缺陷记录与回归证据见
`docs/evidence/new-agent-readiness-incidents-2026-10-19.md`。
