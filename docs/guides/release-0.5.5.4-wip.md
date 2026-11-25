# 0.5.5.4 发布 WIP 与候选验收记录

状态：**WIP / NO-GO**。2026-11-08 建立，现已固定待验收候选，尚未批准发布；表中的未完成项不构成验收通过。
本记录依据[发布规则](release-rules.md)、[发布检查模板](release-checklist-template.md)和
[已有准备记录](release-0.5.5.4-preparation.md)，用于归集后续工作的证据和发布决定。

`release/v0.5.5.4` 是目标为 `dev` 的短期发布准备分支。产品、CI、E2E 修复仍先通过各自
PR 合入 `dev`，再选择其中的固定提交验收；准备分支不恢复为 release-first 集成入口。

## 候选记录

| 项目 | 当前值 |
| --- | --- |
| 计划版本 / 正式 tag | `0.5.5.4` / `v0.5.5.4` |
| 发布类别 | final，尚未批准 |
| 最终候选完整 SHA | `b4b0c1fe28fa4f5331de998ac7cfbc17cb922319` |
| 范围盘点基准 dev | `284dcf72942b1b1b2495c9bbddc1e646cfc386a3` |
| 上一支持版本 / 源仓提交 | `v0.5.5.3.4` / `c3a18570101bc7c38337d1c8de4c93e43ad0381f` |
| 正式发布日期 / 维护窗口 | TBD |
| 发布负责人 / canary / 恢复负责人 | TBD |
| 完整候选证据索引 | TBD；各 PR 的焦点证据不能相加替代候选验收 |
| 升级前完整备份及验证恢复点 | TBD |

范围基准只是此 WIP 建立时的 dev，不是已经通过测试的最终候选。E2E 修复与其他必要改动
合入后，填写新的完整候选 SHA，所有验收记录绑定该 SHA。dev 后续前进不会自动更换候选。

## 相对上一版本的范围

以源仓 Git 对象界定范围；当前基准保留了上一版本的祖先历史。最终候选确定后，重新运行：

```sh
git merge-base --is-ancestor c3a18570101bc7c38337d1c8de4c93e43ad0381f <candidate-sha>
git log --oneline c3a18570101bc7c38337d1c8de4c93e43ad0381f..<candidate-sha>
git diff --stat c3a18570101bc7c38337d1c8de4c93e43ad0381f <candidate-sha>
```

当前已收敛的主要内容见 [CHANGELOG](../../CHANGELOG.md) 的 0.5.5.4 节：

- GUI live/onboarding、v13、owner-user 身份及组织成员页。
- UUID P2–P4 寻址、PAC schema 20、routine 与消息恢复相关修复。
- 组织 membership/current-org/receiver、普通 OrgFS work 文档读取与编辑流程。
- cell 已完成切片、Rust actor 引擎和已完成的 Python 端口；其余工作仍在各自 WIP。
- delivery routing 设计、single-route checker、Zenoh ACL 与 multi-IM 源码；源码进入 dev
  不代表远端授权生命周期、多设备或其他平台激活已验收，运行默认仍只启用 Lark。
- provider key 本地核心；owner-keys 的 OrgFS 写入准入等边界见 KP-030–033。
- dev-first 发布入口、不可变版本锚点、轨道保护与发布链修复。

未合入的 GUI 深模块、cell Phase B、actor 后续端口和 agent teleport 不因本 WIP 而进入发布范围。
最终版本说明必须从实际 Git 差异复核，并记录仍关闭的能力和不兼容边界。

## 候选验收矩阵

每项证据记录完整 SHA、命令、UTC 时间、平台、工具版本、run/job ID、结果与清理状态。
`FAIL`、`BLOCKED`、`NOT_RUN`、缺失记录和跳过均保留；没有全绿结论时维持 NO-GO。

| 范围 | 完成条件 | 当前状态 |
| --- | --- | --- |
| 版本与范围 | 候选的 VERSION、CHANGELOG、逐版本说明、源仓祖先关系一致 | 候选 SHA 已记录，版本与范围 NOT_RUN |
| Python | 锁定依赖、完整 `tests/`、静态门、真实 CI 同一 SHA | NOT_RUN；KP-028/037 等存量问题待整理 |
| Go / Tailcat | 对应固定源码的 Go 测试、传输回归与所需 race 检查 | 最终候选 NOT_RUN |
| GUI | 构建、合同、真实 daemon + 浏览器的 owner/onboarding/组织文件流程 | NOT_RUN；KP-034/035 保留真实失败/阻塞 |
| 契约 | hermetic corpus、场景 inventory、CLI/OpenAPI/客户端一致 | 最终候选 NOT_RUN；inventory 通过不等于场景执行 |
| 无凭据 E2E | 隔离套件逐场景运行并保留最终 RESULT；零测试进程/容器残留 | #2580 已合入 dev；最终候选 NOT_RUN |
| 受控真实 E2E | 专用测试账号/App 的双机、resident harness、Lark 及本次触及场景 | 最终候选 NOT_RUN；缺前置条件时 BLOCKED |
| Native wheel | 同一 SHA 的完整五平台产品 wheel，真实安装、ABI/架构/摘要一致 | BLOCKED，KP-027 |
| 安装器 | POSIX/Windows 同时支持旧 `py3-none` 和新 `cp312-abi3`，拒绝混版/错摘要 | BLOCKED，待实现与实测 |
| 状态迁移 | UUID P4 / PAC schema 的中断、幂等、混合版本边界和恢复演练 | 最终候选 NOT_RUN |
| 分发与 canary | 三方产物回读、真实安装/升级、运行 epoch/版本与消息健康 | NOT_RUN，待发布决定 |

执行命令和真实环境前置条件以[检查模板](release-checklist-template.md)及
[`hyprial-e2e` 技能](../../.agents/skills/hyprial-e2e/SKILL.md)为准。真实账号与测试 App
必须按场景要求准入，不用生产 daemon 或生产 App 补齐缺失前置条件。
[ROADMAP G1](../../ROADMAP.md#g1--发版后必须跑一次含-lark-live-的-e2e) 的 Lark-live 义务
包含 E2E-004、E2E-011 和 E2E-007；仅跑 credentialed workflow 不代表已经完成 G1。

## 发布前必须闭合

- [ ] 完成 E2E 修复 WIP，将完成的修复合入 dev；固定完整候选并填写上面的矩阵。
- [ ] 按 [native wheel 方案](../design/native-wheel-publication-0554.md)确认实际 runner 宿主、
  工具链和执行容量，分别构建 darwin/arm64、darwin/amd64、linux/amd64、linux/arm64、
  windows/amd64；Linux 两架构提供 manylinux 下限证据，缺平台为 BLOCKED。
- [ ] 完成 ABI 命名消费链和安装器兼容；完整产品包同时包含 GUI、Tailcat 与 actor 扩展。
  发布不得使用跳过 native 或放宽 GLIBC 下限的开发选项。
- [ ] 收敛发布编排：先构建并验收同一组固定产物，再切换 stable。当前正式路径在推进
  轨道之后派发 wheel 构建，顺序与部分完成的恢复策略还需实施和复核；保留单一 writer、
  祖先条件、lease、串行发布与显式 dispatch/启动回读。
- [ ] 根据 [Upgrade notes](../../CHANGELOG.md#upgrade-notes)完成升级前完整备份和恢复演练。
  UUID P4 与 PAC schema 20 是单向迁移；自动升级不会在 P4 前自动备份。恢复旧版本必须
  使用升级前备份，并关闭还原配置中的 autoUpgrade，不能直接让旧版本读迁移后状态。
- [ ] 明确 canary、维护窗口、owner 通知、停止条件与恢复负责人；准备发布后的 G1 验证。
- [ ] 补齐实际发布日期、最终范围与逐版本说明；明确每个剩余风险的发布决定。
- [ ] 发布负责人审核精确候选证据后，单独决定解冻及发布动作。

## 发布控制与完成判据

当前 `INTERNAL_PUBLICATION_ENABLED`、`NIGHTLY_PUBLICATION_ENABLED`、
`FORMAL_PUBLICATION_ENABLED` 均保持字符串 `"false"`。本 WIP 不修改这些值，也不触发
tag、轨道、installer、registry、R2 或 GitHub 发布。创建 WIP 不产生合并或发版批准。

满足上面的条件后，按发布规则对固定 SHA 先执行 `release-tag.yml` dry-run，再经发布决定
创建不可变 `v0.5.5.4`。dry-run 仅证明静态输入门，不证明测试、构建、安装或分发成功。
最终必须核对 stable 等于版本提交、nightly 为同一提交或其后代，以及 GitHub、Forgejo
registry、匿名 R2 的五平台 wheel 与 SHA256SUMS 完全一致；部分完成仍记 FAIL。

本 PR 的文档检查与未来候选验收分开记录。WIP 的通过条件是范围、阻断项和后续操作可审阅；
它没有全量测试通过或可发布声明。
