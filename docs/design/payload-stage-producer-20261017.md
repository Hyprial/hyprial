# 公共 production stage：payload 输入锁与可推导生产者（第一切片）

- 任务：PAC `wf-d18b03cb62074e8db628b8b91946b7e9` / node `core-prod-stage-r1`（gui-coordinator 派单）
- 基线：`intg/gui-app-core-20261008 @ a14d97c59b59776f80f3b2cca1ff711bf6f63250`（开工 ls-remote 实测）
- 分支/worktree：`work/core-prod-stage-20261017` / `.worktrees/core-prod-stage-20261017`
- 边界：不获取/不入库外部输入；不读凭据；不启重型 Rust；不预占 D1/D2；L1 待并 linux 分支重叠项不重复实现。

## 一、目标与非目标

目标：让"prepared runtime 是外部输入"这一事实在仓库内有机制可依——
**输入锁（哪些输入、什么摘要）+ 可推导生产者（Node 发行版布局）+ 输入校验门（缺/未锁/过期即命名失败）**。

非目标：不在此切片重建 CPython / site-packages / 平台原型 / Claude SDK 树；不下载任何外部字节；
不把锁的摘要手工填写（`digest: null` 直到真实构建机生成）。

## 二、现状事实（引证，均在当前基线实测）

| 事实 | 证据 |
| --- | --- |
| prepared runtime 是显式外部输入；迁移需记录完整输入归档哈希 | `app/release/README.md:22` |
| stage root 的消费面：python/、site-packages/、source/src/、node*/<dist>（npm+node）、`*-claude-sdk/` | `app/scripts/stage-runtime.cjs:16-23` |
| Node 发行版已有仓库内钉定：名称、sha256、归档内 entry | `app/src/components/catalog.cjs:7-10`（NODE_ASSETS，darwin-arm64/win32-x64） |
| Python 覆盖路径为仓库可推导（overlay 写入 source/src） | `app/scripts/stage-python-source.cjs:47-68` |
| 发布构建 host 门仅 win32 x64 / darwin arm64 | `app/scripts/build-release.cjs:33`；workflow build-tauri 仅 macos-host（`.forgejo/workflows/desktop-release.yml`） |

## 三、生产者边界（哪些输入可收归仓库）

| 输入 | 类别 | 说明与处置 |
| --- | --- | --- |
| Node 发行版（node*/<dist>） | **可推导（本切片）** | 字节仍是外部输入，但仓库已钉 sha256+entry；生产者 `stage-node-dist.cjs` 先验后解，产出布局并回报 treeDigest 供锁记录 |
| bundled-cli.py / bundled-sdk-worker.py / extract-component.py / network worker | 仓库内 | 已由 `stage-runtime.cjs:32-34,50` 从仓库拷入；无需锁（随提交评审） |
| Python overlay（9 个文件） | 仓库内 | `stage-python-source.cjs` 在平台原型上覆盖；随提交评审 |
| 可重定位 CPython 树（python/） | **恒为外部输入** | 构建产物（体积/平台绑定），不入库；锁记录 treeDigest |
| site-packages/ | **恒为外部输入** | 由锁定依赖 `--target` 安装产生；锁记录 treeDigest |
| 平台原型 source/src（除 overlay 外） | **恒为外部输入** | 平台适配层；锁记录 treeDigest |
| Claude SDK 树（*-claude-sdk/） | **恒为外部输入** | 许可/体积；锁记录 treeDigest |

## 四、布局契约（与 stage-runtime.cjs 一致）

| stage root 路径 | 内容 | 来源 | 校验方式 |
| --- | --- | --- | --- |
| `python/` | 可重定位 CPython 树 | 外部 | 锁 treeDigest |
| `site-packages/` | 锁定依赖安装 | 外部 | 锁 treeDigest |
| `source/src/` | 平台原型 + overlay | 外部 + 仓库 overlay | 锁 treeDigest（overlay 先于锁生成） |
| `node*/<dist>/`（mac=`node/`，win=`node24/`） | 官方 Node 发行版 | 外部归档 + 生产者解包 | 归档 sha256≡catalog；解包后 treeDigest 入锁 |
| `*-claude-sdk/` | Claude SDK 树 | 外部 | 锁 treeDigest |
| `installer/npm`、`node/bin/node` | **派生**（取自 <dist>） | 同 Node 发行版 | 随 <dist> 一并锁定，不单列 |

## 五、哈希锁格式与工具

- 锁文件：`app/release/payload-lock.json`（`schemaVersion: 1`）。每 target：`node{asset,sha256,entry,distDigest,source}` + `inputs{name:{path,kind,source,digest}}`。
- 工具：`app/scripts/payload-lock.cjs`
  - `check`（无需 stage 树）：schema、全 target、**锁↔catalog 漂移**（`NODE_ASSET_DRIFT`）；输出未锁清单。
  - `generate <stageRoot> <target>`：从真实字节记录摘要；外部输入缺失即失败（**不写半份锁**）。
  - `verify <stageRoot> <target> [--external-only]`：逐项校验；错误命名：
    `MISSING_STAGE_INPUT` / `UNLOCKED_STAGE_INPUT` / `STALE_STAGE_INPUT` / `MISSING_NODE_DIST` / `UNLOCKED_NODE_DIST` / `STALE_NODE_DIST` / `MISSING_NODE_ENTRY` / `NODE_ASSET_DRIFT`。
- 工具：`app/scripts/stage-node-dist.cjs`（生产者）
  - 先跑门（锁必填：`MISSING_PAYLOAD_LOCK`；外部输入缺/未锁/过期即失败且**不产出**），再验归档（`NODE_ASSET_MISMATCH`）、解包（tar.gz→tar；zip→unzip；其余 `UNSUPPORTED_ARCHIVE_FORMAT`）、拒绝覆盖（`REFUSING_OVERWRITE`），回报 `distDigest`。
- npm 脚本：`npm run lock:check|lock:generate|lock:verify|stage:node`。
- 真实摘要只能来自持有输入树的构建机；本节点（Mini）无真实输入树，**未生成真实摘要**——锁内 `digest: null` 是如实状态，不是通过。

## 六、输入校验门（CI/构建消费）

- CI（无树）：`npm run lock:check` → 结构+漂移门；`unlocked` 清单显式输出。
- 构建机（有树）：`stage:node`（先门后产）→ `lock:generate`（回填 distDigest）→ `lock:verify` 全量通过，方可 `npm run dist`。
- 失败即具名错误原文，不静默通过；无跳过开关（本切片不提供）。

## 七、与 D1/D2 的关系

- **D1（载荷自带官方 Node）** 已在仓库记录（`stage-runtime.cjs:20`、`build-release.cjs:69`、`runtime-paths.test.cjs:46`）。本切片让 D1 的字节第一次**可验证**（catalog 钉定 + 归档先验后解 + distDigest 入锁），不重开 D1。
- **D2（更新平面经载荷 Node 运行，单一实现）**（`tauri-bundle.cjs:6`、`update-tool.test.cjs:2`）。锁使"载荷 Node 从哪个发行版来"可追溯，不重开 D2。
- 仓库内未发现其它 `Decision D2` 之外的 D1/D2 编号（grep 全仓）；派单所引 preflight 报告在本节点不可达（见 §九），如其中另有 D1/D2 语义请 coordinator 指明。

## 八、待决项（选项+建议，交 coordinator/代理决策，不在本切片定案）

1. **锁的强制点**：CI 合并时只 `check`（无树），构建机 `verify` 强制——建议如此；若要求 CI 强制 `verify`，需先有可复用的构建工件缓存（另切片）。
2. **CPython/site-packages 是否改为仓库内可重建**（uv lock + 脚本重装）——体积/许可/可重复性权衡；建议先锁后重建，单独切片评估。
3. **source/src 平台原型是否收编**——建议维持外部+锁，直到平台适配层稳定。
4. **锁与 desktop-release.yml 的接线**——建议在该 workflow 的 build 步骤前加 `lock:verify`，随 L1/CI 线并入时落地。

## 九、未完成与边界（如实）

- 真实锁摘要未生成：Mini 无真实 stage root（平台 preflight 报告 `worker-records/platform-preflight-20261017/` 在本节点实测不存在；`/Volumes/HyprialDev/{builds,build,sdk}` 在本会话读权限之外）。生成步骤：在持有 prepared runtime 的构建机执行 `npm run stage:node` → `npm run lock:generate -- <stageRoot> <target>` → 评审后提交锁。
- Linux target：`catalog.cjs` 无 linux 发行版条目、`stage-runtime.cjs:8` 目标白名单无 `linux`；L1 待并分支 `work/gui-linux-package-20261017` 在本节点远端**不可见**（ls-remote 无此 ref）。重叠项（stage-dev-runtime/tauri-bundle/bundle-dev/catalog linux-x64）本切片不重复实现；`TARGETS` 暂不加 `linux-x64`，待其并入后由 coordinator 决定锁结构扩展。
- Windows 侧 `node24/` 布局沿用既有代码；`.zip` 解包依赖系统 `unzip`（Windows 主机执行时如缺失即 `UNSUPPORTED_ARCHIVE_FORMAT` 具名失败）。
- 未跑重型 Rust（本节点约束）；未改 daemon/sidecar/identity。
