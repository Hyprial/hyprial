# E2E-018: workflow v2 — Allen 例子的图 + flag 反应器 + 只读投影

## Preconditions

隔离 daemon + 脚本化身份（无需外部服务；`real_environment_required: false`）。
运行：`contract/e2e/run-pac-graph-slice.py`。

## Steps

1. `hyprial init`（隔离根）+ `hyprial agent create` 建 7 个 owner：feat-bot / pm-helper /
   pm / dev-help / dev / deploy-help / deploy；从 `agent list` 读取各自的正式 `a.<id>` URI，
   不再用名字构造地址。脚本化 channel 身份按当前协议注册并通过 lease 心跳保持存活。
2. 用一个 workflow v2 JSON 文档声明 spec（丙）版的 13 节点、10 forward、2 back；
   `hyprial workflow plan <file> --json` 验证后，以带唯一 operation key 的
   `hyprial workflow run <file> --yes --json` 发布。format 2 的 node id 只允许 ASCII，
   所以原语义节点映射为 `实施 → implement`、`pm-不通过 → pm-reject`、
   `dev-不通过 → dev-reject`、`上线 → go-live`。
3. 两个声明式负例均不落库：forward 环由 plan 以 `WORKFLOW_SCHEMA_ERROR` 拒绝；
   未注册的完整 owner URI 由 run 以 `SENDER_UNRESOLVED` 拒绝。
4. 每个 set 由 owner 的已注册 session 运行
   `hyprial workflow complete <graph> <node> --request-id <current> --reason-ref <ref>`；
   每个 reset 运行 `hyprial workflow reset`。human 冒充 owner、过期 sessionRef、
   错误 requestId 都被拒绝且不写 flag fact。过期 session 的 CLI 公开错误为
   `PAC_PRINCIPAL_UNVERIFIED`，其 `cause` 仍为 `CALLER_NOT_AUTHORIZED`。
5. 首条 workflow dispatch 带 task brief reference 和 request event id，conversation 为
   `pac-<graph>`。reset 后公开事件出现 `workflow_changed state=withdrawn`；旧 request id
   以 `WORKFLOW_REQUEST_STALE` 拒绝，后置 flag 不自动改变。
6. `demo` 完成后 `pm-confirm-2`（pm 的第二个确认节点）为第 1 round 的 current request，
   且 `deploy-help` 尚未收到 request。
7. `pm-reject` back edge 后，pm-helper 按 explicit rework 先 reset；隔离 cadence tick 随后
   产生新的 workflow request。审计这两个不同 delivery 的 event id、edge、text head、sender；
   节点仍为 13 个，flag facts 数出 pm-helper 第 2 次 set。同一 `(event_id, edge)` 不重复。
8. 中途 `hyprial workflow inspect <graph> --node pm-confirm-2 --context --json` 显示
   `completedCount=0`、`currentActivation.round=1`、前驱为 demo，读取前后数据库逻辑行不变。
9. `hyprial workflow events <graph> --after 0 --json` 精确匹配 26 个 flag facts、
   18 个非控制根的逻辑 workflow requests 和 7 个显式 resets；纯 flag fold / 全字段 drift
   继续由 `tests/daemon/pac/projection/test_pac_restate.py` 等单测把关。
10. 向 pm-helper inbox 写入毒回复正文，断言 flags / notifications / snapshot / 数据库字节均不变。

## Assertions

1. 声明图为 13 节点 / 12 边（10 forward + 2 explicit back）；forward 环和 unknown owner
   分别以实测 v2 code 拒绝，且无坏图落库。
2. owner/session/current-request 三重 fence 生效；dispatch 带 brief reference、request event id
   和 `pac-<graph>` conversation。
3. reset 产生公开 withdrawn transition，旧 request 变 stale，后置 flag 不自动改变。
4. demo 只触发 pm-confirm-2 的第 1 round，deploy-help 不提前执行。
5. pm-reject 产生 explicit rework + 新 workflow request 两个独立 delivery；节点数不变，
   pm-helper set #2 可数，且 `(event_id, edge)` 幂等。
6. workflow inspect --node --context 分离 completedCount/currentActivation，且为只读。
7. 公开 events 精确匹配 26 个 flag facts、18 个逻辑 requests、7 个 resets；纯 fold/drift
   单测仍保留。
8. 毒回复正文不改变任何 flag、notification 或 projection。

## Retired

- 增量图编辑 CAS：`--expect-version` / `PAC_GRAPH_VERSION_CONFLICT` 随 direct PAC CLI 在
  commit `ccd1ddc6` 删除。workflow v2 只有整文档 plan/run，没有可替代的 CAS surface。
- managed graph 的固定 `7 withdraw notifications` 计数没有 workflow-v2 surface。v2 以
  `workflow_changed(state=withdrawn)` + stale request fence 表达撤回；本场景保留并验证全部
  7 个 reset facts，但不伪造旧 notification 计数。
