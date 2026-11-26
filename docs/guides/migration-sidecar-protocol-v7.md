# 迁移说明：hyprial-tailcat stdio 协议升级到 v7

适用：升级到包含 service grant 线 S4-go（`expose` 按公钥集合准入）的版本。
本变更是封闭线协议的大版本升级，代码里不保留 v6 兼容路径；daemon 与 `hyprial-tailcat` 边车必须同批升级。

## 变化

- `hyprial-tailcat` 的 stdio JSON 协议、`version` 输出以及 `genkey` 输出的版本号由 `6` 升为 `7`。
- `expose` 命令在现有 `admission: "allow"` 之外新增 `admission: "keys"`，同时要求提供 `keys` 数组（对端针对本机 `serverPublic` 派生的节点公钥列表；空数组拒绝所有连接）。
  - 同一端口以相同 `target` 与 `proxyProtocol` 再次 `expose` 时，边车就地替换准入规则并关闭已建连接中不再被新规则放行的连接。
  - `exposed` 事件回显 `admission`，但不回显 `keys`。
- 任何版本不是 `7` 的 stdio 命令（包括旧版 v6）一律被边车拒绝并返回 `SIDECAR_PROTOCOL_MISMATCH`。

## 需要做的

1. 停止当前运行的 daemon 与旧版 `hyprial-tailcat` 进程。
2. 同批更新 daemon（Python 包 / wheel）与 `hyprial-tailcat` 二进制（源码安装需重新构建边车二进制；使用 `HYPRIAL_TAILCAT_BINARY` 覆盖路径的环境须同步指向新版二进制）。
3. 用 `hyprial-tailcat version` 确认输出里 `"v":7`。
4. 重新启动 daemon。
5. 若本机有自定义脚本或测试桩直接向 `hyprial-tailcat forward` 发送 JSON 行，将请求中的 `"v": 6` 更新为 `"v": 7`。
