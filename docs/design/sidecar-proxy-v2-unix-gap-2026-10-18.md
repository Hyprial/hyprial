# 边车未强制「PROXY v2 只能走 unix」的安全缺口（原始问题记录）

日期：2026-10-18。状态：WIP，只记录问题，不修复（Allen 裁决原话：「安全缺口开WIP PR但先不用修复」）。

## Resolution update — 2026-10-30

Implementation approved on 2026-10-30. Both Go validators now enforce the
private Unix target rule through `internal/privatefs.ProxyTarget`.
The original report below describes the pre-fix state; its WIP restriction
was superseded by that approval. Red/green commits and verification limits:
[implementation evidence](../evidence/sidecar-proxy-v2-unix-only-2026-10-30.md).
Windows named pipes remain out of scope.

## 规则

带身份的暴露入口（proxyProtocol=v2）只能指向 0700 私有目录中的 unix socket。理由见 src/hyprial/daemon/impl/forwarding/policy.py:41-47：「Any local process can reach a loopback TCP port and forge a PROXY v2 header, so an identity-carrying exposure needs a unix socket in a private directory. The sidecar enforces the same rule.」orgfs 网页入口也是同一条规则（src/hyprial/daemon/impl/orgfs/webserver/config.py:91-95）。

## 缺口

注释里写的「The sidecar enforces the same rule」与实际代码不符。边车的两处校验都接受 proxyProtocol=v2 搭配 tcp: 目标，也都不检查 unix 目录的权限：

- sidecar/hyprial-tailcat/internal/proto/message.go:476-495，ValidateExposure：v2 与 target 分开校验，tcp: 只要求是 loopback。
- sidecar/hyprial-tailcat/internal/servicehost/config.go:123-148，validateExposure：同上，tcp: 只要求 127.0.0.1。

后果：只要能绕开 daemon 侧的 policy，比如经 servicehost 的配置路径直接配置，或者 daemon 侧的校验以后出现回归，边车就会在 127.0.0.1 的 TCP 端口上转发带 PROXY v2 头的连接。本机任何进程都可以直接连上这个端口，自己伪造 PROXY v2 头，冒充任意对端设备。现在 daemon 侧的 policy 是唯一的防线。

## 修复方向（待排期）

1. 两处 Go 校验都加上：proxyProtocol 为 v2 时必须是 unix: 目标；unix 目标的父目录必须是 0700，且属于当前用户，不能是符号链接。
2. 加契约用例：在 contract/sidecar-tailcat/golden 的 invalid 集中加入「v2 加 tcp: 被拒」。
3. 先提交红测试，再提交修复。
4. Windows：按 Allen 的裁决，Windows 上带身份的入口改走命名管道（方案 B），另立工作项；边车侧届时新增 pipe: 目标的同等校验。

## 发现来源

研发部的 Windows AF_UNIX 调研（2026-10-18）顺带发现，并在 origin/dev 上逐行核对过上述行号。
