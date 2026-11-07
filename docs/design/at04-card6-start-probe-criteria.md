# 卡⑥ 73fed655 — 诊断：hosted-upgrade old 树首启超过 45s 的机制｜判据 v3.3（待冻结）

v1 → v2：接受评审 3ea715a6 的 B1–B7 和 minor。v2 → v3：接受评审 fcec1d83 的 K1–K5 和 minor。v3 → v3.1：接受评审 ae093238 的 W1–W3。v3.1 → v3.2：§9-3 自证实测发现「每墙钟秒 nivcsw」在 macOS 上区分力不足，把「nivcsw 速率」钉死为每 CPU 秒（见 §5 定义与 §9-3 数据）；其余不变。v3.2 → v3.3：PR #1019 首份 hosted 实测（run 12477）显示读墙钟含约 1s 的读内 CPU，使 R 可超过 1 而被判「口径异常」不计分，恰好排除读最多的样本；把 §5 的「读耗时」钉死为读的等待部分（墙钟 − 读内线程 CPU），与 G = 墙钟 − CPU 同口径；其余不变。

## 0. 目标与边界
- 目标：给「old 树 daemon 首启慢」定机制，把等待时间归入预登记假设 H1 / H-IO；可能的交付物有三种：「机制已定」「本批观察结果」（样本时间集中时）和「未定/混合」。按 §8，本批样本很可能时间集中，「本批观察结果」是现实的结局，已事先告知 amyws。
- 范围（K1）：只看 old 树 daemon。new 树不纳入慢样本，也不做对照。
- 不做：修复和修复验收；「修好了」不写进成功判据。不改 readiness 预算、任何场景的 PASS/FAIL 判定和 complete 判定。
- 只改测试装置：contract/e2e/run-host-invite.py 的 BOOTSTRAP、run-hosted-suite.py 的汇总（只作观察字段），以及测试。src/、pyproject、uv.lock、VERSION、workflows、docker 不碰。

## 1. 平台前提（B1）
- 本机实测（darwin，3.14.7）：F_NOCACHE 读 64MB 后 (inblock, majflt, minflt) 的增量是 (0, 0, 59)；mmap 逐页触碰 64MB 后是 (0, 0, 4096)。
- HQ 对照协议（评审 5aeed37c，已预登记）：
  - 在 Mac Studio 宿主的原生进程里跑，解释器用 old venv 的 python3.12；
  - 写入 256MB 后执行 sync；
  - 阳性：F_NOCACHE 读；阴性：同一文件普通读；
  - 各报 (ru_inblock, ru_oublock, ru_majflt, ru_minflt) 的增量；
  - 附 sw_vers、Python build 和平台信息，贴原始命令与原始输出。
  - 结果（card6-prep/control-raw.txt，sha256 5ea8716b…，原文照录）：
```
$ sw_vers; uname -a
ProductName:		macOS
ProductVersion:		26.5.2
BuildVersion:		25F84
Darwin node-hq-01.example-tailnet.ts.net 25.5.0 Darwin Kernel Version 25.5.0: Tue Jun  9 22:26:15 PDT 2026; root:xnu-12377.121.10~1/RELEASE_ARM64_T6031 arm64
$ PY=/private/tmp/hyprial-hosted-ci-023uwz/old/.venv/bin/python
$ $PY -c 'import sys,platform;print(sys.version);print(platform.platform())'
3.12.12 (main, Jan 27 2026, 23:41:44) [Clang 21.1.4 ]
macOS-26.5.2-arm64-arm-64bit
$ $PY - <<EOF (control script as sent)
positive F_NOCACHE {'ru_inblock': 0, 'ru_oublock': 0, 'ru_majflt': 0, 'ru_minflt': 130}
negative cached    {'ru_inblock': 0, 'ru_oublock': 0, 'ru_majflt': 0, 'ru_minflt': 0}
```
  - 判读：阳性 ru_inblock 增量 = 0 → inblock 不可用 → H2/H3 按规则合并为 H-IO，打分中彻底去掉 inblock/majflt。未执行 purge（HQ 无 sudo），不影响判读：F_NOCACHE 已绕过缓存；而且即使 F_NOCACHE 没有真正绕开缓存，结论（Darwin 不维护 inblock）也不变，因为它是直接由阳性读的 inblock=0 得出的。
- 预登记判读规则（不许事后调整）：
  - 阳性的 ru_inblock 增量 > 0，且阴性 ≈ 0 → inblock 可用，H-IO 下可细分 H2（页缓存未命中/设备读）与 H3（文件系统层），修订 §2、§5 后重新送审；
  - 阳性的 ru_inblock 增量 = 0 → inblock 不可用，H2/H3 合并为 H-IO，打分公式中彻底去掉 inblock（不是降级为参考）；
  - ru_majflt 有变化而 inblock 不变 → majflt 只对 mmap 型页入有意义，而观测到的栈顶是 read() 路径（get_data/read_text），所以仍不可用于打分，按合并处理。
- 本文 §2–§7 按「合并为 H-IO」的分支书写。

## 2. 预登记假设
缺口 G = 墙钟 − 进程 CPU（定义见 §4）。
- **H1 CPU 调度饥饿**：未被读解释的缺口里，nivcsw 速率高于配对对照 p95 的区间占主导。
- **H-IO I/O 或文件系统等待**：读耗时占缺口的主导（在 Darwin 上不细分页缓存与文件系统层；只在描述层按时间对齐 host-io-latency 和 host-starve 做分型说明，不打分）。
- **H0 以上都不是**（K5）：H0 没有独立分数，只通过 residual 报告。结局只能是「未定/混合」，不会是「已定」；H0 不能被确认，只能说 H1 与 H-IO 都解释不了。

## 3. 探针（BOOTSTRAP 内，只用被测解释器的 stdlib，不 import 新树模块，全部 best effort）
- **P1 逐文件读计时**：包装 `importlib._bootstrap_external.FileLoader.get_data` 和 `importlib.metadata.PathDistribution.read_text`。每次调用记录开始/结束的 monotonic 时间、调用内的线程 CPU 增量（time.thread_time）、路径类别（old/.venv、old/src、stdlib、其他）和字节数。计数与总耗时单独存放；最慢的 50 条只作附加清单。
  - 覆盖不到的范围（写明）：探针安装前的 stdlib import；不经过 get_data/read_text 的读取（例如 open() 直读、C 扩展加载 .so、sqlite）。
- **P2 资源采样线程**：每 0.5s 取一次 getrusage(RUSAGE_SELF)，记录 utime、stime、minflt、majflt、inblock、nvcsw、nivcsw，以及 monotonic 和 UTC 时间。第一条额外记录 os.getloadavg()（1、5、15 分钟），作为 §6 配对的唯一 loadavg 来源（K3a）。**逐条 write + flush + os.fsync**。从探针安装开始，采到就绪或被杀为止。
- **P3 阶段标记**：子进程在 probe.jsonl 里写入探针安装、import 前四个模块完成、进入 cli.main 三个标记。就绪标记只有一种实现（K3b）：父进程在 readiness 判定成立的同一处，把 time.monotonic() 写入 daemon-N-ready-mark.json。macOS 的 time.monotonic 基于系统级时钟，可以跨进程比较；§9 有一条测试断言这一点。
- 输出：daemon-N-probe.jsonl；suite 汇总进 readiness.json 的观察字段 `probe`，不参与 complete 判定。

## 4. G 的定义（B4）
- 起点：P3「探针安装」（在第一个 hyprial import 之前）。终点：daemon-N-ready-mark.json 里的 monotonic 值，即 readiness 判定成立的那一点。超时的样本以 SIGKILL 前 P2 的最后一条为终点，并报出它与超时时刻的差值。
- CPU：RUSAGE_SELF 的 utime+stime，包含采样线程自身（<1%，§6 实测）。daemon 在就绪前不产生需要计入的子进程：用 RUSAGE_CHILDREN 记录，不计入 CPU，只报告。
- 下限：G < 3s 或 G < 10%×elapsed 的样本记为「CPU 主导」，原始数据照列，不参与 §5 的打分。
- 慢样本 = old 树 daemon，G ≥ 15s，或 readiness 超时（并集）。

## 5. 互斥划分与打分（B2）
- 「nivcsw 速率」的定义（v3.2）：区间内 Δnivcsw / Δ(utime+stime)，即每 CPU 秒的非自愿切换次数。分母取下限 0.001s，即 Δnivcsw / max(Δ(utime+stime), 0.001)。不排除低 CPU 区间，因为极度饥饿时线程恰恰拿不到 CPU：阻塞睡眠的线程 Δnivcsw≈0，速率≈0，不算争用；拿不到 CPU 的可运行线程 Δnivcsw>0，速率很高，算争用。
  - 依据（本机实测 8 轮，每轮 3 秒忙循环，15 核，45 个竞争进程）：每墙钟秒的倍数（有竞争/无竞争）最小 0.82、中位 1.86，会倒挂；每 CPU 秒的倍数最小 2.55、中位 5.93。原因：macOS 上忙线程即使无竞争也有约 110 次/秒的时间片到期切换，饥饿时线程拿不到 CPU，每墙钟秒的切换数反而不会明显上升。
  - 原始输出：scratchpad/card6/tdd/nivcsw-metric.txt。
每个慢样本以 G 为单位做划分：
- 「读耗时」的定义（v3.3）：每次读的等待部分 = 读墙钟 − 读内线程 CPU（P1 记录的 time.thread_time 增量），下限 0；与 G = 墙钟 − CPU 同口径（读内 CPU 已计入 G 的 CPU，不能再算作等待）。仍在进行中的读（inflight）没有 CPU 记录，按墙钟计。
  - 依据（run 12477 / task 42671，head 6c7ef7e7，old 树四次启动）：读墙钟 / G = 0.79、0.99、1.12、1.00，读内 CPU 0.91–1.16s；按等待部分计 = 0.65、0.75、0.86、0.87。若用墙钟，1.12 的样本按本节会被判「口径异常」不计分，而被排除的正是读最多的样本。
- 读与区间的对齐：每次读的等待部分按其墙钟跨度等比例切分到它跨过的 0.5s 区间。
- 区间标记：某个区间的 nivcsw 速率高于配对对照 p95，记为「争用区间」。
- 读的标记（K4）：落在争用区间里的那部分读耗时记为 contended，其余记为 quiet。
- R = min(1, Σ全部读耗时 / G)；R_quiet = min(1, Σquiet 读耗时 / G)。任一原值 > 1 时原样报出，标记「探针或口径异常」，该样本不计分。
- 主判定（用 R_quiet）：s_io = R_quiet；W = 1 − R_quiet；s1 = W ×（不含 quiet 读的区间中，争用区间所占的比例）。
- 一致性判定（用 R）：s_io' = R；W' = 1 − R；s1' = W' ×（不含任何读的区间中，争用区间所占的比例）。
- 两套划分都报出。用两套得到的主导假设必须相同，否则该样本记为「混合」。
- residual = 1 − s1 − s_io，必须显式报出（≥ 0）。两套划分都要满足不变式 s1 + s_io ≤ 1，由测试断言。
- 主导：某项 ≥ 0.6，且比另一项高 0.2 以上；否则记为「混合」。

## 6. 对照（B3）
- 配对：old 树、G < 3s 的启动，**且必须是探针落地之后的运行**（nivcsw 只有落地后才有）。起始 loadavg 统一取 P2 第一条的 os.getloadavg()[0]，慢样本和对照用同一来源（K3a），对照的值落在慢样本 ±25% 以内，时间在慢样本前后 7 天内。
- 区间口径（W2）：nivcsw 速率的 p95 用对照集的**全部**采样区间计算；下面「≥ 20 个非读区间」指对照集中**没有任何读落入**的区间。
- 最小样本（K2）：≥ 5 个快启动，并且累计 ≥ 20 个非读区间，**两者都取自配对对照集**，不取自慢样本本身，才能算 nivcsw 速率的 p95。
- 配不上或样本不足：该慢样本整体不计分，只列原始数据。因为 v3 的 s_io 也要靠 p95 区分 contended 和 quiet，所以一起不计分。可计分样本不足 3 个时报「未定」。

## 7. 判定（B6）
- 计入门槛的样本：同一 job、同一棵树最多 1 条，取时间最早的一条（不在结果变量上挑选）；按 taskId 去重。其余样本列出，但不作为独立证据。
- 机制已定：≥ 3 个**可计分**且计入门槛的慢样本主导假设相同，没有 1 个与之相反的主导，并且这 ≥ 3 条**不全部落在同一个 6 小时窗口**。如果全部集中在同一窗口，只报「本批观察结果」，不写「已定」。
- 否证：某假设在 ≥ 3 个可计分慢样本中得分 < 0.2 时判为否证。「已定」和「否证」可以同时报出。
- 未定：到期时可计分慢样本 < 3，或者主导不一致 → 照实列出，不改规则、不延期；若要另开批，必须重新预登记。
- 所有阈值（15s、3s、10%、0.6、0.2、±25%、5 个/20 个、6h、p95）冻结后都不改。

## 8. 观察期与可行性（B7）
- 探针落地后 7 天，或满 5 个计入门槛的慢样本，先到为准。
- 预期产出量口径（写死）：
  - 统计源：带 readiness.json 的 hosted job（#984 落地后，即 09-29 01:10 之后）；
  - 时间窗：09-29 至送审日，另加 7 天内更早且带 readiness 的数据（如果有）；
  - 逐日计数：hosted-upgrade 中 old 树 daemon 的启动总数、elapsed ≥ 15s 的次数、status=timeout 的次数；
  - 附 elapsed 与 importCoreWall−Cpu 的分布，以及数据出处（路径和 sha256）；
  - 数据由 cli-developer 导出。
  - 注：readiness 行里没有进程级 CPU，所以这里用 elapsed 做代理，只用于预估产出量，不用于判定。
- 结果（card6-prep/B7-yield.json，sha256 4338e105…；计算脚本 b7.py 3d289826…；task 映射 hosted-runs-export.json 24cea08b…）：
  - #984 落地后（task createdAt ≥ 09-29 01:10 +08:00）：25 个 job，96 次 old 树启动；09-29：elapsed ≥ 15s 共 19 次，timeout 2 次（task 42250、42185 daemon-5）。
  - 分布：elapsed p50 3.68 / p90 30.34 / max 45.16s；importCoreWall−Cpu p50 0.41 / p90 22.34 / max 36.53s。
  - 19 次慢启动中 daemon-1 占 14 次；按 UTC 小时计，09-28T17 到 09-29T03，其中 03 时有 10 次（时间集中，§7 的 6h 规则会起作用）。
  - 判读：预期产出量远超 3 个，可行性不是问题（评审另算：19 条中 importWall−Cpu ≥ 15s 的有 13 条，而 G 只会更大）。主要风险是时间集中：19 条里有 10 条在 UTC 03 时，按 §7 的 6h 规则，本卡很可能只能给出「本批观察结果」。这一点已写进 §0，并告知 amyws。
  - 引用范围（W3）：§8 只引用已验证的 B7-yield.json（sha256 4338e105ba2078ab1d483d6cd3cb4359259e2ab9c9291b1d2ae07c1ac1e85f4a，评审已独立重算）。b7.py、hosted-runs-export.py、hosted-runs-export.json 目前未能取回核对，不作为本判据的依据；如果以后核对通过，只记入卡日志。

## 9. 自证（TDD 先红后绿，B5 + minor）
1. 钩子生效：在本进程真实 import 一批模块，断言 P1 的 get_data 钩子被调用 ≥ N 次（3.12 上实测约 30 次）；read_text 钩子在 entry_points 路径上被调用 ≥ 1 次。
2. 人为慢读（测试用 loader 里 sleep）：s_io 占主导，不变式成立。
3. CPU 竞争（忙等子进程）：每 CPU 秒的 nivcsw 上升，断言有竞争/无竞争 ≥ 2 倍（本机实测最小 2.55）；本机复现不了就注明「本机不可证」。
4. SIGKILL：采样中途用 SIGKILL 杀掉进程，probe.jsonl 可以逐行解析，最后一条时间戳与被杀时刻相差 ≤ 1s。
5. 探针异常（让 getrusage、写文件、patch 分别抛异常）时，daemon 照常启动，场景结果不变。
6. 开销：本机 10 次开/关探针的完整分布（不只报中位数），中位差 ≤ 0.3s；同一场景开/关探针时的 PASS/FAIL 判定不变。
7. 划分函数：R > 1、G 接近 0、没有对照、全是读区间等边界情况各一个反例。
8. K4 反例：构造「饥饿期间读」的样本（读耗时大，但全部落在争用区间里）。主判定不能判为 H-IO；R 与 R_quiet 的结论不一致时判为「混合」。
9. 跨进程 monotonic：子进程写 time.monotonic()，父进程紧接着读自己的 monotonic，两者差值在 [0, 0.5s] 内。

## 10. 描述层
- 与 hq b753be74 提到的 test_home_guard、test_e2e008 DAEMON_START_TIMEOUT（inbox-store）以及 docker stats 采样按 UTC 时间对齐，只报告是否重叠，不据此归因。

## 11. 流程
一张卡一个 PR，走 TDD、对抗评审、CI、落地验收证据、具名批准，由 e2e-verifier 落地。落地后按 §7、§8 收样本，出机制报告，交评审复核。
