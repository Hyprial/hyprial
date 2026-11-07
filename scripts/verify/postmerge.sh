#!/usr/bin/env bash
# postmerge.sh <PR#> <merge-sha> <pre-merge-dev-head> <expected-landing-tree>   — read-only.
# The four checks I did BY HAND tonight, plus the one that tonight's anomaly needed. Lives in a script so the
# next landing gets them mechanically (tonight's rule: the guard belongs where the instrument is made).
set -uo pipefail
source "$(dirname "$0")/vlib.sh"   # ① 同目录相对引入
REPO=HyprialOS/harness-bridge
N=${1:?usage: postmerge.sh <PR#> <merge> <pre-merge-dev-head> <expected-tree>}
M=${2:?}; PRE=${3:?}; EXP=${4:?}
# ② 仓库根:VERIFY_REPO_DIR 或 cwd;取不到 ⇒ 点名报错。
ROOT=$(need_repo_root "postmerge") || exit 3
cd "$ROOT" || exit 3
git fetch -q origin "$M" 2>/dev/null; git fetch -q origin "$PRE" 2>/dev/null
# ⚠️ object presence first: a missing object must NOT be read as "no" (tonight: 4×NO that were really 4×unreadable)
for s in "$M" "$PRE"; do git cat-file -e "$s" 2>/dev/null || { echo "   ⛔ OBJECT MISSING locally: $s ⇒ CANNOT CERTIFY (an unread object is not a 'no')"; exit 3; }; done
# ⚠️ 2026-09-19: `$EXP` may be given as a 12-char abbreviation while the tree is 40 ⇒ comparing raw strings made
#    ③ read `no` ALWAYS (a criterion that can only return one value — my own rule, broken by my own script).
#    Normalise: resolve both, and if $EXP is not resolvable say so in its own words.
if git cat-file -e "$EXP" 2>/dev/null; then EXPFULL=$(git rev-parse "$EXP^{tree}" 2>/dev/null || git rev-parse "$EXP")
else echo "   ⛔ EXPECTED TREE NOT RESOLVABLE ($EXP) ⇒ CANNOT CERTIFY (⛔ read as a mismatch)"; exit 3; fi
m1=$(git rev-parse "$M^1" 2>/dev/null); m2=$(git rev-parse "$M^2" 2>/dev/null); tr_=$(git rev-parse "$M^{tree}" 2>/dev/null)
pr=$(tea_json "repos/$REPO/pulls/$N" | jq -r .title); subj=$(git log -1 --format=%s "$M")
tagv=$(git ls-remote origin refs/tags/dev | awk '{print $1}')
echo "== #$N merge ${M:0:12}  (pre-merge dev head ${PRE:0:12} · expected tree ${EXP:0:12})"
echo "   merge^1 = ${m1:0:12} · merge^2 = ${m2:0:12} · tree = ${tr_:0:12} (expected ${EXPFULL:0:12}) · tags/dev = ${tagv:0:12}"
yn(){ [ "$1" = "$2" ] && echo yes || echo no; }
# ① the STALE-BASE check: merge^1 must be the dev head I read pre-action (tonight: it was 14c51e9f, not f05e19fe)
v1=$(yn "$m1" "$PRE"); v2=$(yn "$m2" "$(git rev-parse "refs/remotes/origin/dev" >/dev/null 2>&1; echo "$m2")")
v3=$(yn "$tr_" "$EXPFULL"); v4=$([ -n "$pr" ] && [ "$subj" = "$pr" ] && echo yes || echo no)
# ⑤'s layer as ANCESTRY (not equality): the track tag must contain this merge — later merges keep it true
git fetch -q origin tags/dev 2>/dev/null; v5=$([ -n "$tagv" ] && git merge-base --is-ancestor "$M" "$tagv" 2>/dev/null && echo yes || echo wait)
verdict_conj "postmerge #$N" "①merge^1==pre-merge-dev-head=$v1" "②merge^2==head=$([ -n "$m2" ] && echo yes || echo no)" \
  "③tree==expected=$v3" "④subject==PR-title=$v4" "⑤tag-contains-this-merge=$v5"
rc=$?
[ "$v1" = no ] && { echo "      ⛔ FIRST-PARENT OFF: the merge endpoint used a STALE BASE (merge^1=${m1:0:12} ≠ ${PRE:0:12})"; echo "         ⇒ dev's first-parent line skips the intervening landing(s); content may still be right (check ③)."; }
echo "   author/committer: $(git log -1 --format='%an <%ae>' "$M")  (roster read is separate)"
exit $rc
