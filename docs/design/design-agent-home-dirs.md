# agent 独立目录、凭据交付与单路径 config

> 2026-09-14；设计段，不授权实施/发布。目标代码基线 **8c8295f36fe9aedf1f7855cfc2f014a6e13bf3e6**（下文 D）。
> `AGENTHOME3`：在 #510 @77a9bad6 上追加 owner 审阅修订；astra 设计 → 中档模型实施 → 强力模型审查。
> `AGENTHOME4`：在 #510 @9bbd6bd1 上追加 **G6 裁定**改写（spec 追加3，Allen 2026-09-14 16:xx 飞书两句）：项目 cwd 配置照常自动读，屏蔽的是用户级/系统级配置目录；§4.2/§4.2.1 的 A/B/C 候选作废。
> `AGENTHOME5`（2026-09-18）：对齐**已合入的 P1a 实现**（PR #513，merge `899a893a`，dev 头读数）；标注 P1a 已实现 vs P1b 及以后；写入 P1b 前置条件（§7.1）。设计段属性不变——除 P1a 已落部分外，本文仍不授权实施。
> 逐项决定见 §1–8；证据命令、冻结源与射程见 [读数附件](../evidence/design-agent-home-dirs-evidence.md)。

## 0. 生效约束、目标与成熟度

Allen 两句原话（逐字）：

1. 「这个功能实际上应该落在agent实体化的线里，现在secret都放在了hyprial_home/secrets中，未来应该放在hyprial_home/agents/《agent_id》/secrets中。请和h2b-developer讨论，当前给agent提供单独存储目录的条件是否成熟？如果提供单独的config目录，和cwd如何融合。如果条件成熟，可以直接派pac去做，按照先使用astra worker设计，中档模型实施，强力模型审查的标准流程走」
2. 「先不用考虑进程隔离的问题，未来将agent容器化后自然可以避免其它agent读取」

- **本单服务的目标状态**：agent 个性配置与项目配置分层清楚——项目规则随 cwd 生效，系统级配置不污染 worker；设计可交中档分期实施；一期保留真实 HOME，只收紧 provider 凭据交付。
- **既有裁定全部生效后，本单仍存在**：存在。G6 裁定改变了 §4.2.1 与单路径裁定的适用面，上一稿（77a9bad6/9bbd6bd1）仍写 A/B/C 待裁；本轮按裁定改写。本期不做进程隔离；HOME/XDG 重定向仍在 P2，不借目录归属改变一期宿主工具身份。
- 承接 **4adc5a8e** 的单显式路径，适用面经 G6 修正为 **agent 个性配置层**：恰好一个绝对目录，`discovery="explicit-only"`，材料先复制进去，禁止从用户级/系统级目录隐式追加；**不再约束 cwd 的项目配置**——项目层照常自动读（§4.2）。D 的 `acceptance-agent-identity.md` 有序链旧稿不覆盖这条裁定。
- spec「追加 2」已给四旧支 G1 处置，见 §5；不再记为未知，也不授权本单删远端分支。追加1明确本设计段不需要跑全量；豁免本设计全量运行，**不豁免结果正确性、全仓 lint、实施时的产品测试/变异/E2E**。本文 T/M 是未来实现合同，不是已经跑绿的产品测试。
- 不改产品代码，不接触生产 daemon，不读真实 secret。0600/0700 是防误用与其他 OS 用户访问的基线，**不是同 OS 用户 agent 间的隔离**；本文不承诺阻止恶意 agent 读别人的文件/进程环境/钥匙串。

**成熟度结论**：目录归属、已有 registry、逐次启动接线足以做分期实施；不能称“三家 harness 配置/认证已可直接重定向上线”。后者有隐式发现、钥匙串、旧环境合并与恢复路径等必须先验证的闸门。WIP 可以评审，闸门未闭合不能派无条件上线单。

§4.1 保留其他合法绝对单源。**G6 已裁**（spec 追加3）：项目 cwd 配置照常自动读，选仓与组合是 PAC 派单人的职责；屏蔽的只是用户级/系统级目录，由 agent 自己的配置目录接管该层；09-09 单路径裁定只约束个性配置层。§4.2/§4.2.1 按此写死，上一稿的 A/B/C 候选作废，本稿不再有该项待裁。

## 1. 目录键与生命周期

### 1.1 决定：`<agent_id>` 落为注册表 agent 名

记 `H` 为 daemon 已解析的绝对 HYPRIAL_HOME，`N` 为本地 AgentRegistry 解析并授权得到的 `actor` 短名：

```text
H/
  secrets/login.json                 # 用户登录凭据；本期不搬
  secrets/providers/<id>.json         # 明确属于用户的共享provider凭据；逐agent授权使用
  secrets/lark-<adapter>.json         # adapter 凭据；本期保留现有读写合同
  agents/N/                          # A = agent home；不是第二个 HYPRIAL_HOME
    config/                          # C 的推荐位置：唯一、无密值、已批准的配置输入
    secrets/
      provider-<id>.json              # agent API key 等；具体 schema 由实施端口定稿
      tool-<id>                      # agent 工具凭据，例如该 agent 自己的评审令牌
      native/<harness>/              # 二期：必须自行刷新/同目录存储的原生认证 home
    state/<harness>/                 # 非认证运行状态/缓存；能显式改位置时用这里
    state/home-receipt.json           # registry 授权的 provisioning 回执副本；不创建身份
```

> **Switch phase 1 路径注（2026-10-09）**：H2 recap 暂时继续写
> `A/state/recaps.jsonl`，不在第一片迁址；只读 switch handoff 固定发布到
> `A/state/context/handoff/<switch-id>/`。`complete-within-cutoff` 仅表示可核证据覆盖到
> 结算水位，不表示模型记忆无损。最终的 `A/context/handoff/` 与顶层 `memory/` 重排留给
> 后续完整目录重排 PR，与 H2 writer/reader 迁移、transfer/cleanup 拓扑闭集调整一并完成；
> 本阶段不得同时提供两套权威地址。

`C` 不等于 `A`，`A` 不等于 `cwd`，`H` 不重定向成 `A`。HARNESS_STATE_DIR 与 H 可能显式分开，**不能从 `$STATE/..` 猜 H**。daemon 构造目录解析器时传 H；路径推导集中一处，不在每个 connector 拼地址。

选择名字的理由：D 的 SQLite `actor` 为主键；URI 依赖当前 owner/machine，会在身份迁移时改写；换 harness/model 不应换 home。`$STATE/container-workers/<harness>-<name>/home` 是容器运行实例目录先例，不是通用身份目录的命名模板。

| 邻近事件 | 决定 |
| --- | --- |
| `start --name N` 同名 reuse | 仅复用已授权的同一 registry 实体及 home 回执；不清空、不重复复制 secret；live binding 仍由现生命周期守卫处理 |
| stop/down 或 daemon 重启 | 保留 home/config/secret；恢复重新从该实体解析，不从上一个 worker 的 env 快照恢复 |
| owner **别名**改写、同实体 machine 命名迁移 | 目录名不变；使用现身份迁移权限校验更新归属元数据，URI 不是目录主键 |
| **真实账号切换/托管归属变更** | 不因短名相同沿用凭据授权；旧 home 暂停交付，显式重新授权/重新 provision；与 login identity 迁移区分别名变更。**P1a 实际落地形态（已合）：启动时 owner 迁移在旧 owner 名下存在活跃 `agent_secret_grants` 时 fail-closed 拒绝启动（`OwnerMigrationCustodyConflict`，零写入），并由 login S3 编排报具名结局；homes 只报告不拦权（见 §1.2 注）；显式「别名/真实切换」选择的 CLI 入口本 build 尚未接线**（读数见附件 §9） |
| destroy 后新建同名 | 不是旧实体的续期；旧 secrets 默认不自动继承。destroy 吊销 provisioning 回执并留下可诊断残留；显式确认清理/迁出后才给新实体 provision |
| rename | 当前无此命令；本期不加。手工 mv 目录不构成改名；未来 rename 必须联动 registry、home 回执与全部原生会话路径 |
| 两台机器同名 | 各自 H 下独立 home，不自动同步；transfer 需明确接收授权、路径重定位与另行凭据授权，复制目录不能取得 hosted 权限 |

### 1.2 路径不是授权

- 入口先走 registry `native_actor/local_actor` 的本地/合法 hosted 判断；不能先截 URI 最后一段再找 home。
- 沿用名字词法与长度规则；禁止 slash、`..` 路径穿越，不“清洗”非法名字生成另一个合法名字。目录/祖先/凭据条目 symlink、非正规文件、错误 owner 回执拒绝使用；有文件不等于已授权。
- **大小写碰撞是现词法的真实邻域**：`Alice`/`alice` 都符合 D 的名字规则，大小写不敏感文件系统可能别名。同一个 H 下在**新建 agent 之前**就检查实际文件系统对象与已注册/已 provision 名字，串行/原子 reservation；碰撞时两个身份不得共目录，后来的创建响亮拒绝。registry 与目录提交失败需补偿/显式待修状态，不把永远拿不到 home 的实体报创建成功。不得默默 lower/sanitize；不借本单改 SQLite PK。已有冲突名单由迁移预检列出，需操作者处理后上线。
- home provision 的 record generation/receipt 归**现 Agent 权威**维护，文件只作匹配副本；不能以该 JSON 自封一个 agent。实施须复用现 lifecycle resource token/receipt 围栏或在同一 registry 扩展 provisioning 元数据，不能另建“home agents”表。**（P1a 已按此落地：`agents.entity_token` 列（NOT NULL UNIQUE，经 12 步表重建而非可空 ALTER）+ `lifecycle_resources` 的 `agent-home:<actor>` 行为权威 + `state/home-receipt.json` 仅镜像；未建第二张表。）**

> **P1a 作用域注（grants 拦、homes 只报告）与 P1b 到期条件**：P1a 的 custody 闸只按活跃 secret grants 拦权，active home 资源只计入拒启报告——其前提是 **P1a 的 home 里没有凭据材料**（resolver 需具名 grant，无 grant 的 home 不携带交付权；且别名改写必须保留 homes）。**该前提在 P1b 让 home 成为 agent 的真实 HOME（Git/SSH/XDG/原生认证）时失效：home 本身就可能存文件形态的凭据 ⇒ 届时闸的范围必须重新评估，本作用域注不延续到 P1b 之后。**（来源：e2e-verifier 对 #513 的 review 336 第四条；原句要求写进作用域注并「别让它看起来像永久结论」。读数：已合入的代码 docstring 承载了 grants/homes 取舍说明，但**未包含**这句 P1b 到期条件——该前置以本节 + PR #513 Deferred 清单 + hq spec 追加节为权威落点。）
- 新建 0700 目录/0600 secret；失败不 chmod/删掉不属于本次操作的 incumbent。按 actor 加操作围栏；补偿只处理自身创建且 token 仍匹配的资源。

**被否方案**：URI 路径会随 owner 迁移漂移；`<harness>-N` 把运行放进身份；cwd 内凭据会随任务/仓复制；新 UUID 实体模型重复现主键。名字键的 FS 碰撞不能靠“符合正则”自证安全（T01/T02）。

## 2. secret 三级归属：按权限主体，不按使用它的进程

| 级别 | 本期路径与用途 | 谁写 | 谁读/不读 |
| --- | --- | --- | --- |
| 用户级（登录） | `H/secrets/login.json`，用户 refresh token | `hyprial login`/refresh；本期不搬、不重定义 schema | 既有 CLI 登录流程；daemon 和 agent secret resolver **不读** |
| 用户级（提供方） | `H/secrets/providers/<id>.json`，用户明确共享给若干 agent 的 provider key | 该用户/授权迁移工具 provision、轮换 | resolver 仅按显式逐 agent grant 取指定条目；不枚举全库、不读 login；不是任何 agent 默认可得 |
| adapter 级 | `H/secrets/lark-<adapter>.json`，对应 Lark appSecret | 既有 adapter add/remove 原子配置事务 | 对应 adapter 的配置加载器/运行组件；不因 agent pin 该 adapter 就分发给 agent |
| agent 级 | `A/secrets/*`，该 agent 获授权的 provider key、工具令牌及二期原生认证 | 操作者显式 provision/轮换；原生认证由该 agent 的认证流程刷新 | 本次启动的 secret resolver、被授权的该 agent harness/工具；不列全库、不自动授予同名/同 owner agent |

**共享与归属分开**：h2b-developer 本轮确认当前 fast/DeepSeek 使用同一个用户级 key。它的权威落点应是用户级 providers 目录，不能仅因注入某 agent 就改称 agent 私有密钥。每个 agent 获显式 grant，注入时才派生该次使用权；不自动复制成 N 份权威，也不让 worker 扫全局“可用 key”。真正单独授予某 agent 的 provider key 才放 A/secrets。共享来源撤销/轮换必须列出所有受影响 grant/运行进程；本期不引入隐式共享 fallback。

`e2e-verifier-forge-token` 是 spec 触发实例的已知**文件名**，不是本单读到的内容；若授权目标确为 e2e-verifier，迁移目的地只能是它的 A/secrets。作者 agent 只能获自己的作者凭据。Forgejo “非作者批准”仍由独立账号和服务端检查成立，目录位置本身不构成审批隔离。

**修正现状射程**：D 的 `secrets/` 产品引用主要是 login 与 adapter，但 `harnesses/model_provider.py:94–115` 另会读取 HOME 下两个 Pi `auth.json`。因此不能从“两个 secrets 目录读者”推出“没有其他凭据读者”。一期必须**先由用户/操作者迁移这个正在使用的来源到上述明确落点、补逐 agent grant、验证 fast 路径，才切新 resolver 并去掉 ambient fallback**；不能直接删除而让 fast 派单断掉，也不做先新后旧兼容读取。保留“不落 argv/desired-state”的正确部分（T05/T12）。

**被否方案**：把 login/appSecret 都搬到每个 agent 目录会扩大授权并撞 init-login；只按文件名自动认领不证明归属；把未知凭据归公共库会重新形成默认共享。

## 3. 交付方式：选②显式注入，env 白名单同期落地

选择 **按 agent 显式注入**，而不是只传一个目录让模型自己猜哪个文件/哪个工具参数。P1 注入获授权 provider key 到本次子进程 env；保留现用 HOME/工具配置路径，不能因此称 tea/git/ssh 已有 agent 独立身份。必须用文件的消费者，后续能力验证通过才接**精确的 agent-owned 认证路径**，不在 P1 通过换 HOME 假装已接通。`HYPRIAL_AGENT_DIR=A` 可作为非密发现信息，但不是凭据授权协议，也不是给 worker 全量 secrets 索引。

### 3.1 权威与执行边界

1. 公共请求只有 agent 引用、明确 config 修订及运行意图，不含 secret 值。secret 绑定由操作者授予并存于现 agent 配置/权限元数据，普通 cwd 内容、PAC brief、模型工具调用不能自行扩绑另一个 agent 的 secret。
2. daemon 已解析身份及 provisioning 回执后，在即将 spawn 的受信任 resolver 读取**本 agent 私有条目或显式 grant 的指定用户级 provider 条目**；不读取 login、不枚举其他 A、不执行 secret 文件里的 shell。用户共享 key 的归属不因派生交付改变。
3. 创建新的 **完整 child env**：基础白名单 + 由 daemon 生成的通道/身份值 + 该 agent/该 harness 的显式映射。绝不修改 `os.environ`；env 不写 desired-state、PAC、SQLite launch recipe、日志、argv。恢复只持非密引用，重新授权并解析当前版本。
4. 所有 spawn 消费者必须把 env 当完整替换，**不再 `{**os.environ, **env}`，也不以空 dict 变 `None`**。覆盖 CLI TUI、tmux exec broker、headless、重连/重启、fallback harness、子 MCP、DSH 新子进程；本地之外不可承接者应拒绝“agent-owned”启动，而非静默降级。
5. resolver 成功不等于启动成功：完成回执在消费者确认 agent 身份、credential revision 绑定正确后才发；启用 config 加载能力时还须核 config digest。P1 provider-only 不得报单源 config 已加载。错误只报分类、条目 ID、阶段，不报值、值哈希或拼有密值的请求/响应正文。

### 3.2 白名单不是 `*_TOKEN` 黑名单

| 来源 | 一期具体策略 |
| --- | --- |
| 最小非密环境 | 仅显式 `PATH, LANG, LC_ALL, LC_CTYPE, TZ`；值经部署配置批准。TERM/COLORTERM/TERM_PROGRAM 仅 TUI；不复制全部 `LC_*` 或未知变量 |
| HOME/TMP/XDG | **P1 保留现用真实 HOME 原值**，不设 `HOME=A/state/home`；现用 XDG 各键/TMPDIR 逐键审核后保留既有路径或既有缺席状态，不在 P1 改址。保住 `~/.config/tea`、`~/.gitconfig`、`~/.cache/uv`、`~/.ssh` 与各 harness 原生 auth 的现路径；不复制或软链接整棵 HOME。HOME/XDG 与原生 home 的重定向统一移至 **P2**，逐家真实 E2E 后单独启用，不称 OS 隔离 |
| Hyprial 通道 | 按现 WorkerChannel 的正式字段/本次 actor、socket、session 生成；所需 H/STATE 显式取 daemon 设置，不把 A 设成 HYPRIAL_HOME；MCP 的 env 另取最小子集，不默认带 provider key |
| provider | 只根据本次声明加入 `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `DEEPSEEK_API_KEY` / `ZAI_CODING_CN_API_KEY`（pi 的 zai-coding-cn）及适配所需别名（例如 Claude 的 ANTHROPIC_AUTH_TOKEN）；只能取本 agent 对应条目。互斥认证来源冲突时报错，不凭 native 优先级选一个。Claude 订阅登录经 `claude setup-token` 得到的长期令牌以 `CLAUDE_CODE_OAUTH_TOKEN` 注入（authMode `oauth-token`），一个 secret 条目可授权给多个 agent 共享，无需复制 native 登录文件；`ANTHROPIC_OAUTH_TOKEN` Claude Code 并不读取，授权与启动时一律拒绝并提示改用 `CLAUDE_CODE_OAUTH_TOKEN` |
| 原生目录/工具认证路径 | P1 不生成 A 下的新 CODEX_HOME、PI_CODING_AGENT_DIR、CLAUDE_CONFIG_DIR；现部署显式值逐键批准后保持原址/缺席状态，不盲复制父 env。#496 自管的 DSH_HOME 沿其已落地合同保留，迁到 A 也属 P2。工具 token 的精确交付接法先核版本，未知不自造支持；P1 沿用现工具身份合同，不声称 agent-owned native/tool auth |
| 网络/证书 | HTTP_PROXY/HTTPS_PROXY/NO_PROXY（含其小写版本）、SSL_CERT_FILE 等只按逐键批准的部署映射加入；代理 URL 含认证则同属 secret，不作基础继承。端点必须来自已批准配置，不让 cwd 把 token 路由到任意 URL |
| 默认拒绝 | 父进程 provider keys、AWS/Google 默认凭据指针、未批准的 GIT_CONFIG_*（下行例外除外）/GIT_ASKPASS、BASH_ENV/ENV、PYTHONPATH、NODE_OPTIONS、DYLD_* 等；新例外须列用途/消费者/测试，不开前缀通配放行 |
| Git 单次 insteadOf 的明确例外 | 消费者是 **Git 子进程**；用途是现用 code.hyprial.com 隧道绕行的 URL 重写。允许经操作者批准的 `GIT_CONFIG_COUNT=1` 与配套 `GIT_CONFIG_KEY_0` / `GIT_CONFIG_VALUE_0`（键仅批准的 `url.<目标>.insteadOf`，值仅批准的源前缀；映射不得含凭据）。三键成组校验，不完整/多余索引/其他配置键拒绝；若部署明确不用单次重写，三键全部缺席，仍保留既有全局 gitconfig。不把 `GIT_CONFIG_*` 前缀整体放行，也不擅自改用/修改全局 gitconfig |
| SSH_AUTH_SOCK（一期必须） | 当前 worker 推送走 SSH remote，P1 必须显式保留经操作者授权的现用精确 socket 路径，贯通 worker→shell→git→ssh；它授予 SSH 签名能力，不是无害字符串。缺失、不可用或未经授权均不能启用该 P1 worker，不降为可选例外、不暗换 HTTPS 身份；真实 headless commit+push 核远端主体与既有路径 |

**先测后收**：上表是候选最小集合，不是已确认足够的上线白名单。实施先冻结 `harness × 实际读取env × 来源/授权 × 是否保留 × 正反测试` 清单；特别覆盖 uv/python 与 CLAUDE_CODE_*/CODEX_*/PI_* 的实际逐键需求。每家真实 headless E2E（含 commit+push）成功后才启用收紧；不能靠默认猜删变量。未测的组合保持未启用新模式，不拿失败归给用户环境。

这是行为最小化，不是进程隔离。P1 保留 HOME **不豁免 provider 来源验证**：原生 auth 仍可能优先于 env；迁移后若某版本还会读未授权 provider key，必须拒绝该组合的新模式，不能用换 HOME 在 P1 绕过。API-key 路径须验证只消费显式授权来源；原生 OAuth/钥匙串以及工具的独立身份留 P2/G2/G3，不能拿环境干净推断认证归属正确。

### 3.3 为什么不选①；为什么不把②缩成“多传一个 env 参数”

只传路径对任意工具易接，但 native provider 不认识 HYPRIAL_AGENT_DIR；缺凭据时还可能继续读共享 HOME，事后无法知道用谁的账号。②的代价是统一逐次 spawn 接线与适配表，但可机械比较 A/B 两个 worker 的实际环境和认证哨兵。

**更完整的做法**是②的“授权 → 私有解析 → 完整 env → 最终 spawn”的闭环，加必要的精确路径交付；并非单独的另一套 secret 目录服务。仅在上游白名单、下游仍合 ambient 环境，或者把 secret 塞进 daemon 全局 env，都不满足②（T03/T04/T06）。

## 4. config 与 cwd：个性配置单源，项目配置随 cwd 照常自动读

### 4.1 决定：推荐 `C=A/config`，但不把“唯一”偷换成“只能这个固定位置”

- 新 agent 的 provision 流程可显式选择 A/config，创建并批准内容后**把这个绝对路径写入现 Agent.config**。没有 config 的旧行不自动记为“已验证空配置”；明确迁移/明确批准空目录后才可接新启动合同。
- 保留 4adc5a8e 对其他绝对独立目录的表达能力：已有显式 C 可以位于 A 外，归属 identity.machine，单源、无密、冻结且可核。它必须与 cwd 分离，不能指 A 整棵树或 secrets/native/state；重叠使运行文件/凭据进入 manifest，应拒绝。推荐迁入 A/config，但需操作者显式确认，不静默改写 C。
- 不将 A/config 自动追加到一个外部 C；任一 agent 同次运行只选一个 C。跨机路径必须在接收机重新绑定，发送端绝对路径不是可移植内容句柄。

沿用的**实际旧支形状**（不是 D 已有 API；不擅加 schemaVersion/revision 键）：

```json
{
  "sources": [{"path": "/absolute/hyprial/agents/reviewer/config", "required": true}],
  "discovery": "explicit-only"
}
```

4adc5a8e 的 `AgentConfig.from_json` 只接受 `sources/discovery`，`ConfigManifest` 提供内容摘要。修订/receipt 放在启动关联元数据，不偷偷塞入会被旧 parser 拒绝的 config 对象。`required=false` 仅确认不存在时可以缺席，权限/I/O/坏文件不是缺席。

### 4.2 分层与冲突规则（G6 已裁）

Allen 两句原话（逐字，spec 追加3）：

1. 「我赞成B，agent自己的独立目录维护的是自己的“个性”，项目相关的内容放在具体的项目cwd中，如何组合则是派发任务时pac编写者的职责」
2. 「是的,“派一个 worker 去 harness-bridge 仓干活”,显然应该自动读取那个仓的配置,“worker 启动时屏蔽” 指的是屏蔽使用系统/user的.claude/.agent 等系统级文件夹,而使用该agent自己的配置文件夹,以免被系统环境内容污染」

生效规则：

- **agent 个性配置层**：唯一来源是该 agent 自己的配置目录（推荐 `C=A/config`）。09-09 单路径裁定（4adc5a8e）继续约束**这一层**：恰好一个显式绝对路径、材料先复制进去、禁止从用户级/系统级全局目录（~/.claude、~/.agents、~/.codex、~/.pi 等）隐式加载。
- **项目配置层**：仓内 CLAUDE.md、AGENTS.md、`.claude/`、`.pi/`、`.codex/`、`.agents/skills` **随 cwd 照常自动读**。选哪个仓、个性与项目内容如何组合，是派发任务时 PAC 编写者的职责，不是 daemon 的运行时合并规则。
- **屏蔽对象只有用户级/系统级**：worker 不从机器全局目录加载任何个性配置；该槽位的语义由 C 接管。这是「以免被系统环境内容污染」的准确射程——不多不少，不包括项目层。

冲突处置：同一设置同时出现在 C 与项目配置时，C 站在被替换的「用户级」槽位上，沿用各 harness 原生的项目覆盖用户优先序；实施时逐家把这个优先序作为读数验证（G2），不由 daemon 另造一层 merge。派发者可在任务书里明示组合意图，那属于任务输入，不是配置渠道。

cwd 仍是实际仓库/工作树；不复制仓库、不把进程 chdir 到 C 来骗过发现逻辑。普通任务有意读取代码/文档不构成配置发现；本合同约束的是 harness 的自动加载渠道，不承诺模型永不读仓库文档。

⚠️ 连带更正对象（**读数更新：已完成**）：dev 曾于 6dc3fa53（#512）摘入 4adc5a8e 的单路径文档 `docs/reference/acceptance-agent-identity.md`，其 G6 前旧语义曾被本稿记为待另派小单的债；AGENTHOME5 读数（2026-09-18，dev `899a893a`，blob `39411858`）确认该稿已含 §0.4「G6 裁定：单路径只约束个性配置层」及逐节更正点，此项债已闭合，不再另派。

C 层的内容 manifest 在运行前固定，启动前和加载完成后核对；运行期间 lazy skill/context/hook 不得读取 C 中未冻结的新内容。C 的更新采用新修订与新启动；已启动会话要么读冻结副本，要么检测改变后停止使用并报错，不能悄悄跟随 watch reload。项目层不施加这套冻结——它按各 harness 今天的原生行为加载，本设计不为仓内容新增 manifest 义务。

#### 4.2.1 任务级项目规则到达方式：G6 已裁，上一稿 A/B/C 候选作废

裁定即答案：**项目规则经 cwd 自动加载就是到达方式**。不需要派单前生成新 C 修订（旧候选 A），不需要任务书搬运项目规则内容（旧候选 B），也不需要在 C 内预置多项目条目（旧候选 C）——三者都是在「cwd 不提供配置」前提下的补偿设计，前提被裁定取消后失去存在理由。同一 N 跨 harness-bridge/kanban/mfu 工作时，项目规则随各仓 cwd 自然切换，C（个性）保持不变；「跨仓规则残留」不再是配置渠道要解的问题，因为规则本来就不进入身份层。

**对 PAC `dispatch-fix` 的影响归零**：现派发流程「worker 读仓内 CLAUDE.md 与 skills」正是裁定后的正确行为。上一稿提议的必填规则交付方式、材料清单、C 修订回执等概念字段**全部撤回**，模板不需要为规则到达新增任何字段；派发者的职责表现为选定 cwd（仓/工作树）并在任务书中说明组合意图。本单不改 HQ 模板，也不再要求未来改。

仍然成立的边界：

- **权限不随规则走**：项目规则不能扩绑 secret、改 actor/socket、执行未批准 hook——这些由 env 白名单与通道注入守住（§3），与规则怎么到达无关；正文说「读文件」不产生这些权限。
- **项目层以仓/工作树为界**：祖先目录越出仓根向上到 HOME 的配置文件（如 `~/CLAUDE.md`、HOME 级 `.claude`）属用户级，仍在屏蔽范围。各 harness 的祖先遍历能否截断在仓根，列 G2 逐家验证。

邻近事件：跨仓换 cwd ⇒ 项目规则随新仓生效、C 与用户级屏蔽不变（有意如此，T18）；同仓规则在会话中途被更新 ⇒ 按各 harness 现有原生行为处理，本设计不为项目层新增 watch/冻结义务，但 C 层漂移仍拒绝（T10）；resume ⇒ 项目配置仍来自原 cwd，不混入别的仓（T18）。T18/M18 从「裁后合同」转为 P2 实施合同；T09 的诱饵负例改为用户级/系统级方向（§8）。

### 4.3 三家接法（P2：用户级配置目录重定向）与登录态迁移代价

P2 的动作是**把每家 harness 的「用户级配置根」重定向到 agent 自己的目录**（CLAUDE_CONFIG_DIR / CODEX_HOME / PI_CODING_AGENT_DIR 等），使个性配置来自 C 的受控投影；**项目层发现保持开启**，不生成投影去替代它。原生 harness 常把设置、auth、session 放在同一个用户级 home ⇒ 重定向配置根同时搬走登录态，这是 P2 的主要代价（见下）。投影文件只能来自 C 和 daemon 保留的运行/通道字段，不能反读宿主用户偏好覆盖 C。

| harness | 目标接法 | 已核现状 / 必须验证的边缘 |
| --- | --- | --- |
| Pi（RPC/TUI） | `PI_CODING_AGENT_DIR=A/secrets/native/pi`：用户级 settings/skills/extensions/themes/auth 全部改由该目录提供，settings 等无密内容从 C 投影生成。**不再用上一稿的 `--no-context-files --no-skills` 等开关**——那些关的是项目层资源，与 G6 相反；项目 `.pi/settings.json`、context files 照常生效。session 显式定到 A/state/pi | 已安装包文档：该变量存在、auth.json 优先于 env、项目 settings 覆盖 global。**尚无读数：该变量能否只重定向配置而不带走 auth**（auth.json 与配置同根）⇒ 按「重定向即登录态搬家」计代价；真实版本哨兵验收留 G2 |
| Codex（app-server/TUI） | `CODEX_HOME=A/secrets/native/codex`：config.toml 从 C 投影，凭据模式显式选择 file/ephemeral，避免 auto/keyring 回读宿主。项目层（仓内 `.codex/`、AGENTS.md）按 trust 配置照常加载，**不再借 untrusted 关项目层**。保留 env_key 不把 bearer 放 argv；thread start/resume 同步传受控 config，cwd 保持任务仓 | D 仍合 os.environ；trust_level 的项目层语义、auth 存储各模式随 CODEX_HOME 的实际行为需真实版本验证；keyring 模式可能不随目录走 ⇒ 做不到就该组合报「不支持 agent-owned auth」 |
| Claude（SDK/TUI） | `CLAUDE_CONFIG_DIR=A/secrets/native/claude` 为用户级根候选。headless SDK 现值 `settingSources=["project"]`（agent_sdk.py:179）在 G6 下**方向正确**——项目开、用户关；P2 把「用户槽」接到 C 投影（经重定向的配置根或显式传入），⛔ 不是简单加回 `"user"` 读宿主。保留 strictMcpConfig 与 daemon 签定的 WorkerChannel；自动 memory 若落用户级根则随根走，否则禁用或纳入显式修订 | 仓内祖先/子目录 CLAUDE.md 属项目层照常；**越出仓根到 HOME 的祖先文件属用户级，须验证可屏蔽**。目录 override 不证明 macOS Keychain 改址 ⇒ OAuth 登录态可能仍留宿主钥匙串；managed policy 不动。无法保证时拒绝该组合，不用「已重定向」掩盖未测 |

**登录态迁移代价（P2 必须付，不许静默）**：三家的原生认证（auth.json / OAuth token / keyring 条目）与用户级配置同居一根。重定向配置根 = 登录态搬家：每个 agent 要么在新根**重新登录**，要么由操作者按 §6 显式迁移被明确授权的条目；Claude 的 Keychain 与 Codex 的 keyring 模式可能根本不随目录改址，做不到的组合报「不支持 agent-owned auth」，⛔ 不回退宿主登录、不整棵复制宿主 auth。这正是配置根/HOME 重定向整体放 P2 的原因：P1 保真实 HOME，三家现有登录态与 tea/gitconfig/ssh/uv 全在原址（§3.2），P1 只做 provider key 注入。

**原生可变 home 的处理**：需要同目录 auth 的原生 home 放在 A/secrets/native 下，整棵按敏感私有状态管理；能拆开的日志/session 放 A/state。无密配置文件在该处只是带 digest 的生成缓存，不是新增身份输入。原生 `/settings` 或 trust 写入不能变更 C 的权威，需拒绝/显式导回新 C 修订；认证刷新只写本 agent native auth。禁止把 C 内 symlink 指到 secret 或共享 HOME 来“兼容”。

daemon 必需的网络工具/通信设置属于受信运行注入，不让 C 覆盖 actor URI/socket/认证主体；若 native 设置覆盖了这些保留键，在 spawn 前拒绝。系统 managed 安全政策不得为了通过单源测试被关闭；若它同时注入额外能力而合同无法成立，报告该运行选择不支持，交 owner 定后续，不豁免验收。

**被否方案**：直接把 HOME 设成 C 会污染 manifest 且属 P2 前置未验的整棵搬家；上一稿「受控投影 + 关闭项目发现」被 G6 否定——该关的是用户级，不是项目层；为了关用户级发现改 cwd 会改变任务目标；只改配置根而不核钥匙串/keyring 会把「登录态未迁」误报成「已隔离」。把“推荐 A/config”写成唯一合法地址也额外缩窄已裁单路径合同，本稿不这样做。

## 5. agent 实体化旧支：G1 owner 已定去留，不盲 merge

下表是所读语义，不是对旧分支整树的产品签核。远端头本轮重新 `ls-remote` 与原读数一致；去留按 spec 追加2 的 h2b owner 结论，不再列未知。

| 分支 / 固定头 | 已读的相关输入 | 承接与不承接 |
| --- | --- | --- |
| intg/agent-identity / fec94ee9 | registry 的 legacy_capabilities 与拒 runtime 混入；ports 的 AgentRuntimeProjection、binding generation；提交说明的 T-I03/I06/I07 | 目录是实体的附属资源，运行事实仍归 daemon；不重复实体表/命名域。不继承未经同树对照的全量绿声明 |
| intg/agent-identity-config / 4adc5a8e | docs §4.1、AgentConfig/ConfigManifest 全文件、test_agent_config_contract 全文件、composition/application config 传递站点 | **单源裁定生效，适用面经 G6 修正为个性配置层**（§4.2）；复用 parser/manifest 及负例意图，其 T-C02 的 cwd 诱饵负例按 G6 改写为用户级诱饵（T09）。现测试含 InlineLifecycleManager，只证明 schema/落盘/manifest，不证明三家真实加载；不拿它当接线已做完 |
| feat/agent-entity / 9742db91 | registry 原 JSON path_for/to_json 形态及与 merge-base 的 registry 差异；liveness 全文件的 live_binding/displaced_by | 一名一实体、换 harness 不换实体、在线不能强抢。旧 file registry 不能替换 D 的 SQLite；registered_status 旧默认 offline 不能搬回来压掉未知 |
| fix/codex-provider-secret-custody / 0c988ed5 | model_provider、_exec_env_socket、harness-model-providers 文档及 codex 增量 | 保留 env_key/argv 无密、TUI 私有交付原则；**不保留** daemon env/共享 Pi auth fallback，也不保留 exec helper 重新合 ambient env 的行为 |

**G1 处置结论（h2b owner，所据 dev=D；本轮复核命令见附件 §7）**：

| 分支 | 已定处置与读数 | 后续责任（不是本单动作） |
| --- | --- | --- |
| feat/agent-entity | **已被取代**；`769f96665e9ee210602d45ba474fe63c2364363a` 明示按语义移植 #90 尾部含9742db9至SQLite，且为 D 祖先 | 不恢复旧JSON实体树 |
| fix/codex-provider-secret-custody | **已被取代**；`git cherry D 0c988ed5` 三项全 `-`（c570c04c、b92ea072、0c988ed5），补丁等价已在 D | 已入树不等于其所有行为符合新设计；P1仍须先迁后删共享fallback |
| intg/agent-identity | **保留作参考，P1 移植后关** | P1承接者逐项移植/验收后交 owner 关闭，不提前认为已移植 |
| intg/agent-identity-config | **已被部分吸收（读数更新）**：其单路径文档已摘入 dev（#512，6dc3fa53）且 G6 适用面更正已完成（AGENTHOME5 核验，见 §4.2）；单源裁定适用面经 G6 修正为个性配置层（§4.2）；代码（parser/manifest）仍留作 P1 参考 | 代码移植属后续实施单；本文不再列「现在摘取」为待办 |

**G1 的处置决策已闭合，不等于后续移植/摘文档/关闭已完成。本单不删除任何远端分支。** 若搬代码，基线取源再与实施时 dev 对齐。旧 `src/h2b/` 与现 `src/hyprial/` 不以字符串替换视为集成完成，检查合并产物的真实调用与测试量尺。本稿不授权恢复四个旧整树。

## 6. 发布前迁移：显式清单、单权威，不写 fallback 兼容代码

本单不做生产目录盘点，**真实存量文件数及未知项数未观测**。这里只按源码/spec已知类别给规则，不能称“全部已分类”。

1. 操作者在授权窗口只列文件名/元数据先做计划：每项记录原路径、归属级别、目标 agent/adapter、目标路径、写读组件、当前版本与迁移状态。`login.json` 原位；被 channels 引用的 lark 凭据原位；现共享 Pi DeepSeek key 由用户/操作者明确迁到 `H/secrets/providers/deepseek.json` 并授予 fast worker 的逐agent grant；人工 agent 文件必须人工确认授权目标；未知、重名、已存在目标、符号链接/非正规文件、大小写冲突均阻断该项。
2. 分离“别名迁移”与“账号切换”。真正主体变更不能仅修改 owner 字符串就复用旧 token。凭据授权、native auth 重登录另确认。不得读取全部 secret 内容来猜它属于谁。
3. 停止**受影响 agent**并确认当前生命周期不再写凭据；不为搬某个 token 停全部生产。若关停状态未知不执行迁移。先获得源/目标围栏并验证目标空闲，建立 0700 staging、0600 文件，以批准清单逐项迁入；仅迁移工具在此授权窗口读指定密值，绝不打印/提交。
4. 验证目标可用且归属正确，原子发布目标并提交 provisioning/secret 绑定；源在提交后退出旧使用路径。跨 FS 的 copy+fsync+原子发布+删除源需日志化事务阶段与重入判据，不能假设 rename 总可用。暂存副本不构成第二可用 credential source。
5. 已迁移的凭据启动仅认新绑定，不 fallback 到旧目录、daemon env 或共享 native auth；P1针对provider，P2才迁的工具/native认证在P1仍沿现合同，不谎称已迁。失败则停止受影响 agent，保留可诊断的中间状态，执行人工确认的恢复/修复；**不发布一个自动“先新后旧”读取器**。commit 后删除源失败应报“迁移未收尾/旧副本待清理”，不谎报完整完成。
6. 轮换同样走原子替换+版本回执；当前进程仍可能持旧 env，完成凭据撤销需受影响进程停止/重启与服务端吊销。文件删掉不等于旧 token 已失效；不以 Python 清掉局部变量宣称内存擦除。
7. Native OAuth 二期优先逐 agent 重新登录/授权，不整棵复制 OS HOME/全局 auth.json；多 provider auth 文件只迁被明确授权的条目。钥匙串不可显式分域的组合维持“不支持 agent-owned auth”，不回退宿主登录。

可达诊断只报告该操作者既有权限内的条目 ID/阶段；“可列/可诊断”不授予读取值、绑定别人的令牌、删除别人的 home 或停止无关 actor。日志不持 secret 明文/摘要；凭据测试仅用固定非密哨兵（T07/T12/T13）。

## 7. 分期、先后与实施前闸门

| 阶段 | 内容与必须同时成立的边界 | 不能声称 |
| --- | --- | --- |
| P0 设计/决策 | 本设计稿（已出 Draft）；单路径承接（适用面经 G6 修正）；G1处置已定、**G6已裁**（项目 cwd 照常读、屏蔽用户/系统级）、工具/native能力矩阵；**P1a 已实施合入（#513）** | 不代表 P1b 及以后已获授权或完整验收绿 |
| P1a 基础（不启用新 worker 模式） | **✅ 已实施并合入 dev（PR #513，merge `899a893a`，2026-09-18）**：现 registry 附属 home provisioning、名字/FS/账号围栏、三级 secret 清单与 resolver、完整 env 的正式内部类型，均按设计落地并有测试/变异证据；另落地了设计文本之外的评审衍生件：owner 迁移 custody 闸（grants 拦/homes 报告，含读不到≠没有 fail-closed）、拒启具名错误码接 login S3 具名结局、entityToken 外泄面清理、home 路径前缀作为迁移残差（读数见附件 §9） | 仅 mkdir+env 参数不是交付；阶段接口未贯通不能切生产——P1a 未接任何 spawn，此约束仍全部成立 |
| P1b 首个可用 API-key 闭环 | ②provider注入+最终spawn白名单；先迁fast再去共享provider fallback。**HOME/XDG/原生home保持现址**；SSH_AUTH_SOCK必须、Git单次insteadOf精确例外；保住tea/git/ssh/uv现路径。至少一家验证provider来源正确与真实commit+push | 不称工具/native auth已独立，不靠换HOME绕原生provider优先级；不称完整config/任务规则接线已做；不能先发秘密以后再补白名单 |
| P2 原生配置/认证覆盖 | **HOME/XDG与原生home重定向统一在此**；三家用户级配置根重定向 + C 投影（项目层发现保持开启，§4.3）、native auth刷新/会话目录迁移与登录态搬家代价、TUI/恢复/子MCP、工具主体验证，逐家真实E2E与显式启用 | 不承诺任何版本通吃，不绕开managed policy，不把OAuth延期变成永久漏验，不把目录重定向叫进程隔离 |
| 后续容器化 | 真正进程/挂载/用户隔离 | 本期不设计或实施，当前也不称已隔离 |

**与在飞工作的顺序决定**（AGENTHOME5 重取读数，2026-09-18）：

- DSH PR-B **#496**（**读数：open，未合**）：先由该线完成“每 worker 自管 DSH 子进程”并落 dev，再接本单 DSH env；不在共享 DSH 服务上注入 per-agent provider key。其 `dsh_worker_home` 是 `$STATE/dsh/N/home`，P1 保留落地合同，A/state/dsh 或 native 根迁移属 P2，不能留下两个默认权威。**P1b 对 #496 的依赖仅限 DSH 启动入口：provider 注入闭环（resolver→LaunchSpec→最终 spawn 白名单）不依赖它。**
- init-login **#508**（**读数：已合入**）：login.json 不搬；原顺序约束已满足。真实账号切换线 **#493**（S2/S3）**已合入**：S3 编排对 custody 拒启报具名结局（`OWNER_MIGRATION_CUSTODY_*`，identityCommitted=true，不倒带）；但「别名 vs 真实切换」的**显式选择 CLI 仍缺**（dev 上 `revoke_secret_grant`/`migrate_owner`/`rotate_secret_authority` 在 cli.py 命中 0，读数见附件 §9）。
- PAC backend **#505**（**读数：open，未合；已并入集成 PR #599，#599 亦 open 未合**）：先取其 application facade 落地接口再接 runtime/config 投影；不因本功能让 PAC 读取密值。**P1b 的 provider 注入闭环不依赖 #505/#599；依赖它们的是 PAC 侧投影/后续接线（§4）。**

### 7.1 P1b 前置条件（每条注明来源；全部满足或显式豁免前，P1b 不开工）

1. **owner 迁移闸的作用域前提失效时必须重评**（来源：e2e-verifier 对 #513 的 review 336 第四条，已录 §1.2 注）：P1a 的「grants 拦、homes 只报告」依赖「home 内无凭据材料」；P1b 让 home 成为真实 HOME 后该前提失效，闸的范围（是否扩到 home 内凭据材料、如何区分别名改写与真实切换）必须重新评估——⛔ 不得默认沿用 P1a 结论。
2. **pac-graph 库的 owner 迁移归属必须先定**（来源：h2b-developer 审 #536 评论 13001 之 L2，hq spec 追加节；AGENTHOME5 于 dev `899a893a` 实测确认缺口仍在）：`owner_migration` 的冻结输入清单（`MIGRATION_DATABASES`）**不含 `pac-graph.sqlite3`**，而 pac 库的 `graphs.owner`/`nodes.owner` 与 principal URI（`user:<owner>`、`agent:<owner>:<machine>:<actor>`）都携带 owner 拼写；pac 侧 `migrations.py`（SCHEMA_VERSION 8）为纯 schema 迁移，**不碰 owner**。⇒ 今天**没有任何一方迁移 pac-graph 的 owner**：owner 改名后 PAC 图的授权会以 #536 B2 同形锁死。P1b 之前必须二选一并落地：把 `pac-graph.sqlite3` 纳入 owner 迁移输入集（含分类/残差验证），或明写由 pac 侧迁移承担。
3. **账号切换再授权围栏二选一**（来源：hq spec 追加节，h2b-developer 审 #513 评论 12723 附条件 1）：#493 的 S2/S3 已合（含 P1a custody 闸 + S3 具名结局），但显式再授权选择的 CLI 未接线；⇒ P1b **不得先于**「#493 再授权围栏完整落地」开工，**或** P1b 自带「home 内有凭据材料 ⇒ 拒绝静默改写」的闸。两者选一个，写成 P1b 验收项，⛔ 不留口头约定。
4. **依赖状态声明**（来源：AGENTHOME5 读数，2026-09-18，附件 §9）：#496 open（P1b 仅 DSH 入口依赖）；#505/#599 open 未合（P1b 不依赖，PAC 投影依赖）；#508 已合、#493 已合（顺序约束已满足）。
- 四旧支：按 §5 已定处置执行；承接单路径语义不等于等待全部旧支 merge。文档摘取另单；P1按选定语义重接，说明搬运源与差异，移植后再关闭参考支，不删远端。

**G1–G6，实施派单必须填的闸门**：

| 闸门 / 谁闭合 | 已有读数 | 还差什么 |
| --- | --- | --- |
| G1 四支去留 / h2b owner | **决策已闭合**：entity/custody被取代；identity参考并P1a已实施后可关；config 文档已摘入 dev 且 G6 更正完成（§4.2 读数），代码 P1 参考；§5有SHA/cherry读数 | 后续移植/关闭由对应单验收；非本单已执行，不删远端 |
| G2 config/native能力 / 实施者+强力审查 | §4的源码/厂商文档，D没有配置根重定向接线 | 精确版本/加载哨兵/原生auth来源；用户级屏蔽（含越仓根祖先文件）、项目覆盖用户优先序、auth 是否随根走的逐家读数；无法屏蔽用户级必须拒绝该组合 |
| G3 secret映射与工具作者身份 / 操作者 | provider env映射已有源码；Forge默认身份为alicesmith | 每个工具版本如何不走共享登录态、被授权secret条目及实际whoami（不读密值）；e2e独立账号验证 |
| G4 账号切换、FS碰撞和存量迁移 / owner | 名字词法、URI迁移、已知文件类别 | 非生产夹具上的事务与碰撞测试；发布窗口的完整元数据清单及处理未知项 |
| G5 launch闭环 / 实施者 | D中多个env二次合并点；owner定P1保留HOME/SSH必须/Git精确例外 | 每家逐键env清单与真实headless commit+push，P1既有工具路径保真；最终spawn/恢复/TUI/broker/sub-MCP全链证据；仅上游env单测不够 |
| G6 跨仓任务规则 / **已裁闭合**（Allen 2026-09-14 飞书两句，spec追加3） | 项目 cwd 配置照常自动读，组合是 PAC 编写者职责；屏蔽用户/系统级、改用 agent 配置目录；单路径裁定只约束个性层 | 决策已闭合。剩余为实施验收：T09（用户级诱饵不生效、项目照常生效）与 T18（跨仓切换）；dispatch-fix 模板不需新增规则交付字段 |

G2/G3 未就绪可暂不发布对应 harness/工具，豁免该组合本次上线，**不豁免显式 NOT_RUN/不支持标识、拒绝降级、其他已上线组合的归属正确性、未来补验**。P1a 未启用不需要假造产品调用绿，但启用前 P1b 全闭环不可省。

## 8. 实施验收草案：每条修法有测试与固定反例

以下测试文件名是拟新增位置，T/M 名是合同；不是已存在的成功 API。先用当前真实端口类型落位，再注入外部 I/O seam。所有 secret 用临时目录里的非密哨兵，不用生产用户/auth 文件。

| ID（阶段）/修法 | 真实测试动作与预期 | 同一 M-ID 固定反例意图 |
| --- | --- | --- |
| T01（P1）名字键/身份连续性 | 真实 AgentRegistry 创建/重开库，owner别名迁移、换harness、换cwd后A相同；foreign URI同短名拒绝；账号切换未授权不交密值 | M01：取URI最后一段跳过native/local授权；应仅foreign用例红 |
| T02（P1）目录围栏 | 新建agent前并发reservation同名、Alice/alice在大小写不敏感临时FS、symlink/非法路径/预存非本次目录；不得共用/覆盖或假报实体创建成功，失败回执可读 | M02：去掉FS别名检测；应仅碰撞用例红。FS不可提供则该真实场景NOT_RUN，不造一个恒无碰撞桩 |
| T03（P1）完整env不串号 | 父env放A/B/用户/adapter哨兵，真实单个子进程seam依次及有限并发启动A/B，断最终环境字段集与值，daemon env前后等式；secret不进父env | M03：恢复最终spawn的os.environ合并；预计ambient哨兵负例红 |
| T04（P1/P2）所有启动入口 | 参数化headless、CLI TUI、tmux broker、恢复/fallback、子MCP、DSH自管进程；用正式LaunchSpec/WorkerChannel贯通到最终exec，断不重合ambient env；子MCP不默认获provider key；每家冻结逐键env表并在授权测试仓真实commit+push验主体 | M04：仅恢复broker execvpe的ambient合并；预测broker用例红，不用上游mock调用数替代 |
| T05（P1）移除共享auth fallback | 先在临时H模拟当前共享DeepSeek来源→用户providers落点→A显式grant，fast闭环通过后才切新resolver。另例A未获grant且无私有key、临时宿主HOME两处Pi auth有哨兵时必须拒绝；不能断掉已迁移fast，也不能偷读未授权宿主 | M05：恢复require_deepseek_key的HOME回读；缺A凭据用例红 |
| T06（P1 provider/P2工具）逐agent最小映射 | A声明DeepSeek/B声明OpenAI；正式provider配置函数只产生各自key/必要别名；精确工具认证路径属于自身；额外config绑定他人secret拒绝 | M06：按owner全量分发secret；字段集/主体对应负例红 |
| T07（P1）无密持久化/诊断 | 非密哨兵经过失败与成功，枚举argv、desired-state、公开IPC/PAC、日志、错误/trace；既有login T10/T11守卫仍成立；不序列化private env | M07：将private env加进LaunchSpec投影；公开字段集/哨兵用例红 |
| T08（P1）单源形状 | 复用4adc5a8e实际AgentConfig/CLI输入：第二source、顶层list、precedence均在运行前拒绝；C可为A/config或另一个批准绝对目录，不能默默追加/改址 | M08：原样重跑旧M-C01′（接受第二source）而不是换成不同错误；完整文件仅第二source条目红 |
| T09（P2）用户级屏蔽/项目照常 | C固定；临时用户级根（宿主 ~/.claude、~/.codex、~/.pi 对应位置与越仓根的 HOME 级祖先 CLAUDE.md）放诱饵哨兵，cwd 仓内放项目哨兵（settings/CLAUDE.md/AGENTS.md/skills）；三家逐个实跑：用户级诱饵不得到达，项目哨兵与 C 哨兵必须到达 | M09：去掉一家的配置根重定向（或 Claude settingSources 加回 user 读宿主）；用户级诱饵用例红。另一向独立反例：关掉项目发现（如 settingSources 去掉 project）；项目哨兵用例红。两向各自预先点名，不互相充数；无法造成预测红先修仪器，不宣称变异成功 |
| T10（P2）投影与TOCTOU | C与native auth/state分开；冻结后换内容/链接、lazy read/watch reload、restart/resume；实际加载拒绝漂移且认证刷新不改变C摘要 | M10：取消manifest验证；对应漂移用例红；摘要预先从C固定，不能事后自签 |
| T11（P1/P2）缺失/错误/保留通道 | required缺失、optional真缺失、PermissionError、坏格式、越界链接、试图覆盖daemon actor/MCP通道；失败要有阶段/条目，不能启动或窃取通道 | M11：OSError统一missing；权限错误用例红 |
| T12（P1）迁移事务/重入 | 临时H，login/lark/agent/未知四类；目标预占、跨FS、写失败、发布后源删除失败；独立读回绑定/文件状态，不丢源、不覆盖别人、未完成不得报完成 | M12：源删除失败后仍回completed；收尾失败用例红 |
| T13（P1/P2）轮换/销毁/重建 | 下线后同名新实体不能获旧凭据；轮换后新进程获新revision，旧进程未退出不报撤销完成；home清理只匹配自己的receipt | M13：仅凭短名自动重用旧home；同名重建负例红 |
| T14（P2）native认证/工具主体 | 每家精确版本用隔离native root、不同认证哨兵；缺本agent auth但宿主存在时拒绝；刷新只写该root；Forge作者与review主体不同，用whoami/API主体回执而非文件名作证 | M14：一条native auth路径退回宿主；该主体负例红；外部认证不可用记NOT_RUN |
| T15（P1/P2）边缘失败可见 | 清理/回滚/补偿、后台线程、finally、最后重试、facade缺方法和未知else逐处故障注入；耐久失败状态与CLI同现，原始密值不进日志；有起始时间/预算/下一责任人 | M15：后台catch仅log后回成功或丢状态；预测该边缘用例红 |
| T16（P1，追加2修法1）HOME分期保真 | 用真实LaunchSpec到最终子进程，临时HOME里放非密tea/gitconfig/ssh路径哨兵，断HOME原值、批准XDG现址/缺席不变、未建A/state/home；原生根保持既有值。再在授权环境做真实tea默认登录身份、uv与git/ssh E2E；不读取生产secret取证 | M16：仅将P1 HOME改回A/state/home；冻结的HOME保真用例应红，collected不变；不以假tea桩代替真实工具读数 |
| T17（P1，追加2修法2）Git/SSH能力 | 真实Git在临时仓用非密URL重写三键验证insteadOf生效；字段集合拒多余索引/不完整组/非批准key；最终SSH_AUTH_SOCK必须等于授权路径且可用，缺失明确拒绝。授权测试仓由真实headless worker commit+push核远端主体 | M17：仅删除白名单的GIT_CONFIG_VALUE_0；预先点名的完整三键/重写用例红；另独立冻结去掉SSH_AUTH_SOCK的邻域反例，不能替换M17充数 |
| T18（P2）跨仓项目规则随 cwd 切换 | 同一 N 先仓A后仓B真实启动：各仓项目哨兵分别生效、互不残留；C manifest 前后不变；用户级诱饵两仓都不生效；resume 会话仍读原 cwd 的项目配置，不混入新仓 | M18：在项目配置加载点缓存/钉死第一个仓的结果；仓B用例红、C 不变用例仍绿；恢复原实现后同文件绿 |

### 8.1 真实仪器落位（不得复制本文造成功桩）

- D：`AgentRegistry/Agent`、`CreateAgentCommand`、`LifecycleSpec`、`HarnessLaunchSpec`、`WorkerChannel`；沿 `tests/test_final_composition_lifecycle.py` 的生产四域 composition 验先建/补偿，独立 SQLite 连接验耐久状态。不要只用旧 config 测试的 InlineLifecycleManager 证明生产接线。
- D：`tests/test_harness_model_provider.py` 的实际 provider 函数与 LaunchSpec（特别是125行开始的旧 Pi fallback 测试，实施需**改成拒绝回读的负例，不是删掉覆盖**）；`tests/test_tui_tmux.py` 的 exec/broker seam；`tests/test_login.py` 的 T10/T11；adapter 用 `contract/e2e/run-four-lark-adapters.py` 中真实公开 CLI 形态作参考。
- 4adc5a8e：`AgentConfig/ConfigManifest` 与 `tests/test_agent_config_contract.py`。其三种非法形状测试、真实临时文件摘要可复用；文档的 T-C02/C03 是验收意图，不是这六个现测试已证明三家加载。
- 新 private launch env/secret resolver 的正式内部类型必须在实施首个 PR 冻结，再用该类型构造测试；**本设计不定义一个随手的 `{ok:true,loaded:true}` 伪协议**。

### 8.2 红过绿回与最终闸门

每个 M 独立运行，先冻结 patch/blob、测试文件、collected、预测失败 nodeid 集合及异常/断言文本；正确实现绿 → **同一个突变**下完整文件红（不加 `-k`）→ 撤突变并核源 blob 恢复 → 同文件绿。若旧仪器空跑，修断言后仍重跑原 M；多红/少红都不算达到预测。新断言在旧实现上造红（②）与突变造红（①）分开记。

实施焦点：P1a 实际落在 `tests/test_agent_home_dirs.py`、`tests/test_agent_secret_delivery.py`（含 owner 迁移/custody 闸节点）；`test_agent_config_loading.py`、`test_agent_home_migration.py` 属后续切片，尚未创建。邻域按路径事后状态包括 registry/hosted/owner迁移、生命周期补偿、CLI/TUI、恢复、MCP、provider、adapter/login，而非只按文件位置。

实施时全仓 `ruff check .`；焦点、上述邻域、全量 pytest；新 import/事件/URI/错误码逐项跑仓级守卫。全量一次直接落盘不过管道；失败先同树对照，setup级联按共享fixture归因，禁止夹带修共享红。CI 成功不能由本设计的未运行项代替。

本设计阶段：不执行产品突变、不新增测试代码、不做真实认证场景。**P1a 的 T01/T02/T06/T11/T13 及对应 M01/M02/M06/M11/M13 已随 PR #513 实施并通过（含变异证据，见 #513 的 evidence 文件）；T03/T04/T05/T07/T12/T14–T18 仍为合同，未运行。**G1用§5的祖先/cherry/owner处置文稿复核，不能用未来产品测试冒充已执行分支关闭。实际 lint/静态文档核验与未运行账见附件。强力模型的安全边界审查仍是后续闸门，作者不能自签。
