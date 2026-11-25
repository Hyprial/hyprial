# GUI 单一启动路径：工作计划归档

- 日期：2026-11-15
- 性质：规划阶段由 planning agent 写下的工作计划（working plans），为防止规划用机器丢失而落库。
- 文档位置：本目录属 `docs/design/`；复现材料在
  [`docs/notes/gui-single-launch-prototypes-2026-11-15/`](../../notes/gui-single-launch-prototypes-2026-11-15/)（WORM，只追加）。

## 1. 这个目录是什么

这里的文件是各项工作的**详细计划与调查记录**，不是设计文档。记录在案的设计文档（document of record）只有两份，
与本目录冲突时以它们为准：

- [`gui-single-launch-2026-11-14.md`](../gui-single-launch-2026-11-14.md)
- [`daemon-first-init-login-decoupling-2026-11-15.md`](../daemon-first-init-login-decoupling-2026-11-15.md)

计划中可能含有估算，以及标为 UNKNOWN / BLOCKED 的陈述；这些陈述在写作时成立，其后可能已被合入的 PR 改变。
计划正文保持写作时的原文，只做了路径与主机信息的脱敏：规划用机器上的绝对路径改写为仓库相对路径或
“规划时的临时工作区”，PR 一律写作 `#NNNN`。已完成工作（1a 至 1d、4a、1e-1、1e-2 的逐步计划、1f 及其
feature areas、各 `*.patch`、编译好的 native engine）的结果在已合入的 PR 中，不在此归档。

## 2. 计划一览

状态截至 2026-11-15。已合入 PR：0 设计文档 #2755；1a 至 1d 与 4a 更早合入；1e-1 #2806；
F0 #2802、F0b #2809、F5 #2814、feature areas #2820、F6 #2825；2a #2826；1e-2 #2828；Z-0 #2829；
1e-3 #2831；6-pre #2832。进行中的 WIP：#2822（session host 并入 `request_http`）、
#2830（per-home OS service）、#2833（dev 红基线）。

更新（2026-11-19）：下表的状态列保持 2026-11-15 的读数。此后已合入 `dev` 的有：2b #2851、2c #2852、3 #2863、
4b-H #2892、4b-G #2891、4b-S2 #2905、5 #2895（4b-C 随 #2895）、6a #2871、6b #2840、6c #2889、1e-4 #2868；
daemon 先行 Z-1 #2850、Z-2 #2858、Z-3 #2861、Z-8a 至 Z-8c #2830、DL-1 #2845、Z-6a #2916、Z-6pre #2927、
Z-4 #2925、Z-4b #2944、Z-6b #2933、Z-6c #2946、Z-5 #2958 与 #2967、Z-7 #2963、#2976、#2977；#2833 已合入。
#2822（session host 并入 `request_http`）的步骤 1–4 已合入（#2985、#2995、#2996）；`hyprial gui` 的帮助文字待 owner 决定。
Z-9（文档收尾）是 `docs/z9-single-launch-wrap-up` 这一个 PR。

| 文件 | 覆盖的 PR | 状态（2026-11-15） | 尚待完成 | 对应 prototype 目录 |
|---|---|---|---|---|
| `host-2-3.md` | 2a、2b、2c、3（Python session host） | 2a 已合入 #2826；2b 在 review，未合入；2c、3 未开始 | 2b 按 `2b-verification.md` 的必改项修正后合入；2c 接通真实 backend；3 令源码 `hyprial gui` 改用 Python host 并删除 Node host | `host-2-3-prototype/` |
| `dev-4b-5.md` | 4b（`--with-dev-tools`）、5（`--live-source`） | 2026-11-16 更新：host 一侧已合入（PR 3、6a），`--live-source` 拓扑已决定并由 PR 3 实现（计划步骤 5-0、5-1 已完成）；4b-G 已实现于分支 `feat/gui-dev-tools-chunk`，已用普通 merge 同步到 `dev` @ `f20e37ca8`，未合入，mock 来源门有一项失败待修；4b-H、4b-C、4b-S2 与 5 的其余步骤未开始 | 按 `dev-tools-handoff.md` 的五个工作包完成；其中的 owner 待决问题 Q-1 至 Q-7 | `dev-4b-5-prototype/` |
| `dev-tools-handoff.md` | 4b-H、4b-G、4b-C、4b-S2、5（交接给外部开发者） | 2026-11-16 写成；对照 `dev` @ `f20e37ca8` 逐步复核了 `dev-4b-5.md`，列出工作包、安全要求、验收清单与待决问题 | 五个工作包全部；合入顺序 H → G → C → 5，S2 在 C 之后且需 GUI 线签字 | `dev-4b-5-prototype/` |
| `shell-6.md` | 6-pre、6a、6b、6c（原生壳） | 6-pre 已合入 #2832；6a、6b、6c 未开始 | 6a-0 先做测量（无代码）；6a 至 6c 全部；文末含 owner 决定 | 无 |
| `daemon-first.md` | Z-0 至 Z-9（daemon 先行，详细计划） | Z-0 已合入 #2829；Z-1（mock 的 `mutates` 修正）进行中；#2830 进行中 | Z-1 至 Z-9；DL-1 | `daemon-first-prototype/` |
| `daemon-lifecycle.md` | 同上（实测调查） | 调查完成，结论已折入记录在案的设计文档 | 无（事实表 T/G/Y/V 供 `daemon-first.md` 按编号引用） | `daemon-lifecycle-prototype/` |
| `1e.md` | 1e-1 至 1e-4，Stage F | 1e-1 #2806、1e-2 #2828、1e-3 #2831 已合入；1e-4 与 Stage F 未完成 | 1e-4（删除 reference 代理，关闭 KP-049，删除两个浏览器脚本，见第 4 节）；Stage F（fixture 替换） | `1e-2-driver-prototype.test.ts`、`1e-2-dump-prototype.py`、`1e-3-helper-prototype.mjs` |
| `1e-2.md` | 1e-2 | 已合入 #2828；留作 mock 设计决定 D1 至 D11 的记录 | 无 | 同上 |
| `1e-3-step0.md` | 1e-3 之后的 fixture 替换 | 1e-3 已合入 #2831；本文是场景缺口清单 | 作为 Stage F 的输入 | `1e-3-helper-prototype.mjs` |
| `2a-verification.md`、`2a-contract-delta.md` | 2a | review 报告；解释 session 契约现在的样子 | 无 | 无 |
| `2b-verification.md` | 2b | review 报告；解释 Python host 现在的样子 | 2b 的必改项仍待落实 | 无 |
| `1f-decisions.md` | 1f 及 F6 | 已完成（“不渲染不可用说明”这一线，F6 #2825） | 无；owner 决定 Q1 至 Q14 留作记录 | 无 |

## 3. 如何接手一项工作

1. 先读记录在案的设计文档（上面两份），再读对应的计划，最后读仓库根的 `AGENTS.md`。三者冲突时按此顺序取前者。
2. 计划写作时的 base 提交已写在各计划开头；动手前先把计划引用的源码行号与当前 `dev` 对照，行号可能已漂移。
3. 验证纪律：
   - 本地与 CI 一样使用 Python 3.12（`uv sync --extra test --python 3.12`），裸 `uv sync` 可能选到 3.13/3.14，
     出现 CI 不会出现的失败。
   - BLOCKED 不是 PASS，skip 不是结论；未实测项写 UNKNOWN 或 BLOCKED。
   - 修改产品 Python 的 PR 运行全量 `uv run --with libcst==1.9.0 pytest tests/`，不能只跑焦点测试。
   - 已知红基线见 `docs/reference/dev-red-baseline-2026-11-15.md`（#2833 合入后存在）；基线内的失败不算新缺陷，
     但须在 PR 正文列出。
4. 测试用的 home、socket、端口、缓存须与日常环境隔离，不读取或改动生产 daemon、凭据与用户级配置。

## 4. owner 决定汇总（截至 2026-11-15）

以下决定出自各计划的追加段落与 owner 的口头指示，尚未写入记录在案的设计文档。

- **Windows**：暂不在范围内。已有的 Windows 检查（如 `scripts/ci/check_windows_portability.py` 与 PR 模板中的对应项）
  保留、不删除，且须保持绿；Windows 独有的问题只记录、暂不处理，不新增 Windows 代码或测试。
- **打包目标**：macOS arm64 与 Linux x64。
- **应用标识**：bundle id `com.hyprial.hyprialos`，显示名 “HyprialOS”；使用 Developer ID 签名，
  凭据作为 runner secrets，由 owner 配置，仓库中只按名称引用。
- **应用 home**：应用使用自己的 `HYPRIAL_HOME`，与 `~/.hyprial` 分开（daemon 要求包版本精确一致，且每个 home 只有一个锁持有者）。
- **合入方式**：以本地测试结果代替 CI 队列合入，证据写在 PR 正文。
- **1e-4**：删除两个浏览器脚本 `org-lifecycle` 与 `org-rework`；它们覆盖的八类场景保留为 fixture 替换（Stage F）的验收清单。
- **14 组 CLI 到 daemon request 的集成**：由独立开发者实施，每组一个 PR，GUI owner 负责 review 与合入。
