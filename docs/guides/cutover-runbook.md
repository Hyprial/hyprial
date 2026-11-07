# 退役 TypeScript 安装的处理方式

Allen 于 2026-09-11 确认旧 TypeScript 机器已经没有了，仓库因此不再提供旧状态
转换工具或组合清理脚本。当前路径是：**先删除旧版，再安装**。这是一条只在盘点
外意外发现旧安装时使用的恢复说明，不是需要在现有机器上执行的新迁移功能。

## 1. 先确认命中的是旧版

```sh
command -v hyprial
readlink -f "$(command -v hyprial)"
npm ls -g --depth=0 2>/dev/null | grep -F @hyprial/harness-bridge
```

只有当入口是 Node 脚本或 npm 全局包确实存在时，才按下面步骤处理。当前 Python
安装、生产 `~/.hyprial` 和任何不属于该旧安装的进程都不在处理范围内。

## 2. 停止旧 daemon，并归档旧状态

若旧版支持 supervision，先运行旧 CLI 的 `hyprial service uninstall --json`。若是
standalone 版本，从旧 CLI 的 `hyprial daemon status --json` 读取 daemon PID，只对该精确 PID
发送 `TERM` 并等待退出。不要使用 `pkill`、`killall` 或按进程名匹配。

确认旧 daemon 已停止后，将旧 home 整体移走，保留为私有归档，而不是把旧文件
混入新的 Python home：

```sh
legacy_home="$HOME/.hyprial"
legacy_archive="$HOME/.hyprial.ts-retired.$(date -u +%Y%m%dT%H%M%SZ)"
mv -- "$legacy_home" "$legacy_archive"
chmod 700 "$legacy_archive"
```

此归档可能含凭据；不要打印、上传或提交其中内容。当前路径**不迁移旧状态**，
channels、secrets、desired state、users 和消息历史都不会导入新安装。

## 3. 卸载旧包，再全新安装

```sh
npm uninstall -g @hyprial/harness-bridge
uv tool install --force git+ssh://git@git.internal.example.com/HyprialOS/harness-bridge.git@<version-tag>
```

再次运行 `command -v hyprial`，确认它不再解析到 Node 或旧 npm 路径。然后按
[运维指南](operator-guide.md#安装)为新的空 home 配置显式 Zenoh 端点并执行
`hyprial init`。不要把归档目录改回新 home，也不要从中复制旧状态文件。
