# 源码 editable 安装的本机 native engine 构建计划

日期：2026-11-16。状态：WIP 规划。目标分支：`dev`。

## 背景

源码 checkout 使用 `uv sync` 时，当前 editable build hook 不编译 Rust 扩展。
如果 `src/hyprial/_core/actors.abi3.so` 没有预先暂存，安装本身可以完成，但默认 native backend
在第一次创建真实 actor application 时才报 `hyprial._core.actors` 不可导入。wheel 构建仍由 Hatch
build hook 负责 native stage；本计划只增加源码 editable 安装的显式本机路径。

## 目标行为

只有调用者显式设置 `HYPRIAL_BUILD_NATIVE=1` 时，editable 安装才启用本机编译：

```bash
HYPRIAL_BUILD_NATIVE=1 uv sync --extra test
```

构建流程按以下顺序执行：

1. 检查当前源码树中的 native module 和 stamp；模块存在、ABI 正确、源码 digest 匹配时复用它。
2. 模块缺失或过期时，调用现有 `scripts/build/build_native_engine.sh`，使用当前主机的 Python、Rust
   toolchain、target 和 Cargo cache 编译并暂存 `actors.abi3.so`。
3. 编译完成后在同一解释器中导入模块，检查 `ENGINE_ABI_VERSION`、模块 SHA-256 和 stamp digest。
4. 任一步失败都在安装阶段给出明确错误；不回退到 reference backend，也不生成声称可用的纯 Python 安装。

未设置变量时保持现有行为：editable 安装不编译；如果运行时需要 native backend，仍按现有 fail-closed
错误报告缺少扩展。`HYPRIAL_BUILD_NATIVE` 只面向当前主机，不负责交叉编译、五平台 wheel 或预编译
artifact 分发。

`HYPRIAL_BUILD_NATIVE=1` 会让安装阶段执行本机 Rust 编译，通常只有在 `uv sync -v` 中才会看到
Cargo 的详细输出；构建时间取决于 Cargo 缓存和机器，首次构建可能持续约一分钟。需要长期启用时可
先执行 `export HYPRIAL_BUILD_NATIVE=1`。开关是 uv cache key：构建过的环境不代表未设置开关的
环境也已构建，切换开关会重新运行 hook 并重新校验。除空值（未设置）外，`0`、`true`、`yes` 等值
都拒绝；`HYPRIAL_BUILD_SKIP_NATIVE=1` 与本开关同时设置也拒绝。

## 实现边界

- 在 `src/hyprial/gui_build_hook.py` 的 editable hook 入口读取变量，并复用
  `src/hyprial/shell/impl/packaging/native_engine.py` 的检查、stamp 和 Cargo 参数逻辑；不得复制
  native 构建规则。
- hook 必须把稳定的当前构建解释器绝对路径显式传给 helper 的 `PYO3_PYTHON`，优先使用
  `sys._base_executable` 的 realpath，不能依赖首次 `uv sync` 时尚未存在的 `.venv/bin/python`，
  也不能把一次性 uv build 环境路径写入 Cargo 输入。
- Rust toolchain 继续固定为 1.93.1；缺失或版本不符时，安装失败信息必须包含
  `rustup toolchain install 1.93.1`，不放宽版本要求。
- 仅在 `HYPRIAL_BUILD_NATIVE == "1"` 时执行构建；空值、其他值和 `HYPRIAL_BUILD_SKIP_NATIVE` 的
  组合必须有确定的错误或保留行为，不能因拼写错误静默触发或跳过；两个变量同时为 `1` 时拒绝。
- 使用构建解释器和源码 checkout 的 Cargo target；不把生成的 `.so` 或 stamp 提交到 Git。
- 多 worktree 场景尊重调用者已有的 `CARGO_TARGET_DIR`，未设置时使用该 checkout 的默认 target；
  暂存步骤从 cargo 的 `--message-format=json` 输出取产物路径（`CARGO_TARGET_DIR`、
  `CARGO_BUILD_TARGET_DIR`、`.cargo/config.toml` 的 `build.target-dir` 都由 cargo 解析），不能从
  `crates/target` 猜路径。相对路径按
  checkout 根解析，且不使用外层嵌套 Git 仓库的根目录；源码目录不是 Git checkout 时也必须能按
  脚本自身所在的 checkout 根运行。
- 保持 wheel、sdist、worker 镜像和多平台发布路径不变。
- 开关格式校验在所有 Hatch build target 入口执行，因此 wheel、sdist 和 worker 构建也会拒绝非 `1`
  的 `HYPRIAL_BUILD_NATIVE` 以及两个开关同时为 `1`；正常未设置变量时这些发布路径不变。
- `uv` cache key 必须包含 `HYPRIAL_BUILD_NATIVE`、native 输入和暂存产物，避免变量切换后复用错误
  editable 环境。uv 只记录 hook 运行前的 key 状态并取最新 mtime，所以暂存产物被删除后会回到
  “未构建”状态；opt-in hook 构建前触碰已跟踪的 `scripts/build/native-engine.epoch`（同样是 key），
  使删除后的状态不同于任何已记录状态。恢复旧环境用 `uv sync --reinstall-package hyprial`。

## 验证计划

新增隔离测试覆盖：

- 已有有效 `.so` 时不调用 Cargo；
- 缺失 `.so` 且变量为 `1` 时调用本机 build helper，并验证 stamp；
- 缺失 `.so` 且未设置变量时不编译；过期模块、缺 stamp 或字节不符在开关开启时重建，关闭时保持现有错误；
- 变量为非 `1` 的值时拒绝或保持明确的未启用语义；
- `HYPRIAL_BUILD_SKIP_NATIVE=1` 与 `HYPRIAL_BUILD_NATIVE=1` 同时存在时拒绝；
- 没有 `.venv` 的全新 checkout 仍能把当前构建解释器显式传给 Cargo；
- Rust 1.93.1 缺失时给出安装提示，不放宽 toolchain；
- Cargo、ABI、digest 或导入失败时安装失败并保留可诊断错误；
- `uv sync --extra test` 的缓存 key 在开关和值变化时失效，并在产物有效后只校验不重复 Cargo；
- 本机 Linux/macOS 的真实 editable 安装和一次 native import；跨平台矩阵与 wheel 发布继续由现有门禁负责。

## 真实探针记录

环境：本机 4 核共享机器，Linux x86_64，uv 0.11.32，Python 3.12.3，Rust 1.93.1（rustup），
`CARGO_TARGET_DIR` 指向 checkout 外的目录。以下均为实际运行，时间为 `time` 的 real。

**冷构建**（无 `src/hyprial/_core`，空 target 目录，Cargo registry 已缓存）：

```bash
HYPRIAL_BUILD_NATIVE=1 CARGO_TARGET_DIR=$T uv sync --extra test --python 3.12 -v
```

23.4 s（user 1m04s）；日志含 `staged src/hyprial/_core/actors.abi3.so (from $T/release/libactors.so)`、
`stamped …/actors.stamp.json`、`hyprial._core.actors: staged module matches crates/`。
`actors.abi3.so` SHA-256 `a09d2c86e34be30644206789dbb78c589fcde4120074601df72c0d07bbd997dc`
（与 `$T/release/libactors.so` 相同），`uv run --python 3.12 python -c "import hyprial._core.actors"` 得到
`ENGINE_ABI_VERSION=1`。

**删除暂存模块后重建**（本 PR 前的缺陷）：首次构建后 `rm -rf src/hyprial/_core`，再
`HYPRIAL_BUILD_NATIVE=1 uv sync -v`。uv 在 hook 运行前记录 cache key 状态，且把所有匹配文件归约为
最新 mtime（`Computed cache info: Timestamp(…) … Most recently modified: <file>`）。
只监视 `actors.*` 时删除后状态等于首次构建前记录的状态：

```text
禁用 epoch 触碰后:  rm -rf _core; sync  -> 构建
                    rm -rf _core; sync  -> "Checked 65 packages", rc=0, hook 未运行
                    import hyprial._core.actors -> ModuleNotFoundError
```

修复：hook 在构建前触碰已跟踪的 `scripts/build/native-engine.epoch`，`pyproject.toml` 以
`{ file = "scripts/build/native-engine.epoch" }` 监视它。同样的两步（首次构建后立即删除，以及先
`uv sync` 一次让 uv 记录含模块的状态后再删除）都重新构建并暂存：

```text
rm -rf src/hyprial/_core; sync -> staged …, Built hyprial …, Installed 1 package (rc=0, 0.9 s, cargo 已缓存)
sync -> Checked 65 packages
rm -rf src/hyprial/_core; sync -> staged …, Installed 1 package; import ok ENGINE_ABI_VERSION=1
```

已处于“Checked 但模块缺失”状态的环境（修复前构建的）一次性恢复：
`HYPRIAL_BUILD_NATIVE=1 uv sync --reinstall-package hyprial`（实测恢复）。

**target 目录不再猜测**：把真库复制为 `crates/target/release/libactors.so` 并追加字节
`STALE-DECOY-MARKER`（SHA-256 `774c04db7ccafe1c5aeda8be7169c6ad7e8a92b5bdd9c1fc1dc8c2106eced9f2`），
删除暂存模块后分别用下列方式指定真实 target，`uv sync --reinstall-package hyprial`：

| 设置方式 | 暂存模块 SHA-256 |
| --- | --- |
| `CARGO_TARGET_DIR=$T`（未设 `CARGO_BUILD_TARGET_DIR`） | `a09d2c86…97dc`（真库） |
| `CARGO_BUILD_TARGET_DIR=$T`（未设 `CARGO_TARGET_DIR`） | `a09d2c86…97dc` |
| `.cargo/config.toml` 的 `[build] target-dir = "$T"` | `a09d2c86…97dc` |

修复前同一诱饵与 `CARGO_BUILD_TARGET_DIR` 运行旧脚本得到 `774c04db…d9f2`（诱饵被暂存并写 stamp）。

**第二次 sync 只校验**：`PATH` 前置记录所有 `rustup` 调用的包装脚本；第二次
`HYPRIAL_BUILD_NATIVE=1 uv sync` 为 0.03 s（`Checked`），包装日志 0 行，`actors.abi3.so` mtime 不变。
`touch pyproject.toml` 强制 hook 重跑：0.48 s，日志 `staged module matches crates/`，包装日志 0 行，mtime 不变。

**改一个 `.rs`**（`crates/hyprial-py/src/cell.rs` 追加换行）：5.2 s，包装日志含
`rustup run 1.93.1 cargo build --release --locked … --message-format=json-render-diagnostics`，
stamp 的 `source_sha256` 由 `5cd19e812eb1…` 变为 `e5eec874beac…`；还原后再 sync（5.0 s）回到 `5cd19e812eb1…`。

**默认路径**（开关未设、`PATH` 只含 uv 与系统目录，无 Rust）：
`uv sync --extra test --python 3.12 --reinstall-package hyprial` rc=0，0.5 s，只打印
`notice: no src/hyprial/_core/actors.abi3.so is staged …`，没有构建，没有 `_core` 目录。

**非 Git 副本**（`git archive HEAD | tar -x` 到临时目录，无 `.git`）：
`HYPRIAL_BUILD_NATIVE=1 uv sync --python 3.12`，6.3 s（含新建 venv，共享已构建的 target 目录），暂存模块
SHA-256 与 target 中的库相同，可导入，`ENGINE_ABI_VERSION=1`。

**wheel 不受影响**：worker wheel 按 `.forgejo/workflows/kp027-native-candidate.yml` 的方式
`HYPRIAL_BUILD_WORKER_WHEEL=1 HYPRIAL_TAILCAT_TARGET=linux/amd64 HYPRIAL_BUILD_ALLOW_GLIBC_RAISE=1 uv build --wheel --python 3.12`
分别在不设与设置 `HYPRIAL_BUILD_NATIVE=1` 下构建（各约 11 s）：两个 wheel 都是
`cp312-abi3-manylinux_2_34_x86_64`，文件清单逐项相同（1836 项，不含 epoch 与暂存文件），
`hyprial/_core/actors.abi3.so` SHA-256 均为
`e8cda8eb0ac7f1b78a0ed93894ddebd9b995a426f0416361041626daa88f8651`。

测试：`uv run --python 3.12 --with libcst==1.9.0 --with pytest-timeout pytest tests/shell/packaging/native_engine -n 2 --timeout=300 -q -p no:cacheprovider`
为 73 passed；`tests/shell/packaging` 为 210 passed（PR 正文旧数字 88 不是这两个命令的结果）。

未验证：macOS（`.dylib` 路径现由 cargo 的 `compiler-artifact` 消息给出，未在 macOS 上运行）、Windows、
两个 checkout 并发共用同一 `CARGO_TARGET_DIR`（只依次使用过）。没有 Rust toolchain 或当前平台无法编译时，
报告 BLOCKED，不用 reference backend 代替 native 证据。

## 风险与回滚

主要风险是 `uv sync` 的构建时间、Cargo 缓存占用和本机 Rust toolchain 差异。默认关闭开关保持现有
轻量安装路径；删除 editable hook 的这一分支即可回滚，不影响 wheel native stage。

本计划不改变发布字段、IPC 方法或错误码，因此不需要 contract 文件变更；实现 PR 仍需声明
`Contract-Impact` 与 `Skill-Impact`。
