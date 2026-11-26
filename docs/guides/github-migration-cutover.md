# 开发已迁到 GitHub：切点与搬分支

2026-11-25 起，Hyprial 的开发在本仓库（`Hyprial/hyprial`）进行。内部 Forgejo 仓库已于
2026-11-25T07:06Z 归档：可读、可克隆，不能推送，不能开 PR 或 issue。规划与决策见
[GitHub 开发迁移规划](../design/github-development-migration-2026-11-15.md)（D2、D9、D14）。

## 切点

| 项 | 值 |
| --- | --- |
| 内部 `dev` 在归档时的头 | `3e9b4adf7f119d013e0d4926db7caaa2618c71ec` |
| 同步所用的内部提交（上者 + 14 个文件的清理 + 两份规划文档） | `991bf354c528da5e0fa605109ac5b9f78e07c7e4` |
| 本仓库 `dev` 的同步提交 | `8ddf9bb93ed0787a7f429c91bce6c5b6da72f48f` |

同步提交的内容是上述内部提交的整棵树，去掉 21 个排除条目；留下的每个文件与内部逐字节
相同。不带内部历史：同步提交的父提交是 `main` 上最后一次发布快照。

## 把进行中的分支搬过来

本仓库的 `dev` 与内部历史没有共同祖先，所以只改 remote 再推是不行的，要把自己的提交
搬到新的 `dev` 上：

```sh
git remote add github https://github.com/Hyprial/hyprial.git
git fetch github dev
git rebase --onto 8ddf9bb93ed0787a7f429c91bce6c5b6da72f48f 3e9b4adf7f119d013e0d4926db7caaa2618c71ec <分支>
git push github <分支>
```

- 分支不是从归档时的 `dev` 头长出来的：先在本地把它 rebase 到 `3e9b4adf`，再执行上面
  的命令；或者把第二个参数换成该分支与内部 `dev` 的合并基。
- 会冲突的两种情况：改了同步时清理过的 14 个文件（以本仓库的版本为准）；改了被排除的
  路径（见下）。
- 搬完后在本仓库重开 PR。内部未合并的 PR 不会自动出现在这里，它们仍可在归档的内部
  仓库里查看。

## 不在本仓库的内容

| 去向 | 内容 |
| --- | --- |
| 私有仓库 `hyprial-ops` | `docs/notes/`、`docs/evidence/`、含真实地址或标识的设计与协调文档、一份真实环境证据 |
| 留在归档的内部仓库 | `.forgejo/`（内部 CI 定义）、Forgejo 操作 skill、内部发布器及其测试、两份已失效的文档、一个只针对内部 runner 的测试 |

逐条清单见
[denylist 逐条去向清单](../design/github-migration-denylist-disposition-2026-11-24.md)。
改动若落在 `docs/notes/` 或 `docs/evidence/`，改投 `hyprial-ops`；本仓库的 `docs/notes/`
从空开始，只收不含真实主机名与人员的证据（D11）。

## 已迁移的 PR 与 issue（2026-11-25）

归档时内部开着 26 个 PR、58 个 issue，已按下表迁到 GitHub。迁移方式：

- **PR**：把内部 PR 相对其合并基的改动压成一个提交，放在本仓库 `dev` 之上，分支名不变。
  原提交、作者与评审讨论留在归档的内部仓库。迁移后没有运行 CI，也没有人评审过这些
  提交；内部标为 WIP 的在这里是 Draft。
- 改动落在私有条目（`docs/notes/`、`docs/evidence/` 等）下的部分进了 `hyprial-ops` 的 PR。
- 4 个 PR 在内部已与 `dev` 冲突：能应用的改动已应用，未应用的文件列在各自 PR 的正文里，
  由作者对照内部分支补上。
- **issue**：标题、正文与评论合并为一条新 issue，不带作者与评论者。
- 正文与改动内容里的内部标识（真实邮箱、主机名、tailnet 地址、个人目录等）已替换为
  `[redacted]`；带反引号的 `#编号` 是内部编号。

| 内部 PR | 分支 | 迁到 | 备注 |
| ---: | --- | --- | --- |
| 2703 | `docs/org-trace-demand-measurement` | `hyprial-ops` 的 PR 4（私有部分） |  |
| 2722 | `docs/owner-relay-evidence-rules-2026-11-12` | #73 |  |
| 2748 | `docs/kp-058-heartbeat-starvation` | **未迁移** | 唯一的改动与 `dev` 冲突（内部已是不可合并状态），需作者重做 |
| 2818 | `docs/ci-stability-ledger-reconcile-20261115` | #77；`hyprial-ops` 的 PR 5（私有部分） | 1 个文件未应用 |
| 2849 | `docs/github-development-migration-plan` | 已随同步进入 `dev`，无需 PR |  |
| 2907 | `wip/shell-signing-reference` | #76 | 1 个文件未应用；`.forgejo/` 下的改动不迁移 |
| 2988 | `wip/gui-task-page-v13-20261119` | #72 |  |
| 3003 | `docs/windows-native-pi-ci-gap` | `hyprial-ops` 的 PR 3（私有部分） |  |
| 3118 | `fix/orgfs-status-incremental-20261123` | #67 |  |
| 3120 | `wip/services-grant-20261123` | #75；`hyprial-ops` 的 PR 6（私有部分） | 1 个文件未应用 |
| 3129 | `design/tembox-logo` | #66 |  |
| 3131 | `moved/internal-pr-3131` | `hyprial-ops` 的 PR 2（私有部分） |  |
| 3132 | `fix/daemon-disallowed-tools-b-min-20261124` | #65 |  |
| 3134 | `moved/internal-pr-3134` | #71；`hyprial-ops` 的 PR 7（私有部分） |  |
| 3138 | `docs/cell-daemon-request-review-20261124` | #70 |  |
| 3139 | `fix/windows-followup-pi-and-dev-build` | #69 |  |
| 3142 | `docs/orgfs-unified-reader-20261124` | #64 |  |
| 3144 | `docs/ci-dev-red-findings-20261124` | `hyprial-ops` 的 PR 1（私有部分） |  |
| 3145 | `fix/orgfs-boot-meta-flatten-20261124` | #63 |  |
| 3146 | `refactor/public-tree-scrub-1` | 已随同步进入 `dev`，无需 PR |  |
| 3148 | `fix/orgfs-facade-client-id-20261124` | #62 |  |
| 3150 | `intg/orgfs-minimal` | #74 | 3 个文件未应用 |
| 3152 | `feat/daemon-provider-login-autogrant` | #68 |  |
| 3153 | `feat/pi-model-connections` | #61 |  |
| 3154 | `feat/dr-p02-agent-queries` | #60 |  |
| 3155 | `fix/lark-closed-completion` | #59 |  |

内部 open issue 共 58 个，按内部编号从小到大依次成为本仓库的 #1–#58；
每条 issue 的首行写着它的内部编号。

## 现在的状态（2026-11-25）

- 本仓库还没有 GitHub Actions workflow，也没有分支保护：合并没有任何自动门（owner 已
  接受，规划 6.0 节）。接通最小一组合并门（结构门、模块边界门、ruff、pytest、PR 卫生）
  是下一项工作。
- `AGENTS.md`、`CONTRIBUTING.md`、`README.md` 和 agent skill 里仍有针对内部 Forgejo 的
  说法（`fj`、`.forgejo/`、“只读发布镜像”等），尚未改写。其中与本文冲突的，以本文为准。
- 测试里读取 `.forgejo/`、`docs/notes/`、`docs/evidence/` 或内部发布器的用例在本仓库
  会失败，尚未清理；未在 GitHub 上跑过任何 CI，状态 UNKNOWN。
- 0.5.5.4 的发布冻结保持关闭（D13）。
