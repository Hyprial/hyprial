#!/usr/bin/env bash
# Guarantee fontconfig and a Chinese-capable font before the GUI browser tests.
#
# Decisions (2026-11-15, PR 1f/F6):
# - `fc-match :lang=zh-cn` answers with some font even when none covers Chinese,
#   so the screenshot setup (gui/tests/dev01-visual-font.mjs) trusts only files
#   that `fc-list ':lang=zh' file` lists. This step makes that list non-empty
#   instead of relying on a side effect of the Chromium provisioning step.
# - It is a no-op when fc-list already lists a zh font; otherwise it installs
#   fonts-wqy-zenhei and fontconfig, routing Debian hosts through the job's
#   proxy variables the way apt itself does not (only uppercase ones exist).
# - It ends by asserting the list is non-empty; a failed install is a failed
#   step, never a skip.
# - The emptiness test captures the list instead of piping it into `grep -q`:
#   under pipefail an early grep exit can kill fc-list with SIGPIPE.
set -euo pipefail

has_zh_font() {
  command -v fc-list >/dev/null 2>&1 && [[ -n "$(fc-list ':lang=zh')" ]]
}

if has_zh_font; then
  echo "ensure-cjk-font: already present ($(fc-list ':lang=zh' | wc -l) zh font entries)"
  exit 0
fi

export DEBIAN_FRONTEND=noninteractive
apt_opts=()
proxy_http="${http_proxy:-${HTTP_PROXY:-}}"
proxy_https="${https_proxy:-${HTTPS_PROXY:-${proxy_http}}}"
for host in deb.debian.org security.debian.org; do
  [[ -n "$proxy_http" ]] && apt_opts+=(-o "Acquire::http::Proxy::${host}=${proxy_http}")
  [[ -n "$proxy_https" ]] && apt_opts+=(-o "Acquire::https::Proxy::${host}=${proxy_https}")
done

apt-get "${apt_opts[@]}" update
apt-get "${apt_opts[@]}" install -y --no-install-recommends fonts-wqy-zenhei fontconfig
fc-cache -f

has_zh_font || { echo "ensure-cjk-font: fc-list ':lang=zh' is still empty after install" >&2; exit 1; }
echo "ensure-cjk-font: installed ($(fc-list ':lang=zh' | wc -l) zh font entries)"
