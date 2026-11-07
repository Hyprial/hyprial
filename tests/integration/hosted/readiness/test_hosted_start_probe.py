"""AT04 卡⑥：old 树首启诊断探针（criteria-v3.3 §3–§5、§9 自证）。

探针在被测解释器里随 BOOTSTRAP 运行，只用 stdlib；分析函数在 _start_probe.py。
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap
import time
from typing import Any

import pytest

SCENARIOS = Path(__file__).resolve().parents[4] / 'contract/e2e'
RUNNER = SCENARIOS / "run-host-invite.py"
LEDGER = SCENARIOS / "run-capability-ledger.py"
ANALYSIS = SCENARIOS / "_start_probe.py"


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def runner() -> Any:
    return _load(RUNNER, "probe_runner")


@pytest.fixture(scope="module")
def sp() -> Any:
    return _load(ANALYSIS, "probe_analysis")


def _run_probe(runner: Any, tmp_path: Path, body: str, *, extra_env: dict[str, str] | None = None,
               pythonpath: Path | None = None, timeout: float = 30) -> tuple[subprocess.CompletedProcess, Path]:
    """在干净子进程里执行 PROBE + body，返回进程结果与探针文件路径。"""
    probe = tmp_path / "probe.jsonl"
    env = {k: v for k, v in os.environ.items() if not k.startswith("AT01_")}
    env["AT01_PROBE_EVIDENCE"] = str(probe)
    if pythonpath is not None:
        env["PYTHONPATH"] = str(pythonpath)
    env.update(extra_env or {})
    code = runner.PROBE + "\n" + textwrap.dedent(body)
    done = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=timeout)
    return done, probe


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------- BOOTSTRAP 接线

def test_bootstrap_starts_with_probe_and_keeps_at02_literal(runner: Any) -> None:
    assert runner.BOOTSTRAP.startswith(runner.PROBE)
    assert runner.BOOTSTRAP.count("'identity.impl.agents.registry')") == 1
    assert "'identity.impl.agents.registry')" not in runner.PROBE
    assert "_at01_note('imports-done')" in runner.BOOTSTRAP
    assert runner.BOOTSTRAP.index("_at01_note('cli-main')") < runner.BOOTSTRAP.index("hyprial.cli.main()")


def test_probe_uses_only_stdlib(runner: Any) -> None:
    import ast
    tree = ast.parse(runner.PROBE)
    names = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
             for alias in node.names}
    names |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert names <= set(sys.stdlib_module_names), names - set(sys.stdlib_module_names)
    assert "hyprial" not in runner.PROBE


def test_capability_ledger_bootstrap_keeps_probe() -> None:
    ledger = _load(LEDGER, "probe_ledger")
    assert ledger.base.BOOTSTRAP.startswith(ledger.base.PROBE)
    assert "'identity.impl.agents.runtime.grants')" in ledger.base.BOOTSTRAP


def test_hosted_upgrade_path_installs_probe() -> None:
    """评审 59b38336 (2)：hosted-upgrade 经 run-hosted-start 加载同一份 run-host-invite。"""
    upgrade = _load(SCENARIOS / "run-hosted-upgrade.py", "probe_upgrade")
    base = upgrade.base
    assert base.BOOTSTRAP.startswith(base.PROBE)
    assert issubclass(upgrade.RollbackScenario, base.Scenario)
    source = Path(base.__file__).read_text()
    assert "AT01_PROBE_EVIDENCE" in source and "ready-mark.json" in source


# ---------------------------------------------------------------- §9-1 钩子真实被调用

def test_hooks_fire_on_real_imports(runner: Any, tmp_path: Path) -> None:
    done, probe = _run_probe(runner, tmp_path, """
        import importlib
        for name in ('json.tool', 'email.mime.text', 'xml.dom.minidom', 'http.cookiejar', 'difflib',
                     'csv', 'tarfile', 'zipfile', 'wave', 'mailbox'):
            importlib.import_module(name)
        import importlib.metadata as md
        md.entry_points()
    """)
    assert done.returncode == 0, done.stderr
    records = _records(probe)
    install = records[0]
    assert install["kind"] == "install" and install["hooks"] == {"get_data": True, "read_text": True}
    reads = [r for rec in records if rec["kind"] == "sample" for r in rec["reads"]]
    by_hook = {hook: sum(1 for r in reads if r[6] == hook) for hook in ("get_data", "read_text")}
    assert by_hook["get_data"] >= 10, by_hook
    assert by_hook["read_text"] >= 1, by_hook
    assert records[-1].get("final") is True


def test_read_records_carry_wall_cpu_category_bytes(runner: Any, tmp_path: Path) -> None:
    done, probe = _run_probe(runner, tmp_path, "import difflib, csv, tarfile\n")
    assert done.returncode == 0, done.stderr
    reads = [r for rec in _records(probe) if rec["kind"] == "sample" for r in rec["reads"]]
    assert reads
    for start, end, cpu, category, nbytes, path, hook in reads:
        assert end >= start and cpu >= 0 and nbytes >= 0
        assert category in {"venv", "src", "stdlib", "other"}
        assert hook in {"get_data", "read_text"}
    assert {r[3] for r in reads} & {"stdlib"}


def test_samples_carry_usage_and_loadavg_first(runner: Any, tmp_path: Path) -> None:
    done, probe = _run_probe(runner, tmp_path, "import time\ntime.sleep(1.3)\n")
    assert done.returncode == 0, done.stderr
    records = _records(probe)
    assert isinstance(records[0]["loadavg"], list) and len(records[0]["loadavg"]) == 3
    samples = [r for r in records if r["kind"] == "sample"]
    # criteria §3：P2 第一条额外记录 os.getloadavg()，其余不带
    assert isinstance(samples[0]["loadavg"], list) and len(samples[0]["loadavg"]) == 3
    assert all("loadavg" not in r for r in samples[1:])
    assert len(samples) >= 3
    for rec in samples:
        assert set(rec["usage"]) == {"utime", "stime", "minflt", "majflt", "inblock", "nvcsw", "nivcsw"}
        assert rec["utc"].endswith("+00:00")
    monos = [r["mono"] for r in samples]
    assert monos == sorted(monos)


def test_probe_is_noop_without_env(runner: Any, tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("AT01_")}
    code = runner.PROBE + "\nimport difflib\nprint(_at01_mark is None)\n_at01_note('x')\n"
    done = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0 and done.stdout.strip() == "True", done.stderr


def test_marks_are_written(runner: Any, tmp_path: Path) -> None:
    done, probe = _run_probe(runner, tmp_path, "_at01_note('imports-done')\n_at01_note('cli-main')\n")
    assert done.returncode == 0, done.stderr
    marks = [r["name"] for r in _records(probe) if r["kind"] == "mark"]
    assert marks == ["imports-done", "cli-main"]


def test_sampler_stops_at_cap(runner: Any, tmp_path: Path) -> None:
    done, probe = _run_probe(runner, tmp_path, "import time\ntime.sleep(2.0)\n",
                             extra_env={"AT01_PROBE_MAX_SECONDS": "0.6"})
    assert done.returncode == 0, done.stderr
    samples = [r for r in _records(probe) if r["kind"] == "sample" and not r.get("final")]
    assert samples and samples[-1]["mono"] - _records(probe)[0]["mono"] <= 1.2


# ---------------------------------------------------------------- §9-4 SIGKILL

def test_sigkill_leaves_parsable_file_close_to_kill(runner: Any, tmp_path: Path, sp: Any) -> None:
    probe = tmp_path / "probe.jsonl"
    env = {k: v for k, v in os.environ.items() if not k.startswith("AT01_")}
    env["AT01_PROBE_EVIDENCE"] = str(probe)
    code = runner.PROBE + "\nimport time\nwhile True:\n    import difflib\n    time.sleep(0.05)\n"
    process = subprocess.Popen([sys.executable, "-c", code], env=env)
    try:
        time.sleep(2.2)
        killed_at = time.monotonic()
        process.send_signal(signal.SIGKILL)
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
    parsed = sp.load(probe)
    assert parsed["problems"] == []
    assert parsed["install"] is not None and len(parsed["samples"]) >= 3
    assert 0 <= killed_at - parsed["samples"][-1]["mono"] <= 1.0


def test_load_tolerates_torn_last_line(sp: Any, tmp_path: Path) -> None:
    path = tmp_path / "p.jsonl"
    path.write_text(json.dumps({"kind": "install", "mono": 1.0, "utc": "x", "pid": 1, "loadavg": [1, 1, 1],
                                "usage": {}, "hooks": {}}) + "\n" + '{"kind": "sam')
    parsed = sp.load(path)
    assert parsed["install"]["mono"] == 1.0 and parsed["torn"] is True and parsed["problems"] == []


# ---------------------------------------------------------------- §9-5 探针异常不影响启动

SITE_FAULTS = {
    "getrusage": "import resource\ndef _boom(*a, **k):\n    raise OSError('injected')\nresource.getrusage = _boom\n",
    "patch": "import importlib._bootstrap_external as b\ndel b.FileLoader\n",
}


@pytest.mark.parametrize("fault", ["getrusage", "patch", "write"])
def test_probe_faults_never_break_the_program(runner: Any, tmp_path: Path, fault: str) -> None:
    site = tmp_path / "site"
    site.mkdir()
    extra: dict[str, str] = {}
    if fault in SITE_FAULTS:
        (site / "sitecustomize.py").write_text(SITE_FAULTS[fault])
    else:
        extra["AT01_PROBE_EVIDENCE"] = str(tmp_path / "missing-dir" / "probe.jsonl")
    done, _ = _run_probe(runner, tmp_path, """
        import difflib, csv
        _at01_note('imports-done')
        print('PROGRAM-OK')
    """, extra_env=extra, pythonpath=site)
    assert done.returncode == 0, done.stderr
    assert "PROGRAM-OK" in done.stdout


def test_hook_passes_through_loader_errors(runner: Any, tmp_path: Path) -> None:
    done, _ = _run_probe(runner, tmp_path, """
        import importlib._bootstrap_external as b
        loader = b.SourceFileLoader('nope', '/definitely/not/here.py')
        try:
            loader.get_data('/definitely/not/here.py')
        except FileNotFoundError:
            print('PASSED-THROUGH')
    """)
    assert done.returncode == 0 and "PASSED-THROUGH" in done.stdout, done.stderr


# ---------------------------------------------------------------- §9-9 thread_time 与跨进程 monotonic

def test_cross_process_monotonic_is_comparable(tmp_path: Path) -> None:
    done = subprocess.run([sys.executable, "-c", "import time; print(repr(time.monotonic()))"],
                          capture_output=True, text=True, timeout=30)
    parent = time.monotonic()
    child = float(done.stdout)
    assert 0 <= parent - child <= 0.5


def test_thread_time_moves_with_cpu_in_read_records(runner: Any, tmp_path: Path) -> None:
    done, probe = _run_probe(runner, tmp_path, "import difflib, tarfile, zipfile, mailbox\n")
    assert done.returncode == 0, done.stderr
    reads = [r for rec in _records(probe) if rec["kind"] == "sample" for r in rec["reads"]]
    assert all(r[2] <= (r[1] - r[0]) + 0.01 for r in reads)


# ---------------------------------------------------------------- 父进程：ready-mark 与环境

def test_scenario_passes_probe_env_and_writes_ready_mark(runner: Any, tmp_path: Path, monkeypatch: Any) -> None:
    written: dict[str, Any] = {}
    real = runner.write_json

    def spy(path: Path, value: Any) -> None:
        written[Path(path).name] = value
        real(path, value)

    monkeypatch.setattr(runner, "write_json", spy)
    scenario = object.__new__(runner.Scenario)
    scenario.root = tmp_path
    scenario.env = {"AT01_SOURCE": "/tree"}
    attempt = {"label": "daemon-1", "status": "ready", "budgetSeconds": 45.0, "import_": {}}
    runner.Scenario._write_attempt(scenario, "daemon-1", attempt, 10.0, 12.5,
                                   tmp_path / "t.json", tmp_path / "i.json")
    mark = json.loads((tmp_path / "daemon-1-ready-mark.json").read_text())
    assert mark == {"status": "ready", "monotonic": 12.5}


def test_ready_mark_written_for_timeout_too(runner: Any, tmp_path: Path) -> None:
    scenario = object.__new__(runner.Scenario)
    scenario.root = tmp_path
    scenario.env = {}
    attempt = {"label": "daemon-2", "status": "timeout", "budgetSeconds": 45.0, "import_": {}}
    runner.Scenario._write_attempt(scenario, "daemon-2", attempt, 0.0, 45.1, tmp_path / "t", tmp_path / "i")
    assert json.loads((tmp_path / "daemon-2-ready-mark.json").read_text()) == {"status": "timeout", "monotonic": 45.1}


def test_ready_mark_write_failure_is_swallowed(runner: Any, tmp_path: Path) -> None:
    scenario = object.__new__(runner.Scenario)
    scenario.root = tmp_path / "gone"
    scenario.env = {}
    attempt = {"label": "daemon-1", "status": "ready", "budgetSeconds": 45.0, "import_": {}}
    runner.Scenario._write_attempt(scenario, "daemon-1", attempt, 0.0, 1.0, tmp_path / "t", tmp_path / "i")


# ---------------------------------------------------------------- 分析：summarize

def _probe_file(tmp_path: Path, *, install: float = 100.0, samples: list[dict], name: str = "p.jsonl",
                loadavg: float = 10.0) -> Path:
    lines = [{"kind": "install", "mono": install, "utc": "2026-09-29T00:00:00+00:00", "pid": 1,
              "loadavg": [loadavg, loadavg, loadavg], "hooks": {"get_data": True, "read_text": True},
              "usage": {"utime": 0.0, "stime": 0.0, "minflt": 0, "majflt": 0, "inblock": 0, "nvcsw": 0, "nivcsw": 0}}]
    lines += samples
    path = tmp_path / name
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


def _sample(mono: float, cpu: float, nivcsw: int, reads: list[list] | None = None, inflight: list | None = None) -> dict:
    return {"kind": "sample", "mono": mono, "utc": "2026-09-29T00:00:00+00:00",
            "usage": {"utime": cpu, "stime": 0.0, "minflt": 0, "majflt": 0, "inblock": 0, "nvcsw": 0, "nivcsw": nivcsw},
            "reads": reads or [], "inflight": inflight or [], "hookCalls": {}}


def _steady(tmp_path: Path, *, n: int = 40, step: float = 0.5, cpu_rate: float = 0.1, niv_rate: float = 2.0,
            reads_at: dict[int, float] | None = None, niv_at: dict[int, float] | None = None, name: str = "p.jsonl",
            loadavg: float = 10.0) -> Path:
    """install=100，每 step 秒一个样本；reads_at[i]=该区间内读耗时（在区间开头）。"""
    samples = []
    niv = 0.0
    for i in range(1, n + 1):
        t0, t1 = 100 + (i - 1) * step, 100 + i * step
        niv += (niv_at or {}).get(i, niv_rate) * step
        reads = []
        if reads_at and i in reads_at:
            reads.append([t0, t0 + reads_at[i], 0.0, "venv", 100, None, "get_data"])
        samples.append(_sample(t1, cpu_rate * (t1 - 100), int(niv), reads))
    return _probe_file(tmp_path, samples=samples, name=name, loadavg=loadavg)


def test_summarize_ready_end(sp: Any, tmp_path: Path) -> None:
    path = _steady(tmp_path, n=40, reads_at={3: 0.4})
    summary = sp.summarize(sp.load(path), end_mono=118.0)
    assert summary["endSource"] == "ready-mark"
    assert summary["wall"] == pytest.approx(18.0)
    assert summary["cpu"] == pytest.approx(1.8)
    assert summary["G"] == pytest.approx(16.2)
    assert summary["readSeconds"] == pytest.approx(0.4)
    assert summary["loadavg1"] == 10.0


def test_summarize_without_end_uses_last_sample(sp: Any, tmp_path: Path) -> None:
    summary = sp.summarize(sp.load(_steady(tmp_path, n=10)), end_mono=None)
    assert summary["endSource"] == "last-sample" and summary["wall"] == pytest.approx(5.0)


def test_summarize_counts_inflight_read_until_end(sp: Any, tmp_path: Path) -> None:
    samples = [_sample(100.5, 0.05, 1), _sample(101.0, 0.1, 2, inflight=[[100.6, "venv", "get_data"]])]
    summary = sp.summarize(sp.load(_probe_file(tmp_path, samples=samples)), end_mono=None)
    assert summary["readSeconds"] == pytest.approx(0.4)


def test_summarize_missing_file_is_note_not_crash(sp: Any, tmp_path: Path) -> None:
    summary = sp.summarize(sp.load(tmp_path / "nope.jsonl"), end_mono=1.0)
    assert summary["installed"] is False and summary["note"]


# ---------------------------------------------------------------- §9-7 / §9-8 划分

# 每 CPU 秒口径（criteria-v3.3 §5）：静默慢样本 2/s÷0.02 CPU/s=100，饥饿样本 50/s÷0.02=2500
P95 = 150.0

def test_control_p95_needs_five_starts_and_twenty_quiet_intervals(sp: Any, tmp_path: Path) -> None:
    controls = [sp.load(_steady(tmp_path, n=5, name=f"c{i}.jsonl")) for i in range(4)]
    assert sp.control_p95(controls)["ok"] is False
    controls.append(sp.load(_steady(tmp_path, n=5, name="c4.jsonl")))
    result = sp.control_p95(controls)
    assert result["ok"] is True and result["noReadIntervals"] >= 20
    assert result["p95"] == pytest.approx(20.0, abs=21.0)  # 2/s ÷ 0.1 CPU/s


def test_partition_io_dominant(sp: Any, tmp_path: Path) -> None:
    reads = {i: 0.49 for i in range(1, 36)}
    slow = sp.load(_steady(tmp_path, n=40, cpu_rate=0.02, reads_at=reads))
    result = sp.partition(slow, end_mono=120.0, p95=P95, elapsed=20.0)
    assert result["scored"] is True
    assert result["s_io"] >= 0.6 and result["label"] == "H-IO"
    for key in ("", "_R"):
        assert result["s1" + key] + result["s_io" + key] <= 1.0 + 1e-9
    assert result["residual"] >= -1e-9


def test_partition_starvation_dominant(sp: Any, tmp_path: Path) -> None:
    slow = sp.load(_steady(tmp_path, n=40, cpu_rate=0.02, niv_rate=50.0))
    result = sp.partition(slow, end_mono=120.0, p95=P95, elapsed=20.0)
    assert result["label"] == "H1" and result["s1"] >= 0.6


def test_partition_reads_during_starvation_are_not_io(sp: Any, tmp_path: Path) -> None:
    """§9-8：读耗时大但全在争用区间 → 主判定不能是 H-IO；R 与 R_quiet 不一致 → 混合。"""
    reads = {i: 0.49 for i in range(1, 36)}
    slow = sp.load(_steady(tmp_path, n=40, cpu_rate=0.02, niv_rate=50.0, reads_at=reads))
    result = sp.partition(slow, end_mono=120.0, p95=P95, elapsed=20.0)
    assert result["R"] > 0.8 and result["R_quiet"] == pytest.approx(0.0)
    assert result["dominant"] != "H-IO"
    assert result["dominant_R"] == "H-IO"
    assert result["label"] == "mixed"


def test_partition_r_above_one_is_unscored(sp: Any, tmp_path: Path) -> None:
    samples = [_sample(100.5, 0.4, 1, [[100.0, 100.5, 0, "venv", 1, None, "get_data"]] * 3)]
    result = sp.partition(sp.load(_probe_file(tmp_path, samples=samples)), end_mono=100.5, p95=P95, elapsed=0.5)
    assert result["scored"] is False


@pytest.mark.parametrize("cpu_rate,elapsed", [(0.95, 20.0), (0.02, 400.0)])
def test_partition_gap_floor_is_cpu_dominant(sp: Any, tmp_path: Path, cpu_rate: float, elapsed: float) -> None:
    slow = sp.load(_steady(tmp_path, n=40, cpu_rate=cpu_rate))
    result = sp.partition(slow, end_mono=120.0, p95=P95, elapsed=elapsed)
    assert result["scored"] is False and result["reason"] == "cpu-dominant"


def test_partition_without_control_is_unscored(sp: Any, tmp_path: Path) -> None:
    slow = sp.load(_steady(tmp_path, n=40, cpu_rate=0.02))
    result = sp.partition(slow, end_mono=120.0, p95=None, elapsed=20.0)
    assert result["scored"] is False and result["reason"] == "no-control"


def test_partition_all_intervals_have_reads(sp: Any, tmp_path: Path) -> None:
    reads = {i: 0.2 for i in range(1, 41)}
    result = sp.partition(sp.load(_steady(tmp_path, n=40, cpu_rate=0.02, reads_at=reads)),
                          end_mono=120.0, p95=P95, elapsed=20.0)
    assert result["scored"] is True and result["s1"] == 0.0 and result["s1_R"] == 0.0


def test_dominant_rule(sp: Any) -> None:
    assert sp.dominant(0.7, 0.2) == "H1"
    assert sp.dominant(0.2, 0.7) == "H-IO"
    assert sp.dominant(0.6, 0.45) == "mixed"
    assert sp.dominant(0.3, 0.3) == "mixed"


# ---------------------------------------------------------------- §7 判定

def _scored(label: str, task: int, utc: str) -> dict:
    return {"scored": True, "label": label, "taskId": task, "tree": "old", "createdUtc": utc,
            "s1": 0.8 if label == "H1" else 0.1, "s_io": 0.8 if label == "H-IO" else 0.1}


def test_verdict_decided_when_three_spread(sp: Any) -> None:
    samples = [_scored("H-IO", 1, "2026-09-30T00:00:00+00:00"), _scored("H-IO", 2, "2026-09-30T07:00:00+00:00"),
               _scored("H-IO", 3, "2026-09-30T14:00:00+00:00")]
    verdict = sp.mechanism_verdict(samples)
    assert verdict["outcome"] == "decided" and verdict["mechanism"] == "H-IO"
    assert "H1" in verdict["refuted"]


def test_verdict_concentrated_is_observational(sp: Any) -> None:
    samples = [_scored("H-IO", i, f"2026-09-30T03:{i:02d}:00+00:00") for i in range(1, 4)]
    assert sp.mechanism_verdict(samples)["outcome"] == "observational"


def test_verdict_counterexample_blocks(sp: Any) -> None:
    samples = [_scored("H-IO", 1, "2026-09-30T00:00:00+00:00"), _scored("H-IO", 2, "2026-09-30T07:00:00+00:00"),
               _scored("H-IO", 3, "2026-09-30T14:00:00+00:00"), _scored("H1", 4, "2026-09-30T20:00:00+00:00")]
    assert sp.mechanism_verdict(samples)["outcome"] == "undecided"


def test_verdict_unscored_do_not_count(sp: Any) -> None:
    samples = [_scored("H-IO", 1, "2026-09-30T00:00:00+00:00"), _scored("H-IO", 2, "2026-09-30T07:00:00+00:00"),
               {"scored": False, "label": None, "taskId": 3, "tree": "old", "createdUtc": "2026-09-30T14:00:00+00:00"}]
    assert sp.mechanism_verdict(samples)["outcome"] == "undecided"


def test_verdict_one_per_job_earliest(sp: Any) -> None:
    samples = [_scored("H-IO", 1, "2026-09-30T00:00:00+00:00"), dict(_scored("H1", 1, "2026-09-30T00:05:00+00:00")),
               _scored("H-IO", 2, "2026-09-30T07:00:00+00:00"), _scored("H-IO", 3, "2026-09-30T14:00:00+00:00")]
    verdict = sp.mechanism_verdict(samples)
    assert verdict["outcome"] == "decided" and verdict["counted"] == 3


# ---------------------------------------------------------------- suite 观察字段

def test_suite_row_gets_probe_summary_without_touching_complete(tmp_path: Path) -> None:
    suite = _load(SCENARIOS / "run-hosted-suite.py", "probe_suite")
    row = suite.probe_observation(tmp_path, "hosted-upgrade/daemon-1")
    assert row["installed"] is False and row["note"]
    (tmp_path / "hosted-upgrade").mkdir()
    _steady(tmp_path / "hosted-upgrade", n=10, name="daemon-1-probe.jsonl")
    (tmp_path / "hosted-upgrade" / "daemon-1-ready-mark.json").write_text(json.dumps({"status": "ready", "monotonic": 104.0}))
    row = suite.probe_observation(tmp_path, "hosted-upgrade/daemon-1")
    assert row["installed"] is True and row["endSource"] == "ready-mark" and row["wall"] == pytest.approx(4.0)


def test_suite_probe_observation_never_raises(tmp_path: Path, monkeypatch: Any) -> None:
    suite = _load(SCENARIOS / "run-hosted-suite.py", "probe_suite2")
    monkeypatch.setattr(suite, "_start_probe", None)
    row = suite.probe_observation(tmp_path, "x/daemon-1")
    assert row["installed"] is False and "unavailable" in row["note"]


# ---------------------------------------------------------------- 端到端（真实子进程 + fake 树）

@pytest.fixture(scope="module")
def timing_helpers() -> Any:
    return _load(Path(__file__).resolve().parents[4] / 'tests/integration/hosted/readiness/test_hosted_readiness_timing.py', "probe_timing_helpers")


def _start_fake(runner: Any, helpers: Any, tmp_path: Path, monkeypatch: Any, *, probe_on: bool = True) -> Any:
    monkeypatch.setattr(runner.os, "nice", lambda _increment: None)
    # 导入放慢 1.2s，保证就绪前至少有两条采样
    tree = helpers._fake_tree(tmp_path / "tree", extra={"identity/impl/agents/registry.py": "import time\ntime.sleep(1.2)\nVALUE = 1\n"})
    scenario = helpers._scenario(runner, tmp_path, tree)
    monkeypatch.setattr(runner, "ping", helpers._ready_after(runner, scenario, scenario.root / "daemon-1-timing.json"))
    if not probe_on:
        switch = "os.environ.get('AT01_PROBE_EVIDENCE')"
        assert runner.BOOTSTRAP.count(switch) == 1
        monkeypatch.setattr(runner, "BOOTSTRAP", runner.BOOTSTRAP.replace(switch, "None"))
    try:
        state = scenario.start()
        time.sleep(0.8)  # 让采样线程至少再写一条
    finally:
        helpers._stop(scenario.process)
    return scenario, state


def test_real_start_writes_probe_marks_and_ready_mark(tmp_path: Path, monkeypatch: Any, timing_helpers: Any) -> None:
    runner = _load(RUNNER, "probe_e2e")
    scenario, state = _start_fake(runner, timing_helpers, tmp_path, monkeypatch)
    assert state["running"] is True
    probe = scenario.root / "daemon-1-probe.jsonl"
    records = _records(probe)
    assert records[0]["kind"] == "install" and records[0]["hooks"]["get_data"] is True
    assert [r["name"] for r in records if r["kind"] == "mark"] == ["imports-done", "cli-main"]
    mark = json.loads((scenario.root / "daemon-1-ready-mark.json").read_text())
    assert mark["status"] == "ready" and mark["monotonic"] >= records[0]["mono"]
    suite = _load(SCENARIOS / "run-hosted-suite.py", "probe_e2e_suite")
    summary = suite.probe_observation(tmp_path, "scenario-evidence/daemon-1")
    assert summary["installed"] is True and summary["endSource"] == "ready-mark"
    assert summary["G"] is not None and summary["wall"] > 0


def test_probe_on_off_same_scenario_outcome(tmp_path: Path, monkeypatch: Any, timing_helpers: Any) -> None:
    """§9-6：开/关探针，同一场景的就绪判定与 attempt 结论不变。"""
    outcomes = []
    for probe_on in (True, False):
        runner = _load(RUNNER, f"probe_onoff_{probe_on}")
        base = tmp_path / str(probe_on)
        base.mkdir()
        scenario, state = _start_fake(runner, timing_helpers, base, monkeypatch, probe_on=probe_on)
        record = json.loads((scenario.root / "daemon-1-attempt.json").read_text())
        outcomes.append((state["running"], record["status"], (scenario.root / "daemon-1-probe.jsonl").exists()))
    assert outcomes[0][:2] == outcomes[1][:2] == (True, "ready")
    assert outcomes[0][2] is True and outcomes[1][2] is False


# ---------------------------------------------------------------- §9-3 CPU 竞争时 nivcsw 上升

BUSY = "import time\nend = time.monotonic() + {secs}\nwhile time.monotonic() < end:\n    pass\n"


def _nivcsw_rates(runner: Any, tmp_path: Path, sp: Any, name: str) -> dict[str, float]:
    """一次 1.5s 忙循环的每 CPU 秒与每墙钟秒 nivcsw，以及起始 loadavg。"""
    (tmp_path / name).mkdir()
    done, probe = _run_probe(runner, tmp_path / name, BUSY.format(secs=1.5))
    assert done.returncode == 0, done.stderr
    parsed = sp.load(probe)
    first, last = parsed["install"], parsed["samples"][-1]
    switches = last["usage"]["nivcsw"] - first["usage"]["nivcsw"]
    cpu = sum(last["usage"][k] - first["usage"][k] for k in ("utime", "stime"))
    return {"perCpu": switches / max(cpu, 0.001), "perWall": switches / (last["mono"] - first["mono"]),
            "load1": first["loadavg"][0]}


@pytest.mark.skipif(sys.platform != "darwin",
                    reason="criteria-v3.3 §5 的速率口径针对 macOS 调度语义（HQ hosted daemon 是原生 macOS 进程）")
@pytest.mark.skipif(bool(os.environ.get("PYTEST_XDIST_WORKER")),
                    reason="needs an uncontended CPU baseline; xdist workers contend "
                           "(§9-3 用 核数×3 个忙等子进程制造竞争，quiet 基线必须在无其他测试进程时测得)")
def test_cpu_contention_raises_nivcsw(runner: Any, tmp_path: Path, sp: Any) -> None:
    """§9-3（v3.2 附注 1 与评审 cdc2359d）：物理自证，不是回归门禁。
    CI（ubuntu）跳过，并行（xdist）也跳过：只在 macOS 上串行、手动运行才有意义，
    因为「无竞争」基线要求没有其他测试进程在抢 CPU。它在 CI 里不守护任何东西。

    1) 5 轮每 CPU 秒比值（有竞争/无竞争）的中位数 ≥ 2；
    2) 每一轮 cpu_ratio > wall_ratio：同一轮内比较两种口径，环境负载的影响相互抵消。
    环境负载只会抬高 quiet 基线，单轮比值因此有左尾，只记录、不单独断言。"""
    import statistics
    rounds = []
    for round_ in range(5):
        quiet = _nivcsw_rates(runner, tmp_path, sp, f"quiet{round_}")
        hogs = [subprocess.Popen([sys.executable, "-c", BUSY.format(secs=3.0)])
                for _ in range((os.cpu_count() or 4) * 3)]
        try:
            time.sleep(0.3)
            contended = _nivcsw_rates(runner, tmp_path, sp, f"contended{round_}")
        finally:
            for hog in hogs:
                hog.kill()
                hog.wait(timeout=10)
        rounds.append({"cpu_ratio": contended["perCpu"] / max(quiet["perCpu"], 1e-9),
                       "wall_ratio": contended["perWall"] / max(quiet["perWall"], 1e-9),
                       "quietLoad1": quiet["load1"], "quietPerCpu": quiet["perCpu"]})
    assert statistics.median(r["cpu_ratio"] for r in rounds) >= 2, rounds
    assert all(r["cpu_ratio"] > r["wall_ratio"] for r in rounds), rounds


def test_rate_is_per_cpu_second_with_floor(sp: Any, tmp_path: Path) -> None:
    """饥饿区间几乎拿不到 CPU：分母取 1ms 下限，不被排除；阻塞睡眠 Δnivcsw=0 → 0。"""
    samples = [_sample(100.5, 0.0, 3), _sample(101.0, 0.0, 3), _sample(101.5, 0.2, 13)]
    probe = sp.load(_probe_file(tmp_path, samples=samples))
    rates = [i["rate"] for i in sp._intervals(probe, 101.5, probe["samples"])]
    assert rates == [pytest.approx(3000.0), 0.0, pytest.approx(50.0)]


def test_partition_reports_floor_raised_intervals(sp: Any, tmp_path: Path) -> None:
    """v3.2 附注 2：ΔCPU < 1ms 且 Δnivcsw ≥ 1 的区间数与其中被判争用的数目要报出。"""
    samples = []
    cpu, niv = 0.0, 0
    for i in range(1, 41):
        starved = i % 4 == 0
        cpu += 0.0 if starved else 0.01
        niv += 2 if starved else 1
        samples.append(_sample(100 + 0.5 * i, cpu, niv))
    result = sp.partition(sp.load(_probe_file(tmp_path, samples=samples)), end_mono=120.0, p95=P95, elapsed=20.0)
    assert result["floorRaisedIntervals"] == 10
    assert result["floorRaisedContended"] == 10


# ---------------------------------------------------------------- 评审 bffa1feb R1：超时终点

def _mark(path: Path, status: str, mono: float) -> Path:
    path.write_text(json.dumps({"status": status, "monotonic": mono}))
    return path


def test_observe_ready_ends_at_ready_mark(sp: Any, tmp_path: Path) -> None:
    probe = _steady(tmp_path, n=40)
    result = sp.observe_start(probe, _mark(tmp_path / "m.json", "ready", 110.0))
    assert result["endSource"] == "ready-mark" and result["wall"] == pytest.approx(10.0)
    assert result["timeoutMono"] is None and result["timeoutDeltaSeconds"] is None


def test_observe_timeout_ends_at_last_sample_and_reports_delta(sp: Any, tmp_path: Path) -> None:
    """§4：超时样本以 SIGKILL 前最后一条采样为终点，并报出它与超时时刻的差值。"""
    probe = _steady(tmp_path, n=40)  # 最后一条 mono=120
    result = sp.observe_start(probe, _mark(tmp_path / "m.json", "timeout", 105.0))
    assert result["endSource"] == "last-sample" and result["wall"] == pytest.approx(20.0)
    assert result["timeoutMono"] == 105.0 and result["timeoutDeltaSeconds"] == pytest.approx(15.0)
    assert result["samples"] == 40  # 诊断期的采样没有被剪掉
    assert result["lastSampleToEndSeconds"] is None  # 差值只由 timeoutDeltaSeconds 表达


def test_observe_without_mark_uses_last_sample(sp: Any, tmp_path: Path) -> None:
    result = sp.observe_start(_steady(tmp_path, n=10), tmp_path / "absent.json")
    assert result["endSource"] == "last-sample" and result["markStatus"] is None


@pytest.mark.parametrize("payload", ['{"status": "ready", "monotonic": true}', "[]", "not json"])
def test_observe_bad_mark_falls_back_without_raising(sp: Any, tmp_path: Path, payload: str) -> None:
    (tmp_path / "m.json").write_text(payload)
    assert sp.observe_start(_steady(tmp_path, n=10), tmp_path / "m.json")["endSource"] == "last-sample"


def test_suite_timeout_row_uses_last_sample(tmp_path: Path) -> None:
    suite = _load(SCENARIOS / "run-hosted-suite.py", "probe_suite_timeout")
    (tmp_path / "hosted-upgrade").mkdir()
    _steady(tmp_path / "hosted-upgrade", n=40, name="daemon-1-probe.jsonl")
    _mark(tmp_path / "hosted-upgrade" / "daemon-1-ready-mark.json", "timeout", 105.0)
    row = suite.probe_observation(tmp_path, "hosted-upgrade/daemon-1")
    assert row["endSource"] == "last-sample" and row["timeoutDeltaSeconds"] == pytest.approx(15.0)


# ---------------------------------------------------------------- 评审 bffa1feb m1：真实慢读

def test_probe_wrapper_times_a_slow_underlying_read(runner: Any, tmp_path: Path, sp: Any) -> None:
    """§9-2：探针安装前把 FileLoader.get_data 换成先 sleep 1.2s 的版本，再装探针；
    该读的 wall≈1.2、thread_time≪wall，且在无争用对照下划分为 H-IO。
    构造上 G ≈ 3×1.2 + 0.6 ≈ 4.2s，离 §4 的 3s 下限留出 ≥1s 余量（评审 cd49d777 v2）。"""
    probe = tmp_path / "probe.jsonl"
    env = {k: v for k, v in os.environ.items() if not k.startswith("AT01_")}
    env["AT01_PROBE_EVIDENCE"] = str(probe)
    pre = textwrap.dedent("""
        import importlib._bootstrap_external as _b, time as _t, os as _os, tempfile as _tf
        _orig = _b.FileLoader.get_data
        def _sleepy(self, path):
            if path.endswith('slowmod.py'):
                _t.sleep(1.2)
            return _orig(self, path)
        _b.FileLoader.get_data = _sleepy
        _dir = _tf.mkdtemp()
        open(_os.path.join(_dir, 'slowmod.py'), 'w').write('VALUE = 1\\n')
    """)
    post = textwrap.dedent("""
        import sys, time
        sys.dont_write_bytecode = True
        sys.path.insert(0, _dir)
        for _ in range(3):
            sys.modules.pop('slowmod', None)
            import importlib; importlib.invalidate_caches()
            import slowmod
        time.sleep(0.6)
    """)
    done = subprocess.run([sys.executable, "-B", "-c", pre + runner.PROBE + post], env=env,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    parsed = sp.load(probe)
    slow = [r for s in parsed["samples"] for r in s["reads"] if r[5] and r[5].endswith("slowmod.py")]
    assert len(slow) == 3
    for start, end, cpu, *_ in slow:
        assert end - start == pytest.approx(1.2, abs=0.15)
        assert cpu < 0.1 * (end - start)
    assert sp.summarize(parsed, end_mono=None)["G"] >= 4.0  # 余量由构造保证
    result = sp.partition(parsed, end_mono=None, p95=1e9, elapsed=None)
    assert result["scored"] is True and result["label"] == "H-IO", result


# ---------------------------------------------------------------- 评审 bffa1feb R2：可复算的报告驱动

def _job_evidence(tmp_path: Path, name: str, starts: dict[str, dict]) -> Path:
    """一个 job 的 evidence 根：readiness.json + 每个 start 的 probe/ready-mark。"""
    root = tmp_path / name
    (root / "hosted-upgrade").mkdir(parents=True)
    rows = []
    for label, spec in starts.items():
        key = f"hosted-upgrade/{label}"
        _steady(root / "hosted-upgrade", name=f"{label}-probe.jsonl", **spec["probe"])
        _mark(root / f"{key}-ready-mark.json", spec.get("status", "ready"), spec["end"])
        rows.append({"key": key, "scenario": "hosted-upgrade", "tree": spec.get("tree", "old"), "label": label,
                     "status": spec.get("status", "ready"), "elapsedSeconds": spec["end"] - 100,
                     "startedUtc": spec["startedUtc"]})
    (root / "readiness.json").write_text(json.dumps({"rows": rows, "complete": True}))
    return root


FAST = {"n": 12, "cpu_rate": 0.9, "niv_rate": 60.0}          # 快启动：G 很小，每 CPU 秒约 67
SLOW_IO = {"n": 40, "cpu_rate": 0.02, "niv_rate": 0.0, "reads_at": {i: 0.49 for i in range(1, 38)}}  # 纯 I/O 阻塞：无非自愿切换


def _manifest(tmp_path: Path, *, landed: str = "2026-09-30T00:00:00+00:00") -> list[dict]:
    jobs = []
    for i in range(6):
        created = f"2026-09-30T{i * 2:02d}:00:00+00:00"
        ev = _job_evidence(tmp_path, f"fast{i}", {"daemon-1": {"probe": FAST, "end": 105.5, "startedUtc": created}})
        jobs.append({"taskId": 100 + i, "createdUtc": created, "evidence": str(ev)})
    for i, hour in enumerate((1, 9, 17)):
        created = f"2026-09-30T{hour:02d}:30:00+00:00"
        ev = _job_evidence(tmp_path, f"slow{i}", {
            "daemon-1": {"probe": SLOW_IO, "end": 120.0, "startedUtc": created},
            "daemon-5": {"probe": SLOW_IO, "end": 120.0, "startedUtc": created.replace(":30:", ":31:")}})
        jobs.append({"taskId": 200 + i, "createdUtc": created, "evidence": str(ev)})
    return jobs


def test_report_decides_io_with_paired_controls(sp: Any, tmp_path: Path) -> None:
    report = sp.mechanism_report(_manifest(tmp_path), probe_landed_utc="2026-09-30T00:00:00+00:00")
    assert report["verdict"]["outcome"] == "decided" and report["verdict"]["mechanism"] == "H-IO"
    assert report["counts"]["slow"] == 6 and report["verdict"]["counted"] == 3  # 每 job 每树取最早一条
    first = report["slow"][0]
    assert first["control"]["ok"] is True and first["control"]["starts"] >= 5
    assert "floorRaisedIntervals" in first["partition"]


def test_report_controls_must_be_post_landing(sp: Any, tmp_path: Path) -> None:
    report = sp.mechanism_report(_manifest(tmp_path), probe_landed_utc="2026-09-30T23:00:00+00:00")
    assert all(s["partition"]["reason"] == "no-control" for s in report["slow"])
    assert report["verdict"]["outcome"] == "undecided"


def test_report_controls_must_match_load_band(sp: Any, tmp_path: Path) -> None:
    jobs = _manifest(tmp_path)
    for job in jobs[:6]:  # 把快启动的 loadavg 抬到慢样本的 2 倍以上
        probe = Path(job["evidence"]) / "hosted-upgrade" / "daemon-1-probe.jsonl"
        lines = probe.read_text().splitlines()
        first = json.loads(lines[0])
        first["loadavg"] = [25.0, 25.0, 25.0]
        probe.write_text("\n".join([json.dumps(first)] + lines[1:]) + "\n")
    report = sp.mechanism_report(jobs, probe_landed_utc="2026-09-30T00:00:00+00:00")
    assert all(s["partition"]["reason"] == "no-control" for s in report["slow"])


def test_report_new_tree_and_fast_old_are_not_slow(sp: Any, tmp_path: Path) -> None:
    jobs = _manifest(tmp_path)
    ev = _job_evidence(tmp_path, "newtree", {"daemon-2": {"probe": SLOW_IO, "end": 120.0, "tree": "new",
                                                          "startedUtc": "2026-09-30T05:00:00+00:00"}})
    jobs.append({"taskId": 300, "createdUtc": "2026-09-30T05:00:00+00:00", "evidence": str(ev)})
    report = sp.mechanism_report(jobs, probe_landed_utc="2026-09-30T00:00:00+00:00")
    assert 300 not in {s["taskId"] for s in report["slow"]}


def test_report_timeout_start_is_slow_even_without_gap(sp: Any, tmp_path: Path) -> None:
    jobs = _manifest(tmp_path)
    ev = _job_evidence(tmp_path, "to", {"daemon-1": {"probe": FAST, "end": 101.0, "status": "timeout",
                                                     "startedUtc": "2026-09-30T06:00:00+00:00"}})
    jobs.append({"taskId": 400, "createdUtc": "2026-09-30T06:00:00+00:00", "evidence": str(ev)})
    report = sp.mechanism_report(jobs, probe_landed_utc="2026-09-30T00:00:00+00:00")
    timeout = next(s for s in report["slow"] if s["taskId"] == 400)
    assert timeout["status"] == "timeout" and timeout["observation"]["endSource"] == "last-sample"


def test_report_missing_evidence_is_listed_not_fatal(sp: Any, tmp_path: Path) -> None:
    jobs = _manifest(tmp_path) + [{"taskId": 999, "createdUtc": "2026-09-30T07:00:00+00:00",
                                   "evidence": str(tmp_path / "gone")}]
    report = sp.mechanism_report(jobs, probe_landed_utc="2026-09-30T00:00:00+00:00")
    assert any("999" in note for note in report["notes"])


def test_report_cli_writes_json(sp: Any, tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(_manifest(tmp_path)))
    out = tmp_path / "report.json"
    done = subprocess.run([sys.executable, str(ANALYSIS), "--manifest", str(manifest),
                           "--probe-landed", "2026-09-30T00:00:00+00:00", "--out", str(out)],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert json.loads(out.read_text())["verdict"]["mechanism"] == "H-IO"


def test_summarize_prefers_first_sample_loadavg(sp: Any, tmp_path: Path) -> None:
    """criteria §3/§6：配对用的 loadavg1 取 P2 第一条；没有时才退到 install 记录，并注明来源。"""
    path = _steady(tmp_path, n=4, loadavg=10.0)
    lines = path.read_text().splitlines()
    first_sample = json.loads(lines[1])
    first_sample["loadavg"] = [7.0, 7.0, 7.0]
    path.write_text("\n".join([lines[0], json.dumps(first_sample)] + lines[2:]) + "\n")
    summary = sp.summarize(sp.load(path), end_mono=None)
    assert summary["loadavg1"] == 7.0 and summary["loadavgSource"] == "first-sample"
    fallback = sp.summarize(sp.load(_steady(tmp_path, n=4, loadavg=10.0, name="q.jsonl")), end_mono=None)
    assert fallback["loadavg1"] == 10.0 and fallback["loadavgSource"] == "install"


# ---------------------------------------------------------------- v3.3：读耗时 = 等待部分

def test_read_time_is_wait_not_wall(sp: Any, tmp_path: Path) -> None:
    """criteria-v3.3 §5：读耗时 = 读墙钟 − 读内线程 CPU；读内 CPU 已计入 G 的 CPU。"""
    # 每区间 1 次读：墙钟 0.45s，其中读内 CPU 0.15s → 等待 0.30s
    samples = []
    for i in range(1, 41):
        t0 = 100 + (i - 1) * 0.5
        samples.append(_sample(t0 + 0.5, 0.15 * i + 0.01 * i, 0,
                               [[t0, t0 + 0.45, 0.15, "venv", 1, None, "get_data"]]))
    probe = sp.load(_probe_file(tmp_path, samples=samples))
    summary = sp.summarize(probe, end_mono=120.0)
    assert summary["readSeconds"] == pytest.approx(40 * 0.30)
    assert summary["readWallSeconds"] == pytest.approx(40 * 0.45)
    result = sp.partition(probe, end_mono=120.0, p95=P95, elapsed=20.0)
    # G = 20 − 6.4 = 13.6；墙钟口径 R=18/13.6>1 会被判异常，等待口径 R=12/13.6≈0.88 可计分
    assert result["scored"] is True and result["R"] == pytest.approx(12.0 / 13.6)


def test_inflight_read_counts_full_wall(sp: Any, tmp_path: Path) -> None:
    samples = [_sample(100.5, 0.05, 1), _sample(101.0, 0.1, 2, inflight=[[100.6, "venv", "get_data"]])]
    summary = sp.summarize(sp.load(_probe_file(tmp_path, samples=samples)), end_mono=None)
    assert summary["readSeconds"] == pytest.approx(0.4)


def test_pure_wait_reads_unchanged_under_wait_definition(sp: Any, tmp_path: Path) -> None:
    """v3.3 (b)：读内 CPU=0 的读，等待 = 墙钟，数值与旧口径相同。"""
    summary = sp.summarize(sp.load(_steady(tmp_path, n=40, reads_at={3: 0.4, 9: 0.2})), end_mono=120.0)
    assert summary["readSeconds"] == pytest.approx(0.6) == summary["readWallSeconds"]


def test_pure_cpu_read_interval_counts_as_no_read(sp: Any, tmp_path: Path) -> None:
    """v3.3 (c)：区间里的读全是纯 CPU（等待=0）时，该区间在 s1 分母里算「不含读」。"""
    samples = []
    niv = 0
    for i in range(1, 41):
        t0 = 100 + (i - 1) * 0.5
        hot = i > 20
        niv += 100 if hot else 0
        reads = [[t0, t0 + 0.3, 0.3, "venv", 1, None, "get_data"]] if i <= 20 else []  # 前 20 区间：纯 CPU 读
        samples.append(_sample(t0 + 0.5, 0.01 * i, niv, reads))
    probe = sp.load(_probe_file(tmp_path, samples=samples))
    intervals = sp._intervals(probe, 120.0, probe["samples"])
    assert all(i["read"] == 0 for i in intervals[:20])
    result = sp.partition(probe, end_mono=120.0, p95=P95, elapsed=20.0)
    # 40 个区间都算「不含读」，其中后 20 个争用 → s1 = 1 × 20/40
    assert result["R"] == 0 and result["s1"] == pytest.approx(0.5) and result["s1_R"] == pytest.approx(0.5)
