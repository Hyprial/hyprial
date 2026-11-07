# user-proxy harness 契约 spec(v1)

状态:**用途已裁(Allen 2026-09-23),形态与契约为本 spec 的提议,待 Allen 过目**。
本文只定契约,⛔ 不写实现:`capabilities.py` / `cli.py` / daemon 的现有拒绝是当前的诚实基线,
保持不动,由后续实施单改。

归属:hyprial-cli-developer(Allen 2026-09-21「this work belongs to hyprial-cli-developer」;
2026-09-23「请接手 user-proxy 的工作继续推进」)。现状读数取自 `dev` @ `e448cc55`。

记法:每条结论标 **【已核】**(带文件:行号或可复跑命令)、**【已裁】**(带谁、何时、原话)
或 **【提议 · 待谁】**。没有出处的句子不写。

---

## 0. 现状读数(可复跑)

| # | 读数 | 出处 |
|---|---|---|
| R1 | `user-proxy` 在合法 harness 集合里 | `src/hyprial/daemon/desired_state.py:35` |
| R2 | `hyprial agent start user-proxy` 在参数校验阶段拒绝,码 `UNSUPPORTED_CAPABILITY`,文案 `user-proxy handler contract is not defined` | `src/hyprial/cli.py:7452-7456` |
| R3 | 能力表两行九项全 `unsupported`;`HEADLESS_EXEC` / `WAKE_PUSH` 的 note 即上句 | `src/hyprial/harnesses/capabilities.py:252-278` |
| R4 | 并发声明 `HarnessConcurrency("sequential", 1)` | `capabilities.py:283-284` |
| R5 | daemon 在 `ps` 视图里把 `jev` 与 `user-proxy` 归为同一类(脚本型 worker) | `src/hyprial/daemon/application.py:4030` |
| R6 | 名字随 jev 一起引入,仓内没有设计记录 | `git log origin/dev -S'user-proxy' -- src` ⇒ 仅 `582c35a7`(2026-09-20);`git grep -n user-proxy origin/dev -- docs contract` ⇒ 0 |
| R7 | 可复用的先例:jev 的 `python_worker` 机制(协议 v1、schema、正/负例 fixture、validator) | `contract/workers/python-worker/`;`src/hyprial/harnesses/python_worker.py`、`_python_worker.py` |
| R8 | 声明 native/degraded 的机制必须链接到真实实现符号 | `tests/test_harness_capabilities.py:43-104` |
| R9 | degraded 必须写明丢了什么(硬校验) | `capabilities.py:52-53` |
| R10 | python_worker 子进程只能**回复**本次投递:结果帧变成回给发送者的 `HarnessResult`,子进程不能发给别人 | `python_worker.py`(result 帧 → `HarnessResult`,`:470-478`);jev 行 `PROACTIVE_SEND: unsupported`(`capabilities.py` jev 行) |
| R11 | 当前人与人的消息经 squire 转发,落在 `squire/addressing.py` 的 `user:` 路由 → 接收方侍从 adapter 的 owner DM | `src/hyprial/squire/addressing.py:1`(模块 docstring「Receiver-owned `user:` routing over Zenoh and a Squire adapter」) |
| R12 | 人现在没有 cli 命令读收件箱正文;`hyprial message inbox <actor>`(只读)正在另一张单里加 | `hyprial --help`;PR「feat(cli): hyprial query」(R2 改名) |

---

## 1. user-proxy 是什么 —— 【已裁】

Allen 2026-09-23 原话:

> «user-proxy 的作用是开一条单独的通道,使得用户可以通过 hyprial cli 直接联系用户。他在功能上等价于
> 人类用户在飞书中和别的用户 DM。之前我们都是通过 squire 进行转发,但这样消息容易混乱,因此建立一个
> 单独的通道接受直发信息»
>
> «但按照"agent 身份"其实没错,只是这个 agent 是一个简单的 python 程序,只是做消息的转发»
>
> «注意 user-proxy 上线后的大部分时间实际还是走飞书 adapter 来通信,只是不和 squire 等 agent 混在一起。
> 代价是回复的时候需要手动指名回复给谁»

据此:

- user-proxy 是**一个 agent**,每个人一个(替这个人收发);它的程序是 hyprial **打包的**转发程序,
  ⛔ 不是用户自带的程序。
- 它有**自己的**飞书 adapter 通道,⛔ 不与 squire 或其它 agent 共用。
- 方向一:别人(agent 或人)发给这个人的 user-proxy ⇒ 转发程序把消息送进这个人的飞书 DM,并标明来自谁。
- 方向二:这个人在那个 DM 里回复 ⇒ 转发程序把它发出去;**这个人必须写明回给谁**(代价,已裁)。
- 人不开飞书时,用 `hyprial message inbox <user-proxy>` 看(R12)。

## 2. 谁运行转发程序 —— 【提议 · 待 Allen 过目】

daemon 起的子进程,与 jev 同一套 `python_worker` 机制(R7):生命周期、重启预算、custody 全在父进程。
程序是打包的(与 jev 一样随 hyprial 发版),因此就绪帧可以像 jev 一样**校验 `scriptSha256`**。
用户自起进程、远端端点两种形态不做(第 9 节)。

## 3. 缺的那一块:转发需要"发给别人" —— 【提议 · 待 Allen 过目】

jev 只需要回复(R10);转发需要把消息发给**另一个**收件人。本 spec 不让子进程直接调 daemon,
而是在协议里加一种终态帧 `forward`:子进程说"把这段话以本 agent 的身份发给 X",**由父进程执行发送**。
这样子进程仍然不持有任何 inbox 行、不直接发消息,custody 规则不变(第 4 节)。

转发程序的规则(纯函数,不调外部服务):

| 收到的投递来自 | 转发程序输出 |
|---|---|
| 本 user-proxy 自己的飞书 adapter(即这个人本人) | 解析开头的收件人标记 ⇒ `forward` 给该收件人;**没写收件人** ⇒ `result` 回给本人,文案说明"请写明回给谁"及格式 |
| 任何其它发送者 | `forward` 给本人的飞书 DM(`route:<本 adapter>:<dm route>`),正文前加一行"来自 <发送者>" |

收件人标记格式【提议】:消息以 `@<agent 名或 URI>` 开头,例如 `@alice 午饭十二点`。
⛔ 不做模糊匹配:名字解析失败就退回"请写明回给谁",不猜。

## 4. 帧格式 —— 【提议,字段名待实施单冻结】

与 `contract/workers/python-worker/protocol.schema.json` **同信封、同严格度**(UTF-8 JSON 对象、一个 LF 结尾;
CRLF、未知版本、未知字段、重复键、非有限数字、超过 8 MiB 一律拒绝;状态机沿用
`starting -> ready -> accepting -> draining -> exited`)。与 jev 的差异:`kind` 为 `user-proxy`;
就绪帧没有 TypeSafe 相关字段;多一种终态帧 `forward`。

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://hyprial.com/hyprial/user-proxy/v1/protocol.schema.json",
  "title": "hyprial user-proxy protocol v1 (proposal)",
  "oneOf": [
    {
      "type": "object", "additionalProperties": false,
      "required": ["v", "op", "id", "payload"],
      "properties": {
        "v": {"const": 1}, "op": {"const": "call"},
        "id": {"type": "string", "minLength": 1},
        "payload": {
          "type": "object", "additionalProperties": false,
          "required": ["from", "message"],
          "properties": {
            "from": {"type": "string", "minLength": 1},
            "message": {"type": "string"}
          }
        }
      }
    },
    {
      "type": "object", "additionalProperties": false,
      "required": ["v", "op"],
      "properties": {"v": {"const": 1}, "op": {"const": "stop"}}
    },
    {
      "type": "object", "additionalProperties": false,
      "required": ["v", "type", "kind", "pid", "hyprialCommit", "scriptSha256"],
      "properties": {
        "v": {"const": 1}, "type": {"const": "ready"}, "kind": {"const": "user-proxy"},
        "pid": {"type": "integer", "minimum": 1},
        "hyprialCommit": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
        "scriptSha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}
      }
    },
    {
      "type": "object", "additionalProperties": false,
      "required": ["v", "type", "id", "segment", "durationMs", "ok"],
      "properties": {
        "v": {"const": 1}, "type": {"const": "metric"},
        "id": {"type": "string", "minLength": 1},
        "segment": {"enum": ["decode", "call", "send"]},
        "durationMs": {"type": "integer", "minimum": 0},
        "ok": {"type": "boolean"}, "skipped": {"type": "boolean"}
      }
    },
    {
      "type": "object", "additionalProperties": false,
      "required": ["v", "type", "id", "ok", "output"],
      "properties": {
        "v": {"const": 1}, "type": {"const": "result"},
        "id": {"type": "string", "minLength": 1},
        "ok": {"const": true},
        "output": {
          "type": "object", "additionalProperties": false,
          "required": ["message"], "properties": {"message": {"type": "string"}}
        }
      }
    },
    {
      "type": "object", "additionalProperties": false,
      "required": ["v", "type", "id", "ok", "to", "message"],
      "properties": {
        "v": {"const": 1}, "type": {"const": "forward"},
        "id": {"type": "string", "minLength": 1},
        "ok": {"const": true},
        "to": {"type": "string", "minLength": 1},
        "message": {"type": "string"}
      }
    },
    {
      "type": "object", "additionalProperties": false,
      "required": ["v", "type", "id", "ok", "stage", "code", "message", "retryable"],
      "properties": {
        "v": {"const": 1}, "type": {"const": "error"},
        "id": {"type": "string", "minLength": 1}, "ok": {"const": false},
        "stage": {"enum": ["decode", "call", "send", "protocol"]},
        "code": {"type": "string", "minLength": 1},
        "message": {"type": "string", "maxLength": 500},
        "retryable": {"type": "boolean"}
      }
    }
  ]
}
```

实施单须按 R7 同等程度交付:schema 文件 + 正例 fixture(含 `forward`)+ 覆盖状态表每行的负例 +
validator 接进 `contract/run-all.sh`。

## 5. custody —— 【已核,沿用既有语义】

- 父进程持有投递与 inbox 行;子进程只处理帧,⛔ 不 ack、不重试、不丢弃 inbox 行。
- 终态只能由 `result` / `forward` / `error` 帧给出;daemon inbox 永远是真相源
  (`docs/design/design-harness-capability-abstraction.md` §3 目标形态)。
- `forward` 的语义:父进程以本 user-proxy 的身份 `message.send` 给 `to`,**发送被接受后**才结清本次投递;
  发送被拒(收件人不存在等)⇒ 本次投递以错误结清,并回一条说明给本人(不静默丢)。

| 情形 | 结果 | 是否重投 |
|---|---|---|
| 就绪前退出 / 就绪帧非法 | `HARNESS_START_FAILED` | 否 |
| 运行中崩溃、帧非法、重复终态、未知 id | `HARNESS_TRANSIENT_FAILURE` | 是 |
| `forward` 的收件人不存在 | `FORWARD_TARGET_UNKNOWN`(新增) | 否;回一条说明给本人 |

## 6. 凭据 —— 【提议】

转发程序本身不调外部服务,**不需要任何凭据**。飞书那一侧的凭据由它专用的 adapter 持有,走现有
adapter 机制(`hyprial adapter add`),⛔ 不经转发程序。子进程环境白名单不放宽。

## 7. 并发为什么是 1 —— 【已核的依据】

转发必须**保序**:同一个人连续发出的几条,到对方那里顺序不能乱;别人发来的几条,进 DM 的顺序也不能乱。
所以保持 R4 的 `sequential, 1`,且不计划解锁。

## 8. 能力表落点 —— 【提议】

| 能力 | 本阶段 | 说明 |
|---|---|---|
| `HEADLESS_EXEC` | native(`python_worker`) | 满足 R8:实施单必须让 `PythonHarnessProcess` 真正承载 user-proxy |
| `WAKE_PUSH` | native(`python_worker`) | 与 jev 同 |
| `PROACTIVE_SEND` | degraded | 丢了什么:只能在处理一次投递时转发(`forward` 帧),不能无投递地主动发 |
| `EVENT_STREAM` | degraded | 丢了什么:只有 decode/call/send 三段 metric |
| `SESSION_LIFECYCLE` / `MODEL_SELECT` / `TOOL_INJECTION` / `PLUGIN_INJECTION` / `TURN_CONTROL` | unsupported | 无 |
| 非 headless 行 | 全 unsupported | 无交互形态 |

每条 native/degraded 同 PR 改 `tests/test_harness_capabilities.py`(R8、R9)。

## 9. 本阶段明确不做(按序)

| 阶段 | 内容 | 为什么不在本阶段 |
|---|---|---|
| U0 | `hyprial message inbox <actor>`(只读) | 已单独开单(R12),是 U1 的前置 |
| U1 | 实施:打包转发程序 + `forward` 帧 + 父进程发送 + 契约目录 + 能力表,解除 `cli.py:7452` 的拒绝 | 等本 spec 过目 |
| U2 | 每人一个 user-proxy 的开通流程(建 agent、onboard 专用 adapter、pin 入站到该 agent) | 依赖 U1 |
| U3 | squire 转发路径退役或并存规则 | 需要实际用一段时间再定 |
| U4 | 回复不写收件人时自动回给"上一位"的便利 | 已裁的代价是手动指名;是否放宽等 Allen |

## 10. 谁裁、谁审

| 事项 | 状态 | 谁 |
|---|---|---|
| user-proxy 的用途与"回复要手动指名" | 已裁 | Allen 2026-09-23(第 1 节原话) |
| 形态 i、`forward` 帧、`@名字` 标记格式、`FORWARD_TARGET_UNKNOWN` | 提议 | 待 Allen 过目 |
| 帧信封、custody、并发 1、能力表 | 本 spec 定,实施单可在不改语义下冻结字段名 | hyprial-cli-developer 起草;验收按 e2e-verifier 派单材料第四节 |
| 能力表与测试同 PR 的规矩 | 已生效 | `docs/design/design-harness-capability-abstraction.md` + R8 |
