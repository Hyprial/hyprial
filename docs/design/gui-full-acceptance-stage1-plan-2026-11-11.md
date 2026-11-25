# GUI 完整验收第一阶段实施计划

**目标：** 使一个固定提交的 GUI 源码候选镜像携带可校验的 tailcat sidecar，并提供可复现的本地验收入口。此阶段的 PASS 仅覆盖构建、sidecar 版本和隔离本地 GUI；M2/G1–G8 的业务门仍按 [完整设计](gui-full-acceptance-2026-11-11.md) 继续实施。

**实施状态（2026-11-11）：** 已在现有 `run-gui-debug.sh` 中增加 `acceptance` 模式，通过 `make accept-gui` 调用；未另建脚本。下列清单仍含未完成的门，不能作为全部完成的证明。首次 Docker 实测使用已提交源码 `97129c031ca8` 与当时工作区的脚本、Dockerfile；推送候选提交后须按新 SHA 重新构建。

**架构：** 基于现有 `make debug-gui` 的构建脚本增加独立的完整验收入口，固定后端和 GUI 到同一提交。Go builder 使用 `sidecar/hyprial-tailcat/go.mod`、`go.sum` 与源码构建二进制，镜像把它放进包内 `_bin/` 并在构建时校验协议与引擎 pin。调试入口维持原有双快照行为，验收入口不得混用远端 GUI。

**技术：** Bash、Docker、Go 1.27.1、uv/Python 3.12、Node 24、pytest。

**全局约束：** 不读取宿主日常 Hyprial home、daemon socket、凭据或 Lark 配置；构建产物不得包含测试账号秘密；真实功能仍以 daemon 回执为准。仓库的 `AGENTS.md` 及 `docs/reference/known-problems.md` 适用。

## Task 1：固定单一提交的构建上下文

**文件：** `scripts/build/docker/run-gui-debug.sh`、`tests/tooling/gui/test_docker_debug_container_launcher.py`。

**接口：** `make accept-gui MODE=normal|dev PORT=<port>` 调用脚本；脚本用 `git archive HEAD` 归档 `pyproject.toml`、`uv.lock`、`VERSION`、`README.md`、`src/`、`gui/`、`sidecar/hyprial-tailcat/`。Docker 启动资源从当前脚本目录拷贝。候选 SHA 只取一次并进入镜像标签和容器 label。

- [x] fake Docker/Git 场景验证 dirty GUI 不进入上下文，sidecar 源码存在、无宿主挂载；新增 acceptance 用例先失败后通过。
- [x] 验收入口只归档当前 HEAD，不 clone 远端 `dev`；沿用私密 handoff，不在 stdout 输出 grant。
- [x] 焦点测试与 `bash -n scripts/build/docker/run-gui-debug.sh` 通过。
- [ ] 最终候选提交后核对镜像与容器 SHA 标签。

## Task 2：在镜像中构建并校验 sidecar

**文件：** `scripts/build/docker/Dockerfile.gui-acceptance`（新）、真实 Docker 构建与运行容器校验。

**接口：** 构建期执行 `verify_tailcat_sidecar(Path('/tmp/unused'))`；成功输出只记录非秘密的协议版本和 tailcat commit。二进制必须在 `hyprial/_bin/hyprial-tailcat`，与源码 resolver 相同。

- [x] 旧 debug 镜像实测 `SIDECAR_MISSING`；新镜像构建期和运行容器内均通过 `verify_tailcat_sidecar()`，返回协议 v6 与固定引擎提交。
- [x] Go 构建阶段复用 `Dockerfile.hyprial-worker` 的 module pin 检查；二进制进入包的 `_bin/`，校验是构建硬门。
- [ ] 最终候选提交后重建，核对新 SHA 的镜像 ID、标签与运行容器。

## Task 3：本地双节点与清理证据

**文件：** 现有本地 relay E2E、`docs/guides/gui-acceptance.md`；运行编排仍待实现。

**接口：** 两个临时 home 与独立端口；复用 `sidecar/e2e-zenoh-slots/run_slots_e2e.py` 的本地 relay/slot 验证，不将本地成功写成真实外部 tailnet 成功。每次运行输出不含凭据的候选 SHA、镜像 ID、sidecar version、场景状态与证据路径。

- [ ] 先写失败场景：sidecar 缺失或 pin 不符时验收入口失败，禁止静默退回 `HYPRIAL_NETWORK_ISOLATED=1`；中断后只销毁本次创建的容器和临时 home。
- [ ] 增加本地双节点运行命令和机器可读结果，沿用已有 E2E runner，避免自造第二套网络控制面。
- [ ] 运行本地双节点 E2E、镜像构建及 GUI 浏览器入口；逐项记 `PASS`/`FAIL`/`BLOCKED`，不把未跑场景计作通过。

## 第一阶段完成门

运行焦点 pytest、Shell 语法、结构与模块边界检查、`git diff --check`；真实 Docker 镜像须通过 sidecar 校验和单一 SHA 检查。之后写第二阶段计划，处理 KP-025 与空 home M2 首次旅程；最终再执行 G1–G8 全套验收。任何阶段发现现有功能缺口，先补真实契约和授权测试，不用 fixture 成功替代。

宿主 `test_docker_debug_container_entrypoint.py` 的两个用例初次失败，诊断日志显示固定的 4173 端口被现有实例占用。入口脚本已支持 `HYPRIAL_GUI_APP_PORT` 覆盖，测试改用空闲端口并经红绿验证，现为 2 PASS；Docker 默认仍使用 4173。
