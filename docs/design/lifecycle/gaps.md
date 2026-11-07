# 接口缺口汇总

对照生命周期设计与 daemon_request / CLI 后，`有差异`、`仅 CLI`、`缺口` 三类逐条记在这里。
R&D 负责人据此拆成 0.5.5.4 的工作项。说明见 [README.md](README.md)。

## 首轮对照基线（2026-10-31）

- 产品源码与 CLI/OpenAPI：`dev@4d676ec37`。同时核对 `release/v0.5.5.3@43caf606d`；两者本轮涉及的旧组织网络实现相同，声明目录都只有 `link.*`。
- 在途补充：`PR #2217@06a2aee3b`（组织核心）、`PR #2221@92cede5c8`（登录邀请）、`PR #2210@49d5f8dfe`（Agent/provider 契约）。本轮均未进入上述 dev/release 基线，不把 PR 能力写成已发布能力。
- 每行依据的 `ref + 文件:行号` 用 `git show <sha>:<文件>` 核对；不是本设计分支同路径的行号。旧 inventory 基线 `4a5177` 仅作导航，最终以当前 router、handler、MethodSpec 和命令注册为准。
- “已有”须同时有实现和声明契约；仅旧 IPC 而无 MethodSpec 记“有差异”，不是“没有实现”。`HTTP 未开放` 表示声明明确为 false；声明 true 也不证明 GUI、HTTP 服务或部署已接通。dev 的声明范围见 `src/hyprial/daemon/impl/request_catalog/registry.py:241-244`、`contract/daemon-request/openapi.json:506`。
- a/b 行保留作者原步骤文字，按接口职责拆分。预览归读侧，但生成操作记录不保证无副作用；PR #2217 的 `org.list` 也可能清除失效 current，MethodSpec 标 mutates=true。客户端草稿、复制、渠道选择列为“缺口”仅表示没有对应 daemon 接口，不主张为纯界面动作增设 RPC。
- 下表逐项对应全部“有差异 / 仅 CLI / 缺口”；建议均待评审，不是产品决定。共享根因可合并立项，但不能漏掉受影响旅程。
- 按补充复核意见，把契约缺失与行为/字段差异分开：依据列统一标记“有差异：无契约（MethodSpec 未声明）”，现状列仍保持四选一。补契约不等于修复行为；同时存在的两类工作须分别验收。

## A. 补契约组：旧 IPC 无 MethodSpec（35 行）

本组各行的契约原因均为 **有差异：无契约（MethodSpec 未声明）**。
“缺契约的方法”仅列本轮检查的在途 PR 仍未覆盖的方法；混合行中已由 PR 声明的另一方法见对象文档。
可按方法去重拆成“补契约”工作项；行为/字段问题保留在独立列，不随声明落地自动关闭。

| 对象 | 旅程步骤 | 需要的能力（读/写） | 现状 | 缺契约的方法 | 行为/字段差异或边界 | 建议 | 工作项 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Agent | agent.md / LC-AGENT-01 / 1 | 写 | 有差异 | agent.create | 无职责字段/必填约束 | 先确认职责存储与校验，再补 create MethodSpec | 待立项 |
| Agent | agent.md / LC-AGENT-01 / 3 | 写 | 有差异 | agent.destroy / agent.destroy.preview | destroy 不可逆，preview 未绑定 operationId | 明确确认绑定与恢复边界，补删除契约 | 待立项 |
| Agent | agent.md / LC-AGENT-02 / 2 | 写 | 有差异 | agent.secret.grant | 批量防覆盖在 CLI | 补 grant 契约，评审是否下沉批量编排 | 待立项 |
| Agent | agent.md / LC-AGENT-02 / 3.a | 写 | 有差异 | lifecycle.start | 启动已有 IPC，运行态须另查 | 补启动参数、错误、重试边界契约 | 待立项 |
| Agent | agent.md / LC-AGENT-02 / 3.b | 读 | 有差异 | ps | list 有运行投影，但 model 来自 Agent 记录 | 补 ps 契约；区分配置模型与实测运行模型，再定义展示来源 | 待立项 |
| Agent | agent.md / LC-AGENT-03 / 2.a | 读（预检） | 有差异 | hosts / transfer.plan / transfer.precheck | 机器读取与 SSH 冷迁移预检，不是 GUI 目标选择 | 补读取/预检契约；不把 home migrate 当跨机迁移 | 待立项 |
| Agent | agent.md / LC-AGENT-03 / 2.b | 写 | 有差异 | transfer.quiesce / transfer.receive / transfer.resume / transfer.complete 等 | 冷迁移由 CLI/SSH 组合多个 IPC | 补各方法契约；评审单一确认操作及进度/回滚查询 | 待立项 |
| 对话会话 | conversation.md / LC-CONV-01 / 1.a | 读 | 有差异 | message.history | 历史仅按已有 conversationId 读取，无会话列表 | 为历史读声明契约，并与会话目录职责分开 | 待立项 |
| 对话会话 | conversation.md / LC-CONV-01 / 2.a | 写 | 有差异 | message.send | send 有幂等底层但 CLI 不带 idempotencyKey；受理不代表回复 | 可信宿主固定原请求 ID，补发送契约与重试规则 | 待立项 |
| 对话会话 | conversation.md / LC-CONV-01 / 2.b | 读 | 有差异 | message.status / message.history | 投递状态与回复历史分开；无首次设置完成方法 | 补读取契约；以真实回复为完成证据，保留投递/回复区别 | 待立项 |
| 对话会话 | conversation.md / LC-CONV-02 / 1 | 读 | 有差异 | hosts / targets | 不绑定会话目标 | 补目标读取契约；会话绑定另行评审 | 待立项 |
| 对话会话 | conversation.md / LC-CONV-02 / 2.b | 读 | 有差异 | message.history | 已有参与者校验与游标；客户端仍须按 messageId 去重 | 声明 tail/older/after、游标和去重边界 | 待立项 |
| 对话会话 | conversation.md / LC-CONV-02 / 3.a | 写 | 有差异 | message.send | CLI 未暴露幂等键 | 补发送契约；明确可信接入层保存的原请求键，禁止超时换键重发 | 待立项 |
| 对话会话 | conversation.md / LC-CONV-02 / 3.b | 读 | 有差异 | message.status / message.history | status 按 messageId，响应丢失时无 operationId 结果查询 | 补读取契约与原请求核对路径；未知保持未知 | 待立项 |
| 设备 | device.md / LC-DEVICE-02 / 3 | 读 | 有差异 | org.network / hosts | directory/hosts 字段及 presence 语义不同 | 补读取契约，确认系统/加入时间来源；unknown 不映射为离线 | 待立项 |
| 成员邀请 | member-invite.md / LC-INVITE-01 / 1 | 写 | 有差异 | org.invite | invite 生成时已经写 Casdoor/OrgFS 成员 | 补邀请契约；产品复核成员生效点，链接与设备加入分开展示 | 待立项 |
| 成员关系 | member.md / LC-MEMBER-01 / 2 | 读 | 有差异 | orgfs.members | OrgFS 投影，不是实时 Casdoor 确认或订阅 | 补成员事实来源/同步状态与契约 | 待立项 |
| 成员关系 | member.md / LC-MEMBER-02 / 1 | 读（预览） | 有差异 | org.remove | 同方法用 confirmed=false 预览，不是独立 preview | 声明预览结果、阶段及副作用 | 待立项 |
| 成员关系 | member.md / LC-MEMBER-02 / 2 | 写 | 有差异 | org.remove | operationId 确认参数已有，未发现额外偏差 | 固化确认、版本冲突和重复调用规则 | 待立项 |
| 成员关系 | member.md / LC-MEMBER-02 / 3 | 写（内部编排） | 有差异 | org.remove | phase 不必为 complete，无独立操作状态查询 | 补阶段契约，定义按 operationId 观察的恢复路径 | 待立项 |
| 成员关系 | member.md / LC-MEMBER-03 / 1 | 读（预览） | 有差异 | org.leave | 同方法预览，非独立只读方法 | 声明预览及授权错误，不从列表推断可离开 | 待立项 |
| 成员关系 | member.md / LC-MEMBER-03 / 2 | 写 | 有差异 | org.leave | 只在 phase=complete 写 left | 声明阶段与完成结果，未知先核对 | 待立项 |
| 消息连接 | message-connection.md / LC-MSGCONN-01 / 1 | 读 | 有差异 | adapter.list | Agent 列表契约在途；选择不是 pin | 补 adapter 候选读取契约；界面选择不得提前 pin | 待立项 |
| 消息连接 | message-connection.md / LC-MSGCONN-01 / 3 | 读 | 有差异 | adapter.pins | pins 可读但无预览版本/确认令牌 | 补读取契约，固化旧绑定读取和确认信息；不要发明 adapter pins 命令 | 待立项 |
| 消息连接 | message-connection.md / LC-MSGCONN-01 / 4 | 写 | 有差异 | adapter.pin | 无预期旧绑定 CAS，UNIQUE 冲突不是 CAS | 补 pin 契约；另补 expected-old/版本比较与冲突回读规则 | 待立项 |
| 消息连接 | message-connection.md / LC-MSGCONN-01 / 5.b | 读 | 有差异 | adapter.status / adapter.pins | 两项分开读取，无三事实一致快照 | 补读取契约；定义组合读取与未知状态规则 | 待立项 |
| 模型凭据 | model-credential.md / LC-MODEL-01 / 1 | 写 | 有差异 | agent.secret.provider-write | stdin 写入已有，固定 Pi/DeepSeek 和防重不在方法内 | 可信宿主只开放固定保存动作，补契约 | 待立项 |
| 模型凭据 | model-credential.md / LC-MODEL-01 / 2 | 写 | 有差异 | agent.secret.provider-write | 仅保存回执，不验证模型可用；无 Squire 导航语义 | 声明非秘密回执，不把保存当模型验证 | 待立项 |
| 模型凭据 | model-credential.md / LC-MODEL-01 / 3.b | 写 | 有差异 | agent.secret.provider-write | 原子写覆盖整条 entry；保存记录/授权映射待确认 | 确认替换语义与旧授权关系，声明失败保持规则 | 待立项 |
| 模型凭据 | model-credential.md / LC-MODEL-02 / 2 | 写 | 有差异 | agent.secret.grant / agent.secret.revoke | grant/revoke 已有且不回显值；不能用原生 provider logout 替代 | 补授权写契约；与原生凭据文件操作分开 | 待立项 |
| 组织 | org.md / LC-ORG-02 / 1 | 写 | 有差异 | org.create | create 串行/可恢复；表单防重属客户端 | 补 create 契约并定义重复提交行为 | 待立项 |
| 组织 | org.md / LC-ORG-02 / 2 | 写 | 有差异 | org.create / org.repair | 设备发布失败仍可 active 且 devicePublished=false | 产品复核严格成功条件，补结果与修复契约 | 待立项 |
| 组织 | org.md / LC-ORG-03 / 2.a | 读（预览） | 有差异 | org.leave | 预览复用同方法 | 补预览结果和副作用说明 | 待立项 |
| 组织 | org.md / LC-ORG-03 / 2.b | 写 | 有差异 | org.leave | 仅 phase=complete 写 left | 补确认/阶段/结果查询契约 | 待立项 |
| 组织 | org.md / LC-ORG-03 / 3 | 读 | 有差异 | org.delete | 报 LAST_AUTHORITY_REFUSED 的写入口，无只读能力查询 | 补拒绝契约；评审能力读取方式，不探测性调用删除 | 待立项 |

## B. 行为/字段或接口覆盖差异组（11 行）

本组不重复建立 A 组的旧 IPC 补契约任务：涉及的公开方法已在上述未合 PR 声明，或所需观察能力根本没有独立方法。
PR 未合前 dev 仍无相应 MethodSpec；这些行不能据此视为 dev 已有契约。PR 合并后仍须逐项评审本组差异。

| 对象 | 旅程步骤 | 需要的能力（读/写） | 现状 | 差异或缺口 | 建议 | 工作项 |
| --- | --- | --- | --- | --- | --- | --- |
| Agent | agent.md / LC-AGENT-02 / 1 | 读 | 有差异 | #2210 的 secret.list 仅活动 grants，无完整授权可用性状态 | 分开授权记录、撤销历史和解封结果，不合成假状态 | 待立项 |
| 设备 | device.md / LC-DEVICE-01 / 2.b | 读 | 有差异 | 无统一设备就绪查询，现行错误含 DEVICE_RECORD_MISSING | 对齐就绪证据及错误映射，不硬编码 DEVICE_NOT_READY | 待立项 |
| 设备 | device.md / LC-DEVICE-02 / 1 | 写 | 有差异 | 直接 join 可跳过发布，#2221 邀请拾取才回读完整性 | 统一或明确区分加入完成与设备准备结果 | 待立项 |
| 设备 | device.md / LC-DEVICE-02 / 2 | 写（后台同步） | 有差异 | 后台发布不保证目录已经到达；无独立完成观察方法 | 定义目录同步观察点，不造同步成功记录 | 待立项 |
| 成员邀请 | member-invite.md / LC-INVITE-02 / 1 | 读 | 有差异 | #2221 的 list 需账号快照，账号获取仍在 CLI | 明确可信宿主获取快照边界，不从浏览器自报权威 | 待立项 |
| 成员邀请 | member-invite.md / LC-INVITE-03 / 1 | 写 | 有差异 | #2221 join 契约只在最终响应给 steps，不是实时四步进度 | 定义进度观察或调整展示；清空输入属客户端 | 待立项 |
| 成员邀请 | member-invite.md / LC-INVITE-03 / 2 | 写 | 有差异 | #2221 声明 join 不改变 register-device 可 skipped 且 steps.ok=true | 评审严格完成判定，或显式展示设备未完成 | 待立项 |
| 成员关系 | member.md / LC-MEMBER-01 / 1 | 写 | 有差异 | #2221 邀请拾取与直接 join 的完整性检查不同 | 对齐两条加入路径的结果定义 | 待立项 |
| 模型凭据 | model-credential.md / LC-MODEL-02 / 1 | 读 | 有差异 | #2210 的活动 grants 不含完整撤销/等待/解封状态 | 分开记录与可用性观察，不推断统一状态 | 待立项 |
| 模型凭据 | model-credential.md / LC-MODEL-03 / 1 | 读意图（方法可写） | 有差异 | #2210 provider.default 非纯读，也不是 dev-01/J10 模型服务列表 | 单列服务目录与可用性，标出 default 的副作用 | 待立项 |
| 模型凭据 | model-credential.md / LC-MODEL-03 / 2 | 写 | 有差异 | #2210 provider.login/default 是 harness 凭据，不是模型厂商服务 CRUD/OAuth | 先确认日常模型服务对象，勿混入首次设置 | 待立项 |

## C. 仅 CLI 组（5 行）

| 对象 | 旅程步骤 | 需要的能力（读/写） | 现状 | 差异或缺口 | 建议 | 工作项 |
| --- | --- | --- | --- | --- | --- | --- |
| 设备 | device.md / LC-DEVICE-01 / 1 | 写（引导） | 仅 CLI | 登录及设备准备在 CLI，无 daemon 登录入口 | 明确可信宿主引导边界，不直接开放任意命令 | 待立项 |
| 设备 | device.md / LC-DEVICE-01 / 2.a | 写（引导） | 仅 CLI | ensure_device_key 在登录流程内 | 定义设备准备固定动作和失败恢复回执 | 待立项 |
| 消息连接 | message-connection.md / LC-MSGCONN-01 / 2.a | 写（授权发起） | 仅 CLI | scope 授权编排在 CLI，审批在飞书 | 定义可信宿主固定授权动作与过期/拒绝结果 | 待立项 |
| 消息连接 | message-connection.md / LC-MSGCONN-01 / 2.b | 读 | 仅 CLI | CLI 直接 list_scopes，无 daemon 契约 | 下沉或封装权限读取；不以授权链接证明成功 | 待立项 |
| 消息连接 | message-connection.md / LC-MSGCONN-01 / 5.a | 读 | 仅 CLI | 权限事实只在 CLI 查询 | 与绑定、在线结果分别确认，禁止合成授权成功 | 待立项 |

## D. 无对应方法组（10 行）

| 对象 | 旅程步骤 | 需要的能力（读/写） | 现状 | 差异或缺口 | 建议 | 工作项 |
| --- | --- | --- | --- | --- | --- | --- |
| Agent | agent.md / LC-AGENT-01 / 2 | 写 | 缺口 | 无详情配置更新方法；config rebind 不是详情编辑 | 明确可编辑字段、并发规则后设计更新接口 | 待立项 |
| Agent | agent.md / LC-AGENT-03 / 1 | 写 | 缺口 | 无 Agent 组织加入/移出及最后组织约束入口 | 等组织审批规则确认后补方法与约束；勿复用 ACL 冒充归属 | 待立项 |
| 对话会话 | conversation.md / LC-CONV-01 / 1.b | 写（打开/绑定） | 缺口 | 无独立会话建立/打开/目标绑定接口 | 先确认会话权威和 ID 归属，避免另建 durable inbox | 待立项 |
| 对话会话 | conversation.md / LC-CONV-02 / 2.a | 写（打开/绑定） | 缺口 | send 的 conversationId 不是可恢复会话目录 | 与本地会话共用同一权威，不另建远端会话模型 | 待立项 |
| 成员邀请 | member-invite.md / LC-INVITE-01 / 2 | 写（客户端） | 缺口 | 无复制/关闭清理接口；邀请命令仅返回链接 | 客户端实现短暂展示与清理，不自动发送凭据 | 待立项 |
| 消息连接 | message-connection.md / LC-MSGCONN-02 / 1 | 写（客户端选择） | 缺口 | 无会话沟通方式接口，pin 不等于渠道切换 | 先定选择的持久化范围；不重复创建连接 | 待立项 |
| 消息连接 | message-connection.md / LC-MSGCONN-02 / 2 | 写（客户端草稿） | 缺口 | 历史读不负责切换及草稿保留 | 保留客户端草稿/目标，共用 daemon 历史事实 | 待立项 |
| 模型凭据 | model-credential.md / LC-MODEL-01 / 3.a | 读 | 缺口 | 无用户模型凭据的非秘密保存记录查询 | 新增最小记录查询；不要复用 Agent 自有 secret list | 待立项 |
| 组织 | org.md / LC-ORG-03 / 1.a | 读（预览） | 缺口 | 无组织改名预览入口 | 明确名称/标识语义后设计影响预览 | 待立项 |
| 组织 | org.md / LC-ORG-03 / 1.b | 写 | 缺口 | 无组织改名写方法 | 明确改名一致性与确认规则后立项 | 待立项 |

## 首轮统计与验证

“已有”列均为明确标注未合 PR 的行，不是 dev 已发布能力。拆分前 55 行，拆分后 67 行；上表 61 行。
有差异 46 行按补契约组 35 行与行为/字段或接口覆盖差异组 11 行归档；分组不改变原现状统计。

| 文件 | 已有（未合 PR） | 有差异 | 仅 CLI | 缺口 | 合计 |
| --- | --- | --- | --- | --- | --- |
| org.md | 3 | 5 | 0 | 2 | 10 |
| member-invite.md | 3 | 4 | 0 | 1 | 8 |
| member.md | 0 | 7 | 0 | 0 | 7 |
| agent.md | 0 | 8 | 0 | 2 | 10 |
| device.md | 0 | 4 | 2 | 0 | 6 |
| model-credential.md | 0 | 7 | 0 | 1 | 8 |
| message-connection.md | 0 | 4 | 3 | 2 | 9 |
| conversation.md | 0 | 7 | 0 | 2 | 9 |
| 合计 | 6 | 46 | 5 | 10 | 67 |

静态验证（2026-10-31）：

- PASS：与 `146dd59d2` 逐行比较 8 份对象文档，除接口列及 a/b 拆分外，原步骤角色/操作/预期结果、状态图和正文均未改变；67 行均有命名 ref 与文件行号，路径与行号范围可解析。
- PASS：按“文件 / 旅程 / 步骤”核对，上表 61 行与全部非“已有”映射一一对应，无遗漏/重复。
- PASS：补充分类后，补契约组 35 行与对象文档中统一无契约标记一一对应；其余三组分别为 11/5/10 行。
- PASS：`git diff --check`。
- PASS：`uv run --with ruff ruff check .`。原命令 `uv run ruff check .` 因环境未安装 ruff 而 BLOCKED；临时叠加工具后通过，未改依赖/锁文件。
- PASS：`uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py`。
- PASS：`uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py`。
- UNKNOWN：产品运行行为、真实 GUI/HTTP/平台验收及全量 pytest 未执行。本任务只有文档映射，静态对照不替代这些验收；未操作生产 daemon、账号、设备或凭据。

## revMAP 首轮退回修正（2026-10-31）

复核依据：`git:f97863bec829116b58aee48e4bab7546eaac61aa:docs/notes/lifecycle-map-review-2026-10-31/review.md`。

- `agent.md / LC-AGENT-01 / 1`：创建命令改为 `hyprial agent create --name <name>`，name 是必填选项，不是位置参数；已补命令注册源码引用。
- `agent.md / LC-AGENT-02 / 3.b`：移除不存在的 ps CLI，保留 `hyprial agent list --json`；daemon `ps` 仍存在，补 router 引用并明确无对应 CLI。
- 两行现状仍为“有差异”，不改变 67/61 行与 A/B/C/D 分组统计。定向回归断言先失败、修正后通过；正文保持、引用范围、分组覆盖与 diff 检查再次通过。独立复审待确认，本修正不替代 reviewer 结论。

Contract-Impact: 仅记录现行实现和在途差异，不改发布字段或契约。
Skill-Impact: 不改变 agent 操作方式，无 skill 变更。
