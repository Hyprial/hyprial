# scripts/verify — 验收/落地仪器(语义快照,原 tools/verify/)

**来源**:e2e-verifier 的验收仪器组,2026-09-19T02:29:23Z 冻结于
`instruments-snapshot-2026-09-19/`(逐文件 sha256 见该快照的 README),本目录 = 那份快照 +
快照 README「已知不一致」节点名允许的搬运修复(六处宿主/暂存假设 + 同文件 docker 两种写法)。
**本目录是这些仪器此刻语义的快照;改语义(尤其 `vlib.sh` 的三值判决)请另开单。**

| 文件 | 职责 | 退出码 |
|---|---|---|
| `vlib.sh` | 共用守卫:`need_readable` / `verdict_conj`(三值 0/2/1)/ `tea_json` / `classify_task` / `need_repo_root` / `need_exec` | (被 source) |
| `devcheck.sh <PR#> [owner/repo] [base]` | 落地预检 + payload 渲染(11 条带名条件合取) | 0 READY · 1 CANNOT CERTIFY · 2 WAIT · 3 声明的依赖缺失 |
| `tagaccept.sh [expected_dev_sha]` | 标签层验收(四条件合取) | 0/1/2 判决 · 3 不可读/依赖缺失 |
| `intggate.sh <PR#> <branch>…` | 集成闸:逐张包含性 + 连续性 + 对象在位 + 自比正控 + 树判据 | 0/1 判决 · 3 不可判 |
| `onbranch.sh <job> [branch] [window]` | 「某 job 在某分支跑过吗」+ 自带对照 | 0 FOUND · 1 NO-OCCURRENCE · 3 CANNOT CERTIFY |
| `premerge.sh <PR#>` | 动作那一刻的闸(④ ⚠️由③决定,一致性核对) | 0/1/2 判决 · 3 依赖/产物缺失 |
| `postmerge.sh <PR#> <merge> <pre-dev-head> <tree>` | 落地后五项回读(陈 base 形状点名) | 0/1/2 判决 · 3 对象缺失 |
| `README-digest.md` | 操作档(判据来源),快照原样 | — |

## 环境变量(显式依赖 —— 缺失时响亮报错,⛔ 静默降级)

| 变量 | 谁用 | 缺省 | 缺失时 |
|---|---|---|---|
| `VERIFY_REPO_DIR` | 全部 | `git rev-parse --show-toplevel`(cwd) | 点名报错(“set VERIFY_REPO_DIR…”) |
| `JOBLOG` | devcheck · tagaccept | **无(必填)** | `exit 3`,点名 `JOBLOG` |
| `DOCKER` | devcheck · tagaccept | `docker`(PATH) | `exit 3`(devcheck)/ 具名 UNREADABLE(tagaccept 的 note 行) |
| `FORGEJO_CONTAINER` | devcheck · tagaccept | `hyprial-forgejo-forgejo-1` | `exit 3`(devcheck,容器不在位)/ 具名 UNREADABLE(note 行) |
| `VERIFY_OUTDIR` | devcheck(写)· premerge(读) | **无(必填)** | `exit 3`,点名 `VERIFY_OUTDIR`;两处必须同目录 |
| `OLD_INTG` | intggate | 空 ⇒ continuity 未验(判决仍红) | — |
| `BEFORE_ID` | tagaccept | 17979(④ 的窗口阈值) | — |
| `INTG_REPO` | intggate | `HyprialOS/harness-bridge` | — |
| `ONBRANCH_TASKS_FILE` | onbranch | 空 ⇒ 走 `tea api`(测试缝) | — |

另需 `git` `jq` `python3` `tea`(已登录)在 PATH 上(devcheck 开局逐个查,缺 → `exit 3`)。

⛔ 不要把 `/tmp/...`、 `/Users/...`、`/usr/local/bin/docker`、`hq-forgejo-job-log` 的宿主编址写回这些文件;
`tests/tooling/verification/test_verification_instruments.py` 有一条静态守卫扫这个。
