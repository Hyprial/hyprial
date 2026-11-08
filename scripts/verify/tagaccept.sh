#!/usr/bin/env bash
# tagaccept.sh [expected_dev_sha] — the acceptance for "layer ⑤ reached" (i.e. refs/tags/dev moved).
# ⚠️ EXIT-CODE CONTRACT (2026-09-19, reviewer feedback from e2e-verifier on PR #661):
#    0/1/2 = the verdict_conj of conditions ①–④ · 3 = a declared dependency missing
#    (JOBLOG / repo root / an unreadable task list). The trailing [note] guard-log line is
#    INFORMATIONAL — it is NOT one of the four conditions, so a missing docker/container there
#    prints a NAMED "guard-log line count UNREADABLE (DOCKER=… FORGEJO_CONTAINER=…)" and does NOT
#    change the rc. ⇒ A scripted caller must read that line's NAME; rc=0 alone never means
#    "the note had a reading". (Same rule as devcheck.sh's header: unreadable gets its own word.)
# Why this is a script and not a memory: the criterion has THREE parts, and the cheapest wrong
# reading is "the tag moved" alone — which lets a green be read as "it is not red".
#   ① the tag moved (authoritative live read, not a local fetch)
#   ② it moved to the sha we tested
#   ③ the green that moved it carries its OWN test summary line (⛔ merely "no failure")
set -uo pipefail
# ④: docker/容器名是宿主观设 ⇒ env 可覆盖;缺失时有自己的出口(见文件尾 note 行)。
FORGEJO_CONTAINER="${FORGEJO_CONTAINER:-hyprial-forgejo-forgejo-1}"
DOCKER="${DOCKER:-docker}"
# ③: job-log 读取器住在仓外 ⇒ 调用方给;缺失 ⇒ 响(exit 3),⛔ 静默成空日志。
JOBLOG="${JOBLOG:-}"
source "$(dirname "$0")/vlib.sh"   # ① 共用守卫:①不可读有自己的出口 ②判决=合取(见文件头的寿命声明)
EXP="${1:-c8fd049624cda96f0d1efcbfc16d41b71fbbbdfa}"
# ② 仓库根:调用方给(VERIFY_REPO_DIR)或从 cwd 推;取不到 ⇒ 点名报错。
ROOT=$(need_repo_root "tagaccept") || exit 3
cd "$ROOT" || exit 3
need_exec "$JOBLOG" JOBLOG "set JOBLOG=<path to forgejo-job-log.sh>" || exit 3
echo "== tagaccept @ $(date -u +%Y-%m-%dT%H:%M:%SZ)   expected dev sha = ${EXP:0:12}"
HD=$(git ls-remote origin refs/heads/dev | awk '{print $1}')
TG=$(git ls-remote origin refs/tags/dev  | awk '{print $1}')
C1=no; C2=no; C3=no; C4=no
echo "  [①] live: heads/dev = ${HD:0:12}   tags/dev = ${TG:0:12}"
if [ "$TG" = "$HD" ]; then C1=yes; echo "      ✓ [①]=yes the tag reached the branch head"; else echo "      ✗ [①]=no: tag is $(git rev-list --count "$TG..$HD" 2>/dev/null) commit(s) behind (all-count)"; fi
echo "  [②] does the tag point at the sha we tested?"
if [ "$TG" = "$EXP" ]; then C2=yes; echo "      ✓ [②]=yes it points at the sha we tested (${EXP:0:12})"; else echo "      ✗ [②]=no — tag=${TG:0:12} expected=${EXP:0:12}"; fi
echo "  [③] does the GREEN carry its own summary line?  (the mover is tests.yml's unit job on a push to dev)"
RAW=$(tea_json "repos/HyprialOS/harness-bridge/actions/tasks?limit=1000")
# ⚠️ The response is ~2.3 MB and jq TRUNCATES it. Learned the hard way: this script (written *after* the
# rule was named) reproduced the very defect -- an unread instrument rendered as "no summary line",
# i.e. a check that did not run looking like a check that failed. The verdict below must therefore
# require a READABLE task list, and say so loudly when it isn't.
if ! need_readable "$RAW" "TASK LIST"; then
  echo "         ⇒ [#3] is UNVERIFIED, ⛔ failed: 'I could not read it' 与 'it has no summary' 必须⛔ 同名"
  exit 3
fi
T=$(printf '%s' "$RAW" | jq -r --arg s "$HD" '[.workflow_runs[]?|select(.head_sha==$s and .name=="unit")]|sort_by(.id)|last|"\(.id)\t\(.status)\t\(.run_number)"')
TID=$(printf '%s' "$T" | cut -f1); TST=$(printf '%s' "$T" | cut -f2)
echo "      unit task on that sha: id=$TID status=$TST"
LOG=$("$JOBLOG" "$TID" 2>/dev/null)
if printf '%s' "$LOG" | grep -qE '[0-9]+ (passed|failed)'; then
  # ⚠️ A summary line in the LOG ⛔  the job is finished: an in-flight job can already show "5118 passed"
  #    while its status is still `running`. So [③] is "the green carries its own summary",
  #    and the VERDICT below is the CONJUNCTION of all four -- never [③] alone.
  if [ "$TST" = "success" ]; then C3=yes; echo "      ✓ [③]=yes summary line present AND status=success: $(printf '%s' "$LOG" | grep -oE '[0-9]+ passed[^\n]*' | tail -1)"
  else echo "      ⏳ [③]=pending: the log already shows this: $(printf '%s' "$LOG" | grep -oE '[0-9]+ passed[^\n]*' | tail -1)  —  but status=$TST (an in-flight job can show a summary before it finishes)"; fi
else
  LAST=$(printf '%s' "$LOG" | tail -1 | cut -c1-70)
  FATAL=$(printf '%s' "$LOG" | grep -m1 -E "fatal: unable to access|Couldn't connect to server" | cut -c1-70)
  echo "      ⛔ NO summary line ⇒ this green is NOT readable as a green.  last=[...$LAST]  fatal=[$FATAL]"
  echo "      ✗ [③]=no: NO summary line ⇒ this green is NOT readable as a green"; C3=no
fi
# [④] hq-adjutant's condition (c): was it THIS push run that moved the tag, or did a new push do it?
# A new push to dev would show up as a task with a head_sha different from the one we tested.
# ⚠️ First version counted EVERY task with a different head_sha (= the whole history, 8628) -- a criterion that
# ALWAYS fires is as useless as one that never fires. The question is "since my BEFORE snapshot": an id threshold.
BEFORE_ID="${BEFORE_ID:-17979}"
NEWER=$(printf '%s' "$RAW" | jq -r --argjson b "$BEFORE_ID" '[.workflow_runs[]?|select((.id|tonumber) > $b)]|length' 2>/dev/null)
NEWSHA=$(printf '%s' "$RAW" | jq -r --argjson b "$BEFORE_ID" --arg s "$HD" '[.workflow_runs[]?|select((.id|tonumber)>$b and .head_sha!=$s)]|length' 2>/dev/null)
echo "  [④] same push run?  (threshold: tasks with id > $BEFORE_ID, i.e. since the BEFORE snapshot)"
echo "      unit task: task_id=$(printf '%s' "$T" | cut -f1) run_INDEX=$(printf '%s' "$T" | cut -f3) status=$(printf '%s' "$T" | cut -f2)"
echo "      ⚠️ label note: 5460 is the per-repo run INDEX (UI URLs use it); the global run id is a different number (9230). Two numbering schemes, one name."
echo "      tasks created since then: ${NEWER:-?}   · of those, on a DIFFERENT head_sha: ${NEWSHA:-?}"
if [ "${NEWSHA:-0}" = "0" ]; then C4=yes; echo "      ✓ [④]=yes no task on a different head_sha ⇒ no new push ⇒ the mover was a RE-RUN of run 5460"; else echo "      ✗ [④]=no: $NEWSHA task(s) on other shas since then ⇒ check whether a NEW PUSH moved the tag"; fi
echo "      (both values are possible on this check: 0 ⇒ re-run; >0 ⇒ a new push exists to rule out)"
# ---- THE VERDICT IS THE CONJUNCTION (added 2026-09-18 after a near-miss: a summary line in the log of a
#      still-running job made [③] true and the old verdict printed "⑤ reached" while the tag had NOT moved)
C3V=$C3; [ "$C3" = pending ] && C3V=pending
verdict_conj "layer⑤" "①=$C1" "②=$C2" "③=$C3V" "④=$C4"; VRC=$?
GUARDLINES=$("$DOCKER" exec "$FORGEJO_CONTAINER" sh -c 'wc -l < /data/gitea/log/committer-guard.log' 2>/dev/null)
if [ -n "$GUARDLINES" ]; then
  echo "  [note] guard log lines = $GUARDLINES  (a *push* would add nothing here unless it violated; ⛔ a mover indicator)"
else
  echo "  ⛔ [note] guard-log line count UNREADABLE (DOCKER='${DOCKER}' FORGEJO_CONTAINER='${FORGEJO_CONTAINER}') ⇒ 这一项【没有读数】,⛔ 当作 0"
fi
exit $VRC
