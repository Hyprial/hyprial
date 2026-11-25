"""Mac-host jobs that route through the local proxy must bypass it for the forge.

On the macos-host runner the hosted/e2e jobs set HTTP(S)_PROXY to the local
mihomo (127.0.0.1:7896).  actions/upload-artifact@v3 does not treat the
leading-dot entry ``.internal.hyprial.com`` as matching
``git.internal.hyprial.com``, so every artifact upload went through the
proxy to the external exit and failed ("socket hang up" 5/5 on every Mac
hosted run, greens included, hidden by continue-on-error).  The exact host
must be in NO_PROXY; tailscale and the tailnet CGNAT range go with it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[4] / '.github/workflows'
REQUIRED_EXACT = ("git.internal.hyprial.com", "tailscale", "100.64.0.0/10")


def _proxied_macos_host_jobs() -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        workflow_env = document.get("env") or {}
        for name, job in (document.get("jobs") or {}).items():
            if not isinstance(job, dict) or job.get("runs-on") != "macos-host":
                continue
            env = {**workflow_env, **(job.get("env") or {})}
            if not (env.get("HTTP_PROXY") or env.get("HTTPS_PROXY")):
                continue
            found.append((path.name, name, str(env.get("NO_PROXY", ""))))
    return found


def test_some_macos_host_job_is_proxied() -> None:
    # Guards the guard: if no job matches, the check below proves nothing.
    assert _proxied_macos_host_jobs()


@pytest.mark.parametrize(
    ("workflow", "job", "no_proxy"),
    _proxied_macos_host_jobs(),
    ids=lambda value: str(value)[:40],
)
def test_proxied_macos_host_job_bypasses_the_forge_exactly(
    workflow: str, job: str, no_proxy: str
) -> None:
    entries = {entry.strip() for entry in no_proxy.split(",") if entry.strip()}
    missing = [host for host in REQUIRED_EXACT if host not in entries]
    assert not missing, f"{workflow}:{job} NO_PROXY lacks exact {missing}"
