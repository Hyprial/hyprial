# E2E-017: outbox 死信三缺陷(C1/C2/C3)端到端

## Preconditions

隔离 daemon + 脚本化身份 + sqlite 预置死信行(自含,real_environment_required: false)。

## Steps

见下方"断言(7/7)"编号流程。

## Assertions


Status: executable via `run-outbox-deadletter.py`(自含,real_environment_required: false)。

跨组件链(单测只能看到一段,本场景的存在理由):
死消息持有 → 身份解析 → **绝不**出现在 `hyprial message targets`(C3);离线不按时重试、
上线无 ACK 才短间隔重试、唯一负终态是 TTL(C2);prune 的 C1 对照保持不变。

## 断言(7/7)

1. 隔离 daemon + 注册 duty-driver / sleeper;
2. 远程 ghost(canonical 未注册)消息入队;
3. C2:远程 ghost 离线 2s 后仍 `attempts=0`;
4. **C3 核心:ghost 在离线持有期间【从未】出现在 targets**(注册 sleeper 在列对照);
5. C2:把 attempts 种到旧 cap 以上，实时 pump 仍不生成
   `DELIVERY_RETRY_EXHAUSTED` / `DELIVERY_CONFIRMATION_UNRESOLVED`；再只把 TTL
   种到已过，终态恰为 `TTL_EXPIRED`，且之后 3s 零新增终态记录;
6. C1 对照组:裸名 host 行按 UNDELIVERABLE_SCHEME 清、同机未注册 ghost 按
   TARGET_UNRESOLVABLE 清、**注册但离线的 sleeper 不在 pruned 列表**，且每封
   sleeper 信要么仍在 outbox、要么有 `state=fetched, reason=ACK_RECEIVED`
   投递状态;
7. 终局 targets 仍无任何 ghost。

## 场景设计注记(实跑发现,也是行为知识)

- **同机 ghost 不存在"排队重试"**:daemon 代持本机 URI 的 inbox(#195),同机
  未注册 agent 的消息【即时送达】进其 inbox 行。故 C2/C3 用【远程】ghost;
  TARGET_UNRESOLVABLE 的 prune 侧用 sqlite 预置 + 远未来 due time 防泵抢先。
- 旧 cap 控制用"种子到 cap+2"，证明 attempts 只作观测，不能再终止邮件；TTL
  单独种到已过，避免闸门等待整个默认保留期。
- prune 对照组全部 sqlite 预置 + 远未来 due time(bare/local-ghost/sleeper)。
