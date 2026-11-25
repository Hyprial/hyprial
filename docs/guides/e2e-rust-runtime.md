# E2E native wheel 的隔离 Rust 工具链

E2E-005 会从候选源码构建并安装真实 wheel。actor 扩展进入源码后，免凭据 macOS 门禁也需要
Rust 1.93.1；editable 安装成功并不证明后面的 wheel 构建具备 Cargo。dev `284dcf729` 的
macOS run 41797 在这一阶段明确报告没有 Cargo，不能通过跳过 native 来取得通过结果。

`scripts/ci/ci-ensure-rust.sh` 在该 workflow 的 runtime 准备之后、场景执行之前运行：

- Cargo 和 rustup 配置目录位于本次 `HYPRIAL_E2E_ROOT/toolchain/rust/`，不会使用用户
  默认的 `.cargo` 或 `.rustup` 配置。每条路径先校验物理父目录，拒绝通过 symlink 写入全局目录。
- Rust 版本固定为 1.93.1，校验 rustc、Cargo 和实际 host。已有 rustup 必须为 1.28.2
  才可作为程序复用，安装仍使用本次私有 homes；不会运行全局默认工具链或修改全局 PATH。
- 无 rustup 时下载官方 versioned archive 中的 rustup-init 1.28.2，用官方 `.sha256`
  文件公布的固定摘要验证后才执行。代理和 TLS 校验保持正常，下载和校验失败会终止步骤。
- bootstrap、已验证的 dist archives 与 Cargo registry/git 缓存位于 `HYPRIAL_E2E_RUST_CACHE`；默认选择 CI cache
  下的 E2E 专属子目录，或用户 `.cache/hyprial-e2e` 下的专属目录。配置 homes 仍按 run 隔离。
- 通过 `GITHUB_ENV` 传递 `CARGO_HOME`、`RUSTUP_HOME`、`RUSTUP_TOOLCHAIN` 和绝对路径
  `HYPRIAL_CARGO`，通过 `GITHUB_PATH` 传递对应工具链的 bin；E2E-005 更换 HOME 后仍能
  使用同一套明确的工具。私有 run 目录由原有 E2E cleanup 生命周期管理。

本地可设置独占的绝对 E2E 根与缓存后运行该脚本。需要复用已信任、已经安装的工具链时，
显式设置 `HYPRIAL_E2E_RUST_SEED=<toolchain-directory>`；脚本只读验证该目录并在本次 run
内引用，不修改 seed。独立进程的环境交接由上述两个输出文件承担，本地后续命令也需使用
这些值，不能假设脚本改变了调用者的 shell 环境。

验证与官方摘要来源见 [Rust 准备证据](../notes/e2e-rust-runtime-2026-11-08/README.md)。
Linux 的真实安装、编译探针不替代 macOS 候选 CI、E2E-005 完整运行或五平台发布验收。
本工具不改变场景断言、job 的 90 分钟预算、发布 hold 或任何发布步骤。

## 冷下载、完整 archive 缓存与私有安装

候选 `732d22fe` 的 macOS run 41919 在下载 rustc 时达到 Rust 准备步骤的 10 分钟期限，
18 个场景全部未运行，底层下载停滞原因仍为 UNKNOWN。本次只将这一个工具步骤放宽到
20 分钟，并给下载增加明确诊断；完整场景、native ABI 和发布验收仍须用后续固定 SHA 实跑。

`ci-rust-dist.py` 每次获取官方固定版本 `channel-rust-1.93.1.toml` 和官方 `.sha256`，
先验证 manifest，再选择实际 host 的 cargo、rust-std、rustc xz URL 与 SHA-256。
只接受 `https://static.rust-lang.org/dist/<官方日期>/<组件>-1.93.1-<host>.tar.xz`，
不接受镜像、URL 凭据、重定向、其他 host/version、缺失组件或损坏摘要。minimal profile
中的其他平台包按 host 可用性过滤；官方 Cargo 包元数据为 0.94.0，但该发行 archive 与
最终 `cargo --version` 的工具链版本是 1.93.1，不能混淆这两个版本字段。

curl 保留代理及 TLS 验证，连接期限为 10 秒、低于 16 KiB/s 持续 30 秒即终止。
组件每次请求最多 240 秒，最多两次重试，并有 600 秒重试预算及 750 秒进程兜底；
manifest 请求使用更短的 60 秒期限和一次重试。日志记录组件、官方 URL、HTTP 状态、
下载字节数、耗时与失败退出码，不输出认证信息。bootstrap 同样具有 TLS、低速、连接、
总时限和有限重试控制；最终仍必须匹配仓库内四 host 的官方固定摘要。

部分下载只保存在本 run 的私有临时目录。完成且校验成功后，原子发布到
`HYPRIAL_E2E_RUST_CACHE/dist/1.93.1/<host>/<sha256>`；发布临时文件不作为缓存入口，
不会被其他 run 复用，失败会清除。复用时重新计算整个 archive 摘要，拒绝 symlink、
非普通文件和坏缓存，不自动覆盖它们。随后复制到本 run 私有的
`RUSTUP_HOME/downloads/<sha256>`，由 rustup 再次验证并安装。安装目录和 partial 文件
不跨 run 共享；某个组件失败时，先前完整验证的其他组件仍可供后续 run 复用。

rustup 的版本探针在私有目录中清除 `RUSTUP_TOOLCHAIN` 并设置 `RUSTUP_AUTO_INSTALL=0`，
避免 `rustup --version` 在预取之前触发隐式安装。预取完成后才显式安装固定工具链。
真实 Linux 的拒绝 archive 请求对照、完整 shell 冷/暖安装及 Cargo 编译记录见
[冷准备证据](../notes/e2e-rust-cold-provisioning-2026-11-08/README.md)。
