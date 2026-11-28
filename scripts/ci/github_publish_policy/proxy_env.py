"""发布侧 git 环境的代理判定（从 `github_publish.py` 抽出，避免主模块超行预算）。

现场（2026-10-30，run 40868 / task 98850）:
    job env 里有 `HTTPS_PROXY=http://host.docker.internal:7896`（run 自己的 setupEnv 有记录），
    但 `github_publish` 的 git 命令走 `hardened_git_environment` 的**窄名单**（只留 PATH/LANG/TMPDIR/LC_*），
    代理被静默丢弃 ⇒ clone 裸连 github.com ⇒ `GnuTLS recv error (-110)` / `SSL connection timeout` /
    `expected flush after ref listing`。同一台机器经该代理 clone 只要 4-5 s。

判据: **请求了代理却没进 git 环境 ⇒ 直接失败**，不允许静默降级成裸连
（"配错了"不能伪装成"网络抖动"）。
"""

from __future__ import annotations

from typing import Protocol

#: 这几个变量被"请求"了才有代理意图；`no_proxy` 只是排除表，不算请求。
REQUESTED_KEYS = frozenset({"https_proxy", "http_proxy", "all_proxy"})
#: 出现在 git 环境里就算"带上了代理"的变量（含 `no_proxy`，它同样影响行为）。
PRESENT_KEYS = frozenset({"https_proxy", "http_proxy", "all_proxy", "no_proxy"})

MISSING_MESSAGE = (
    "proxy variables are set for this job but did not reach git's environment "
    "({names}); refusing to fall back to a direct connection to github.com"
)


class ProxyErrorType(Protocol):
    """调用方自己的错误类型（`PublishError`），避免本模块依赖主模块。"""

    def __call__(self, message: str) -> BaseException: ...  # pragma: no cover


def keys_in_git_env(requested_env: dict[str, str], git_env: dict[str, str],
                    error_type: ProxyErrorType) -> list[str]:
    """返回真正进了 git 环境的代理变量名；请求了却没到位 ⇒ 抛 `error_type`。"""
    keys = sorted(name for name in git_env if name.lower() in PRESENT_KEYS)
    requested = {name for name in requested_env if name.lower() in REQUESTED_KEYS}
    if requested and not [name for name in keys if name.lower() in REQUESTED_KEYS]:
        raise error_type(MISSING_MESSAGE.format(names=", ".join(sorted(requested))))
    return keys
