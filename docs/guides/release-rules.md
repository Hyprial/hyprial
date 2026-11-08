# 发布规则（唯一事实源）

Allen 于 2026-10-28 确认以下四条规则。从 0.5.5.2 起执行；工作流、操作手册
和发布验收均引用本文，不各自维护另一套规则。

以 `release/**` 为目标的 PR 与 dev PR 一样触发 `tests`/`gui`/`desktop-check` 的 PR CI（按 audited scope 选择套件），不因 base 是 release 线而跳过。

## 1. 四条规则与执行机制

> 1. internal track总是跟随dev,dev更新即全网更新

精确定义：仅在 **dev push 的 `unit` 汇总门通过**后，
`gate-python.yml::unit` 的 `Move the track tags to the unit-green dev head` 步把
`internal` 原位移到该次测试的完整 SHA。不接受 PR、main、release/* push 或
手动 dispatch；失败或取消不移动。旧运行不得覆盖已前进的指针。
随后的 dispatch 步分别启动 `wheel-release.yml` 和 `github-publish.yml`。
“全网更新”指发布到所有分发面，不绕过客户端的自动升级开关或升级周期。

> 2. nightly track 总是指向最后一次通过E2E的internal tag

`release-nightly-promote.yml::promote` 读取 `ci/gate-status` 上两份 marker：
`e2e-macos-isolated.yml` 与 `e2e-macos-credentialed.yml` 必须在定时运行中全绿，SHA 相同且均在
4 小时内。候选固定为 marker SHA，不重新解析已经移动的 internal/dev。
单边绿、不同 SHA、过期或缺失 marker 不产生候选。
保留 10-26 的 **stable 保底**：没有新双绿时 nightly 跟随 stable；若 nightly
已经领先则保留，不回滚。正式发布与 nightly 晋升共用
`scripts/ci/release_tracks.py::Tracks.nightly`，移动后显式派发两条发布流水线。

> 3. dev 合入main的时候,更新stable track,tag打上最新的version号(这和当前的流程不一样,本次豁免,但从0.5.5.2 开始执行。后续当release/v* 合入dev的时候,不更新stable)

独立 `release-main-dispatch.yml` 只监听 main push，从 **pushed SHA** 读取 `VERSION`。
若 `v<VERSION>` 已存在，记录 no-op；否则显式 dispatch `release-tag.yml`，
输入为 `tag=v<VERSION>`、`sha=<pushed SHA 的完整 40 位>`、`dry_run=false`。
不依据 dev 的当前版本猜测，也不在 dev 或 release/* push 上运行。
`release-tag.yml` 保留独立锚点职责和目标提交的 CHANGELOG 门；祖先门只接受
main 历史（支持 dev→main 的 merge commit，拒绝 dev-only 提交），创建不可变 tag
后显式启动 `github-publish.yml`。后者的 `Advance and verify release tracks`
步仅在真实 v* 发布通过后调用 `release_tracks.py`，推进 stable 并应用 nightly 保底。
**release/v* 合入 dev 只走 internal 链，绝不更新 stable。**

> 4. forgejo\github\R2 三方的release产物总是保持一致

`wheel-release.yml` 在固定 source SHA 构建五平台 wheel，验证后直接写 R2 并做
匿名回读，再上传 Forgejo PyPI registry。`github-publish.yml` 的
`Attach wheels to the public release` 步等待该精确版本的五个 wheel，附加到对应
track 的 GitHub Release，并写 `SHA256SUMS`。
`ATTACH_WHEEL_ASSETS` 和 `MIRROR_WHEELS_TO_R2` **不是可选开关**；三轨均必发。
缺凭据、缺 wheel、校验或派发失败必须使流水线失败，不能静默跳过后报成功。
三方非跨服务原子事务；部分完成是 FAIL，必须修复并重新验收，不称为发布完成。
正式 v* 是源码发布锚点；五平台 wheel 附件归对应 track Release，不另建 v* wheel 线。

## 2. 完整发布链

```text
dev push → unit green → internal tag
  ├─ wheel-release(internal) → R2 + Forgejo registry
  └─ github-publish(internal) → GitHub mirror + 精确 wheel 附件

main push（VERSION 尚无 v<VERSION> 锚点）→ main-release → release-tag
  → v<VERSION> → github-publish 正式发布 → stable + nightly 原位更新
  ├─ wheel-release(stable, nightly) → R2 + Forgejo registry
  └─ github-publish(stable, nightly) → GitHub Release + wheel 附件
```

只有实际移动的轨道才派发新 wheel 构建；已有且领先的 nightly 不重建。
Actions token 推 tag 不触发后续 workflow，所以各写入方必须显式 dispatch。
派发被接受不等于作业启动，更不等于三方发布完成。

## 3. 不变量与红线

- `v*` 不可变；已存在不能覆盖、删除重建或借重跑替换内容。
- 每个 tag 只有一个写入权威：internal 为 `gate-python.yml::unit`；stable 为
  `github-publish.yml` 的正式发布步；nightly 为共享 `Tracks.nightly` writer。
- 可移动 tag 原位更新，绝不先删再建；使用 lease 防并发覆盖。
- stable 只接受当前 stable 的后代（或首次创建）；旧候选不能回滚轨道。
- 不取消自动触发的发布，除非 **Allen 明确批准**；发布并发队列不能启用
  `cancel-in-progress`。半途取消可能留下已上传但未完成的产物。
- 版本戳 `<VERSION>+g<source short7>` 标识来源，不用于比较提交新旧。
  公网快照 commit 与源仓 commit 不同，验收须分别记录，不能直接比较两者 SHA。

## 4. 过渡记录

0.5.5.1 通过人工 `release-tag` dispatch 发布，属于 Allen 对规则 3 的本次豁免。
从 **0.5.5.2** 开始使用 main push 自动锚定；release/v* 合入 dev 不再触发 stable。
10-26 原话及当时补齐记录保留在 [升级轨道历史](upgrade-tracks.md)，不能当作
继续手动打 tag 或绕过 main 的授权。本文记录规则，不声称已完成线上发布验收。

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
