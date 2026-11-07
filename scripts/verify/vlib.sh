#!/usr/bin/env bash
# vlib.sh — 我所有【验收类】脚本共用的两个模式(2026-09-18 夜,hq-adjutant 的"总纲"的落地尝试)
#
# 为什么有它(而⛔ 只是"我这次记得加"):
#   今晚实测过一条:**规则写在摘要里,两小时后就传不到下一台仪器**(第四级)。
#   判据(hq-adjutant 给):「它是这台仪器【被造出来那一刻】就有的吗?」
#   ⇒ 一条【事后补上】的守卫,保护的是【已经犯过的那一次】,而⛔ 是下一台仪器。
#   ⇒ 所以正确的落地形态 = 【守卫长在共用的地方,新脚本天生就有】—— 而不是我记得加。
#
# 两个模式(各自都有今晚的实测代价背书):
#   A. `need_readable`  —— "读⛔ 到"必须有【自己的出口】,⛔ 与"没问题"共用一个
#      · 假阴实例:actions/tasks 抓回来被截断 ⇒ 空 ⇒ 打印"没有汇总行"(而若标签真移了,它仍会说"别声称")
#      · 而它的下一步是"再查查",而"再查查"永远⛔ 会报错
#      · ⚠️ 对【间歇】故障:开火时把【可比对的量】(字节数)一起打出来,否则下次开火⛔ 知道是否同一件事
#   B. `verdict_conj`   —— 判决 = 各条件的【合取】,并逐条打印
#      · 假阳实例:任务仍 running,而日志里已有汇总行 ⇒ 只看一项的判决打印了「✓ 达成」,而那件事还没发生
#      · 抓它的是【输出内部不一致】(判决行与分项矛盾)⇒ 所以分项要打印,而⛔ 只为给人看:
#        是为了让判决【可以被它自己的分项反驳】
#
# 寿命声明(与配方同一份)—— ⚠️ 2026-09-18 改写成【与位置无关】的形态,因为原来那句"本文件住 /tmp
#   ⇒ 寿命 = 这台机器的 /tmp 清理策略"**在搬进仓库之后会变成假话,而没有任何测试会读注释**:
#   · 若它住在一个临时目录 ⇒ **寿命 = 那个目录的清理策略**(重启 / 定期清理 / 部署)
#   · 若它住在一个仓库里   ⇒ **寿命 = 那个仓库的寿命(被动)** ⇒ 那时【本文件会被 source 到各处】,
#     于是它的**语义**成为所有未来仪器的起点 ⇒ 改它要动所有调用方
#   ⇒ 📌 一条真话若只在【旧坐标】上成立,那搬动它的那一刻就该改它 —— 而这一步现在不需要任何人记得 ✓

# ═══ PORTABILITY / MOVE RECORD (2026-09-19) ═══
# 这一组文件【已搬进】 harness-bridge 仓(2026-09-19T02:29:23Z 冻结入 `tools/verify/`,
# 2026-10-01 随仓库布局收拢迁 `scripts/verify/`;
# 逐文件 sha256 见快照 README)。搬入时闭合了六处宿主/暂存假设 —— 「静默」的定义(hq-adjutant):
# **它缺了之后,这台仪器的输出会变成【空】,而"空"在这里已经是一个合法读数**。
#   ① `source /tmp/vlib.sh` ⇒ **LOUD** · 已修:`source "$(dirname "$0")/vlib.sh"`(同目录)✓
#   ② `cd /Users/<user>/<work>/e2e-resident`(主机+用户名写死)⇒ **LOUD** · 已修:`need_repo_root`
#      (`VERIFY_REPO_DIR` 或 `git rev-parse --show-toplevel`,取不到就【点名报错】)✓
#   ③ 绝对路径的 `…/hq-forgejo-job-log/tools/forgejo-job-log.sh` ⇒ ⛔ **SILENT**(缺 ⇒ `L` 空 ⇒ 落进"空 = 一个结果")·
#      已修:`JOBLOG=${JOBLOG:-}` + 使用点 `need_exec "$JOBLOG" JOBLOG || exit 3` ✓
#   ④ `docker exec hyprial-forgejo-forgejo-1` ⇒ ⛔ **SILENT**(缺 ⇒ md5/conf 读为空 ⇒ "不可读"被读成状态)·
#      已修:`DOCKER` / `FORGEJO_CONTAINER` env 覆盖 + 缺失时自己的出口(exit 3 / 具名 UNREADABLE)✓
#        ⚠️ 2026-09-19 实测:用**裸 `docker`** 时本机找不到它(只在 `/usr/local/bin`)⇒
#          当时靠"分名"救了一次(`FIELD UNREADABLE` 而⛔ "一致")⇒ 见 devcheck.sh 的同一段 ✓
#        ⚠️ 而容器选择必须**按身份**(含 `forgejo`),**候选⛔ 唯一就点名报歧义** —— 否则会挑到别的项目的 postgres ✓
#   ⑤ 输出写死 `/tmp/merge_p${N}.json` ⇒ **LOUD**(写不进会响)· 已修:输出目录由调用方给(`VERIFY_OUTDIR`,必填)✓
#   ⑥ 本文件的"寿命声明" ⇒ ✅ **已闭合**:它已改写成【与位置无关】(临时目录 ⇒ 该目录的清理策略;仓库 ⇒ 仓库的寿命)✓
#   ⑦ ⭐ **共用那份本身是【语义快照】**:搬进去的时刻由【派单时间】决定,而⛔ 由【正确性】决定 ⇒
#      本文件此刻的语义 = 三值判决 · 最后 `=` 切分(值)· classify_task 五类 —— 搬入时【未改】,要改另开单 ✓
#   ⇒ ✅ 验收标准(⛔ "改好了"):**每一处静默依赖,给一条【把它拿掉之后跑一次】的读数** ——
#     证明它现在会报错,而⛔ 是返回空(读数见该 PR 正文与 tests/tooling/verification/test_verification_instruments.py)✓
# ═════════════════════════════════════════════════
# A. 校验一份"必须以可解析形式到手"的数据。
#    用法: need_readable "<原始内容>" "<名字>" <字节数阈值>  || return 3
#    语义: 不可解析 ⇒ 打印【自己的名字】+ 量 + 原因,并返回 3(⛔ 与"空"混同)
need_readable() {
  local raw="$1" label="${2:-payload}" bytes
  bytes=$(printf '%s' "$raw" | wc -c | tr -d ' ')
  if ! printf '%s' "$raw" | jq -e '.' >/dev/null 2>&1; then
    echo "   ⛔ $label UNREADABLE — BYTES=$bytes (记录它:间歇故障下次开火时靠它对照)"
    echo "      parse error: $(printf '%s' "$raw" | jq '.' 2>&1 | head -1 | cut -c1-70)"
    echo "      ⇒ 这⛔ 是『没有内容』,而是『我读⛔ 到』—— 两者⛔ 许同名"
    return 3
  fi
  return 0
}

# B. 判决 = 合取。用法(⛔ 用关联数组:bash 会把 [①] 当算术下标 ⇒ 会炸):
#      verdict_conj "层⑤" "①=yes" "②=no" "③=pending" "④=yes"
#    语义: 全 yes ⇒ ✓ 并返回 0;否则逐条打印未满足项并返回 1。
#    ⚠️ "pending"(还没发生)与 "no"(已确定为否)必须分开印。
#    ⚠️ 三值(2026-09-18 补):【还没到】与【已确定为否】必须分开印,而且必须有不同的【退出码】——
#    因为一个 `pending`(任务还在跑)曾被读成 "NOT READY ⇒ rerun, do not land":那既错读了对象,
#    又开出一个被禁止的处方。故: 全部 yes ⇒ 0 · 有未到而无确定为否 ⇒ **2 (WAIT)** · 否则 ⇒ 1。
verdict_conj() {
  local what="$1"; shift
  local pair k v line="" miss="" wait=""
  # ⚠️ 2026-09-19: split on the LAST `=` — the VALUE is always the final field. (Splitting on the first made
  #   `②merge^2==head=yes` parse as value "=head=yes" ⇒ a satisfied condition counted as a miss.
  #   I had *documented* that trap hours earlier and still walked into it ⇒ fixed in the constructor, not the caller.)
  for pair in "$@"; do line="$line [${pair%%=*}]=${pair##*=}"; done
  echo "   ⇒ VERDICT($what):$line"
  for pair in "$@"; do
    k="${pair%%=*}"; v="${pair##*=}"
    case "$v" in
      yes) ;;
      wait|pending|inflight|running|in_flight) wait="$wait $k";;
      *) miss="$miss $k";;
    esac
  done
  if [ -z "$miss" ] && [ -z "$wait" ]; then
    echo "      ✓ $what 达成 —— 每一条都读过(任何一条 no 都会是一次假声称)"
    return 0
  fi
  if [ -z "$miss" ]; then
    echo "      ⏳ WAIT $what — 还没有判决,而这些【尚未发生】:$wait"
    echo "         ⚠️ 这一句⛔ 是『NOT READY』:前者是关于【我自己的状态】,后者是关于【对象的结论】⇒ ⛔ 重掷,等它"
    return 2
  fi
  echo "      ⛔ CANNOT CERTIFY $what — 未满足:$miss${wait:+ · 且未到:$wait}"
  echo "         ⚠️ 把那句读成『没有内容』是错的:逐条看上面的值(pending ≠ no)"
  return 1
}

# C. `tea api` can prepend a NOTE line ("NOTE: no login matched this repository, falling back to ...") ⇒
#    a JSON parser then sees non-JSON and the failure looks exactly like "no data". Strip everything
#    before the first `{`. (Found 2026-09-18: the same class as the 2.2 MB truncation -- same family.)
tea_json() { tea api "$@" 2>/dev/null | sed -n '/^[\[{]/,$p'; }

# E. need_repo_root — fix ② 的共用半(2026-09-19 搬进仓库时新增;它【不改】上面任何既有函数的语义,
#    只是把六份脚本里各自的「cd 到写死的本机路径」收敛成一份)。
#    用法: ROOT=$(need_repo_root "<script-name>") || exit <rc>
#    语义: 优先调用方显式给的 VERIFY_REPO_DIR;否则 `git rev-parse --show-toplevel`;
#          两者都取不到 ⇒ 把【自己的名字 + 缺的那个变量】打到 stderr(⛔ stdout —— 调用点用 $() 捕获 stdout,
#          打去 stdout 会被吞掉),返回 3。空仓库根 ⇒ 之后每条 git 读数都是【空】,而空是合法读数 ⇒ 必须响。
need_repo_root() {
  local who="${1:-instrument}" root="${VERIFY_REPO_DIR:-}"
  if [ -z "$root" ]; then
    root=$(git rev-parse --show-toplevel 2>/dev/null)
  fi
  if [ -z "$root" ]; then
    echo "⛔ INSTRUMENT FAILURE($who): no repo root — set VERIFY_REPO_DIR=<checkout of the repo> or run from inside one." >&2
    echo "   缺了它,下面所有 git 读数都会变成【空】,而空在这台仪器里是一个合法读数 ⇒ 必须响,⛔ 许静默降级。" >&2
    return 3
  fi
  if ! git -C "$root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "⛔ INSTRUMENT FAILURE($who): VERIFY_REPO_DIR=$root is not inside a git work tree." >&2
    return 3
  fi
  printf '%s\n' "$root"
}

# F. need_exec — fix ③④ 的共用半(同为搬入时新增):一个「必须是可执行文件」的依赖,缺失时给【自己的名字】。
#    用法: need_exec "$JOBLOG" JOBLOG "set JOBLOG=<path to forgejo-job-log.sh>" || exit 3
#    语义: 未设置或不可执行 ⇒ stderr 点名 <NAME> + 补救变量,返回 3(⛔ 与"配置缺键""没有内容"共用任何词)。
need_exec() {
  local path="${1:-}" name="${2:-DEPENDENCY}" hint="${3:-}"
  if [ -z "$path" ] || [ ! -x "$path" ]; then
    echo "⛔ DEPENDENCY MISSING: $name — '${path:-<unset>}' is not an executable file." >&2
    [ -n "$hint" ] && echo "   ⇒ $hint" >&2
    echo "   缺了它,这台仪器对应的读数会变成【空】,而空在这里已经是一个合法读数 ⇒ 必须响,⛔ 许静默。" >&2
    return 3
  fi
  return 0
}

# D. classify_task — ONE classification for the task log, used by BOTH the display and the verdict.
#    Why: on 2026-09-18 I fixed two defects in the *display* copy of this logic (`skipped`, then JS stack
#    traces) while the *verdict* kept its own older copy => it still called a correct landing "not decidable"
#    and a real content red "undecidable, rerun". The defect was not the patterns: it was the second copy.
#    ⚠️ WHY THE VERDICT COMES FROM THE commit-status `state`, NEVER FROM THE task `status` (added 2026-09-19):
#      · **`state`(per-CONTEXT, latest-wins) is the authority for 绿/红** —— it is what the gate reads, and it is
#        what the server's combined verdict is made of.
#      · **the task `status` is only used to explain a non-success** (skipped-by-design · in-flight · failure).
#      · and the task read CAN LIE BY TIE-BREAK: two tasks can share a second, so a reader sorting by
#        `created_at` will let the OLDER one overwrite the NEWER one ⇒ a `success` was read as `pending`
#        (2026-09-18, #655, ids 27/28 on the same second). ⇒ **never let the task read name the verdict** ✓
#    stdin: the job log.  $1: the task's status.  stdout: "<LABEL>\t<detail>"
#      SKIPPED · READABLE_SUMMARY · READABLE_DECISIVE · NEVER_STARTED · UNREADABLE
classify_task() {
  local st="${1:-}" LOG; LOG=$(cat)
  local ski; ski=$(printf '%s' "$LOG" | grep -m1 "Skipping job .* due to")
  [ -n "$ski" ] && { printf 'SKIPPED\t%s\n' "$(printf '%s' "$ski" | cut -c1-100)"; return 0; }
  [ "$st" = "skipped" ] && { printf 'SKIPPED\t(status=skipped)\n'; return 0; }
  # ⚠️ 5th class (added 2026-09-18): a task still RUNNING has not returned a verdict at all.
  # 'pending' is not a value on the pass/fail axis -- it is the ABSENCE of one. Folding it into "not success"
  # is the same error as matching a pattern in an empty set: it produced "5 UNREADABLE red task(s) => rerun"
  # for a head whose five contexts were merely still in flight (a healthy mid-flight head read as a fault).
  case "$st" in running|pending|waiting|queued) { printf 'IN_FLIGHT\t(status=%s)\n' "$st"; return 0; };; esac
  local sum; sum=$(printf '%s' "$LOG" | grep -oE '[0-9]+ (passed|failed)[^\n]*' | tail -1)
  [ -n "$sum" ] && { printf 'READABLE_SUMMARY\t%s\n' "$sum"; return 0; }
  local fat; fat=$(printf '%s' "$LOG" | grep -m1 -E "fatal: unable to access|Couldn't connect to server|Failed to connect to" | sed 's/^[0-9T:.Z-]*//' | cut -c1-90)
  [ -n "$fat" ] && { printf 'NEVER_STARTED\t%s\n' "$fat"; return 0; }
  # any line that NAMES a failure cause -- pytest, shell, uv, or a JS/Node stack (all are decisive)
  local dec; dec=$(printf '%s' "$LOG" | grep -m1 -E "^[0-9T:.Z-]* *(error|ERROR|fatal|FAILED|Traceback|panic):|The lockfile at|needs to be updated|No such file|Permission denied|cannot open|AssertionError|\bError:|at file:///|node:internal/|^[0-9T:.Z-]* *at .*:[0-9]+:[0-9]+" | sed 's/^[0-9T:.Z-]*//' | cut -c1-100)
  [ -n "$dec" ] && { printf 'READABLE_DECISIVE\t%s\n' "$dec"; return 0; }
  printf 'UNREADABLE\t%s\n' "$(printf '%s' "$LOG" | tail -1 | cut -c1-70)"
}
