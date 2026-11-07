# 公网快照从白名单改为黑名单（denylist）方案

- 日期：2026-11-03
- 状态：**已裁定**（Allen 2026-11-03「同意黑名单方案，另开 PR 实现」）；实现见 §8；
  安全评审 REJECT 后的修复见 §9 与 §10（§8 的结论有误，以 §9/§10 为准）
- 起因：Allen 2026-11-03「我建议按照黑名单的方式去设计，即除了明确不应该暴露到公网的
  信息，其他代码内容都应该同步到公网 Github 开源（但标注公网 Github Repo 只是内网镜像，
  暂时不接受外部 PR）」
- 关联：HyprialOS/harness-bridge#2327（README 重写）；现行实现
  `scripts/ci/github_publish.py`、`.forgejo/workflows/github-publish.yml`、
  `.forgejo/workflows/github-publish-allowlist.txt`

## 1. 现状

`github-publish` 只把 `SNAPSHOT_WHITELIST` 中的路径推到 `Hyprial/hyprial`：

```text
src/  sidecar/hyprial-tailcat/{go.mod,go.sum,cmd/,internal/}
pyproject.toml  uv.lock  VERSION  README.md  LICENSE  CONTRIBUTING.md
```

再减去 `SNAPSHOT_EXCLUDE`（`src/gui/{docs,vendor,tests}/`）与 `SNAPSHOT_EXCLUDE_GLOBS`
（Tailcat 的 `*_test.go`、`testdata/`）。快照约 234 个文件，经两道扫描闸（gitleaks 8.30.1、
`INTRANET_RULES` 内网标记 grep + allowlist）和 wheel 可安装性闸后才推送。

后果：

1. `README.md` 公开，但它链接的 `docs/`、`ROADMAP.md`、`GLOSSARY.md`、`contract/`、
   `CHANGELOG.md` 都不公开，公网读者看到的链接全部 404。
2. 根 `gui/`（当前唯一的 GUI 主线）、`tests/`、`contract/` 不公开——公网仓不能独立构建 GUI，
   也看不到测试与契约，"开源"名不副实。
3. 每新增一个应公开的目录都要改白名单常量和测试，默认是"不公开"。

## 2. 目标

- **默认公开**：除黑名单外，仓库内容全部进入公网快照。
- **黑名单只放"明确不应公开"的内容**，每条写明理由与裁定人。
- **现有两道扫描闸与 wheel 闸全部保留**，并继续 fail-closed：新增目录里的命中照样判红。
- **公网仓明确是只读镜像**：README 顶部声明，且公网 PR 不会被合并（见 §6）。
- 快照仍是**单个 bot commit**（`BOT_COMMIT_IDENT`），不带内网 git 历史；这一点不变。

## 3. 实测：候选黑名单下的快照（dev `c3f4129ad`）

度量方法：`git archive HEAD` 排除 §4 的 Deny 项，解包后跑与 CI 同版本、同 sha256 的
gitleaks 8.30.1（`gitleaks dir`），再用 `INTRANET_RULES` 的四条正则逐文件计数。

| 项 | 现白名单 | 候选黑名单 |
| --- | --- | --- |
| 文件数 | ~234 | 4882 |
| 体积 | — | 62 MB（解包后） |
| gitleaks 命中 | 0（现网门绿） | 47，全部 `generic-api-key` |
| 内网标记命中 | 已 allowlist 的 4 个文件 | 86 个文件（git-host 74 / ts.net 66 / CGNAT 100 / client-id 4） |

gitleaks 47 条按目录：`scripts/refactor` 13、`tests/daemon` 9、`contract/sidecar-tailcat` 8、
`tests/fixtures` 8、`tests/identity` 3，其余各 1。逐条看过匹配文本，全部是测试合成值
（`nodekey:0123…`、`SENTINEL_API_KEY_1234`、`A1b2C3d4…`、文件 SHA-256 等）。
**例外待人工确认**：`tests/fixtures/host-invite-bd6eb960/database.sql` 含
`entityToken`/`resourceToken` 形状的 32 位 hex，需确认是合成数据而非真实库导出。

对照：被排除的 WORM 区内网标记集中度很高——`docs/evidence` 3845 个文件 / 285 MB，
157 个文件命中（git-host 902、ts.net 587、CGNAT 462、client-id 68）；`docs/notes`
2504 个文件，106 个文件命中。这两处只能整体排除，不能逐条豁免。

## 4. 候选黑名单

### Deny（整目录不公开）

| 路径 | 理由 |
| --- | --- |
| `docs/evidence/` | 评审原始证据，WORM 原字节保存，含大量内网主机、tailnet 地址、人员邮箱与 Lark client id；体积 285 MB |
| `docs/notes/` | 证据与复现材料（WORM，只追加），同上 |
| `docs/handoff/` | 内部交接记录 |
| `gui/evidence/` | GUI 验收证据截图与记录 |
| `.forgejo/` | 内网 CI 定义，含内网 runner、主机与发布凭据名；在 GitHub 上也不会运行 |
| `.idea/` | IDE 本地配置 |
| `scripts/archived/` | 已退役脚本，含内网主机 |
| `src/gui/{docs,vendor,tests}/`（保留） | 现有 `SNAPSHOT_EXCLUDE`，Allen 2026-09-16 裁定 |
| Tailcat `*_test.go`、`testdata/`（保留） | 现有 `SNAPSHOT_EXCLUDE_GLOBS`，Allen 2026-09-16 裁定 |

### Review（需 owner 逐项确认，默认先 Deny）

| 路径 | 待确认点 |
| --- | --- |
| `.agents/skills/forgejo-ops/` | 内网 Forgejo 操作手册，是否对外有价值 |
| `edge/` | Cloudflare Worker 配置；扫描 0 命中，但需确认 `wrangler` 配置里没有账号 ID / 路由需要保密 |
| `tests/fixtures/host-invite-bd6eb960/` | 见 §3，确认是否为合成数据 |

### 公开后需处理的 86 个内网标记文件

主要分布在 `tests/`（daemon / shell / identity / integration / tooling / fixtures）、
`contract/e2e/`、`docs/design/`、`docs/guides/`、`sidecar/`、`.agents/skills/`、`scripts/ci/`。
处理顺序：

1. **优先改写成示例值**：测试 fixture 里的 `*.ts.net` 主机、`100.64.0.0/10` 地址改成
   `example.ts.net` 以外的 RFC 2606 / RFC 5737 示例（如 `node-one.example`、`192.0.2.x`）。
   这能顺带减少测试对真实 tailnet 名的耦合。
2. **确属历史叙述的**（设计文档里的旧内网 clone 地址）：进 allowlist，写明理由。
3. **无法处理的单个文件**：加入 Deny，并在本文追加一行理由。

## 5. 实现草图（第二个 PR）

改动集中在 `scripts/ci/github_publish.py`，保持"一处常量"原则：

1. 用 `SNAPSHOT_DENYLIST`（目录前缀）+ 现有 `SNAPSHOT_EXCLUDE_GLOBS` 取代
   `SNAPSHOT_WHITELIST`；`SNAPSHOT_EXCLUDE` 并入 `SNAPSHOT_DENYLIST`。
2. `build_snapshot` 改为 `git archive <rev> -- . ':(exclude)<deny>'…`；
   **保留 `excluded_entries` 解包后复查**（pathspec 被静默忽略时 fail-closed 的那道防线）。
3. 保留"必需文件"检查：`src/`、`pyproject.toml`、`uv.lock`、`VERSION`、`README.md`、
   `LICENSE` 缺失仍判红（改名为 `SNAPSHOT_REQUIRED`）。
4. 新增"敏感路径守卫"测试：`docs/evidence/`、`docs/notes/`、`.forgejo/` 等 Deny 项
   **必须**在黑名单里，删掉任何一项都会让测试变红（防止误删黑名单而直接公开）。
5. `github-publish-allowlist.txt` 按 §4 结果更新；`stale` 判红规则保留。中文 README
   `docs/i18n/README.zh-CN.md` 与英文 README 一样保留两处 `git.internal.example.com`
   （v0.4.0 历史恢复、开发 clone），需要同样的 `intranet-git-host` 豁免条目。
6. 更新 `tests/tooling/release/publish/github/test_snapshot.py` 与
   `test_security.py`：快照文件数、必需项、Deny 项三类断言。
7. 发布预算：gitleaks 扫描 46.6 MB 实测 1.9 s，不需要调整 job 超时；`git archive`
   体积从 KB 级到 62 MB，需在一次 `workflow_dispatch` dry-run 中实测推送耗时。

上线顺序：先 `dry_run=true` 生成快照清单与两闸读数，经人工复核清单后，再做第一次真实推送。
**公开是不可撤回的**——推到 GitHub 后即使删除也可能已被抓取，所以第一次推送前的清单复核
不能省。

## 6. 公网仓"只读镜像"声明

- `README.md` 顶部提示：公网仓是内部 Forgejo 的发布镜像，暂不接受外部 PR；问题请走
  GitHub Issues。（本 PR 已写入。）
- 可选：在公网快照中额外生成 `.github/PULL_REQUEST_TEMPLATE.md` 说明同一点；或在 GitHub
  仓库设置里关闭 PR（GitHub 目前不支持完全关闭 PR，可用自动关闭的 workflow 实现）。
  二者都只影响公网仓，不进入内网仓库。

## 7. 待裁定

1. §4 Deny / Review 列表是否同意。
2. `tests/` 是否公开（建议公开：开源项目的测试是可信度的一部分；代价是先处理约 70 个
   fixture 文件的内网标记）。
3. 人员邮箱（`*@hyprial.com`，`docs/design/` 101 处，多为 agent URI）是否需要新增一条
   `INTRANET_RULES`。现有四条规则不覆盖邮箱。
4. 是否在第一次真实推送前由第二人复核快照清单。

## 8. 实现记录（2026-11-03）

实现分支 `hotfix/public-snapshot-denylist`，叠在 HyprialOS/harness-bridge#2327 之上。§7 的四个问题按本文建议落地：

1. **Deny / Review 列表**：§4 的 Deny 与 Review 三项全部进入 `SNAPSHOT_DENYLIST`；
   另外加入 `contract/e2e/`（真实环境 E2E，含内网主机与凭据引用）。
2. **`tests/` 公开**。gitleaks 命中的测试文件按 2026-09-16 先例**整文件排除**，不做逐条豁免
   （gitleaks 门本身没有豁免机制）。被其他测试 import 的两个 `support.py` 改用低熵的合成值
   （`img_v2_fwd`、`img_v3_fwd`），保留在公网树里。
3. **人员邮箱规则本次不加**。现有四条 `INTRANET_RULES` 不变，邮箱规则另行裁定。
4. **首推前复核**：第一次真实推送前先做 `workflow_dispatch` dry-run，复核快照清单。

公开前替换掉的真实标识（只改测试与文档；`src/` 中已有 allowlist 的两处 docstring 不动）：

- tailnet 名 → `example-tailnet`；个人设备名 → `member-laptop`；
- 5 个真实节点地址 → `100.64.30.1`–`100.64.30.5`（保持原有数值顺序）。

其余 88 个 (文件, 规则) 命中都是合成数据或内网 git 主机名，已逐条写入 allowlist，并注明理由。

**新增防线**：`.gitleaks.toml` / `.gitleaksignore` 在任何深度都不进快照。gitleaks 会读取被扫描目录里的
这两个配置文件；如果不排除，发布树自带一个配置就能关掉扫描门。

**顺带修复**：wheel 构建钩子要从干净的 Git 树打包 GUI，旧白名单快照既没有 `gui/` 也没有 `.git`，
在本地复现时 `uv build` 报 `GUI project is missing`。installability 闸改为在快照的 git 化副本里构建
（`github_publish.py wheel-tree`，origin 为公网镜像）。发布步骤仍从源 commit 重新抽取原始快照。

> 2026-11-06 合入 0.5.5.3.4 时修订：0.5.5.3.2/0.5.5.3.3 已用 GUI 溯源戳（`gui/gui-source.json`，
> `github_publish_policy/guistamp.py`）解决了无 `.git` 快照构建 GUI 的问题，且 wheel 记录的是可追溯的
> 源提交；wheel-tree 记录的是一次性 bot 提交、并绕过溯源戳复核，两者重复，故删除 wheel-tree，
> installability 闸恢复为直接在快照里 `uv build`。

本地全链路实测（快照 → 内网标记闸 → gitleaks 8.30.1 → wheel-tree → `uv build --wheel`）：

- 快照 4660 个文件 / 45.2 MB；
- 内网标记 0 处未豁免、233 处已豁免、0 条失效；gitleaks 0 条；
- 构建出 `hyprial-0.5.5.3.1-py3-none-manylinux_2_17_x86_64` wheel，含 Tailcat 与 GUI 资源。

## 9. 安全评审 REJECT 与修复（2026-11-03）

评审在本地构建并扫描了快照（没有发布），提出 5 个阻断项。§8 的做法确实不够：

1. **真实 tailnet 节点和设备名**：§8 只替换了 tailnet 后缀，节点名原样保留。
2. **员工真实邮箱**：§8 把邮箱规则推迟了，结果缺口一直开着。
3. **真实飞书 App ID**：§8 没有盘点这一类。
4. **内网 git 主机**：§8 用 28 条豁免放行了它。
5. **豁免按「文件 + 规则」生效**：同一文件里新增的真实标识会被一并放行。评审用反例验证过：在已豁免的文件里加一行真实主机名，门禁 0 报错。

修复（裁定：`update_source.py` 由 infra-ops/upgrade-dl-channel 处理，`context.py` 由 infra-ops/agent-git-identity 处理，#2330 都不改它们）：

- **规则**：新增 `staff-email`、`mdns-device-name`、`lark-app-id` 三条规则。主机名和邮箱类规则改为不区分大小写：复核时发现大写写法的内网主机名漏过了扫描。
- **私有标记**：真实节点名、设备名和个人 handle 不能写进公开的正则（规则本身就会把它们公开），所以放在 `.forgejo/workflows/github-publish-private-markers.txt`（不进快照）。命中这一类的规则叫 `private-marker`，且不可豁免。
- **路径也扫描**：名字可能出现在文件名里；路径命中同样不可豁免。
- **豁免按行生效**：格式为 `path :: rule :: line-sha256-16 :: reason`。一条豁免只覆盖一种行内容；改动这一行或新增一行都会重新报 HIT；失效条目判红；旧的整文件格式直接报配置错误。
- **清理（保持长度、全局一致）**：
  - 员工邮箱：本地部分映射为同长度的合成名，域名改为 `example.com`；邮箱派生出的 slug 与 URL 编码形式同步替换。
  - 节点名、设备名、handle：映射为同长度的占位。
  - App ID：改为 `cli_<...>` 占位。
  - 测试里的内网 git 主机：改为从 `FORGEJO_GIT_URL` 推导，只断言形状。
  - README：改为公网地址。
- **不能改写的内容整体排除**：
  - 签名向量及其设备目录（换值就得重新签名）；
  - 逐字节钉住的原始 prompt（`docs/design/gui-reference-slice/`）；
  - 加载 `contract/e2e` 的测试；
  - 内网 CI 工具与它的测试（`scripts/ci/`、`tests/tooling/ci/`、`tests/tooling/release/`）。

结果（本地，与 CI 同流程）：

- 快照 4467 个文件；
- 内网标记 **1 处**未豁免：`context.py` 的 `staff-email`，按裁定不豁免，等 agent-git-identity 合入后消失；
- 144 处由 128 条按行豁免覆盖：`update_source.py` 2 条，其余是合成的 CGNAT / ts.net 测试值。合成 IP 已逐个与 `docs/notes`、`docs/evidence` 中出现过的地址核对，重合的都是测试代码副本。
- gitleaks 0 条；wheel 构建成功。

未处理、待裁定：`alicesmith`（owner 本人的 handle）、`bobadmin`（HQ 机器账号）、`mate`（出现在真实存在的分支名里）仍在公开树里。

## 10. 复审第 2 轮（REJECT）与修复（2026-11-03）

复审确认第 1 轮的真实节点、设备与 tailnet 标识、员工邮箱、slug、App ID 已清零；按行豁免经得起错行、错文件、错规则与失效条目的测试；private markers 不进快照且不可豁免；gitleaks 为 0。新提出的问题与修复：

1. **真实基础设施被当作「合成值」豁免**：`docs/design/service-connect/` 写有真实的 MagicDNS 域名、`sub2api` 服务地址、HQ 地址和 DERP 公网 IP。修复：
   - 整个目录放进黑名单，删除相关豁免；
   - 另一份文档里对旧 MagicDNS 域名的引用改为占位；
   - 域名和 DERP IP 加入私有标记清单。
   - 新规则：**快照中的任何 CGNAT 地址都不得出现在 `docs/notes` 或 `docs/evidence` 中**。重叠的 10 个地址连同其余测试地址一起把第三段加 20，保持原有顺序；由 `test_no_published_cgnat_address_appears_in_internal_evidence` 强制执行（在旧 head 上失败：10 个重叠；新 head 上通过）。
2. **`update_source.py`**：删除它的两条豁免，门禁在这一处保持红色，直到 infra-ops/upgrade-dl-channel 合入。
3. **`context.py`**：保持现状，不豁免；它里面的 owner handle 同时命中私有标记，同样记为 pending。
4. **规则缺口**：`mdns-device-name` 改为不区分大小写；`staff-email` 可以识别 URL 编码的 `%40`。两处都先写失败测试再修。
5. **三个 handle**（Allen 裁定，名单见不公开的 private-markers）：分别换成 `alicesmith`、`bobadmin`、`mate`；保持长度并全局一致，加入私有标记清单。
   - 提到以成员昵称命名的真实集成分支的地方，改为占位写法 `intg/<member>-gui`；
   - 该分支的使用规则文档（`docs/domains/` 下）整份放进黑名单。

结果（本地）：快照 4456 个文件；内网标记门只剩 4 处 pending（`context.py` 2 处、`update_source.py` 2 处），140 处由 124 条按行豁免覆盖，0 条失效。§9 中写的 4467 个文件是过时的数字。

