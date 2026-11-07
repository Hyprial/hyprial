调研：git-ai（逐行 AI 归属）  2026-10-21

状态：**调研交付，未试装、未采纳**。本文不是「已评估可用」的声明。任何关于它在
我们仓库里能否工作的判断，在隔离环境跑完第三节的三个试验之前一律按 UNKNOWN 处理。

- 上游：`https://github.com/git-ai-project/git-ai`
- 读的版本：本地 clone v1.7.6（`Cargo.toml` `name = "git-ai"`, `version = "1.7.6"`）
- 读数时刻：epoch 1792633056–1792633075
- 另有两个同名项目 `pivotdude/git-ai`（生成提交信息）与 `recrsn/git-ai`，不是同一个东西；引用时认准 `git-ai-project/git-ai`

## 一、它是什么

按行记录「这段代码是谁写的」：哪几行是 AI 写的、哪个 agent、哪个模型、哪一次 prompt。
不是「AI 参与度」这类统计，而是逐行归属，并且归属随提交进入仓库。

```
git commit        提交时输出一行 you / ai 占比
git ai blame      逐行显示 authored by（agent 名或人名）
git ai stats      聚合 AI 行数、被接受率、按 agent×模型拆分
```

## 二、机制（读代码所得，三段）

1. **归集**：每个受支持的 agent 在改文件前后发 checkpoint。以 pi 为例，`git-ai install-hooks`
   把一个托管扩展装到 `~/.pi/agent/extensions/git-ai.ts`，由它发 `before_edit` / `after_edit`，
   转成 `git-ai checkpoint pi --hook-input stdin`（`agent-support/pi/git-ai.ts`，381 行；

- `agent-support/pi/README.md`）。

2. **存储**：归属写进 git notes 的 `refs/notes/ai`（随仓库走，可 push/fetch）；prompt 正文写
   本地 SQLite，不进仓库（`data-privacy.md`）。
3. **兜底**：常驻 daemon 观测 git 的 trace2 事件流与 `HEAD` reflog，识别「没走 checkpoint 的
   提交」并补算（`src/daemon/ref_cursor.rs`；全仓 trace2 引用 168 处）。

由此，「不用 git hooks、不包一层 git 二进制」这句成立：它靠 trace2 拿到 git 的每条调用
记录，靠 reflog 定位提交边界。

## 三、客观读数

| 项 | 读数 |
| --- | --- |
| 许可 | Apache-2.0（`LICENSE` 首行为 Apache License 2.0） |
| 体量 | Rust 238 个 `.rs`，174,572 行；`tests/` 14 项；`.github/workflows/` 20 个 |
| 标准 | `specs/git_ai_standard_v3.0.0.md`，RFC 2119 措辞，含 rebase / merge / reset / cherry-pick / stash-pop / amend 六种重写的归属规则 |
| CI | `git-core-compat.yml`（多版本 git 兼容）、性能基准、CodeQL、夜间升级 |
| 生态 | VS Code / Cursor / Windsurf / IntelliJ / VisualStudio 插件；GitHub Action |
| 社区 | 约 2.9k star、228 open issues、2025-07 建仓 |
| 商业化 | 另有 `git-ai-project/self-hosted`（企业自托管，需商业许可） |

最值得记的一条：**它把「重写时归属怎么跟着走」写成了标准**。rebase 改写 hash 之后仍能对
上行，是这类工具的坟场；它对六种重写各写了规则，并有 git 版本兼容测试。

## 四、它刻意不做的事

- 不用启发式猜「这行像 AI 写的」—— agent 自报，不推测。
- 不装 git hook、不改 `core.hooksPath`、不替换 git。
- prompt 正文不进仓库。

与我们既有的纪律同源：**生产者自报，⛔ 由检测器猜**。

## 五、与我们的关系

1. **已内置 pi 支持**：`agent-support/pi/`，装到 `~/.pi/agent/extensions/git-ai.ts`。我们这套
   harness 产出的代码可零改动被跟踪。
2. 我们目前要回答「这一行是谁写的」只能靠自维护的证据链（坐标、commit、时刻各自携带）。
   它把这件事下沉到 git 层，理论上比逐条判据自带坐标便宜。
3. 但它的归属记录落在 git 里，**prompt 正文留在本机** ⇒ 「为什么这么写」不随仓库走，
   跨人不可查。审计能拿到「谁、哪个模型、哪几行」，拿不到「当时的原话」。

## 六、风险（按处置顺序）

1. **daemon 常驻**：每台开发机多一个常驻进程（读 trace2/reflog + 本地 SQLite）。本部门
   近期已因 daemon 缺陷整晚返工，此项成本不得低估。
2. **prompt 不出门**：见五.3。
3. **notes 契约**：`refs/notes/ai` 需 push/fetch 才为团队所见；CI 不加 action 会断链。
   未装本工具的同事不受影响（这是它的设计目标之一）。
4. **成熟度**：1.x、建仓 14 个月、228 open issues。
5. **名字撞车**：见文首。

## 七、试装方案（未执行）

装在一个可丢弃仓库上，三个试验；第三个是证伪条件。

```
试验1  rebase 带冲突解决后再 ai blame ⇒ 归属是否仍在正确的行上
试验2  squash 三个提交成一个 ⇒ 各来源是否保留，还是并成一条
试验3  **让一个没装扩展的 agent 改文件再提交** ⇒ 它报什么
```

试验 3 的作用：若未归集的 AI 改动被安静地算成「人写的」，本工具即为**负告警** ——
报表干净，实际把 AI 产出记在人头上。daemon 有 trace2 兜底，因此它很可能识别出「有改动
但无 checkpoint」；但标 `unknown` 还是标成人，必须看实测输出，⛔ 由文档推断。

结论口径（试装通过也只到这一层）：

- 能证明：逐行归属在本机 rebase / squash 下不丢。
- ⛔ 不能证明：团队级 AI 占比可信。后者要 `refs/notes/ai` 真正 push/fetch 起来、
  且参与方装同一套之后才谈得上。

## 八、判词

**NOT-EVALUABLE（未试装）**。信息面足以说明它值得一次有证伪条件的试装；不足以支持采纳。
未决项：一个成本极低的隔离试装（三试验 + 红/绿判据），读成本约半小时。

引用本文时请连同「未试装」一起引用。只引「它支持 pi」会读成「我们已经在用」。
