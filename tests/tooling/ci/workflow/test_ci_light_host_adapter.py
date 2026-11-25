"""ci-light host 适配改写的回归钉子（fix round 1，2026-11-05 评审阻断点）。

阻断点: gate-python.yml pr-hygiene 首个步骤把改写后的 origin/代理只写进
$GITHUB_ENV —— 它只对**后续**步骤生效；同一步骤随后的
`git remote add origin "${GITHUB_SERVER_URL%/}/..."` 与 fetch 仍持有容器里
可解析、宿主机不可解析的默认值（及 host.docker.internal 代理）⇒ 首个
ci-light 实跑必死在本步骤内。修复后改写值经 env_sync 同时进入当前 shell
与 $GITHUB_ENV，两条作用域走同一写路径，不再漂移。

钉法（e2e-verifier 判词形状：正向形状必须配能变红的执行断言）: 从 gate-python.yml
原样抽出该步骤的 run: 脚本执行。
  * DEFAULT_SERVER_URL 指向 RFC 2606 保证不解析的 .invalid 主机 ⇒ 必走改写分支；
  * GIT_CONFIG_GLOBAL/SYSTEM 清空 + HOME 隔离 + 假 token ⇒ fetch 必然失败，
    全程不依赖网络；remote add 早于 fetch，断言点因此成立。
  * 旧行为下 remote URL 保持 .invalid ⇒ 断言变红，即本测试对回归敏感。
  * localhost 用例钉住可解析分支：保持 runner 默认、不写 GITHUB_ENV。
其余三个已切换 job 的 server URL 解析形状用静态审计钉住（同轮评审要求逐一
复核）：skill-declaration/contract-impact 用局部 origin 变量、
release-changelog 经 $GITHUB_ENV 交给后续 checkout 的 server-url。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[4] / ".forgejo/workflows"

CHECKOUT_STEP_NAME = "Check out the head commit with base history"
UNRESOLVABLE_SERVER_URL = "http://host-adapter-regression.invalid:3000"
RESOLVABLE_SERVER_URL = "http://localhost:3000"
REPOSITORY = "HyprialOS/harness-bridge"

FORGEJO_ORIGIN = "http://forgejo:3000"
REWRITTEN_ENV_VARS = (
    "GITHUB_SERVER_URL=http://forgejo:3000",
    "HTTPS_PROXY=http://127.0.0.1:7896",
    "HTTP_PROXY=http://127.0.0.1:7896",
    "https_proxy=http://127.0.0.1:7896",
    "http_proxy=http://127.0.0.1:7896",
    "NO_PROXY=localhost,127.0.0.1,::1,forgejo,forgejo-tailscale,tailscale,.ts.net,.internal.hyprial.com,code.hyprial.com",
    "no_proxy=localhost,127.0.0.1,::1,forgejo,forgejo-tailscale,tailscale,.ts.net,.internal.hyprial.com,code.hyprial.com",
    "FORGEJO_SERVER_URL=http://forgejo:3000",
)

JOB_PROXY_ENV = {
    "HTTPS_PROXY": "http://host.docker.internal:7896",
    "HTTP_PROXY": "http://host.docker.internal:7896",
    "NO_PROXY": "localhost,127.0.0.1,::1,tailscale,forgejo-tailscale,.ts.net,.internal.hyprial.com,code.hyprial.com",
    "UV_HTTP_TIMEOUT": "60",
}


def _checkout_script() -> str:
    doc = yaml.safe_load((WORKFLOWS / "gate-pr-hygiene.yml").read_text(encoding="utf-8"))
    steps = doc["jobs"]["pr-hygiene"]["steps"]
    for step in steps:
        if isinstance(step, dict) and step.get("name") == CHECKOUT_STEP_NAME:
            return str(step["run"])
    raise AssertionError(f"gate-pr-hygiene.yml: pr-hygiene 缺少名为 {CHECKOUT_STEP_NAME!r} 的步骤")


def _run_checkout_script(server_url: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    workdir = tmp_path / "work"
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    workdir.mkdir()
    home.mkdir()
    bin_dir.mkdir()
    resolver = bin_dir / "python3"
    resolver.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "sys.exit(0 if sys.argv[-1] == 'localhost' else 1)\n",
        encoding="utf-8",
    )
    resolver.chmod(0o755)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": str(home / "gitconfig"),
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GITHUB_ENV": str(tmp_path / "github-env"),
        "GITHUB_SERVER_URL": server_url,
        "DEFAULT_SERVER_URL": server_url,
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_RUN_ID": "1",
        "GITHUB_TOKEN": "host-adapter-regression-dummy-token",
        "RUNNER_TEMP": str(tmp_path),
        "BASE_REF": "dev",
        "HEAD_SHA": "0" * 40,
        **JOB_PROXY_ENV,
    }
    return subprocess.run(
        ["bash", "-c", _checkout_script()],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _origin_url(workdir: Path) -> str:
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=workdir,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_unresolvable_default_host_rewrites_origin_within_the_same_step(tmp_path) -> None:
    result = _run_checkout_script(UNRESOLVABLE_SERVER_URL, tmp_path)
    assert result.returncode != 0, (
        "fetch 必须失败（假 token + 无网络）；exit 0 意味着测试环境外联，"
        f"stdout={result.stdout!r} stderr={result.stderr[-2000:]!r}"
    )
    workdir = tmp_path / "work"
    remote = _origin_url(workdir)
    assert remote == f"{FORGEJO_ORIGIN}/{REPOSITORY}.git", (
        "改写必须在本步骤内生效（$GITHUB_ENV 只影响后续步骤）："
        f"remote={remote!r} stdout={result.stdout!r}"
    )
    assert UNRESOLVABLE_SERVER_URL not in remote
    github_env = tmp_path / "github-env"
    assert github_env.exists(), "改写值仍需为后续步骤写入 $GITHUB_ENV"
    written = github_env.read_text(encoding="utf-8")
    for entry in REWRITTEN_ENV_VARS:
        assert entry in written, f"$GITHUB_ENV 缺少 {entry!r}: {written!r}"


def test_resolvable_default_host_keeps_runner_defaults(tmp_path) -> None:
    result = _run_checkout_script(RESOLVABLE_SERVER_URL, tmp_path)
    assert result.returncode != 0, "fetch 同样必须失败：可解析分支也不得外联成功"
    remote = _origin_url(tmp_path / "work")
    assert remote == f"{RESOLVABLE_SERVER_URL}/{REPOSITORY}.git", f"可解析时不得改写 origin: {remote!r}"
    github_env = tmp_path / "github-env"
    assert not github_env.exists() or github_env.read_text(encoding="utf-8") == ""


def _job_scripts(workflow: str, job: str) -> list[str]:
    doc = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    steps = doc["jobs"][job].get("steps") or []
    return [str(step["run"]) for step in steps if isinstance(step, dict) and "run" in step]


def _assert_origin_resolved_before_remote_add(workflow: str, job: str) -> None:
    """局部 origin 变量形状：remote add 只能落在已解析的 ${origin:-...} 上。"""
    scripts = _job_scripts(workflow, job)
    assert scripts, f"{workflow}:{job} 没有 run 步骤"
    assert any('origin="http://forgejo:3000"' in script for script in scripts), (
        f"{workflow}:{job} 缺少不可解析分支的 forgejo origin 回退"
    )
    for script in scripts:
        for match in re.finditer(r"remote add origin.*?(?=\n\s*\n|\Z)", script, re.DOTALL):
            block = match.group(0)
            assert '"${origin' in block, (
                f"{workflow}:{job} 的 remote add 未用局部 origin 变量（同步骤作用域回归）: {block!r}"
            )


def test_other_ci_light_jobs_resolve_the_origin_in_step() -> None:
    _assert_origin_resolved_before_remote_add("gate-pr-declarations.yml", "declaration")
    _assert_origin_resolved_before_remote_add("gate-pr-declarations.yml", "impact")


def test_release_changelog_hands_the_origin_to_a_later_step() -> None:
    doc = yaml.safe_load((WORKFLOWS / "gate-pr-declarations.yml").read_text(encoding="utf-8"))
    steps = doc["jobs"]["changelog"]["steps"]
    resolver = next(step for step in steps if "CHECKOUT_ORIGIN" in str(step.get("run", "")))
    assert 'origin="http://forgejo:3000"' in str(resolver["run"])
    assert 'printf \'CHECKOUT_ORIGIN=%s\\n\' "${origin}" >> "${GITHUB_ENV}"' in str(resolver["run"])
    checkout = next(step for step in steps if step.get("uses") == "actions/checkout@v4")
    assert checkout["with"]["server-url"] == "${{ env.CHECKOUT_ORIGIN }}"
