"""tests for scripts/verify/ — the verification instruments moved into the repo.

What is pinned here (spec: instruments-into-repo, 2026-09-19):
  ① scripts source the SIBLING vlib.sh, never /tmp/vlib.sh
  ② the repo root comes from VERIFY_REPO_DIR or `git rev-parse --show-toplevel`;
    when neither works the failure is NAMED (not an empty reading)
  ③ JOBLOG missing => rc 3 naming JOBLOG (was: silently empty logs)
  ④ DOCKER / FORGEJO_CONTAINER missing => rc 3 naming them (was: empty md5 read
    as a state; two empty strings comparing equal printed "✓ master==effective")
  ⑤ payload output dir is caller-given (VERIFY_OUTDIR), never hardcoded /tmp
  ⑥ vlib.sh's lifetime declaration is position-independent
  ⑦ verdict_conj is the three-value version (0/2/1; a certain NO beats pending);
    values are split on the LAST `=`
  ⑨ devcheck.sh has exactly ONE docker spelling (the overridable variable)
plus the regression found while moving: premerge.sh's ④ marker must live in the
KEY, because on the value's tail it made the all-yes branch structurally
unreachable.

Stubs: `tea`/`docker`/joblog stubs below return canned bytes in the SHAPES the
real endpoints/tools produce (the same shapes the scripts parse); they invent no
new shape. No test touches a real daemon, container, or network — git remotes
are local bare repos under tmp_path.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
VERIFY = REPO / "scripts" / "verify"
SCRIPTS = [
    "devcheck.sh",
    "tagaccept.sh",
    "intggate.sh",
    "onbranch.sh",
    "premerge.sh",
    "postmerge.sh",
]

pytestmark = pytest.mark.skipif(
    any(shutil.which(c) is None for c in ("bash", "git", "jq", "python3")),
    reason="verification instruments need bash/git/jq/python3 on PATH",
)

INSTR_ENV_VARS = [
    "VERIFY_REPO_DIR",
    "VERIFY_OUTDIR",
    "JOBLOG",
    "DOCKER",
    "FORGEJO_CONTAINER",
    "OLD_INTG",
    "BEFORE_ID",
    "INTG_REPO",
    "ONBRANCH_TASKS_FILE",
]


def run_script(script: str, *args: object, env: dict | None = None,
               cwd: Path | None = None, timeout: int = 90) -> subprocess.CompletedProcess:
    e = dict(os.environ)
    for k in INSTR_ENV_VARS:
        e.pop(k, None)
    if env:
        e.update({k: str(v) for k, v in env.items()})
    return subprocess.run(
        ["bash", str(VERIFY / script), *[str(a) for a in args]],
        capture_output=True, text=True, env=e, cwd=cwd, timeout=timeout,
    )


def run_vlib(body: str) -> subprocess.CompletedProcess:
    e = dict(os.environ)
    for k in INSTR_ENV_VARS:
        e.pop(k, None)
    return subprocess.run(
        ["bash", "-c", f'source "{VERIFY}/vlib.sh"\n{body}'],
        capture_output=True, text=True, env=e, timeout=30,
    )


def git(*args: str, cwd: Path) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(args)}: {r.stderr}"
    return r.stdout.strip()


class Rig:
    """A local bare `origin` + a work clone, so `git fetch origin` works offline."""

    def __init__(self, root: Path):
        # named harness-bridge.git: devcheck asserts origin matches the repo NAME
        self.origin = root / "harness-bridge.git"
        self.work = root / "work"
        self.origin.mkdir()
        git("init", "--bare", "-q", ".", cwd=self.origin)
        git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=self.origin)
        self.work.mkdir()
        git("init", "-q", ".", cwd=self.work)
        git("config", "user.email", "t@t", cwd=self.work)
        git("config", "user.name", "t", cwd=self.work)
        git("config", "commit.gpgsign", "false", cwd=self.work)
        git("remote", "add", "origin", str(self.origin), cwd=self.work)

    def commit(self, name: str, content: str, branch: str, parent: str | None = None) -> str:
        if parent:
            git("checkout", "-q", "-b", branch, parent, cwd=self.work)
        else:
            git("checkout", "-q", "--orphan", branch, cwd=self.work)
        (self.work / name).write_text(content)
        git("add", name, cwd=self.work)
        git("commit", "-q", "-m", f"{branch}: {name}", cwd=self.work)
        sha = git("rev-parse", "HEAD", cwd=self.work)
        git("push", "-q", "origin", f"{branch}:{branch}", cwd=self.work)
        return sha


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    r = Rig(tmp_path)
    r.dev = r.commit("a.txt", "base\n", "dev")
    r.head = r.commit("b.txt", "change\n", "feature", parent="dev")
    # devcheck fetches the PR head via refs/pull/<N>/head
    git("update-ref", "refs/pull/1/head", r.head, cwd=r.origin)
    git("fetch", "-q", "origin", cwd=r.work)
    return r


def write_stub(bin_dir: Path, name: str, body: str) -> Path:
    p = bin_dir / name
    p.write_text(f"#!/usr/bin/env bash\n{body}\n")
    p.chmod(0o755)
    return p


def tea_stub_body(pr_sha: str, tasks_json: str, pr: int = 1, title: str = "t") -> str:
    pr_json = json.dumps({
        "state": "open", "merged": False,
        "base": {"ref": "dev"}, "head": {"sha": pr_sha}, "title": title,
    })
    status_json = json.dumps({
        "state": "success", "total_count": 1,
        "statuses": [{"status": "success", "context": "unit"}],
    })
    return f"""set -u
path="$2"
case "$path" in
  repos/*/pulls/{pr}) printf '%s' '{pr_json}';;
  repos/*/branch_protections) printf '[]';;
  repos/*/collaborators/*/permission) printf '{{"permission":"write","role_name":"x"}}';;
  repos/*/commits/*/status) printf '%s' '{status_json}';;
  repos/*/actions/tasks*) printf '%s' '{tasks_json}';;
  *) echo "tea-stub: unexpected path: $path" >&2; exit 1;;
esac
"""


DOCKER_STUB = """set -u
case "$1" in
  ps) printf '%s\\n' 'hyprial-forgejo-forgejo-1' 'hyprial-forgejo-postgres-1';;
  inspect)
    if [ "${2:-}" = "-f" ]; then printf '2026-09-19T00:00:00Z\\n'; else exit 0; fi;;
  exec)
    shift 2  # drop: exec <container>
    case "$1" in
      md5sum) printf 'abc123  -\\n';;
      printenv)
        case "$2" in
          POSTGRES_USER) printf 'gitea\\n';;
          POSTGRES_DB) printf 'gitea\\n';;
          *) exit 1;;
        esac;;
      psql) printf '0\\n';;
      sh) printf 'on\\n';;  # sed of guard.conf -> FIRST_PARENT_MODE=on (guard FIXED)
      *) exit 1;;
    esac;;
  *) echo "docker-stub: unexpected: $*" >&2; exit 1;;
esac
"""


# ── ①⑥⑨ static: no host/staging addressing may live in the shipped scripts ──

FORBIDDEN = ["/tmp/vlib.sh", "/Users/", "/usr/local/bin/docker",
             "hq-forgejo-job-log", "/tmp/merge_p"]


def test_no_hardcoded_host_or_staging_paths():
    offenders = []
    for f in sorted(VERIFY.glob("*.sh")):
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue  # the move RECORD names the old paths in comments
            for bad in FORBIDDEN:
                if bad in line:
                    offenders.append(f"{f.name}:{i}: {bad}")
    assert not offenders, "hardcoded host/staging paths: " + "; ".join(offenders)


def test_all_scripts_source_sibling_vlib():
    for name in SCRIPTS + ["vlib.sh"]:
        text = (VERIFY / name).read_text()
        if name == "vlib.sh":
            continue
        assert 'source "$(dirname "$0")/vlib.sh"' in text, name


def test_vlib_lifetime_declaration_is_position_independent():
    text = (VERIFY / "vlib.sh").read_text()
    assert "与位置无关" in text
    assert "现在住在 /tmp" not in text  # the pre-move claim would be a lie in the repo


# ── ⑦ vlib semantics: three-value verdict, last-= split, value combinations ──

def test_verdict_conj_value_combinations():
    cases = [
        # (pairs, expected rc, expected word)
        (['"a=yes" "b=yes"'], 0, "达成"),
        (['"a=yes" "b=pending"'], 2, "WAIT"),
        (['"a=yes" "b=no"'], 1, "CANNOT CERTIFY"),
        # the discriminating cell: no+pending => the certain NO wins (rc 1, not WAIT)
        (['"a=no" "b=pending"'], 1, "CANNOT CERTIFY"),
        # value split on the LAST '=': a satisfied condition with '=' in the pair
        (['"②merge^2==head=yes"'], 0, "达成"),
    ]
    for pairs, rc, word in cases:
        r = run_vlib(f'verdict_conj "t" {" ".join(pairs)}; exit $?')
        assert r.returncode == rc, f"{pairs}: rc={r.returncode} out={r.stdout}"
        assert word in r.stdout


def test_need_readable_has_own_exit():
    r = run_vlib('need_readable "not-json{" "MYPAYLOAD" || exit 3')
    assert r.returncode == 3
    assert "MYPAYLOAD UNREADABLE" in r.stdout and "BYTES=" in r.stdout
    r = run_vlib('need_readable "{\\"a\\":1}" "OK" ; echo rc=$?')
    assert "rc=0" in r.stdout


def test_classify_task_classes_and_inflight_priority():
    # a RUNNING task whose log already shows a summary line is IN_FLIGHT — never a green
    r = run_vlib('printf "5118 passed in 30s\\n" | classify_task running')
    assert r.stdout.startswith("IN_FLIGHT")
    r = run_vlib('printf "5118 passed in 30s\\n" | classify_task success')
    assert r.stdout.startswith("READABLE_SUMMARY")
    r = run_vlib('printf "Skipping job x due to y\\n" | classify_task success')
    assert r.stdout.startswith("SKIPPED")
    r = run_vlib('printf "fatal: unable to access http://x\\n" | classify_task failure')
    assert r.stdout.startswith("NEVER_STARTED")
    r = run_vlib('printf "AssertionError: boom\\n" | classify_task failure')
    assert r.stdout.startswith("READABLE_DECISIVE")
    r = run_vlib('printf "something inconclusive\\n" | classify_task failure')
    assert r.stdout.startswith("UNREADABLE")


# ── ② repo root: named error when unresolvable; env override honoured ──

@pytest.mark.parametrize("script", SCRIPTS)
def test_repo_root_missing_is_a_named_error(script, tmp_path):
    args = {"devcheck.sh": ("1",), "tagaccept.sh": (), "intggate.sh": ("1", "b"),
            "onbranch.sh": ("j",), "premerge.sh": ("1",), "postmerge.sh": ("1", "m", "p", "t")}[script]
    env = {"VERIFY_OUTDIR": str(tmp_path)} if script == "premerge.sh" else None
    if script == "premerge.sh":
        # past the OUTDIR check, to reach the repo-root check
        (tmp_path / "merge_p1.json").write_text('{"head_commit_id":"x"}')
        (tmp_path / "merge_p1.coords").write_text("x\ny\n")
    r = run_script(script, *args, env=env, cwd=tmp_path)
    assert r.returncode != 0
    assert "VERIFY_REPO_DIR" in r.stderr, f"{script}: {r.stderr[:300]}"


def test_need_repo_root_honours_env_and_cwd(tmp_path):
    r = run_vlib("need_repo_root t")
    # cwd of the test process is inside this repo => resolves to a toplevel
    assert r.returncode == 0 and Path(r.stdout.strip()).is_dir()
    r2 = run_vlib(f'VERIFY_REPO_DIR="{tmp_path}" need_repo_root t')
    assert r2.returncode != 0  # tmp_path is not a git work tree
    r3 = run_vlib(f'VERIFY_REPO_DIR="{REPO}" need_repo_root t')
    assert r3.returncode == 0 and r3.stdout.strip() == str(REPO)


# ── ③ JOBLOG: missing => rc 3 naming it (tagaccept checks it before any read) ──

def test_tagaccept_joblog_missing_is_loud(rig):
    r = run_script("tagaccept.sh", "0" * 40, cwd=rig.work)
    assert r.returncode == 3
    assert "JOBLOG" in r.stderr
    r2 = run_script("tagaccept.sh", "0" * 40, env={"JOBLOG": "/nonexistent/joblog"},
                    cwd=rig.work)
    assert r2.returncode == 3 and "JOBLOG" in r2.stderr


# ── ④ docker / forgejo container: missing => rc 3 naming it (devcheck) ──

def test_devcheck_docker_missing_is_loud(rig):
    r = run_script("devcheck.sh", "1", env={"DOCKER": "/nonexistent/docker"},
                   cwd=rig.work)
    assert r.returncode == 3
    assert "DOCKER" in r.stderr


def test_devcheck_container_missing_is_loud(rig, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_stub(bin_dir, "docker", "exit 1")  # binary runs, but inspect fails
    env = {"DOCKER": str(bin_dir / "docker")}
    r = run_script("devcheck.sh", "1", env=env, cwd=rig.work)
    assert r.returncode == 3
    assert "FORGEJO_CONTAINER" in r.stderr or "hyprial-forgejo-forgejo-1" in r.stderr


# ── devcheck end-to-end on stubs: READY path, ⑤ outdir, red path, ③ at use ──

def devcheck_env(rig, tmp_path, tasks_json, outdir=None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    write_stub(bin_dir, "tea", tea_stub_body(rig.head, tasks_json))
    write_stub(bin_dir, "docker", DOCKER_STUB)
    joblog = write_stub(bin_dir, "joblog", "printf '5123 passed in 41s\\n'")
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "DOCKER": str(bin_dir / "docker"),
        "JOBLOG": str(joblog),
    }
    if outdir is not None:
        env["VERIFY_OUTDIR"] = str(outdir)
    return env


EMPTY_TASKS = '{"workflow_runs":[]}'


def test_devcheck_ready_renders_payload_into_verify_outdir(rig, tmp_path):
    outdir = tmp_path / "out"
    outdir.mkdir()
    env = devcheck_env(rig, tmp_path, EMPTY_TASKS, outdir=outdir)
    r = run_script("devcheck.sh", "1", env=env, cwd=rig.work)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-1000:]
    assert "READY" in r.stdout
    payload = outdir / "merge_p1.json"
    coords = outdir / "merge_p1.coords"
    assert payload.exists() and coords.exists()
    data = json.loads(payload.read_text())
    assert data["Do"] == "merge" and data["head_commit_id"] == rig.head
    assert coords.read_text().splitlines() == [rig.head, rig.dev]


def test_devcheck_ready_without_outdir_fails_loud(rig, tmp_path):
    env = devcheck_env(rig, tmp_path, EMPTY_TASKS)  # no VERIFY_OUTDIR
    r = run_script("devcheck.sh", "1", env=env, cwd=rig.work)
    assert r.returncode == 3
    assert "VERIFY_OUTDIR" in r.stderr
    assert "READY" in r.stdout  # the verdict was READY; only the render was refused


def test_devcheck_readable_red_is_cannot_certify_rc1(rig, tmp_path):
    tasks = json.dumps({"workflow_runs": [
        {"id": 100, "name": "unit", "status": "failure",
         "head_sha": rig.head, "run_number": 5},
    ]})
    outdir = tmp_path / "out"
    outdir.mkdir()
    env = devcheck_env(rig, tmp_path, tasks, outdir=outdir)
    bin_dir = tmp_path / "bin"
    write_stub(bin_dir, "joblog", "printf '10 passed, 3 failed in 9s\\n'")
    r = run_script("devcheck.sh", "1", env=env, cwd=rig.work)
    # the printed verdict and the exit code must agree (F1: verdict no but rc 0)
    assert "CANNOT CERTIFY" in r.stdout
    assert r.returncode == 1, r.stdout[-1500:]
    assert not (outdir / "merge_p1.json").exists()  # a stale payload must never be rendered


def test_devcheck_joblog_missing_at_point_of_use(rig, tmp_path):
    tasks = json.dumps({"workflow_runs": [
        {"id": 100, "name": "unit", "status": "failure",
         "head_sha": rig.head, "run_number": 5},
    ]})
    env = devcheck_env(rig, tmp_path, tasks)
    env["JOBLOG"] = "/nonexistent/joblog"  # ③ removed at the moment it is needed
    r = run_script("devcheck.sh", "1", env=env, cwd=rig.work)
    assert r.returncode == 3
    assert "JOBLOG" in r.stderr


# ── premerge: the all-yes branch must be REACHABLE (④ marker regression) ──

def premerge_rig(rig, tmp_path):
    outdir = tmp_path / "out"
    outdir.mkdir(exist_ok=True)
    (outdir / "merge_p1.json").write_text(json.dumps({
        "Do": "merge", "head_commit_id": rig.head,
        "MergeTitleField": "t", "MergeMessageField": "m",
    }))
    (outdir / "merge_p1.coords").write_text(f"{rig.head}\n{rig.dev}\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    write_stub(bin_dir, "tea", tea_stub_body(rig.head, EMPTY_TASKS))
    return {"PATH": f"{bin_dir}:{os.environ['PATH']}",
            "VERIFY_OUTDIR": str(outdir)}


def test_premerge_all_yes_reaches_rc0(rig, tmp_path):
    r = run_script("premerge.sh", "1", env=premerge_rig(rig, tmp_path), cwd=rig.work)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "⚠️由③决定" in r.stdout  # the marker survives — inside the KEY
    assert "达成" in r.stdout


def test_premerge_stale_dev_is_cannot_certify_rc1(rig, tmp_path):
    env = premerge_rig(rig, tmp_path)
    stale = (Path(env["VERIFY_OUTDIR"]) / "merge_p1.coords")
    stale.write_text(f"{rig.head}\n{'0' * 40}\n")  # dev moved since render
    r = run_script("premerge.sh", "1", env=env, cwd=rig.work)
    assert r.returncode == 1
    assert "CANNOT CERTIFY" in r.stdout


def test_premerge_outdir_unset_is_loud(rig, tmp_path):
    r = run_script("premerge.sh", "1", cwd=rig.work)
    assert r.returncode == 3
    assert "VERIFY_OUTDIR" in r.stderr


# ── onbranch via its file seam: FOUND / NO-OCCURRENCE / empty window ──

def onbranch_tasks(rig, runs):
    f = Path(rig.work) / "tasks.json"
    f.write_text(json.dumps({"workflow_runs": runs}))
    return {"ONBRANCH_TASKS_FILE": str(f)}


def test_onbranch_found_and_no_occurrence(rig):
    runs = [
        {"id": 1, "name": "unit", "status": "success", "run_number": 1,
         "head_sha": rig.dev},
        {"id": 2, "name": "unit", "status": "success", "run_number": 2,
         "head_sha": rig.dev},
        {"id": 3, "name": "gui", "status": "failure", "run_number": 1,
         "head_sha": rig.dev},
    ]
    env = onbranch_tasks(rig, runs)
    r = run_script("onbranch.sh", "gui", env=env, cwd=rig.work)
    assert r.returncode == 0 and "FOUND — 1 run(s), 1 on-branch" in r.stdout
    assert "CONTROL" in r.stdout  # the filter's positive control fired
    r2 = run_script("onbranch.sh", "nonexistent-job", env=env, cwd=rig.work)
    assert r2.returncode == 1 and "NO-OCCURRENCE" in r2.stdout


def test_onbranch_empty_window_cannot_certify(rig):
    env = onbranch_tasks(rig, [])
    r = run_script("onbranch.sh", "unit", env=env, cwd=rig.work)
    assert r.returncode == 3 and "CANNOT CERTIFY" in r.stdout


# ── intggate: containment / continuity / tree, with and without a gap ──

def intggate_env(rig, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    write_stub(bin_dir, "tea", tea_stub_body(rig.intg, EMPTY_TASKS, pr=2))
    return {"PATH": f"{bin_dir}:{os.environ['PATH']}"}


@pytest.fixture
def intg_rig(rig):
    # old intg head sits on dev; the new intg head merges feature ON TOP of it,
    # so continuity (old intg is an ancestor) and containment (feature inside) both hold.
    rig.old_intg = rig.commit("c.txt", "old intg\n", "oldintg", parent="dev")
    git("checkout", "-q", "-b", "intg", "oldintg", cwd=rig.work)
    git("merge", "-q", "--no-ff", "-m", "fold feature", "feature", cwd=rig.work)
    rig.intg = git("rev-parse", "HEAD", cwd=rig.work)
    git("push", "-q", "origin", "intg:intg", cwd=rig.work)
    rig.outside = rig.commit("e.txt", "outside\n", "outside", parent="dev")
    return rig


def test_intggate_all_inside_rc0(intg_rig, tmp_path):
    env = intggate_env(intg_rig, tmp_path)
    env["OLD_INTG"] = intg_rig.old_intg
    r = run_script("intggate.sh", "2", "feature", env=env, cwd=intg_rig.work)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SELF-TEST" in r.stdout


def test_intggate_missing_branch_head_rc1(intg_rig, tmp_path):
    env = intggate_env(intg_rig, tmp_path)
    env["OLD_INTG"] = intg_rig.old_intg
    r = run_script("intggate.sh", "2", "feature", "outside", env=env,
                   cwd=intg_rig.work)
    assert r.returncode == 1
    assert "is NOT inside the integration head" in r.stdout


# ── tagaccept end-to-end on stubs: tag moved, green carries its summary ──

def test_tagaccept_happy_path_and_docker_note(rig, tmp_path):
    git("tag", "dev", rig.dev, cwd=rig.work)
    git("push", "-q", "origin", "refs/tags/dev", cwd=rig.work)
    tasks = json.dumps({"workflow_runs": [
        {"id": 5, "name": "unit", "status": "success", "run_number": 9,
         "head_sha": rig.dev},
    ]})
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    write_stub(bin_dir, "tea", tea_stub_body(rig.head, tasks))
    joblog = write_stub(bin_dir, "joblog", "printf '5123 passed in 41s\\n'")
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}",
           "JOBLOG": str(joblog), "BEFORE_ID": "0",
           "DOCKER": "/nonexistent/docker"}  # ④: note line must NAME the gap
    r = run_script("tagaccept.sh", rig.dev, env=env, cwd=rig.work)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "达成" in r.stdout
    assert "guard-log line count UNREADABLE" in r.stdout
    assert "DOCKER" in r.stdout


# ── postmerge end-to-end on stubs: the stale-base shape is named ──

def test_postmerge_happy_and_stale_base(rig, tmp_path):
    # a merge commit M with parents (pre-dev, head); tags/dev contains it
    git("checkout", "-q", "dev", cwd=rig.work)
    git("merge", "-q", "--no-ff", "-m", "t", "feature", cwd=rig.work)
    merge = git("rev-parse", "HEAD", cwd=rig.work)
    git("push", "-q", "origin", "dev", cwd=rig.work)
    git("tag", "-f", "dev", merge, cwd=rig.work)
    git("push", "-q", "-f", "origin", "refs/tags/dev", cwd=rig.work)
    tree = git("rev-parse", "HEAD^{tree}", cwd=rig.work)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    write_stub(bin_dir, "tea", tea_stub_body(rig.head, EMPTY_TASKS, title="t"))
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}
    r = run_script("postmerge.sh", "1", merge, rig.dev, tree, env=env, cwd=rig.work)
    assert r.returncode == 0, r.stdout + r.stderr
    # stale base: claim the pre-merge dev head was something else
    r2 = run_script("postmerge.sh", "1", merge, "0" * 40, tree, env=env,
                    cwd=rig.work)
    assert r2.returncode == 3  # the all-zero object is missing => unreadable, not "no"
    other = rig.commit("z.txt", "other\n", "other", parent="dev~1")
    r3 = run_script("postmerge.sh", "1", merge, other, tree, env=env, cwd=rig.work)
    assert r3.returncode == 1
    assert "FIRST-PARENT OFF" in r3.stdout
