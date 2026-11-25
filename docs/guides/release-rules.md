# 发布规则

分支、候选和轨道角色的机器可读规定见
[`.forgejo/git-flow.json`](../../.forgejo/git-flow.json)；CI planner 实际读取该配置，
本文定义对应的发布操作、验收和冻结规则。

Allen 于 2026-11-08 确认改为 dev-first：所有完成的修改先经 PR 进入 `dev`，
再从已验证的固定提交发布。本文取代 2026-10-28 的 main-push 发布入口及
2026-10-31 的 release-first 集成要求。旧发布记录保留，不作为继续旧流程的依据。

## 1. 单一集成主线与发布候选

- `dev` 接受完成的 feature、fix、docs、ci、hotfix 与 integration PR；WIP 保持 Draft，
  目标可以改为 dev，但 retarget 不等于已审查或可合并。
- `intg/*` 可以暂时承载有依赖的开发；不再要求先进入长期 `release/v*` 集成分支。
  现存 release/main 的独有功能、修复及祖先历史须在收敛时保留，不以覆盖分支代替合并。
- `release/v<版本>` 可以作为发布准备 WIP，目标仍为 dev；说明、升级安排和修复先进入
  dev，再选择候选。该分支不承接另一条功能集成流程，也不自动触发发布。
- 发布候选必须是受保护 dev 历史中的完整 40 位 SHA，可以落后于持续前进的 dev tip。
  证据绑定该 SHA；后续修改产生新候选，不能沿用旧 SHA 的通过结论。
- `v<VERSION>` 是不可变版本锚点，`stable` 是正式分发轨道；不新增名为 `release` 或
  `releases/v<VERSION>` 的发布 tag。源仓 main 不再触发正式发布；公网 GitHub main
  仍由发布器维护快照，两者不是同一提交历史。

## 2. 轨道与发布链

### internal

正常启用时，只有 dev push 的 `gate-python.yml::unit` 汇总门通过才能推进 internal。
候选必须在当前 dev 祖先链中，并且是原 internal 的后代；旧运行不得覆盖新指针。
使用 lease 原位移动，随后显式 dispatch `wheel-release.yml` 与 `github-publish.yml`。
PR、main、release push、手动测试 dispatch 均不推进 internal。

**0.5.5.4 收敛期间冻结 internal**：`gate-python.yml::unit` 的
`INTERNAL_PUBLICATION_ENABLED` 固定为字符串 `"false"`；移动和 dispatch 两步都只接受
精确 `"true"`。缺失值同样不发布。测试继续执行，安装脚本的独立发布路径保持原规则。
解除冻结必须是经过审查的单独修改，并以候选验收和升级准备为依据。
这不是仓库变量开关；发布分支不能仅因合并成功就宣称冻结已在线生效。

### nightly

`release-nightly-promote.yml` 沿用两份定时 E2E marker：isolated 与 credentialed
必须同 SHA、均通过且在四小时内。候选固定为 marker SHA，不重新解析移动中的
internal/dev。无新双绿时应用 stable 保底，已领先的 nightly 不回滚。

本轮只清理 PR 和代码，`release-nightly-promote.yml` 的
`NIGHTLY_PUBLICATION_ENABLED: "false"` 同时阻止 nightly writer 和发布派发；
定时验收标记仍可读取。解除这一临时冻结须单独审查，不随代码合入自动解除。

### 正式版本与 stable

`release-main-dispatch.yml` 已退为仅可手动查看的无写入诊断，不再监听 main push。
正式入口为 `release-tag.yml`，显式输入 `tag=v<VERSION>`、完整 `sha`、`dry_run`。
默认 dry-run；从 dev 获取工具和祖先边界，目标 VERSION 与 CHANGELOG 从候选对象读取。
创建不可变 tag 后，显式启动该 tag 的 `github-publish.yml`；正式发布成功路径是
stable 的唯一 writer，并应用 nightly stable 保底。不能另行手动移动 stable。

**本次仅准备发布，正式写入保持关闭**：`release-tag.yml` 的
`FORMAL_PUBLICATION_ENABLED` 为字符串 `"false"`。dry-run 可验证静态门；真实 tag
写入必须在候选验收、五平台构建和发布决定完成后单独解除此 hold。
当前静态门（tag 未存在、VERSION、dev 祖先、CHANGELOG）不自动证明测试通过；
PR edited 产生的轻量绿也不是完整候选证据，不用自报 PASS 字段代替真实记录。

```text
完成的 PR → dev → 精确 SHA 的集成验证
  ├─ internal（仅在解除冻结后）→ wheel / registry / R2 / GitHub
  └─ 固定发布候选 → 完整验证和发布决定 → release-tag(v<VERSION>, SHA)
       → github-publish 正式发布 → stable + nightly
       → wheel-release 与对应 GitHub track 发布
```

## 3. 不变量与分发一致性

- `v*` 不可变；已存在不能覆盖、删除重建或借重跑替换内容。
- stable 只接受现有 stable 的后代。首次切换 dev-first 时必须保留旧 main/stable
  的祖先历史；相同文件树或 cherry-pick 等价不能替代祖先关系。
- 每个轨道保留单一 writer，使用 lease 防并发覆盖；可移动 tag 绝不先删再建。
- wheel 构建使用显式 source SHA，并在对象/清单写入前复核远端轨道仍指向它。
  发布串行、`cancel-in-progress: false`；不取消自动发布，除非 Allen 明确批准。
- Actions token 推 tag 不触发下一条 workflow，因此显式 dispatch 及启动回读必须保留。
  HTTP 204 不代表任务启动，任务启动不代表完成。
- Forgejo registry、GitHub Release、R2 三方的五平台 wheel 和 SHA256SUMS 必须一致。
  正式 v* 为源码锚点，wheel 附件仍归对应 track Release；没有新建 v* wheel 通道。
  三方不是跨服务原子事务，部分完成是 FAIL，不能称为发布成功。
- `<VERSION>+g<source short7>` 标识构建来源，不用于比较提交新旧。
  公网快照与源仓 commit 不同，分别记录，不能直接比较两者 SHA。

## 4. 0.5.5.4 发布前的边界

冻结的候选必须完成 Python/Go/GUI/契约及相关真实 E2E 验证，不能用某个 PR 的绿
替代合并结果。Rust native engine 随候选进入后，五平台 wheel 需要各自可运行的
构建工具链和真实构建记录，不能假设 Tailcat 的交叉编译能力等于 native engine
也支持跨宿主构建；缺平台为 BLOCKED，不能编造 runner label 或省略平台。

UUID P4 寻址和 PAC schema 迁移为单向升级。`daemon upgrade`/自动升级不会在 P4
前自动备份；保留迁移后的数据直接运行旧版本不受支持。解除 internal 冻结之前就须
明确备份、验证过的恢复步骤和升级安排，不能等 stable 发布时才处理。
详见 0.5.5.4 CHANGELOG 的 Upgrade notes、发布检查模板及 ROADMAP 的真实 Lark 义务。

## 5. 操作员验收清单

每次记录版本、源仓完整 SHA、track、workflow run ID、UTC 时间和 PASS/FAIL/BLOCKED。
以下检查均为只读；未测项写 UNKNOWN/BLOCKED，不用 skip 代替结论。

1. 在源仓执行 `uv run python scripts/ci/check-release-tracks.py <VERSION>`：
   stable 等于 v* 所指提交，nightly 等于它或为其后代。另核对 internal 的 unit 证据。
2. GitHub `Hyprial/hyprial`：检查正式 v* Release 与对应 track Release、快照来源；
   对每个待验 track 下载五个 `.whl` 和 `SHA256SUMS`，验证文件名、版本和全部摘要。
   可用 `gh release view <track> --repo Hyprial/hyprial` 查看附件清单；查询成功不等于下载校验成功。
3. Forgejo registry：在 `https://code.hyprial.com/api/packages/HyprialOS/pypi`
   的 simple index / 包文件接口下载同一 `<VERSION>+g<sha7>` 的五个 wheel，
   与 GitHub 的 `SHA256SUMS` 逐项比较。凭据只走受控环境或 stdin，不写 URL/日志。
4. R2 公网：匿名下载 `https://dl.hyprial.com/wheel/<track>/SHA256SUMS`，
   再按清单请求 `/wheel/<track>/<wheel filename>`；**五个 wheel 均须 HTTP 200**，
   且五个摘要与 GitHub/Forgejo 完全相同。不要只检查 manifest 的 200。
5. 五平台闭集为 darwin/arm64、darwin/amd64、linux/amd64、linux/arm64、windows/amd64。
   缺一个、混入旧版本或摘要不同均为 FAIL。移动轨道期间先固定清单和来源 SHA，
   验收结束再确认指针未变化；变化则重新取样，不拼接两次发布的结果。

安装与修复细节见 [wheel 分发](wheel-distribution.md)；发布前的 E2E 义务见
[发布检查模板](release-checklist-template.md) 和仓库 `ROADMAP.md`。
