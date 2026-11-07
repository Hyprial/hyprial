# 设计:socialware 安装改走 release 制品(公网可用、无需 token)

> **已退役（2026-10-03，Allen 裁决；2026-10-13 命令面更新）：代码已删除，现行占位
> `hyprial socialware install` 返回 `COMING_SOON`；旧一级 `hyprial install` 已删除。**
> 整套应用安装体系（catalog、制品下载、manifest、receipt、legacy git 安装、命令挂载、升级）
> 已从 `src/hyprial/daemon/impl/installers/` 删除；`hyprial gui` 改为内建命令，直接使用 wheel
> 内的 GUI 捆绑物。本文正文保留为历史记录，不再描述现行行为。

- 日期:2026-09-14(修订 1:同日,应安装器 owner 审读补 1/补 2/定 3 与 kanban 侧四条意见;
  修订要点:P2 拆 P2a/P2b 消除无人值守升级后的启动断窗 §6/§8、自升级预检与迁移告警 §6、
  `HYPRIAL_SOURCE_COMMIT/VERSION` 注入 §5、fetch-by-ref 单据实更正 §11)
- 设计基线:dev `6dc3fa53`(`src/hyprial/installers/core.py` 771 行)
- spec:hq `notes/spec-socialware-release-install-design-2026-09-14.md`;出处为 Allen 三句原话
  (走 release 免 token 路径 / 「替换,git来源只给开发者用」 / 成员以自己身份操作才自己配 token)
- 本文只是设计,⛔ 不含产品代码。安装器侧 owner:h2b-developer;socialware 侧 owner:devusr。
- **目标状态**:任何成员(含 tailnet 外)无需个人 token 即可 `hyprial install <app>`;安装路径无内网特化。

## §0 托管无关形态(前提与 A/B/C)

**前提读数(infra-op 实测,Forgejo 15.0.5,`REQUIRE_SIGNIN_VIEW=true`,内外网一致,2026-09-14)**:
pypi simple 匿名 401;release 附件匿名 303 跳登录;releases API 匿名 403;仓库页 / raw 匿名 303。
`REQUIRE_SIGNIN_VIEW` 只有全开/全关;package 可见性跟随组织、release 跟随仓库权限
⇒ **Forgejo 原生做不到「只放开 release/package」**。托管候选(infra-op 提,待 Allen 裁):

- **A** 同域名 cloudflared 按路径分流到 GET-only 白名单小代理(服务端注入只读 bot token);
- **B** CI 发布时同步制品到公共存储(R2 / 静态站);
- **C** 关 `REQUIRE_SIGNIN_VIEW`,把不该公开的仓改 private。

**设计的托管无关不变量**:安装器对托管的全部依赖收敛为——

1. **一个匿名可读的 catalog 文档 URL**(HTTPS GET 返回 JSON,§2);
2. **catalog 里每条 app 的一个匿名可读制品 URL + sha256**(HTTPS GET 返回 tar.gz 字节,§1)。

安装器代码里**没有任何按托管方案分支的逻辑**;A/B/C 的差异全部塌缩为两个 URL 前缀
(catalog 文档所在;制品 URL 所在),它们只出现在**一个默认常量**与 **catalog 的数据**里。

**A/B/C 各自对设计的影响**:

| | 对本设计的影响 |
|---|---|
| **A(路径分流小代理)** | URL 保持 `code.hyprial.com` 原生形态:制品 `https://code.hyprial.com/HyprialOS/<repo>/releases/download/<tag>/<file>`,catalog `https://code.hyprial.com/HyprialOS/socialware-registry/raw/branch/main/catalog-v2.json`。代理白名单须覆盖这两类 GET 模式(含 Forgejo 附件下载可能的同域 302)。⚠️ 隧道对**普通单次 GET** 的时延无读数(已有的 217s 读数是 git 按 sha 浅取的协商路径,不可比)——列为 A 的前置读数。 |
| **B(同步到公共存储)** | URL 换 host(如 `https://dl.hyprial.com/...`);发布流程多一个同步步骤(§7),且**发布次序规则**:catalog 只有在制品于公共 URL 上匿名可读并 sha256 核对通过之后才允许更新(否则出现「catalog 指向尚未同步的制品」窗口)。安装器不变——URL 是 catalog 数据。 |
| **C(关全站登录墙)** | Forgejo 原生 URL 直接匿名可读,零新增部件。连带:git clone 也变匿名——**不改变本设计**(git 来源退出正式路径是裁定,不是因为拿不到);「不公开仓改 private」清查时,socialware 各仓 + socialware-registry 必须保持 public,否则 release 附件跟随仓库权限一起不可读。 |

**待裁项**(依赖 A/B/C 选择,⛔ 本设计不自选):
① 默认 catalog URL 常量的值;② catalog 里制品 URL 的 base;③ 选 A 则白名单模式与隧道 GET 时延读数;选 B 则存储位置与同步步骤。
除这四点外,§1–§10 全部与选择无关;实现与测试用本地 file/http fixture 即可先行(§8 P0)。

## §1 制品形状

- **形态**:源码树 tar.gz,`<app>-<version>-src.tar.gz`,顶层单一前缀目录 `<app>-<version>/`。
  由发布 CI 在不可变 tag 上产出:
  `git archive --format=tar.gz --prefix=<app>-<version>/ -o <app>-<version>-src.tar.gz refs/tags/v<version>`。
- **内容** = 该 tag 的完整源码树(仓根 `install.sh`、`hyprial-install.json`、bundles/tools/pyproject 等),
  与 git 路径 checkout 出的树逐字节等价 ⇒ 安装器对**树内容**的契约(manifest 校验、`install.sh` 位置、
  start 脚本校验)**逐字沿用** core.py 现有 `_read_manifest` 语义,零改动。
  devusr 侧 manifest v3(kind/cli)与本设计正交:release 来源字段活在 catalog 与回执里,⛔ 不进 `hyprial-install.json`。
- **摘要**:sha256(tar.gz 全部字节),小写 hex 64 位,发布时算出、记入 catalog。
  release 同时附 `SHA256SUMS` 一份,**只作人读与对账**——信任根是 catalog 里的 pin,
  与 tsnet sidecar 既有原则一致(`tsnet_sidecar.py`:「pinned digest is the trust root, SHA256SUMS is reconciliation only」)。
- **溯源**:catalog 条目带 `commit`(tag 剥离后的 40 hex,发布 CI 记录)。信息性字段,
  不参与校验;安装器把它透传进回执与 `ApplicationLaunch.source_commit`(§3),
  使 `gui_runtime.py:206` 等既有消费者形状不变。
- **不可变性**:`(version, sha256)` 一经发布永不变。重切 ⇒ 换新版本号。
  发布侧由「tag 已存在则拒发」守住;安装侧由「同版本不同 sha256 响亮报错」守住(§5)。

## §2 catalog 与 registry 托管

**catalog v2 文档**(整份 JSON,单 URL GET):

```json
{
  "schema": "hyprial.catalog/v2",
  "apps": {
    "kanban": {
      "release": "https://<待裁 base>/kanban-0.2.0-src.tar.gz",
      "sha256": "<64 hex>",
      "version": "0.2.0",
      "commit": "<40 hex>",
      "manifest": "hyprial-install.json"
    }
  }
}
```

- 条目必备:`release`(绝对 URL)、`sha256`、`version`(PEP 440 可解析)、`commit`、`manifest`(沿用相对路径校验)。
- **release-only**:条目含 `git`/`ref` 键,或缺 release 三件套 ⇒ `INSTALL_CATALOG_INVALID`,
  错误文本指向开发者路径文档(§4)——不是静默忽略(与 `_V2_KEYS` 未知键拒收同一原则)。
- 加载:`load_catalog` 从 git clone 改为**一次 HTTPS GET**(stdlib urllib,超时常量,跟随重定向)。
  catalog 自身不做 sha pin——它是信任根所在,与今日「TLS + host 信任 git clone 的 registry」同级,不降不升。

**registry 位置与过渡**:真源仍是 `socialware-registry` 仓(改动走 PR、人审合并即上线闸),
但消费从 clone 变为 GET 该仓 main 上的**新文件 `catalog-v2.json`**(经 A/B/C 决定的匿名 URL)。
⚠️ 为什么必须是新文件:旧 `load_catalog`(core.py:139-151)对**任何一条**缺 `git/ref/manifest` 的条目
抛错,砸的是**整份 catalog** ⇒ 在旧 `catalog.json` 里就地改写会让所有旧版本安装器瞬间全灭。
新文件让新旧两代安装器在过渡期互不可见;迁移完成(§8 P3)后旧 `catalog.json` 冻结并留一行指路,择期删除。

**`HYPRIAL_INSTALL_REGISTRY` 语义变更**:从「git 仓 URL」变为「catalog 文档地址」——
接受 `https://` URL、`file://` URL 或本地路径。默认值 = 待裁项①。

## §3 回执 schema v2(记录实际地址)

```json
{
  "schema": "hyprial.install-state/v2",
  "name": "kanban",
  "source": {
    "type": "release",
    "url": "<catalog 记载的 release URL>",
    "finalUrl": "<重定向后实际取到字节的 URL;无重定向时与 url 相同>",
    "sha256": "<64 hex,已核对>",
    "version": "0.2.0",
    "commit": "<40 hex>"
  },
  "registry": "<本次实际使用的 catalog 文档地址>",
  "manifest": "hyprial-install.json",
  "sourceManifest": { "path": "source-manifest.json", "sha256": "<64 hex>" },
  "installedAt": "...", "upgradedAt": "...", "previousVersion": "..."
}
```

- **实际地址**三处齐记:registry 文档地址、catalog 声称的制品 URL、重定向后真正取字节的 finalUrl
  ——正是 cc-h2b-monitor 教训(「回执记的是 catalog 地址,事后看不出实际走的哪条路」)的兑现。
  install/upgrade 的命令输出同样打印三者。
- 校验(`_read_install_receipt` v2 分支):sha256/commit 格式、version 可解析、url 为 https/file scheme。
  现 `:691` 的「sourceCommit 必须是 git commit」由 `source.sha256` + `source.commit` 双字段校验取代。
- **既有消费者审计**(dev 6dc3fa53):
  - `installers/mount.py:118-140`:只读 `manifest` 键、不检 schema ⇒ v2 无感,零改动;
  - `cli.py:2884`:只判 `install.json` 是否为文件 ⇒ 零改动;
  - `gui_runtime.py:206`:`process.json` 记 `launch.source_commit` ⇒ v2 由 `source.commit` 填充,形状不变;
  - `home_migration.py:66-67`:迁移清单枚举了 `hyprial.install-state/v1` 字面 ⇒ 实现时补 v2 一行(触点清单项)。
- v1 回执的接受面**按期收窄**(§8 P2a/P2b):
  - **P2a**:启动路径照旧接受 v1 回执(保留既有 git 校验与脏检,app 不断);升级路径读到 v1 ⇒ 路由进迁移(§6);
  - **P2b**(全部在册节点无 v1 回执后才进轨):启动路径遇 v1 ⇒ `INSTALL_MIGRATION_REQUIRED`
    fail-loud 兜底,报错文本给出唯一补救命令 `hyprial upgrade <name>`。

## §4 git 来源的定位(Allen 已裁:替换)

- **正式路径只有 release**:catalog v2 结构上不承载 git 条目(§2)。
- **安装器删除整个 git 面,分两步落地**(次序动机见 §6/§8;终态一致,⛔ 不保留「dev 模式才走」的
  git 分支——那就是被禁的第二套正式逻辑):
  - **P2a 删「取/升」半面**:`_resolve_commit`、`_checkout_exact`、`_is_ancestor`、`_upgrade_relation`
    ——安装与升级从此只有 release 一条路;
  - **P2a 保「验存量」小面**:`_existing_source`/`_installed_source` 的 git 校验、两处
    `git status --porcelain` 脏检、`_run_git`——只为 v1 回执的**启动**继续可用而活,新装/升级不可达;
  - **P2b 删净**:上述保留面 + `_run_git`(⇒ #511 在其中追加的 ssh 应急提示随之消亡,README/skill
    里的对应段落一并删除,列入验收 §9)。
- **开发者路径**(git 来源仅开发者用,单一逻辑):
  1. 日常开发循环照旧:clone + 直接跑仓内 `install.sh`(devusr 今日工作流),不经安装器;
  2. 要端到端过一遍安装器(含命令挂载)时,从自己的 checkout 构建本地制品——
     `git archive` 一条命令(发布 CI 用的同一条,§7,socialware 仓提供同一脚本)、`shasum -a 256`、
     手写一份本地 catalog-v2 JSON、`HYPRIAL_INSTALL_REGISTRY=file:///…/catalog.json`,
     其中 `release` 用 `file://` URL。安装器全程走与正式路径**同一套**取→验→解包→回执逻辑。
  - **边界**:`file://` 制品 URL **仅当 registry 本身经 file:// / 本地路径加载时才接受**;
    https catalog 里出现 file:// ⇒ `INSTALL_CATALOG_INVALID`。理由:远端 catalog 不得把安装器
    指向本机文件系统。
- ⚠️ 若 Allen 对「git来源只给开发者用」的本意是「开发 registry 里保留 `{git,ref}` 条目、安装器保留
  git 机器」:那要求 ~300 行 git 逻辑仅为开发场景存活,构成第二套逻辑,与 spec「⛔ 不做两套正式逻辑」
  相抵 ⇒ 本设计推荐上述 file:// 形态;如需保留 git 条目形态,标为**待裁项⑤**。

## §5 升级与启动检查(无 git 历史)

**关系判定**(取代祖先判定):比较 catalog `(version, sha256)` 与回执 `(version, sha256)`,
版本比较用 PEP 440(`packaging.version`;实现时核对依赖树里是否已有 packaging,没有则加显式依赖):

| catalog vs 回执 | 关系 | 行为 |
|---|---|---|
| 版本等、sha 等 | `current` | 无动作(`alreadyCurrent`;`--force` 可重装) |
| 版本更高 | `upgrade` | 走升级(staged 下载→验→解包→确认→换树,沿用现有 backup/换树/失败回滚机制 :606-663) |
| 版本更低 | `downgrade` | `INSTALL_CATALOG_BEHIND`,`--force` 可越过(语义同今日) |
| 版本等、sha 不等 | `artifact-changed` | `INSTALL_ARTIFACT_CHANGED`,**不可 force**——这是发布侧打破不可变性,补救是发新版本,不是本机硬装 |

旧词汇映射:fast-forward→upgrade、downgrade→downgrade;**diverged 概念消亡**
(无祖先关系可言;最近的邻居是 artifact-changed)。

**脏判定**(取代 `git status --porcelain --untracked-files=no`):

- 解包完成、`install.sh` 运行**之前**,对 staged 树生成 `source-manifest.json`:
  `{"schema": "hyprial.source-manifest/v1", "files": [{"path": "...", "sha256": "...", "exec": bool}, ...]}`
  (排序的相对路径;树内无符号链接——解包已拒收,§1/§9)。文件落在 `apps/<name>/` 与回执并排,
  其 sha256 记入回执(`sourceManifest.sha256`)锁完整性链。
- **dirty ⇔ 清单内任一文件缺失、内容 sha 不符、或 exec 位不符**;**清单外新增文件不算脏**——
  与今日 `--untracked-files=no` 逐语义对齐(install.sh/运行期生成物照旧被忽略)。
- 启动(`prepare_application_launch` :740 处)与升级(:568 处)同用这一判定;成本 = 全树逐文件
  sha256(源码树 MB 级),可接受。
- **为什么清单取自 install.sh 之前**:今日 launch 的 git 脏检在 install.sh 之后仍绿,证明
  install.sh 不改「tracked」文件 ⇒ 前置清单与之等价,且不给 install.sh 留「改了源码但清单已把它
  洗白」的窗口。

**取制品**:stdlib urllib GET(超时、跟随重定向、记录 finalUrl)→ 落 staged 临时文件 → 全文件 sha256
比对 catalog pin,不符 ⇒ `INSTALL_ARTIFACT_DIGEST_MISMATCH`,staged 清理,`source/` 分毫不动 →
安全解包:`tarfile` + Python 3.12 `filter="data"`,并显式拒收 symlink/hardlink/device 成员、
绝对路径与 `..` 穿越、以及「顶层不是单一 `<app>-<version>/` 前缀目录」的畸形包
(`INSTALL_ARTIFACT_MALFORMED`)→ 剥前缀得源码树。
**来源环境注入(定 3,安装器 owner 已裁:注入)**:`_run_install`(core.py:434-435 今日只注入
`HYPRIAL_HOME`)与 `prepare_application_launch` 的 env 增加
`HYPRIAL_SOURCE_COMMIT=<source.commit>`、`HYPRIAL_SOURCE_VERSION=<source.version>`——
release 树无 `.git`,app 的 install.sh / start 脚本今天可能用 `git rev-parse` 取版本
(kanban `install.sh:42-43` 实证在用;gui 等未核,P1 验收逐 app grep,§9),安装器本就持有
这两个值,注入成本极低。install/upgrade/迁移三条路径与启动同注;kanban 侧承诺:有注入优先用
注入,并与 export-subst 烤进制品的 commit 核一致,不一致响亮报(§7)。

新错误码 `INSTALL_ARTIFACT_UNAVAILABLE / _DIGEST_MISMATCH / _MALFORMED / _CHANGED /
INSTALL_MIGRATION_REQUIRED` 与 InstallError 现有码同一命名空间(contracts/ipc_errors.py 无 INSTALL_
条目,不涉注册表守卫;实现时以全量跑确认无其它仓级守卫枚举此码集,⛔ 不以邻域覆盖代替)。

## §6 迁移(已装 git 来源节点)

- **入口唯一**:升级路径读到 v1 回执(git 形)⇒ 迁移计划:
  `{from: {type: git, commit: <回执 sourceCommit>}, to: {type: release, version, url, sha256}}`,
  经 confirm 后执行 = 一次全新 release 安装进 staged + 既有换树机制(backup→swap→install.sh→
  成功删 backup / 失败回退),成功后写 v2 回执。旧树(含 `.git`)随 backup 清理消失。
- **本机改动的处置**:迁移不做 git 脏检(git 面已删)。保护由既有换树机制天然提供——install.sh
  失败则回退旧树;确认提示里写明「本地对源码树的改动将随迁移丢弃」。按今日语义,脏树本来就拒启动,
  丢弃不损失任何受支持状态。
- **实例(HQ kanban)**:回执 v1 `sourceCommit=7ab353ee…` ⇒ v0.2.0 发布且 catalog v2 上线后,
  操作员在该节点跑 `hyprial upgrade kanban`,计划显示 git@7ab353ee → release 0.2.0(URL+sha256),
  确认即迁移。
- **无人值守窗口(补 1;修订 1 重定)**:节点在 03:17/15:17 无人值守自动升级 hyprial
  (devusr-dev 上 16 个 agent 与 kanban 同机)。初版「升级即硬报 `INSTALL_MIGRATION_REQUIRED`
  等人来迁」会让 app 启动在无人在场时断掉——被安装器 owner 打回,按 #515 §2.4 模式改为三件套:
  - **(a) 自升级预检**:`hyprial upgrade --check`(自升级侧)的输出增加
    `pendingAppMigrations: [<name>, ...]` —— 扫 `apps/*/install.json`,列出装上目标版本后
    需要迁移的 v1 回执 app(P2a 起为预告,P2b 起为「不迁则断」的红牌);
  - **(b) 自动升级后告警(write-then-send)**:`hyprial autoupdate run` 完成自升级后,只要存在
    v1 回执,先落**独立 marker 文件**(独立文件名与 `kind`,如 `app-migration-required.json`,
    ⛔ 不复用 `upgrade-restart-failed.json`——`autoupdate/alert.py` 的 marker 是固定文件名整文件
    覆写,混用会互相冲掉),再经 alert.py 既有 owner DM 通道发告警,列出 app 清单与逐条补救命令
    `hyprial upgrade <name>`;沿用该模块三条既有合同:never-raises、送达 messageId 回写 marker
    (`record_alert_outcome`)、**成功不告警**(无 v1 回执 ⇒ 不落 marker 不发信,负控进测试矩阵);
  - **(c) 次序**:题面「catalog v2 上线 + 各节点迁移**先于**带 P2 的 hyprial 进轨」在单段 P2 下
    **不可满足**——迁移代码本身是 P2 的一部分,节点没装上新 hyprial 就没有 `hyprial upgrade <app>`
    可跑,次序是循环的。⇒ 修订为 **P2 拆两段**(§4/§8):P2a(release 路径 + 迁移 + 预检 + 告警,
    v1 启动照旧可用)**不破坏任何存量**,可随轨道无人值守铺开;真正会断启动的 P2b **进轨前置** =
    「catalog v2 已上线,且全部在册节点无 v1 回执」——即把题面次序加在唯一需要它的那一段上,
    且此时它可满足。判据可执行:逐节点 `hyprial upgrade --check` 的 `pendingAppMigrations` 为空
    (节点清单同下;⛔ 不用「过了几天应该都迁了」的时间推断)。
- 硬失败兜底(`INSTALL_MIGRATION_REQUIRED`)保留为 P2b 的边缘路径:漏网节点得到的是带唯一补救
  命令的响亮错,而不是静默跳脏检放行——后者把脏闸对存量安装静默关掉,仍然被否。
  ⛔ 不写回退兼容代码:v1 回执除「P2a 启动可用」与「路由进迁移」外无任何消费。
- 节点清单:P3 runbook 逐节点执行(已知 HQ kanban;其余由各节点 operator 认领),⛔ 不做自动批量。

## §7 发布侧

**socialware 各仓**(kanban、gui/dsh-h2b-talk)新增 `.forgejo/workflows/release.yml`:

- 触发:`on.push.tags: ['v*']`(不可变 tag;与 harness-bridge 轨道 tag 改名单无交集——那边是
  `*-track` 可移动 tag)。
- 步骤(沿用 `wheel-release.yml` 的既有模式):checkout **事件 SHA**(⛔ 不重解析 tag 名)→
  校验 tag 版本 == 仓内声明版本(pyproject 或等价物,不符响亮失败)→ `git archive`(§1 命令)→
  算 sha256、写 `SHA256SUMS` → 经 Forgejo API 建 release 并上传两个附件(凭据 = 仓 secret
  `RELEASE_PUBLISH_TOKEN`,按名引用零值,同 wheel-release 纪律)→ 把 `version/sha256/commit/URL`
  打进 job 输出,供 registry 更新直接抄。
- 并发:按 tag 分组、`cancel-in-progress: false`(半截取消留脏 release,同 wheel-release 理由)。
- **tag 已存在 ⇒ 拒发**(不可变性发布侧的那一半)。
- 选 **B** 时:上传后加同步步骤,且**先证公共 URL 匿名可读并 sha 相符,再动 catalog**(§0 次序规则)。

**registry 更新**:发版后向 socialware-registry 开 PR 改 `catalog-v2.json` 对应条目
(可人工照 job 输出抄,亦可由发布 CI 自动开 PR——推荐后者,留人审合并作为上线闸)。

**对齐 devusr(v0.2.0;四条意见原文在 PR #518 评论,2026-09-14)**:kanban 首个制品 = v0.2.0
release 附件(形态一致:tar.gz 源码树 + 仓根 install.sh;`uv tool install` 不需要 `.git`);
发版时 pyproject `0.2.0.dev1`→`0.2.0`,与「tag 版本 == 仓内声明版本」校验一致,dev 版本不进
release;kanban 以 `.gitattributes` export-subst 把 commit 烤进制品自带溯源,install.sh 有
`.git` 照旧、无则读烤入文件,**有 §5 注入则优先用注入并与烤入值核一致,不一致响亮报**;
kanban 的 `-dirty` 后缀在 release 树里自然消失,由 §5 source-manifest 接管。过渡期旧 catalog
保持 `@main` 不动(新旧文件隔离,§2)。
**P1 前置项**:kanban 仓 secret `RELEASE_PUBLISH_TOKEN` 须在发版 workflow 进仓前配置到位
(按名引用零值;配置归属待定,⛔ 本设计不指派)。
**gui 缺口**:catalog v2 是 release-only,gui 目前无 release ⇒ P1 需要从 `gui-tested` 所指提交
切一个 gui release(owner 认领),否则 gui 暂不进 catalog v2、装不了——列为 P1 交付项。

**hyprial 本体**:不并轨。本体走 wheel 通道(`wheel-release.yml` → Forgejo PyPI registry),
消费者是 uv、制品是 wheel、信任模型是精确 pin 本地段,与「源码树 + install.sh」形态无公共面。
pypi simple 匿名 401 的问题归 A/B/C 同一次裁定顺带考虑,但本设计**不依赖**其结果。

## §8 分期

| 期 | 内容 | 闸 |
|---|---|---|
| **P0 裁定** | Allen 选 A/B/C ⇒ 待裁项①②(两个 URL 前缀)落定;选 A 补隧道 GET 时延读数 | 只挡「真实 URL 常量与 E2E」,不挡 P1/P2a 开工(fixture 先行) |
| **P1 发布侧** | socialware 各仓 release workflow(前置:`RELEASE_PUBLISH_TOKEN` 配置到位,§7);kanban v0.2.0 制品;gui 首个 release;`catalog-v2.json` 建档;逐 app grep install.sh/start 对 `.git` 的依赖(§9.11) | 匿名 `curl` 两制品 + catalog URL,sha256 全部相符 |
| **P2a 安装器(不破坏存量)** | harness-bridge:catalog v2 loader、取/验/解包、回执 v2、升级/脏判定、迁移入口、来源 env 注入(§5)、自升级预检 (a) 与迁移告警 (b)(§6)、git「取/升」半面删除;v1 启动照旧可用 | 焦点 + 全量 pytest 绿;全仓 ruff;测试矩阵(§10)对应行全绿;可随轨道无人值守铺开 |
| **P3 切换与迁移** | catalog v2 填真实 URL 上线;各节点按 (b) 告警/runbook 跑 `hyprial upgrade <app>` 迁移(HQ kanban 在册);旧 catalog.json 冻结指路 | 匿名环境 E2E 装通;逐节点 `upgrade --check` 的 `pendingAppMigrations` 为空 |
| **P2b 安装器(删净 git 面)** | 删 §4 保留面 + `_run_git`;`INSTALL_MIGRATION_REQUIRED` 兜底;#511 提示与 README/skill 段落移除 | **进轨前置 = P3 完成**(catalog v2 已上线且全部在册节点无 v1 回执,§6c);ssh 应急提示三处 grep 为零 |

P1 与 P2a 可并行;P3 在 P0/P1/P2a 之后;P2b 最后,其进轨次序是补 1(c) 的答案(§6)。
协调项:已开的 #511(`fix/installer-timeout-hint`,改 `_run_git`)落在 P2b 将删除的面上——
先合后删,不冲突;P2a/P2b 实施单开工时按届时 dev 头重读该处。

## §9 验收判据草案(含负例)

1. **匿名可装**:干净环境(无 token、无 tailnet、无 git 凭据)默认 registry 下
   `hyprial install kanban --yes` 成功;回执含 registry/url/finalUrl/sha256/version/commit。
   (真实 URL 部分依赖 P0;彩排版用本地静态 http fixture,先行。)
2. **负例 · sha256 不符**:篡改一个字节的制品 ⇒ `INSTALL_ARTIFACT_DIGEST_MISMATCH`,
   `apps/<name>/source/` 不存在或原样未动,无残留 staged 目录。
3. **负例 · 路径穿越/链接**:含 `../x`、绝对路径、symlink、hardlink 成员的 tar 各自被拒
   (`INSTALL_ARTIFACT_MALFORMED`),零文件落盘到树外。
4. **负例 · git 条目**:catalog 条目含 `git/ref` ⇒ `INSTALL_CATALOG_INVALID`,错误文本指向开发者文档。
5. **升级矩阵**:高版本升级成功且换树原子;同版本同 sha `alreadyCurrent`;低版本
   `INSTALL_CATALOG_BEHIND` 且 `--force` 可越;同版本异 sha `INSTALL_ARTIFACT_CHANGED` 且 force 无效。
6. **脏判定**:改清单内文件一字节 ⇒ 启动与升级均拒(`INSTALL_SOURCE_DIRTY`);
   **负例**:树内新增清单外文件 ⇒ 不脏,照常启动(与 git `--untracked-files=no` 逐语义对齐)。
7. **迁移**:v1 git 回执节点跑 `hyprial upgrade` ⇒ 计划显示 git→release,完成后回执 v2、
   `source/.git` 不存在;**P2a 负例**:v1 回执 start 照旧可用、行为与修前一致(不断);
   **P2b 负例**:v1 回执 start ⇒ `INSTALL_MIGRATION_REQUIRED` 并给出命令。
8. **开发者边界负例**:https catalog 含 `file://` 制品 URL ⇒ 拒;file registry + file 制品 ⇒ 通,
   走与正式路径同一套代码(以覆盖率或调用探针证同一)。
9. **删 ssh 应急提示**(spec 列入;P2b):release 路径落地后,#511 引入的提示在
   `installers/core.py`、README、`hyprial-ops` SKILL 三处 `grep -rn "insteadOf"` 为零;
   `test_readme_and_ops_skill_quote_the_same_ssh_fast_path` 等相应测试同步删除。
10. **发布侧**:tag 与仓内版本不符 ⇒ workflow 响亮失败;重复 tag 拒发;SHA256SUMS 与附件实测相符。
11. **无 .git 依赖清查(定 3,P1)**:逐 app grep install.sh / start 脚本对 `.git`/`git ` 的依赖并
    给出处置(kanban 已知 `install.sh:42-43`,devusr 侧以 export-subst 处置;gui 等待核);
    注入验证:`_run_install` 与启动 env 含 `HYPRIAL_SOURCE_COMMIT/VERSION` 且值等于回执字段。
12. **无人值守三件套(补 1)**:`upgrade --check` 列出 `pendingAppMigrations`;自动升级后有 v1 回执 ⇒
    独立 marker 落盘在发信之前、送达 messageId 回写;**负控**:无 v1 回执 ⇒ 不落 marker、不发信;
    marker 文件名/kind 与 `upgrade-restart-failed.json` 不同,互不覆写。

## §10 测试 / 变异矩阵(本单 docs-only:全部 **NOT_RUN**,交 P2a/P2b 实施单执行)

桩纪律:catalog fixture 必须是 §2 的真实 v2 形状、制品 fixture 必须是真 `git archive`/`tarfile`
产物,⛔ 不自造形态。变异验证按「红过绿回须重跑当初那一次突变、整文件跑、⛔ 不加 `-k`」执行。

| # | 测试(落在 tests/test_installers*.py 族;15 归 P2b,19–24 归 P2a) | 使其必红的突变 | 状态 |
|---|---|---|---|
| 1 | catalog v2 正常解析,条目字段齐 | loader 忽略 `schema` 字段 | NOT_RUN |
| 2 | 含 `git/ref` 条目 ⇒ INSTALL_CATALOG_INVALID | 条目形状检查跳过未知键 | NOT_RUN |
| 3 | sha256 相符才解包 | 删除摘要比对 | NOT_RUN |
| 4 | 摘要不符时 `source/` 未动、staged 已清 | 比对移到换树之后 | NOT_RUN |
| 5 | `../` 穿越成员被拒 | 解包 filter 降为 `tar` | NOT_RUN |
| 6 | symlink/hardlink 成员被拒 | 删显式成员类型检查 | NOT_RUN |
| 7 | 非单一前缀根的畸形包被拒 | 剥前缀改为取第一个成员的首段 | NOT_RUN |
| 8 | 回执记 registry/url/finalUrl 三地址 | finalUrl 回填为 catalog url | NOT_RUN |
| 9 | 升级关系矩阵四格(§5 表) | 版本比较翻转 / 相等误判 | NOT_RUN |
| 10 | 同版本异 sha 不可 force | force 分支放行 artifact-changed | NOT_RUN |
| 11 | 改清单内文件 ⇒ 拒启动 | 脏判定只比文件存在性不比内容 | NOT_RUN |
| 12 | 新增清单外文件 ⇒ 不脏(负例) | 脏判定把新增计为脏 | NOT_RUN |
| 13 | exec 位翻转 ⇒ 脏 | 清单不记 exec 位 | NOT_RUN |
| 14 | v1 回执 ⇒ 升级路由进迁移,产 v2 回执、无 `.git` | 迁移后仍写 v1 schema | NOT_RUN |
| 15 | 【P2b】v1 回执 start ⇒ INSTALL_MIGRATION_REQUIRED | 启动路径静默放行 v1 | NOT_RUN |
| 16 | https catalog 携 file:// 制品 ⇒ 拒 | scheme 边界检查删除 | NOT_RUN |
| 17 | v2 回执下命令挂载照常(mount 无感) | 回执 `manifest` 键改名不同步 mount | NOT_RUN |
| 18 | 发布 workflow:tag≠版本 ⇒ 失败(CI 侧彩排) | 校验步删除 | NOT_RUN |
| 19 | 【P2a】v1 回执 start 行为与修前逐字段一致(负例) | P2a 启动路径也报 MIGRATION_REQUIRED(提早破坏) | NOT_RUN |
| 20 | `upgrade --check` 列出全部 v1 回执 app | 扫描分支恒回空列表 | NOT_RUN |
| 21 | 自动升级后有 v1 ⇒ marker 先落盘、后发信、messageId 回写 | 次序倒置为 send-then-write | NOT_RUN |
| 22 | 无 v1 回执 ⇒ 不落 marker、不发信(负控) | 告警分支无条件执行 | NOT_RUN |
| 23 | 迁移告警 marker 文件名/kind 独立,与 restart-failed 互不覆写 | 复用 `upgrade-restart-failed.json` 文件名 | NOT_RUN |
| 24 | install 与启动 env 含 `HYPRIAL_SOURCE_COMMIT/VERSION` 且等于回执值 | 注入删除,或值取自 catalog 而非回执 | NOT_RUN |

## §11 债务 / 绊线 / 已知无关红

- 上一单留给本单的债:**无**(原 astra 会话未开工,工作树 = 干净 dev 6dc3fa53)。
- 绊线:①#511(open,`fix/installer-timeout-hint`)改 P2b 将删除的 `_run_git`,先合后删、不冲突,
  实施单开工按届时 dev 头重读;②(修订 1 更正)`fix/installer-fetch-by-ref` 那一单已以
  SPEC_DISAGREEMENT 结束,**没有代码、没有 PR、没有分支**——实施者⛔ 不要去找它;其 spec 里的
  隧道计时读数(git 按 sha 浅取 ~120s+)仍可引用;③`home_migration.py` 的 v1 字面清单(§3);
  ④tsnet sidecar 的「no HTTP」裁定(Ruling E, 2026-09-05)**限定 sidecar 二进制分发**,
  与本设计的 HTTP 匿名取制品不同对象、不冲突。
- 已知无关红:**无**(docs-only,本单只跑全仓 ruff)。
