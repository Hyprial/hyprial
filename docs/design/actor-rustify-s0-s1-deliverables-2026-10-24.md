# S0 / S1 的具体交付物与完成条件

依据 [评审27578](https://code.hyprial.com/HyprialOS/harness-bridge/pulls/1897#issuecomment-27578) 修订。本文定义接下来要交付什么，不代表S0/S1已经完成；#1897继续WIP。

最新规划已补齐[分发ADR的六项选择](actor-rustify-build-distribution-adr-2026-10-24.md)及[S2全量替换设计/执行计划](actor-rustify-s2-execution-plan-2026-10-24.md)，实施规则已获Allen裁定。S1可不等待#1892推进；最终BASE和实际验收证据仍OPEN。S2涵盖旧S2–S5全部工作，不能停在一个切片。

## S0：交付目标分层设计和可执行任务拆分

S0的目标是把现有owner/消息循环收敛为少数资源原语和Python应用服务。61个注册点是追踪输入，不是要创建61类Rust actor，也不是用“已找到函数”替代语义审查。资源**类型**收敛不等于全部实例合成一条全局队列。

主评审成果为[目标分层图与风险处置](actor-rustify-target-architecture-2026-10-24.md)。[61项逐入口表](actor-rustify-entry-layering-proposal-2026-10-24.md)仅作为遗漏检查/迁移追踪附录；不能把“逐项有建议”当作资源边界已经收敛，也不能据此标S0完成。

### S0-1：目标分层图与资源目录

图中必须标明Python应用服务、native资源、共享运行设施、外部I/O和原有持久权威。每类资源列出身份范围、实例数量规则、状态所有者、操作面、持久位置、生命周期和fault/generation边界。

评审给出的StateStore、SqliteResource、Blob/Space写入边界、TransportSession、ProcessCustody、Timer、EffectExecutor及操作控制是候选清单，不把“6–8类”当硬指标，也不宣称本轮已经验证全部能力：

- 同一数据库文件的writer身份必须核对；领域服务可以共享资源，不因此混合业务规则。
- SpaceWriter是需要分析的领域资源边界，不等于Rust core内置OrgFS规则。CRDT留在Python时必须证明一致性和版本化提交方案，否则该路径标BLOCKED。
- Python SDK调用属于Python应用服务/边缘I/O；不能为了使用Factory把callable交给Rust。Factory仅承担适合原生执行的通用工作或框架内worker管理。
- Timer与操作等待/结果观察采用共享设施，不把每个timer/reply再变成独立actor。

### S0-2：旧入口追踪附录

覆盖61个注册点及绕actor的读路径，并逐项记录：

| 必填信息 | 要回答的问题 |
|---|---|
| 旧入口、调用方、真实消息、source SHA | 当前具体执行了什么，动态转发是否追到真实handler？ |
| 状态/数据库/资源身份 | 唯一权威在哪里，是否与其他入口共享底层资源？ |
| 目标去向 | 由哪个资源原语承接，哪些部分成为Python应用服务，哪些包装可以删除？ |
| 顺序、容量、fence、回执、失败结果 | 合并/拆分后哪些语义必须保留？ |
| 待删除代码及验证用例 | 迁移完成如何证明旧机制不再承担职责？ |
| 证据与未解决项 | 哪些已核对、哪些推断、哪些BLOCKED，阻塞影响哪个阶段？ |

评审的A/B/C/D分类、saga和EffectLane模式作为逐项核对的输入；它们的数量与交叉关系还需落到表中，不能直接把类别数当已验收的实现数量。重点补齐`harness-runtime → owner.receive`与实际数据库共享关系。

### S0-3：并发、背压与故障设计

针对每个合并/拆分决定，给出改变前后的顺序和故障范围，并指定可执行的回归场景：

- 同key顺序、log写入顺序、submit与completion先后关系、合并后的队头阻塞。
- 原有64/128/4096等不同容量如何映射到资源配额；一个热Lark/OrgFS资源不能占尽共享执行机会。
- PAC及remote-workflow的同步调用链和等待图，识别循环等待与worker饥饿。
- 硬规则：native handler不得调用另一个资源或回调Python；需要组合多个资源的业务步骤在Python显式编排，保留各阶段提交/fence。
- 共享worker不等于共享generation或回执。重启、隔离和未完成操作责任仍按资源区分。

### S0-4：源码与分发决策记录

在首个S1实现PR前，以一份ADR收敛以下六项，并同步本计划与10-23旧survey的冲突：

1. 源码位置、core/binding拆分及是否同一个hyprial wheel。
2. 保留Hatchling时，hook使用cargo还是maturin-as-tool。
3. ABI/CPython支持策略，包括是否abi3、free-threaded是否在支持范围。
4. 平台矩阵、每个目标的编译方式、runner及运行验收方式。
5. 公网预编译wheel、源码安装和缺工具链错误的策略。
6. core workspace与`app/src-tauri`的关系、MSRV及lockfile管理；不能仅凭未来path依赖就假定必须共用一个lockfile。

[ADR](actor-rustify-build-distribution-adr-2026-10-24.md)选择**同仓crates、同一wheel、Hatch调用Cargo、abi3-py312标准CPython、本goal两硬格加三后续格和独立core/Tauri锁**，不再保留互相冲突的首选方案。规则已获Allen裁定，实际构建验证仍OPEN；新增源码根必须同步AGENTS、结构门、构建产物排除和SNAPSHOT_WHITELIST，不能先放到未扫描目录。

已核对当前源码：Hatchling只声明`packages=["src/hyprial"]`；GUI hook已有force_include和`py3-none-<plat>` tag设置；wheel-release在Ubuntu循环构建五个Go目标；结构门点名源码根，当前没有`crates/`；`app/src-tauri`有独立workspace和MSRV1.85。它们足以证明ADR必要，但不能证明新增扩展已经可发布。

### S0-5：实施任务包

G0一次交付S1-A至S2.7全部任务包，包含前置条件、文件范围、接口输入输出、测试/负对照、删除项和交付判据。研发负责人评审后由PAC连续推进，不需要实施者重新猜架构，也不在S1结束后等待人工规划。

**S0完成条件：** 目标分层明确；旧入口有可追溯去向或明确阻塞；S1依赖的六项分发决定已收敛；顺序/背压/故障风险有验证方案；全部任务包可执行并经研发负责人评审。未解决的CRDT或领域路径不能标为已经可迁移。S0若只有部分前置工作完成，可先做不依赖阻塞项的S1子任务，但不能因此把S0整体写成PASS。

## S1：交付可安装、可运行、可验证的共享底座

S1交付真正代码、安装产物和CI证据。它证明基础设施可用，不代表Dispatch或其他业务已经迁移完成。

### S1-1：共享宿主与薄绑定

- 按ADR落下纯Rust core crate、PyO3绑定和kernel公共门面；纯core不得依赖CPython/PyO3/领域模块。
- 一个宿主共享一套Ractor/Tokio运行设施，多个资源使用opaque handle，不按资源新建runtime。
- CommandHandler是唯一native命令入口；接纳、资源/全局预算、失败通知、资源关闭和宿主关闭有明确路径。
- 复用框架提供的循环、定时和worker管理，不重写通用runtime；不在Rust内部运行Python业务callback。

### S1-2：最小可运行调用链及行为证据

提供由kernel公共面调用的少量**通用测试资源**：注册/打开多个资源，提交owned数据命令，获得结构化Outcome，观察完成，关闭资源与宿主。至少包含真实状态读取/条件更新，不只演示import或ping。

需要证明：共享宿主没有全局串行瓶颈；同资源顺序与不同资源隔离；容量满时明确拒绝；等待超时不丢已接纳责任；panic/失败可观察；drain能区分完成与超时未完成；等待者消失不会悄悄丢弃操作。测试资源不读写真实业务数据库，也不改变Dispatch公开行为。

S1明确只支持已实现的能力。没有持久回执/恢复的资源不能宣称durable custody，未实现的Sqlite/CRDT/Process等能力不能伪造成功或fallback到Pykka；它们属于S2后续子里程碑，必须全部完成才可结束S2。

### S1-3：同一wheel与安装验证

- 在原hyprial wheel中包含正确目标扩展，安装后可经公共面import并使用；tag、Python/native版本及ABI与ADR一致。
- 源码安装可重建扩展；缺Rust/C工具链、扩展缺失/不兼容有明确错误，不静默启动另一种backend。
- 兼容现有GUI/Go产物构建，不用“本地cargo build通过”代替wheel、升级及安装证明。
- 公网快照包含构建所需源码，构建不依赖仅内部可获取的第二个同步版本包。

### S1-4：CI与目标平台证据

按Allen R1新裁定，本goal只要求darwin/arm64原生与linux/amd64 manylinux Docker两硬格的构建及import/最小调用证据。darwin/amd64、linux/arm64、windows/amd64保留原构建/运行证据口径，移入dev合入/发布后续门，不阻塞S1功能底座。

交叉编译产物也必须验证目标架构/tag及可运行性，不能拿Go交叉编译成功代替PyO3验证。后续三格未PASS前#1897不得合入dev或进入任何发布，缺`_core`不得fallback。CI-I是S1-D/E前置；同步结构门、公开快照清单、工具链版本、wheel扩展检查，并保持已有GUI/Go检查有效。

**S1完成条件：** 在干净隔离环境中，通过正常uv安装一个hyprial包，启动共享宿主，创建多个测试资源、发送命令、获得结果并正确关闭；[附录G](actor-rustify-g0-decisions-and-acceptance-2026-10-25.md)本goal两硬格和失败路径有证据。只有手工加载实验.so、只跑一个计数器，或仍靠Pykka兜底，都不能算S1完成。S1是自动内部里程碑，不等待人工再规划。

## 阶段衔接

S0输出“应该怎样分层、如何实现及验证”；S1输出“可安装运行的公共底座”；S2输出“完整生产替换且无Pykka/旧actor引擎的候选”。Dispatch、持久资源、逐域编排和最终删除均是S2内部步骤，不能用任一局部步骤替代终态。
