#!/usr/bin/env python3
"""Publish 收尾验收:刚推上去的那个 commit 上,README 的安装命令能装吗?

⭐ 这一步的位置是判据的一部分:它跑在 publish **刚把快照推上镜像之后**,
   因为那是公网内容刚变的唯一时刻,也是这些判据唯一会查出东西的时刻。

判据(与 org-improver / infra-ops 2026-09-16 对齐):
  ① README 里 ⛔ 不得有字面占位符 ``<version-tag>``
  ② README 里每个安装 tag 必须在镜像上【当前真存在】(git/ref/tags/<tag> ⇒ 200)
  ③ ⭐ **正控先跑**:一个已知不存在的 tag 必须 404 ——
     ⛔ 不过就【停住】且 ⛔ 不报后面的结论(否则那些 200 没有分辨力)
  ④ ⛔ 不读 ``raw.githubusercontent.com/<repo>/main/...``:它按【文件】粒度缓存,
     陈旧时完全看不出来(200、内容自洽、格式正确)
  ⑤ ⭐ 一律按 **刚推的那个 commit sha** 读,⛔ 不按 ``main`` 这个名字 ——
     🔑 publish 自己知道它刚推的 sha,而任何按 ref 的读都可能拿到上一次的内容。
     在这个位置上这一格是免费的。

⚠️ 措辞边界:只断言「这个 ref 存在」,⛔ 不断言「它指向我们发的那个版本」——
   轨道 tag(``dev-track`` 等)会移动:实测同一下午从 f87dfb28(0.4.5)漂到
   76caefbd(0.4.6),尺度是分钟级。

退出码:0 = 全部通过;1 = 有判据失败(⇒ 让 publish 红);2 = 用法/取数失败。
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
INSTALL_RE = re.compile(r"uv tool install(?: --force)? git\+https://github\.com/[^\s]+\.git@(\S+)")
PLACEHOLDER = "<version-tag>"
CONTROL_TAG = "definitely-absent-tag-for-control"
# Transport failures (-1) are retried; only a persistent one is reported, and
# then as unverifiable -- never as "the tag does not exist" (2026-10-30: one
# TLS EOF failed the v0.5.5.2.2 release before its track step).
FETCH_BACKOFF = (2.0, 5.0, 10.0)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _get_retrying(url: str) -> tuple[int, bytes]:
    status, body = _get(url)
    for wait in FETCH_BACKOFF:
        if status != -1:
            break
        _sleep(wait)
        status, body = _get(url)
    return status, body


def _get(url: str) -> tuple[int, bytes]:
    """⛔ 刻意【不带任何凭据】。

    镜像仓是 public,本脚本用到的三类端点匿名全部可读(2026-09-16 实测:
    ``git/ref/tags/<存在>`` ⇒ 200 · ``git/ref/tags/<不存在>`` ⇒ 404 ·
    ``contents/README.md?ref=<sha>`` ⇒ 200)——⭐ **正控的分辨力在匿名下也成立**。
    ⇒ 🔑 token 在这条路径上**不提供任何能力,只提供速率余量**;
      而一枚不需要的凭据,是将来最容易被扩权的那一种。
    ⚠️ 残余风险:匿名 60 次/小时,且分母是【每个出口 IP】。
      ⇒ ⭐ 撞上时表现为 403,而**正控先跑**会读到 403≠404 ⇒ **立即停住**,
        📌 所以限流变成一次响亮的红,⛔ 不是静默的假绿。
    """
    headers = {
        # ⛔ 不用默认 UA:部分前置对 Python-urllib 直接拒。
        "User-Agent": "hyprial-verify-public-install/1",
        "Accept": "application/vnd.github+json",
    }
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=headers), timeout=20
        ) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:  # 网络层失败与 404 不是一回事,单独报
        print(f"  ⛔ 取数失败 {url}: {e}", file=sys.stderr)
        return -1, b""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="镜像仓,如 Hyprial/hyprial")
    ap.add_argument("--commit", required=True,
                    help="刚推上去的 commit sha(⛔ 不要传 main)")
    a = ap.parse_args()

    if a.commit in {"main", "HEAD", ""}:
        print("verify-public-install: --commit 必须是 sha,⛔ 不能是 ref 名", file=sys.stderr)
        return 2

    lines: list[str] = [f"verify-public-install: repo={a.repo} commit={a.commit[:12]}"]
    failed = 0

    # ③ 正控先跑 —— 不过就停,⛔ 不报后面的结论
    ctl, _ = _get(f"{API}/repos/{a.repo}/git/ref/tags/{CONTROL_TAG}")
    if ctl != 404:
        lines.append(f"  control: 已知不存在的 tag 返回 {ctl},必须 404 "
                     "⇒ 存在性判定无分辨力,⛔ 停止(后面的结论不予给出)")
        print("\n".join(lines))
        return 1
    lines.append("  control: 已知不存在的 tag ⇒ 404 ✅ 尺子有分辨力")

    # ⑤ 按 sha 读,⛔ 不按 main
    st, body = _get(f"{API}/repos/{a.repo}/contents/README.md?ref={a.commit}")
    if st != 200:
        lines.append(f"  readme: 按 commit 取 README 失败 HTTP {st}")
        print("\n".join(lines))
        return 1
    readme = base64.b64decode(json.loads(body).get("content", "")).decode("utf-8", "replace")
    lines.append(f"  readme: {len(readme)} bytes @ {a.commit[:12]}(按 sha 读,⛔ 非 main)")

    # ① 占位符
    n = readme.count(PLACEHOLDER)
    if n:
        failed += 1
        lines.append(f"  ⛔ placeholder: {PLACEHOLDER} 出现 {n} 次 —— 用户复制后必定失败")
    else:
        lines.append(f"  ✅ placeholder: 无 {PLACEHOLDER}")

    # ② 每个 tag 必须真存在
    tags = sorted(set(INSTALL_RE.findall(readme)))
    if not tags:
        failed += 1
        lines.append("  ⛔ tags: 没解析到任何公网安装命令 —— 判据落空,当作失败")
    for tag in tags:
        if tag.startswith("<"):
            failed += 1
            lines.append(f"  ⛔ tag {tag}: 是字面占位符,⛔ 不是 ref")
            continue
        ts, tb = _get_retrying(f"{API}/repos/{a.repo}/git/ref/tags/{tag}")
        sha = ""
        if ts == 200:
            try:
                sha = json.loads(tb)["object"]["sha"][:12]
            except Exception:
                sha = "?"
        if ts == 200:
            lines.append(f"  ✅ tag {tag}: 200 -> {sha}")
        elif ts == -1:
            failed += 1
            lines.append(f"  ⛔ tag {tag}: unverifiable(fetch_failed) —— "
                         f"{len(FETCH_BACKOFF) + 1} 次取数都是传输失败,无法判断它在不在")
        else:
            failed += 1
            lines.append(f"  ⛔ tag {tag}: HTTP {ts} —— 该 tag 在镜像上不存在,"
                         "用户复制这条命令会失败,而报错像他自己的网络问题")

    lines.append(f"verify-public-install: {'PASS' if not failed else f'FAIL ({failed})'}")
    print("\n".join(lines))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
