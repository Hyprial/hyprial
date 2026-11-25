# S2实施计划、完成判据与拟议goal

状态：**第四轮R1–R4已落实，待研发负责人复核；最终BASE_SHA及基线实测清单仍OPEN。尚未设置/启动goal，也未开始产品替换。** 新S2包含旧S2–S5的全部迁移与删除，不能停在Dispatch切片。设计见[S2真实替换设计](actor-rustify-s2-design-2026-10-24.md)，S1分发前置见[ADR](actor-rustify-build-distribution-adr-2026-10-24.md)。

本轮修订于2026-10-25完成；这里的“规划完成”是设计/执行方案已形成，不是S0/S1/S2的实现和验收已经完成。

## 1. 起点与连续执行含义

- G0：一次性交付S1-A至S2.7全部任务包及反例，不是独立执行goal；任务包在执行开始前经研发负责人评审。
- S1/S2：原G1/G2只保留为PAC内部里程碑，自动连续推进，不在S1结束或S2开始前等待人工规划；测试和独立评审随切片完成。
- 总goal：唯一执行goal，最终验收不在内部里程碑上重复定义。

本轮只提交规划修订，不启动实施。外部等待仅三类：#1892只挡重叠迁移和最终BASE；Allen复核附录A/B/D/H及⑪只挡S2.6；硬E2E凭据只挡S2.7。最终BASE由#1892合入`dev`时的dev head产生，并立即执行`dev→intg`、重生成附录A/B/D/H。其他节点不等待这些外部结果。14项决定及裁定记录见[G0决定与验收附录](actor-rustify-g0-decisions-and-acceptance-2026-10-25.md)。

## 2. S1前置工作包

| ID | 工作与主要范围 | 交付/验收 |
|---|---|---|
| S1-A | crates workspace、pure core/binding拆分；同步AGENTS/结构根/公开快照 | core独立构建不依赖Python；新根真实被门禁扫描，原GUI/Go规则保留 |
| S1-B | kernel公共面、共享Ractor宿主、owned数据/句柄、await/同步边界 | 多资源共享宿主；同资源顺序、配额、失败、关闭与fork/PID边界有测试；无Python业务handler注册 |
| S1-C | 操作责任与结果观察、资源epoch、共享Timer/必要原生执行设施 | ticket仅内存并按epoch管理；线程死亡/deadline/executor关闭为in-doubt；重启由原durable store/receipt reconcile；不自建通用调度器或全局durable ledger |
| S1-D | CI-I先行；Hatch native构建helper、tag/版本校验、editable/worker/package分支 | CI-I机械审计PASS；同一个包的wheel、uv sync和worker镜像均有正确native核心；缺失/错误架构不fallback |
| S1-E | CI-I先行；附录G本goal两硬格与公开快照/sdist | darwin/arm64原生、linux/amd64 manylinux Docker取得import+公共调用、ABI和Go/GUI证据；后续三格只进入dev合入/发布门 |

CI-I是S1-D/E与S2.6共同前置。S1-A可先做目录/打包风险验证，但不能以本机.so成功宣布S1结束。S1正式实现不改变业务公开行为；真实业务迁移在S2进行。S1检查是自动内部里程碑，不等待人工再规划。

## 3. S2子里程碑与依赖

| ID | 设计/实现任务 | 主要源码范围 | 必须通过的验证 | 前置 |
|---|---|---|---|---|
| S2.0 | 当前精确基线的资源别名/值类型/旧失败/删除清单；兼容性决策；最终BASE到S2.6再冻结 | 附录A/B/D五类并集及L1–L5扩展：Timer/Executor/to_thread/create_task、mailbox结构、全部直接与工厂/contract写者 | `tests/kernel/actors/test_legacy_engine_ast_gate.py` AST门可执行；白名单结构键与负对照；clean-tree可重跑；每条有去向 | S1、G0全部任务包；L1–L3/L5在S2.0冻结前完成，L4在S2.2前完成 |
| S2.1 | 首个完整Dispatch StateStore切片；自动内部检查点 | dispatch/runtime/state、相关holds/offers/progress调用方、新core State/操作控制 | 实际数据类型、全操作、同key竞争、批次交错、版本、超时、关闭；旧基线对照 | S1、S2.0；PASS后自动铺开S2.2–S2.5，设计矛盾才升级 |
| S2.2 | 原持久资源、文件/Blob基本能力与ChangeFeed | desired_state/state_db/state_persistence、identity store、squire文件store及core存储driver | 原格式/约束/PRAGMA、同事务前置条件、跨进程busy、真实提交/回滚、幂等、订阅 | S1、S2.1、决定③；L4写者清单已补齐 |
| S2.3 | OrgFS提交边界设计验证并实现 | document/space_state、storage/store/space_authority/blob/replica、composition/checkout | 方案A：Python space锁+native事务原语；附录F全部OrgFS反例；blob文件/refs同串行域 | S2.2、决定④、#1892重叠已整合或owner书面协调；L6移植表完成；风险不闭合则整体S2受阻 |
| S2.4 | Process/Transport与Python边缘I/O切换 | processes、transport、forwarding、transfer、harness carrier/turn；Lark/Provider边缘 | 真实受控进程、身份fence、query/declaration句柄、断连/关闭、unknown外部结果、配额隔离 | S1、相应S2.2持久能力、决定⑦ |
| S2.5 | 全部领域编排移出actor | Inbox/IPC、Session/Agent/Harness、PAC/Routine、org、registry管理、settlement | 全量业务trace/receipt/fence；W准入；PAC等待图无环；SDK ticket/in-doubt | S2.1–S2.4所需资源、决定⑤⑥、#1892重叠已整合或owner书面协调；L6移植表完成 |
| S2.6 | 完整候选接线、核心替换与旧实现删除 | application bootstrap、kernel/interface与导出、actor_runtime旧包、附录A全部站点、pyproject/uv.lock/tests | AST旧引擎为0；附录B并发diff为0；无Pykka/fallback；附录I规则审计 | CI-I、最终BASE、Allen复核附录A/B/D/H及⑪、所有生产入口新路径完成 |
| S2.7 | 产品、分发、故障恢复与集成验收，形成单一交付PR | tests/contract/CI、正式wheel/源码/worker产物、docs/notes证据 | 总goal判据1–8逐项满足；附录E/G/H证据；精确head；不自行发布/合入dev | S2.6、附录G本goal两硬格、硬E2E凭据、决定⑫ |

S2.3与S2.4在底层能力稳定后可独立实施，但受各自写入范围和资源依赖约束；不通过同时修改同一authority来争抢合入。每项有可审查提交/内部PR，全部汇入`intg/actor-rustify`；最终只有#1897面向dev交付。

原G1/G2只是上述PAC内部里程碑，自动推进，不设置人工规划等待。测试随切片RED→GREEN并独立评审，不留到最后统一调试；S2.1是自动检查点，只有暴露设计矛盾才升级Allen。执行编排另见[并行实施编排](actor-rustify-parallel-delivery-2026-10-25.md)。

## 4. 测试迁移方式

**纯语言/分层迁移：** 同一输入和既有断言应在旧基线与候选上满足相同业务行为；用故障注入证明新测试能抓住丢工作、错误版本、重复提交和失效fence。不是把旧版不能import native当行为RED。

**主动行为改变：** 如同值CAS/no-op版本语义变化，必须单独提交、先真实RED→GREEN、说明Contract-Impact。不能用“迁Rust顺便修了”混淆归因。

**架构删除检查：** 旧树应该在“无Pykka依赖/无旧引擎”检查中失败；这属于结构负对照，不冒充业务行为负对照。

原`tests/kernel/actors`/timers/custody/effects等测试的行为覆盖迁到新公共面或Rust conformance；仅依赖Pykka实现细节的断言有明确替代，不能删除整组测试来获得绿色。实际领域测试、CLI/IPC和installed-wheel验证必须经过native路径，不能全用Python假backend替代。

## 5. S2完成判据（任何一项不满足，都不能complete）

唯一权威文本是下方总goal的判据1–8；旧版概述已被该机械判据替代。基线例外与测试删除映射只能按附录H处理，CI只按附录I的已裁规则处理。FAIL/BLOCKED/UNKNOWN均不能标PASS或complete，也不能通过修改附录、删测试、缩小平台或增加skip收尾。

## 6. 实施控制与外部前置

- G0草案和S1从当前intg精确head开短分支；最终BASE产生后先执行`dev→intg`并重核受影响工作。所有内部PR只合入`intg/actor-rustify`；不直接推dev，不擅自发布包或重启生产。
- 只使用隔离home/state/socket/端口/数据库/cache，不读取生产daemon或`~/.hyprial`。Python一律uv。
- Session/Agent/PAC等目录按附录J冻结窗口与并行团队协调；与#1892重叠的迁移须已整合该线或取得两线owner书面协调。
- tripwire与registry-union仅可按Allen已裁定的⑨及附录I修改；不能移动代码、改skip或放宽断言绕门。
- 平台runner、公开产物渠道和真实provider条件按已裁定的⑧⑩及附录E/G落实；缺实际证据报告BLOCKED且不能完成。
- 与迁移无关的既有缺陷按附录H基线流程裁定，不自动扩成无限全仓修复，也不把FAIL/NOT_RUN写成PASS。
- 每周至少一次`dev`→intg并通过base-freshness；#1892合入后立即同步并重生成最终BASE清单。以阶段结果、变更范围和验收证据推进，不凭耗时或预算耗尽宣称完成，不追加无关跑分。

## 7. 拟议goal（只供评审，尚未设置）

**当前不可设置：R1–R4已落实为单一总goal和连续执行线，但决定①的精确`BASE_SHA`、附录A/B/D/H最终复核及⑪逐条例外仍OPEN。OPEN项被填实前不得执行 `/goal`。G0只交付全部任务包，不是独立goal；S1/S2内部里程碑自动推进。附录G本goal两硬格与后续三格的dev合入/发布门不得混淆。**

### 总 goal（唯一执行goal与最终验收）

<!-- GOAL_PROPOSAL_START -->
在 `intg/actor-rustify` 上完成 hyprial 的 actor 执行内核替换。基线 SHA 为 OPEN（决定①要求取#1892合入`dev`后的`dev` head；当前#1892未合入，须由研发负责人提出、Allen裁定后以精确40位SHA替换本OPEN标记）。完成时只交付一个 `intg/actor-rustify → dev` 的 PR 及其精确 head 的证据，不合入 dev，不发布，不访问生产 daemon，不访问 `~/.hyprial`，Python 一律经 uv 运行。

固定约束：

- 领域规则、SDK、CRDT 计算和应用编排留在 Python；
- Rust（`crates/hyprial-core` 纯 Rust，`crates/hyprial-py` 是薄 PyO3 绑定）只提供通用资源能力，Ractor 承担 actor 执行；
- Rust 不调用任何 Python 业务 handler 或 callable，绑定层的值转换和等待完成通知不得包含业务逻辑；
- 原有 durable 权威、公开行为、fence 和 receipt 保持不变，语义变化必须单独 RED→GREEN 并附 Contract-Impact。

只有以下全部机械检查为 PASS，才能标记完成：

1. **旧引擎为零。** src/ 中不存在 `kernel/impl/actor_runtime/`；hyprial.kernel 的 ActorRuntime、ActorSpec、ActorBackend、EffectLane、ActorHandle、handler_factory、guardian、scheduler 等符号，导入和定义都是 0（AST 门，不用文本 grep）；附录 A 冻结的手写 lane 和 mailbox 全部删除；S2 新增的线程、队列和条件循环站点全部在附录 B 白名单中。
2. **Pykka 为零。** 产品 AST 中没有 pykka；pyproject、uv.lock、wheel METADATA 和默认测试环境都不含 pykka；`tests/support/gates/actor_boundary_gate.py` 已反转为“没有 Pykka 和旧引擎”。
3. **分层门。**
   - `cargo tree -p hyprial-core -e normal` 中没有 pyo3；
   - hyprial-py 中 `call`/`call1`/`call_method`/存储的 `Py<PyAny>` 只出现在附录 C 的 await 桥白名单里；
   - kernel 公共面签名中没有 Callable 参数；
   - 只有 kernel 的 adapter 可以导入 `hyprial._core`；
   - core 中没有领域命名的模块或消息枚举（附录 C 列出禁用词）。
4. **迁移清单剩余为 0。** 附录 D 的每一行（61 个构造点、全部 EffectLane、手写 lane、各数据库的写者）都有去向、新路径的测试 nodeid 和证据链接。附录 D 中属于 A 类的行，都写明了所用的 native 能力；凡是“仅 Python 锁”的行，都在Allen裁定的例外列表里。
5. **不允许静默降级。**
   - 隔离环境里用正式 wheel 启动的 daemon，`disabledCapabilities` 事件数为 0，capability 集合与基线 diff 为 0；
   - 从已安装环境删除 `hyprial/_core*` 后，`hyprial daemon start` 非 0 退出并给出约定错误码；
   - 设置 `HYPRIAL_TAILCAT_ALLOW_MISSING=1` 构建时，不得产出可安装的纯 Python wheel。
6. **真实执行与恢复。** 在不安装 pykka 的干净隔离环境中，按附录 E 的硬 E2E 清单逐项 PASS，并附 native 资源的操作轨迹。附录 F 的反例全部有对应测试且 PASS，包括：
   - OrgFS 的成员移除 TOCTOU、多事务 edit 的崩溃点、blob put/delete 竞争、after_journal/before_state 故障后重启；
   - PAC 的跨进程写者、等待图上满池/超时/shutdown 不死锁；
   - SDK 线程死亡后操作为 in-doubt；
   - 满载、等待者消失、取消、部分提交、worker 和 daemon 重启、PID 复用、关闭期限。
7. **分发。** 附录 G 本 goal 段的每一格，按裁定的构建方式和运行证据 PASS：wheel 的 tag、ABI、版本和 API schema 一致，能 import 并完成一次公共调用；editable 安装（含 uv cache-keys 重建）、worker 镜像、公网快照 `uv build` 都 PASS；Go 和 GUI 原有检查继续有效。
8. **回归。**
   - 结构门（crates/ 是 PRODUCT_ROOT，`crates/target` 排除）、模块边界、ruff、cargo fmt/clippy/test、完整 tests/、isolated contract 全部 PASS；
   - 失败的 nodeid 集合是附录 H 例外清单的子集，skip 集合是基线集合的子集，diff 中没有新增 skip/xfail/importorskip；
   - 删除的测试 nodeid 在附录 H 的映射表中有替代；
   - tripwire 和 registry-union 只按附录 I 的规则修改（只追加条目，或移入 HISTORICAL_ROWS）。

关于状态：

- 任何一项是 FAIL、BLOCKED 或 UNKNOWN，状态都只能是“暂停并报告”，不得标记完成；
- 不得修改附录来满足判据；附录的变更只能由Allen书面裁定，并写明 SHA；
- 以下内容都不构成完成：Dispatch 切片、S1 能安装、性能数据、61/61 映射。
- 附录 G 后续门的 darwin/amd64、linux/arm64、windows/amd64 三格在 #1897 合入 dev 或任何包含它的发布前必须全部 PASS；在此之前 dev、internal 和 stable 不得包含 Rustify。功能 goal 完成不等于可以合入或发布，缺 `_core` 的平台不得 fallback 安装。
- 每周至少把 dev 合入 intg 一次，并通过 base-freshness 检查；在并行团队的目录上改动前，先按附录 J 的冻结窗口协调。
<!-- GOAL_PROPOSAL_END -->

### G0：S1-A至S2.7全部任务包（规划交付，不是独立goal）

最终 BASE_SHA 产生前，以当前精确 intg head 一次性产出S1-A至S2.7的全部任务包和反例原型，不等待#1892。每个任务包必须含：精确base、目标/非目标、唯一owner、允许写入文件集合、接口版本、依赖与外部等待、RED/GREEN命令及测试nodeid、证据形式、独立评审与合入顺序。内容包括：

- CI-I及S1-A–S1-E全部任务包；
- S2.0–S2.7全部任务包，不能只准备S1；
- 全量清单、OrgFS/PAC/Inbox/SDK设计与反例；
- L1–L3/L5在S2.0前、L4在S2.2前、L6在S2.3/S2.5前的具体交付；
- 附录G本goal两硬格、后续三格及dev合入/发布门；
- 硬E2E、基线两次测试和⑪例外裁定任务；
- 最终BASE产生后的A/B/D/H重生成与Allen复核任务。

完成条件：上述全部任务包经研发负责人评审，附录规则带Allen裁定记录和SHA。G0完成不等于总验收；完成后由PAC按依赖自动推进原G1/G2内部里程碑，不再创建阶段goal，不在S1结束或S2开始前等待人工规划。外部等待只按三类精确节点生效，测试随切片完成。

附录A/B/D见[G0清单与写者附录](actor-rustify-g0-inventory-2026-10-25.md)，附录C/E/F/G/H/I/J及14项决定见[G0决定与验收附录](actor-rustify-g0-decisions-and-acceptance-2026-10-25.md)。此文本不触发`/goal`；第四轮复核PASS后按用户授权开始实现。
