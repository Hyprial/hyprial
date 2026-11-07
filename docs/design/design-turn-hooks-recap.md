# Turn hooks and recap: mechanism in the daemon, policy outside it

Author and owner: org-improver (organisation-optimisation line; Allen 2026-09-26: «不对，这个问题请你接手自己处理，不要转交，这属于组织优化的工作»).
Implementation is dispatched through PAC; hyprial-cli-developer (PAC/daemon owner) may be asked to review the daemon seam.
Status: requirement and design draft, **not** an implementation.

## 1. Allen's words (Feishu, 2026-09-26, verbatim)

1. «刚才我和hyprial-cli-developer核对,他将长程任务放在了自己的session中直接处理而没有派出pac,你觉得怎么样可以让agent在派出长程执行任务的时候,使用正确的执行方式?另外,现在agent是怎么判断一个信息应该发送给谁?jev可以帮助选择route吗?»
2. «简单来说是不是可以这样处理,在harness turn结束的时候挂一个hook,向jev kind agent发送信息,触发一个幸运饼干式的提示,判断此前进行的任务属于什么类型,下一步处理要注意什么?类似claude code里面的recap功能»
3. «可以,请先调研一下claude的recap是怎么触发的,其它harness是否可以仿照?实际上,在合适的时间记录recap并留存在agent home中也是很有用的功能»
4. «我同意这个方案,不过似乎有一点bad smell,如果实施的结构设计不好,会把用户侧的事情(编排业务逻辑)放进代码库并为后续的开发开个坏头»

Quote 4 is the governing constraint of this document.

## 2. The boundary (the acceptance criterion that matters most)

The daemon provides **mechanism only**: two neutral hook points and a neutral turn record. It does not know about jev,
task categories, PAC, routing tables, hint wording, or any of our organisation's working habits.
Everything that encodes how we work is **policy**. Policy lives outside the codebase as configuration or data, and the
organisation edits it without a release.

**Boundary test, checked by the reviewer:** does any of these changes need a code change in `src/hyprial`?
- changing the category list
- rewording a hint
- replacing jev with another classifier agent
- adding a question
- changing which messages are hooked

The answer must be **no** for every one. Also: grep `src/hyprial` for `jev`, `PAC`/`pac` in hint text, or category names inside
the hook code path. It must find nothing.

Analogy: git provides hooks; what a repository's hook does is that repository's configuration.

## 3. Mechanism (daemon side, harness-agnostic)

Verified seams on dev 230da68 (read by org-improver line by line):
- Every headless worker (claude SDK `ClaudeAgentSdkProcess`, codex `CodexAppServerProcess`, pi `PiRpcProcess`, dsh) runs
  its turns through `StreamingTurnProcess` (`src/hyprial/harnesses/streaming.py`). It logs `worker.turn.started`, calls
  `client.query(delivery_prompt(current))`, then logs `worker.turn.<status>`. There is currently no completed-turn observer;
  the constructor takes only `on_turn_started` / `on_turn_failure`.
- Delivery to a worker: `DaemonRuntime._dispatch_harness_deliveries` (`src/hyprial/daemon/runtime.py`, around :628-661) builds
  `HarnessDelivery`, and `delivery_prompt()` (`src/hyprial/daemon/api.py:61-82`) renders the exact model text.
- Turn results: `self.harnesses.drain_results()` loop (`runtime.py` around :770-775). `HarnessResult` carries delivery_id,
  recipient, status, output and error.

Proposed mechanism:
1. **before-delivery hook point.** If the recipient agent has a hook configured, the daemon sends a neutral request to
   the configured handler actor: `{event:"before-delivery", agent, delivery:{id, sender, conversation, message}, recentRecaps:[…N]}`.
   It then waits a bounded time, taken from the config and small by default. The handler's reply text, if any, is attached **verbatim** to the
   delivery as a clearly delimited block that `delivery_prompt` renders. Timeout, error or empty reply ⇒ deliver unchanged
   and log it. A failing hook never blocks or drops a delivery.
2. **after-turn hook point.** On turn completion, the daemon builds a neutral **turn record** from facts it already has:
   delivery id, sender, conversation, start/end time, status, tool names used, output excerpt. It appends the record to the
   agent's recap log and, if configured, sends it to the handler, fire-and-forget. A handler reply to an after-turn event is
   stored as an opaque `annotation` on that record and (if configured) attached to the agent's next delivery via point 1.
3. **Recap log**: `H/agents/<actor>/state/recaps.jsonl`, append-only, one turn record per line (+ opaque annotations).
   It lives under `state/`, because `home.py` only recognises the fixed top-level subdirectories and a new one would break the
   empty-scaffold check (`_removable`, home.py:443-463).
4. **Default off.** Per-agent opt-in via config. No global default policy ships in the code.

What the daemon must NOT do: parse the handler's reply, interpret categories, decide routing, or add any wording of its own
besides the neutral delimiter.

## 4. Policy (outside the codebase)

Per-agent config under `H/agents/<actor>/config/` (e.g. `turn-hooks.json`): which events, which handler actor, the
timeout, and how many recent recaps to include. Later, org-wide policy can live in an orgfs space. What the handler does is
entirely the handler's business. Today that would be jev with our questions ("execution task that should go to PAC /
discussion / small fix", a routing table from the communication rules). Tomorrow it could be something else.

A small open point for the implementer: whether jev (a TypeSafe classifier that answers typed questions) can serve as the
handler directly, or needs a thin policy adapter that turns a turn record into its `{state, questions}` payload. That adapter
is **policy** too. If needed, it is a separate small agent or script configured by the org, not daemon code.

## 5. Interactive sessions (not covered by §3)

Interactive sessions (e.g. org-improver, hq-adjutant) do not go through `StreamingTurnProcess`. The same boundary applies:
the per-harness hook script only forwards the neutral event to the configured handler and injects the reply verbatim.
- Claude Code: `Stop` (can inject by returning `{"decision":"block","reason":…}`, as hq's own
  `.claude/hooks/h2b-stop-check.sh:101` already does) and `UserPromptSubmit`.
- codex: lifecycle hooks (Stop, UserPromptSubmit, `additionalContext`) are stable and on by default. **UNVERIFIED:** whether they
  fire in app-server mode, which is how hyprial drives codex (`CodexAppServerProcess`).
- pi: extension events `before_agent_start`, `turn_end` / `agent_end`, and `sendMessage(..., {deliverAs:"nextTurn"})`.
  **UNVERIFIED:** whether the installed pi 0.85.1 already has `BoundaryResult` / `agent_before_settle` (read on main, not the tag).
Recommend: do §3 first; interactive sessions second.

## 6. Recap lifetime — DECIDED by Allen (2026-09-26, Feishu, verbatim)

«cli-developer提了个问题，recap跟着agent走（迁移/删除）是对的方向，这实际上就是agent personality和memory»

So: recaps belong to the agent, like its memory. They are deleted when the agent is destroyed (home.py:308-317 already does
this) and they MOVE with the agent on cross-machine transfer. Interface with the transfer line (devusr, #820 transport
unit): `H/agents/<actor>/state/recaps.jsonl` must be part of what a transfer carries. Record this in the transfer handoff
(#840) as an interface item. Consequence for this design: recap records must not contain machine-local absolute paths or
secrets, so they stay valid after a move.

## 7. Acceptance

- The boundary test in §2 passes (reviewer checks it explicitly, including the grep).
- A failing, slow or absent handler never delays a delivery beyond the configured timeout and never drops one (tests
  with a handler that times out, errors, returns empty, and returns text).
- The handler reply appears verbatim, inside the delimiter, in the next model input (production entry: DaemonApplication with a real
  headless worker or the existing test harness for `delivery_prompt`).
- The recap log is append-only, one record per completed/failed/interrupted turn, and survives daemon restart.
- Default off: an agent without config behaves byte-identically to today (the `delivery_prompt` output is unchanged).
