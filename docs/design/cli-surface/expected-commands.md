# hyprial CLI 目标命令结构（expected commands）

- 日期：2026-10-04；修订：2026-10-05 回填 Allen 对 D1/D8 的裁决；2026-10-06 一级与 `agent` 二级定稿
  （Allen 与 user-exp-improver 在飞书逐条确定）；**2026-10-06 重排：本文只写目标态（改完长什么样），
  实施计划、模块化、分类、重叠排查与 PR 顺序移到 [cli-refactor-plan.md](cli-refactor-plan.md)**。
- 地位：本文是**目标态**，实施 PR 以本文为验收标准；
  [cli-top-level-command-consolidation.md](../../reference/cli-top-level-command-consolidation.md) 是现状盘点（下文简称《盘点》），
  条目编号（A–AA）沿用《盘点》。
- 退役策略：一律直接删除旧写法，不留别名、不设过渡期；同 PR 更新仓内全部调用者并加回退守护测试
  （《盘点》4.3）。
- 原则：一级命令按**领域名词**收拢，动作放进所属领域；同一件事只有一份实现、一个入口。

## 0. 已决裁定

| 日期 | 裁定 |
|---|---|
| 2026-10-05 | D4：`upgrade run` 保留（隐藏，daemon 调度器以它为升级子进程）。其中"旧定时器转隐藏、保留一个迁移周期"一条已被 2026-10-06 的 A8 结论取代：旧定时器连同 `timer install/uninstall` 一起删除 |
| 2026-10-06 | D1 改判：`install` 不再保持一级可见，改为 `socialware install`，只保留结构和入口（恒 `COMING_SOON`） |
| 2026-10-06 | D2：`dispatch matrix` → `workflow dispatch`；`user-proxy` → `squire proxy`（**已被 2026-10-07 squire 拆分取代**） |
| 2026-10-06 | D6：消息域一级定名 `message`，收进 send/ack/reply/targets 与全部投递观察面 |
| 2026-10-06 | D8：网络家族规划重启；一级 `network` 改名 `host`（主机域），`hosts` → `host list` |
| 2026-10-06 | 一级定为 22 个，后因 fs 保留一级变为 **23 个**；全部可见，无隐藏一级；`mcp` 移出 CLI 树（《盘点》H，方案 B） |
| 2026-10-06 | `agent` 二级定稿（§2.1）；`agent keep` 并入 `agent restore … always`，连同 keep 名单存储一起删除 |
| 2026-10-06 | 重叠排查 A1–A8、B、C 全部要修，在 #1126 内规划（见实施计划） |
| 2026-10-07 | 新增：`agent home provision`（为旧 agent 补建 home，卡 905a6523）；`workflow`/`routine` `overview serve`（实时只读网页，两入口共用一个模块、默认定位不同）；`workflow progress`（#1210 心跳） |
| 2026-10-07 | `squire` 拆分（§2.1、§2.6、§2.11）：模型档位收进 `agent tiers`（原 `dispatch matrix`）与 `agent tiers probe`（原 `squire probe`），探测只有一个入口；user-proxy 是"人"的收件端，收进 `user proxy setup\|status`；`squire` 只留 `setup`。删除 `workflow dispatch` 与 `squire probe` |
| 2026-10-07 | `identities upsert` 提前删除（#1202）：确认"谁是谁"只走 `user bind`；identities 表与读侧回退保留，已有 verified 行迁移见卡 ed923b04 |
| 2026-10-07 | `adapter` 二级定稿（§2.7）：`onboard` 并入 `add`；`doctor`/`authorize` → `scopes check/authorize`；`pins` 删除，绑定列进 `adapter list`；`identities upsert` 等身份解析器 |
| 2026-10-07 | `daemon`/`upgrade` 二级定稿（§2.4、§2.5）：新增 `daemon start`（后台拉起，`run` 为前台进程本身）；`upgrade --source/--ref` 隐藏；`upgrade` 顺带解包配套 GUI，`gui upgrade` 只作修复 |
| 2026-10-14 | R5（daemon + upgrade）落地：一级 `restart` → `daemon restart`；新增 `daemon start`（后台拉起，复用 `init` 的启动原语；`daemon run` 为前台进程本身，help 标注调试/进程管理器用）；`autoupdate status|restart|run` → `upgrade status|apply|run`（`upgrade run` 隐藏，daemon 调度器与遗留 launchd/systemd 模板改 spawn `upgrade run`）；`upgrade --source/--ref` 从 help 隐藏（功能保留）；`upgrade` 安装成功后顺带解包配套 GUI，`gui upgrade` 保留作修复路径。**本单偏差**：F8（删遗留定时器）未在本分支基线，`autoupdate install/uninstall` 仍在，`autoupdate` 一级待 F8 一并删除；本单不做 A8 行为改动（R 系列只改命令面）。 |
| 2026-10-08 | work items（替代 kanban，设计 owner org-improver）挂在 `org work` 下（§2.8），一级仍为 23 个；用 `--org` 选组织，work 存在该组织的目录 space（`group-<NAME>`）的 `work/` 下，用户不接触 space id。`workflow missions collect\|finish\|check` 并入 `org work`（work item 即扩展后的 mission），旧路径不留别名 |
| 2026-10-07 | `workflow`/`routine` 二级定稿（§2.9、§2.10）：`status` 并入 `inspect`；`gc` 留组级做唯一回收入口（worker + 已关闭 graph + 节点 worktree，后两者 TODO）；`history`/`migration` 迁移期隐藏、迁移后删除；`routine rm` → `remove` |
| 2026-10-14 | R1（agent 命令面）落地：`start`/`down` → `agent start`/`agent stop`（`agent stop` 只停 agent，不再接受适配器）；`top` → `agent top`；transfer 家族 → `agent move` + 隐藏 `agent transfer …`；`restore-policy`/`restore-threshold` → `agent restore`；`home-census`/`migrate` → `agent home …`；`grant` 动词化并删除 `revoke`、`grants <ACTOR>`、一级 `grants-check`/`grants-visibility`。**本单偏差**：`agent start` 仍为 `agent start <HARNESS> --name <NAME>`（§2.1 的 name-first `agent start <NAME> --harness` 未随本单落地，留待后续切片）；`agent grant <ACTOR> check\|visibility` 保留 `--caller` 作为 what-if 身份，ACTOR 只作位置词。 |

尚待裁定：`agent` 以外各组的二级形态（组标题标"二级待逐组确认"）；`user` 组子命令形态（A2）；
D3（`agent grants` 只留全局审计，本文按建议写入）；D7（`grant … check/visibility --live`，本轮不加）。

## 1. 一级（23 个）

| 一级 | 收进来的现有命令 | 说明 |
|---|---|---|
| `help` `version` `doctor` `log` `config` | — | 观察与配置 |
| `login` | — | 只做登录/重新认证；凭据、身份和设备阶段都完整时只报告状态，设备阶段缺失时原地续跑且不重新认证，`--force` 才强制重新认证；首启不再经由 `login` |
| `logout` | — | 只清除当前 home 的用户登录；保留 agent 凭据与设备 key；幂等，变更前需确认或 `--yes` |
| `init` | `onboarding plan/apply` → `init --plan` / `init` | 首启覆盖整台机器（建 home、登录、squire、daemon）；desktop 契约 §11 的调用同 PR 更新 |
| `profile` `gui` | — | |
| `squire` | — | 个人助手（只管助手本身：`squire setup`） |
| `socialware` | `install` → `socialware install` | 只保留结构和入口（恒 `COMING_SOON`） |
| `user` | `adapter identities upsert`、`squire setup --owner-open-id` 的登记职能（A2）；`user-proxy` → `user proxy` | 本机唯一的"谁是谁"库，以及"人"的收件端 |
| `adapter` | `lark-auth` → `adapter user-auth` | 适配器的完整生命周期；停适配器只走 `adapter stop` |
| `agent` | `start`/`down` → `agent start/stop`；`transfer` 家族 14 个入口 → `agent move` + 隐藏 `agent transfer …`；`top` → `agent top`；`ps` 的 connector/session 段 → `agent list` 运行状态列；`grants-check/-visibility` → `agent grant <ACTOR> check/visibility`；`dispatch matrix` → `agent tiers`；`squire probe` → `agent tiers probe` | agent 的完整生命周期；connector 就是 agent 的运行态；模型档位选择 |
| `message` | `send` `ack` `reply` `targets`；`delivery status/trajectory`、`outbox`、`query … inbox/outbox`；合并读取 `batch` | 消息的完整生命周期 |
| `org` | — | 组织与成员（`member` / `context` 子组）；`org network` 留在 org 下 |
| `fs` | — | OrgFS 空间与文件，保留一级（Allen 2026-10-06 改判）；内部 `space` / `member` / `trash` / `purge` 子组 |
| `host` | `hosts` → `host list` | 本机与网络上的其它 host；预留 `host status` 与 `host create` |
| `service` | — | **改义**：独立服务接入（sub2api 这类任意 Docker 部署的服务）；原"看 daemon 服务状态"的含义移到 `daemon status` |
| `workflow` | `worktrees` 并入 `workflow gc` | PAC |
| `routine` | — | PAC |
| `daemon` | `restart`；`ps` 的 daemon 段 → `daemon status`；`upgrade` 组（status/apply/run） | 本机运行与升级；唯一执行迁移步骤的操作入口 |

合计 23（观察与配置 5 + login/logout/init/profile/gui 5 + 其余 13）。
`mcp` 不在 CLI 树中：channel 服务器改为模块私有进程入口，由 daemon 直接 spawn。

## 2. 二级 / 三级

"来源"列：`=` 不变；其它写明来自哪个现有命令。

### 2.1 `agent`（Allen 2026-10-06 定稿）

| 子命令 | 来源 | 说明 |
|---|---|---|
| `create` `host-invite` `destroy` | = | |
| `list [--inactive-since <DURATION>]` | = + 运行状态列 | 增列：在哪个 harness 上、session 是否在线（吸收 `ps` 的 connector/session 段）；`--inactive-since` 用 §3.1 的统一闲置定义 |
| `start <NAME> [--harness claude\|codex\|pi\|… \| --tier fast\|strong\|super]` | 一级 `start <harness> --name <NAME>` | 名字在第一位；`--tier` 与 `agent tiers` 用同一个解析函数（§3.5） |
| `stop <NAME>` / `stop --all` | 一级 `down` | `--all` 只停 agent，**不碰适配器** |
| `top` | 一级 `top` | 闲置用 §3.1 的统一定义 |
| `unblock` | = | |
| `restore <NAME> active\|always\|never` | `restore-policy`、`keep add/remove` | 重启恢复策略只有这一组、一份存储 |
| `restore --threshold <DURATION>` | `restore-threshold` | 阈值即 §3.1 的闲置阈值 |
| `restore --list` | `keep list`、`restore-policy` 的读 | 列出每个 agent 的策略与阈值 |
| `move <NAME> --to <HOST>` | 一级 `transfer` | 人用的迁移入口；目标是 `host` 组管理的主机 |
| `transfer …`（**整组隐藏**） | `transfer-bundle-export/validate/receive/land/complete`、`transfer-credential-seal/open`、`transfer-runtime probe/downgrade-state`、隐藏 `transfer-precheck/receive/cred-stage/container-home/session-path` | 协议级命令，由两端 daemon 与脚本调用；持久化值 `hosted_by='transfer-receive'` 是数据，不随命令改名 |
| `home census` / `home migrate preflight\|execute\|rollback` | `home-census`、`migrate …` | 纯移动 |
| `home provision <NAME>` | 新增（Allen 2026-10-07，卡 905a6523） | 为 home 出现之前创建的旧 agent 补建 agent home 目录与注册回执：保留 row、entity_token、pin 与 inbox，不改 config/运行时，幂等（已有就绪 home 返回 `provisioned:false`；home 存在但未就绪报 `AGENT_HOME_CONFLICT`）。与 `home migrate` 的关系：provision 只建 home，不切 P2；migrate 以 home 存在为前提并切到 P2 |
| `ensure-home [<NAME>… \| --all-legacy]` | daemon readiness batch | 为既有 agent 批量补建 P2 home；默认推迟运行中的 agent，支持 dry-run 与数量上限 |
| `readiness <NAME> --role developer\|pac` | daemon readiness batch | 只读检查实际 agent HOME/XDG/PATH；UNKNOWN 与不就绪均以失败退出，不修改配置 |
| `grant <ACTOR> [add\|list\|revoke\|check\|visibility]` | `grant`、`grants <ACTOR>`、`revoke`、`grants-check`、`grants-visibility` | 叶子 + 位置动词，缺省 `add`（现行 `agent grant <ACTOR> --capability … --scope …` 拼写不变）；`check`/`visibility` 是 what-if 求值器，不读台账；help 文案见 §3.2 |
| `grants` | `grants`（去掉可选位置参数） | 只留全局审计列表；单个 actor 用 `grant <ACTOR> list` |
| `secret provider-write\|grant\|list\|revoke` | = | 凭据授权，不并入 `grant` 动词集 |
| `tiers [--tier fast\|strong\|super]` | `dispatch matrix` | 模型档位矩阵只读视图：每个档位现在会选中哪个 harness:provider:model，读已记录的可用性；与 `agent start --tier` 共用解析（B） |
| `tiers probe [--combo H:P:M] [--dry-run]` | `squire probe`（吸收 `dispatch matrix --probe`） | **探测可用性的唯一入口**：实测候选组合并记录到本人配置，用统一判定（§3.3） |

### 2.2 `message`（Allen 2026-10-06 定稿；R2 已落地投递观察面）

R2 把投递观察面迁到 `message` 下（改名不改签名）。`send`/`reply`/`ack` 与下文
标 R12 的改写随 R12 迁入；`status` 的位置参数化与 `--from` 默认化、`trajectory` 改名
`trace`、`outbox --undeliverable`、`--kind route` 均未随 R2 落地，留待后续切片。

| 子命令 | 来源 | 说明 |
|---|---|---|
| `send <TEXT> --to … [--file/--image] [--topic] [--conversation]` | 一级 `send` | **删除 `--reply-to`**：回复只走 `message reply` 一条路（R12） |
| `reply <MESSAGE_ID> <TEXT>` | 一级 `reply` | 回复并消费这条待处理消息（R12） |
| `ack <MESSAGE_ID>` | 一级 `ack` | 不回复，只消费（R12） |
| `targets [--kind agent\|user]` | 一级 `targets` | 能投递给谁；`channel_route` 过滤器已退休（B9）——路由配置看 `adapter route list`；未过滤输出仍可含 `channel_route` 行 |
| `inbox [<AGENT>]` | `query <AGENT> inbox` | 只读，不消费；省略时默认本机主人 `user:<owner>` |
| `batch --from <ACTOR> [--cursor C \| --after-cursor C]` | `message.pending.batch` | 消费式读取一个 daemon 渲染的完整合并批次，但不确认任何消息；逐项 reply/ack。0.5.5 只提供此路径，不提供一级 `inbox batch` 别名 |
| `outbox [--actor <ACTOR>]` / `outbox prune` | `outbox list/prune`、`query <AGENT> outbox` | **唯一的 outbox 视图**，一份序列化（A7）；`--actor` 过滤某本机 actor 的发送 |
| `status [--from <ACTOR>] [--message-id <ID>] [--timeout <S>]` | `delivery status` | 发出去的消息最终怎样了 |
| `probe-delivery-paths [--from <ACTOR>] [--route route:<adapter>:<route>]` | 显式低频就绪探针 | 向 agent、user、route 三族各发一条 `hyprial-probe` 标记消息，写 `state/delivery-paths-probe.json`；doctor `delivery-paths` 只读该文件 |
| `trajectory <MESSAGE_ID>` | 一级 `trajectory` | 与 `status`、一级 `log` 共用读取实现（B7）；改名 `trace` 未随 R2 落地 |

### 2.3 `host`（Allen 2026-10-06 定稿）

只管机器本身。

| 子命令 | 来源 | 说明 |
|---|---|---|
| `list` | 一级 `hosts` | 网络上的在线节点，标出本机 |
| `status` | 新增 | 本机网络状态：Tailcat 边车、对外暴露的端口、到其它主机的可达性（数据已有，合并展示） |
| `create` | **预留，TODO** | 添加本地或云端虚拟机作为新 host（`agent move` 的目标）；先注册入口并标 TODO，暂不实现 |

R4 对 `status` 与 `create` 采用可见的 `COMING_SOON` 入口：两者在功能实现前均以退出码 1
明确失败，JSON 返回 `code: "COMING_SOON"`，不读写 host 或 daemon 状态。这样既锁定目标命令形态，
也让自动化能区分“已规划但尚未实现”和未知/拼错命令。

原 `network expose/unexpose/exposures/peer-key` 移到 `service`（§2.13，Allen 2026-10-06）。

### 2.4 `daemon`（Allen 2026-10-07 定稿）

| 子命令 | 来源 | 说明 |
|---|---|---|
| `start` | 新增（显式化现有启动原语） | 在后台拉起 daemon：spawn 一个脱离终端的 `daemon run`，等就绪后返回。`init` 等命令隐式拉起时走同一个原语 |
| `stop` | = | 停止判定统一用严格版（§3.4） |
| `restart` | 一级 `restart` | 唯一的停启实现（A1）；`daemon upgrade` 与 `daemon upgrade apply` 复用它。`via="hyprial restart"` 字段值是已发布语义，不随命令改名 |
| `status` | 一级 `service`（旧义）、`ps` 的 daemon 段 | daemon 不在线时也可用 |
| `run` | = | daemon 进程本身，前台运行到 SIGINT/SIGTERM；help 标注"前台运行（调试/进程管理器用）"，日常用 `start` |
| `service install\|uninstall\|status` | 暴露已有 ServiceManager | 管理当前用户的 launchd/systemd unit；install/uninstall 需交互确认或 `--yes`，Windows 返回 `SERVICE_PLATFORM_UNSUPPORTED`。只操作同时满足固定 label/unit 与当前 `hyprial` executable fence 的文件 |

`daemon restart`、`daemon upgrade` 与 `daemon upgrade apply` 先判定 daemon 是否已由该
service 接管。已接管时只用 `launchctl kickstart -k` 或 `systemctl --user restart`，并验证
服务 socket 的 daemon pid 必须等于新的 supervisor job pid，且不能沿用重启前的 job pid；JSON 的
`restart.supervision` 使用 `supervised`、`unsupervised`、`lost` 三值，`lost` 必须失败。
已加载 label 缺少 unit 文件时不得回退为非托管重启。

### 2.5 `upgrade`（Allen 2026-10-07 定稿）

| 子命令 | 来源 | 说明 |
|---|---|---|
| （组默认动作）`upgrade [--tag] [--force] [--no-restart]` | 一级 `upgrade` | 安装并经 `daemon restart` 重启；同时解包与新版本配套的 GUI 组件（原 `gui upgrade` 的工作）。`--source forgejo` / `--ref` 是内部测试用的一次性来源：保留功能，从 help 隐藏 |
| `status` | `autoupdate status` | |
| `apply` | `daemon upgrade apply` | 切到已装好的新版本，经 `daemon restart` 的同一实现 |
| `run`（隐藏） | `autoupdate run` | 只由 daemon 调度器 spawn；daemon 不在时不再自行做本地升级（A8） |

自动升级只有 daemon 调度器一个；旧的 launchd/systemd 定时器与 `autoupdate install/uninstall` 删除，
存量单元由一次性迁移清理（A8）。开关仍是 `config set autoUpgrade`。

### 2.6 `user`（`proxy` 已定；其余子命令形态待身份解析器）

- **已决**："open_id → 人"只有一处存储、一个写入口（A2）。`adapter identities upsert` 已删除（#1202），确认只走
  `user bind`；`squire setup --owner-open-id` 不再各写各的库。
- `proxy setup|status`（原一级 `user-proxy`，Allen 2026-10-07）：代表"这个人"收发消息的 agent，发给 `user:<owner>`
  的消息经它的专用 Lark 路由送到本人；只能经 `user proxy setup` 起，`agent start` 拒收 user-proxy（B）。
- `bindings publish|withdraw (--org <ORG> | --all) [--json]`（与 org-improver 约定，#1223 G5）：把本人在本节点的
  已确认绑定发布到组织目录，或撤回。其他成员由此在审计里看到"已在别处确认"，旧数据随之退役。只有本人（操作员）
  可执行，agent 调用返回 `CALLER_NOT_AUTHORIZED`。
- 现有子命令 `add` `list` `show` `bind` `unbind` 暂按现状列出；合并后的确切形态等 org-improver 的身份解析器
  （Casdoor union_id）上线后定。

### 2.7 `adapter`（Allen 2026-10-07 定稿）

| 子命令 | 来源 | 说明 |
|---|---|---|
| `add` | `add`、`onboard` | **唯一注册入口**：默认走 onboard 流程（创建或接管 Lark App 再注册）；给了现成 App 参数就只登记 |
| `remove` `start` `status` `reload` | = | |
| `stop` | = | **停适配器的唯一入口**（吸收 `down lark`，B） |
| `list` | `list`、`pins` | 适配器清单的唯一入口（`ps.adapters` 随 `ps` 退役，B）；新增"绑定的 agent"列，取代 `pins` |
| `pin` `unpin` | = | `pin` 是唯一的绑定入口（`squire setup` 也走它，A6） |
| `scopes check` / `scopes authorize` | `doctor` / `authorize` | App 权限范围（scope）的盘点与授权；`doctor` 改名避免与一级 `doctor` 撞名 |
| `route list\|candidates\|add\|remove` | = | 通道路由的唯一视图（B） |
| `identities sync\|list\|find` | = | 平台通讯录缓存。`upsert`（唯一能写 verified 的手工入口）在身份解析器上线后随 `user` 组删除（A2，§2.6） |
| `media get` | = | |
| `user-auth check\|complete` | 一级 `lark-auth` | |

### 2.8 `org` 与 `fs`（Allen 2026-10-06 定稿；**fs 保留一级**，一级变 23 个）

`fs space create <NAME>` 只要名字、建的是当前用户自己的空间，不依赖组织，所以 fs 不收进 org。两组都按对象再分一层，最长三级。

**`org`：组织与成员**

| 子命令 | 来源 | 说明 |
|---|---|---|
| `create` `join` `leave` `delete` `list` | 同名 | 组织本身；`execute` **删除**（与 `join` 等价，B） |
| `network` | 同名 | 组织视角的成员、设备与映射 |
| `member invite\|remove\|pending` | `invite` / `remove` / `pending` | 成员；`pending` = 领取发给本账号的邀请 |
| `context show\|status\|fetch\|import` | `show` / `status` / `fetch` / `import` | 组织上下文 |
| `work add <TITLE>` `show <ID>` `ls [--status] [--owner] [--assignee] [--invalid]` | 新增（#1230）；`ls --invalid` 吸收 `workflow missions check` | 工作项（替代 kanban，Allen 2026-10-08；设计见 org-improver《work items》§3）。work item 即扩展后的 mission：存在组织目录 space `group-<NAME>` 的 `work/` 下，全员可读写 |
| `work status <ID> <STATUS>` `check <ID> <N>` `note <ID> <TEXT>` `done <ID>` | 新增（#1230） | 状态、勾选清单第 N 项、追加日志；`done` 有验收门槛（清单全勾且调用者是 owner） |
| `work link <ID> <GRAPH>` `keywords <ID> --add\|--remove` `repair <ID>` | 新增（#1230） | 关联 PAC graph；匹配提示词；修复无效项（无"把当前内容接受为新基线"的覆盖） |
| `work show <ID> --copy id:<NODE_ID>`、`work resolve <ID> --keep id:<NODE_ID>` | 新增（R17 / #1263） | 同名冲突：两台设备各写了一份同 id 的 work item。`show --copy` 读其中指定的一份（冲突时不带 `--copy` 直接 `show` 报 `WORK_ITEM_NAME_CONFLICT`，并列出各份的 nodeId）；`resolve --keep` 组织内有读写权限的成员都能执行（squire 替人执行时也是成员，不是操作员），保留这一份，其余改名为新的 `M-` id；不改内容、不删除，执行人记入日志与账本；非成员拒绝。squire 的冲突巡检走这两条，不碰 space id。`--copy` 不叫 `--node`，避免和 PAC 节点（`workflow inspect --node`）混淆 |
| `work collect --routine <NAME> --graph <GRAPH> [--window-days N]`、`work finish --collected <FILE> --replies <FILE> [--declined <GRAPH>]…` | `workflow missions collect\|finish` | 原样搬入，space 参数换成 `--org` |

`org work …` 全部带 `--org <ORG>`：本节点只属于一个组织时可省略，属于多个且未给时报错并列出候选，不猜。
没有 `--space`，也没有 `work.space` 配置项。

**`fs`：OrgFS 空间与文件**

| 子命令 | 来源 | 说明 |
|---|---|---|
| `space list\|create\|join\|status\|serve\|web\|checkout` | `spaces` / `create` / `join` / `status` / `serve` / `web` / `checkout` | 空间本身 |
| `member list\|invite\|remove` | `members` / `invite` / `remove-member` | 空间成员（与 `org member` 是不同层级，已核实是两件事） |
| `ls` `read` `write` `mkdir` `mv` `rm` `resolve` `history` `watch` `import` `export` | 同名 | 文件操作平铺（最常用） |
| `trash list\|restore` | `trash` / `restore` | 回收站 |
| `purge <space-id> [--yes]` | `purge`（不带 plan id） | 仅清除本机 space replica，不向 peers 广播删除；交互确认或显式 `--yes` |
| `purge plan\|run\|status\|unban` | `purge-plan` / `purge <space-id> <plan-id>` / `purge-status` / `unban` | 内容级彻底清除；unban = 清除后允许同一内容再次写入 |

### 2.9 `workflow`（Allen 2026-10-07 定稿）

| 子命令 | 来源 | 说明 |
|---|---|---|
| `plan run list cancel events reset complete fail` | = | |
| `inspect <GRAPH> [--node N [--context]]` | `status`、`inspect`、`context` | 看图状态的唯一入口：整张图，或 `--node` 看单个节点；`--context` 输出节点的不可变上下文（原 `context`） |
| `gc [--dry-run] [REPO]... [--base REF] [--all]` | `gc`、一级 `worktrees` | **唯一回收入口**，覆盖三类资源：① workflow 自有 worker（现有：daemon 每 5 分钟自动回收，命令侧只做预览）；② 已关闭的旧 graph（**TODO**：保留期与归档/压缩方案待设计，PAC journal 现由触发器强制只追加）；③ 节点 worktree（**TODO**：图关闭后清理；原 `worktrees` 的只读建议并入 `gc --dry-run`，其 `[REPO]...` / `--base` / `--all` 参数一并保留） |
| `worker stop\|restart`、`overview show\|publish`、`notify resend` | = | `missions collect\|finish\|check` 已移到 `org work`（§2.8） |
| `overview serve [--host 127.0.0.1] [--port 39001]` | 新增（Allen 2026-10-07） | PAC 全景页的只读实时网页（SSE），默认定位到 workflow 区。与 `routine overview` 共用同一个 pac overview 模块（数据模型、页面、服务各一份），CLI 只传 `--focus`。默认只绑 127.0.0.1；`0.0.0.0` 须显式给出并提示无认证 |
| `progress <GRAPH> <NODE> --note <TEXT> [--request-id R]` | 新增（#1210） | 节点当前请求的 owner 发进展心跳：写一条 `node.progress` journal 事件，推进 `lastProgressAtMs`；note ≤280 字，同一节点 60 秒内合并（`coalesced:true`）。`inspect`/`list`/overview 显示 `lastProgressNote`。另有 MCP 工具 `workflow_progress` 供无 shell 的 worker 使用 |
| `history list\|status`、`migration status` | = | 只服务旧 workflow 迁移：迁移期隐藏，迁移结束后删除 |

### 2.10 `routine`（Allen 2026-10-07 定稿）

`plan` `add` `list` `status` `remove` `set` `pause` `resume`；`templates [show]`；`overview show|serve`（新增，Allen 2026-10-07：同一张 PAC 全景页，默认定位到 routine 区，调用与 `workflow overview` 相同的 pac overview 模块）。
`rm` 改名 `remove`（与 `adapter remove`、`member remove` 统一），其余不变。

### 2.11 `squire`（Allen 2026-10-07 定稿）

| 子命令 | 来源 | 说明 |
|---|---|---|
| `setup` | = | 只管私人助手本身。建 agent 走 `agent.create`、绑适配器走 `adapter.pin` 的正式 IPC 路径（A6）；owner 登记写 §2.6 的唯一存储（A2） |

`squire probe` → `agent tiers probe`；`squire proxy`（原 `user-proxy`）→ `user proxy`。

### 2.12 `socialware`

`install`：恒 `COMING_SOON`，只保留结构与入口。

### 2.13 `service`（Allen 2026-10-06 定稿）

一个组同时管服务的提供方和使用方。

| 子命令 | 来源 | 说明 |
|---|---|---|
| `expose <PORT> --target <unix:/path\|loopback> [--proxy-protocol v2\|none]` | `network expose` | **提供方**：本机把一个服务开放给组织网络并携带调用方身份（PROXY v2，TLV 0xE0 nodekey）。来历 f44d1e18（09-28，随 Tailcat 切换加入）；目前唯一业务用户是 OrgFS web 服务器（nodekey→成员身份，查不到即 403） |
| `unexpose <PORT>` | `network unexpose` | 提供方 |
| `exposures` | `network exposures` | 提供方：列出本机对外暴露的服务 |
| `whois <IP:PORT>` | `network peer-key` | 提供方：按连接地址查对端节点 key（识别谁连进来）；**改名 whois** |
| `connect <NAME>` / `disconnect <NAME>` / `list` / `guide <NAME>` | 新功能（service connect v0.4） | **使用方**：按名字接入组织目录里的独立服务（如 sub2api，任意 Docker 部署的服务），本机 `127.0.0.1:<固定端口>` |

旧一级 `service` 的只读 daemon 探测不在此组（移到 `daemon status`）。service connect v1 的服务端走 Docker 边车，不复用 `expose`。

### 2.14 其余一级（二级待逐组确认）

| 一级 | 形态 | 说明 |
|---|---|---|
| `init` | `init [--plan] [--service\|--no-service]` | `--plan` = 原 `onboarding plan`；无参 = 原 `onboarding apply` + 原 `init`；home-guard 后唯一能建 home 的入口。交互式未给服务开关时询问，非交互式不注册服务，除非显式 `--service` |
| `login` | 叶子 | 有效登录只报告状态；`--force` 重新认证 |
| `logout` | 叶子 | 当前 home 用户登录的幂等清理；交互确认或 `--yes`；不删 agent 凭据和设备 key |
| `profile` | `list` `show` `use` `create` | 多 home 选择 |
| `gui` | 叶子 + 位置动词 `start\|status\|stop\|upgrade` | 配套 GUI 组件随 `upgrade` 自动解包；`gui upgrade` 只留作修复手段（`--force` 重新解包） |
| `config` | `set` `get` `list` | `get`/`list` 为新增（《盘点》U） |
| `doctor` | 叶子 | 推荐的修复写目标拼写（如 `agent restore <NAME> always`，不再推荐 keep） |
| `log` | 叶子 | 全组件结构化历史 |
| `help` `version` | 叶子 | `help` 由注册树渲染 |

## 3. 统一语义

### 3.1 "闲置"只有一个定义

现在有三个各自独立的定义：restore 阈值（`identity/impl/agents/state/worker_state.py:22`，12h）、
`agent list --inactive-since`（daemon 侧按参数时长过滤）、`agent top` 写死的 30 分钟
（`daemon/impl/operations/top.py:79`）。目标：三者读同一个定义——同一个活动时间来源、同一个阈值来源
（`agent restore --threshold`），`--inactive-since` 只覆盖时长、不另立"活动"口径。具体规则（活动时间取哪个
字段、`top` 是否改用 restore 阈值）在实施计划 F9 定，定后回填本节。

### 3.2 `agent grant` 的 help 说真话

see-actors / send-to 授权**会被强制执行**（`daemon/impl/application/messaging/visibility.py` 每次请求读台账裁决）。
`agent grant` 的 help 不再写"只记录、不强制"，改为按能力说明哪些被运行时强制；`check`/`visibility` 的 help
写明"what-if evaluator, does not read the live ledger"，读真实台账用 `grant <ACTOR> list`。

### 3.3 探测可用性只有一个判定

探测只有一个入口 `agent tiers probe`（原 `squire probe`，吸收 `dispatch matrix --probe`），用 `classify_run` 一个分类函数，
结果写 UserProfileStore；`agent tiers` 只读这份记录，不再自带诊断。

### 3.4 daemon 停止只有一种严格度

`daemon stop`、未受 supervisor 管理的 `daemon restart`、`daemon upgrade`、`daemon upgrade apply`
都用严格版（`require_exit=True`：必须证明旧进程已退出）。受管理 daemon 的 restart/upgrade
不先自行 stop/start，而由 supervisor 完成原子重启，再通过 socket pid 与 job pid 双向核验。
`doctor` 检查游离 socket owner、`DAEMON_OWNERSHIP_BUSY` 重拉循环和 unit/template 漂移；
`doctor --fix` 只修复可归属的 Hyprial unit，替换前保留备份，外国 unit 一律拒绝改写。

### 3.5 其它单入口规则

| 事 | 唯一入口 |
|---|---|
| agent 运行态 | `agent list`（`agent top` 同源快照） |
| 适配器清单 / 停止 | `adapter list` / `adapter stop` |
| 通道路由 | `adapter route list` |
| 首启 | `init`（`login` 不再触发首启） |
| user-proxy 建立 | `user proxy setup` |
| tier → harness 解析 | 一个函数，`agent start --tier` 执行、`agent tiers` 展示 |
| 模型可用性探测 | `agent tiers probe` |
| outbox 序列化 | 一份 |
| 在线节点过滤 | 一份（`host list`） |
| 重启恢复策略存储 | 一份（`agent restore`） |
| "open_id → 人"存储 | 一份（`user`） |
| 自动升级调度器 | 一个（daemon） |

## 4. 退役清单（不留别名）

### 4.1 一级

| 退役 | 替代 |
|---|---|
| `ps` | `agent list`（connector/session）+ `daemon status`（daemon 段）；`ps.adapters` → `adapter list`；`ps.meshNodes` → `host list` |
| `top` | `agent top` |
| `targets` | `message targets` |
| `hosts` | `host list` |
| `network expose/unexpose/exposures/peer-key` | `service expose/unexpose/exposures/whois`（§2.13） |
| `onboarding plan/apply` | `init --plan` / `init` |
| `install` | `socialware install` |
| `restart` | `daemon restart` |
| `service`（旧义：daemon 只读探测） | `daemon status`（`service` 名字改作独立服务接入，§2.13） |
| `autoupdate status` / `restart` / `run` | `daemon upgrade status` / `daemon upgrade apply` / 隐藏 `daemon upgrade run` |
| `upgrade`（一级）/ `upgrade status` / `upgrade apply` / `upgrade run` | `daemon upgrade [status/apply/run]`；旧命令不保留 |
| `dispatch matrix` / `dispatch matrix --probe` | `agent tiers` / `agent tiers probe` |
| `worktrees` | `workflow gc --dry-run`（节点 worktree 回收为 TODO，见 §2.9） |
| `user-proxy status/setup` | `user proxy status/setup` |
| `delivery status` | `message status` |
| `trajectory` | `message trajectory` |
| `outbox list/prune` | `message outbox` / `message outbox prune` |
| `query <ACTOR> inbox` / `outbox` | `message inbox <ACTOR>` / `message outbox list --actor <ACTOR>` |
| `send` `ack` `reply` | `message send/ack/reply` |
| `grants-check` / `grants-visibility` | `agent grant <ACTOR> check` / `visibility` |
| `lark-auth check/complete` | `adapter user-auth check/complete` |
| `mcp claude-channel/agent-channel/claude-channel-recover` | 已删除；`daemon upgrade`/`doctor --fix` 迁移旧 launcher JSON，活会话需 relaunch |
| `mcp claude-turn-ended` | 删除（无调用者） |
| `transfer <NAME> --to` | `agent move <NAME> --to` |
| `transfer-bundle-*`、`transfer-credential-*`、`transfer-runtime`、隐藏 `transfer-precheck/receive/cred-stage/container-home/session-path` | 隐藏 `agent transfer …` |
| `start <harness> --name <NAME>` | `agent start <NAME> --harness/--tier` |
| `start user-proxy` | `user proxy setup` |
| `down <NAME>` / `down --all` | `agent stop <NAME>` / `agent stop --all` |
| `down lark` | `adapter stop` |
| `fs spaces/members/invite/remove-member/trash/restore/purge-plan/purge/purge-status/unban` | `fs space list` / `fs member list\|invite\|remove` / `fs trash list\|restore` / `fs purge plan\|run\|status\|unban` |

### 4.2 二级 / 三级

| 退役 | 替代 |
|---|---|
| `agent restore-policy <NAME> <P>` | `agent restore <NAME> <P>` |
| `agent restore-threshold` | `agent restore --threshold` |
| `agent keep add/remove/list` | `agent restore <NAME> always` / `active` / `agent restore --list`（存储一起删除，名单一次性迁移为 `always`） |
| `agent revoke <ACTOR> <GRANT_ID>` | `agent grant <ACTOR> revoke <GRANT_ID>` |
| `agent grants <ACTOR>` | `agent grant <ACTOR> list`（`agent grants` 只留全局审计） |
| `agent home-census` | `agent home census` |
| `agent migrate preflight/execute/rollback` | `agent home migrate preflight/execute/rollback` |
| `org execute` | `org join` |
| `org show/status/fetch/import` | `org context show/status/fetch/import` |
| `message targets --kind channel_route` | `adapter route list` |
| `adapter identities upsert` | 删除（#1202）；确认走 `user bind` |
| `squire setup --owner-open-id` 自带的 owner 存储（users.json） | 并入 `user` 的唯一存储（形态等身份解析器） |
| `squire probe` | `agent tiers probe` |
| `workflow status <GRAPH>` | `workflow inspect <GRAPH>` |
| `workflow context <GRAPH> <NODE>` | `workflow inspect <GRAPH> --node <NODE> --context` |
| `workflow history …`、`workflow migration status` | 迁移期隐藏，迁移结束后删除 |
| `routine rm` | `routine remove` |
| `workflow missions collect` / `finish` / `check` | `org work collect` / `org work finish` / `org work ls --invalid`（squire 模板调用方同一 PR 改） |
| `adapter onboard` | `adapter add`（默认即 onboard 流程） |
| `adapter doctor` / `adapter authorize` | `adapter scopes check` / `adapter scopes authorize` |
| `adapter pins` | `adapter list`（绑定的 agent 列） |
