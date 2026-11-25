# contract/ 名称对账报告

对象：`origin/intg/rename-hyprial@455f1a2e7c5781d617985f38cd7f6083a3809fd0` 的自身增量，目标分支 `wk/contract-name-align`。

交接账：上一单留给本单的债：无；已知无关红：无。绊线与禁令：不改两份历史证据，不改 adapter 注册名，不改 CI 服务端机密名，不修 `dsh-lock.json` 的悬空 repository/commit/path，不碰除此处点名事件 schema 与名称守卫以外的 `src/`、`tests/`、`sidecar/`。

## 已换

- 产品与命令面：`h2b` / `H2B` / `.h2b` / `h2b.cli` / `src/h2b` → `hyprial` / `Hyprial` / `.hyprial` / `hyprial.cli` / `src/hyprial`。代码佐证：`pyproject.toml` 的 distribution 与 console script、`src/hyprial/cli.py`。
- CLI 与解释器选择：`--h2b` / `H2B_BIN` / `H2B_PYTHON` → `--hyprial` / `HYPRIAL_BIN` / `HYPRIAL_PYTHON`。代码佐证：`contract/run-all.sh`、`tests/test_contract_python_resolver.py`。
- home 与身份：`H2B_HOME` / `H2B_OWNER` / `H2B_WORKER_ACTOR` / `H2B_WORKER_SESSION_REF` → 对应 `HYPRIAL_*`。代码佐证：`src/hyprial/daemon/identity.py`、`src/hyprial/cli.py`。
- Zenoh 与 inbox：`H2B_ZENOH_LISTEN` / `H2B_ZENOH_CONNECT` / `H2B_HOLD_TTL_MS` / `H2B_HOLD_MAX_ITEMS` / `H2B_STATUS_RETENTION_MS` → 对应 `HYPRIAL_*`。代码佐证：`src/hyprial/daemon/application.py`、`src/hyprial/inbox/pull.py`。
- Lark 与 resident runner：`H2B_LARK_APP_ID` / `H2B_LARK_APP_SECRET` / `H2B_LARK_READY_FD` / `H2B_E2E_LARK_CHAT_ID` / `H2B_RESIDENT_SKILL` → 对应 `HYPRIAL_*`。代码佐证：`src/hyprial/adapters/lark/onboarding.py`、`src/hyprial/adapters/lark/runtime.py`、`.forgejo/workflows/e2e-controlled.yml`。
- DSH runner：`H2B_DSH_ENDPOINT` / `H2B_DSH_AGENT_PRESET` / `H2B_DSH_REPLY_TIMEOUT` → 对应 `HYPRIAL_*`。代码佐证：`contract/e2e-scenarios/run-dsh-worker.py`。
- `tests/test_dsh_e2e_assets.py` 的 8 行属于跟着改名同步,不是为让红变绿而放宽：原断言要求 prompt 出现三项旧 `H2B_DSH_*` 名,现先断言整个 `H2B_` 前缀不存在,再逐项钉住 `HYPRIAL_DSH_ENDPOINT`、`HYPRIAL_DSH_AGENT_PRESET`、非机械出口 `DSH_HOME`,并补钉 `HYPRIAL_DSH_REPLY_TIMEOUT`。
- 非机械前缀两项：`H2B_DSH_HOME` → `DSH_HOME`，代码佐证 `src/hyprial/harnesses/dsh.py:390`；`H2B_CODEX_BIN` → `HARNESS_CODEX_BIN`，代码佐证 `src/hyprial/cli.py:5352`。
- Python 引用：`h2b.harnesses.capabilities` / `h2b.log.Logger` → `hyprial.harnesses.capabilities` / `hyprial.log.Logger`。代码佐证：`src/hyprial/agents/capabilities.py`、`src/hyprial/alarm.py`。
- SDK URL 来源：`python-sdk/h2b` → `python-sdk/hyprial`。代码佐证：`contract/e2e-scenarios/run-lark-app-onboarding.py` 的 URL query 断言。
- 运行资源：`h2b-e2e001-*` / `h2b-*` DSH preset → `hyprial-e2e001-*` / `hyprial-*`。代码佐证：`contract/e2e-scenarios/run-two-machine-roundtrip.py`、`src/hyprial/harnesses/dsh.py:436`。
- `h2b.agent-task.event/v1` → `hyprial.agent-task.event/v1`：四份 example、schema const、`agent_task.py` 两个消费者和测试输入八处同步；测试不靠修改 schema 校验绕绿。
- `mfu-h2b-agent-task` → `mfu-hyprial-agent-task`：只改本仓 contract/manifest 自身名称。

## 改名单漏项

- `+h2b/ack`：`contract/wire-golden/README.md` 保留。代码/设计面只有旧名 `docs/design.md:61`，没有 `+hyprial/ack`；本单不自行发明协议 tag。

## 已删死指令

- `contract/e2e-scenarios/19-hosted-transfer.md`：删除不存在的专用镜像 `h2b-e2e019-worker:local` 及其旧 preflight 报错；当前 runner 明示 `realContainer=NOT_RUN`，不存在可对账的新镜像名。

## 需要人点

### A. Forgejo 服务端机密名

本单未改 `.forgejo/workflows/sidecar.yml`。建议由仓管理员按“服务端先新增同值新名 → 改 workflow → CI 绿后删旧名”执行：

```diff
- secrets.H2B_TSNET_BIN_DEPLOY_KEY
+ secrets.HYPRIAL_TSNET_BIN_DEPLOY_KEY
- TS_OAUTH_CLIENT_ID_H2B_CI
+ TS_OAUTH_CLIENT_ID_HYPRIAL_CI
- TS_OAUTH_CLIENT_SECRET_H2B_CI
+ TS_OAUTH_CLIENT_SECRET_HYPRIAL_CI
```

### B. Adapter 注册名

`allenwoods-h2b-e2e` 全部原样保留：

- `contract/e2e-scenarios/{04-feishu-roundtrip,07-four-lark-adapters,09-squire-setup,13-all-harness-lifecycle}.md`
- `contract/e2e-scenarios/{run-all-harness-lifecycle,run-feishu-roundtrip,run-squire-setup,run-four-lark-adapters}.py`
- `contract/e2e-scenarios/feishu-live-roundtrip-evidence.md`

它已长进 route URI、凭据文件名、脚本常量、命令参数、生产迁移代码与 desired-state；属 adapter 改名迁移单,不在本单射程。`hq-adjutant-feishu`、`hyprial-kanban`、`mac-studio-feishu`、`allen-squire` 同属 adapter 注册名，亦未改。

### C. 悬空的 MFU lock

`contract/mfu-agent-task-v1/dsh-lock.json` 的 repository、commit、path 指向悬空，已上报；本单只做名称对账。`contract` 自身名已改，三项指针保持原样，没有伪造可达的新仓、新提交或新路径。README 中 HQ 决策文件名也保持原样。

### 验收读数

待最终提交后按该提交重取：内容维、路径维及各自正控；测试红绿、焦点、全量与全仓 Ruff 读数也在此补记。
