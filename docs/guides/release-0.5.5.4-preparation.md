# 0.5.5.4 发布准备与 dev-first 收敛

2026-11-08，仓库所有者批准：完成的改动先进入 dev，从经过验证的固定提交发布；先收敛代码，再集中处理 CI。本轮只做 PR 和代码清理，不执行版本发布，internal、nightly、stable 保持原位。本记录不构成发布验收通过，也不授权自动前移 stable。

## 历史与范围

收敛前的源仓基准：

| 引用 | 提交 |
| --- | --- |
| dev / internal | `61948416cab933002d12b70f84922f4c41baa058` |
| release/v0.5.5.4 | `88a65162212ed79dad67e1adf9d2ded9843927cc` |
| main / stable / v0.5.5.3.4 | `c3a18570101bc7c38337d1c8de4c93e43ad0381f` |

release 有 1,441 个 dev 尚未包含的提交；dev 有 5 个 release 尚未包含的提交。计数包括合并和文档。本次采用普通 merge 保留完整父链，不以 reset 或挑选补丁代替历史收敛。main 与旧 release/v0.5.5.3.4 的产品树相同，但其合并提交必须纳入 dev，才能满足 stable 推进的祖先条件。

第一批包括 #2427、#2467、#2513、#2572、#2514、#2528。#2528 的运行逻辑此前已进入 release，本次保留其剩余契约说明与完整提交历史。cell 包含已完成的 Phase A 和前半段 Phase B；actor 包含已完成的 P2a/P2b，未把后续 WIP 整条混入。

#2551、#2541、#2147 保留 Draft 并改以 dev 为目标；后续新增的 GUI 深模块 #2578 也保持 Draft、目标为 dev。维护者随后明确追加 #2383、#2268、#2148、#2063：设计、checker、ACL 和多 IM 源码也纳入本轮，保留现有未启用能力的开关与验收边界。#2523、#2333、#2195 经契约对齐与独立复核后已合入。#2214 按维护者确认的简化方案重写：保留 `org work ls/show`，统一走普通 OrgFS，移除专用写入、历史验证及审批账本依赖；不采用原 PR 的验证图方案。#2449 的旧实现会弱化现有 running/unknown graph 围栏，按被后续修复替代收口。

## 保全与冲突处理

- 保留完整 release 功能：GUI live/onboarding、v13、真实 owner principal、UUID P2–P4、PAC schema 20 与 held 提醒、routine pause/权威回收、平台创建时间，以及已合入的 0.5.5.3.4 修复。
- 保留 dev #2539 隔离 org 创建和 #2571 发布链修复。org 创建同时保留隔离模式不访问 sidecar，以及联网幂等重试确认发布状态的语义。
- 修正两线组合暴露的 org.create 严格 schema：声明 tailnetInitialized，并允许隔离模式的 null 发布字段；保留对应真实隔离 daemon 回归。
- 保留当前 owner-user GUI 身份规则，修正 cell threads 方法的旧 operator 模式；浏览器不能自行声称可信 from 身份。
- 生成的 OpenAPI、CLI 清单与 GUI 客户端从最终声明再生成，不能在冲突时只选某一分支的生成文件。
- 不带回 release-first source-branch 限制，保留 registry/结构/声明检查；actor CI-I 跟随拆分后的 gate-pr-hygiene。

## 发布控制

#2574 冻结 internal 的 tag writer 和 GitHub/wheel dispatch，普通 Python 检查继续运行。维护者明确本轮仅做 PR 和代码清理、不发布版本后，nightly 的 tag writer 与发布派发也保持关闭，避免定时任务在清理期间推进轨道。独立 installer 发布流程保留原状，本次未通过修改 installer 抢先变更公网安装入口。

main push 不再触发正式发版。release-tag 接受固定 40 位 SHA，并验证该提交在 dev 历史内，继续检查 VERSION、版本说明和不可变 tag。正式发布开关保持关闭；dry-run 可验证输入，但不能替代候选测试或真实发布回读。

`v0.5.5.4` 是不可变版本锚点；`internal`、`nightly`、`stable` 是受祖先与并发保护的移动轨道。正式发布前必须单独解除 hold，不能仅凭 PR 合并或部分 CI 绿灯宣称可发布。

## 本轮收敛结果

第一批聚合 #2575 已进入 dev，Forgejo 已将 #2427、#2467、#2513、#2572、#2514、#2528、#2383 标记为 merged，包括两条原标注“基线快照·勿合”的集成线。第二批以同样的普通 merge 保留 #2268、#2148、#2063 的源提交，identity 公共面采用双方并集，384 个导出无删除、重映射或重复。

- #2268：20 项焦点测试通过；所有 110 个注册方法仍先走 registry，旧分支排除条件不改变既有授权顺序。整体 request-contract 门仍有 IPC import ratchet 债务（KP-028）；与 multi-IM 组合后还把外部 SDK 名称误判为本地 IPC（KP-029）。组合 CI 焦点为 242 passed / 1 failed，按本轮先合代码、后整理 CI 的决定保留真实 FAIL。
- #2148：1,090 项焦点测试、2 项安装边界检查通过。凭据与 ACL 组件保持准备状态；远端认证与完整授权生命周期、daemon 接入及多设备实测仍未交付。
- #2063：平台源码测试 360 项通过（1 项缺外部 Tryx 制品）；现有 Lark/desired-state 865 项通过，历史回滚显式检查 30 项通过。运行默认仍只启用 Lark；其他平台激活与实机验收另行开展。
- #2523：真实成员查询与 nullable 创建结果已合入；缺少 Agent 关系数据时保留“状态未确认”。
- #2333：修复 personal-org owner 检查，并保留当前 expected-profile 条件写入契约、HTTP 声明和 cell 寻址后合入。
- #2195：完成 provider 安全与授权语义复核后合入；上述三项合入后的 dev 为 `88df3a586383cc74c837505f982bedaaf76e62cd`，不代表发布候选已经验收。
- #2214：普通 OrgFS 简化与验证范围见 [本轮证据](../notes/work-orgfs-2026-11-08/README.md)。既有 work 文档、通用 OrgFS 历史及恢复能力保留；专用审批账本不再参与读取，也不自动删除旧文件。

这些计数对应各条证据记录的具体提交与测试范围，不相加充当最终候选的全量验收。cell 的完整 live HTTP 测试、五平台 native 发布验证仍未完成。本轮清理远端分支以“源 head 是实际 dev 的祖先”为必要条件，保留 main/dev、保护规则命中的分支，以及所有未关闭 PR 的 head/base；删除使用精确旧 SHA lease 并保存恢复映射。第一批已完成 841 条删除，恢复映射见 [分支清理记录](../evidence/dev-first-branch-cleanup-2026-11-08.json)；所有提交仍可从 dev 历史到达。

## 发布前必须闭合

1. 固定最终 dev 候选 SHA，完成该 SHA 的 Python、Go、GUI、契约及所需 E2E；保留 FAIL、BLOCKED、INCOMPLETE 的真实结果。
2. 完成 [native wheel 发布方案](../design/native-wheel-publication-0554.md) 与 KP-027：当前 host-only native 构建和五平台单机流程冲突，ABI wheel 格式还需要验证、清单、附件及安装入口全链路迁移。单平台 conformance 不代表五平台 wheel 验收。
3. 复核 UUID P4、PAC schema 20、迁移中断/重试、真实 GUI owner-user/登录、消息和 Lark 等本次触及的链路。
4. 整理真实发布日期与最终版本说明；发布时核对 GitHub assets、Forgejo registry、匿名 R2 五平台 wheel 和 SHA256SUMS，对应同一源 SHA。
5. 最后确认 internal 解冻和 stable 推进时机；迁移时临时增加的 dev 合并白名单必须恢复原值，其他保护设置保持不变。

## 升级与恢复

UUID P4 和 PAC schema 20 是单向升级，旧版本不能直接使用迁移后的状态。现有 daemon upgrade/自动升级不会在 P4 之前自动备份。升级前停止写入者并备份完整状态；降级只能还原升级前备份，并关闭还原配置里的 autoUpgrade，避免立即再次迁移。完整文件范围与恢复步骤保留在 CHANGELOG 的 Upgrade notes 中。

本轮测试只使用一次性 home/socket/状态目录，未操作生产 daemon 或生产凭据。验收明细按实际测试提交追加到关联 PR 与证据记录；未冻结的候选不写成最终发布 SHA。
