# 合入流程：确认"要合的提交"就是分支头（`scripts/ci/pr_merge_guard.py`）

**定位：这是合入流程的一步（人执行），不是必需的 CI 检查。**
它需要仓库读权限与一次 `git fetch`，且只在"合并前后"有意义 —— 放进必需检查会把
"我读不到 PR" 和 "合入姿势不对" 混成同一个红。

## 它解决的事（2026-10-30 现场）

PR #2158 的 merge commit `5b8ba962b` 的 parents 是 `[dev, 51e7de589]`，
而最后一个提交 `f1e995e98` 比这次合并**晚 1 分 51 秒**才产生。结果是：

```
PR 页/API: merged=true, head.sha=f1e995e98
主干:      该 sha 不在 dev（merge-base --is-ancestor ⇒ NO）
后果:      生产代码进了主干、判据没进（旧判据可被 log.info 包裹绕过）
```

平台的 `merged` 字段回答的是"这个 PR 关了"，**不回答**"我关心的提交在主干里"。
只有 `git merge-base --is-ancestor` 能回答后者。

## 三个时钟（不要混用）

```
① PR head.sha          平台记录的"这个 PR 现在指哪"
② refs/heads/<head.ref> 分支**现在**的真实头
③ refs/pull/<n>/head    PR 镜像；PR 关闭后**冻结在合并那一刻**，不是"现在的头"
```

① == ② 才能说"我要的提交就是分支头"；③ 只说明"合并时看到的是哪个头"。

## 用法

```bash
# 合前（任一 checkout，只要能 fetch）
python scripts/ci/pr_merge_guard.py --pr 2160 --repo . --base dev
#   --remote 省略时**自动探测**：仓库里恰好一个 remote 才采用；0 个或多个 ⇒ rc=3（ambiguous_remote）
#   这是刻意的：默认 origin 会在"本机远端叫 internal"这种环境里静默查错对象

# 合后：断言那个 sha 真的进了主干
python scripts/ci/pr_merge_guard.py --pr 2160 --repo . --remote origin --base dev \
    --expect-in-base a6b510d2c
```

所需环境：`GITHUB_API_URL` + `GITHUB_TOKEN`（workflow 自带的那两个即可，无需额外凭据）。

## 为什么不用 `--remote origin` 默认值

同一台机器上 checkout 的远端可能叫 `internal`（本仓就是）。默认 `origin` 会让工具**静默查错远端**，
或者在"没有 origin"时报一个看起来像网络问题的错。所以：唯一远端自动采用；歧义一律 `rc=3`，
并在 stderr 打印候选列表。

## 退出码（三态，别折叠）

```
0  所有断言成立
1  有断言不成立（合前 head 不一致 / 合后不在主干）
3  无法判定（读不到 PR 或 fetch 失败）—— 既不是通过，也不是失败
```

## 建议的合入动作顺序

```
1. 合前跑一次（无 --expect-in-base）: pr_head == branch_head?
2. 记下这一行的 sha 作为"我要合的那个提交"
3. 合并
4. 合后跑一次带 --expect-in-base <sha>；rc=0 才算"真的进了主干"
5. 若 rc=1: 不要重推覆盖，先看是"合并早于最后一次推送"还是"合到了别的头"
```
