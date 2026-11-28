# GitHub 分支规则与团队权限

状态：建议稿（2026-11-25）。规则文件已入库，**尚未在 GitHub 上生效**：起草会话没有修改
仓库规则、团队或默认分支的权限，需要组织管理员按“启用步骤”操作。

## 团队与仓库角色

组织里已有三个团队。本文假设它们的 slug 是 `dev`、`prod`、`operator`；若不同，改
`.github/CODEOWNERS` 里的名字。

| 团队 | 仓库角色 | 能做什么 | 不能做什么 |
| --- | --- | --- | --- |
| Dev（开发） | Write | 推分支、开 PR、评审并批准、合并已满足规则的 PR | 直接推 `dev`/`main`、强推、删受保护分支、动版本 tag、改仓库设置 |
| Prod（产品） | Triage | 管理 issue 与 PR 的标签、指派、关闭与重开；评论和评审 | 推代码；其批准不计入必需的批准数 |
| Operator（运营） | Triage | 同上 | 同上 |
| 组织管理员 | Admin | 仓库设置、规则、发布 tag | — |

Triage 角色的评审意见可见，但 GitHub 只把有写权限者的批准计入必需批准数。`CODEOWNERS`
里列出的团队也必须有写权限才会被自动请求评审并生效；Prod 与 Operator 在其中出现的
几条路径（产品文档、指南、README、CHANGELOG）因此只起“通知”的作用。若希望产品对这些
路径的批准具有约束力，需要把 Prod 的角色提到 Write。

## 规则

规则以 ruleset 的形式存放在 `.github/rulesets/`，可在仓库 Settings → Rules → Rulesets →
New ruleset → Import a ruleset 逐个导入。

| 文件 | 对象 | 规则 |
| --- | --- | --- |
| `dev.json` | `dev` | 只能经 PR 合入；1 个批准；有新提交时旧批准失效；评审对话须全部解决；必需检查 `gate`；禁止强推与删除 |
| `main.json` | `main` | 同上，另加 code owner 批准 |
| `version-tags.json` | `v*` tag | 创建、移动、删除都只允许组织管理员（版本 tag 不可变） |
| `track-tags.json` | `stable`、`nightly`、`internal` tag | 创建、移动、删除只允许组织管理员 |

绕过：分支规则只给组织管理员“仅限 PR”的绕过（可以在没有他人批准时合并自己的 PR，
仍不能直接推送）；tag 规则给组织管理员完全绕过。没有给任何团队绕过权。

没有采用的常见规则及原因：

- **要求分支与 `dev` 同步后才能合并**：本仓库合并频率高（切换前约每天 30 个 PR/issue），
  这条会让每个 PR 在合并前反复重跑。
- **要求线性历史**：仓库一直用 merge commit。
- **要求签名提交**：agent 的提交目前没有签名能力。

## 需要先知道的后果

- **每个 PR 都要另一个有写权限的账号批准。** 内部仓库此前是 0 个必需批准。作者不能批准
  自己的 PR；如果多个 agent 共用一个 GitHub 账号（规划 D7 的基线），它们之间也不能互相
  批准，批准必须来自人或另一个账号。若这会卡住日常节奏，把 `dev.json` 里的
  `required_approving_review_count` 改为 0，保留其余规则。
- **`gate` 是必需检查。** 它由 `.github/workflows/gate.yml` 提供。在该 workflow 合入 `dev`
  且变绿之前导入 `dev.json`，所有 PR 都无法合并。
- 切换时迁来的 PR 分支没有这个 workflow，需要先合入最新的 `dev` 才会产生 `gate` 检查。

## 启用步骤（组织管理员）

1. 团队授权：仓库 Settings → Collaborators and teams，加入 Dev（Write）、Prod（Triage）、
   Operator（Triage）。
2. 默认分支：Settings → General，把默认分支从 `main` 改为 `dev`。
3. 等最小合并门的 PR 合入且 `gate` 在 `dev` 上为绿。
4. 导入 `version-tags.json`、`track-tags.json`（这两个不依赖 `gate`，可以先导）。
5. 导入 `dev.json`，再导入 `main.json`。
6. Settings → Actions → General：Fork pull request workflows 设为“Require approval for all
   external contributors”；Workflow permissions 设为只读。
7. Settings → Code security：开启 secret scanning 与 push protection。

## 未验证

- 规则文件没有被导入过，导入格式是否被 GitHub 原样接受是 UNKNOWN。
- 团队 slug 是假设的。
- 免费方案下公开仓库对这些规则与绕过设置的支持，以导入时的界面为准。
