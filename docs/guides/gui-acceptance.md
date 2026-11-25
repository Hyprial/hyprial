# GUI 验收候选镜像（第一阶段）

此入口从当前分支的已提交 `HEAD` 归档后端、GUI 和 tailcat 源码，构建带 sidecar 的本地候选镜像。未提交的工作区改动不会进入镜像。与 `make debug-gui` 使用远端 `dev` GUI 快照不同，候选镜像的两份源码来自同一个提交。

```sh
make accept-gui MODE=normal PORT=4178
```

镜像使用 Go 1.27.1 构建 `hyprial-tailcat`，安装到源码包的 `hyprial/_bin/`，并在构建时执行包内 `verify_tailcat_sidecar()`。该检查要求协议 v6 和 daemon 固定的 tailcat 引擎提交一致；不匹配会使构建失败。启动命令打印候选 SHA、镜像来源及私密 handoff 文件路径，不打印一次性浏览器 grant。手动测试后按 Ctrl-C 停止命令，并检查它创建的容器已退出；镜像缓存保留。

当前阶段仍设置 `HYPRIAL_NETWORK_ISOLATED=1` 并使用合成 owner，且以 `HYPRIAL_BUILD_SKIP_NATIVE=1` 安装可编辑源码。因此这里只能验证同一 SHA 的构建、sidecar 打包和隔离 GUI 本地入口。不能把该结果记为真实设备加入、跨节点、外部登录、wheel 安装态或 M2/G1–G8 完整验收通过。完整目标和后续阶段见 [设计](../design/gui-full-acceptance-2026-11-11.md)。

## 本地实测（2026-11-11）

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| 候选源码版本一致 | PASS | 镜像标签与启动记录的后端、GUI 均为 `97129c031ca8`；测试确认 dirty GUI 文件未进入上下文 |
| sidecar 构建及包内校验 | PASS | 构建期及运行容器中均返回 `v=6`、`sidecar=0.1.0`、`tailcat=b4dc28e8aa8936f0a90a41ad8293a64e3d6b645f` |
| 本地 GUI 健康入口 | PASS | 运行容器的 `http://127.0.0.1:4178/__gui/healthz` 返回 HTTP 200 |
| 双节点连接与真实首次旅程 | UNKNOWN | 此阶段尚未执行，不由上述健康检查推断 |
| wheel 安装态与外部服务 | UNKNOWN | 此阶段没有构建完整 wheel，也未使用测试账号 |

宿主启动器与容器入口的焦点测试共 8 项通过。入口脚本默认使用容器内 4173；宿主测试通过 `HYPRIAL_GUI_APP_PORT` 选择空闲端口，避免与现有手动测试实例冲突。该变量只改变 GUI 与本地代理之间的内部端口。

`npm --prefix gui run test:browser` 的五个开发入口视口（375/760/800/900/1440px）均通过；`verify:first-run-browser` 在重新构建 `gui/dist` 后的开发与生产各四个视口也通过。这只证明当前浏览器契约，未证明真实账号登录或 M2 全流程。

这次实测镜像 ID 为 `sha256:4b49ca0a0cc2d8b9eec0b5d15b14128680239485fb1228db857b8adf9cdd84cf`。该记录只适用于上述候选提交；分支更新后须重新构建与验收。
