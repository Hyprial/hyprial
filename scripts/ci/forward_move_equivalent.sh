#!/usr/bin/env bash
# forward_move_equivalent.sh -- is <new_head> a PURE forward-move of <old_head>?
#
# ONE question: the SAME reviewed content sitting on a NEWER base, so the review
# binding renews instead of being re-earned (hq `flow-gate-discipline` §6.6
# rule 0).  It is a CI-internal judgement aid -- run by hand or by an agent, no
# new CI job, not a required check, no runner/Forgejo change.
#
# Interface:
#   forward_move_equivalent.sh <old_merge_base> <old_head> <new_base> <new_head>
#   exit 0 = PURE, exit 1 = NOT PURE (fail closed; also for unresolvable refs),
#   exit 2 = usage error.  Every run prints a greppable evidence block naming
#   every ref and tree it compared.  The walk is TRANSITIVE: it follows the merge
#   graph from <new_head> back to <old_head> hop by hop, so a multi-hop advance
#   needs no extra arguments and a single-hop move is just a chain of length 1.
#
# The three layers, in order.  Layer 1 is the hoped-for fast path; layer 2 is
# DECISIVE; layer 3 is a pre-filter and must NEVER decide.
#   1. fast path -- the two own-diffs are byte-identical:
#        git diff <old_merge_base>..<old_head>  vs  git diff <new_base>..<new_head>
#   2. DECISIVE -- every hop from <new_head> back to <old_head> has exactly two
#      parents and its tree equals the AUTOMATIC merge of those two parents:
#        git rev-list --parents -1 <cur>          # exactly <cur> <p1> <p2>
#        (worktree at <p1>) git merge --no-commit --no-ff <p2>; git write-tree
#        == git rev-parse <cur>^{tree}
#      Equal => no manual conflict resolution => PURE.  Different => a
#      hand-resolved merge or a content change => NOT PURE (re-review).  This
#      layer is immune to hunk-context shifts and catches hand-resolved merges,
#      which is why it REPLACED "apply the old own-diff and compare".
#   3. pre-filter only -- `-U0` changed-line sequences, and `git range-diff` as
#      a hint.  range-diff's `=` is a bonus; `!` does NOT mean "changed" (a
#      `cherry picked from …` trailer alone produces `!`).
#
# CHAIN (hq, 2026-10-09): because the walk is transitive, EVERY hop must pass the
# decisive tree check; one failing hop is NOT PURE.  Every hop prints its own
# line and the final line carries the whole-chain verdict plus the hop count.
# Two real instances: #1348 = 1 hop, #1157 = 3 hops
# (dc32db246 -> 9469101e -> d7acd4eb -> 52aff69d).  The two opposite failure
# modes this design avoids, both worse than a needless re-review:
#   * SINGLE-HOP-ONLY would report a genuine multi-hop advance as NOT PURE --
#     fail-closed (safe) but it forces a re-review that is not earned;
#   * STRUCTURAL-ONLY ("exactly two parents" without the tree check) would PASS a
#     chain whose intermediate hop was hand-resolved -- DANGEROUS.  The decisive
#     per-hop tree check is exactly what catches that.
# Never silently judge a multi-hop advance as single-hop.
#
# CONTROL (the point of the tool): tests/tooling/ci/execution/
# test_forward_move_equivalent.py builds (a) a genuine pure forward-move that
# MUST report PURE and (b) a commit with the SAME parents but a HAND-EDITED tree
# that MUST report NOT PURE.  (b) is the discriminating case: the structural
# layer alone would pass it, proving layer 2 is load-bearing.  LESSON: a control
# built on a NO-CONFLICT merge (`checkout --ours` + `add` + `write-tree`) is
# VACUOUS -- the tree does not move, so nothing is tested.  A control that
# cannot fail is not a control.
set -uo pipefail

if [ "$#" -ne 4 ]; then
  echo "usage: forward_move_equivalent.sh <old_merge_base> <old_head> <new_base> <new_head>" >&2
  echo "  walks every hop from <new_head> back to <old_head> (multi-hop supported)" >&2
  exit 2
fi

OLD_MB=$1
OLD_HEAD=$2
NEW_BASE=$3
NEW_HEAD=$4

REPO=$(git rev-parse --show-toplevel 2>/dev/null) || {
  echo "forward-move: not inside a git repository" >&2
  exit 2
}

for name in OLD_MB OLD_HEAD NEW_BASE NEW_HEAD; do
  eval "ref=\${$name}"
  sha=$(git -C "$REPO" rev-parse --verify --quiet "${ref}^{commit}" 2>/dev/null) || {
    echo "forward-move: verdict=NOT_PURE reason=cannot-resolve $name=$ref"
    exit 1
  }
  eval "$name=\$sha"
done

TMP=$(mktemp -d "${TMPDIR:-/tmp}/forward-move.XXXXXX")
WT="$TMP/wt"
cleanup() {
  git -C "$REPO" worktree remove --force "$WT" >/dev/null 2>&1 || true
  git -C "$REPO" worktree prune >/dev/null 2>&1 || true
  rm -rf "$TMP"
}
trap cleanup EXIT

echo "forward-move: refs old_merge_base=$OLD_MB old_head=$OLD_HEAD new_base=$NEW_BASE new_head=$NEW_HEAD"

# --- layer 1: the fast path (own diffs byte-identical) -----------------------
git -C "$REPO" diff "$OLD_MB".."$OLD_HEAD" >"$TMP/own_old.diff"
git -C "$REPO" diff "$NEW_BASE".."$NEW_HEAD" >"$TMP/own_new.diff"
OLD_DIFF=$(git -C "$REPO" hash-object --stdin <"$TMP/own_old.diff")
NEW_DIFF=$(git -C "$REPO" hash-object --stdin <"$TMP/own_new.diff")
if cmp -s "$TMP/own_old.diff" "$TMP/own_new.diff"; then
  echo "forward-move: layer1 own_diffs_identical=yes old_diff_object=$OLD_DIFF new_diff_object=$NEW_DIFF"
  echo "forward-move: verdict=PURE reason=layer1-own-diffs-byte-identical"
  exit 0
fi
echo "forward-move: layer1 own_diffs_identical=no old_diff_object=$OLD_DIFF new_diff_object=$NEW_DIFF (fast path missed; layer 2 decides)"

# --- layer 3: pre-filter (hint only, NEVER decides) --------------------------
# Strip hunk headers so line-number shifts do not produce fake differences.
filter_u0() { git -C "$REPO" diff -U0 "$1".."$2" | grep -E '^[+-]' | grep -vE '^(\+\+\+|---)'; }
filter_u0 "$OLD_MB" "$OLD_HEAD" >"$TMP/seq_old.txt"
filter_u0 "$NEW_BASE" "$NEW_HEAD" >"$TMP/seq_new.txt"
if cmp -s "$TMP/seq_old.txt" "$TMP/seq_new.txt"; then
  SEQ=identical
else
  SEQ=differs
fi
RANGE_DIFF=$(git -C "$REPO" range-diff "$OLD_MB".."$OLD_HEAD" "$NEW_BASE".."$NEW_HEAD" 2>/dev/null || true)
RD_EQUALS=$(printf '%s\n' "$RANGE_DIFF" | grep -c '^[0-9].* = ' || true)
RD_BANGS=$(printf '%s\n' "$RANGE_DIFF" | grep -c '^[0-9].* ! ' || true)
echo "forward-move: layer3 prefilter_u0_sequences=$SEQ range_diff_equals=$RD_EQUALS range_diff_bangs=$RD_BANGS (hint only, not a verdict)"

# --- layer 2: DECISIVE (every hop tree == automatic merge of its parents) ----
# The automatic merge of p1 and p2, computed in a temp worktree, because the
# repo's own comment records that `git merge-tree --write-tree`'s machine
# output changed in git 2.38.  Prints the tree sha, or nothing on conflict.
auto_tree() {
  local p1=$1 p2=$2 tree=""
  rm -rf "$WT"
  git -C "$REPO" worktree add --detach --force "$WT" "$p1" >/dev/null 2>&1 || return 1
  if git -C "$WT" -c user.email=fm@test.invalid -c user.name=forward-move \
      merge --no-commit --no-ff "$p2" >/dev/null 2>&1; then
    tree=$(git -C "$WT" write-tree 2>/dev/null) || tree=""
  else
    git -C "$WT" merge --abort >/dev/null 2>&1 || true
  fi
  git -C "$REPO" worktree remove --force "$WT" >/dev/null 2>&1 || true
  [ -n "$tree" ] || return 1
  printf '%s\n' "$tree"
}

cur=$NEW_HEAD
hops=0
chain_ok=yes
while [ "$cur" != "$OLD_HEAD" ]; do
  hops=$((hops + 1))
  if [ "$hops" -gt 16 ]; then
    echo "forward-move: hop cur=$cur structural=no reason=chain-above-16-hops"
    chain_ok=no
    break
  fi
  # shellcheck disable=SC2086
  set -- $(git -C "$REPO" rev-list --parents -1 "$cur")
  if [ -z "${3:-}" ] || [ -n "${4:-}" ]; then
    echo "forward-move: hop cur=$cur parents=${2:-none},${3:-none} structural=no reason=need-exactly-two-parents"
    chain_ok=no
    break
  fi
  p1=$2
  p2=$3
  tree=$(git -C "$REPO" rev-parse "$cur^{tree}")
  if auto=$(auto_tree "$p1" "$p2"); then
    if [ "$auto" != "$tree" ]; then
      echo "forward-move: hop cur=$cur parents=$p1,$p2 tree=$tree auto_tree=$auto pure=no reason=hand-resolution-or-content-change"
      chain_ok=no
      break
    fi
    echo "forward-move: hop cur=$cur parents=$p1,$p2 tree=$tree auto_tree=$auto pure=yes"
  else
    echo "forward-move: hop cur=$cur parents=$p1,$p2 tree=$tree auto_tree=<conflict> pure=no"
    chain_ok=no
    break
  fi
  cur=$p1
done

if [ "$chain_ok" = yes ] && [ "$cur" = "$OLD_HEAD" ]; then
  echo "forward-move: verdict=PURE old_head=$OLD_HEAD new_head=$NEW_HEAD hops=$hops reason=every-hop-tree-equals-auto-merge"
  exit 0
fi
echo "forward-move: verdict=NOT_PURE old_head=$OLD_HEAD new_head=$NEW_HEAD hops=$hops reason=parents-or-tree-differs"
exit 1
