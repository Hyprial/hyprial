# #1897 独立评审处理与 Ractor 选型

评审来源：[R&D head 评论](https://code.hyprial.com/HyprialOS/harness-bridge/pulls/1897#issuecomment-27506)，审查 head `bede7aa85`。本文件记录采纳、边界调整与仍未证明的部分，不修改评审原文。

## 1. 并行工作线的澄清

评审已经提到 #1891 正在修 Inbox，并非完全不知道算法修复存在；需要更新的是该独立任务的最新实测和它与 Rust 线的关系。

- Inbox 第①项由 subagent 独立实施，当前证据 head `630c54fd019ddacc73b9d8f762eac5b6b14a4121`；正式基线 `e384bba7b`、候选 `fecc25c85` 均包含已发布 v0.5.5 和 #1889。
- 同一 35 项测试在基线为 19 failed/16 passed；候选受影响目录 504 passed。25 分钟 N24/M1000，Inbox CPU 降 94.88%、daemon CPU 降 64.69%；另有反序短重复和混合写对照。
- **整体验收仍 FAIL**：idle version +2956 来自 Prune/LinkLedger；队列峰值14→20、p50 0.256→3.816ms 回退；全套失败及生产 overload 未复现、活跃 OrgFS/provider 未测均保留。不能写成所有性能问题已解决。
- [完整 Inbox 报告](https://code.hyprial.com/HyprialOS/harness-bridge/src/commit/630c54fd019ddacc73b9d8f762eac5b6b14a4121/docs/notes/inbox-perf-0551-20261024/post055-performance.md) 为这些数值的依据。

因此接受“算法不能靠换语言修复”，但不把已独立执行的 Inbox 工作或所有额外算法建议，串成 Rust 文档/接口选型必须等待的前置项目。

用户最新确认 PAC tick 与 macOS ps 已在 #1892 留言，编为第 7、8 项，交给独立 agent，排在 inbox-fix 之后。第 7 项要求生产规模负对照和决策一致性；第 8 项建议按同一 PID/启动时间缓存 ps，必须验证 0.5.5 身份标记兼容，不能直接删掉 ps。这项安排已转告 inbox-fix subagent，不要求它接手两项实现。DesiredStateIo diff 仍是未纳入本次实现的独立建议。

## 2. 逐项处理

| 意见 | 处理 |
|---|---|
| 构造点统计不足以证明 handler 覆盖 | 采纳。补充 61 个入口、451 个 handler 分支、157 个 effect 分支的证据清单和首版语义核对；动态路径仍明确待审。v1只限定三类能力，不声称六类覆盖全部 |
| Python finalize/GIL 仍可能主导 | 采纳。真实原型计入 prepare/finalize、转换与完整工作量；不以空 actor 计数器预测 daemon |
| 逐 dict.get 迁移会放大调用 | 采纳。冻结粗粒度 snapshot/CAS/事务，不做每个容器操作一个 FFI/队列请求；对照也要采用相同批量形状 |
| 容量数字、背压、按key顺序、drain缺失 | 采纳。v0.2给出固定原型参数、计数口径、单接纳、顺序和 incomplete drain 规则 |
| 已接纳操作的重启责任 | 采纳并细分 handler generation 与 daemon 重启；区分 volatile 接纳和原数据库已持久 intent/receipt，EffectLane 内存 reserve/ack 不被夸大为跨进程保证 |
| ChangeFeed按资源订阅、只唤醒相关者 | 采纳为硬要求，并补充 lost-wake、cursor落后、重放不重复推进版本 |
| 文档选型不一致 | 已与用户讨论维护成本并选择 Ractor。Tokio 为底座，PyO3 为薄绑定；撤回自建 executor，不叠加 Tower。已补有限语义实验，完整产品接线仍待验收 |
| 移动端未知且漏现有Go桥 | 采纳范围收敛。v1明确node-only；记录Rust↔Go mobilebridge c-archive，未来移动复用单独构建/体积/生命周期验收 |
| 微基准只证明朴素适配慢 | 采纳。保留数据范围；“全部差额因为重入GIL”仍是解释而非profiler已证明归因 |
| 先上线Python池化kernel，再决定Rust | 不作为产品前置。用户已确定Rust路线且目标只保留一套model；可以使用隔离Python行为/批量接口对照，不新开完整Python池化产品线 |
| 每个 actor 独占线程、定时器线程是结构问题 | 采纳共享Ractor/Tokio任务与计时设施，不自建调度器；不从静态构造点直接推导生产737线程分布。本轮源码口径为61个ActorRuntime构造表达式、14个_Reply定义 |
| darwin native start成功就直接跳过ps | 不能机械采纳。用户已在 #1892 第8项明确要求按PID和启动时间缓存，并验证0.5.5身份标记仍匹配；不得减弱PID fence |

评审提供的生产 CPU 分布/“约76%”是其观察与归因，不是本线程重新测得的因果占比。我们遵守禁止访问生产的约束，没有为了验证该数字读取生产状态。现有独立对照也只支持其受控负载范围。

## 3. 供讨论的文件

- [接口 v0.2](actor-rustify-interface-v02-2026-10-24.md)：限定首版接口、容量、顺序、超时/取消、恢复、drain、ChangeFeed 和 conformance cases。
- [框架决定](actor-rustify-runtime-decision-2026-10-24.md)：Ractor选择、与Actix比较、node-only及维护成本边界。
- [消息与覆盖清单](actor-rustify-message-catalog-2026-10-24.md)：实际入口/消息分支证据及仍需手工核对的动态部分。

框架已选 Ractor。语义实验5 passed，三项负对照各为RED：无界入口、stop代替drain、Actix普通异步响应顺序。没有据此宣布真实native热路径、完整迁移、跨平台或生产性能PASS；后续按限定接口验证，不靠删掉责任语义换取速度。

后续已完成[真实Dispatch内存功能P0a标定](../notes/ractor-dispatch-performance-2026-10-24.md)：8项行为测试、三项故障负对照、15组性能配对；记录了CPU/调用p99改善以及最大延迟、线程/RSS回退。用户确认不再追加跑分，转入[正式实施规划](actor-rustify-implementation-plan-2026-10-24.md)，重构重点为分层和减少actor底座维护。SQLite/完整daemon/迁移验收仍UNKNOWN。

## 4. 实施计划评审27578的处理

[最新评审](https://code.hyprial.com/HyprialOS/harness-bridge/pulls/1897#issuecomment-27578)审查head `bcfc5d222`，认可分层方向并要求改写S0。已将S0改为目标资源/应用服务图、旧入口处置表、顺序/配额/故障设计、源码/分发ADR和可执行任务包；S1明确交付共享宿主、同一wheel和目标平台CI证据。见[S0/S1交付清单](actor-rustify-s0-s1-deliverables-2026-10-24.md)。

评审的入口分类是进一步逐项核对的输入，不能直接当作已完成的迁移映射。Python-only SDK不会为了进入Factory而变成Rust业务callback；Space/CRDT边界未闭合时保留BLOCKED。

已核对当前Hatch/force_include/wheel tag、五个Go wheel目标、结构门点名根目录和独立Tauri workspace。早先未决的源码路径、ABI、工具链、runner、公网wheel和workspace/lockfile选择，现由下节ADR给出明确提议；仍待主团队确认与实际构建验证。新增crates目录时同步门禁/AGENTS/快照清单，不能以文档更新宣称S0/S1验收完成。

现已补齐[61项具体分层建议](actor-rustify-entry-layering-proposal-2026-10-24.md)，逐条写出Python职责、native能力、旧包装处理和风险，并从源码提取容量表达式。覆盖校验为61/61，但不是迁移语义PASS。另确认profile/用户库、routine双库、OrgFS journal/blob refs/replica等不能凭名称合并；CRDT及部分跨资源提交路径仍明确BLOCKED。

## 5. 用户要求的完整S2与goal评审

用户随后明确S2必须以“完全剔除Pykka、核心actor引擎Rustify”为标准，故将旧S2–S5重组为S2.0–S2.7；Dispatch不再是S2终点。新增[分发ADR](actor-rustify-build-distribution-adr-2026-10-24.md)、[真实替换设计](actor-rustify-s2-design-2026-10-24.md)、[实施计划及拟议goal](actor-rustify-s2-execution-plan-2026-10-24.md)。

六项分发选择已给出提议定案，真实平台验证仍UNKNOWN。S2覆盖全部业务入口、持久/OrgFS/Process/Transport资源、纯Python应用服务拆分、依赖/旧引擎删除、正式分发和回归；未迁移或BLOCKED入口不得留在终态。按用户要求在PR中提交拟议/goal请主团队分析合理性，**本轮没有设置goal或开始产品实施**。
