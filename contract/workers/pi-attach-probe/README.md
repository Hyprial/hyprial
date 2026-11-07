# pi interactive attach — 版本守卫探针

来源:attach-pi 的首单调研(hq/notes/research-pi-interactive-attach.md,归档
db97c1e),实测于 pi 0.83.0;随 `feat/tui-attach` 入库。
用途:**pi(或 Claude Code)升级后重跑**,验证 attach 依赖的扩展 API 未漂移。

## 文件

- `probe.ts` — 生命周期/注入探针:验证 ① `-e` 在 TUI 加载(`ctx.mode==="tui"`)、
  ② idle 注入 `pi.sendUserMessage` 触发 turn、③ streaming 裸注入被拒(`input`
  事件伪阳性,不能当投递凭证)、④ `deliverAs:"followUp"` 在 idle/busy 两态都安
  全、⑤ `agent_settled` 在交互模式发且 `isIdle`、⑥ `getSessionId()` 可读。
- `probe2.ts` — 桥扩展兼容探针:验证 `pi_harness_bridge.ts` 原样载入 TUI、
  `harness_whoami` 可调、无 `HYPRIAL_WORKER_*` 时 fail-closed。
- `probe.jsonl` / `probe2.jsonl` — 0.83.0 的基准输出(golden)。

## 跑法(隔离,不碰生产 daemon)

```bash
mkdir -p /tmp/pi-attach-probe/{sessions,cwd}
cd /tmp/pi-attach-probe/cwd
env -u PI_SESSION_ID -u PI_SESSION_FILE -u PI_CODING_AGENT \
    -u HYPRIAL_WORKER_ACTOR -u HYPRIAL_WORKER_SESSION_REF -u HYPRIAL_HOME -u HARNESS_STATE_DIR \
    PROBE_LOG=/tmp/pi-attach-probe/probe.jsonl \
    nohup sh -c 'tail -f /dev/null | script -q /tmp/pi-attach-probe/tui.raw \
      pi --session-dir /tmp/pi-attach-probe/sessions -e <本目录>/probe.ts' &
# 等 ~60s(两个 LLM turn),比对 probe.jsonl 与 golden,然后按 PID 清理全部进程
```

probe2 同理,`-e src/hyprial/harnesses/pi_harness_bridge.ts -e probe2.ts` 双扩展。
注意:cwd 必须空(无 `.pi`/`.agents`),否则项目信任对话框阻塞 session_start(调研 E8)。
`tail -f /dev/null |` 是防 BSD script stdin EOF 秒杀 TUI 的关键。
