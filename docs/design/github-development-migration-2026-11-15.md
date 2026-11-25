# GitHub 开发迁移规划

状态：切换已执行（2026-11-25）：内部仓库已归档，开发在 GitHub 上进行；切点与搬分支方法见
[切换记录](../guides/github-migration-cutover.md)。6.0 节第 5 步（在 GitHub 上的修改）与 Phase 3、4 尚未完成。
本文件其余部分保留为决策与规划记录：不改 workflow、不建 GitHub
分支、不迁移 issue、不旋转 secret，也不触碰 0.5.5.4 的发布冻结。

日期：2026-11-15（UTC）。起草：infra-ops。决策人：owner。

决策记录（2026-11-16T00:43:37Z，owner Lark verified）：owner 回复“全部同意按建议”，
批准剩余五项建议（D2–D6）；D1=public 与 D7=只要求可追溯此前已定。第 4 节据此记录
D1–D7。该批准是**决策批准，不是执行授权**；执行仍需 Phase 0 门与 owner 的书面批准。

修订记录（2026-11-24，owner 在本 PR 的 cloud session 中指示）：迁移后短期只有内部贡献者
提交 PR（D8）；迁移后不再从内部仓库向公网仓库推送，现有发布 denylist 作为机制退役，
GitHub 上可能维护公开与私有两个仓库（D9）。本次修订据此改写目标模型、W2、W3、W5，
新增 W8（打包与签名），并在 2.4 节补充 2026-11-24 的复测读数。D8、D9 同样是决策方向，
不是执行授权。

同日 owner 在同一 cloud session 中又确认四项（D10–D13）：W8 的两项签名建议按建议采纳；
迁移后证据的落点按建议采纳；私有仓库名为 `hyprial-ops`；**先迁移，再发布 0.5.5.4**。
D13 推翻了本文此前“迁移排在 0.5.5.4 收敛之后”的安排，相关段落已改写。

同日 owner 给出切换顺序（D14）：**先在内部仓库清理隐私信息，然后把仓库全部内容（不带
历史）同步到 GitHub，同步后立刻归档内部 Forgejo，再在 GitHub 上做其余修改。** 这取代了
第 6 节原先“先并行对跑 CI、再切写入口”的 Phase 1→Phase 2 顺序，见 6.0 节。owner 同时
批准开始清理。

随后 owner 又确认两项（D15、D16），建好了私有仓库 `Hyprial/hyprial-ops`，并明确授权起草
会话向 GitHub 上 Hyprial 组织的仓库推送。同步前检查的演练结果见 6.0 节末尾。

## 0. 一句话

代码已经在向 GitHub 同步，但同步的是**发布快照**；本规划要把**开发主线**——分支、
PR、CI、issue 与 agent 身份——迁到 GitHub，同时保留现有 release/tag 的公信力，并给出
可回滚的单向切换路径。

## 1. 结论摘要

- 现状不是“从零迁移”。公网仓库已经按 tag 接收内部快照；缺的是开发写入口和 CI。
- **分阶段、单向切换**：先并行只读/CI 演练，再切写权限，最后把内部 Forgejo
  降为归档/只读。全程只允许一个 writer，避免双写。
- **public 仓库 + GitHub-hosted 标准 runner 起步**：public 仓库的标准 runner
  免费且不消耗套餐分钟；本项目实测 24h 13,706 runner-min、峰值 22 并发，GitHub Free
  的 20 总并发/5 macOS 会在峰值短暂排队，但无需付费。private 仓库的免费额度完全不够。
- **D1–D7 已于 2026-11-16 确定**：public 仓库、从当前公网快照开新历史、
  hosted 起步 + 必要自托管、GitHub-native 发布、不批量迁移 issue/PR、内网访问先测量、
  agent 身份只要求可追溯。决策不等于执行授权；Phase 0 的测量、spike 与写切换门仍保留。
- **Agent 身份按“可追溯”而非“独立 actor”设计**：GitHub 的写操作 actor 是 user 或
  GitHub App（bot），没有内部 Forgejo 那种 per-agent 用户/RBAC 语义。owner 在 D7 只要求
  可追溯，不要求独立 actor/权限；本文采用的基线模型是共享账号 + PR 正文 `Submitted-by`
  + 自有审计日志。文档必须明确它只是**声明 + 日志可追溯**，不是认证，也不是权限边界。
  Phase 0 仍做身份 spike，验证该模型能否满足可追溯；若不能满足，再走每 agent
  App/machine user 或保留 Forgejo 写入口（另行立项）。
- **D8、D9 于 2026-11-24 记录**：短期只有内部贡献者；内部→公网的推送链在切换后消失，
  denylist 不随迁移保留。它管的内容改为一次性分类：进公开仓库、脱敏后进公开仓库、
  进私有仓库或删除。公开仓库只用 GitHub-hosted runner，全部自托管 runner（含持有签名
  身份的打包 Mac）只注册给私有仓库。
- **现在还不具备执行条件**（2026-11-24 核对，见 2.4 节）：Phase 0 的身份 spike、泄漏
  扫描、D6 测量在仓库里都没有结果记录；GitHub 侧尚无分支、workflow 或 ruleset；
  KP-100 登记的签名身份隔离未解决。0.5.5.4 的发布冻结不再是迁移的前置条件（D13），
  但在迁移期间保持关闭。

## 2. 现状（实测）

### 2.1 两个仓库的角色

| 对象 | 现状 | 依据 |
| --- | --- | --- |
| 内部 Forgejo 源仓 | 开发主线；`dev` 为默认分支；dev-first；22 个 open PR（2026-11-15T17:46Z）；18 个 CI workflow 文件 | 内部 Forgejo API/仓库配置 |
| 公网 GitHub `Hyprial/hyprial` | public；默认分支 `main`；只有 `main` 一个分支；0 个 Actions workflow；0 个 ruleset；issues 开启、discussions 关闭；允许 fork；组织为 Free plan | GitHub API 2026-11-15 |
| 公网内容 | 最后一次 push 2026-11-08T07:38:55Z；`VERSION` 0.5.5.3.4；29 个 tag，含 `v*` 与 `stable`/`nightly`/`internal` 等 release；README 明确写“read-only release mirror” | GitHub API/README |
| 同步链 | tag push（`v*`/`internal`/`nightly`/`stable`）→ 发布 workflow → 黑名单快照 → 内网标识扫描 + gitleaks → `uv build` → 推 GitHub `main` 与可移动 tag | 内部发布 workflow 及其注释 |
| agent 身份模型 | 内部 Forgejo 有按 agent 注册的账号和提交身份；公网 GitHub 目前只有组织/个人账号，尚无 agent 账号或 App 设计 | 内部 Forgejo 用户/提交记录；GitHub 组织设置 |

同步链的四个关键事实：

1. 公网 `main` 是**发布器生成的快照提交历史**，不是内部 `dev` 的历史；两边 SHA 不可
   直接比较。
2. 发布门包括黑名单快照、内网标识 allowlist、gitleaks 固定版本扫描和 wheel
   installability 检查；任何新公开内容都必须先过这些门。
3. 正式 `v*` 在公网不可变；可移动 tag 只允许 tag ref force，不允许 force 分支。
4. 发布凭据是细粒度 token，仅 Contents 读写；发布 runner 走代理访问 GitHub。迁移后
   这条“内部推公网”的链路应退役，而不是继续承担开发同步。

### 2.2 CI 规模与限制

| 口径 | 最近 24h | 最近 7 天 |
| --- | ---: | ---: |
| 已完成 task | 3,076 | 12,452 |
| runner 分钟 | 13,706 | 45,561（日均 6,509） |
| 平均并发 | 约 9.5 | 约 4.5 |
| 起止重叠峰值 | 22 并发 | — |
| macOS 峰值 | 1 | — |

- 24h artifact：2,647 个，原始约 460MB、压缩约 224MB；当前保留 90 天，按此推算
  90 天约 41GB 原始 / 20GB 压缩。
- 当前队列（2026-11-15T16:47Z）：通用标签 29 waiting、最老 22.5 分钟；Linux/macOS
  arm64 各 9 waiting、最老约 105.6 分钟；running 19。瓶颈主要是 CPU 和单点 arm64
  lane，不是等待中的 task 数量。
- GitHub 免费额度：public 仓库 + 标准 GitHub-hosted runner 免费；GitHub Free 为
  **20 总并发 / macOS 最多 5**；artifact 与 Packages 共享 **500MB** 存储额度
  （public 仓库 artifact 是否完全免费，官方文档未像 runner 分钟那样明确；本规划按
  保守额度设计）。
- private 仓库不可行：Free 的 2,000 分钟约等于本项目 3–7 小时的用量；Team 3,000、
  Enterprise 50,000 也远低于本项目的月用量。

### 2.3 迁移面

- 18 个 CI workflow；`actions/upload-artifact@v3` 共 48 处、分布在 13 个文件。
  v3 在 GitHub 已退役，迁移时必须升到 v4，并同步调整 artifact 名称、保留期和权限。
- 13 个 workflow 涉及 Docker/DinD、`host.docker.internal` 或宿主代理；2 个使用
  `/ci-artifacts` 宿主挂载。GitHub-hosted runner 没有这套宿主目录和固定代理布局。
- 13 个非 `GITHUB_TOKEN` 的 secret 名称，覆盖发布、包仓库、R2、主页同步、Lark
  E2E 与模型 API。迁移后 `GH_PUBLISH_TOKEN` 与发布 runner 应退役；其余按环境/OIDC
  重新划分。
- Agent 身份不是可平移到 GitHub 的配置项：GitHub 的 actor 是 user 或 GitHub App
  （`<app>[bot]`），没有 Forgejo 的每 agent 用户与权限。共享账号 + `Submitted-by`
  只能提供弱声明；owner 在 D7 只要求可追溯，不要求独立 actor/权限；本文采用的基线是
  弱声明 + 自有审计日志，由 Phase 0 spike 验证。独立 actor、独立权限、独立审计需要
  machine user 或 App，且各有账号、seat、私钥与轮换成本。machine user 仍是个人账号，
  不能由组织自动创建；每 agent 一个 machine user 还受人工注册、2FA、seat 与条款限制。
  GitHub App 的 actor 是 App 级，只有“每 agent 一个 App”才近似独立。
- 13 个脚本或 skill 硬编码内部 Git/Forgejo URL；CI planner 读取 `.forgejo/git-flow.json`；
  测试引用 `.forgejo/workflows` 路径。迁移必须同时改机器可读配置、测试和 agent skill。
- 公网快照不包含 `.forgejo/`，所以 GitHub Actions 是全新的；不能假设现有 workflow
  可以直接被 GitHub 读取。

### 2.4 2026-11-24 复测

对内部 `dev`（`14d37d81`）与 GitHub API 的只读复测。2.1–2.3 节保留 2026-11-15 的原始读数；
两者不一致处以本节为准，执行前仍须再测。

| 项目 | 2026-11-15 | 2026-11-24 |
| --- | ---: | ---: |
| 内部 open PR | 22 | 15（其中 11 个 Draft/WIP） |
| 内部 open issue | 未记录 | 58 |
| workflow 文件（`.yml`） | 18 | 19 |
| `actions/upload-artifact@v3` 引用 | 48 处 / 13 个文件 | 54 处 / 15 个文件 |
| 涉及 Docker/DinD 或宿主代理的 workflow | 13 | 16 |
| 使用 `/ci-artifacts` 宿主挂载的 workflow | 2 | 2 |
| 非 `GITHUB_TOKEN` 的 secret 名称 | 13 | 13 |
| 发布 denylist 条目 | 未记录 | 42 |
| 内部 `VERSION` / 公网 `VERSION` | — / 0.5.5.3.4 | 0.5.5.4 / 0.5.5.3.4 |
| GitHub 分支 / workflow / ruleset / PR | 1 / 0 / 0 / 0 | 1 / 0 / 0 / 0 |
| GitHub 最后一次 push | 2026-11-08 | 2026-11-08 |

未复测（沿用 2.2 节读数，状态 UNKNOWN）：runner 分钟、并发峰值、队列、artifact 体量。

Phase 0 门的核对结果：

| 门 | 2026-11-24 状态 | 依据 |
| --- | --- | --- |
| D1–D7 已记录 | PASS | 第 4 节 |
| 身份 spike 结论 | 未做或未落仓库 | 内部 `dev` 全树检索无记录 |
| 选定 SHA 的泄漏扫描 | 未做或未落仓库 | 同上 |
| D6 内网访问测量 | UNKNOWN | 同上 |
| 沙箱仓 workflow 翻译与样例 PR | 未开始 | GitHub 上 0 个 workflow |
| 0.5.5.4 与迁移的先后 | 已定（D13）：先迁移 | 发布冻结在迁移期间保持关闭；KP-027 改在 GitHub 上解决，见 W4 |
| 签名身份与 PR job 隔离 | 阻塞 | `known-problems.md` 的 KP-100“仓库公开之前必须满足的条件” |
| denylist 条目分类（D9 新增） | 未开始 | 本次修订才提出，见 W2 |

## 3. 目标模型

```text
开发者 / agent（短期只有内部账号，D8）
      │  PR
      ▼
公开仓库 `dev`（受保护，required checks，只用 GitHub-hosted runner）
      │
      ├─ 合并后的 CI
      │
      └─ 固定候选 SHA → `v*` tag（不可变）→ 未签名产物 + SHA256SUMS
                                                │
                    ┌───────────────────────────┘
                    ▼
私有仓库 `hyprial-ops`（写权限只给少数维护者；全部自托管 runner 只注册在这里）
      ├─ 真实环境 E2E 的运行与配置值（代码在公开仓库）：按公开仓库的完整 SHA 检出
      ├─ 打包、签名、公证（W8）：按 SHA256 取产物 → 签名 → 上传 GitHub Release
      ├─ 部署凭据与真实标识名单（现 private markers）
      └─ 含真实主机名/人员的 WORM 证据（现 `docs/notes/`、`docs/evidence/`）

GitHub Release ─ R2 / PyPI / 包仓库 ─ stable / nightly / internal 轨道

内部 Forgejo：只读归档（可选：GitHub → 内部的反向只读镜像）
```

两个仓库之间的规则（D9）：

- **依赖只有一个方向**：私有仓库按完整 SHA 引用公开仓库；公开仓库的构建、测试和
  required checks 不得依赖私有仓库的任何内容，否则外部读者无法复现，公开仓库也不再自足。
- **没有仓库间的内容同步**：不存在“私有 → 公开”的快照推送，因此也不存在 denylist。
  一个文件只属于一个仓库；要公开的内容直接在公开仓库里写。
- **公开仓库的 PR 分支在 push 的那一刻就是公开的**。CI 里的扫描只能事后发现，不能阻止；
  阻止只能发生在 push 之前（本机/agent 的 pre-push 检查与 GitHub push protection，见 W5）。
- **产品代码不按组件拆仓库**：wheel 构建同时打入 GUI、tailcat 与 Rust crate，契约测试与
  结构门跨这些目录；私有仓库只放“不能公开且不是产品代码”的内容。

分支角色：

- `main`：公网稳定/发布锚点，默认分支；只由发布流程更新，禁止直接 push。
- `dev`：唯一集成主线；所有完成的 feature/fix/docs/ci/hotfix/integration PR 的目标。
- `intg/*`、`release/*`：沿用现有语义；WIP 保持 Draft。
- `v*`：不可变；现有公网 tag 与 release 保留，不重写、不删除重建。
- `stable`、`nightly`、`internal`：可移动轨道；单一 writer，保留 lease/并发保护。

过渡期只允许一个方向接收写入：Phase 2 之前内部是 writer，Phase 2 之后 GitHub 是
writer。禁止双向自动同步；任何临时双跑都必须明确哪一侧只读。

## 4. 已定决策与执行前门

2026-11-16T00:43:37Z，owner 回复“全部同意按建议”（消息 `b08738e1-c0e7-53ac-83ad-133690ce0e4e`），
D1–D7 按下表记录。D6 的最终拓扑仍由 Phase 0 测量结果决定；D7 的独立 actor 若日后成为硬
要求，另行立项，不混在本次切换里。D8、D9 于 2026-11-24 由 owner 在本 PR 的 cloud session
中给出；D9 中“两个仓库”owner 的原话是“可能”，私有仓库的最终范围由 W2 的分类结果确定。

| ID | 问题 | 选项与建议 | Owner 决定（2026-11-16） | 为什么要现在记录 |
| --- | --- | --- | --- | --- |
| D1 | 仓库可见性 | public / private；建议 **public** | **public** | public 标准 runner 免费；private 免费分钟只有本项目 3–7 小时，不可行。public 的代价是代码、PR、issue 全公开，必须前置泄漏门 |
| D2 | 公网历史 | 从当前公网快照开新历史 / 过滤内部完整历史；建议 **新历史** | **从当前公网 `main` 快照开新历史**，内部历史归档 | 内部历史包含内网标识、人员邮箱和旧凭据；逐 commit 重写风险高。若必须保留历史，应单独立项审计，不混在切换里 |
| D3 | CI 托管 | 全 hosted / 全自托管 / 混合；建议 **hosted 起步 + 必要自托管** | **hosted 起步 + 必要的自托管**；public 仓库挂自托管 runner 必须禁止未审查 fork 代码 | 通用与 arm64 可用 hosted；内网 E2E、真实凭据、DinD/宿主挂载可能需要自托管 |
| D4 | 发布路径 | GitHub-native / 继续内部发布器；建议 **GitHub-native** | **GitHub-native**；内部发布器保留到切换完成，随后退役 token 与发布 runner | 开发在 GitHub 后，内部 tag→公网快照的链路会变成反向依赖 |
| D5 | issue/PR 历史 | 不批量迁移 / 批量迁移；建议 **不批量迁移** | **不批量迁移**；冻结前收敛内部 open PR，必要条目在新仓重建并链接 | 当前公网 0 个 open issue；内部 22 个 open PR 应在冻结前收敛；历史留在归档 |
| D6 | 内网访问 | 直连 GitHub / 内部只读镜像；建议 **先测量再定** | **先测量稳定性/延迟再定**；若不稳，保留内部只读镜像；绝不双写 | 开发机与 agent 访问 GitHub 的稳定性/延迟未知；最终拓扑必须由 Phase 0 读数决定 |
| D8 | 贡献者范围 | 只接受内部贡献者 / 开放外部贡献；owner 直接给出 | **迁移后短期只有内部贡献者提交 PR**（2026-11-24） | 决定 W8 的签名方案与 runner 边界可以多简单。注意这是政策，不是 GitHub 的强制能力：public 仓库任何人都能从 fork 开 PR，必须靠 fork workflow 审批与“公开仓库不挂自托管 runner”来落实 |
| D9 | 内部→公网发布链与仓库数量 | 保留 denylist 过滤链 / 退役并改为分仓库；owner 直接给出 | **迁移后不再从内部仓库推送到公网，denylist 作为机制退役；GitHub 上可能维护公开与私有两个仓库**（2026-11-24） | 现有 42 条 denylist 里有开发必需的目录（CI 工具及其测试、E2E、GUI 文档与测试）。GitHub 成为唯一写入口后“过滤后发布”不再成立，每条必须在切换前定去向 |
| D10 | 打包与签名（W8） | 手动签名 / 公开仓库 environment secret / 经私有仓库自动化；建议切换时手动、之后经私有仓库 | **按建议采纳**（2026-11-24）：切换时维持手动打包与签名；之后若自动化，经 `hyprial-ops` 在打包 Mac 上签名，另行立项 | 签名身份的隔离是公开前的硬门（KP-100），方案必须先定 |
| D11 | 迁移后证据的落点 | 全部公开 / 全部私有 / 按是否含真实标识分开；建议分开 | **按建议采纳**（2026-11-24）：公开仓库的 `docs/notes/` 从空开始，只收不含真实主机名/人员的证据；含真实标识的原始证据写入 `hyprial-ops`，公开侧只留结论与指向 | `AGENTS.md` 现规定证据归 `docs/notes/`；切换时必须同步改写这条规则，否则 agent 会把原始证据推到公开仓库 |
| D12 | 私有仓库名称 | owner 直接给出 | **`hyprial-ops`**（2026-11-24） | 文档、runner 注册与跨仓库引用都要用到 |
| D13 | 迁移与 0.5.5.4 的先后 | 先收敛发布再迁移 / 先迁移再发布；本文此前写的是前者 | **先迁移，再发布 0.5.5.4**（2026-11-24） | 0.5.5.4 因 KP-027（native engine 的五平台 wheel 发布未接通）被阻断，而五平台构建正是 GitHub-hosted runner 能补上的容量。代价见 W4 与第 8 节 |
| D14 | 切换顺序 | 先并行对跑 CI 再切换（本文原方案）/ 清理后直接同步并归档；owner 直接给出 | **清理 → 全量同步（不带历史）→ 立刻归档 Forgejo → 在 GitHub 上修改**（2026-11-24）。起草方建议“最小一组 CI 在 GitHub 跑绿后再归档”，owner 选择同步后立刻归档 | 始终只有一个写入口，不维护两套 CI；代价是切换后到 GitHub CI 接通之前没有合并门，见 6.0 节与第 8 节 |
| D15 | E2E 里的 Lark App id | 改为环境变量注入 / 保留写死并豁免；建议保留 | **保留写死并在扫描里豁免**（2026-11-24） | E2E 的隔离守卫靠写死的测试 App id 与生产 App id 确保只跑在测试 App 上；App id 不是密钥。改成注入会削弱这道守卫 |
| D16 | 同步前扫描的豁免规则 | 零豁免 / 允许逐条确认的豁免；建议后者 | **豁免只给逐条确认为合成值或已公开的值，每条写明理由**（2026-11-24）；真实标识名单的命中仍不可豁免 | 命中里有测试用的 CGNAT 段地址、占位 id 与通用的 tailnet 域名后缀，改掉会改变测试含义 |
| D7 | agent 身份 | 共享账号 + PR 声明 / 每 agent machine user / 每 agent GitHub App / 混合 / 保留 Forgejo 写入口；建议 **Phase 0 spike** | **只要求可追溯**；采用基线：共享账号 + PR `Submitted-by` + 自有审计日志；明确标为弱审计，不是认证或权限边界；Phase 0 spike 保留，验证归属与审计；若需独立 actor/权限，另行立项或保留 Forgejo 写入口 | GitHub 的 actor 模型是 user/app，不是任意 agent URI；owner 已把硬约束从“独立身份”收敛为“可追溯”，但可追溯本身仍需 spike 验证 |

## 5. 工作流

### W1 身份、权限与仓库治理

- 建立 GitHub 组织团队：owner、maintainer、developer、只读 agent；启用 2FA、审计日志
  与最小权限。
- **身份 spike（Phase 0 必做，D7 已选“可追溯”）**：验证共享账号 + PR 正文
  `Submitted-by` + 自有审计日志能否对每个 agent 的 commit/PR/merge 形成可追溯记录，
  并记录其边界：
  1. 共享 GitHub 账号 + PR 正文 `Submitted-by`：**owner 选定基线**；只有声明，不是认证；
     需要自有日志补足“谁在何时以该账号做了什么”，并明确日志本身的可信边界。
  2. 若基线不能满足可追溯（例如无法区分同一账号下的两个 agent），再评估每 agent
     machine user + fine-grained PAT/SSH（独立作者，但账号、2FA、seat 与凭据生命周期重）。
  3. 若需要独立 actor/权限，则评估每 agent 或每角色 GitHub App + 短时 installation
     token（actor 为 `<app>[bot]`），或把该部分留在内部 Forgejo；单独立项，不混在本次切换。
- **不把“可追溯”写成“已认证”**：`Submitted-by` 与自建日志是声明/追溯层；GitHub 的
  push/merge/audit 仍归共享账号。文档、PR 模板和 runbook 都要这样写。
- 禁止把 token 写进 remote URL 或日志；禁止把共享个人 token 当作 agent 身份。
- 配置 `CODEOWNERS`、ruleset、required checks、review 要求、tag 保护/immutable
  releases；确认 merge queue 在当前 plan 可用后再启用。
- 验收：身份 spike 记录共享账号模型的 actor/审计/权限/seat 结果，并证明每个 agent 的
  commit/PR 可由 `Submitted-by` + 自有日志追溯到具体 agent；若无法证明，记录为 BLOCKED
  并启用备选（machine user/App/保留 Forgejo）。一个人类和一个 agent 各用最小权限开测试
  PR；都不能直接 push 受保护分支；PR 正文标识真实 `Submitted-by`，但不得把它当成权限控制。

### W2 历史、公开面、分仓库与归档

- 冻结内部写入，记录最终 `dev` SHA；对完整树、提交信息、分支名、PR 正文和附件做
  内网标识/真实标识名单/gitleaks 等价扫描。
- **denylist 只再用一次**（D9）：它的 42 条是分类的输入清单，不是迁移后的机制。切换前
  每一条都要有去向与责任人；分类完成、种子提交生成后，denylist、发布快照脚本及其测试
  随内部发布器一起退役（Phase 4）。
- 用当前公网 `main` 作为公开仓库新历史的种子；把选定内部 SHA 中**分类为公开**的内容
  作为一个 “sync” 提交放到新的 `dev`。分类为私有的内容作为私有仓库的初始提交。两边都不
  推内部历史。
- 保留现有公网 tag 与 release；不改写不可变 `v*`；不删除历史 `dev-track` 等旧 tag。
- 内部 Forgejo 归档为只读；保留备份、URL 重定向和引用说明。

denylist 条目的建议去向（按组）。逐条清单见
[`github-migration-denylist-disposition-2026-11-24.md`](github-migration-denylist-disposition-2026-11-24.md)，
其中有每条的文件数、体积、命中数与公开侧引用数。该清单第 5 节的 7 项已由 owner 于
2026-11-24 确认；其余条目是建议，责任人待定。按该清单：25 条进公开仓库（12 条需改写）、
11 条进私有仓库、4 条随 Forgejo 归档、1 条删除、1 条无内容；私有仓库只装证据、内部
协调文档与真实环境配置值。

| 组 | 现 denylist 条目 | 建议去向 | 理由与要做的事 |
| --- | --- | --- | --- |
| A 内部 CI 定义 | `.forgejo/` | 不迁移，随 Forgejo 归档 | 由 `.github/workflows/` 取代（W3）；其中的真实标识名单进私有仓库 |
| B CI 工具与其测试 | `scripts/ci/`、`tests/tooling/ci/`、`tests/tooling/release/` | **公开**（改写后） | 公开仓库的 required checks 要用它们；先去掉内部服务器默认值与对 `.forgejo/` 的读取。其中只服务于发布快照的部分直接删除 |
| C Forgejo 操作 skill | `.agents/skills/forgejo-ops/` | 不迁移，随 Forgejo 归档 | 由 GitHub 操作 skill 取代（W6） |
| D WORM 证据 | `docs/notes/`、`docs/evidence/`、`gui/evidence/` | 前两个**私有**；`gui/evidence/` **公开** | 前两个含真实主机名、人员、client id，WORM 要求保持原字节，不能脱敏。`gui/evidence/` 文本无命中，10 张截图已看过，是隔离预览的空界面。迁移后新证据的落点见第 10 节未决问题 11 |
| E 部署配置 | `edge/` | **公开**（改写后；owner 2026-11-24 确认） | 三个 Cloudflare Worker（DERP 中继地图、发布产物分发、成员模型网关）；源码与配置无硬编码密钥。改 1 个脚本里的个人路径和三份 README 的内部引用。部署凭据放私有仓库或受保护的 environment |
| F 真实环境 E2E | `contract/e2e/`、`tests/integration/e2e/` | **公开**（改写后）；配置值**私有** | 2026-11-24 逐条读数修正：公开侧有 57 个文件引用 `contract/e2e/`，隔离契约测试也依赖它，不能整体私有。代码公开，真实测试 App id 与内部主机改由环境变量注入，值放私有仓库；运行仍在私有仓库的自托管 runner 上 |
| G 依赖 F 的隔离测试 | 2 个测试文件（读取 `contract/e2e` 的测试 App 列表或 fake） | **公开** | 随 F 公开；确认其中 CGNAT 段与 App id 形状是合成值 |
| H GUI 文档与测试 | `gui/docs/`、`gui/tests/`、`gui/vendor/` | **公开**（审查/改写后） | `AGENTS.md` 要求改 GUI 前先读 `gui/docs/`，它必须和代码在同一个仓库；`gui/tests/` 的 tailnet 形状夹具改为合成值；`gui/vendor/` 目前为空 |
| I 含真实标识的设计文档 | `docs/` 下 11 条（service-connect、交接文档、methodspec 证据、组织模型示例等）及依赖它们的 1 个测试 | 逐条二选一：脱敏后**公开**，或**私有** | 仍被代码或测试引用的设计依据应脱敏公开；纯交接/协调材料进私有 |
| J 只因 gitleaks 误报被排除的测试与夹具 | 7 条（合成 token、nodekey 形状夹具、CRDT frontier 证据等） | **公开** | 内容是合成的；需确认 GitHub secret scanning/push protection 不拦截，否则改夹具形状或登记为误报 |
| K 归档与生成物 | `scripts/archived/`、`scripts/refactor/preview/`、`.idea/` | 删除或**私有** | 已退役脚本仍写着内部主机；生成物与 IDE 配置不应入库 |
| L 未做公开审查的夹具 | 数据库 dump 夹具、P20 探针夹具及其测试 | 审查后定 | 审查通过则公开，否则改为合成夹具 |
| M 非构建输入 | `crates/hyprial-py/tests/` | **公开** | 当初排除只因不是 cargo 构建输入，没有敏感内容的记录 |

- 验收：42 条 denylist 逐条有去向、责任人与完成记录；公开种子提交无未豁免命中；公开
  仓库在不访问私有仓库的情况下完成全量构建与 required checks；现有 release 仍可安装；
  私有仓库初始提交可读回；归档可读回。

### W3 CI 迁移

- 18 个 workflow 逐一翻译到 `.github/workflows/`；把 v3 artifact 升到 v4；重新设计
  保留期、压缩与上传范围。
- runner 映射：通用/轻量/发布类优先用 GitHub-hosted 标准 runner；Linux arm64 用
  GitHub-hosted arm64（执行前复核当时标签与可用性）；macOS 用 hosted macOS；涉及
  内网、真实 Lark 凭据或宿主挂载的 E2E 保留受控自托管。
- **自托管 runner 只注册给私有仓库**（D3 + D9）：公开仓库不挂任何自托管 runner。现有
  6 种自托管标签（通用轻量、发布、Linux arm64、macOS arm64、macOS 宿主、GitHub 发布）
  逐个定去向：改用 hosted、迁到私有仓库或退役。私有仓库没有 fork PR，自托管 runner 在
  那里不接触未审查的外部代码；私有仓库的 hosted 分钟额度很小（Free 2,000 分钟/月），
  所以它的 job 默认跑自托管。
- 私有仓库的 E2E 如何被公开仓库的事件触发、结果如何回写到公开 PR（跨仓库 dispatch、
  轮询或手动；回写 commit status 需要 App 或细粒度 token）在 Phase 0 沙箱里验证。在此
  之前，真实环境 E2E 不作为公开仓库的 required check。
- DinD、`host.docker.internal`、`/ci-artifacts` 不能在 hosted runner 上原样假设；
  需要改为容器内构建、artifact 上传或外部对象存储。
- secret 迁到 GitHub environments/OIDC；fork PR 默认拿不到 secret；禁止用
  `pull_request_target` 检出并执行不可信 fork 代码。
- CI planner、`.forgejo/git-flow.json`、测试路径、`fj`→`gh`、agent skill 同步修改。
- 验收：同一 SHA 在 GitHub 的 required checks 与内部结果对齐；队列/并发在目标内；
  artifact 存储低于额度；无 secret 泄漏。

### W4 发布与分发

- 在 GitHub 上实现 release-tag 入口：完整 SHA、VERSION/CHANGELOG 校验、不可变
  `v*`、受保护 environment、人工批准。
- 五平台 wheel、SHA256SUMS、R2、PyPI/包仓库、GitHub Release 的写入保持单一 writer；
  stable/nightly/internal 的 lease 与“先读后写”不变量保留。
- 用 OIDC 替代长期云凭据（能替代的部分）；保留必要的发布 secret 并定期旋转。
- 发布器在切换完成前保留；切换后删除发布 workflow 与发布 token，避免两套发布链。
- **0.5.5.4 是 GitHub 上的第一次正式发布（D13）**。KP-027 的剩余工作因此落在本工作流：
  native engine 的 wheel 要求目标 OS/架构等于构建宿主，五个平台各用一个 GitHub-hosted
  runner 的 matrix job 构建，不再在单个作业里循环；Linux 构建必须在能证明 glibc ≤ 2.17
  的环境里完成。校验器、R2 清单、GitHub 附件与两个安装脚本对新旧 wheel 标签的兼容
  （2026-11-09 的候选）随代码一起迁移，安装器必须继续认识既有轨道上的旧 wheel。
- 迁移期间内部的 internal 与正式发布开关保持关闭；GitHub 上的等价开关默认关闭，解除
  条件沿用 `docs/design/native-wheel-publication-0554.md`，由 owner 单独批准。
- D13 的代价：第一次 GitHub-native 发布同时带着新的发布链和新的 wheel ABI，两类风险
  叠加。缓解：Phase 3 先对一个不移动任何轨道的 dry-run 候选跑通五平台构建与安装回读，
  再发布 0.5.5.4；内部发布器在此之前不退役，但也不用它发布 0.5.5.4。
- 验收：先 dry-run，再完成一次真实发布；五平台产物与摘要跨分发端一致；回滚步骤已
  演练。

### W5 安全与合规

- 开启 GitHub secret scanning、push protection、Dependabot、dependency review、
  CodeQL（按语言与成本选择）；默认 `GITHUB_TOKEN` 只读。
- 限制可用 action，全部按版本或 SHA 固定；禁止浮动引用。
- 公开仓库的 issue/PR 也要执行“不得写入凭据、生产状态、内部标识”的规则。
- 自托管 runner 只在私有仓库（W3）；公开仓库把“外部贡献者的 workflow 需审批”设为
  对所有外部贡献者生效。D8 的“只有内部贡献者”靠这两条落实，而不是靠约定。
- **push 之前的检查**：公开仓库的分支一经 push 即公开，所以内网标识与真实标识名单的
  检查要在开发机和 agent 的 pre-push 阶段运行，名单从私有仓库读取，不进公开仓库。
  公开仓库 CI 里的同类扫描保留为事后发现，命中按泄漏事件处理（删分支不等于撤回）。
  GitHub push protection 能否加载自定义模式取决于当时的 plan 与产品条款，Phase 0 核对。
- 验收：push protection 能拦截测试 token；action 策略生效；fork PR 无法取得 secret；
  安全报告渠道已公布。

### W6 文档与工具

- 切换后更新 README、CONTRIBUTING、AGENTS、`docs/guides/*`、`.agents/skills/*` 和
  所有安装/克隆示例；删除“read-only release mirror / 不接受 PR”的过期表述。
- 更新 `fj` 为 `gh`/GitHub API 的操作说明；保留 Forgejo 归档的只读访问说明。
- 清理硬编码内部 URL 的脚本与测试；为 GitHub 路径增加策略测试。
- 验收：新贡献者按文档能在 GitHub 开 PR；全仓搜索无残留内部 Git URL；docs 测试通过。

### W7 切换与回滚

- 冻结窗口、备份、公告、22 个 open PR 的收敛、最终同步、权限切换、监控。
- 切换前保留内部可写作为回滚路径；切换后至少观察一个完整发布周期再考虑停止内部
  写入。
- 回滚触发条件、决策人、数据恢复步骤和最长恢复时间必须在切换前写清。
- 验收：清单签字；回滚演练通过；切换后第一个 PR 与第一次发布均成功。

### W8 打包与签名

背景：`docs/reference/known-problems.md` 的 KP-100 记录了现状与“仓库公开之前必须满足的
条件”。本工作流把它接进迁移计划；此前 D3 等决定没有覆盖签名。

- 现状（owner 决定 2026-11-20）：没有任何 workflow 打包、签名或公证；macOS 包由操作者在
  打包 Mac 上手动运行打包脚本。Developer ID 身份与公证 profile 在该 Mac 的登录
  keychain 里，而同一个 macOS 账号下还跑着两个 CI runner，其中一个执行 PR 触发的 job。
- 签名脚本本身可以公开：命令行只带路径、证书 SHA-1 与 profile 名，不含密钥。要保护的是
  私钥与公证凭据，它们不进任何仓库（公开或私有）。把打包代码闭源不增加安全性。
- **切换前必须完成（硬门）**：持有签名身份的 Mac/账号不再为公开仓库运行任何 job。按 W3
  的规则，该 Mac 只注册给私有仓库即满足；否则把身份移到不跑 CI 的独立 macOS 账号。
- 切换时的做法（D10，owner 2026-11-24 确认）：维持手动打包与签名，操作者用公开仓库上已存在的
  提交构建（KP-100 的分发规则不变）。
- 之后若要自动化（D10，owner 2026-11-24 确认方向，另行立项）：公开仓库产出未签名产物与
  SHA256SUMS；私有仓库的 workflow 在打包 Mac 上按摘要取产物、签名、公证、上传 GitHub
  Release。这样对公开仓库有写权限不等于能使用签名身份。不选“公开仓库 environment
  secret + hosted runner”作为默认，是因为它把公开仓库的全部维护者放进签名信任链；
  D8 之下它可行，留作备选。
- Windows/Linux/移动端目前没有打包目标（owner 决定 2026-11-15：没有 Windows 目标）；
  出现时沿用同一条规则：签名材料只在私有仓库一侧可用。
- 验收：公开仓库的 workflow 与 runner 列表里没有任何能到达签名身份的路径（按 runner
  注册范围核对，不按 workflow 条件核对）；一次签名包由公开仓库上的提交构建并通过
  公证与 Gatekeeper 首次打开；KP-100 条目更新为已满足或记录剩余风险。

## 6. 分阶段执行与门

### 6.0 采用的切换顺序（D14，2026-11-24）

本节取代下文 Phase 0–Phase 2 的先后顺序；Phase 3（发布切换）与 Phase 4（退役与归档）
不变。下文 Phase 0–2 的条目保留为工作清单：凡是 6.0 没有列为“同步前”的，都改在 GitHub
上、同步之后完成。

1. **清理（在内部仓库，owner 已批准开始）**
   - 按逐条去向清单改写要公开的条目：含真实标识的 15 个文件，以及命中内网模式的文件。
   - 不在内部仓库搬走或删除私有条目，也不改 denylist：现有发布器仍读它，改了会改变
     下一次内部发布的内容。私有与归档条目在同步时从快照里排除。
2. **同步前的唯一硬门：扫描**
   - 用现有发布器的快照与扫描逻辑，把排除清单换成“11 条私有 + 4 条归档 + 1 条删除”，
     对选定的 `dev` SHA 生成公开快照。
   - 真实标识名单零命中，不可豁免。内网标识规则的命中只能按 D16 豁免：逐条确认为合成
     值或已公开的值，每条写明理由。gitleaks 的每条发现逐条读过并记录结论；出现记录
     之外的文件或规则即阻断。公开不可撤回，这是整个方案里唯一挡泄漏的门。
   - 现有 allowlist 的 156 条豁免沿用：它们对应的内容已在公网 `main` 上。
   - 排除清单、豁免清单、扫描脚本与 gitleaks 逐条结论存放在 `hyprial-ops` 的
     `migration/`，不进公开仓库（真实标识名单在其中）。
3. **同步**
   - 公开快照作为一个提交推到 `Hyprial/hyprial` 的 `dev`，以当前公网 `main` 为父提交
     （D2：不带内部历史）。提交信息记录来源 SHA 与排除清单的摘要，不写内部主机。
   - 被排除的 11 条私有条目作为 `hyprial-ops` 的初始提交。
4. **立刻归档 Forgejo**（内部仓库转只读；回滚即取消归档）。未合并的内部 open PR 在
   归档前合并、关闭或记下分支名，需要的在 GitHub 上重开。
5. **在 GitHub 上修改**，按这个顺序：
   - 最小一组合并门：结构门、模块边界门、ruff、pytest、PR 卫生。
   - 指向已移出路径（`docs/notes/`、`docs/evidence/` 等）的引用、`AGENTS.md` 的证据规则
     （D11）、`fj`→`gh` 与 agent skill。
   - 其余 workflow；真实环境 E2E 与自托管 runner 接到 `hyprial-ops`。
   - 身份 spike（D7）与 D6 的内网访问测量改为切换后的验证，不再是切换前的门。

owner 接受的代价：第 4 步之后、第 5 步第一项完成之前，GitHub 上没有任何合并门。这段
时间内合并靠评审，合并后的问题由随后接通的 CI 在 `dev` 上暴露。

**同步前检查的演练（2026-11-24，内部提交 `b518593e`，第一批清理 PR 的分支，尚未合并）**

- 清理前，对将要公开的树扫描得到 123 处命中（46 个文件）。第一批清理改掉 25 处；
  发布器及其测试、一份真实环境证据、一个断言 Forgejo runner 账号目录的测试共 48 处随
  文件一起排除；其余 50 处按 D15、D16 豁免。
- 结果：7,320 个文件，命中 0，豁免 206（沿用 156，新增 50），过期豁免 0。文件名不含
  真实标识。19 张 PNG 全部看过，都是夹具数据或品牌图。
- gitleaks 8.30.1：386 条发现，分布在 50 个文件，逐条读过，全部是示例键、公钥形状的
  夹具、哨兵值或内容哈希，没有凭据。
- 排除清单比逐条去向清单多 5 项：发布器脚本、其策略包、其测试（归档），一份真实环境
  证据（私有），一个只对 Forgejo runner 有意义的测试（归档）。
- 未检查（UNKNOWN）：GitHub push protection 是否拦截其中的示例值（只有实际推送才能
  看到）；SVG、字体与图标文件只核对了格式。
- 演练不是同步：真正同步用的是合并第一批清理之后、冻结时刻的 `dev` 提交，届时整套
  检查重跑一遍。

**同步前还需要 owner 做的协调**：内部 `dev` 一直有合并进入。同步取的是某一个提交，
之后合进内部仓库的改动不会出现在 GitHub 上。所以同步前要先宣布冻结，让各条线停止向
内部 `dev` 合并，并把未合并的 PR 合并、关闭或记下分支。

### Phase 0 — Preflight（只读）

- D1–D7 已由 owner 于 2026-11-16 记录（第 4 节）；本阶段完成 D7 的“可追溯”身份 spike
  （共享账号 + `Submitted-by` + 自有日志）并记录 actor、审计、权限与费用；清点账号、
  团队、secret、runner、分支保护与发布冻结状态。D6 的内网访问测量也在此阶段完成。
- 对选定 SHA 做完整泄漏扫描；在临时私有沙箱仓翻译 workflow，跑样例 PR。
- 记录当前 Forgejo 与 GitHub 的 SHA、tag、release、artifact 保留基线。
- denylist 42 条的逐条去向清单已有建议稿（W2 所链文档，2026-11-24）；本阶段完成 owner
  逐条确认与责任人指派，并在沙箱里证明公开部分不依赖私有部分即可构建和通过
  required checks。
- 清点 6 种自托管 runner 标签的去向（W3）；确认签名身份所在的 Mac/账号在切换后只为
  私有仓库服务（W8）。

门：D1–D13 已记录；D6 有测量结论；身份 spike 证明可追溯或记录 BLOCKED 与备选；无未豁免
泄漏；denylist 逐条有去向；沙箱 required checks 通过且不依赖私有内容；签名身份隔离方案
已定；owner 书面批准执行。（0.5.5.4 与迁移的先后已由 D13 确定，不再是门。）

### Phase 1 — 并行只读/CI

- 在 GitHub 上建立只读演练分支或临时私有仓；同一 SHA 同时跑内部和 GitHub CI。
- 不开放公网 merge；不让 fork 代码接触 secret 或自托管 runner。
- 对比成功率、排队时间、执行时间、artifact 大小和保留策略。

门：required checks 对齐；峰值并发与队列可接受；artifact 在额度内；无 secret 暴露。

### Phase 2 — 写入口切换

- 冻结内部 merge；收敛内部 open PR（2026-11-24 为 15 个）；按 W2 的分类生成公开仓库
  `dev` 的种子提交与私有仓库的初始提交。
- 创建受保护的 `dev` 与 `main` ruleset；开启 PR；按 D7 验证结果切换 agent 的
  remote/CLI/skill。
- 内部 Forgejo 转为只读；如需内部访问，建立 GitHub → 内部的反向只读镜像。

门：第一个真实 PR 在 GitHub 合并；分支保护生效；回滚演练完成；owner 批准继续。

### Phase 3 — 发布切换

- GitHub-native release-tag 跑通；停用内部发布器；更新安装与文档。
- 观察一个完整发布周期，核对五平台产物、R2、PyPI/包仓库与 GitHub Release。

门：一次不可变 `v*` 发布成功；五平台摘要一致；stable/nightly/internal 状态一致；
无内部发布任务残留。

### Phase 4 — 退役与归档

- 内部 Forgejo 只读归档；停用发布 runner 与 `GH_PUBLISH_TOKEN`；删除过期 workflow；
  更新 runbook、备份与审计记录；做复盘。

## 7. 切换清单

**冻结与数据**

- [ ] 公告冻结窗口；暂停内部 merge 与发布写入。
- [ ] 记录最终 `dev` SHA、VERSION、CHANGELOG、所有 tag/release。
- [ ] 备份内部仓库、数据库、CI 配置与 artifact 索引。
- [ ] 收敛内部 open PR（2026-11-15 为 22 个，2026-11-24 为 15 个）：合并、关闭或明确
      转交；WIP 保持 Draft。
- [ ] denylist 42 条逐条定去向并执行；公开种子提交与私有初始提交各自通过扫描。

**GitHub 仓库与权限**

- [ ] 团队/成员/agent 身份与最小权限。
- [ ] `main`、`dev`、`v*` ruleset；required checks；CODEOWNERS；tag 保护。
- [ ] issue/PR 模板、行为准则、贡献指南、安全报告渠道。
- [ ] secret scanning、push protection、Dependabot、action 策略。
- [ ] 私有仓库建立；写权限名单最小化；公开仓库不注册任何自托管 runner。
- [ ] 外部贡献者的 workflow 一律需审批；pre-push 检查在开发机与 agent 上就位。

**CI**

- [ ] 18 个 workflow 翻译完成；v3 artifact 全部升 v4。
- [ ] runner 映射与容量验证；hosted/自托管边界明确。
- [ ] DinD、宿主代理、`/ci-artifacts` 替代方案验证。
- [ ] secret/OIDC/environment 配置；fork 审批；默认 token 只读。
- [ ] CI planner、测试、agent skill、`fj`→`gh` 更新。

**发布**

- [ ] release-tag dry-run；不可变 tag；五平台构建。
- [ ] R2、PyPI/包仓库、GitHub Release 的单一 writer 与一致性检查。
- [ ] stable/nightly/internal 轨道策略；`internal` 是否保留由 owner 决定。
- [ ] 内部发布器停用与 token 退役；denylist、发布快照脚本及其测试删除。
- [ ] 签名身份所在的 Mac/账号不为公开仓库运行任何 job（W8 硬门）；KP-100 条目更新。

**文档与回滚**

- [ ] README/CONTRIBUTING/AGENTS/docs 更新；安装命令验证。
- [ ] 内部归档入口、URL 重定向、只读说明。
- [ ] 回滚演练：恢复内部写入口、恢复 required checks、撤销 GitHub 权限。
- [ ] 监控与责任人；切换后第一个 PR/发布的验收记录。

## 8. 风险与缓解

| 风险 | 可能性/影响 | 缓解 |
| --- | --- | --- |
| 公开泄漏内网标识、人员信息或凭据 | 高/高 | 新历史、不全量推内部历史；树/提交/PR 全扫描；push protection；分批公开 |
| CI 行为与内部不一致 | 高/高 | 同 SHA 并行；required checks 对齐；沙箱先跑；未测项标 BLOCKED |
| 20 并发/5 macOS 上限造成排队 | 中/中 | 峰值 22 仅短暂排队；减少 matrix/合并 shard；必要时升 Team 或限量 |
| artifact 500MB 额度不足 | 高/中 | JUnit 7–14 天、wheel 30 天；大文件走 R2；只上传必要文件 |
| public 仓库的自托管 runner 被 fork 利用 | 中/严重 | 默认 hosted；自托管只接可信分支；fork 全审批；runner group 隔离 |
| 内网 E2E/Lark 凭据在 hosted runner 不可达 | 高/高 | 保留受控自托管 macOS/内网 runner；明确哪些 job 只对可信分支开放 |
| 过渡期双写导致分叉 | 中/高 | 单一 writer；写切换时冻结；禁止双向自动同步 |
| 0.5.5.4 成为第一次 GitHub-native 发布，新发布链与新 wheel ABI 的风险叠加（D13） | 高/高 | 发布冻结在迁移期间保持关闭；Phase 3 先跑不移动轨道的 dry-run 候选；五平台构建、glibc 下限与安装回读逐项实测后再解除冻结 |
| 迁移拖长，0.5.5.4 的用户可见交付随之推迟 | 中/中 | owner 已接受这一先后（D13）；Phase 0 的排期里写明对 0.5.5.4 的影响 |
| 共享账号模型无法把两个 agent 的操作区分开（可追溯性失败） | 中/高 | D7 已选“可追溯”；Phase 0 spike 用 `Submitted-by` + 自有日志证明归属；若无法区分，启用 machine user/App 或保留 Forgejo 写入口（另立项） |
| agent 工具链/fj 习惯断裂 | 高/中 | 先提供 `gh` 包装与 skill；沙箱演练；保留只读归档说明 |
| 公网历史与旧 release 信任受损 | 中/高 | 不重写现有 tag/release；新历史明确说明；保留 provenance |
| 内部网络访问 GitHub 不稳定 | 中/高 | 先测量；必要时内部只读镜像；不让生产依赖 GitHub 可用性 |
| 归档丢失/不可读 | 低/高 | 双备份；定期恢复演练；保留只读实例 |
| 切换后到 GitHub CI 接通之前没有合并门（D14，owner 已接受） | 高/中 | 最小一组合并门是 GitHub 上的第一项工作；在此之前只合并必要改动；回滚是取消 Forgejo 归档 |
| 同步前的扫描是唯一的泄漏门，漏掉即公开 | 中/严重 | 三项扫描零命中才推，不新增豁免；现有豁免逐条复核；图片与二进制文件另行清点 |
| denylist 退役后，原本被过滤的内容直接进入公开仓库 | 高/高 | 切换前 42 条逐条分类；迁移后靠 pre-push 检查 + push protection 预防，CI 扫描只作事后发现 |
| 公开仓库的 PR 分支一经 push 即公开，无法撤回 | 中/高 | 预防放在 push 之前；命中按泄漏事件处理并旋转相关凭据；agent 的 hook 与 skill 同步更新 |
| 公开仓库的构建或测试暗中依赖私有仓库 | 中/高 | 沙箱里在无私有访问的条件下跑全量 required checks；依赖方向写入结构门 |
| 签名身份被公开仓库的 PR job 使用 | 中/严重 | 持有身份的 Mac 只注册给私有仓库（W8 硬门）；按 runner 注册范围核对，不信任 workflow 条件 |
| 私有仓库的真实环境 E2E 与公开 PR 脱节，回归晚发现 | 中/中 | 定时或合并后按公开 `dev` 的 SHA 触发；结果回写方式在 Phase 0 验证；发布门要求同一 SHA 的 E2E 结果 |
| 两个仓库的文档/skill 各写一半，agent 找不到规则 | 中/中 | 规则与操作说明只在公开仓库；私有仓库只放数据、配置与凭据引用，并指回公开仓库 |

## 9. 验收指标

- GitHub 上 required checks 覆盖率 100%；样例 PR 与内部同 SHA 结果对齐。
- 峰值并发不超过 20，或排队时间有明确上限和记录；不产生付费 runner。
- artifact 存储低于 500MB 额度；JUnit/wheel 有明确保留期；长期证据在外部存储。
- 未豁免的 secret/内网标识扫描命中为 0；push protection 开启。
- Agent 身份按 D7 的“可追溯”要求验收：每个 agent 的 PR/commit 可由 `Submitted-by` +
  自有审计日志追溯到具体 agent。共享账号 + `Submitted-by` 必须明确标注为弱审计/声明层，
  不是认证，不作为权限边界；若 spike 无法做到可追溯，记录 BLOCKED 并启用备选。
- 一次 dry-run + 一次真实不可变发布；五平台 wheel 与 SHA256SUMS 跨端一致。
- 回滚演练在约定时间内完成；owner 签字。
- 切换后第一个真实 PR 与第一次发布均有完整证据。
- denylist 42 条全部有去向与完成记录；公开仓库在无私有仓库访问权限的环境里通过全量
  required checks。
- 公开仓库的 runner 列表为空（只用 GitHub-hosted）；签名身份只能从私有仓库的 runner 到达。

## 10. 未决问题

D1–D7 已于 2026-11-16、D8–D13 已于 2026-11-24 记录（第 4 节），不再作为待拍板项；以下是
会影响实施但不需要重新拍板方向的剩余问题。

1. 公网提交的 provenance 写法：现有快照提交信息记录了内部发布来源；新历史需要明确
   “哪个源 SHA、哪次过滤”而不泄露内部主机。旧 tag（含历史 `dev-track`）保留不动。
2. 包仓库依赖：内部 registry 与 PyPI 官方发布的先后关系（当前 0.5.5.4/PyPI 工作仍在
   进行）；Forgejo 归档后包从哪里提供。
3. macOS 真实凭据 E2E 与内网服务：哪些必须自托管，哪些可 hosted。
4. GitHub plan 与并发：Free 的 20/5 是否长期够用；何时需要 Team。
5. 外部贡献者政策：DCO/CLA、review 权限、安全报告渠道、行为准则。D8 之下短期不接受
   外部 PR，但 README/CONTRIBUTING 要写明这一点，以及何时重新评估。
6. issue/discussions 是否开启，模板与标签体系。
7. 内部 open PR（2026-11-24 为 15 个，其中 11 个 Draft/WIP）的收敛责任人与截止时间。
8. 迁移时间窗与 PyPI 发布的先后顺序。（与 0.5.5.4 的先后已由 D13 确定：先迁移。）
9. D6 的最终拓扑：Phase 0 对开发机与 agent 访问 GitHub 的稳定性/延迟测量结果；
   直连还是内部只读镜像。
10. D7 的实施细节：共享账号、`Submitted-by` 与自有审计日志的具体字段和可信边界；
    若可追溯验证失败，machine user/App 的数量、seat、条款与审计字段。

11. D11 的落地细节：`AGENTS.md` 中证据规则的新写法；agent 如何判断一份证据含不含真实
    标识；公开侧“结论与指向”的格式。切换时与 `AGENTS.md` 的其他改动一起提交。
12. `hyprial-ops` 的写权限名单；它是否同时承载部署（现 `edge/`）的 CI。（名称已由 D12
    确定，范围见逐条去向清单。）
13. 跨仓库触发与结果回写的机制（dispatch、轮询或手动）以及所需 token/App 的权限。
14. （已由 D10 确定。）五平台 native wheel 在 GitHub-hosted runner 上的实测：各平台
    runner 标签与可用性、glibc ≤ 2.17 的构建环境、构建时长与并发占用。
15. B 组里哪些文件只服务于发布快照、可直接删除；逐条去向清单各条改写工作的责任人。
    （清单第 5 节的 7 项已于 2026-11-24 由 owner 确认，不再是未决问题。）

## 11. 参考

- 仓库规则：`.forgejo/git-flow.json`、`docs/guides/dev-source-branch-rule.md`、
  `docs/guides/release-rules.md`、`CONTRIBUTING.md`、`AGENTS.md`、`README.md`。
- 现有发布链：`.forgejo/workflows/github-publish.yml` 及其 allowlist/private markers；
  denylist 在 `scripts/ci/github_publish_policy/denylist.py`；正式发布入口见 `release-tag.yml`。
- 签名与打包：`docs/reference/known-problems.md` 的 KP-100；`packaging/clients/README.md`
  的 “Package by hand”。
- 实测数据：2026-11-15 对内部 Forgejo 只读查询与 GitHub API 采样（本文第 2 节）；
  原始读数由 infra-ops 保留，不在公网文档展开内部主机名与凭据。
- GitHub 官方文档：Actions 计费与用量、Actions 限制、GitHub-hosted runners、
  artifact 保留期、secret scanning/push protection、OIDC、rulesets 与 immutable
  releases。
- GitHub 身份模型：账号类型（含 machine user）、GitHub App installation token 与
  audit log。具体条款、seat 影响与 actor 字段需在 D7 spike 时按当时文档核对，本文
  不把“可以给每个 agent 独立身份”当作既有能力。

## 12. 本文件不做什么

- 不修改任何 workflow、runner、secret、分支保护或发布 hold。
- 不在 GitHub 创建分支、PR、issue 或 release。
- 不迁移 issue/PR 历史，不删除/重写已有 tag。
- 不改变 `.forgejo/git-flow.json` 或当前 dev-first 规则。
- 不把 GitHub 的共享账号 + `Submitted-by` 描述成认证或权限边界；D7 只按“可追溯”
  记录，独立 actor 未获批准，若需要则另行立项。
- 不解除 0.5.5.4 的 internal/nightly/formal publication 冻结。
- 不创建私有仓库，不移动或删除任何 denylist 条目下的文件，不改 denylist 本身；W2 的
  去向表是建议，逐条确认前不执行。
- 不改变签名身份的位置、runner 的注册或 KP-100 的状态。
