#!/usr/bin/env bash
# 部署后探针：先证非流式链路通，再回答 P1 唯一未知的那一格 ——
# AI Gateway binding（env.AI.run）到底支不支持**流式**。
#
# 为什么需要它：设计 §9 选了 binding（Worker 里不带任何 CF 凭据），但 binding 的
# 结构化入参没有承诺支持 stream ⇒ 部署后必须当场测；不支持就切 §9-C
# （HTTP provider-native 端点 + Access 服务令牌）。
#
# ⚠️ 前提：令牌必须是**成员的 Casdoor 访问令牌**（`hyprial login` 那套身份）。
#    ⛔ 不是 CF API key —— 本 Worker 验的是 RS256 签名 + iss + aud，CF key 一律 401，
#    而那个 401 是"用错凭据"，不是"验签坏了"。两者别混。
# ⛔ 令牌只从环境变量读（不进 argv、不进日志、本脚本不回显）。
#
# 用法：
#   HYPRIAL_TOKEN='<Casdoor 访问令牌>' ./probe.sh [base-url]
#   可选： HYPRIAL_MODEL='anthropic/claude-sonnet-4.6'
set -euo pipefail

BASE="${1:-https://model.hyprial.ai}"
TOKEN="${HYPRIAL_TOKEN:?need HYPRIAL_TOKEN: a member Casdoor access token (NOT a CF API key)}"
MODEL="${HYPRIAL_MODEL:-anthropic/claude-sonnet-4.6}"

hr() { printf '\n=== %s ===\n' "$1"; }

hr "0) /healthz（应 200；不需要令牌）"
curl -sS -o /dev/null -w '  HTTP %{http_code}\n' "$BASE/healthz"

hr "1) 非流式（应 200；证明 验签→binding 转发→用量记录 整条通）"
code=$(curl -sS -D /tmp/probe-h.txt -o /tmp/probe-b.json -w '%{http_code}' \
  -X POST "$BASE/v1/chat/completions" \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d "{\"model\":\"$MODEL\",\"max_tokens\":16,\"messages\":[{\"role\":\"user\",\"content\":\"reply with the single word: ok\"}]}")
echo "  HTTP $code"
grep -iE '^(cf-aig-|content-type)' /tmp/probe-h.txt 2>/dev/null | sed 's/^/  hdr /' || true
head -c 240 /tmp/probe-b.json 2>/dev/null; echo
if [ "$code" != "200" ]; then
  echo "  ⚠️ 非 200 ⇒ 先解决这一格再谈流式：401 = 令牌不是成员 Casdoor 令牌或已过期；"
  echo "     400 = 模型 id 缺 '<厂商>/<模型>' 前缀；5xx = 上游/绑定问题。"
  exit 1
fi

hr "2) ⭐ 关键问题：流式是否**逐块**到达"
echo "  判读：首块明显早于末块 ⇒ binding 支持流式（留 A 案）；"
echo "        所有块同一时刻到达 ⇒ binding 把整体一次性返回 ⇒ 走 §9-C 兜底。"
curl -sS -N -X POST "$BASE/v1/chat/completions" \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d "{\"model\":\"$MODEL\",\"max_tokens\":32,\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"count from 1 to 10, one per line\"}]}" \
  | python3 -c '
import sys, time
t0 = time.time(); n = 0; first = None
for line in sys.stdin:
    line = line.rstrip("\n")
    if not line.strip():
        continue
    n += 1
    dt = time.time() - t0
    if first is None:
        first = dt
    if n <= 3 or n % 10 == 0:
        print(f"  块 {n} @{dt:.2f}s: {line[:80]}")
print(f"  共 {n} 块；首块 @{first if first is not None else -1:.2f}s")
'
echo
echo "  ⚠️ 非流式那一步若是 200，请顺带在 CF 网关日志里确认 **我们盖章的 metadata**"
echo "     出现了（org/user/req ≤5 项）—— 那是后端归属与计费对账的锚点。"