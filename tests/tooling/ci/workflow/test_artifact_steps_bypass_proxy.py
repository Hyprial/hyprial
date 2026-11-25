"""Artifact upload/download steps must not inherit the job's HTTP(S) proxy.

Measured 2026-10-30 (probe branch ci/artifact-upload-probe, run 40608):
with the gate-python.yml proxy env (HTTPS_PROXY=http://host.docker.internal:7896 plus
a NO_PROXY list that DOES contain the runtime host forgejo-tailscale),
actions/upload-artifact@v3 still tunnelled every chunk PUT through the proxy:
16/16 uploads failed. Without the proxy env, 16/16 succeeded with zero
retries. On the shared runners the proxy resolves, and the intermittent
"socket hang up" of chunk PUTs leaves the server with no tmp<run_id>
directory, so the confirm returns 500 and the step retries for minutes.

So every artifact step clears the four proxy variables itself, and the
artifact traffic goes straight to the runtime URL.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[4]
WORKFLOWS = ROOT / ".forgejo" / "workflows"
PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
ARTIFACT_ACTIONS = ("actions/upload-artifact", "actions/download-artifact")


def _artifact_steps():
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        doc = yaml.safe_load(path.read_text()) or {}
        for job_id, job in (doc.get("jobs") or {}).items():
            for index, step in enumerate(job.get("steps") or []):
                uses = str(step.get("uses", ""))
                if uses.startswith(ARTIFACT_ACTIONS):
                    yield f"{path.name}:{job_id}:{step.get('id') or step.get('name') or index}", step


def test_there_are_artifact_steps_to_check():
    assert sum(1 for _ in _artifact_steps()) >= 20


def test_every_artifact_step_clears_the_proxy():
    leaking = []
    for where, step in _artifact_steps():
        env = step.get("env") or {}
        if any(env.get(name) != "" for name in PROXY_VARS):
            leaking.append(where)
    assert not leaking, f"artifact steps that would inherit the job proxy: {leaking}"
