# 实现变更与 E2E 场景同步

> **2026-11-10 owner 决定：PR CI 彻底不再做 E2E 影响检测。** `gate-pr-hygiene` 的
> `Require a disposition for every affected E2E scenario` 步骤已删除，PR 模板也不再要求
> 正文声明——**任何 PR 都不需要写 `E2E-Impact` / `E2E-Unknown`**。
> 检测已落在 **E2E 定期执行（周期任务）**：`e2e-macos-isolated.yml` 与
> `e2e-macos-credentialed.yml` 各自在场景循环前跑 `--validate-only` 结构校验（`schedule`
> 触发时），PR 阶段不再有任何检查器调用。检查器、声明格式与覆盖表保留在树内，其中声明
> 格式仅供历史参考，**没有任何 CI 路径会读取它**。

检查器用于列出实现变更的影响范围与遗漏；实际场景是否通过，仍由固定源码 SHA 的独立执行
证据回答。检测（声明完整）不等于运行成功，也不解除发布 hold。

## 开发时先列范围

```sh
uv run --no-project --python 3.12 python scripts/ci/check_e2e_impact.py \
  --base origin/dev --head HEAD --plan-only
```

检查器读取准确的 base/head git 对象和 `merge-base..head` 完整差异；rename 的旧、新路径
均参与匹配。`affected` 逐项列出场景、触发路径和相应 runner/doc/helper 的变化；`unknown`
列出未映射的产品、协议、构建或 CI 路径。历史不完整、映射缺失、失效 pattern 或重复 ID
会明确报错，不能当作无影响。

`contract/e2e/scenarios.json` 的 `covers` 关联稳定 CLI、API、契约和实现目录。每个场景还
自动依赖自己的 runner/document，静态递归读取本地 import 和确实存在的 `.py` delegate，
并纳入 `covers` 显式列出的专属 helper。复杂动态加载或外部生成输入应显式补 `covers`；
检查器不会执行待测代码来猜依赖。pattern 对完整仓库相对路径做区分大小写的匹配，`*`
可匹配子目录；目录使用 `目录/**`。每个 pattern 必须在相应快照中命中真实 tracked 文件。

覆盖采用 base/head 的并集；候选不能通过删除自己的映射逃避这次检查。修改、增加或移除
场景登记本身也会列出相应场景。新增产品/协议入口没有映射时必须处置 UNKNOWN，不能自动
得出 none。覆盖会逐步补齐，不声称当前映射已经穷尽所有实现依赖，也不要用 `src/**` 之类
全仓 pattern 把任何改动机械地映射给全部场景。

## 声明格式（已退役：PR 正文不再使用）

本节**已退役**：PR 正文的 `E2E-Impact` / `E2E-Unknown` 声明不再被任何 CI 读取，写与不写都不影响任何 PR。
周期任务已接入（两个 macOS E2E workflow 的 `schedule` 路径），当前运行的是**结构校验**
（runner/document 存在、`covers` 仍命中、依赖可解析），不是让作者逐场景写正文行；
下面的声明格式只作为历史记录保留，解析器仍留在树内但没有任何 CI 调用点。

```text
E2E-Impact: E2E-017 updated: 将 outbox 断言同步到 list 子命令，保留死信与重放断言
E2E-Impact: E2E-003 unchanged: reason=仅调整内部持久化索引，公开 pin URI、重启及恢复契约保持原样; validation=在候选固定 SHA 运行原 run-pin-restart-survival.py，记录独立结果
E2E-Impact: E2E-011 not-applicable: 只调整 macOS 工具准备，此受控开户流程不使用该工具; requires-review
E2E-Unknown: src/hyprial/new_protocol.py: 新入口尚无场景映射，需要 reviewer 确认并补覆盖计划; requires-review
```

- `updated` 需要同一 diff 真正修改对应 runner、document 或 helper；只改 `covers` 不够。
- `unchanged` 不强迫修改测试，但必须写具体契约判断 `reason=` 和原场景的验证记录或
  安排 `validation=`。单独的 PASS、自报绿灯或 URL 不能代替这些内容。记录应给出实际
  命令、固定 source SHA 和结果；计划应写清原场景的执行安排及尚未运行的范围。
- `not-applicable` 和每个 UNKNOWN 精确路径都必须给理由，且以 `; requires-review` 结束。
  检查只确认处置存在，将条目保留在 `review_required`；reviewer 仍须判断理由是否成立，
  可要求补映射或场景。它不会自动批准豁免。`review_required` 是待审报告，不是检查器自动
  产生的审批或失败；是否强制人工 approvals 由分支保护另行决定。本门只强制声明完整性，
  不宣称 `requires-review` 自身已经形成人工审批硬门。

本地验证正文：

```sh
uv run --no-project --python 3.12 python scripts/ci/check_e2e_impact.py \
  --base origin/dev --head HEAD --pr-body-file /tmp/hyprial-pr-body.txt
```

正常输出只有 `declaration-complete` / `execution=NOT_EVALUATED` 这类声明结论；绝不输出
场景 PASS。真实隔离/受控 E2E、模型凭据及 live Lark 测试仍遵循
[E2E skill](../../.agents/skills/hyprial-e2e/SKILL.md) 的权限与隔离要求；此门不会自动启动它们。

## 与 OpenAPI 行为差异统一

OpenAPI 行为差异只作为额外的影响触发源，复用这里的 `scenarios.json`、逐场景声明解析器和
既有 contract diff，不另建一份 registry 或检查器。纯 annotation 变化不触发；方法、字段、
错误、enum 和示例语义变化仍落到同一组 `updated` / `unchanged` / `not-applicable` 声明。
URI 语义、会话租约和 CLI 等不一定改变 OpenAPI 结构的漂移，继续由 `covers` 与 UNKNOWN 捕获。

不引入另一种全局 `E2E-Impact: not-needed` 语法。后续若增加简写，也只能在同一个解析器中
展开为逐场景 `unchanged`，并要求同等的理由与验证；`execution` 始终保持 `NOT_EVALUATED`。

## CI 落点与可信边界

`gate-pr-hygiene` 中的 E2E 检测步骤已删除，PR 阶段不再有任何检查器调用；同一 job 的
base-owned hygiene 脚本、merge-tree、身份与 registry-union 门保持原样。

检测的唯一落点是 **两个 E2E 定期执行 workflow**（`e2e-macos-isolated.yml` 与
`e2e-macos-credentialed.yml`）：`schedule` 触发时，在场景循环之前运行
`scripts/ci/check_e2e_impact.py --repo . --validate-only`，只做结构校验。

周期任务没有 PR 候选，因此**不区分 base/head**：检查器、`contract/e2e/scenarios.json`
与 workflow 都来自同一次 fetch 的默认分支树，步骤只读该树——不执行分支或 PR 代码，也不
读取 PR 正文。这也是为什么它不再需要「从 base 提取检查器」这种 PR 时代的可信边界。

`--validate-only` 校验的是映射假设本身：registry `schema` 与 `coverage_schema`、每个场景
的 runner/document 是否仍存在于该树、每个 `covers` pattern 是否仍命中真实 tracked 文件、
以及 runner 的本地 import/`.py` 字符串依赖是否仍可解析。它不计算 `changed`/`unknown`/
`affected`，也不执行任何场景：这三项在周期任务里恒为 `NOT_EVALUATED`。

失败语义：结构校验不成立时，它是**场景循环之前的红色步骤**，输出
`status=assumptions-invalid` 且 exit 1，并按 `diag/e2e-assumptions.json` 存档；由值班
开发者更新 registry/runner/doc 后重跑。默认分支树缺少检查器或覆盖 schema 时同样
exit 1 `assumptions-invalid`——不得静默通过，也不得为此修改分支保护。

这一边界只关乎映射假设是否仍成立，不证明任何场景已在候选上执行。
