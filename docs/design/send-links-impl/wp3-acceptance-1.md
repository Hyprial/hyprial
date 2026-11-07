# WP3-A 第一次验收（opus，2026-10-14）：REJECTED，返工清单

验收对象：wp3/send-links @ 3e2b20926，base 2120854e3。Stage A 的 7 条验收标准中 1、3、4、5、6 PASS，2 UNKNOWN（替代抽样 10 个目录 HEAD 与 base 失败集相同，无回归），先红 PASS。被拒原因两条。

## 不符项（必须全部修）

B1. 【link_governed 重复实现】`src/hyprial/daemon/impl/dispatch/admission.py:111-125` 新增的 `link_governed(*, recipient, sender)` 与 WP4 分支同文件里的 `link_governed(sender, recipient, *, internal_actors=frozenset())` 签名不同、语义不同（WP3 版只看 `agent:` 前缀，不豁免 daemon 内部 actor）。实测缺陷：`agent:op:node-1:dispatch-coordinator`（user-proxy 转发、PAC 通知的发送方）发给 agent 的消息在无 gate 时 read_body 返回 MESSAGE_BODY_UNAVAILABLE。要求：把本分支的 `link_governed` 改成与 WP4 完全相同的签名与实现（参考只读副本 /private/tmp/hf-wp4-send-links/src/hyprial/daemon/impl/dispatch/admission.py 中的 `link_governed`、`_INTERNAL_ACTOR_NAMES` 等；按 README 裁定 45/51，内部 actor 按保留名判、不限节点，sender 与 recipient 任一为内部 actor 即不受约束），read_body 的调用处传 `internal_actors`（至少含本节点 `_dispatch_service_actor`）。加测试：来自 dispatch-coordinator 的转发在无 gate 时可读。合并时 admission.py 以 WP4 为准，本分支的函数体必须逐字相同以消冲突。
B2. 【评审 §8 要求的测试缺失】
- R2：两种回复形态（message.reply、message.send 带 replyTo）都在 outbox、custody、线框、inbox 上保留原 id；复用同一 conversation 的普通 send 不带 replyTo 且原请求仍 pending；被拒的 send(replyTo=…)（summary 超限）不 ack 原消息。现只测了 message.reply 的超限。
- R3：gate 未绑定、admit=False（W 满或互引未到）、False→True 过渡、held 行不经 retained-row 回退泄漏；重复读只准入一次；owner 不受 W 限；有效回复不受 W 限。现没有任何用 FakeLinkGate(admit=False) 的测试。
- R4：用真实 `_trusted_message_origin="ipc"`：强制认证下未认证调用者、observe 模式无 fence、伪造/过期/被顶替的 fence、有效 worker fence；每种拒绝断言发送方回执与 publisher 调用计数不变；read_body 与 settle_hint 在 owner onBehalfOf 下、以及对别人消息的行为。现只有 SESSION_SUPERSEDED 一例且全部直接调 handle()。
- A3：补「Stage A 缺 summary 的 send 仍成功」的测试（Stage B 要翻转它）。

## 同时做（集成裁定）
- 裁定 39：`message.send` 的 `replyTo` 校验为非空字符串再落 payload。
- `_admit_read_body` 因 gate 未绑定拒绝时打与 WP4 相同的 `link.gate_unbound` 日志。

## 交给 WP8 的契约清单（本包不改 contract/）
见验收报告：wire golden 与 ipc/wire.py 的 summary 映射（后者按裁定 40 归 WP3-B）、mcp-tools、cli-blackbox、worker 协议 schema、e2e runners 与文档；SUMMARY_REQUIRED {summaryBytes,maxBytes}、MESSAGE_BODY_UNAVAILABLE、settle_hint 两态、read_body 记 fetched、Stage B 无 message 键的 golden；升级连续性场景。
