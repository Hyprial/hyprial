# GitHub 迁移：发布 denylist 逐条去向清单

状态：第 5 节的 7 项已由 owner 于 2026-11-24 按建议确认（见第 5 节）；其余条目是建议，
无异议但责任人未指派。去向确认不是执行授权。本文件不移动、不删除、不改写任何文件，
也不改 denylist 本身。

日期：2026-11-24（UTC）。所属规划：
[`github-development-migration-2026-11-15.md`](github-development-migration-2026-11-15.md)
的 W2（决策 D9：迁移后不再从内部仓库向公网推送，denylist 作为机制退役）。

## 1. 结论

- 42 条里 **25 条进公开仓库**（13 条原样，12 条改写后）、**11 条进私有仓库**、
  **4 条随内部 Forgejo 归档不迁移**、**1 条删除**、**1 条本来就没有内容**。
- 私有仓库基本就是**证据与内部协调材料**：11 条合计约 8,400 个文件、约 380 MB，其中
  `docs/notes/` 与 `docs/evidence/` 占绝大部分。它不含产品代码，也不含测试框架。
- **规划 W2 初稿把真实环境 E2E（`contract/e2e/`、`tests/integration/e2e/`）整体划为私有，
  这是错的**：公开侧有 57 个文件引用 `contract/e2e/`，`AGENTS.md` 规定的隔离契约测试命令
  也依赖它。本清单改为“改写后公开”，只把真实测试 App id、内部主机等**配置值**放私有。
- 进公开仓库的条目里，含真实标识的文件一共只有 **15 个**（分布在 8 条里，第 4 节逐个
  列出；`scripts/ci/` 那 1 个就是 denylist 文件本身，随发布器删除）。改写工作量是有限的。
- `scripts/ci/` 必须公开：`AGENTS.md` 规定的结构门与模块边界门就在这个目录里。

## 2. 读数方法与边界

- 对象：内部 `dev` 的 `ea728eeb`（2026-11-24），denylist 取自
  `scripts/ci/github_publish_policy/denylist.py`，共 42 条。
- “文件”“KB”：该条目下被 Git 跟踪的文件数与字节数。
- “真实标识”：含有真实标识名单（现 private markers）中任一字面量的文件数，不区分大小写。
- “内网模式”：发布器 `INTRANET_RULES` 各规则命中的文件数之和（一个文件命中两条规则计 2）。
- “公开侧引用”：不在 denylist 之下、且不是 `docs/` 与 Markdown 的被跟踪文件中，出现该
  条目路径字符串的文件数。它是字符串匹配，包含注释和忽略规则，不等于导入依赖；
  `edge/` 的路径太短，匹配无意义，未统计。
- 没有做的事（UNKNOWN）：除第 7 条的 10 张截图外没有读图片内容（其他条目里的图片未
  清点）；没有运行 gitleaks 或 GitHub secret scanning；
  没有对提交历史、分支名和 PR 正文做扫描（规划 D2 已定不迁移内部历史）；没有检查
  denylist **之外**的文件，它们由现有发布门负责，本清单不重复。
- 分组字母沿用规划 W2 的 A–M 分组表。

## 3. 逐条清单

| # | 组 | denylist 条目 | 文件 | KB | 真实标识 | 内网模式 | 公开侧引用 | 建议去向 | 理由与要做的事 |
| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| 1 | M | `crates/hyprial-py/tests/` | 5 | 73 | 0 | 0 | 2 | 公开 | 当初排除只因不是 cargo 构建输入；公开侧的构建脚本和测试已经引用它 |
| 2 | A | `.forgejo/` | 25 | 603 | 7 | 28 | 15 | 归档，不迁移 | 由 `.github/workflows/` 取代（W3）；真实标识名单移入私有仓库；公开侧引用它的文件随 W3 改写 |
| 3 | K | `.idea/` | 6 | 2 | 0 | 0 | 0 | 删除 | 取消跟踪并加入 `.gitignore` |
| 4 | C | `.agents/skills/forgejo-ops/` | 2 | 15 | 0 | 2 | 3 | 归档，不迁移 | 由 GitHub 操作 skill 取代（W6）；公开侧 3 个引用文件同步改 |
| 5 | D | `docs/evidence/` | 3959 | 279,439 | 311 | 361 | 15 | 私有 | WORM，不能脱敏 |
| 6 | D | `docs/notes/` | 4376 | 108,518 | 1181 | 214 | 53 | 私有 | WORM，不能脱敏；公开侧的引用是路径字符串（忽略规则、契约清单），需改指向 |
| 7 | D | `gui/evidence/` | 13 | 1,090 | 0 | 0 | 0 | 公开 | 文本扫描无命中；10 张截图已于 2026-11-24 逐张看过，都是隔离预览场景下的空界面（首次设置流程的原型/实现对比、“开始”概览页），没有真实姓名、主机名、账号或会话内容 |
| 8 | H | `gui/docs/` | 50 | 1,051 | 3 | 1 | 4 | 公开（改写后） | `AGENTS.md` 要求改 GUI 前先读；3 个 PR 记录/计划文件含真实标识，改写或移入私有 |
| 9 | H | `gui/vendor/` | 0 | 0 | 0 | 0 | 0 | 无内容 | 目录下没有被跟踪的文件，条目随 denylist 一起消失 |
| 10 | H | `gui/tests/` | 328 | 3,114 | 2 | 1 | 6 | 公开（改写后） | 2 个文件含真实标识（一个夹具、一个测试），换成合成值 |
| 11 | E | `edge/` | 33 | 146 | 1 | 0 | 未统计 | 公开（改写后） | 三个 Cloudflare Worker：DERP 中继地图、发布产物分发、成员模型网关。源码与配置里没有硬编码密钥（account id 是占位符，其余经 binding 与环境变量）。要改：1 个脚本里的个人主目录路径；三份 README 对内部决策文档与内部笔记的引用，以及生产实测记录。部署凭据放私有仓库或受保护的 environment |
| 12 | K | `scripts/archived/` | 7 | 117 | 0 | 1 | 2 | 公开（改写后） | 1 个文件写着内部 Git 主机；公开侧 2 个测试仍引用该目录 |
| 13 | L | `tests/fixtures/agent_home_p20/tool-profile/` | 2 | 1 | 0 | 3 | 0 | 公开（改写后） | 夹具里的内部 remote 与邮箱换成示例值 |
| 14 | L | `tests/tooling/verification/test_probe_agent_home_p20.py` | 1 | 5 | 0 | 0 | 0 | 公开 | 随上两行（它驱动 `scripts/archived/` 的探针并读取该夹具） |
| 15 | B | `scripts/ci/` | 87 | 1,396 | 1 | 4 | 45 | 公开（改写后） | 含 `AGENTS.md` 规定的结构门与模块边界门，必须公开。删除发布快照脚本及其策略包（唯一含真实标识的文件就是 denylist 本身）；3 个文件的内部 Git 主机默认值改掉 |
| 16 | B | `tests/tooling/ci/` | 74 | 613 | 3 | 8 | 1 | 公开（改写后） | 3 个文件含真实标识（PR 卫生规则的夹具与测试、runner 凭据隔离测试），换成合成值；读取 `.forgejo/` 的测试随 W3 改写 |
| 17 | B | `tests/tooling/release/` | 85 | 638 | 0 | 9 | 2 | 公开（改写后） | 删除发布快照的测试（`publish/github/`）；其余随 W3/W4 改写；无真实标识命中 |
| 18 | F | `contract/e2e/` | 185 | 2,086 | 2 | 11 | 47 | 公开（改写后） | 公开侧有 57 个测试/源码/校验文件引用它，`./contract/run-all.sh --isolated-only` 也依赖它，不能整体私有。2 个文件含真实标识（其中 1 个是真实环境证据，移入私有）；真实测试 App id 与内部主机改为由环境变量注入，值放私有仓库 |
| 19 | F | `tests/integration/e2e/` | 64 | 286 | 2 | 6 | 0 | 公开（改写后） | 2 个文件含真实标识，换成合成值；需要真实环境的用例在公开仓库里无凭据时报告 BLOCKED，不 skip |
| 20 | I | `docs/design/gui-reference-slice/` | 4 | 21 | 3 | 0 | 0 | 私有 | 逐字保存并以 SHA-256 钉住，不能改写 |
| 21 | I | `docs/domains/<成员名>-gui-integration-workflow.md` | 1 | 9 | 1 | 0 | 0 | 归档，不迁移 | 针对内部 Forgejo 上一条以成员命名的集成分支的规则，迁移后不再适用 |
| 22 | I | `docs/design/service-connect/` | 7 | 202 | 4 | 1 | 0 | 私有 | 7 个文件中 4 个含真实服务、HQ 与 DERP 地址；公开侧如仍需要设计依据，另写不含地址的版本 |
| 23 | I | `docs/design/actor-rustify-external-developer-guide-2026-11-01.md` | 1 | 6 | 0 | 2 | 0 | 归档，不迁移 | 讲的是原私有仓库与内部账号开通，迁移后失效 |
| 24 | I | `docs/design/actor-rustify-native-engine-handoff-2026-11-01.md` | 1 | 7 | 1 | 2 | 0 | 私有 | 交接文档，含真实 agent 联系方式与 tailnet 路由地址 |
| 25 | I | `docs/design/daemon-request/c03-methodspec-2026-11-02.json` | 1 | 7 | 1 | 1 | 0 | 私有 | 证据文件，含真实 owner 标识；公开侧无引用 |
| 26 | I | `docs/design/daemon-request/c04-methodspec-2026-11-02.json` | 1 | 7 | 1 | 1 | 0 | 私有 | 同上 |
| 27 | I | `docs/design/daemon-request/methodspec-coverage-2026-11-02.json` | 1 | 5 | 1 | 2 | 0 | 私有 | 证据文件，含真实 owner 与设备标识；公开侧无引用 |
| 28 | I | `docs/design/org-membership-model-2026-10-29.md` | 1 | 17 | 1 | 1 | 0 | 公开（改写后） | 设计依据（组织成员模型仍在实施）；把示例里的真实 owner 标识换成示例值 |
| 29 | I | `docs/design/zenoh-acl-0553-goals/` | 16 | 66 | 2 | 5 | 0 | 私有 | 16 个内部协调文档 |
| 30 | I | `docs/design/zenoh-acl-1785-1850-takeover-plan-2026-10-25.md` | 1 | 18 | 0 | 2 | 0 | 私有 | 内部接管计划 |
| 31 | I | `tests/daemon/contract_mock/test_preserved_prompts.py` | 1 | 1 | 0 | 0 | 0 | 私有 | 它钉住上面逐字保存的提示词目录，随其进私有 |
| 32 | G | `tests/daemon/network/test_network_isolated.py` | 1 | 36 | 0 | 2 | 0 | 公开 | 随 `contract/e2e/` 公开；命中的 CGNAT 段与 App id 形状需确认是合成值 |
| 33 | G | `tests/daemon/adapters/lark/fixtures/test_fake_lark_service.py` | 1 | 7 | 0 | 1 | 0 | 公开 | 随 `contract/e2e/` 公开；命中的 App id 形状需确认是合成值 |
| 34 | J | `contract/sidecar-tailcat/golden/` | 4 | 15 | 0 | 0 | 1 | 公开 | 合成的 nodekey 形状夹具；确认 GitHub secret scanning 不拦截 |
| 35 | K | `scripts/refactor/preview/` | 4 | 194 | 0 | 0 | 1 | 公开 | 生成物，仅因 gitleaks 误报被排除；公开侧 1 个文件引用。确认 secret scanning 不拦截，否则重新生成 |
| 36 | L | `tests/fixtures/registry-bd6eb960/` | 4 | 8 | 0 | 0 | 1 | 公开 | 4 个文件共 8 KB，扫描无命中，公开侧 1 个测试使用。2026-11-24 已读全部 4 个文件：`provenance.json` 标明 `syntheticOnly`，行内都是合成名称与示例域名，没有凭据值 |
| 37 | J | `tests/daemon/adapters/lark/sdk/test_lark_media.py` | 1 | 22 | 0 | 0 | 0 | 公开 | 合成 media key |
| 38 | J | `tests/daemon/cells/observability/recovery/test_run_owners.py` | 1 | 2 | 0 | 0 | 0 | 公开 | 合成 lease token |
| 39 | J | `tests/daemon/delivery/identity/test_sender_identity.py` | 1 | 14 | 0 | 1 | 0 | 公开（改写后） | 合成 user key；1 处邮箱形状命中内部域名规则，换成示例域名 |
| 40 | J | `tests/daemon/routing/test_route_outbound.py` | 1 | 26 | 0 | 0 | 0 | 公开 | 凭据引用夹具（合成） |
| 41 | J | `tests/identity/agents/secret_storage/test_secret_store.py` | 1 | 21 | 0 | 0 | 1 | 公开 | 合成 token；公开侧 1 个文档测试引用 |
| 42 | J | `tests/daemon/orgfs/storage/store/frontiers/evidence.json` | 1 | 28 | 0 | 0 | 0 | 公开 | 录制的 CRDT frontier token 触发通用规则；没有测试读取它，也可以直接删除 |

第 21 条的文件名含一位成员的名字，这里用 `<成员名>` 代替；原名见 denylist 文件。

## 4. 进公开仓库前要处理的 15 个含真实标识的文件

| 条目 | 文件 | 处理 |
| --- | --- | --- |
| `gui/docs/` | `business-lines/chat-channels-v2-pr-record.zh-CN.md` | 改写或移入私有（PR 记录） |
| `gui/docs/` | `business-lines/chat-channels-pr-record.zh-CN.md` | 改写或移入私有（PR 记录） |
| `gui/docs/` | `business-lines/org-members-batch1-plan.zh-CN.md` | 改写 |
| `gui/tests/` | `fixtures/agents/agent-detail-tabs.ts` | 换成合成值 |
| `gui/tests/` | `features/connections/platform-integrations.test.tsx` | 换成合成值 |
| `scripts/ci/` | `github_publish_policy/denylist.py` | 随发布器删除 |
| `tests/tooling/ci/` | `hygiene/fixtures/submitted-by-fixtures-2026-10-09.json` | 换成合成值 |
| `tests/tooling/ci/` | `hygiene/test_pr_hygiene_rules.py` | 换成合成值 |
| `tests/tooling/ci/` | `runner/test_ci_credential_isolation.py` | 换成合成值 |
| `contract/e2e/` | `08-cc-tui-worker.md` | 改写 |
| `contract/e2e/` | `e2e-014-real-env-evidence.md` | 移入私有（真实环境证据） |
| `tests/integration/e2e/` | `e2e008/test_e2e008_cc_launch_env.py` | 换成合成值 |
| `tests/integration/e2e/` | `lark/test_feishu_e2e_info_hygiene.py` | 换成合成值（该测试本身在检查信息卫生，改写时保持断言不变） |
| `docs/design/org-membership-model-2026-10-29.md` | 该文件本身 | 示例里的真实 owner 标识换成示例值 |
| `edge/` | `derpmap/scripts/schema.ts` | 随第 11 条的决定处理 |

命中内网模式（内部 Git 主机、tailnet 主机名、CGNAT 段、内部域名邮箱、App id 形状）的文件
另需逐个改为示例值或环境变量；数量见第 3 节各行。

## 5. owner 已确认的条目

2026-11-24，owner 在本 PR 的 cloud session 中对下列 7 项回复“全部同意”，按建议确认。其中
第 1、2、6 项的建议在确认前已根据实际读取的内容更新（`edge/` 的改写范围、截图内容、
dump 夹具内容），确认的是更新后的版本。

1. **第 11 条 `edge/`**：改写后公开。改写范围见第 3 节该行；部署用的 Cloudflare 凭据放
   私有仓库或受保护的 environment。
2. **第 7 条 `gui/evidence/`**：公开。10 张截图已看，内容见第 3 节该行。
3. **第 20、31 条（逐字保存的提示词及钉住它的测试）**：一起进私有。
4. **第 22 条 `docs/design/service-connect/`**：私有。如果公开侧的代码或文档仍把它当
   设计依据，另写一份不含真实地址的公开版本，由该设计的负责人判断。
5. **第 21、23 条**：随 Forgejo 归档、不迁移。
6. **第 36 条（数据库 dump 夹具）**：公开。
7. **第 42 条**：公开（没有测试读取它，日后可直接删除）。

## 6. 对规划的影响

- 私有仓库（`hyprial-ops`）的范围比规划 W2 初稿小：不含 E2E 代码，只含证据、内部协调文档、真实标识
  名单，以及真实环境 E2E 的配置值。规划 W2 的分组表已按本清单修正 E、F、G 三组。
- 真实环境 E2E 的**代码**在公开仓库，**运行**仍在私有仓库的自托管 runner 上（规划 W3）：
  私有仓库按完整 SHA 检出公开仓库，注入配置值后运行。
- 迁移后新证据的落点（规划第 10 节未决问题 11）不受本清单影响，仍待定。

## 7. 本文件不做什么

- 不移动、不删除、不改写任何 denylist 条目下的文件；不改 denylist。
- 不创建私有仓库。
- 不把“扫描无命中”当作“可以公开”的充分条件：其他条目里的图片、历史与 secret scanning
  均未检查。
- 不指派责任人，不排期；改写工作本身尚未开始。
