#!/usr/bin/env bash
# intggate.sh <PR#> <branch> [<branch>...]   — the FIFTH condition, made structural.
#
# Why it exists (2026-09-18, hq-adjutant + cli-developer): for an integration PR (intg/* -> dev) the usual
# three criteria ALL pass on a tree that is missing a branch's *latest* commit:
#   · local suite green        -> it ran on the incomplete tree
#   · markers present          -> they check the four originals' markers, not their current heads
#   · merge-tree == head tree  -> it asks "will it land as-is", not "is it complete"
# so the missing-type gap is invisible to all three. The question that is NOT answered is:
#   "is each folded branch's CURRENT head inside the integration head?"
# This script answers exactly that and nothing else.
#
# ⚠️ Two traps it closes by construction:
#   1) `git merge-base --is-ancestor` returns non-zero BOTH for "not an ancestor" AND for "object missing"
#      => assert object presence FIRST, or an unreadable input reads as a negative (a false negative on a
#      gate, which also looks like confirming "something is missing" => sends the queue into a wasted re-fold).
#   2) it must be run AT THE MOMENT OF ACTION (branches move), and it must assert the head is unmoved.
set -uo pipefail
source "$(dirname "$0")/vlib.sh"   # ① 同目录相对引入
N="${1:?usage: intggate.sh <PR#> <branch> [<branch>...]}"; shift
REPO="${INTG_REPO:-HyprialOS/harness-bridge}"
# ② 仓库根:VERIFY_REPO_DIR 或 cwd;取不到 ⇒ 点名报错。
ROOT=$(need_repo_root "intggate") || exit 3
cd "$ROOT" || exit 3
echo "== intggate @ $(date -u +%Y-%m-%dT%H:%M:%SZ)   PR #$N   branches: $*"
H=$(tea_json "repos/$REPO/pulls/$N" | jq -r .head.sha)
[ -n "$H" ] && [ "$H" != "null" ] || { echo "   ⛔ head unreadable ⇒ cannot certify"; exit 3; }
echo "   head = $H"
git fetch -q origin "$H" 2>/dev/null
if ! git cat-file -e "$H" 2>/dev/null; then
  echo "   ⛔ head object NOT present locally after fetch ⇒ 祖先判定【无法判定】(⛔ '⛔ 是祖先')"; exit 3
fi
echo "   (head object present ✓ ⇒ 下面的判定可信)"
# ⭐ SELF-COMPARISON positive control (hq-adjutant/cli-developer, 2026-09-18): `--is-ancestor` also fails when
#    the *command* cannot work here -- which is a DIFFERENT question from "are both objects present".
#    `<head> <head>` must return true; if it doesn't, the instrument itself is broken and every NO below is
#    meaningless. Cost: zero. Needs no external data. Cannot go stale.
if git merge-base --is-ancestor "$H" "$H" 2>/dev/null; then
  echo "   ✓ SELF-TEST: --is-ancestor <head> <head> = true ⇒ 这条命令此刻在这台机器上可用"
else
  echo "   ⛔ SELF-TEST FAILED: --is-ancestor <head> <head> is NOT true ⇒ 仪器本身不可用 ⇒ 下面的 NO 全部无意义"; exit 3
fi
FAIL=0
for B in "$@"; do
  # ⚠️ 2026-09-19: this takes BRANCH NAMES; I passed shas twice and got a "not contained" that was really
  #    "could not resolve your argument" (and my own `tail -5` cut off the line that said so). So: accept both,
  #    and PRINT which interpretation was used — "what I passed" must never be invisible.
  if printf '%s' "$B" | grep -qE '^[0-9a-f]{40}$'; then KIND=sha; git fetch -q origin "$B" 2>/dev/null
  else KIND=branch; git fetch -q origin "refs/heads/$B:refs/remotes/orig/$B" 2>/dev/null; fi
  S=$(printf '%s' "$B" | grep -qE '^[0-9a-f]{40}$' && printf '%s' "$B" || git rev-parse "refs/remotes/orig/$B" 2>/dev/null)
  if [ -z "$S" ] || ! git cat-file -e "$S" 2>/dev/null; then
    echo "   ⛔ $B: interpreted as $KIND ⇒ ref/object UNREADABLE ⇒ 无法判定(⛔ 当作缺失)"; FAIL=1; CONT_FAIL=1; continue
  fi
  echo "   ⓘ $B ⇒ interpreted as $KIND, resolved to ${S:0:10}"
  if git merge-base --is-ancestor "$S" "$H" 2>/dev/null; then
    echo "   ✓ $B @ ${S:0:10} IS inside the integration head"
  else
    echo "   ⛔ $B @ ${S:0:10} is NOT inside the integration head ⇒ 这一张的【当前】头⛔ 在里面(缺失式缺口)"
    echo "      ⇒ ⛔ 自己去补折(那会改变交付的那棵树,而它的标志物清单与树判据都会过期)⇒ 让作者重折并重出交付面"
    # ⚠️ 2026-09-19(搬进仓时发现):这一支原来只置 FAIL=1,而 FAIL 是死变量(exit 走的是 verdict_conj 的 rc,
    #    判决里的 containment 只看 CONT_FAIL)⇒ 【逐张包含性全 ⛔ 而判决照样 达成/rc=0】——这台闸的第五条件
    #    在结构上从未接通。修:未包含同样计入 CONT_FAIL,让判决=它各打印部件的合取。
    FAIL=1; CONT_FAIL=1
  fi
done
# CONTINUITY (its own assertion, not folded into containment): "all four are inside" can also hold for a
# REBUILT tree that happens to contain them. Set OLD_INTG=<previous intg head> to check continuity.
if [ -n "${OLD_INTG:-}" ]; then
  git fetch -q origin "$OLD_INTG" 2>/dev/null
  if git cat-file -e "$OLD_INTG" 2>/dev/null && git merge-base --is-ancestor "$OLD_INTG" "$H" 2>/dev/null; then
    CONT_OK=1
    echo "   ✓ CONTINUITY: previous intg head ${OLD_INTG:0:10} IS an ancestor ⇒ 这是叠上去的,而⛔ 重建"
  else
    echo "   ⛔ CONTINUITY: previous intg head ${OLD_INTG:0:10} is NOT an ancestor ⇒ 树可能被重建(⚠️ 包含性全 ✓ 时仍会发生)"; FAIL=1
  fi
else
  echo "   ⓘ CONTINUITY: OLD_INTG 未给 ⇒ 这一项【未验】(而它是与包含性【独立】的一问)"
fi
echo "   --- tree criterion (recomputed NOW, since the head may move):"
git fetch -q origin refs/heads/dev:refs/remotes/origin/dev 2>/dev/null
MT=$(git merge-tree --write-tree refs/remotes/origin/dev "$H" 2>/dev/null | head -1)
HT=$(git rev-parse "$H^{tree}" 2>/dev/null)
echo "      merge-tree(dev,head) = ${MT:0:12}   head tree = ${HT:0:12}   equal=$([ "$MT" = "$HT" ] && echo YES || echo NO)"
[ "$MT" = "$HT" ] || { echo "      ⛔ merge-tree(dev,head) ≠ head tree ⇒ 落地树【⛔ 是】这棵树(dev 已前移 ⇒ 这张需重折或等同步)"; FAIL=1; }
# ⚠️ 2026-09-18/19: the verdict used to collapse every component into ONE flag ⇒ it could not be refuted by its
#    own printed parts (my own rule). Now each component is named and valued separately.
CONT=$([ -n "${OLD_INTG:-}" ] && { [ "${CONT_OK:-0}" = 1 ] && echo yes || echo no; } || echo no)
verdict_conj "intg-gate" \
  "head-object-in-place=yes" "self-test=yes" \
  "containment=$([ "${CONT_FAIL:-0}" = 0 ] && echo yes || echo no)" \
  "continuity=$CONT$([ -n "${OLD_INTG:-}" ] || echo '(OLD_INTG 未给⇒未验)')" \
  "tree==head=$([ "${MT:-x}" = "${HT:-y}" ] && echo yes || echo no)"
rc=$?
[ -n "${OLD_INTG:-}" ] || echo "      ⓘ continuity 未验:设 OLD_INTG=<上一版 intg head> 才算过这一项(它与包含性【独立】)"
exit $rc
