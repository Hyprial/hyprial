# GUI v0.1 lifecycle TODO

分支 `intg/gui-agent-journey`；PR [#2727](https://code.hyprial.com/HyprialOS/hyprial/pulls/2727)。更新：2026-11-14。

本文件只维护顺序、状态和验收条件。具体方法名、参数/回执、已有实现位置、接线缺口，统一见
[用户旅程与方法对账](gui-v0.1-method-plan-2026-11-13.md)；术语只维护 [GLOSSARY](../../GLOSSARY.md)。
下列未完成项是计划，不表示已实现或已通过真实验收；T12b 元数据接线已独立审核通过，启动关联证据仍待补齐。

## 本次交付范围（用户收敛，2026-11-14）

Allen 的最新合入门槛覆盖下列历史排期：#2727 先于 GUI 单一启动其余 PR 合入 dev；本分支冻结新功能、页面和文案，只修冲突及新增测试/CI缺陷。未完成项移至合入后的后续 PR，#2738 不在本轮处理。按 #2755 第3、4.2、5节记录存量路径/fixture/静态不可用文案，不恢复退役路径、不再修改原生壳或打包目录。测试门满足后再启动固定候选供用户手测；不绕过必需检查。

用户决定先固定一个可以启动的组合，后续再补齐其余能力，替代此前“所有组合完成后统一手测”的要求。
本次候选先将 Pi 启动增量 `e808f9a940c220a070c19d8015be479eb1e76469` 与 dev `dab89f8c6` 合并，测试修复后为 `b685ffc2c`；用户反馈PR再次冲突后，继续对齐 dev `f04d40b3a` 的旧迁移链路退役。验收范围仍为：登录/初始化后，创建 Pi Agent、导入 Pi 原生 `auth.json` 与 `models-store.json`、配置模型/工作目录、显式确认目录、启动并导航 Chat。
受控模型下真实浏览器/HTTP/daemon/Pi 启动通过；真实用户文件/厂商认证仍须手测，不称为已验收。
Key 正式装配、自定义 provider 测试、其他 harness 启动矩阵、平台/Agent 删除、完整聊天、目录选择器与原生打包均为后续项，不再阻塞这一组合的候选交付；现有已接线能力保留。
合入 dev 前仍须完成本次改动回归及固定 SHA 独立审核；只修阻断当前范围的问题，不因上游 Rust CI 全红扩大修复范围。未执行 merge 或 release。

### 当前 dev 合并与启动核对

- 当前合并目标改为 `dev@f04d40b3abb014636b3e4daea01bf8fb6c73a876`。#2723删除旧agent move/transfer/P2链路，与本线两处pin端口调整产生修改/删除冲突；保留dev的删除，不恢复退役文件。运行时三方目录核对：合并前153/124、dev138/102、合并后153/124；本线所有声明哈希及OpenAPI不变，dev声明零丢失。定向回归、固定新SHA审核和手测仍需分别记录，旧b685的PASS不覆盖本次合并。
- 此次合并的定向回归497 PASS（含P2退役、pin/删除恢复、Agent启动、登录初始化真实HTTP生命周期），CI计划/收集134 PASS；结构、模块边界、方法登记、请求契约、daemon架构、术语、生成客户端、TypeScript检查通过。Ruff扫描仓库跟踪文件零问题，83项均为本地未跟踪历史探针，保留其原文、不提交。证据 `/private/tmp/gmf-y294ryjw`；未重跑全量或外部厂商验收，独立审核与用户手测待完成。
- 将 `dev@cecde87f6e51fd023b9e89a273f268e3b91f69aa` 合入已公开的集成分支，保留既有审核 SHA。两处文本冲突已解决：方法数量按实际目录改为153；保留上游OrgFS问题KP-048，本线初始化恢复条目改为KP-059。
- 对比共同基线、原集成头和dev：上游仅新增 `teleport.run`，没有删除或修改本线依赖的MethodSpec。合并后153个声明/124个HTTP方法与运行时导出一致；旧Web auth/setup/live退役文件没有恢复。服务装配、关闭顺序及真实启动仍须回归。
- [#2755](https://code.hyprial.com/HyprialOS/hyprial/pulls/2755) 当前仅是设计PR，单一Python宿主与 `--with-dev-tools` 尚未实施。此次沿现有正式源码入口验收，不新增启动通道；命令和边界见对账同名小节。
- e808独立审核检查点：17项独立探针通过，父版6项失败；规范cwd及拒绝零副作用通过。restore未再次比对规范路径，祖先目录被替换为符号链接可重定向批准目标，审核定为P3；后续在自动恢复依赖前补拒绝/重新批准，不宣称仓库身份绑定。
- 原e808完整Python尚在排队时取消，由本次合并固定快照取代；取消不算通过。旧812全量已结束：27977 PASS/31 FAIL/44 SKIP，不作为当前候选验收。合并后的回归、浏览器、独立审核与用户手测结果分别记录，不沿用旧结论冒充新验收。
- 首次合并7d204415b：干净Python3.12后端769 PASS，Ruff/结构/模块/登记/请求契约/GUI与daemon架构/术语/client/fixture/parity/CI策略通过。受控Chrome→HTTP→daemon→Pi完整创建/导入/启动/导航PASS（/private/tmp/gui-dev-sync-pi-e2e-7hs57iqa/result.json），不是厂商认证。e808独立审核最终限定PASS，P3保留。
- 验证期间dev又合入#2749/#2754，更新到dab89f8c60d5c20c6c714b506a48b0bdcf15ea26。无冲突；主要新增原生CI/停止事件顺序及登录凭据路径定义下沉，未改GUI或MethodSpec。补测登录/退出和相关边界，不恢复旧credentials导入。此后冻结候选，不追逐无关上游提交。
- 103c9c456的共享浏览器/Pi创建→导入→启动→Chat导航再次PASS（/private/tmp/gui-candidate-pi-e2e-748ll7ai/result.json）。后端扩大472 PASS/1 FAIL：本线拆出的agent_pins两文件漏登记严格resident清单；补登记原composition来源，仍执行凭据禁入扫描，不改checker或加豁免。清单+新增负例3 RED→GREEN，产品代码不变。
- GUI全量2142 PASS/13 FAIL/1 SKIP：12项KP-050；额外四步引导颜色断言在e808与7d单独复跑同样失败（KP-060），保留用户暂缓引导的决定。不能报告GUI全绿。原生打包和真实厂商认证仍未验收。
- 测试清单修复266c6463：干净Python3.12累计481项后端焦点及Ruff/结构/模块/登记/契约/双侧架构/术语/CI策略通过。正式源码启动器在独立59647端口启动，health确认daemon driver；用户手测待反馈。
- 266全量因两个非package测试同名 `test_recovery.py` 在收集期退出；本线文件改名为 `test_adapter_removal_recovery.py` 并更新真实port测试的helper导入，保留所有断言，不改pytest导入模式或跳过测试。管理恢复/组织恢复66 PASS，全仓28462项收集成功；完整执行按新固定头重跑，未结束不记PASS。
- 最终候选 `b685ffc2c851e13215d35b3c48a6454c80690920` 全量已结束：28392 PASS / 20 FAIL / 43 SKIP / 7 XFAIL / 2 XPASS，不记全量PASS。6项为Darwin下载缓存环境限制，13项post-install快照差异经后续精确对照确认属于本分支遗漏，1项teleport中途SIGTERM恢复失败仍待归因；原始日志 `/private/tmp/gcfull-h8kjyzez/python-full.log`。此结果不等于最新合并头的全量结果。
- `91208a0` 对照 `dev@f04d40b3a`：13个post-install分支检查在dev全通过、本分支全失败；原生成器测得预加载清单增加3个 `identity.impl.agents.actor.adapter_removal` 模块，现已同步清单，执行函数/场景计数、未预加载导入拒绝及所有测试断言不变，相关22项通过。另两项pin测试仍引用dev已删P2模块；删除纯P2装配测试，混合测试保留现存facade.pin的预约拒绝和补偿断言，pin/退役门39项通过。未新增skip/xfail或门禁例外。6项uv缓存失败在两树同样复现；干净快照全树Ruff、CLI inventory、CI策略、契约差异、网络/等待/Windows检查均通过。证据 `/private/tmp/gci-f8rl8g9y`；修后远端完整CI和增量审核另记。
- b685增量独立审核已完成，限定PASS：方法目录153声明/124 HTTP保持一致；resident登记、测试收集、登录退出边界和daemon装配无回退。481焦点独立通过；不代表完整Python、真实厂商认证或原生打包通过。报告 `/private/tmp/evidence-gui-candidate-b685-20261114/REPORT.md`，审核图 `wf-05f19d16afa64d8eb8a400c80b327ff7`。
- 按用户降载要求，测试已结束，手测服务及孤儿验证服务已停止；4080/4180/4181/59647/62734均无监听，运维独立复核无残余。测试数据保留，未动生产daemon或connector；会话主进程高占用与心跳延迟仍未解决，运维跟踪于PR #2748（其问题编号与本分支不一致，不复用本分支KP-058）。当前不再启动测试或服务，候选尚未重新手测或合并dev；三种启动方式以现有启动指南和当前源码为准，本机账号模式须与运行中daemon的版本/方法目录一致，尚未实测。

T25d 回归修复及 T26 清理的代码/架构已获独立 PASS（限定范围）；完整 Python 已结束，清理新增的 1 项测试断言已修，增量已独立复审 PASS（c2f4571ee，测试/文档范围）。下一阶段按下表接线。已在 dev 复现的
基线失败按 KP-057 单独跟踪，不扩大例外清单、不跳过断言，也不把远端失败写成通过。

## 下一阶段实施顺序

2026-11-14 顺序调整：先验证 T12d 创建/凭据/启动与 T14 导航/基础对话；T18 删除异常恢复的当前改动收束后独立审核，文件组合/启动恢复/删除 HTTP 接线继续列为未完成，不再挡住主线验证。T10 正式依赖仍 BLOCKED，不以宿主环境注入或旧 Key 通道替代。

T12d 进展：af3a835a8 实际 Pi 启动复现 `PI_P2_LAUNCH_UNTRUSTED` 后，按 cli-developer 的代码核对扩展既有 `agent.start.approve`。共享启动弹窗要求显式工作目录与确认，批准随启动配置供自动恢复；不是一次性授权。真实 Chrome/production Node/HTTP/隔离 daemon/Pi 已点击创建、导入两文件、确认启动、读回 running、导航 Chat；1440/390 无溢出，无 JS 错误，一次 start。后端焦点359、GUI焦点79 PASS；全量 GUI 2142 PASS / 12 KP-050 FAIL / 2 SKIP，未出现新增失败。真实 daemon 重启后同 cwd 的 Pi 自动恢复 PASS（/tmp/hgs-41pso4cl/restored-result.json）；补充输入符号链接重定向 RED→GREEN，批准保存严格解析的规范目录；停止移除自动恢复意图。固定最终快照完整 Python 与独立审核继续，厂商认证 UNKNOWN。证据 `/tmp/gui-pi-approval-e2e-_6jsoy0x/result.json`、`/tmp/hgs-wnwdcx5a/result.json`；用户未重新手测。

基础消息真实 HTTP 受理并触发本地受控模型请求，但 45 秒没有用户收件箱回复；该项 FAIL。新探针明确返回 `replyPathUnavailable=true`，隔离用户没有 Squire/profile 平台回复路由；不能把发送成功当作对话通过，继续核对已有 GUI 会话路径，不新增聊天方法。证据 `/tmp/hgs-wnwdcx5a/result.json`。T18 内部恢复增量 af3a835a8 已获第三方限定 PASS（无阻断项）；超时后的自动补偿和文件/HTTP 接线仍后置，报告 `/private/tmp/evidence-removal-recovery-af3a835-20261114/REPORT.md`。

T12d 固定提交 `e808f9a940c220a070c19d8015be479eb1e76469` 已派独立审核：`wf-246c87c3a2ad40f0b137770b6f7f3d22#review`，投递 accepted、节点 requested，尚无结论。会话恢复后已确认 cli-developer 补充消息；规范路径已保存，但 restore 对规范路径目标被替换的再次比对尚未新增，交审核明确评估，不能宣称仓库身份锁定。最终共享页面浏览器重跑 PASS：`/private/tmp/gui-pi-approval-e2e-ory2u5n3/result.json`，仍是受控身份/设备/模型。固定提交全量 Python 排队等待旧快照测试结束，未完成不记 PASS。

| 顺序 | 待办 | 完成条件 |
|---|---|---|
| 1 | T10 修共享 Key 正式装配，扩展自定义 Pi 凭据及测试 | 先确认 U2 正式绑定签发/撤销/发布依赖，再修既有 user.provider.* / agent.provider.* 的 owner store、身份映射、写入准入和签名（KP-030）；再扩展 login/list 的 endpoint/protocol、实现已批准的 user.provider.test。独立验证保存、授权、启动物化、真实响应；测试不持久化。依赖未到位时可先推进独立的 T12b |
| 2 | T12b 原生 Auth 清单与沿用/替换/移除接线 | agent.provider.auth.list 及共享读回已获独立 PASS（97e7966，限定范围）；替换沿用既有 login，移除沿用 logout，不新增 auth.status/切换写方法。导入即能读回文件；导入版本/时间与关联启动证据仍缺权威来源，正式返回 null。未知删除结果只读回 |
| 3 | T11 平台接入；T18 平台删除；T19 旧 Lark 入口退场 | identities.list/sync 已独立审核通过；adapter.register 已补 daemon/HTTP/GUI 并获独立 PASS；authorization 已独立 PASS；onboard 已独立 PASS，均将既有库能力收进 daemon；identity.resolve 已有只补页面；删除补 management.adapter.remove.preview 并迁移既有 remove 的确认协议。统一链实测后删除剩余 GUI-only 分发，不删共用平台实现 |
| 4 | T12 Agent 配置；T12d 启动组合 | 原生/Key 分开验收创建记录、写凭据、配置模型、显式启动、真实回复。harness 保持创建选择，组织入口禁用；不把 mock running 当认证成功 |
| 5 | T13 路由；T14 基础聊天冒烟 | adapter.routes.candidates/list/add/remove 已接线并独立限定 PASS；平台先收到消息再读候选并配置。完成 Agent 配置、启动、导航至 Chat，尝试已有链的一次基础对话；完整聊天、多端和 route 发送 HTTP 扩展留下一阶段，不新增 route.send |
| 6 | T12e Agent 删除 | 暴露已有 agent.destroy.preview/destroy；完善停止、平台绑定、任务/消息、目录副作用与并发确认。真实模式保留禁用直至接线验收 |
| 7 | T12f 目录选择；T15 状态计数；T14 Squire 引导 | 可信宿主返回绝对路径，当前保留输入；创建后 cwd 已扩展 agent.start，0e1b0b14e 已获独立限定 PASS。任务计数复用 workflow/routine 读方法，先定口径；Squire 复用普通 Agent 链。各页数量/状态从同一事实派生 |
| 全程 | T16 统一接线验收；T20 CLI 迁移 | GUI → GeneratedClient → HTTP → MethodSpec 准入 → daemon request → daemon/identity。能同批迁移的 CLI 改为同一请求面；暂不能迁移的明确调用点/条件，不建立第二份状态。分别报告 mock/契约/HTTP/浏览器/外部服务结果 |

初始 16 个计划请求逐项列在对账第三节（另补既有设计遗漏的 management.adapter.remove.preview），其中原生 Auth 清单、平台身份 list/sync 已实现并独立审核通过；adapter.register 已独立 PASS，authorization.status 已独立 PASS，begin/operation/cancel 已独立 PASS，onboard 三个已独立 PASS、routes 四个已独立 PASS，余 user.provider.test 尚未新增；已有删除、身份查询、凭据写入、模型设置等列在第二节，不能混成“新方法”。
明确不补：auth.status、另一个 Key CRUD、agent.model.set、chat.create、route.send、默认 harness setter。

权限范围：本人 Key 供本人 Agent 选用，不新增组织审批或 ACL 配置 UI。组织可见不自动获得写权限，组织与 move 本轮不扩展；后续按后端归属与统一权限判定。cli-developer 已按4cfc1b29a确认新通路的 U2/owner-store/写入准入仍未实现，没有现存正式个人替代通道；KP-030 是实现缺口，不是用户需要额外申请权限。组织 Agent 的 provider.status 也被拒绝，不能作为只读目录。

## 登录与初始化恢复

- [ ] T05 启动前检查 IPC socket 路径长度：长路径明确拒绝，短路径正常，不静默切换 home/state。
- [ ] T06 修 KP-059：身份已提交但初始化未完成，重启后状态建议 initialize 而目录不开放。需状态、准入和恢复一致；不伪造 completed、不重放未知写。
- [ ] T07 在专用隔离环境实测初始化中刷新/强刷/关闭重开/后退：覆盖提交前、未回包、generation 切换、完成后；写不重复、等待有界。
- [ ] T08 服务退出的诊断/恢复：区分进程退出、连接/握手失败、业务结果未知，保留可定位原因。
- [x] T09 启动指南说明临时数据保留与重启：复用同一 home/state/binary，不以重新 mktemp 代替恢复。见 [开发指南](../guides/gui-dev-mode.md)。

## 已完成的前端与核查

- [x] T01–T03 隔离 4181 演示恢复与 home/state 核查；4180 为独立纯前端预览。此处记录历史完成，不保证服务当前常驻。
- [x] T04 会话恢复单测覆盖轮换、reload、30 秒上限、不重放写；不代替 T07 强刷验收。
- [x] T11a 平台身份语义、权限 ID、不编造 owner；重复同步保留已有绑定。正式身份读写仍归 T11。
- [x] T12a/T12c 创建和详情 UI：真实文件选择、兼容 Key 筛选、停止/启动单入口、所属组织禁用；凭据写失败保留已创建 Agent。
- [x] T12b 前端与元数据接线：Key/Auth 共用清单与空状态，Auth 详情沿用/替换/移除，启动后重读；正式元数据读取已独立审核通过，启动关联证据未完成。
- [x] T12d 前端部分：创建不自动启动，模型启动前配置，自定义尚未接线时禁用。
- [x] T12e 前端部分：mock 删除含确认/取消，并更新目录、计数、授权、绑定及聊天；正式删除仍未接。
- [x] T17 方法核查：136 个声明/109 个 HTTP 与 OpenAPI 一致，16 个拟定请求未注册；113 个隔离后端焦点测试通过，不算整条用户旅程验收。
- [x] T21 移除无消费旧 UI、无效装配参数及导出；剩余 Lark 实际消费者留待 T19 迁移。
- [x] T22 平台 mock 移至 dev，绑定 mock 移至测试 fixture；模块门新增对同文件 mock 导出的检查。
- [x] T23 文档收敛：一个对账、一个 TODO、根术语表；删除重复文档与测试证据文件。既有组件目录/入口映射/启动指南仅维护各自职责。

- [ ] T24 四步引导后续单独整理（用户明确暂缓）；保留当前实现和归档数据。KP-054 的浏览器测试已对齐当前 fixture/菜单/归档行为，12/12 PASS；未改四步引导产品代码，KP-055 仍保留。
- [x] T25a rebase 到 dev `46a29ba48`（含 #2742），保留 Web/native 生命周期分流；HTTP 方法清单与原分支一致。核对及已完成回归见对账的 rebase 小节。
- [x] T25b 本轮真实 Web 新账号注册/登录、初始化、退出/重登录：用户在隔离端口 62734 明确反馈整段正常；采用独立短路径 home，不接日常 daemon。全量 Python 失败仍需单独定位，不能直接归上游。
- [ ] T25c 原生授权页打开与打包验收（打包线 + GUI 能力接点）：处理 KP-056；系统浏览器打开、回 GUI、原生 external promotion/退出均需安装包复测。原生 7 项既有测试失败由打包线核对。
- [x] T26 旧 Web auth/setup/live 清理代码/架构独立 PASS：固定 `ccca186d5`，含专用测试及旧 runner；保留四步引导/归档、native capability、聊天控制器与 #2742 生命周期。复审 GUI 2086 PASS / 12 KP-050 FAIL / 2 SKIP、共享展示 12/12、Python 焦点 1231 及受控真实浏览器两轮循环 PASS。完整 Python 27222 PASS / 36 FAIL；35 项与已验证 dev 失败同 nodeid，新增的旧种子哈希断言已修，增量已独立复审 PASS（c2f4571ee）。不代表整包通过；见对账 T26 小节。
- [x] T25d 清理前回归：`5a6e002717` 独立 PASS（限定范围）。登录竞态账号 51 PASS，独立合并/失效探针 7 PASS；共享展示浏览器 12/12 PASS；GUI 全量 2112 PASS / 12 KP-050 FAIL / 2 SKIP、Python onboarding 530 PASS。GUI 架构、method-registration、契约、结构、术语等门通过；daemon 架构 HEAD/dev 均 611/14，相同基线失败。完整 Python、远端 CI job 日志、外部 issuer 与原生未独立复验，不记整包 PASS。

## 验证与后续边界

本轮修复 `5a6e0027170665525c13430bd47eec499ff4a743` 已同步普通分支及 PR #2727。
独立复审 `wf-d6c441fc9ffa486b827533b5954e6449#review` 已完成并返回限定范围 PASS。
证据：`/private/tmp/evidence-gui-account-race-5a6e002-20261114/REPORT.md`；原始日志不入库。
T26 仅清理无产品消费者的旧 Web auth/setup/live 与其专用测试、退役的旧聊天 runner；
保留四步引导/归档、native capability、聊天控制器及 #2742 活动生命周期。
清理已固定并推送 `ccca186d5a715d5f3bab30f018c3eddec98b39db`；
独立复审 `wf-2032f27eb8514ce7b74fb5d791498fc0#review` 已完成，代码/架构限定范围 PASS。
证据 `/private/tmp/evidence-gui-cleanup-ccca186-20261114/REPORT.md`；原始日志不入库。
复审指出的过时 dev-03 启动指南已删除，文档索引指向现有对账；启动命令只维护在 GUI 开发指南。
PR 已含本轮退场的 Contract-Impact 声明；未更改现行 MethodSpec/OpenAPI。
完整 Python 日志 `/tmp/gui-cleanup-python-full.log`：27222 PASS / 36 FAIL / 45 SKIP / 7 XFAIL / 2 XPASS。
失败项与 `/tmp/gui-ci-prepared-failures.log` 比较，35 项相同；新增种子断言改为允许例外缩减、拒绝用缩减清单重新首次播种。
定向复跑 `/tmp/gui-cleanup-seed-tests.log` 为 13 PASS / 1 既有 baseline FAIL；不拼成全量 PASS。
远端 ccca186d5 的 GUI、声明、hygiene、模拟模型 E2E 已 PASS；Python checks/CLI inventory FAIL，分片未运行；具体远端失败日志仍未取得。

当前 Key 正式装配 BLOCKED（KP-030）；跨进程初始化失败恢复 BLOCKED（KP-059）；Auth 元数据读取已独立审核通过；版本关联启动证据未完成，路由已获独立限定 PASS；身份 list/sync、adapter.register、authorization.status 已独立审核通过，authorization 三个操作已独立 PASS。
真实模型、真实飞书完整旅程、原生壳仍 UNKNOWN/NOT RUN。前端预览和受控后端通过不能覆盖这些范围。
本轮清理的实际检查结果写入 PR，不再新增日志、截图、探针或证据归档文件。

### T12b 当前增量（2026-11-14，未完成整段启动验收）

- 已实现独立 Auth 元数据读取；已有 login/logout 不新增写入口。完整文件可沿用，不完整只能上传修复；读取失败不能冒充空列表。
- 已测隔离真实 daemon + HTTP + production Node + Chrome：Claude/Codex/Pi 各一次创建、导入、沿用、替换、全局读取、移除；合成凭据，无厂商认证/启动验收。
- 原生 Auth 的持久导入版本/时间、版本关联启动证据仍待启动阶段补齐；共享 Key 的 U2 依赖、平台和路由缺口不因此完成。
- 最终 GUI 为 2088 PASS / 12 KP-050 FAIL / 2 SKIP；远端 GUI、受控模型 E2E、声明及 PR hygiene 已通过。完整 Python 为 27224 PASS / 43 FAIL / 45 SKIP；35 项同固定 dev，8 项新增方法登记遗漏已修，受影响八文件 147 PASS。修后仅测试/清单/文档变化，未重跑全量，不拼成全量通过。
- PR hygiene 在新方法的六处精确 wire 术语登记上复现失败；仅补路径/整行登记及拒绝扩展的回归测试，7 FAIL → 20 PASS，不增加通配规则或架构例外。
- 独立审核最终快照 `97e7966f7214392f18a9775ddfe620917dacd539` 返回限定范围 PASS：8/8 原失败项、140 受影响测试、950 IPC contracts、42 OpenAPI 通过；独立受控浏览器覆盖三种 harness 的导入/沿用/替换/移除。产品仍为 `2348e7f56`，最终两提交只修测试/清单/文档。
- 审核图 `wf-e8cf08b108394990bb8992d52e7c8198` 在最终增量到达前以 REWORK 完成，保留历史不重置；最终 PASS 以回执 `e56fbf59-18ff-5c7c-834e-d755cfacded4` 和更新后的 `/private/tmp/evidence-gui-auth-list-2348e7f-20261114/REPORT.md` 为依据。不为改写图面重复派审。
- 远端 Python checks/CLI inventory 为 FAIL、分片未执行，具体 job 日志未取得。已有 dev 失败跟踪 KP-057，不据本地归属推定远端原因。真实厂商认证/启动与修后全量 Python 未独立验收。

### T11 平台请求接线（2026-11-14）

| 增量 | 固定提交 / 状态 | 验证与剩余事项 |
|---|---|---|
| identities.list/sync + 空名称契约修复 | ad135057d，独立 PASS | 实际 HTTP 13、后端/契约 1303、GUI 30；受控 daemon/HTTP/浏览器 PASS。真实平台仍待测 |
| adapter.register | 890b41d42，独立 PASS | 重复名拒绝、保存/加载分离、未知不重发；受控真实 HTTP/浏览器 PASS；独立后端1750 PASS，短 TMPDIR 环境失败复跑通过 |
| authorization.status | 7d9a6c938，独立 PASS | 独立权限读取，不申请权限；独立 GUI2105 PASS/12 KP-050 FAIL/2 SKIP；后端1845 PASS，真实socket超时2.012s、受控浏览器PASS |
| authorization.begin/operation/cancel | 6309ab75a，独立 PASS | 复用现有授权 service/SDK，显式权限与 operationKey，daemon 私有回执、重启 unknown、不自动重发；共享 GUI 恢复/取消。受控浏览器链 PASS，真实飞书待测 |

- 正式路径统一为 GeneratedClient → HTTP → MethodSpec → daemon request → daemon 授权库。CLI 只作为既有能力查验来源，不是 GUI 执行通道。
- 当前：新应用 onboard 已获独立 PASS；路由已获独立限定 PASS，随后删除 → Agent 配置/启动/基础对话。共享 Key 正式写入仍受 KP-030 阻断。
- 全量 Python 串行：旧 ecfe 快照结束为27246 PASS/40 FAIL；35项同已复现dev清单，另5项已聚焦复跑通过：shutdown 时序失败单跑未复现；3项 Claude 测试受继承的 context window 环境变量影响；浏览器缺系统 Chrome 配置及隔离工作树依赖。保留原全量失败，不拼成全量 PASS。6309授权写快照全量已启动，未完成不算PASS；未中断活跃pytest。
- 原始证据只放 `/tmp`。注册独立报告 `/private/tmp/evidence-platform-register-890b41d-20261114/REPORT.md`；授权作者日志前缀 `/tmp/gui-platform-authorization-`，受控浏览器 `/private/tmp/gui-platform-authorization-e2e-k2cvu2_b/result.json`。
- 远端 GUI/声明/hygiene 曾通过，Python checks/CLI inventory失败且未取得job日志；不推断远端根因、不记整包PASS。daemon架构21条与父一致，未扩大baseline。

授权写独立复核6309ab75a：后端72/扩大1827 PASS，GUI43/全量2108 PASS，12项仍KP-050、2 SKIP；回执探针7、GUI写探针2及受控真实浏览器PASS。报告 `/private/tmp/evidence-platform-auth-write-6309ab7-20261114/REPORT.md`；真实飞书/issuer、完整Python、原生、远端CI仍未验收。

### T11 新建平台应用（2026-11-14，独立 PASS）

- 已实现 adapter.onboard.begin/operation/cancel，复用官方 SDK 与既有配置写入；共享页面可恢复、取消、显式重新开始并读回接入。
- 用户操作：Agent 设置 → 添加接入 → 创建新应用 → 名称 → 开始添加 → 打开平台授权页面 → 完成后刷新添加状态。
- 取消只回收本地等待；远端已创建应用不自动删除。保存结果未知只核对，禁止自动重发。
- 受控真实 daemon/HTTP/production Node/Chrome 流程 PASS；真实飞书创建与整体配置到启动仍待验收。路由四个方法已实现，剩余拟新增请求为 user.provider.test；已有方法的接线与正式装配工作另计。

新应用回归：GUI 全量2111 PASS/12 KP-050 FAIL/2 SKIP；仅既有macOS缓存脚本失败。生成客户端/fixture/parity、build/typecheck、结构/模块/术语/Windows/方法登记/请求契约/GUI架构、CI策略156项通过；daemon架构601/21，与授权快照逐条一致，未增baseline。

新应用扩大后端/HTTP/生命周期回归2002 PASS，golden/OpenAPI70 PASS；原有应用注册与授权回归包含其中。第三方已独立复跑并返回限定范围 PASS。

新应用固定提交 `0d6db89ef18a8f2e85b434ef145bc590ce369f2e` 已推送普通分支和PR2727；独立审核图 `wf-75af13cc56e64139aa79c4afd607409d` 已完成并返回限定范围 PASS，按新 SHA 实读代码。干净树Ruff与固定SHA受控浏览器复跑PASS；全量Python排在6309之后。开始核对T13既有路由库与GUI绑定页。

### T13 消息路由接线（2026-11-14，独立限定 PASS）

- 统一 HTTP/MethodSpec/daemon 链读取候选与配置、保存/删除路由；复用既有 route 库、配置锁与入站 SQLite，不经 CLI。
- 写请求带 expectedRevision，接入 App 或路由变化均拒绝旧页面写；删除默认项需 clearDefault。未知回包只读核对，不自动重放；写入成功与加载结果分别展示。
- 共享页面与 dev 状态同步；绑定来源切换时丢弃旧列表/写回执。候选读取失败不显示为空。
- 受控隔离 daemon/HTTP/production Node/Chrome 已点击创建 Agent、导入合成 Auth、绑定平台、读取合成入站候选、保存默认路由、刷新读回和确认删除；不代表真实飞书、厂商认证或 Agent 启动。
- 焦点后端100 PASS，GUI17 PASS；旧来源列表竞态1 FAIL→PASS。全量首次2115 PASS/14 FAIL/2 SKIP，其中12为KP-050，另2为新增示例摘要不符合合成值门，已定位并仅修示例，不改扫描器。
- 用户要求完成全部计划后统一手测，不提前拆出 Auth 或 Key 手测阶段。共享 Key T10 仍需完成；已向 emergent 核对统一 ACL、U2 和 owner-store 的正式接点（消息921a9e89-acc1-56cd-865d-8cd9f7ef6eee），不新增第二套权限体系。

新应用0d6db89ef独立审核已PASS：后端85/扩展2616、GUI54/全量2111、backend探针8/GUI探针3、受控真实浏览器均通过；12项KP-050仍FAIL，完整Python/真实飞书/原生/远端CI未验收。报告 `/private/tmp/evidence-platform-onboard-0d6db89-20261114/REPORT.md`。
- [x] T11恢复入口修复：已保存但未加载的新应用恢复后关闭表单，即使列表为空仍提供reload；读回失败结束pending但阻止新建，显式重试只读取。新增2项RED→GREEN，焦点31 PASS，全量GUI2119 PASS/12 KP-050 FAIL/2 SKIP；受控真实daemon/HTTP/production Node/Chrome完成读取失败→重试→新建→保存但reload失败→刷新页面→恢复→显式reload读回，begin=1/reload=1/cancel=0。证据 `/private/tmp/gui-onboard-recovery-e2e-dzyds2av/result.json`。独立增量4cfc1b29a已PASS；报告 /private/tmp/evidence-onboard-recovery-4cfc1b2-20261114/REPORT.md。
共享Key核对已转到hyprial-cli-developer（emergent明确该领域本线UNKNOWN），直连消息569a3ed6-964d-5ffa-83a1-590418bab63d。只询代码/接口，不读生产凭据、不复制权限体系。

路由最终回归：GUI 2117 PASS / 12 KP-050 FAIL / 2 SKIP；示例修复后 contract mock 两项通过，无新增 GUI 失败。扩大后端1796 PASS / 1 FAIL / 1 ERROR，唯一失败是 macOS 临时 socket 路径过长；短路径重跑该文件18 PASS，保留原记录。受控浏览器最终证据 `/private/tmp/gui-platform-routes-e2e-elkela6a/result.json`。

共享 Key 咨询：cli-developer 已按 dev@46a29ba48 实读确认生产 resolver、owner 身份映射、provider 写入准入及 U2 签发/绑定/发布/撤销均未接通（回复8e39b1ad-2e05-5061-89b4-29fbf2c9495e）。复用 OwnerSpaceResolver/OrgFsOwnerStore、S3 owner-home、OwnerKeyPort/Signer 与统一准入，不另建 ACL、不把本地 unbound key 当可信签名。其 F-9/legacy 建议引用旧 dev；当前分支冻结册已记录2026-11-10提前HTTP暴露且不做legacy兼容，已回告精确HEAD要求补核，不能按旧意见回退。T10仍在计划内，正式依赖未满足前不能声明可用。

路由固定提交 `0f4f55de214b9c439bdebb558e2315659d47eb34` 已推送普通分支与PR2727；独立审核图 `wf-ebe1c62aeb0644e19344e9be129805ca` 已接受。固定SHA受控浏览器 `/private/tmp/gui-platform-routes-e2e-6gpswan3/result.json`，干净Ruff通过；全量Python独立快照串行排在0d6之后。远端0f4的GUI push成功/PR失败，hygiene及Python检查失败；已发现PR身份行位置错误并改回首行，尚未取得各job日志，不将其余失败归为环境。

路由0f4f55de2独立审核已PASS：后端13/扩展2663、CLI11、探针11+3、GUI2117 PASS/12 KP-050 FAIL/2 SKIP及受控真实浏览器通过；非阻断问题继续跟踪：刷新后加载提示、已有路由修改默认、损坏配置/非文件DB诊断。确定性写错误与逐方法按钮能力投影已修（3项RED→GREEN，焦点17 PASS，全量GUI2124 PASS/12 KP-050 FAIL/2 SKIP）；无写重放。平台恢复4cfc增量已获独立PASS，图wf-3d19b644d3a0474eb7b7deb470ad517e。

全量测试进度：6309快照27285 PASS/47 FAIL；0d6与0f4全量因新增onboard测试文件与已有文件重名而收集失败，已改唯一文件名、两文件29 PASS。本轮新增的错误码引用、resident登记及shutdown测试替身已修；聚焦48 PASS/2既有FAIL，固定修后快照需重跑全量。不能仅凭失败nodeid相同归类为基线；本次按具体offender区分，未更改检查器或扩张豁免。

修后完整收集27422项通过，先前收集冲突已消除。后端144项回归通过（CLI单测需按原完整组合导入hyprial.cli），路由确定性失败的受控真实浏览器通过：重复同名保存明确ROUTE_EXISTS、按钮可编辑，不自动重发，2次显式add/1次remove/errors=[]。证据 /private/tmp/gui-route-recovery-e2e-6daowsoz/result.json。

- T12f 工作目录参数与共享启动弹窗已接：显式已有绝对目录覆盖旧启动 intent；无效/已运行不改记录，后续启动失败保留已保存选择；不迁移 home/凭据，不新增方法。受控真实 HTTP/浏览器确认拒绝和成功读回，实际 harness 启动尚未验收；第三方与全量待完成。
- 回归修复1e4979f955已获独立限定 PASS，审核图 wf-24857d8ad18a43e7973a6e659a1ed05c；路由错误与逐动作能力、受控真实浏览器、strict resident inventory/shutdown 修复通过。全量Python仍运行，真实平台未验收。报告 `/private/tmp/evidence-platform-regression-1e4979f-20261114/REPORT.md`；P3 列表刷新能力在后续增量补齐，inbox gate 的 tuple/for provenance 缺口不在当前实际调用路径，未修改或放宽检查器。

T12f固定0e1b0b14e已获独立限定 PASS（wf-2948f92daf1b4052832de10250e6f9db）：agent contracts320/inventory139/真实Chrome HTTP132、GUI62/全量2125 PASS（另12KP-050 FAIL/2SKIP）、受控浏览器均通过。完整Python排队，真实模型启动仍待验。报告 `/private/tmp/evidence-agent-cwd-0e1b0b14-20261114/REPORT.md`。P3文案b0442e70f已独立限定PASS：已派发的INVALID_ARGUMENT只显示参数校验失败，只有not-dispatched才写未发送；2项RED→GREEN、焦点82、独立矩阵4项及build/parity/CI策略通过；全量GUI2139 PASS/12既有KP-050 FAIL/2 SKIP。报告 `/private/tmp/evidence-agent-refusal-copy-b0442e7-20261114/REPORT.md`，浏览器/厂商未重新验收。

T11 身份绑定查询已接已有 identity.resolve：平台身份表仅用户行可显式查询，按 unionId 或 adapter/openId；不会查 App 创建者、不自动逐行查询。预览没有显式绑定数据时返回未绑定，不从观测行编造账号。已确认成员、仅出站访客和未绑定经隔离真实 HTTP/浏览器验证，3次查询/errors=[]；独立审核待完成。路由刷新按钮已补 list MethodAccess 拒绝投影。原始证据 `/private/tmp/gui-platform-binding-e2e-mb3zvkh_/result.json`。

T18/T12e 删除补充核查已由 cli-developer 确认：须补正式 preview/execute 配对和 HTTP profile；平台删除还缺独立于 start 超时的 actor removal reservation/settlement。RPC 删除名沿用既有名，平台另补 `management.adapter.remove.preview`（既有设计已列，原16项清单遗漏），不能绕过确认或只先stop再删。具体链路见对账，尚未实现。

- T18/T12e：HTTP 配对确认基础旧1c2cea2独立限定 PASS，新基线b7cb8aa92已重新跑393项HTTP/lifecycle；124条产品路由未扩大，业务删除尚未开放。平台 removal reservation/settlement 内部基础78fbb5ef6独立限定 PASS；补修复审发现的完成回执无法进入邮箱后无法恢复的问题。领域版本认领、pin/删除共享准入、Agent 销毁确认与调用方迁移仍待完成，不以基础通道通过代替删除验收。
- T11 查询绑定固定 3faf1c6e6 已推送分支及 PR2727，审核图 wf-30e2981ae0914911b4c264e3bd7f1c39；固定 SHA 浏览器证据 /private/tmp/gui-platform-binding-e2e-pfqn0tyt/result.json。

- T11 查询绑定返工：3faf1c6独立FAIL（旧sync跨来源覆盖），c78bf582a修复、4项RED→GREEN/47焦点PASS，复审wf-c8e64744beaa4aa7ab9ad1c228ec4add；全量GUI2137 PASS/12既有KP-050 FAIL/2SKIP。
- 基线跟进：远端dev64551e355包含CLI清单/架构门修复与teleport增量；合并预检仅known-problems文档冲突。固定当前增量后更新基线并复验，不将既有CI失败无限归为环境。

- 已rebase dev64551e355，daemon请求架构门通过；产品GUI/HTTP无冲突。provider-key mock缺口编号改KP058（避免覆盖上游teleport KP047）。新基线累计回归与全量正在安排，原始失败/报告保留。

- 身份查询/同步返工 c78bf582a 已独立限定 PASS（新基线对应 b90ac201e）；47焦点、全量GUI2137 PASS/12既有KP-050 FAIL/2SKIP，真实受控主流程通过。延迟竞态由独立jsdom探针覆盖；父级通知/reload来源栅栏为P3待修，不能称浏览器级竞态验收。报告 `/private/tmp/evidence-platform-binding-c78bf58-20261114/REPORT.md`。
- 新基线累计 b7cb8aa92 已建干净Python3.12/npm ci隔离环境，全仓Ruff通过；完整Python排在正在运行的1e4979后。0e1b0b14旧排队未启动即取消，被新累计头取代，不记PASS。

- 2026-11-14 用户确认：linyilun 的 Rust 核心即将迁入 dev，期间远端 CI 可能全红，暂不追修迁移故障。继续本地 CI 与本分支回归；只修本次引入/直接相关的问题，独立记录上游、环境和迁移失败，不据此声明全量或发布通过。

- T18 管理事务接入 pin 预约：在线事务使用 actor 原子快照，提交/回滚均携带 token；补偿或释放失败保留预约，显式结算前禁止后续删除。10 项新增用例覆盖真实 Agent port、丢回执与失败补偿，管理层 19、CLI 删除 27、扩大 217 PASS；正式 preview/execute、运行时与文件恢复接线仍待完成。本增量待独立审核，未声明产品删除可用。
- cfd6c159 pin 基础独立限定 PASS，但补偿 create/revive 可重建 reserved pin 的遗漏必须修复。现已在 Agent mailbox 内检查持久补偿 payload；home/非 home 两项 RED→GREEN，释放预约后可继续原补偿，不提前写回执。连同管理层33 PASS，扩大回归与第三方复核继续。报告 `/private/tmp/evidence-pin-reservation-cfd6c15-20261114/REPORT.md`。
- 删除事务与补偿修复累计 `554260069` 已推送普通分支/PR2727，独立复审 `wf-ea8ba33931cc47829e408b81b2ecd3e3` 正在进行。扩大1260 PASS/3 SKIP（最后读取失败保护前），最终焦点34 PASS；两项rebase相关架构回归38 PASS。新dev a191e7b6b仅新增两份CI的原生引擎构建步骤，暂未改变审核固定HEAD，不影响方法清单。

- ed7f29完成回执恢复已独立限定PASS（27项探针、4项父版RED→GREEN、Lark805 PASS），报告 `/private/tmp/evidence-removal-recovery-ed7f29b-20261114/REPORT.md`。完整Python按固定SHA串行运行，不记全量PASS。
- 删除/pin共享准入：已加入Agent authority的有界预约，原子取得pin快照；普通绑定/解绑与Agent销毁、迁移互斥，补偿携带token并执行strict CAS，内部重启不丢预约。P2迁移的pin写入改经同一authority。领域文件事务尚未持有此预约，产品删除/HTTP/CLI仍未开放；本增量测试及独立审核完成后继续接入。Key正式依赖仍BLOCKED，全部旅程完成后统一手测。
- 本增量验证：4项初始RED；最终pin/P2焦点18 PASS，隔离环境扩大919 PASS/3 SKIP，静态门禁通过。跳过不作为通过，完整Python及独立审核另记；没有GUI变化，不声称浏览器或真实平台删除验收。

- 累计失败归属：固定 b7cb 的35项失败中，31项在干净 dev64551同nodeid复现；4项本分支回归已逐项修复。最后两项由resident清单的字典展开导致严格AST检查失败，已恢复1181条完全相同且顺序不变的字面量，测试辅助函数改从其实现模块导入；不放宽checker、不改历史分类。两项RED→GREEN，相关CI/绑定测试通过；inventory/login另跑28 PASS/4既有FAIL，与dev对照一致。原始证据 `/tmp/gui-literal-inventory-*.log`、`/tmp/gui-dev64551-failures-attribution.log`。完整累计Python仍待最终快照运行。
- T18恢复缺口经cli-developer按554260069实读确认：现有Lifecycle journal没有adapter domain，PreviewRegistry仅在daemon epoch内有效，配置删除只有进程内补偿。后续在既有共享StateDatabase内记录删除意图和恢复阶段，复用Agent pin与Lark运行时预约，不建第二份数据库、不将旧token跨daemon复用；正式HTTP删除接线前必须验证中断恢复。咨询只作设计证据，不计验收通过。
- 554260069三项增量独立限定PASS：management32探针、补偿准入30探针、identity871 PASS/3 SKIP、P2/CLI443 PASS；报告 `/private/tmp/evidence-pin-transaction-5542600-20261114/REPORT.md`。新发现本线cfd导出使identity/interface.py达到902行，违反已有900行测试预算；按现有facade/types拆分迁移3项类型声明并补types公开清单，运行时导出保持原样。预算测试RED→GREEN，不更改门禁或预算。产品删除、跨daemon恢复仍未验收。
- T18运行时typed port已接到既有actor：版本/代际过期在stop前拒绝，停止未知不解锁、不重发，匹配token才可提交或中止。修复stop完成回执早于投影发布的问题。初始7 RED；新增9 PASS，扩大160 PASS、Lark814 PASS；结构/模块/方法/契约/daemon架构/术语/Windows通过。原始证据 `/tmp/gui-removal-port-*.log`；未调用真实平台、未开放HTTP删除，完整累计Python和第三方复核另记。
- c9226c144及前置两项修复已推送普通分支/PR2727，独立复审图 `wf-61e5299ca8854653a1a9376d12f9f0fc` 已受理。057a61ffe字面量恢复另获独立PASS：1181条值和顺序SHA相同，父2 RED→新2 PASS，相关套件只减少这两项失败。
- T18持久意图库已实现：复用共享StateDatabase，完整配置/registration/pin/实体身份原像、仅凭据摘要、单目标原子占用、提交后禁止转补偿、未结算不淘汰、终态保留；不执行外部效果。新增测试先因缺模块失败，当前17 PASS（含两个独立进程读取），管理相关此前34 PASS，三个既有架构/清单门通过。原始证据 `/tmp/gui-removal-journal-*.log`。**下一步仍是恢复补偿入口、文件事务组合、启动前结算和正式HTTP删除接线**，不能把存储测试计作业务恢复验收。
- c9226c144运行时接点已独立限定PASS：9项产品测试、30项独立探针；Lark813 PASS/1父版同样存在的时序不稳定项。报告 `/private/tmp/evidence-lark-port-c9226c1-20261114/REPORT.md`。812a59ef0持久意图库独立审核图 `wf-acb55c32e7a44a2587c8366b3f093826` 已受理。
- T18恢复补偿入口：新租约绑定journal原像及Agent entity token，恢复前核对当前pin，拒绝替换其他Agent或恢复同名新身份；恢复只允许补偿，未结算不释放。初始10项RED，迁移反序冲突另有RED；隔离Agent/管理回归920 PASS/3 SKIP（skip不作验收）。真实Harness port探针另发现既有持久化错误元数据被遗漏，现复用已有异常投影，避免补偿失败被当成成功。新增恢复与真实Harness持久化故障用例已通过，最终焦点48 PASS；尚未组合到正式删除入口。证据 `/tmp/gui-removal-preimage-*.log`。
