#!/usr/bin/env bash
# onbranch.sh <job-name> [branch=dev] [window=1000]  — read-only.
# Q: has <job-name> ever run on <branch>'s commits, and with what verdict?
# Design rules (tonight's cells): visible search condition · both values possible ·
#   own positive control for the FILTER (not just "the window is non-empty") · object presence before --is-ancestor.
set -uo pipefail
source "$(dirname "$0")/vlib.sh"   # ① 同目录相对引入
R=HyprialOS/harness-bridge
JOB=${1:?usage: onbranch.sh <job-name> [branch] [window]}
BR=${2:-dev}
N=${3:-1000}
# ② 仓库根:VERIFY_REPO_DIR 或 cwd;取不到 ⇒ 点名报错。
ROOT=$(need_repo_root "onbranch") || exit 3
cd "$ROOT" || exit 3
git fetch -q origin "$BR" 2>/dev/null
B=$(git rev-parse "refs/remotes/origin/$BR" 2>/dev/null) || { echo "UNREADABLE: branch ref $BR" >&2; exit 3; }
# test seam: ONBRANCH_TASKS_FILE lets the empty-window branch be exercised without the network
if [ -n "${ONBRANCH_TASKS_FILE:-}" ]; then RAW=$(cat "$ONBRANCH_TASKS_FILE"); else
  RAW=$(tea_json "repos/$R/actions/tasks?limit=$N"); fi
need_readable "$RAW" TASKS >/dev/null || exit 3
TOT=$(printf '%s' "$RAW" | jq '.workflow_runs|length')
CTRL=$(printf '%s' "$RAW" | jq -r '[.workflow_runs[].name]|group_by(.)|max_by(length)|.[0]')   # busiest name = self-supplied positive control
CCNT=$(printf '%s' "$RAW" | jq --arg c "$CTRL" '[.workflow_runs[]|select(.name==$c)]|length')
echo "== $JOB on branch '$BR' (${B:0:12})   $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "   search condition : name==\"$JOB\"  [requested limit=$N ⇒ MEASURED window = $TOT task(s)]"
[ "$TOT" != "$N" ] && echo "   ⚠ LABEL/BEHAVIOUR: requested $N but the endpoint returned $TOT — the label above is the MEASUREMENT, not your parameter"

echo "   CONTROL (filter works): the same expression for the busiest name \"$CTRL\" matched $CCNT row(s)"
if [ "$TOT" = "0" ]; then echo "   CANNOT CERTIFY: empty window ⇒ any 'no occurrence' would be vacuous"; exit 3; fi
if [ "$CCNT" = "0" ]; then echo "   CANNOT CERTIFY: filter matched nothing even for a control name ⇒ filter/endpoint suspect"; exit 3; fi
M=$(printf '%s' "$RAW" | jq --arg j "$JOB" '[.workflow_runs[]|select(.name==$j)]|length')
if [ "$M" = "0" ]; then
  echo "   VERDICT: NO-OCCURRENCE — \"$JOB\" has no run in this window (control proves the filter is live)"; exit 1
fi
echo "   rows (newest first):"
printf '%s' "$RAW" | jq -r --arg j "$JOB" '.workflow_runs[]|select(.name==$j)|"\(.id)\t\(.status)\t\(.run_number)\t\(.head_sha)"' \
 | sort -rn | while IFS=$'\t' read -r id st rn sha; do
     git fetch -q origin "$sha" 2>/dev/null
     if git cat-file -e "$sha" 2>/dev/null; then
       if git merge-base --is-ancestor "$sha" "$B" 2>/dev/null; then loc=ON-$BR; else loc=off-$BR; fi
     else loc=OBJECT-MISSING; fi
     printf "   %-6s %-8s run %-5s %s  %s\n" "$id" "$st" "$rn" "${sha:0:10}" "$loc"
   done
ON=$(printf '%s' "$RAW" | jq -r --arg j "$JOB" '.workflow_runs[]|select(.name==$j)|.head_sha' | while read -r s; do git merge-base --is-ancestor "$s" "$B" 2>/dev/null && echo x; done | wc -l | tr -d ' ')
FAIL=$(printf '%s' "$RAW" | jq -r --arg j "$JOB" '.workflow_runs[]|select(.name==$j)|select(.status!="success")|.head_sha' | while read -r s; do git merge-base --is-ancestor "$s" "$B" 2>/dev/null && echo x; done | wc -l | tr -d ' ')
echo "   VERDICT: FOUND — $M run(s), $ON on-branch, of which $FAIL non-success.  [both values possible: 0 matches ⇒ NO-OCCURRENCE]"
