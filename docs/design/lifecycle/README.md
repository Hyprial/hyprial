# 业务生命周期设计（0.5.5.4）

本目录存放各业务对象的生命周期设计，并把每一步对应到 CLI 命令和 daemon 方法。
它是业务组件开发流程的第一步：先有生命周期，再拆成接口（CLI 命令 / daemon request 方法），
然后才写基础封装组件和完整业务组件。

- 负责人：陈瑞华（设计）；R&D 负责人 `emergent-org-structure-refactor`（接口对照复核、缺口立项）。
- 分支：`design/lifecycle-flows`，WIP PR 进 `release/v0.5.5.4`。设计和后续完善都推到这个 PR。
- Allen 2026-10-31：本设计与 0.5.5.3 的集成和发版并行，不阻塞后者；设计与完善进入 0.5.5.4。

## 目录约定

```
docs/design/lifecycle/
  README.md               本文件：目标、做法、提交方式
  _template.md            单个生命周期的模板（复制后改名）
  <对象>.md               每个业务对象一份，例如 org.md、member-invite.md、agent.md、
                          device.md、model-credential.md
  gaps.md                 对照后发现的接口缺口汇总（R&D 负责人维护，可由设计方补充）
```

一个对象一个文件，不写成一份大文件。对象之间的衔接用链接表达，例如邀请完成后进入成员生命周期。

编号约定：本目录的旅程编号写成 `LC-<对象>-NN`（例如 `LC-INVITE-01`）。引用其他文档的编号时一律带文档前缀，因为同一个编号在不同文档里可能指不同的事：例如 dev-01 的 `J10` 是模型服务旅程，组织交互设计的 `UJ10` 是 Agent 迁移。写成 `dev-01 J10`、`org UJ10`，不单独写 `J10` 或 `UJ10`。

## 产品侧首批设计（Draft）

下列文档按 `intg/gui-init@365698612` 的 GUI 设计资料整理，记录对象状态、旅程和权限。每份文档都标明已实现、在途或待决的来源状态。旅程表中的接口列等待 R&D 负责人按最新 `dev` 对照；`待研发对照` 不表示已有契约。

| 对象 | 文档 | 主要边界 |
| --- | --- | --- |
| 组织 | [org.md](org.md) | 组织状态与“当前组织”、成员离开分开 |
| 成员邀请 | [member-invite.md](member-invite.md) | 生成链接、网页接受、本机领取、加入分开 |
| 成员关系 | [member.md](member.md) | Casdoor 成员事实与 OrgFS、GUI 投影分开 |
| Agent | [agent.md](agent.md) | 身份、运行、凭据授权、组织归属分开 |
| 设备 | [device.md](device.md) | 设备身份、目录同步、网络可达性分开 |
| 模型凭据 | [model-credential.md](model-credential.md) | 保存、授权、启动、真实回复分开 |
| 消息连接 | [message-connection.md](message-connection.md) | 平台连接、绑定 Agent、沟通方式分开 |
| 对话会话 | [conversation.md](conversation.md) | 会话目标、消息受理、真实回复分开 |

接口首轮完成后，产品侧复核状态与接口差异，再把确定的 `有差异`、`仅 CLI`、`缺口` 写入 [gaps.md](gaps.md)。消息连接与对话会话的正式接口和权威状态仍待研发按最新 `dev` 对照。

## 第一部分：如果还没有整理，怎么整理

按对象整理，不按页面整理。页面是生命周期的投影，同一个对象可能出现在多个页面上。

1. **列对象。** 写出用户能感知、有状态的业务对象，例如组织、成员、邀请、agent、设备、
   模型服务凭据、消息会话。每个对象写一句定义和它的拥有者（谁能创建、谁能删除）。
2. **画状态。** 每个对象列出全部状态和状态之间的转移。用 mermaid `stateDiagram-v2`。
   状态用固定词，不用叙事，例如：`未创建`、`待确认`、`生效`、`暂停`、`失败（可重试）`、`已移除`。
   每条转移标出：谁触发（角色）、前提条件、结果状态。
3. **写旅程。** 把常见的转移串成用户旅程，每条旅程一节，结构如下：
   - 前提（角色、已有状态）；
   - 步骤表（见模板）；
   - 异常：取消、失败、权限不足、网络断开、并发冲突，每种写出停在哪个状态、能否重试；
   - 衔接：完成后进入哪条旅程或哪个对象的生命周期。
4. **标权限。** 每一步写清角色（所有者 / 管理员 / 成员 / agent / 未登录用户），
   以及没有权限时看到什么。
5. **不写实现。** 不写按钮样式、不写接口名；接口名在第三部分对照时填。

已有可参考的写法：`intg/gui-init` 分支上的 `gui/docs/org/interaction-design.zh-CN.md` §16（UJ01–UJ12），
以及业务口径 `gui/docs/org/prd.zh-CN.md`。组织模块可以直接从那里整理成生命周期，不必重写。

## 第二部分：怎么扫描 daemon_request / CLI，了解已完成的能力和实现逻辑

以 `dev` 分支最新版本为准（本 PR 的基线较旧，查现状时请看 `dev`）。从总表到细节分四步。

1. **CLI 命令全集。** `contract/cli/surface.json` 是已安装 `hyprial` 命令树的完整快照：
   每条命令路径、参数、选项、默认值、隐藏标记和一行说明。
   想看某一族的说明，在任意机器上执行 `hyprial <命令族> --help`（只读，不连 daemon）。
2. **daemon 方法全集。** `docs/design/daemon-request/ipc-inventory.md` 列出 daemon 现有的 195 个方法，
   每个方法附源码位置和对应的 CLI 命令族。
   `docs/design/daemon-request/cli-inventory.md` 反过来列出每条 CLI 命令用了哪些方法，
   并分类：`I` 已走 daemon；`N` 业务逻辑还在 CLI 进程里；`I+N` 混合；`B` 引导；`P` 纯展示；`D` 暂不迁。
   这两份表的基线是 `4a5177`，之后新增的方法以源码为准（见下一步）。
3. **已声明契约的方法。** `contract/daemon-request/openapi.json` 由 daemon 的 `MethodSpec` 生成：
   `x-hyprial-methods` 是已声明的全部方法，含参数、返回、错误码 schema；
   `paths` 只含开放给 HTTP 的方法。每个方法的示例在 `MethodSpec` 的 examples 里。
   这一层是 GUI 能直接使用的接口；方法名在 inventory 里但不在这里，说明它还没有契约，GUI 不能直接用。
4. **看实现逻辑。** 从 inventory 的“分发来源”列跳到源码（`src/hyprial/daemon/impl/...`），
   看这个方法读写什么状态、什么时候报错。看不懂的，在 PR 里留言，由 R&D 负责人补充说明，
   不要自己猜。

不要在自己的日常环境里执行会改状态的命令来“试一下”。需要实测时，在 PR 里写出想验证的场景，
由 R&D 侧在隔离环境中执行并把输出贴回 PR。

## 第三部分：怎么把生命周期和 daemon_request / CLI 对照，明确功能接口

对每条旅程步骤表的每一行，填写后四列（模板里已经留好）：

| 列 | 怎么填 |
| --- | --- |
| CLI 命令 | 完成这一步的现有命令，例如 `hyprial org invite`。没有就写 `—` |
| daemon 方法 | 这一步实际调用的方法，例如 `org.invite`。来自 cli-inventory 或源码 |
| 现状 | 四选一：`已有`（方法和契约都在，行为一致）；`有差异`（方法在，但行为或字段和设计不一致，写出差在哪）；`仅 CLI`（逻辑只在 CLI 进程里，cli-inventory 标 N）；`缺口`（没有方法） |
| 依据 | 源码位置、契约位置或实测记录；没有依据的不写“已有” |

判断规则：

- **以 daemon 为准，不以 CLI 为准。** CLI 能做但 daemon 没有方法，GUI 用不了，记 `仅 CLI`。
- **只读和写分开判断。** 能查状态不代表能改状态，一个步骤里两者都有时分两行写。
- **差异要写具体。** 例如“设计要求邀请可撤回，`org.invite` 没有撤回方法”，而不是“部分支持”。
- **设计改还是接口改，先不定。** 对照只负责把差异列清楚；由 Allen / R&D 负责人在 PR 评审里决定。

对照完成后，所有 `有差异`、`仅 CLI`、`缺口` 汇总到 `gaps.md`，每条一行：
对象、旅程步骤、需要的能力（读/写）、现状、建议。R&D 负责人据此拆成 0.5.5.4 的工作项。

## 提交方式

1. 拉取分支：`git fetch origin design/lifecycle-flows && git switch design/lifecycle-flows`。
2. 一个对象一个提交，提交说明写 `docs(lifecycle): <对象> lifecycle`。
3. 只改 `docs/design/lifecycle/` 下的文件；其它目录的改动请单独提。
4. 推送到同一分支，PR 会自动更新；每推一次在 PR 留一句“新增/更新了哪个对象”。
5. 先交第一部分（状态和旅程）也可以，接口列留空；R&D 负责人会先做一轮对照，你再复核。

## 可直接交给 agent 的提示词

如果用 agent 帮忙整理，可以把下面这段原样交给它：

> 你在 HyprialOS/harness-bridge 仓库的 `design/lifecycle-flows` 分支上工作，只改 `docs/design/lifecycle/`。
> 读 `docs/design/lifecycle/README.md` 和 `_template.md`，按其中三部分为【对象名】写一份生命周期设计。
> 现状一律以 `dev` 分支为准：用 `git show origin/dev:<路径>` 读
> `contract/cli/surface.json`、`docs/design/daemon-request/ipc-inventory.md`、
> `docs/design/daemon-request/cli-inventory.md`、`contract/daemon-request/openapi.json` 和相关源码。
> 每一个“已有”都要写出依据（文件和行号）；找不到依据就写“缺口”或“待确认”，不要推断。
> 不执行任何会改变本机 hyprial 状态的命令，不读取 `~/.hyprial`，不碰凭据。
> 完成后把发现的差异和缺口追加到 `gaps.md`，并列出需要人来决定的问题。
