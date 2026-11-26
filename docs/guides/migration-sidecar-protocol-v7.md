# 迁移说明：hyprial-tailcat stdio 协议升级到 v7

适用：升级到支持 `expose` 按公钥集合准入（`admission: "keys"`）的 `hyprial-tailcat` 版本。
本变更是封闭线协议的大版本升级，代码里不保留 v6 兼容路径；同一台机器上的 daemon 与 `hyprial-tailcat` 边车必须同批升级。
边车之间的远端控制协议仍为 v1，跨机器的升级先后不影响节点间互通。

## 变化

- `hyprial-tailcat` 的 stdio JSON 协议、`version` 输出以及 `genkey` 输出的版本号由 `6` 升为 `7`。
- `expose` 命令必须显式提供 `admission`（`"allow"` 或 `"keys"`，缺省直接拒绝返回 `BAD_REQUEST`），并在 `admission: "keys"` 时提供 `keys` 数组（对端针对本机 `serverPublic` 派生的节点公钥列表，须使用规范写法 `nodekey:<64位小写十六进制>`、不可重复、上限 4096 项；空数组拒绝所有连接）。
  - 同一端口以相同 `target` 与 `proxyProtocol` 再次 `expose` 时，边车就地替换准入规则并关闭已建连接中不再被新规则放行的连接。
  - `exposed` 事件回显 `admission`，并在 `keys` 模式下回显 `keyCount`（整数），不回显完整 `keys` 列表。
- 任何版本不是 `7` 的 stdio 命令（包括旧版 v6）一律被边车拒绝并返回 `SIDECAR_PROTOCOL_MISMATCH`。

## 需要做的

1. 停止当前运行的 daemon 与旧版 `hyprial-tailcat` 进程。
2. 同批更新 daemon（Python 包 / wheel）与 `hyprial-tailcat` 二进制（源码安装需重新构建边车二进制；使用 `HYPRIAL_TAILCAT_BINARY` 覆盖路径的环境须同步指向新版二进制）。
3. 用 `hyprial-tailcat version` 确认输出里 `"v":7`。
4. 重新启动 daemon。
5. 若本机有自定义脚本或测试桩直接向 `hyprial-tailcat forward` 发送 JSON 行，将请求中的 `"v": 6` 更新为 `"v": 7`。
