# ADR：Rust Core源码、构建与分发

状态：**构建/平台/CI规则已获Allen裁定，实际产物与运行证据OPEN**。六项选择已给出明确方案；尚未编译正式产品、创建目标环境或发布wheel。S1可按已裁规则推进，不等待#1892，但不把设计选择写成实现验证PASS。

本轮修订于2026-10-25完成；文件沿用本组10-24讨论的日期标识。平台、CI和硬E2E裁定见[G0决定与验收附录](actor-rustify-g0-decisions-and-acceptance-2026-10-25.md)，缺少实际证据仍为BLOCKED且不能完成。

## 1. 六项决定

| 事项 | 本方案的选择 | 未通过验证时怎么处理 |
|---|---|---|
| 位置与打包 | 同仓`crates/` Cargo workspace；`hyprial-core`为纯Rust，`hyprial-py`为薄PyO3绑定；只发布一个hyprial Python包 | 不另发同步版本的hyprial-kernel包，不退回Python包树里的Rust源码布局 |
| 构建方式 | 保留Hatchling；现有hook委托小型native构建helper，调用`cargo build --release --locked`；不切换根build backend、不默认引入maturin-as-tool | 链接/标签问题在S1解决；不能用复制本机.so冒充正式构建 |
| ABI与解释器 | 采用`abi3-py312`作为绑定目标，标准GIL CPython；决定⑬已裁定CI验收3.12/3.14，free-threaded/PyPy/GraalPy不支持，free-threaded须明确报错 | abi3能力验证失败则BLOCKED并评审变更；不伪写abi3标签。既有支持声明不得静默缩减，额外已声明支持版本需补测或由Allen明确处理 |
| 平台与编译 | Allen R1新裁定：本goal仅darwin/arm64原生与linux/amd64 manylinux Docker两硬格；darwin/amd64、linux/arm64、windows/amd64保留为后续门，Cargo使用固定vendor | 本goal两硬格缺证据不能完成；后续三格未PASS前#1897不得合入dev或进入任何发布，dev/internal/stable不得包含Rustify；缺`hyprial._core.actors`不得fallback |
| 公网wheel与源码安装 | 受支持平台须有可公开获取的同包wheel及校验值；公开快照/sdist也携带完整构建源码和固定vendor；源码安装显式要求工具链 | 分发链路/缺工具链错误属于S1交付；本轮不执行真实发布。没有预编译产物不能把公网安装验收标PASS |
| workspace / lockfile | `crates/Cargo.toml`与`crates/Cargo.lock`独立管理core/binding；`app/src-tauri`继续保留自己的workspace/lockfile | 本次不合并Tauri workspace、不顺带改其MSRV；未来复用core时单独验证消费方锁和工具链 |

选择依据是减少分发和底座维护：Python包版本仍由根VERSION决定，原生模块内嵌同一产品版本及独立的接口schema编号；Cargo内部版本不是第二条公开产品发布线。构建工具链沿用实验已用的Rust1.93.1作为初始固定版本，不据此宣称已在Tauri的MSRV1.85验证。

纯core不得依赖PyO3/CPython，必须可独立构建和测试。绑定层只负责owned值编解码、调用、Future/事件传递与异常映射；不承载领域代码。Cargo workspace共用锁和target目录的规则见[官方说明](https://doc.rust-lang.org/cargo/reference/workspaces.html)；这不要求另一个消费者workspace也使用同一个锁。

## 2. 平台矩阵

保留当前`verify_tailcat_wheels.py`的架构和最低平台口径，Python/ABI部分改为实际扩展所需标签。按Allen R1新指示分两段，不把功能替换验收与dev合入/发布许可混为一谈。

### 2.1 本goal两个硬格

| 目标 | Rust target | 平台标签/底线 | S1/S2必须取得的证据 |
|---|---|---|---|
| darwin/arm64 | aarch64-apple-darwin | macosx_13_0_arm64 | macos-host原生构建、uv安装、import与公共调用 |
| linux/amd64 | x86_64-unknown-linux-gnu | manylinux_2_17_x86_64 / manylinux2014 | Ubuntu runner的manylinux兼容Docker内构建、动态依赖审计、import与公共调用 |

### 2.2 多平台分发后续门

| 目标 | Rust target | 平台标签/底线 | dev合入/发布前必须取得的证据 |
|---|---|---|---|
| darwin/amd64 | x86_64-apple-darwin | macosx_13_0_x86_64 | 原生或交叉构建；x86目标机/Rosetta运行冒烟，明确记录实际路径 |
| linux/arm64 | aarch64-unknown-linux-gnu | manylinux_2_17_aarch64 / manylinux2014 | 原生或manylinux交叉构建；原生或qemu-user import/公共调用 |
| windows/amd64 | x86_64-pc-windows-msvc | win_amd64 | 原生或cargo-xwin交叉构建；Windows目标机/VM的pyd安装与调用/关闭 |

**五个平台正式产物均未在本轮验证。** 本goal只以2.1两格作为判据7的平台范围；2.2三格未PASS前，#1897不得合入dev或进入任何发布，dev/internal/stable不得包含Rustify。详细门见[附录G](actor-rustify-g0-decisions-and-acceptance-2026-10-25.md)。不能在较新glibc主机上编译后直接贴manylinux_2_17标签，不能缺`hyprial._core.actors`时fallback，也不能偷偷提高最低平台版本。

PyO3提供limited API/abi3和手动Cargo构建路径；使用它们仍需正确链接与标记产物，并非“配置了feature即已跨平台验证”。[PyO3构建文档](https://pyo3.rs/v0.27.2/building-and-distribution)、[feature参考](https://docs.rs/pyo3/0.27.2/pyo3/#features-reference)

free-threaded支持单独立项，不设置未经验证的无GIL声明；标准GIL ABI选择不等于所有依赖已具备free-threaded安全性。[PyO3 free-threading](https://pyo3.rs/v0.27.2/free-threading.html)

## 3. 构建与安装接线

> **模块路径更正（HQ R5，2026-11-04）**：本ADR原写的`hyprial._core`扩展与`hyprial/_core.abi3.so`已被冻结的[原生ABI规范](actor-engine-native-abi-spec-2026-11-01.md)取代；正式模块路径是`hyprial._core.actors`（命名空间包`hyprial._core`下的扩展），下文与上文均按此更正。实现见`src/hyprial/shell/impl/packaging/native_engine.py`与[wheel分发指南](../guides/wheel-distribution.md#native-engine)。

> **固定vendor尚未落地（待HQ追认，2026-11-04）**：第1节“Cargo使用固定vendor”“公开快照/sdist也携带……固定vendor”目前未实现。wheel native阶段执行`cargo build --release --locked`，依赖按`crates/Cargo.lock`钉死版本与校验和，但在构建时从crates.io下载；`crates/`没有`vendor/`和`.cargo/config.toml`源替换，sdist与公开快照也不携带vendor。依赖不可达的构建主机（如DinD release-ci）因此失败，对应格按G0记BLOCKED。vendoring（`cargo vendor` + 源替换 + `--frozen`/`--offline`，并进入sdist与`SNAPSHOT_WHITELIST`）延到第4节native目标矩阵一并落地。

建议入口保持薄：Hatch hook编排GUI、Go和native阶段，具体native构建逻辑归`src/hyprial/shell/impl/packaging/`，可从公开源码直接使用。编译输出只暂存于构建目录，通过force_include作为私有扩展`hyprial._core.actors`进入wheel（Unix的`hyprial/_core/actors.abi3.so`，Windows的`hyprial/_core/actors.pyd`；`hyprial._core`是无`__init__.py`的PEP 420命名空间包）；仅kernel adapter导入它。源码留crates并进入sdist，不把整个target目录打入wheel。

hook使用uv管理的构建解释器，并把其绝对路径作为PYO3_PYTHON传给Cargo；不依赖PyO3自动找到系统python。pure core的构建/测试不需要Python解释器。

wheel标签由统一target描述生成，包含Rust扩展ABI、平台以及现有Go payload最低版本；不能让tailcat阶段再次把它覆盖成`py3-none-<plat>`。Hatch支持hook设置tag/pure_python及editable的强制包含配置，但这些能力仍需实际集成测试。[Hatch wheel build data](https://hatch.pypa.io/latest/plugins/builder/wheel/#build-data)

三种安装路径都要有正式结果：

1. **产品wheel**：uv安装后无需本机Rust编译器；扩展、Go、GUI产物与版本匹配。
2. **editable / uv sync**：当前hook直接return的行为不能沿用。显式构建/安装与当前源码匹配的扩展，检测过期构建与ABI不匹配；不能靠手工设置PYTHONPATH加载实验.so。
3. **worker精简wheel/镜像**：当前`HYPRIAL_BUILD_WORKER_WHEEL=1`只允许省略GUI/随镜像提供的Go产物，不能跳过native core。Docker构建阶段需包含crates与Rust工具链，运行镜像安装对应架构的同包产物。

`HYPRIAL_TAILCAT_ALLOW_MISSING`不是native缺失开关；缺扩展、错误架构、产品版本/API schema不匹配必须明确失败，不能改用Pykka或纯Pythonactor。import契约类型本身不应偷偷创建runtime；宿主由应用bootstrap显式启动。

## 4. CI和公开快照

S1需要修改/扩展的既有接线包括：

- `src/hyprial/gui_build_hook.py`及packaging helper：统一target、native staging、editable和worker分支。
- `.forgejo/workflows/wheel-release.yml`：从一台Ubuntu的Go五目标循环，改为有实际运行验证的native目标矩阵，最后汇总同版本产物与SHA256。
- `.forgejo/workflows/github-publish.yml`、`gui.yml`的wheel/installability步骤及worker Dockerfile：固定Rust工具链，传递同一源revision。
- `scripts/ci/github_publish.py`的唯一SNAPSHOT_WHITELIST：加入crates构建所需文件；不另造第二份公开镜像清单。
- `check_structural_limits.py`和AGENTS：新增crates产品根及target排除，Rust产品代码同样执行目录/行数规则。
- wheel验证器：同时核对Go与native模块的架构、ABI/tag、版本/API schema、可导入和可调用性，不只检查ZIP里有个.so。

新增检查不能删掉原GUI/Go/公共镜像检查。当前验证器文件名带tailcat，可以扩展或拆出共用校验，但要保持唯一的受支持target描述。

## 5. 本轮证据边界

已核对的是当前源码的Hatchling、五个平台、tag覆盖、editable/worker早退分支、公开白名单及独立Tauri workspace。上述源码都未修改，目标环境可用性和正式wheel均UNKNOWN。Allen已裁定分发规则和目标环境证据口径；本轮仅修订文档，不启动产品实施或真实发布。
