#!/usr/bin/env bash
# premerge.sh <PR#>   — the ACTION-MOMENT gate, read-only. Run immediately before firing the merge.
# Why it exists: everything else in my sequence was an instrument; this step was typed by hand, and it is the
# step that decides whether the live coordinates still match what the payload was rendered against.
set -uo pipefail
source "$(dirname "$0")/vlib.sh"   # ① 同目录相对引入
REPO=HyprialOS/harness-bridge
N=${1:?usage: premerge.sh <PR#>}
# ⑤ 的另一半:payload/coords 的目录由调用方给(VERIFY_OUTDIR),必须与 devcheck.sh 渲染时同目录 ⇒ 必填,⛔ 写死 /tmp。
OUTDIR="${VERIFY_OUTDIR:-}"
if [ -z "$OUTDIR" ]; then
  echo "   ⛔ DEPENDENCY MISSING: VERIFY_OUTDIR — 渲染产物目录未给(与 devcheck.sh 的 VERIFY_OUTDIR 同目录)。exit 3" >&2
  exit 3
fi
PAY="$OUTDIR/merge_p${N}.json"; CO="$OUTDIR/merge_p${N}.coords"
[ -f "$PAY" ] && [ -f "$CO" ] || { echo "   ⛔ no payload/coords for #$N in $OUTDIR ⇒ render first (devcheck.sh) — a stale payload must never be reused"; exit 3; }
PH=$(jq -r .head_commit_id "$PAY"); PD=$(sed -n 2p "$CO")
# ② 仓库根:VERIFY_REPO_DIR 或 cwd;取不到 ⇒ 点名报错。
ROOT=$(need_repo_root "premerge") || exit 3
cd "$ROOT" || exit 3
LH=$(tea_json "repos/$REPO/pulls/$N" | jq -r .head.sha); LD=$(git ls-remote origin refs/heads/dev | awk '{print $1}')   # ls-remote = authoritative
git fetch -q origin "$LH" 2>/dev/null; git fetch -q origin "$LD" 2>/dev/null
for s in "$LH" "$LD"; do git cat-file -e "$s" 2>/dev/null || { echo "   ⛔ OBJECT MISSING ($s) ⇒ CANNOT CERTIFY (an unread object is not a 'no')"; exit 3; }; done
# ⚠️ 2026-09-19(搬进仓时发现):④ 的「⚠️由③决定」标记原写在【值】的尾巴上
#    (`…=$(y …)  ⚠️由③决定`),而 verdict_conj 按最后一个 = 切值 ⇒ 值变成 "yes  ⚠️由③决定" ⇒ ④ 永远落入 miss ⇒
#    正向分支(四条全 yes ⇒ rc=0)【结构上不可达】(实测:四条全 yes 仍 rc=1)。标记必须留着(⛔ 删,判据),
#    所以挪进【键】的第一个 = 之前:值恢复纯 yes/no,标记仍逐字印在判决行里。
y(){ [ "$1" = "$2" ] && echo yes || echo no; }
# ③ is the gate hq-adjutant asked to promote from observation to precondition, enforced HERE (action moment):
AN=$([ -n "$LD" ] && [ -n "$LH" ] && git merge-base --is-ancestor "$LD" "$LH" 2>/dev/null && echo yes || echo no)
MT=$(git merge-tree --write-tree "$LD" "$LH" 2>/dev/null | head -1); HT=$(git rev-parse "$LH^{tree}" 2>/dev/null)
verdict_conj "premerge #$N" "①head-unmoved-since-render=$(y "$LH" "$PH")" "②dev-unmoved-since-render=$(y "$LD" "$PD")" \
  "③head-contains-current-dev-tip=$AN" "④landing-tree(⚠️由③决定)==this-head-tree=$(y "$MT" "$HT")"
rc=$?
[ "$AN" = no ] && echo "      ⛔ ③ 是【前置闸】:head ⛔ 含当前 dev 头 ⇒ 合并结果的落地树可能【⛔ 是】CI 测过的那棵 ⇒ 补折/catch-up 后再来"
[ "$MT" != "$HT" ] && echo "      ⛔ ④ 同上:现在合它,落地树⛔ 等于 head 树(dev 已前移)"
# ⚠️ 2026-09-19: ④ 不是独立条件 —— **③ 成立 ⇒ dev 是 head 的祖先 ⇒ 合并内容上就是快进 ⇒ 落地树【必然】= head 树** ⇒
#    所以 ④ 的信息量全部来自 ③;留它只作为【一致性核对】(若 ③ 被判读错,④ 会不同意)⇒ 判决里已标注 ⚠️由③决定 ✓
echo "   payload head=${PH:0:12} (render-time dev=${PD:0:12}) · live head=${LH:0:12} · live dev=${LD:0:12} (ls-remote)"
exit $rc
