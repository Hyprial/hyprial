#!/usr/bin/env bash
# ⚠️ EXIT-CODE CONTRACT (2026-09-19): this script exits 0=READY · 1=CANNOT CERTIFY · 2=WAIT · 3=a declared
#    external dependency is missing (JOBLOG / DOCKER / FORGEJO_CONTAINER / VERIFY_OUTDIR — named on stderr).
#    For rc 1, "couldn't read" is NOT a separate exit code here — it is one of the NAMED conditions inside the
#    verdict (`⑤任务表可读=no` etc.), which is *better for a human reader* (it says WHICH read failed) and
#    *weaker for a scripted caller* (a wrapper cannot tell "unreadable" from "a real no"). ⇒ If you wrap this
#    script, map those named conditions to your own exit codes. (The other instruments carry it as `exit 3`.)
# devcheck.sh <PR#> — READ-ONLY readiness for a PR landing on dev.
# Environment (all optional unless said): VERIFY_REPO_DIR · DOCKER · FORGEJO_CONTAINER · JOBLOG · VERIFY_OUTDIR
# (VERIFY_OUTDIR is REQUIRED for the payload render — ⑤: the output directory is given by the caller, never /tmp).
# Encodes today's rules: the tree is the criterion · the certificate is per-tree ·
# read the EFFECTIVE permission (the join), not the containers · measure the artifact that ACTS ·
# every reading carries its moment. The merge itself is fired by hand.
set -uo pipefail
REPO="${2:-HyprialOS/harness-bridge}"
BASE="${3:-dev}"
# ④: container + docker binary are host assumptions ⇒ env-overridable, and their absence gets its OWN exit (3).
FORGEJO_CONTAINER="${FORGEJO_CONTAINER:-hyprial-forgejo-forgejo-1}"
DOCKER="${DOCKER:-docker}"
# ③: the job-log reader lives outside this repo ⇒ the caller names it; missing ⇒ loud, never an empty log.
JOBLOG="${JOBLOG:-}"
source "$(dirname "$0")/vlib.sh"   # ① 共用守卫,同目录相对引入(见该文件头的寿命声明)
# on-disk repo path is <lowercased-owner>/<name>.git
RORG=$(printf '%s' "${REPO%%/*}" | tr 'A-Z' 'a-z'); RNAME="${REPO##*/}"
EFF="/data/git/repositories/${RORG}/${RNAME}.git/hooks/pre-receive.d/00-committer-guard"
MASTER=/usr/local/share/forgejo-committer-guard/pre-receive
N="${1:?usage: devcheck.sh <PR#> [owner/repo] [base]}"
# ── instrument guard (2026-09-18: this script silently degraded when run from /tmp;
#    2026-09-19 fix ②: the candidate table hardcoded one machine's paths ⇒ replaced by need_repo_root):
# a broken instrument must FAIL LOUDLY -- an empty reading is NOT a negative reading.
ROOT=$(need_repo_root "devcheck") || exit 2
cd "$ROOT" || { echo "⛔ INSTRUMENT FAILURE: cannot cd to $ROOT" >&2; exit 2; }
if ! git remote get-url origin 2>/dev/null | grep -q "${REPO##*/}"; then
  echo "⛔ INSTRUMENT FAILURE: $ROOT has no origin remote matching '${REPO##*/}' (repo=$REPO)."
  echo "   Everything below would print EMPTY readings, which are NOT negatives."
  echo "   ⇒ point VERIFY_REPO_DIR at a checkout of $REPO; do not read a verdict from this run.  exit 2"
  exit 2
fi
# Declared dependencies, checked BEFORE any reading: anything missing here would otherwise surface as
# an empty value downstream, and empty is a legal reading in this instrument ⇒ it must fail loud.
for _cmd in git jq python3 tea; do
  command -v "$_cmd" >/dev/null 2>&1 || { echo "⛔ DEPENDENCY MISSING: '$_cmd' not on PATH" >&2; exit 3; }
done
need_exec "$DOCKER" DOCKER "set DOCKER=<path to the docker CLI>" || exit 3
if ! "$DOCKER" inspect "$FORGEJO_CONTAINER" >/dev/null 2>&1; then
  echo "⛔ DEPENDENCY MISSING: container '$FORGEJO_CONTAINER' not visible via '$DOCKER'." >&2
  echo "   ⇒ override with FORGEJO_CONTAINER=<name>; without it §6 reads EMPTY md5/conf, and empty here is a" >&2
  echo "     legal reading ('master==effective' would compare two empties and print ✓).  exit 3" >&2
  exit 3
fi
echo "== 0 moment: $(date -u +%Y-%m-%dT%H:%M:%SZ)   $REPO PR #$N  base=$BASE   cwd=$PWD"
P=$(tea_json "repos/$REPO/pulls/$N")
echo "$P" | jq -r '"   state=\(.state) merged=\(.merged) base=\(.base.ref) head=\(.head.sha)"'
HEAD=$(echo "$P" | jq -r '.head.sha'); PBRANCH=$(echo "$P" | jq -r '.base.ref')
# ⚠️ 合并提交的 subject 是【将来读 git log 的人唯一免费拿到的那行字】(坐标都在正文里,而正文要多一次 `git show`)⇒
# 用 PR 的真实标题。2026-09-18 的实测:#655 的合并 subject 又是默认的「PR #655 -> dev」,
# 而"改用真实标题"这条改进【当时代】批准了、我记下了、而【没有进到这个会执行的地方】⇒ 现在写进构造:
PTITLE=$(echo "$P" | jq -r '.title // ""')
[ -n "$PTITLE" ] || { echo "   ⛔ PR TITLE UNREADABLE ⇒ the merge subject would silently fall back to the terse default"; TITLE_UNREAD=1; }
[ "$PBRANCH" = "$BASE" ] || { echo "   ⛔ PR base=$PBRANCH but I was told base=$BASE"; exit 0; }
echo "== 1 refs:"; git fetch -q origin "refs/heads/$BASE:refs/remotes/origin/$BASE" 2>/dev/null
git fetch -q origin "+refs/pull/$N/head:refs/remotes/pr/$N" 2>/dev/null || echo "   ⚠️ could not fetch the PR head ref ⇒ tree/ancestry readings may be UNVERIFIED for this head"
DEV=$(git rev-parse "refs/remotes/origin/$BASE"); TAGDEV=$(git ls-remote origin "refs/tags/$BASE" 2>/dev/null | awk '{print $1}')
echo "   heads/$BASE = $DEV"; echo "   tags/$BASE  = ${TAGDEV:-<no such tag>}"; [ -z "$TAGDEV" ] && echo "   (no $BASE tag in this repo)" || { [ "$DEV" = "$TAGDEV" ] && echo "   ✓ equal" || echo "   ⚠️ tag lag ≠ 0"; }
echo "== 2 approvals on $BASE:"; BP=$(tea_json "repos/$REPO/branch_protections")
printf '%s' "$BP" | jq -r --arg b "$BASE" '.[]|select(.branch_name==$b)|"   approvals=\(.required_approvals) whitelist=\(.merge_whitelist_teams) statuscheck=\(.enable_status_check) outdated=\(.block_on_outdated_branch)"'
# ⚠️ THREE cases must not look alike (added 2026-09-18 after reviewing a repo with legitimately ZERO rules):
#   (i) the query FAILED                     -> unreadable  -> the gate criterion is UNVERIFIED (⛔ a green)
#   (ii) the query returned [] (no rules)    -> the gate column has NO SUBJECT -> record it as "empty", ⛔ "green"
#  (iii) a rule exists but lists no contexts -> unreadable  -> UNVERIFIED
if ! printf '%s' "$BP" | jq -e '.' >/dev/null 2>&1; then
  echo "   ⛔ BRANCH-PROTECTION QUERY UNREADABLE ⇒ 'no required gates' and 'I could not read them' must not look alike"
  REQ_UNREAD=1; REQ_NOSUBJ=0
elif [ "$(printf '%s' "$BP" | jq -r 'if type=="array" then length else -1 end' 2>/dev/null)" = "0" ]; then
  echo "   ⚠️ branch_protections = EMPTY ARRAY ⇒ this repo has NO rules ⇒ the required-gate column has NO SUBJECT (record 'empty', ⛔ 'green')"
  REQ=""; REQ_UNREAD=0; REQ_NOSUBJ=1
else
  REQ=$(printf '%s' "$BP" | jq -r --arg b "$BASE" '.[]|select(.branch_name==$b)|.status_check_contexts[]?' 2>/dev/null)
  if [ -z "$REQ" ]; then
    echo "   ⛔ a rule exists for $BASE but lists no contexts readable ⇒ UNVERIFIED (⛔ 'no gates required')"
    REQ_UNREAD=1; REQ_NOSUBJ=0
  else REQ_UNREAD=0; REQ_NOSUBJ=0; fi
fi
echo "== 3 effective permission (the JOIN, not a container listing):"
tea_json "repos/$REPO/collaborators/e2e-verifier/permission" | jq -r '"   e2e-verifier → \(.permission) (role=\(.role_name))"' 2>/dev/null || echo "   (no collaborator entry / not readable)"
# ⚠️ The verdict for each context comes from `/status` `state` (per-CONTEXT, latest-wins) — the same thing the
#    gate reads. The per-RUN task list below is used ONLY to explain a non-success, and it can lie by tie-break
#    (`created_at` has second granularity: on 2026-09-18 a `success` task was overwritten by an older `pending`).
#    ⇒ authority = `state`; task status = explanation of why. (Asked for by hq-adjutant; rationale also in vlib.sh.)
echo "== 4 contexts on the head sha:"; STAT=$(tea_json "repos/$REPO/commits/$HEAD/status")
printf '%s' "$STAT" | jq -r '"   combined=\(.state)  total=\(.total_count)" , (.statuses[]?|"     \(.status)\t\(.context)")' | sort | sed 's/^/  /' | head -20
# required list, by LIST not by colour: every required context must be PRESENT on this head
MISSING=""
if [ -n "$REQ" ]; then
  while IFS= read -r pat; do [ -z "$pat" ] && continue
    base_pat="${pat%\*}"
    printf '%s' "$STAT" | jq -r '.statuses[]?.context' | grep -q "^${base_pat}" || MISSING="$MISSING$pat; "
  done <<< "$REQ"
fi
if [ -n "$MISSING" ]; then echo "   ⛔ REQUIRED gates ABSENT on this head: $MISSING (the list, not the colour)";
else [ -n "$REQ" ] && echo "   ✓ all required contexts present on this head: $(printf '%s' "$REQ" | tr '\n' ' ') "; fi
echo "== 4b decidable-vs-unreadable red (a red that carries no summary is NOT evidence):"
TASKS_RAW=$(tea_json "repos/$REPO/actions/tasks?limit=1000")
TASKS=$(printf '%s' "$TASKS_RAW" | jq -r --arg s "$HEAD" '.workflow_runs[]?|select(.head_sha==$s)|"\(.id)\t\(.name)\t\(.status)"' 2>/dev/null)
# ⚠️ A TRUNCATED/GARBLED response used to fall through to "✓ no non-success task" -- an absence produced by
# a failed instrument read, i.e. the same shape as tonight's two self-explosions. Make it loud.
if need_readable "$TASKS_RAW" "TASK LIST"; then TASKS_UNREAD=0; else
  echo "      ⇒ 'no non-success task' 与 'I could not read the task list' 必须⛔ 同名 ⇒ verdict held"
  TASKS_UNREAD=1
fi
# ⚠️ 一个 sha 上可以有【旧的失败任务】与【后来重跑成功的同一上下文】并存 ⇒ 旧红【⛔ 是证据】(它被取代了)。
# 这就是"对象的漂移"在任务层的形态:statuses 是 per-context(最新者胜),tasks 是 per-run。
CLS=$(printf '%s\n' "$TASKS" | python3 -c '
import sys
rows=[l.rstrip("\n").split("\t") for l in sys.stdin if l.strip()]
maxsuc={}
for r in rows:
    if len(r)>=3 and r[2]=="success": maxsuc[r[1]]=max(maxsuc.get(r[1],0), int(r[0]))
sup=[]; live=[]
for r in rows:
    if len(r)>=3 and r[2] not in ("success",""):
        (sup if int(r[0])<maxsuc.get(r[1],0) else live).append(r)
#
for r in sup: print("SUP\t%s\t%s\t%s"%(r[0],r[1],r[2]))
for r in live: print("LIVE\t%s\t%s\t%s"%(r[0],r[1],r[2]))
')
SUPC=$(printf '%s\n' "$CLS" | grep -c '^SUP' || true)
[ "${SUPC:-0}" != "0" ] && { echo "   ⓘ $SUPC 条旧的失败任务被【后来同上下文的重跑成功】取代 ⇒ ⛔ 计为证据:"; printf '%s\n' "$CLS" | awk -F'\t' '$1=="SUP"{print "      ~ task "$2" ("$3", "$4") superseded ⇒ not evidence"}'; }
BAD=$(printf '%s\n' "$CLS" | awk -F'\t' '$1=="LIVE"{print $2"\t"$3"\t"$4}')
if [ -n "$BAD" ]; then
  # ③: the job-log reader is a DECLARED dependency — a missing one must not degrade into empty logs
  #    (an empty log classifies as UNREADABLE, i.e. 'missing tool' would be read as a task state).
  need_exec "$JOBLOG" JOBLOG "set JOBLOG=<path to forgejo-job-log.sh>" || exit 3
fi
if [ -z "$BAD" ]; then
  echo "   ✓ no non-success task on this sha"; RED_R=0; RED_U=0
else
  echo "   (non-success tasks on this sha: $(printf '%s\n' "$BAD" | grep -c .) -- all listed, none hidden; WINDOW = latest 1000 tasks repo-wide, so a very old task can fall out of view)"
  RED_R=0; RED_U=0
  printf '%s\n' "$BAD" | while IFS=$'\t' read -r tid tname tstat; do
    [ -z "$tid" ] && continue
    L=$("$JOBLOG" "$tid" 2>/dev/null)
    CLS=$(printf '%s' "$L" | classify_task "$tstat"); LB=${CLS%%$'\t'*}; DT=${CLS#*$'\t'}
    case "$LB" in
      SKIPPED)          echo "   ⓘ task $tid ($tname, $tstat) -> SKIPPED-BY-DESIGN: $DT"; echo "      => ⛔ 计为失败,而⛔ 计为不可判:它是条件本身把它排除的" ;;
      READABLE_SUMMARY) echo "   OK task $tid ($tname, $tstat) -> READABLE failure (test summary): $DT" ;;
      READABLE_DECISIVE)echo "   OK task $tid ($tname, $tstat) -> READABLE failure (decisive line): $DT"; echo "      => 它【有】决定性失败行 ⇒ 关于这棵树有信息(而⛔ 不可判)" ;;
      NEVER_STARTED)    echo "   !! task $tid ($tname, $tstat) -> UNREADABLE (never started: runner-side transport): $DT"; echo "      => the job died BEFORE its input existed (SPATIAL: which runner you land on, ⛔ a time window)" ;;
      IN_FLIGHT)        echo "   ⏳ task $tid ($tname, $tstat) -> IN-FLIGHT: 还没有判决(⛔ 是红、也⛔ 是不可判)"; echo "      => 'pending' ⛔ pass/fail 轴上的一个值,而是那个值【尚未存在】⇒ 等它,⛔ 重掷" ;;
      *)                echo "   !! task $tid ($tname, $tstat) -> UNREADABLE (interrupted): no summary, no fatal, no decisive line; ends [...$DT]"; echo "      => this red is evidence for NEITHER side" ;;
    esac
  done
fi
# ⚠️ RECONCILE THE TWO COORDINATE SYSTEMS (added 2026-09-18): statuses are per-CONTEXT (latest wins),
# tasks are per-RUN. My tasks view has a WINDOW, so it can miss a red the server still reports.
# Two copies of "what is red" existed and the narrower one could speak for the wider one. Now it must declare the shortfall.
SRVFAIL=$(printf '%s' "$STAT" | jq -r '.statuses[]?|select(.status=="failure" or .status=="error")|.context' 2>/dev/null | sort)
SRVN=$(printf '%s' "$SRVFAIL" | grep -c . || true)
MYN=$(printf '%s\n' "$BAD" 2>/dev/null | grep -c . || true)
echo "== 4c reconciliation (server per-context  <->  my per-run window):"
echo "   server says: state=$(printf '%s' "$STAT" | jq -r .state) total=$(printf '%s' "$STAT" | jq -r .total_count) failures=$SRVN"
if [ "${SRVN:-0}" != 0 ]; then
  printf '%s\n' "$SRVFAIL" | while IFS= read -r c; do [ -z "$c" ] && continue
    nm="${c%% *}"; if printf '%s\n' "${TASKS:-}" | awk -F'\t' -v n="$nm" '$2==n{found=1} END{exit !found}'; then
      echo "     [in my window] failure: $c"
    else echo "     [OUTSIDE MY TASK WINDOW] failure: $c  <= the server reports this red; my tasks view cannot see its task"; fi
  done
fi
# ⚠️ compare only FAILURE-class tasks: a running task is legitimately absent from the server's failure list, so
# lumping "non-success" here generated a false alarm (it fired on a head whose 3 non-success tasks were merely IN-FLIGHT).
MYFAIL=$(printf '%s\n' "$BAD" 2>/dev/null | awk -F'\t' '$3=="failure"||$3=="error"{c++} END{print c+0}')
[ "${SRVN:-0}" = 0 ] && [ "${MYFAIL:-0}" != 0 ] && echo "   ⚠️ I see $MYFAIL failure-class task(s) the server does NOT list as a failure ⇒ stale/superseded tasks"
echo "== 5 tree check (the anti-silent-rewrite criterion, not mere equality):"
MB=$(git merge-base "$DEV" "$HEAD" 2>/dev/null)
MT=$(git merge-tree --write-tree "$DEV" "$HEAD" 2>/dev/null | head -1)
echo "   merge-base(dev,head) = ${MB:0:12}   merge result = ${MT:0:12}   head tree = $(git rev-parse "$HEAD^{tree}" 2>/dev/null | cut -c1-12)"
if [ -z "$MB" ]; then
  echo "   ⚠️ the local clone has no objects for $BASE ($REPO) ⇒ the tree criterion must run where that repo is checked out"
  echo "      (this is an instrument limit, NOT a conflict) ⇒ fall back to: post-merge merge^1 check + the guard's log line"
  MT=""; HT="x"
elif [ -z "$MT" ]; then
  echo "   ✗ merge-tree produced nothing (conflict or bad input)"; MT=""; HT="x"
else
  HEADF=$(git diff --name-only "$MB" "$HEAD" | sort)
  MERGF=$(git diff --name-only "$DEV" "$MT" | sort)
  EXTRA=$(comm -23 <(printf '%s\n' "$MERGF") <(printf '%s\n' "$HEADF"))
  echo "   files head changed (mb→head): $(printf '%s\n' "$HEADF" | grep -c . )   files the merge changes (dev→merge): $(printf '%s\n' "$MERGF" | grep -c .)"
  # up-to-datedness (block_on_outdated_branch=true — but the gate reads a CACHED `commits_behind`, so it MAY refuse a stale head; never predict it)
  # ⚠️ 口径 matters: `rev-list --count H..D` counts SIDE-BRANCH commits merged into D too, so it overstates
  #    "the base moved". The question "is this head up to date?" is answered by: is D's tip an ancestor of H?
  BEHIND=$(git rev-list --count --first-parent "$HEAD..$DEV" 2>/dev/null)
  AHEAD=$(git rev-list --count --first-parent "$DEV..$HEAD" 2>/dev/null)
  BEHIND_ALL=$(git rev-list --count "$HEAD..$DEV" 2>/dev/null)
  # ⚠️ 2026-09-18: `merge-base --is-ancestor` returns non-zero BOTH for "not an ancestor" AND for "object missing".
  #    A missing object therefore reads as a negative -- a false negative on a gate. Assert presence FIRST.
  if ! git cat-file -e "$HEAD" 2>/dev/null || ! git cat-file -e "$DEV" 2>/dev/null; then
    echo "   ⛔ ANCESTRY UNREADABLE: an object is missing locally ($(git cat-file -e "$HEAD" 2>/dev/null && echo "head ok" || echo "head MISSING") / $(git cat-file -e "$DEV" 2>/dev/null && echo "dev ok" || echo "dev MISSING"))"
    echo "      ⇒ 这⛔ 是『⛔ 是祖先』,而是『无法判定』—— 先 fetch,再看结论"
    UPTODATE=unreadable
  elif git merge-base --is-ancestor "$DEV" "$HEAD" 2>/dev/null; then UPTODATE=yes; else UPTODATE=no; fi
  [ "${UPTODATE}" = "unreadable" ] || echo "      (objects present ✓ ⇒ 这次的祖先判定可信)"
  # ⚠️ 口径 must be in the label (2026-09-18): the same word "behind" carries two quantities.
  # Which one is correct depends on the intended action: a merge crosses the first-parent steps; a rebase faces them all.
  echo "   up-to-date with $BASE: $UPTODATE   (behind(first-parent)=$BEHIND / ahead(first-parent)=$AHEAD | behind(all)=$BEHIND_ALL)"
  # The gate's input may be a CACHED `commits_behind` (API-invisible) -- so the criterion is a COMPARISON:
  #   computed = git rev-list --count <head>..dev   vs   the DB column.
  #   They disagree ⇒ the gate reads a stale value ⇒ it is BLIND for this PR right now.
  # I can only print MY half; the comparison must happen where the column is readable (infra-ops).
  echo "      computed-for-comparison: git rev-list --count <head>..refs/heads/$BASE = $BEHIND_ALL"
  # ⭐ 判据要【执行】,而⛔ 只是印出来让人自己对:把 DB 那个【闸真正读的】缓存字段取来,并给出比较的【判决】。
  #    (2026-09-18: five open PRs were ALL stale, so the field is not "wrong sometimes" -- it is a DIFFERENT object.)
  #    读⛔ 到必须是它自己的一个词(⛔ 与"一致"共用一个结果)。
  # ⚠️ 三个失败各有自己的词(2026-09-18 实测:我第一次写这段时用了裸 `docker`,而本机 docker 只在
  #    /usr/local/bin ⇒ 于是它报 "FIELD UNREADABLE" —— 而【那个词是对的、原因是我的 PATH 假设】⇒ 可见分名救命)。
  #    2026-09-19: DOCKER 的可执行性与 forgejo 容器的在位已在开局查过(缺 ⇒ exit 3);这里只剩 postgres 发现。
  FIELD=""; WHY=""
  ALL=$("$DOCKER" ps --format '{{.Names}}' 2>/dev/null | grep -i postgres || true)
  # ⚠️ ⛔ 用 `grep -m1` 静默挑第一个:本机有多套 postgres(实测挑到了别的项目的 `hyprial-sub2api-postgres`
  #    ⇒ 查询当然返回空,而"空"与"没有那一行"同形)。判据:候选必须【唯一】,否则点名报歧义。
  # ⚠️ post-freeze delta #1(2026-09-19,hq-adjutant 裁定;快照字节保持原样,仓内副本唯此一行偏差):
  #    postgres 容器的【身份】必须是两个条件的合取 forgejo ∧ postgres(实测命中 hyprial-forgejo-postgres-1):
  #    · 单用 forgejo ⇒ 会挑到应用容器(其中没有 psql ⇒ 响亮报错)
  #    · 单用 postgres ⇒ 会挑到别的项目的库(查询返回空 ⇒ 【静默】:"空"被读成"没有那一行")
  PGC=$(printf '%s\n' "$ALL" | grep -i forgejo | grep -i postgres | head -1)
  if [ -z "$PGC" ]; then
    NC=$(printf '%s\n' "$ALL" | grep -c . || true)
    if [ "$NC" = "1" ]; then PGC=$(printf '%s\n' "$ALL" | grep .)
    else WHY="POSTGRES CONTAINER AMBIGUOUS OR ABSENT (candidates: $(printf '%s' "$ALL" | tr '\n' ' '))"; fi
  fi
  if [ -z "$PGC" ] && [ -z "$WHY" ]; then WHY="POSTGRES CONTAINER NOT FOUND"
  else
    PGU=$("$DOCKER" exec "$PGC" printenv POSTGRES_USER 2>/dev/null | tr -d '\r'); PGD=$("$DOCKER" exec "$PGC" printenv POSTGRES_DB 2>/dev/null | tr -d '\r')
    FIELD=$("$DOCKER" exec "$PGC" psql -U "${PGU:-gitea}" -d "${PGD:-gitea}" -tAc "select commits_behind from pull_request where id=$N" 2>/dev/null | tr -d '\r ')
    [ -n "$FIELD" ] || WHY="QUERY RETURNED NOTHING (container=$PGC; table pull_request id=$N)"
  fi
  if [ -z "$FIELD" ]; then
    echo "      ⛔ GATE-INPUT-FIELD UNREADABLE — $WHY ⇒ 无法判断那道闸此刻对这张 PR 是瞎的还是热的(⛔ 读成\"一致\")"
  elif [ "$FIELD" = "$BEHIND_ALL" ]; then
    echo "      ✓ gate input MATCHES the computed value (field=$FIELD, computed=$BEHIND_ALL) ⇒ 闸的输入此刻是新鲜的就这张而言;⛔ 预测它会⛔ 会拒"
  else
    echo "      ⛔ GATE IS BLIND FOR THIS PR RIGHT NOW: field=$FIELD vs computed=$BEHIND_ALL ⇒ 它读的是另一个对象(⛔ 依赖它来判\"⛔ 会被拦\")"
  fi
  if [ "$UPTODATE" = "no" ]; then
    echo "   ⚠️ OUTDATED: $BASE's tip ($(printf %.8s "$DEV")) is NOT in this head (merge-base=$(printf %.8s "${MB:-?}"))"
    echo "      ⇒ ⚠️ do NOT predict a refusal: the gate reads the CACHED pull_request.commits_behind and may be blind"
    echo "        (measured 2026-09-18: #649 was really behind 1, the field said 0, and it MERGED). Compare the field (DB read)"
    echo "        with this reading; and if it does land, the greens must be re-read on the new head (they bind to their base)."
  fi
  if [ -n "$EXTRA" ]; then
    echo "   ⛔ the merge touches files the head never touched ⇒ it would REWRITE the base backwards:"; printf '%s\n' "$EXTRA" | sed 's/^/      /'
  else
    echo "   ✓ the merge changes nothing beyond what the head changed (no silent rewrite)"
  fi
  HT=$MT
fi
echo "== 6 guard (the artifact that ACTS) + config:"; M=$("$DOCKER" exec "$FORGEJO_CONTAINER" md5sum "$MASTER" 2>/dev/null | awk '{print $1}'); E=$("$DOCKER" exec "$FORGEJO_CONTAINER" md5sum "$EFF" 2>/dev/null | awk '{print $1}')
FPMC=$("$DOCKER" exec "$FORGEJO_CONTAINER" sh -c 'sed -n "s/^FIRST_PARENT_MODE=//p" /data/gitea/committer-guard/guard.conf' 2>/dev/null | tr -d '\r')
echo "   master=$M"; echo "   effective=$E  (runs for every push)"; [ "$M" = "$E" ] && echo "   ✓ master==effective" || echo "   ⚠️ DIVERGED (a copy replaced the symlink, or a repo was missed)"
echo "   FIRST_PARENT_MODE=${FPMC:-<unset ⇒ script default>}"
echo "== 6b guard STATE (can a rejection right now tell you anything about your diff?):"
BUGGY=0; [ "$E" = "bfb3c37286f288bf56ba0f78c20155af" ] && BUGGY=1
ENVFP=$("$DOCKER" exec "$FORGEJO_CONTAINER" printenv COMMITTER_GUARD_FIRST_PARENT_MODE 2>/dev/null | tr -d '\r')
echo "   env  COMMITTER_GUARD_FIRST_PARENT_MODE = ${ENVFP:-<unset>}   (the render source)"
echo "   conf FIRST_PARENT_MODE                 = ${FPMC:-<unset>}   (the rendered artifact)"
if [ "$BUGGY" = 0 ]; then
  # The script being fixed is one thing; whether the check is in FORCE is another (the flag can still say warn).
  if [ "${FPMC}" = "warn" ] || [ "${ENVFP}" = "warn" ]; then
    GSTATE="FIXED-BUT-WARN"
    echo "   ⚠️ guard = FIXED-BUT-WARN: the script is fixed, but the first-parent check is still in WARN"
    echo "      ⇒ it will NOT block ⇒ this landing's shape rests on MY reads (tree criterion + post-merge merge^1), not on the guard"
    echo "      ⇒ the flag is removed only after infra-ops's pre-enforce list — #15 is already landed (6c52ffb);"
    echo "        what still blocks it: the two identities absent from the roster, and whether the hook inherits env at all"
  else
    GSTATE="FIXED"; echo "   ✓ guard = FIXED and in force ⇒ normal landing path"
  fi
elif [ "${FPMC}" = "warn" ]; then
  GSTATE="BUGGY-WARN"; echo "   ⚠️ guard = BUGGY-WARN (mitigation visible in guard.conf) ⇒ landings pass, real defect still live"
elif [ "${ENVFP}" = "warn" ]; then
  GSTATE="MITIGATION-CLAIMED-UNVERIFIED"
  echo "   ⚠️ guard = $GSTATE"
  echo "      the mitigation is set in the RENDER SOURCE (container env) but is absent from guard.conf ⇒"
  echo "      it works only if Forgejo passes the container env to the hook process — UNVERIFIED"
  echo "      ⇒ an attempt is now an EXPERIMENT, not an error:"
  echo "         hook log line mode=warn  ⇒ mitigation reached the hook (attempt may LAND)"
  echo "         hook log line mode=enforce ⇒ env did not reach it (rejection, identical shape to 09:40Z)"
  echo "      ⚠️ success is not benign: a successful attempt LANDS the PR"
else
  GSTATE="BUGGY-ENFORCE"
  echo "   ⛔ guard = $GSTATE (script buggy, no mitigation anywhere) ⇒ a rejection would NOT be about your diff"
fi
echo "   forge StartedAt = $("$DOCKER" inspect -f '{{.State.StartedAt}}' "$FORGEJO_CONTAINER" 2>/dev/null)   (a restart re-renders guard.conf)"
echo "== VERDICT:"; ok=1
[ "$GSTATE" = "BUGGY-ENFORCE" ] && { echo "   ⛔ guard is BUGGY-ENFORCE ⇒ do not attempt a dev landing (a rejection would prove nothing)"; ok=0; }
[ "$GSTATE" = "MITIGATION-CLAIMED-UNVERIFIED" ] && echo "   ⚠️ an attempt is an EXPERIMENT: it is informative either way, and if it succeeds it LANDS the PR"
[ "$(echo "$P"|jq -r .state)" = open ] || { echo "   ⛔ PR is not open"; ok=0; }
if [ "${UPTODATE:-}" = "unreadable" ]; then echo "   ⛔ the ancestry reading is UNVERIFIED (missing object) ⇒ treat up-to-datedness as unknown"; ok=0
elif [ -n "${UPTODATE:-}" ]; then
  [ "$UPTODATE" = "yes" ] || { echo "   ⛔ OUTDATED head (merge-base=${MB:0:8} ≠ dev tip) ⇒ ⚠️ do NOT predict a refusal: the gate reads the CACHED pull_request.commits_behind (measured 2026-09-18: #649 was really behind 1, field said 0, and it merged). Compare the field (DB read) with this reading; and the greens must be re-read on a refreshed head."; ok=0; }
else
  echo "   ⚠️ up-to-datedness unknown (instrument) ⇒ treat as UNVERIFIED"; ok=0
fi
if [ -n "${MISSING:-}" ]; then echo "   ⛔ a REQUIRED context is absent on this head ($MISSING) ⇒ not a green, whatever the colour"; ok=0; fi
[ "${TASKS_UNREAD:-0}" = "1" ] && { echo "   ⛔ the task list was unreadable ⇒ the red/Green evidence is UNVERIFIED for this run ⇒ not a green"; ok=0; }
[ "${REQ_UNREAD:-0}" = "1" ] && { echo "   ⛔ the required-context list itself was unreadable ⇒ the gate criterion is UNVERIFIED ⇒ not a green"; ok=0; }
[ "${REQ_NOSUBJ:-0}" = "1" ] && echo "   ⓘ no subject for the gate column here (repo has zero rules) ⇒ I report 'empty', and the shape rests on my own reads" 
if [ -n "${BAD:-}" ]; then
  # a non-success task on this sha: readable ⇒ content failure (fatal); unreadable ⇒ not decidable (fatal too, but a RERUN is the remedy)
  UNREAD=0; READABLE=0; INFLIGHT=0
  while IFS=$'\t' read -r tid tname tstat; do [ -z "$tid" ] && continue
    L=$("$JOBLOG" "$tid" 2>/dev/null)
    LB=$(printf '%s' "$L" | classify_task "$tstat"); LB=${LB%%$'\t'*}
    case "$LB" in
      SKIPPED) : ;;                                   # by design ⇒ neither a red nor undecidable
      READABLE_SUMMARY|READABLE_DECISIVE) READABLE=$((READABLE+1)) ;;
      IN_FLIGHT) INFLIGHT=$((INFLIGHT+1)) ;;           # no verdict yet ⇒ neither a red nor undecidable
      *) UNREAD=$((UNREAD+1)) ;;
    esac
  done <<< "$BAD"
  [ "$READABLE" = 0 ] || { echo "   ⛔ $READABLE READABLE red task(s) on this sha ⇒ a content failure ⇒ do not land"; ok=0; }
  [ "$UNREAD" = 0 ] || { echo "   ⛔ $UNREAD UNREADABLE red task(s) ⇒ NOT decidable (neither a pass nor a content failure) ⇒ rerun, do not land"; ok=0; }
  [ "$INFLIGHT" = 0 ] || { echo "   ⏳ $INFLIGHT task(s) STILL IN-FLIGHT ⇒ this head has no verdict yet ⇒ WAIT (⛔ rerun, ⛔ land)"; ok=0; inflight_only=1; }
fi
[ "$DEV" = "$TAGDEV" ] || echo "   ⚠️ tag lag ≠ 0 ⇒ find the CAUSE, not just the fact: the tag moves only when tests.yml's unit job greens on the new head, so a transport failure can pin it. Clients that track tags/$BASE are then behind by $(git rev-list --count "$TAGDEV..$DEV" 2>/dev/null) commit(s)."
[ -n "$MT" ] || ok=0
[ "$M" = "$E" ] || ok=0
# ⭐ 2026-09-18:判决【只由共用 helper 产生】—— 此前这里有一份本地的第二份(第二份正是今晚两次假判的根因)。
#   而它顺带补上一个真缺陷:旧版只打印"⛔ NOT READY",而【理由是散在十几处 ok=0 里的】⇒
#   判决【⛔ 是它各打印部件的合取】。现在每条条件有名字、有值、印在同一行,于是判决可被它自己的输出反驳 ✓
yn(){ [ "$1" = "$2" ] && echo yes || echo no; }
verdict_conj "PR #$N" \
  "①守卫非BUGGY-ENFORCE=$(yn "$GSTATE" BUGGY-ENFORCE | sed 's/^yes$/no/;s/^no$/yes/')" \
  "②PR-open=$(yn "$(echo "$P"|jq -r .state)" open)" \
  "③祖先可读且up-to-date=$([ "${UPTODATE:-}" = yes ] && echo yes || echo no)" \
  "④必需上下文在位=$([ -n "${MISSING:-}" ] && echo no || echo yes)" \
  "⑤任务表可读=$([ "${TASKS_UNREAD:-0}" = 1 ] && echo no || echo yes)" \
  "⑥必需清单可读=$([ "${REQ_UNREAD:-0}" = 1 ] && echo no || echo yes)" \
  "⑦可读红-0=$([ "${READABLE:-0}" = 0 ] && echo yes || echo no)" \
  "⑧不可读红-0=$([ "${UNREAD:-0}" = 0 ] && echo yes || echo no)" \
  "⑨无在飞=$([ "${INFLIGHT:-0}" = 0 ] && echo yes || echo wait)" \
  "⑩树可算=$([ -n "$MT" ] && echo yes || echo no)" \
  "⑪无多余路径=$([ "$M" = "$E" ] && echo yes || echo no)"
VRC=$?
case "$VRC" in
  0) VERDICTWORD="READY"; echo "   ✅ READY (fire by hand with head_commit_id=$HEAD)" ;;
  2) VERDICTWORD="WAIT" ;;
  *) VERDICTWORD="not READY" ;;
esac
# ⛔ F1(今晚在另一台仪器上犯过):打印了 CANNOT CERTIFY 而脚本返回 0 ⇒ 判据与退出码走散了。
#    判决的 rc 必须【就是脚本的 rc】—— 否则任何 `|| exit` 的调用方都会把"⛔ 认证"读成"通过"。
DECIDED=$VRC

# == 7 payload (only when READY): rendered with THIS moment's coordinates ==================
# Rules encoded: coordinates are re-taken at the moment of use (never reused); the message carries
# the two premise columns (verified / UNVERIFIED), so the landing records what it relies on.
# ⑤: the output directory is given BY THE CALLER (VERIFY_OUTDIR) — never a hardcoded /tmp path.
if [ "$ok" = 1 ]; then
  OUTDIR="${VERIFY_OUTDIR:-}"
  if [ -z "$OUTDIR" ] || [ ! -d "$OUTDIR" ]; then
    echo "⛔ DEPENDENCY MISSING: VERIFY_OUTDIR — payload output directory not given or not a directory ('${OUTDIR:-<unset>}')." >&2
    echo "   ⇒ render with VERIFY_OUTDIR=<dir> (premerge.sh reads the same variable ⇒ same directory).  exit 3" >&2
    exit 3
  fi
  PAY="$OUTDIR/merge_p${N}.json"; CO="$OUTDIR/merge_p${N}.coords"
  TASKLINE=$(tea_json "repos/$REPO/commits/$HEAD/status" | jq -r '"combined=\(.state) total=\(.total_count)"')
  python3 - "$N" "$HEAD" "$DEV" "$TAGDEV" "$TASKLINE" "$E" "$FPMC" "$ENVFP" "$GSTATE" "$PTITLE" "$PBRANCH" > "$PAY" <<'PYEOF'
import json,sys
n,head,dev,tagdev,taskline,emd5,fpmc,envfp,gstate,title,base = sys.argv[1:12]
# subject = the PR's own title (the one line a future `git log` reader gets for free)
subj = title if title else f"PR #{n} -> {base}  [!! TITLE UNREADABLE — terse default, not the PR title]"
msg = f"""{subj}

合并时坐标（本次重渲染，非复用）:
- 被合并对象(head): {head}
- 闸: {taskline}
- 引用: refs/heads/dev = {dev} · refs/tags/dev = {tagdev}（相等: {dev==tagdev}）
- 守卫: effective md5 = {emd5} · env FIRST_PARENT_MODE = {envfp} · conf FIRST_PARENT_MODE = {fpmc} · state = {gstate}

形状由谁背书 / 这次落地的【验过的前提】与【未验的前提】:
- 验过: 树判据（合并只改动 head 改动过的文件 ⇒ 无静默回改）· head 未移动 · 闸跑在本 sha 上 · 我持有的有效权限读数为 write
- 未验: """ + ({'MITIGATION-CLAIMED-UNVERIFIED':
   "守卫的 mitigation 是否到达钩子子进程 —— 本次落地正是它的检验:钩子日志 mode=warn ⇒ 到达; mode=enforce ⇒ 未到达（此时形状不由守卫背书, 而由上面的树判据与合并后的 merge^1 核对背书）",
   'BUGGY-WARN': "缺陷本身仍在（脚本 md5 未变）⇒ 形状不由守卫背书",
   'BUGGY-ENFORCE': "不适用（此状态不建议落地）",
   'FIXED': "无"}).get(gstate, "见上")
msg += f"""

合并后必须核: merge^1 == {dev}（pre-merge dev head）—— 守卫处于 warn 时,这是唯一还在工作的形状检查。
"""
print(json.dumps({"Do":"merge","head_commit_id":head,
  "MergeTitleField":(title if title else f"PR #{n} -> {base}  [!! TITLE UNREADABLE — terse default, not the PR title]"),
  "MergeMessageField":msg},ensure_ascii=False))
PYEOF
  echo "== 7 payload re-rendered with THIS moment's coordinates -> $PAY ($(wc -c < "$PAY") bytes)"
  # ⚠️ 2026-09-19: the payload carries head_commit_id but NOT the dev head it was rendered against ⇒ so the
  #    pre-merge re-read could not tell "dev moved since render". Write a sidecar with the render-time coordinates
  #    (the merge endpoint would reject unknown fields, so it must live OUTSIDE the payload).
  printf '%s\n%s\n' "$HEAD" "$DEV" > "$CO"
  echo "     (+ $CO: the render-time head/dev, for premerge.sh)"
else
  echo "== 7 payload NOT rendered (verdict was ${VERDICTWORD:-not READY}) -- a stale payload must never be reused"
fi
exit ${DECIDED:-1}
