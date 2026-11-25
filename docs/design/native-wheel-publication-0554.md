# 0.5.5.4 native wheel 发布准备与验收

状态：**发布 BLOCKED，待实现和实测**。2026-11-08 审计源码为
`4f295fc5af1f96d56c96f774663b623898c13210`，包含 actor #2514 和 dev-first CI 迁移。
它是本次调查的固定输入，不自动成为最终发布候选；最终候选及其测试证据必须另行固定
完整 40 位 SHA。问题登记为 [KP-027](../reference/known-problems.md#kp-027-native-engine-的五平台-wheel-发布尚未接通)。

本轮只准备发布。`gate-python.yml::unit` 的 `INTERNAL_PUBLICATION_ENABLED` 和
`release-tag.yml` 的 `FORMAL_PUBLICATION_ENABLED` 都保持字符串 `"false"`。
dev 合并成功、旧 wheel job 绿或 release-tag dry-run 通过，均不能作为解除冻结的依据。
发布入口、不可变 `v<VERSION>` 与可移动轨道规则以
[发布规则](../guides/release-rules.md) 为准；本计划不新增发布 tag 类型。

## 1. 已确认的实现缺口

### 宿主与目标必须一致

[gui_build_hook.py](../../src/hyprial/gui_build_hook.py) 的正常 wheel 初始化会调用 native
preflight 和 staging。[native_engine.py](../../src/hyprial/shell/impl/packaging/native_engine.py)
的 `preflight()` 拒绝 `target != host`，要求 Cargo、crate 源码/锁文件、正确 abi3 feature
及启用 GIL 的 CPython ≥ 3.12。Tailcat 的 Go 交叉编译能力不能替代 actor 扩展的宿主要求。

[wheel-release.yml](../../.forgejo/workflows/wheel-release.yml) 当前只有一个
`runs-on: [release-ci]` 的 `build-publish` 作业，在其中按
`darwin/arm64 → darwin/amd64 → linux/amd64 → linux/arm64 → windows/amd64`
设置 `HYPRIAL_TAILCAT_TARGET` 并执行 `uv build --wheel --python 3.12`。
这不能构建合入 actor 后的五平台集合；在已观察的 Linux x86_64 runner 上，第一个目标
就与宿主不同。还需显式供应并验证 Rust 工具链，不能依赖 runner 偶然预装 Cargo。

### ABI 文件名的消费链需要一起迁移

native wheel 的 tag 为 `cp312-abi3-<platform>`，载荷为
`hyprial/_core/actors.abi3.so`（Windows 为 `actors.pyd`）。以下路径仍要求 `py3-none`：

| 路径 | 当前约束与迁移责任 |
| --- | --- |
| [verify_tailcat_wheels.py](../../scripts/ci/verify_tailcat_wheels.py) | 文件名、WHEEL Tag 和五平台闭集均硬编码旧 tag；须同时校验 actor 与 Tailcat 载荷 |
| [attach_wheel_assets.py](../../scripts/ci/attach_wheel_assets.py) | GitHub 附件计划按旧 tag 生成五个预期文件名 |
| [publish_wheels.py](../../scripts/ci/publish_wheels.py) | R2/清单解析的文件名正则只接受旧 tag |
| [install.sh](../../install.sh) | POSIX 清单选择和版本提取依赖旧 tag |
| [install.ps1](../../install.ps1) | Windows 清单选择和版本提取依赖旧 tag |

因此只有编译矩阵还不够。上述生产者与消费者必须使用同一版本/ABI/平台契约，不能把
native wheel 改名为 `py3-none`，也不能把“接受任意 wheel 文件名”当兼容修复。
目前五平台新 wheel 的构建、发布及安装端到端验证均未执行。

2026-11-09 消费兼容修复候选：校验器、GitHub 附件计划和 R2 清单读取同时接受完整且
同版本、同 ABI 的 `py3-none` 或 `cp312-abi3` 五平台集合，拒绝混合 ABI、重复和不支持
的文件名。native WHEEL 标签必须与文件名一致，actor 模块必须存在且为对应架构的
普通文件；legacy 标签不能携带 native actor 冒充兼容。POSIX/Windows 安装脚本保留旧
轨道支持，同时接受新 ABI；同平台新旧两份并存仍判多匹配失败，摘要和版本回读保留。
native 安装固定 GIL CPython 3.12（`--python 3.12`）；`cpython>=3.12` 在真实 `uv tool
install` 中可解析到 free-threaded 3.14t 并拒绝 abi3 wheel（2026-11-13 实测）。这是消费契约修复候选，不是发布准备完成：真实 actor
导入及 ABI、RECORD、Linux libpython/GLIBC 审计、五平台宿主矩阵和完整产品安装仍需
独立实测；本节不解除任何 hold，也不把模拟传输/安装测试当作真实产品安装。

### Linux 的兼容下限必须有真实证据

当前 Linux 平台声明为 `manylinux_2_17_{x86_64,aarch64}.manylinux2014_{x86_64,aarch64}`。
native 检查要求扩展不依赖 `libpython`，最高 GLIBC 符号版本不超过 2.17。普通
`node:22-bookworm` 构建环境不能证明该下限；每个架构都需要匹配的 manylinux2014
构建环境和产物审计。

[manylinux_native_evidence.sh](../../scripts/build/manylinux_native_evidence.sh) 可复用部分
证据收集逻辑，但当前固定 `manylinux2014_x86_64` 和 x86_64 rustup 下载地址，归档的输入
仅为 crates；其中临时 wheel 也不是完整产品 wheel。它不是已完成的双架构发布 builder。
其 Rust 版本为 1.93.1，CPython 构建入口为 cp312，并检查 cp312/cp313/cp314 原生导入。
`HYPRIAL_BUILD_ALLOW_GLIBC_RAISE=1` 和 `HYPRIAL_BUILD_SKIP_NATIVE=1` 都是本地选项，
不能用于发布；不得靠放宽平台声明、少打包一个平台或关闭 native 检查解除阻断。

## 2. 实际 runner 证据与权限边界

2026-11-08 仅执行只读 API 请求；未注册 runner、申请注册 token 或修改配置。

| 查询 | 结果 |
| --- | --- |
| `GET /api/v1/repos/HyprialOS/hyprial/actions/runners?visible=true&limit=100` | 403：`user should be the owner of the repo` |
| `GET /api/v1/orgs/HyprialOS/actions/runners?visible=true&limit=100` | 403：`Must be an organization owner` |

repo admin 权限不能替代组织 owner。下列证据来自已执行 job 的 `runs_on` 和实际日志，
不代表当前在线状态、空闲容量或新候选构建已通过：

| 实际平台 | selector 与证据 | 能证明的范围 |
| --- | --- | --- |
| Linux x86_64 | `release-ci`；[wheel-release run 41557](https://code.hyprial.com/HyprialOS/hyprial/actions/runs/41557)，API run ID 51905 / job 133845；uv 和 CPython 为 Linux x86_64 | 旧源码 `61948416cab933002d12b70f84922f4c41baa058` 的旧 wheel 作业成功；不是新 native wheel 证据 |
| macOS arm64 | `macos-host`；[e2e-macos-isolated run 41367](https://code.hyprial.com/HyprialOS/hyprial/actions/runs/41367)，API run ID 51659 / job 133172；uname、uv 和 Go 为 Darwin arm64 | 源码 `6d94236bc93156be1345447fb7415a536333729a` 的执行环境可识别；该 E2E 作业整体失败，不记 PASS |
| macOS x86_64、Linux aarch64、Windows amd64 | 未取得有权清单或对应近期执行证据 | 可用性 UNKNOWN，不断言不存在，也不编造 selector |

组织 owner 下一步应提供五个平台的实际 selector、宿主 OS/架构、在线容量和工具安装方式。
Linux 两架构还需确认相应容器执行能力；每次候选作业仍须测量真实宿主，不能只相信 label。

## 3. 实施顺序与完成条件

### A. 固定候选并补齐执行环境

CI/发布维护者确认完整 source SHA 在受保护 dev 历史内，固定根 VERSION 和预期五平台集合。
组织 owner 确认上一节的容量后再写具体 `runs-on`，不把待确认的名字先提交成可用矩阵。
Linux 使用架构匹配、digest 固定的 manylinux2014 镜像；记录 Rust 1.93.1、Go 1.27.1、
启用 GIL 的 CPython 3.12 及其他打包工具的版本和获取摘要。工具链版本变更另行审查。

完成条件：五个目标都有可执行的宿主方案和隔离构建目录；缺一个即保持 BLOCKED。

### B. 增加只构建候选的工作流

新增显式 full-SHA 的候选构建入口，每个宿主只构建自己的目标，不能解析运行时变化的
dev/internal tip。该阶段不给 registry/R2/GitHub 发布凭据，不写 tag、轨道或公网对象。
Windows 使用经过验证的 PowerShell/构建工具链，不能直接照搬现有 POSIX shell 块。

每个作业输出一个完整产品 wheel，以及 source SHA、VERSION、目标、实际宿主、工具链、
ABI、wheel SHA-256 和测试结果。打包内容同时含 GUI、Tailcat 和 actor 扩展。
以实际 wheel 在隔离环境执行安装和 `hyprial._core.actors` 导入，核对
`ENGINE_ABI_VERSION == 1`；Python 版本兼容范围须有对应验证，不把 abi3 标签当实测。

完成条件：同一 SHA 五份真实构建回执；编译成功、pytest 绿与安装成功分别记录。

### C. 迁移并加强 wheel 集合校验

为第一节的全部消费者统一 `cp312-abi3` 命名契约和版本提取；添加先失败后通过的契约测试，
覆盖完整五平台、遗漏/重复/混版、错误摘要、不安全文件名和元数据不符。
校验真实 actor ELF/Mach-O/PE 架构、无 `libpython` 依赖、Linux GLIBC 下限，以及
Tailcat 的对应架构；WHEEL 标签、RECORD、载荷和文件名必须一致。

完成条件：五个 wheel 绑定相同完整 SHA 与版本，只有该闭集可以进入发布计划；不能拼接
不同运行的“最新成功”产物，不能用手写 PASS 字段替代日志和产物。

### D. 先保证安装器兼容既有轨道

POSIX 和 Windows 安装器须同时支持既有轨道的 `py3-none` 与新候选的 `cp312-abi3`，
并选择受支持的 GIL CPython ≥ 3.12。保留恰好一个平台匹配、摘要校验和失败即停止的语义。
验证旧轨道安装、新 native wheel 安装、无匹配/多匹配、错误 ABI、错误摘要及不安全文件名；
真实 Windows 与 POSIX 安装结果不可互相替代。

`release-install-scripts.yml` 在相关 dev 变更上独立发布公网安装器，不受 internal hold
控制。因此本轮没有修改 installer；后续兼容修改必须先在隔离分发源验证，再进入 dev，
不能部署只认识新 ABI 的版本而使仍指向旧 wheel 的 stable/nightly/internal 无法安装。

完成条件：新旧格式均有真实安装证据，版本回执正确，运行中的生产 daemon 未被测试改动。

### E. 聚合验收后再决定发布

聚合上面的固定产物和证据，完成该 SHA 的 Python/Go/GUI/契约及相关真实 E2E，另完成
UUID P4/PAC 单向迁移的备份与恢复准备。保留有失败或未测的项目，不能把不同 SHA 的绿相加。
发布时消费已验收的同一组 bytes；若必须重建，重建结果需要重新验证，旧摘要不能沿用。

随后才能通过独立、经过审查的变更解除相应 hold。保留不可变 v*、单一轨道 writer、
祖先关系与 lease 防回退、写入前 source freshness 检查，以及 R2 全局串行发布。
显式 dispatch 和启动回读继续保留；完成后 Forgejo registry、GitHub Release 与 R2 的
五个 wheel 和 SHA256SUMS 全部相同才可记分发 PASS。任何部分失败不得记完整发布成功。

完成条件：发布负责人审核精确 SHA 的完整证据并作发布决定；本文件本身不是发布授权，
也不证明旧 ref 上的全部 workflow 已被管理员禁用。

## 4. 当前交付状态

- 静态调查已确认宿主限制、ABI 消费不兼容和 Linux 兼容下限要求，问题为待修。
- 实际 runner 证据覆盖近期 Linux x86_64 / macOS arm64 执行；其余三平台容量 UNKNOWN。
- 新候选五平台构建、分发和真实安装未执行，状态 BLOCKED；没有新版本或轨道发布结论。
- 负责人：R&D 负责人协调；组织 owner 负责 runner 清单，CI/发布维护者负责 A/B/C/E，
  分发与安装维护者负责 D。完成各项后更新 KP-027，并在最终候选记录引用实际 run 和摘要。
