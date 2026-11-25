# GUI 组织模块 PRD

## 0. 文档信息

| 项 | 内容 |
| --- | --- |
| 状态 | 定版 v1.0（2026-10-29），与交互设计 v1.0 一起通过审查，审查记录见 [README](README.md#审查记录)；v1.1（2026-11-23）按 OC4 组织模型修订：用户可同时属于零个或多个工作组织（`contract/org-network/membership.md`） |
| 负责人 | 姚升悦（dev-04 组织、邀请和成员）；起草：cc-gui-orgs |
| 适用范围 | 根 `gui/` 共享网页 UI 中与组织相关的全部功能（不只组织 tab） |
| 代码基线 | 后端：`dev` 5c5f568a1；GUI：`intg/gui-init` 66d18aaa8（含 `dev` 5c5f568a1） |
| 配套文档 | [GUI 组织模块交互设计](interaction-design.zh-CN.md)（GUI 修改的标准）；[目录说明](README.md) |

### 0.1 本文写什么

本文列出 GUI 中组织模块要支持的功能，以及每个功能的后端现状和 GUI 现状。
功能只来自三类来源，不做新的发明：

1. **已实现**：`dev` 上已有的 CLI、daemon 能力。
2. **在途规划**：已有设计文档或分支、尚未合入 `dev` 的工作。
3. **用户要求**：姚升悦 2026-10-29 给出的业务逻辑、用户旅程与决定。
   - 其中既未实现、也没有在途规划的部分记为“缺口”（第 11 节）。
   - 姚升悦决定先在 GUI 中设计的缺口（部门与结构配置、改名），交互见交互设计。
   - 其余缺口不在本文补设计。

本线只做 GUI 设计，不接后端，也不实现后端功能（任务登记第 3 节第 5 条）。

### 0.2 来源与标注

| 标注 | 含义 |
| --- | --- |
| 〔已实现〕 | `dev` 5c5f568a1 上存在，给出源码路径 |
| 〔在途〕 | 分支@提交，未合入 `dev`；括号内注明状态（WIP、未批准实施等） |
| 〔GUI〕 | `intg/gui-init` 66d18aaa8 的 GUI 现状 |
| 〔缺口〕 | 用户要求，但未实现，也没有找到在途规划 |
| 〔待确认〕 | 需要负责人决定，见第 10 节 |

主要来源：

| 编号 | 来源 | 状态 |
| --- | --- | --- |
| S1 | 姚升悦 2026-10-29 业务逻辑与用户旅程（会话原文） | 用户要求 |
| S2 | 组织成员模型 #2078：`design/org-membership-model` @2dd2227fb，`docs/design/org-membership-model-2026-10-29.md` | 在途，WIP，未经 Allen 确认 |
| S3 | 研发负责人 emergent-org-structure-refactor 2026-10-29 17:1x 答复 | 设计稿 WIP，未经 Allen 确认 |
| S4 | hq-adjutant 2026-10-29 答复（org-context 字段、link、OrgFS 实测） | 实测与代码核对 |
| S5 | agent 迁移 R4.2：`design/agent-transfer-refactor-plan-20261027` @37ae1cfec，`docs/design/agent-move-refactor-proposal-20261028.md` | 在途，用户已选方案，未批准实施，目标 0.5.5.2 |
| S6 | 网络统计：`feat/network-inspect-20261012` @d1ea0aa35、`feat/network-monitor-20261012` @7560059f6、`feat/network-html-20261012` @1f245a65c、`feat/network-check-v1` @301b2c735 | 在途，未合入，落后 `dev` 2400 多个提交 |
| S7 | 邀请短链接：`feat/org-invite-short-link` @64b5b1e67 | 在途，未合入 |
| S8 | 链接原语：`docs/design/design-send-links-2026-10-14.md`、`docs/design/send-links-impl/` | 已实现一部分；WP2（link 表落 OrgFS）在途 |
| S9 | GUI 任务登记 `docs/design/gui-init-tracks.md` 第 7 节 | 已合入 `intg/gui-init` |
| S10 | daemon request 公共契约：`intg/cli-command-surface` @e0b75e324 | 在途，未合入 `dev`；只有 link.add/drop/list/stats 迁入 |
| S11 | 姚升悦 2026-10-29 的决定：<br>- 对 Q03、Q04、Q05、Q07、Q09、Q10、Q12、Q14、Q17 的答复。<br>- 群聊约定：跨 tab 的组织交互想到就改上去，之后由陈瑞华合并。<br>- 配置发布只做好交互，不区分后端是否已经支持。<br>- 今天这一版的实现范围：全部设计，包括跨 tab 的部分。<br>- 集成人负责的文件按任务登记处理：壳层和开发定位层都不改，需要的改动写成接口提案 | 用户决定 |
| S12 | hyprial-cli-developer 2026-10-29 答复：<br>- 组织配置没有单一负责人，也没有设计与用户旅程文档。<br>- 结构与部门归 emergent-org-structure-refactor 线。<br>- 权限与 ACL 归 #1982（`feat/zenoh-credentials-dev-0552` @a855bc155，0.5.5.2）。<br>- CLI 与数据面归 hyprial-cli-developer（#2006 `intg/move-cmd-complete` @9cc74ec4a）。<br>- 设备记录读到的是本机副本 | 答复，未复核 |

## 1. 背景与目标

组织（org）是 hyprial 组织人、机器、agent 的基础模块（S1）。用户可以同时属于零个或多个工作组织，没有组织是合法状态；加入同一工作组织的人和 agent 之间可以通信；组织中有配置文件描述通信拓扑、权限与结构；组织内的 agent 可以经过 owner 和相关步骤迁移；成员、拓扑、联系可以查看，也有交互统计（S1）。

目标：在 GUI 中完整呈现上述能力的入口、状态和流程，使 GUI 设计可以作为后续页面修改的标准。

非目标：
- 不接后端，不实现后端能力。后端未接通的功能在 GUI 中显示“暂不可用”“状态未确认”等真实状态。
- 不在 GUI 中建立第二份权威：成员、组织文件、通信判定、迁移状态的权威分别在 Casdoor、OrgFS、daemon。
- 不定义后端协议。GUI 需要的数据与动作写成“GUI 需要的能力”，由后端契约负责人定义。

## 2. 业务规则

| 编号 | 规则 | 来源 |
| --- | --- | --- |
| B1 | 一个组织 = 一个 Casdoor group + OrgFS 空间（目录空间、ACL 空间）+ 一个 tailnet。组织名只用小写字母、数字和连字符，最长 57，不能以 `-acl` 结尾；`foo` 与 `group-foo` 指同一组织 | 〔已实现〕`shell/impl/cli/commands/org/commands.py`、`daemon/impl/org/network/service.py` |
| B2 | 用户没有组织是合法状态；Squire 与本地凭据保存在本机受保护状态目录，用户可以同时属于零个或多个工作组织 | OC4（`contract/org-network/membership.md`） |
| B3 | 一个 home 可以同时属于零个或多个工作组织；“当前组织”是 home 级本地设置，CLI 与 GUI 共用，只决定组织类操作的默认目标与新建 agent 的默认归属，不影响通信判定和运行中的 agent | 多组织存储〔已实现〕；当前组织〔在途〕S2 §3.1、S3 |
| B4 | agent 可以属于零个或多个工作组织（`orgs: []` 合法）；新建时未指定组织则归当前组织，没有当前组织时不加入任何工作组织；允许移出最后一个组织 | OC4（`contract/org-network/membership.md`）；现状 agent 记录没有组织字段〔已实现〕`identity/impl/agents/registry/_base.py` |
| B5 | 两方能否通信：工作组织内至少共享一个组织且该组织配置允许；owner 与自己的本地 agent 通信不依赖组织归属，同一 owner 的两个 agent 不因同 owner 互通 | OC4 + 〔在途〕S2 R3–R5；现状由 link 准入与 bypass 决定〔已实现〕 |
| B6 | Casdoor group 是成员的唯一事实源；邀请与移除先写 Casdoor，再落到 OrgFS | 〔已实现〕`daemon/impl/org/network/casdoor/membership.py`；S2 R6 |
| B7 | 组织配置文件描述结构、通信拓扑与权限（S1）。现有承载是 org-context（`meta`、`lines`、`members`、`routing`、`norms`、`residents`），由各节点 owner 采信，候选稿发到 OrgFS `context` 空间。部门没有承载；姚升悦要求部门等配置作为 OrgFS 文件，由组织所有者设置，可从模板导入（S11） | org-context〔已实现〕`daemon/impl/org/document.py`；部门〔缺口〕；S4、S11、S12 |
| B8 | 通信权限由 link 原语控制：agent 只能向持有 link 的对端发送；link 文件在各组织空间的 `link/` 下；当前 bypass 默认开、窗口为观察模式 | 〔已实现〕`daemon/impl/links/`；WP2〔在途〕S8 |
| B9 | 组织内的 agent 可以迁移到组织内的机器。现状：`agent teleport` 经 daemon 网络跨机迁移；规划：迁移单 + 双方握手，必须同组织同一获准空间，迁移后 owner 不变 | 〔已实现〕`shell/impl/cli/commands/agent/teleport/`；〔在途〕S5 |
| B10 | 离线设备重试有预算，用完进入休眠，有明确需求时唤醒 | 〔在途〕S2 R7 |
| B11 | 组织文件对整个组织网络可读，不应存放秘密；移除成员后其后续写入被拒，已合并内容保留；彻底清除只有 owner 能做，需两步 | 〔已实现〕OrgFS（`hyprial fs`）；设计见 `design/org-shared-folder-2026-09-26` |

S2 第 8 节有 6 项待决，Allen 均未确认（S3）。下文用“待决 1–6”指这 6 项；S2 第 5 节另有 D1–D6，指要删除的 owner 机制清单，两者不是一回事。

| 编号 | 待决内容（推荐值） |
| --- | --- |
| 待决 1 | 已随 OC4 失效：组织仅剩工作组织，邀请只能由所有者发出（第 3 节、F04） |
| 待决 2 | 发给 `user:` 的消息也要求共同组织 |
| 待决 3 | agent 加入工作组织需要该组织管理员或所有者同意，主人发起申请 |
| 待决 4 | 0.5.5.3 只观察，0.5.5.4 再强制 |
| 待决 5 | 重试预算：连续失败 8 次（约 10 分钟）后进入休眠 |
| 待决 6 | 删除组织时同时删除 Casdoor group |

GUI 按推荐值设计，并标“待确认”。

## 3. 角色

| 角色 | 现状 | 来源 |
| --- | --- | --- |
| 所有者（owner） | 组织空间的 owner。只有所有者能邀请成员、移除成员、删除组织、修改空间元数据。GUI 中组织配置（含部门与结构）和改名也由所有者设置（S11） | 〔已实现〕`daemon/impl/orgfs/document/operations/spaces.py`、`daemon/impl/org/network/policy.py`；S11 |
| 成员（member） | 空间的 rw 成员；只能写自己的设备行与绑定行 | 〔已实现〕同上 |
| 管理员（admin） | 只出现在权限词表中，没有代码会分配。本设计只分所有者和成员两级（S11）；S2 推荐由“管理员或所有者”审批，本设计按所有者处理 | 〔已实现〕词表；S11 |
| agent 主人 | agent 的 owner；发起 agent 迁移；规划中发起 agent 加入组织 | 〔已实现〕迁移；〔在途〕S2 |
| 受邀人 | 持有邀请链接，或已在网页上接受邀请的用户 | 〔已实现〕 |
| 设备（机器） | 用户的每台机器；第二台电脑登录后加入组织网络 | 〔已实现〕 |

Casdoor 只记录 `hyprial/<org>` 组的成员关系，不记录角色（〔已实现〕）。

## 4. 功能清单

### 4.1 总表

| 编号 | 功能 | 后端现状 | 在途规划 | GUI 现状 | 结论 |
| --- | --- | --- | --- | --- | --- |
| F01 | 组织列表与当前组织切换 | `org list` 返回 org、spaceId、role、devices、state，以及 leftOrgs；`orgs: []` 合法；没有当前组织 | S2：`org use`、kind、current | 没有组织列表与切换器 | 在途 |
| F02 | 新建组织 | `org create` 已实现，可同名重试；`org repair` 预览与修复未完成的创建 | — | J5 内联表单（未接通） | 已实现 |
| F03 | 组织概览（人数、agent 数、机器数、我的角色） | 由 `org list`、`org network` 组合得到 | S2 第 7 节的实体与事件 | 没有独立概览 | 部分 |
| F04 | 邀请成员（生成邀请链接） | `org invite --user` 返回 link、deepLink、command、expiresAt；1 小时过期；只有所有者能邀请 | S7 短链接；S2 改名为 `org member invite` | J5 内联表单 + M-12 邀请状态（未接通） | 已实现 |
| F05 | 成员列表（人） | 成员表 = OrgFS 空间成员（rw/ro）；`org network` 返回目录成员与设备 | S2 member 实体（user、role、来源） | J5 成员块（空、有数据） | 部分 |
| F06 | 待处理邀请（领取邀请） | `org pending` 读取网页上已接受的邀请，逐条确认或 `--accept-all`；登录后自动全部接受一次 | S2：登录后逐条确认；邀请状态 pending/accepted/declined/failed | 只有“邀请待领取”状态标签，没有入口 | 已实现（确认方式在途调整） |
| F07 | 用邀请链接加入 | `org join <链接>`：四步（连接邀请人、加入组织、注册设备、连接成员），75 秒超时 | — | J5 加入表单（未接通） | 已实现 |
| F08 | 移除成员 | `org remove --yes`：两段确认（预览 → 确认），带阶段与冲突处理；只能在 authority 节点执行 | S2 改名为 `org member remove` | 没有 | 已实现 |
| F09 | 组织内 agent 列表 | agent 没有组织字段；org-context 的 members/residents 是描述性记录 | S2：`<org>/agents/<agentId>.json`、`agent org ls` | 组织页没有；Agent 详情显示“所属组织” | 在途 |
| F10 | agent 归属：加入、移出组织（含审批） | 未实现 | S2：`agent org add/remove/ls`；审批规则见待决 3，状态未设计 | Agent 表单有“所属组织”下拉（默认“不加入组织”） | 在途 |
| F11 | 在组织下新建 agent | `agent create` 没有 `--org` | S2：`agent create --org`，默认当前组织 | Agent 表单“所属组织”下拉（未接通） | 在途 |
| F12 | 组织内机器：列表、在线状态、第二台电脑加入 | `org network` 的 directory、directoryPending（同步中，不是待审批）；`host list` 给出 online/unknown | S2：online/offline/dormant、`org network retry`；S6 健康探测 | J6 机器列表（在线、离线；未接通） | 部分 |
| F13 | 拓扑：结构与通信 | org-context `routing` 有 kind、to、scope（scope 无语义定义，没有代码读取）；link 列表 `link list` | S6：本机节点子图拓扑；S8 WP2 | J7 拓扑（结构/通信视图、Inspect；未接通） | 部分 |
| F14 | 统计：某 agent 视角下的通信量、任务、时间窗口 | 没有聚合统计；`link list` 有在途 taskIds，`agent top` 有 turnCount 等，`workflow overview --window-days` 按 actor 推断任务线 | S6：`org network inspect --window 1d/7d/30d`、`monitor`、`--html` | 没有 | 在途（任务维度为缺口） |
| F15 | 组织配置：结构与部门、通信权限、规范、版本、改名 | org-context：`org context validate`、import（版本递增）、show、status；候选发到 `context` 空间 | S8 WP2（link 表落 OrgFS）；所有者设置、模板导入、改名（S11，GUI 先设计） | 没有 | 部分；部门、改名后端未支持，GUI 先设计 |
| F16 | OrgFS 内容查看与编辑 | `hyprial org fs`：ls、resolve、read、write、mkdir、mv、rm、history、watch、import、export、member、trash、purge；已有文本用 read 返回的 version 配合 write --expect-version 条件编辑 | OrgFS 修复：重新发布（S4） | 没有 | 已实现（当前空间历史层待修复） |
| F17 | 把 agent 迁移到组织内机器 | `agent teleport <name> --to <nodeId> --yes`：经 daemon 网络迁移，失败时源 agent 原样重启（`TELEPORT_UNRESOLVED` 除外） | S5：迁移单、双方握手、7 项展示、状态机 | 没有 | 已实现；对端除传输层外不做认证；组织条件在途 |
| F18 | 通信被拒原因与修复入口 | link 拒绝码（如 `LINK_UNKNOWN`）；bypass 下按组织成员放行 | S2：`org.admission.refused`（NO_SHARED_ORG、ORG_POLICY_DENIED） | 聊天只有三种不可发送原因 | 在途 |
| F19 | 离开组织、删除组织 | `org leave --yes` 两段确认；`org delete` 两步确认（预览→确认，OC7），只在 tailnet authority 设备上执行 | — | 有：打开时展示预览，输入组织名后确认 | 离开、删除均已实现 |
| F20 | 组织变化的刷新与事件 | 没有面向 GUI 的事件 | S2 第 7 节 6 个事件，经 S10 公共契约暴露，目标 0.5.5.3 | 没有 | 在途 |
| F21 | 组织 ACL 授权（授权 agent 写 ACL 空间） | `org acl grant/revoke/ls`，记录只在所有者节点 | 权限语义归 #1982（S12） | 没有 | 已实现；GUI 不呈现，由相关操作连带处理（S11） |
| F22 | 组织工作文档 | `org work ls`、`org work show <相对文件路径>` 仅为 `work/` 下普通 `orgfs.ls/read` 快捷入口；schema 只作只读投影，状态与 owner 字段不构成审批证明 | 编辑、冲突和恢复统一使用 F16 的 OrgFS 接口，无专用任务写入或本地审批账本验证 | 复用组织文件能力，不另设任务业务页面 | 后端只读快捷入口已实现；GUI 接线按组织文件进度 |

GUI 网关前提：`dev` 上的 GUI 适配层只有 capabilities.get、sessions.*、targets.list，没有任何组织、OrgFS、agent 归属、迁移、统计方法；公共契约 S10 只迁入了 link 方法。因此 F01–F22 在 GUI 中目前全部是“未接通”。

### 4.2 关键功能说明

**F01 组织列表与当前组织**
- 组织状态取值：active、creating、partial、deleted、legacy-active；另有已离开（leftOrgs）。
- 组织均为工作组织；空列表（`orgs: []`）合法且正常，界面显示空状态与引导动作（交互设计 5.2）。
- 当前组织切换会影响之后新建 agent 的默认归属（S3）。

**F02 新建组织**
- 输入：组织名（规则见 B1）。
- 结果：spaceId、aclSpaceId、是否已存在、Casdoor group、设备是否已发布。设备发布失败时整体失败，可同名重试。
- 创建未完成（creating、partial）可由所有者修复；修复默认只预览。

**F04 邀请成员**
- 邀请链接有四种写法：`https://hyprial.ai/join#v2.<…>`（主形式）、`hyprial://join#v2.<…>`、`hyprial-invite:v2:<…>`、`v2.<…>`；旧版 v1 报 `INVITE_VERSION_UNSUPPORTED`，需要重新签发。
- 邀请含连接凭据，只能私发；1 小时过期。
- 短链接（`hyprial.ai/j/<id>`）在途：生成失败时短链接为空，长链接照常可用（S7）。

**F06 待处理邀请**
- 网页上接受邀请后，记录写入 Casdoor 用户属性；设备执行拾取时逐条确认，跳过本机已加入和主动离开的组织。
- 结果分为 joined、failed、skipped、pending，附 warnings。
- 规划：登录时逐条确认，不再静默全部接受（S2 第 5 节 D6）；邀请状态 pending/accepted/declined/failed（S2 第 7 节）。

**F07 用邀请链接加入**
- 四步：连接邀请人、加入组织、注册设备、连接成员；超时 75 秒；加入会清除该组织的“已离开”记录。
- 错误：`INVITE_MALFORMED`、`INVITE_DIGEST_MISMATCH`、`INVITE_EXPIRED`、`INVITE_CONSUMED`、`INVITE_INVALID`、`MEMBER_REVOKED`、`AUTHORITY_PIN_MISMATCH`。

**F08 移除成员 / F19 离开组织**
- 两段确认：第一次取得预览（operationId、受影响设备、tailnet、authority 设备、阶段），第二次带同一 operationId 与设备列表确认。
- 阶段：待确认 → 待 authority 或 OrgFS 处理 → 待 tailnet 处理 → 完成。
- 预览变化报 `REMOVAL_CONFIRMATION_STALE`；版本冲突报 `REVISION_CONFLICT`，需要重新确认；不能复用旧确认。
- 移除只能在 authority 节点执行，否则报 `AUTHORITY_REQUIRED`；不能移除最后一个 authority（`LAST_AUTHORITY_REFUSED`）。

**F12 机器**
- `org network` 中的设备在线状态固定为 unknown；`host list` 按 mesh presence 给出 online 或 unknown。
- “同步中”指目录内容尚未到达，不是待审批，界面不提供批准按钮（S4、`docs/design/org-device-inbound-admission.md`）。
- 读到的设备记录是本机副本，不一定是空间的当前状态（S12）。

**F13 拓扑 / F15 组织配置**
- org-context 字段：`meta`（version、issued_at、publisher）、`lines`（业务线：负责人、协调人、看板、代码仓库）、`members`（username、display、role、home_node、squire、status、machines）、`routing`（kind、to、scope）、`norms`（autonomy、escalation、etiquette）、`residents`（name、node、role、serves）。
- 采信：各节点 owner 在本地导入，版本必须递增；没有可读的采信记录时只显示正文并给出警告。
- 设置权限：组织所有者设置配置，可以先从模板或配置文件导入再修改，不必从头编写（S11）。部门与结构的 schema 归 emergent-org-structure-refactor 线（S12）；schema 出来之前，GUI 先按交互设计做。
- 通信权限要分两层呈现：`routing` 是“允许向谁发”的静态约定；link 是“持有才能发”的动态凭证。界面应回答“这一对 agent 之间有没有 link”（S4）。
- link 表落 OrgFS（WP2）依赖 OrgFS 可用。S4 在 2026-10-29 观察到 `group-internal` 空间的历史层为空，这只是该空间的情况，界面按交互设计 5.8 对所选空间判定。WP2 可用前，通信层只显示本机 Agent 的 link，并注明范围。

**F14 统计**
- 在途设计（S6）：只覆盖本机节点子图（每条边至少一端在本机）；字段为节点（类型、是否本机、已注册、发出、收到）、边（发送方→接收方、消息数、渠道）、按 UTC 小时分桶的时间线、总计、覆盖度（恒为未知）；计数口径为去重后的消息 × 实际收件人；数据来自本地日志与 inbox，不含正文。
- 单个 agent 视角由前端过滤边得到；没有按任务的维度（Q08）。
- 投影只是 daemon 内部函数，没有对外方法；要在新 `dev` 上重做。

**F16 OrgFS**
- 冲突：带基础版本做三方合并；带期望版本做严格校验，失败报 stale-write；只读成员写入报 not-a-member。
- 限制：单次读取不超过 2 MiB；导入、导出不超过 64 MiB。
- 历史：不提供（owner 2026-11-25 决定，见 `docs/design/orgfs-minimal-plan-2026-11-24.md` Q5）；保留回收站与恢复。
- 当前状态（S4，2026-10-29 实测 `group-internal`）：内容完整，但历史层为空（frontier 全零）；修复方式为重新发布，由该空间 owner 执行。这是该空间的问题，不代表所有空间；GUI 按所选空间的反馈显示状态。

**F17 迁移**
- 现状：`agent teleport` 要求 `--yes`（交互终端可确认；`--dry-run` 只预检）；本机停止并导出、目标恢复同一会话、成功后本机移除；失败时源 agent 重启，`TELEPORT_UNRESOLVED` 时源 agent 刻意保持停止，按 `data.manualCheck` 人工核对。
- 规划（S5）：
  - 迁移单绑定不可变计划；要改条件只能取消后重建，双方重新同意。
  - 源端有权操作者执行检查、请求、开始；目标端有权接收者发出邀请；参与方可查看、取消；有恢复权限者可重试。
  - 同意前展示 7 项：主人证明、旧/新 URI 与机器变化、harness 版本、凭据处置、旧 URI 去向、回滚条件、预计停机；缺项显示未知、原因与责任方。
  - 意向状态：pending-source、pending-target、pending-verification、plan-conflict、ready；终态：withdrawn、rejected、expired、cancelled、complete。
  - 执行阶段：admitting → quiesced/transferring → prepared → execution-granted → published → resumed → network-ready/source-retired → complete；异常：cancel-requested、blocked-target-proof、landed-unverified、agentReport missing。
  - 回滚：execution-granted 之前可恢复源；resumed 之后不能回退，迁回要另建迁移单。
  - 进度：字节数、文件数、速率、最近进展、重试原因；不编造剩余时间；ok 不等于完成。
  - 组织条件：同组织、同一获准空间；跨组织拒绝；迁移后 owner 不变。
  - 源端和目标端核对的是同一份计划（编号、版本、7 项）。目标方同意后，计划仍可能处于待验证或计划冲突；能否开始以后端返回 ready 为准，前端不自行推进状态。

## 5. GUI 需要展示的实体

| 实体 | 字段 | 来源 |
| --- | --- | --- |
| 组织 | 名称、kind（work）、我的角色、状态、是否当前组织、设备数 | `org list`〔已实现〕；kind、current〔在途〕S2 |
| 成员（人） | 账号、显示名、角色、来源（Casdoor）、同步状态 | OrgFS members〔已实现〕；S2 member 实体〔在途〕 |
| agent 归属 | agent → 组织列表、加入时间、加入人 | 〔在途〕S2 §3.4 |
| 设备 | 主机名、owner、是否本机、在线状态、首次发布时间、同步中 | `org network`、`host list`〔已实现〕；休眠〔在途〕S2 |
| 邀请 | 组织、邀请人、时间、过期时间、状态、失败原因 | 〔已实现〕；状态取值〔在途〕S2 |
| 组织配置 | org-context 六段、版本、发布者、采信状态、候选；部门与汇报关系（字段待对齐）；模板（名称、说明、包含内容） | org-context〔已实现〕；部门、模板见 S11，schema 归属见 S12 |
| link | 对端、是否持有、在途数、最近使用时间、bypass 状态 | `link list`〔已实现〕 |
| OrgFS 节点 | 路径、类型、版本、最后修改人与时间、历史 | 〔已实现〕 |
| 迁移单 | moveId、源与目标、7 项、意向状态、执行阶段、进度、证据 | 〔在途〕S5 |
| 通信统计 | 节点、边、时间线、总计、覆盖度 | 〔在途〕S6 |
| 准入事件 | 发送方、接收方、共同组织、原因 | 〔在途〕S2 |

## 6. 状态取值

| 对象 | 取值 | 来源 |
| --- | --- | --- |
| 组织 | active、creating、partial、deleted、legacy-active；已离开 | 〔已实现〕 |
| 组织（规划） | active、deleted、left、sync-problem | 〔在途〕S2 |
| 邀请 | pending、accepted、declined、failed（含原因码） | 〔在途〕S2；拾取结果 joined/failed/skipped/pending〔已实现〕 |
| 加入步骤 | 连接邀请人、加入组织、注册设备、连接成员 | 〔已实现〕 |
| 移除、离开 | 待确认、待 authority 或 OrgFS、待 tailnet、完成；版本冲突需重新确认 | 〔已实现〕 |
| 设备 | online、unknown；同步中 | 〔已实现〕 |
| 设备（规划） | online、offline、dormant（附失败次数、最后尝试时间） | 〔在途〕S2 |
| agent 归属审批 | 申请中、已批准、已拒绝 | 〔待确认〕Q06，未设计 |
| 准入 | 观察期“将被拒绝”、强制期“已拒绝”；原因 NO_SHARED_ORG、ORG_POLICY_DENIED | 〔在途〕S2 |
| 组织配置 | 已采信版本、待采信候选；校验错误、警告 | 〔已实现〕 |
| 迁移 | 见 4.2 F17 | 〔已实现〕+〔在途〕S5 |

## 7. GUI 需要处理的主要错误

| 错误码 | 场景 | 界面处理 |
| --- | --- | --- |
| `INVITE_EXPIRED`、`INVITE_CONSUMED`、`INVITE_INVALID`、`INVITE_MALFORMED`、`INVITE_DIGEST_MISMATCH` | 加入 | 说明原因，提示“请邀请人重新发送” |
| `INVITE_VERSION_UNSUPPORTED` | 加入 | 说明需要重新签发新版邀请 |
| `MEMBER_REVOKED` | 加入 | 说明已被移出该组织 |
| `DEVICE_NOT_READY` | 拾取邀请 | 说明本机设备尚未就绪，提示完成登录 |
| `ORG_ACTION_NOT_AUTHORIZED` | 邀请、移除、删除 | 说明需要所有者权限 |
| `REMOVAL_CONFIRMATION_STALE`、`REVISION_CONFLICT` | 移除、离开 | 重新展示最新预览，要求再次确认 |
| `AUTHORITY_REQUIRED` | 移除 | 说明需要在 authority 设备上执行 |
| `LAST_AUTHORITY_REFUSED` | 移除、离开 | 说明当前不能执行 |
| `AUTHORITY_REQUIRED`、`OPERATION_CONFLICT` | 删除 | 说明只能在管理设备上删除；有子组织时先删子组织 |
| `CASDOOR_GROUP_UNAVAILABLE`、`CASDOOR_ORG_QUOTA_EXCEEDED`、`CASDOOR_GROUP_WRITE_FAILED` | 创建、邀请 | 说明账号服务暂不可用或额度已满 |
| `TRANSFER_OWNER_MISMATCH`、`TRANSFER_CONFIRMATION_REQUIRED` | 迁移 | 说明只能迁移自己的 agent；迁移需要明确确认 |
| `ORG_CONTEXT_NOT_AUTHORIZED` | agent 读取组织配置 | 说明该 agent 没有组织配置读取权限 |
| stale-write、not-a-member | OrgFS 写入 | 展示冲突差异或只读原因 |

## 8. 约束

1. 后端未接通时只显示“未连接”“状态未确认”“暂不可用”；依赖项部分可用时显示“依赖项状态未确认”（S4 建议）。不显示成功。
2. 状态遵循 UI 规范的五种：loading、ready、empty、error、unknown；“已保存”“已连接”“已完成”不混用。
3. 邀请链接、短链接、命令都是凭据：输入用密码框，提交后清空，不写入本地存储、URL 或日志，展示时提示“只私发给被邀请人”。
4. 浏览器不能自报身份；权限由后端返回，GUI 只据此决定按钮是否显示。
5. 写操作明确确认，不因网络超时自动重试；需要两段确认的操作必须展示预览。
6. 375、760、1440 宽度无根横向溢出；键盘可达；状态不只靠颜色表达。
7. 窄屏提供全部组织功能（S11）；现有窄屏网页已有组织入口。

## 9. 依赖与在途工作

| 工作 | 负责人 | 分支或文档 | 状态 | 对 GUI 的影响 |
| --- | --- | --- | --- | --- |
| 组织成员模型 | emergent-org-structure-refactor（研发负责人） | S2 | WIP，6 项待决未确认 | 当前组织、agent 归属、事件 |
| agent 迁移 R4.2 | codex-sw | S5 | 未批准实施，目标 0.5.5.2 | 迁移向导与状态机 |
| 网络统计 | codex-network | S6 | 未合入，需要在新 `dev` 上重做 | 统计页、拓扑通信层、健康探测 |
| 邀请短链接 | — | S7 | 未合入 | 邀请结果中的短链接 |
| link 表落 OrgFS（WP2） | link 线（问题报告作者 emergent-org-structure-refactor） | S8 | 在途 | 通信权限展示 |
| OrgFS 历史层修复 | 该空间 owner | S4 | 修复方式已定：重新发布 | 文件页、配置页、通信权限 |
| daemon request 公共契约 | A 线 | S10 | 未合入 `dev` | 全部 GUI 接线的前提 |
| 组织配置 schema：部门、结构、权限落在哪些 OrgFS 路径与字段 | emergent-org-structure-refactor 线（S12） | 没有设计文档，只有问题报告 `org-structure-constrain-problem-report-2026-10-14.md` | 未立 | 设置页“结构与部门”、模板 |
| 权限语义与 ACL | #1982 负责人（S12） | `feat/zenoh-credentials-dev-0552` @a855bc155 | 在途，0.5.5.2 | 通信权限展示；ACL 不在 GUI 呈现 |
| CLI 与数据面契约、迁移命令 | hyprial-cli-developer（S12） | #2006 `intg/move-cmd-complete` @9cc74ec4a | 在途 | 迁移、设备记录 |

## 10. 已决与待确认问题

### 10.1 已决

| 编号 | 问题 | 结论 | 来源 |
| --- | --- | --- | --- |
| Q03 | 组织切换器放在哪里；壳层改动由谁做 | 目标位置是左栏（交互设计 4.1）。本版不改壳层，先放在组织 tab 页头，挂到左栏写成接口提案（交互设计 13.2）。组织相关交互不论在哪个 tab，都由 dev-04 按交互设计修改，集成人负责的文件除外 | S11 |
| Q04 | 部门等组织配置的设计负责人与 schema | GUI 侧：由组织所有者设置，可以从模板导入，不必从头编写。后端 schema 归 emergent-org-structure-refactor 线 | S11、S12 |
| Q05 | 谁可以编辑并发布组织配置 | 组织所有者。采信仍由各节点 owner 在本节点操作 | S11 |
| Q07 | 组织改名 | 作为基础设置，先在 GUI 中设计，后端对齐时补上，不单独立项 | S11 |
| Q09 | 组织 tab 的“文件”页由哪条开发线实现 | 不做任务分配，由 dev-04 按交互设计修改 | S11 |
| Q10 | Agent 详情的“迁移”入口由哪条开发线实现 | 同 Q09 | S11 |
| Q12 | 移动端是否提供组织功能 | 提供。现有窄屏网页已有组织入口，按交互设计第 12 节适配 | S11；第一轮审查核对 |
| Q13 | 开发场景 M-12 的“邀请创建成功” | 改为交互设计 11.1 的表述 | 本文 |
| Q14 | 管理员角色何时启用 | 本设计只分所有者和成员两级 | S11 |
| Q15 | 是否需要“发出的邀请”列表 | 没有实现，也没有在途规划，不设计 | 本文 |
| Q17 | GUI 是否呈现组织 ACL 授权 | 不呈现，由后端在相关操作中连带处理 | S11 |

### 10.2 待确认

| 编号 | 问题 | 现有线索 | 由谁确认 |
| --- | --- | --- | --- |
| Q02 | S2 第 8 节的 6 项待决 | Allen 均未确认（S3）；GUI 按推荐值设计 | Allen |
| Q06 | agent 加入工作组织的审批状态与事件 | S3：未设计。初步想法是在 agent 详情中发起，在组织成员页中审批 | #2078 负责人 |
| Q11 | 第二台电脑如何加入已是成员的组织 | 邀请 1 小时过期；拾取读取网页上已接受的邀请 | #2078 负责人 |
| Q18 | 迁移目标端先发邀请（pending-source）时的 GUI 入口 | S5 允许目标端先发出邀请；交互设计目前只设计了源端发起的路径 | R4.2 负责人 codex-sw |

Q08（按任务统计）和 Q16（跨节点统计）属于缺口，见第 11 节。

## 11. 缺口清单

用户要求中，以下部分既未实现，也没有找到在途规划。“GUI 处理”列说明交互设计中的做法。

| 缺口 | 用户要求（S1） | 现状 | GUI 处理 |
| --- | --- | --- | --- |
| 部门 | “组织的 context 中的功能进行配置，比如部门” | 没有字段承载（S4）；schema 归 emergent-org-structure-refactor 线（S12） | 先行设计：所有者在“设置 → 结构与部门”中设置，可以从模板导入（S11）。未接通时显示“暂不可用” |
| 汇报关系等结构 | “结构” | 只有业务线与常驻角色，没有汇报关系（S4） | 与部门一起设计 |
| 改名 | “也可以改名之类的基础配置” | 没有改名字段与命令 | 先行设计：基础信息中的“改名”（S11）。数据源没有实现时不渲染 |
| 按任务统计通信（Q08） | “围绕什么任务……做了多少通讯” | 统计设计没有任务维度 | 不渲染 |
| 跨节点统计（Q16） | 组织整体统计 | 只覆盖本机子图 | 不渲染，并始终显示范围说明 |
| 迁移到他人机器 | “转移自己的 agent 到组织内的机器上” | 现在只支持同 owner；跨 owner 托管的语义未定（S5） | 他人的机器不列出 |

## 12. 验收

本文作为 GUI 设计输入，验收标准如下：

1. 第 4 节的功能中，除 F21（不在 GUI 呈现）和 F22（归任务 tab）外，每个功能在交互设计中都写明：
   - 入口和所在 tab。
   - 交互深度。
   - 状态。
   - 未接通时的表现。
   子操作是否呈现，在交互设计各区块中写明。
2. 第 11 节的缺口按“GUI 处理”列执行：
   - 先行设计的项，未接通时显示“暂不可用”。
   - 其余标为“暂不可用（未立项）”，不设计成可用功能。
3. 第 10 节的已决问题在交互设计中落实；每个待确认问题都有明确的确认人。
4. 文中每条后端现状，都能按给出的路径或分支核对。
