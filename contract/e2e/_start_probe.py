"""AT04 card 6: read and score the isolated daemon's start probe (criteria-v3.3 §4–§7).

The probe itself is PROBE in run-hosted-registry.py; this module only reads its
daemon-N-probe.jsonl. Nothing here raises on bad evidence: problems come back as
notes, and a start that cannot be measured is simply not scored.

Terms, all in seconds of one start:
- wall = end − install, CPU = utime+stime at the last sample ≤ end minus at install,
  G = wall − CPU (the gap this card explains);
- intervals are consecutive samples; a read's wall time is split over the intervals
  it spans; an interval's nivcsw rate is per CPU second, Δnivcsw / max(ΔCPU, 1 ms)
  (criteria-v3.2 §5: on macOS a busy thread is preempted ~110/s with no contention,
  so a per-wall-second rate barely moves when the thread is starved); an interval is
  *contended* when that rate exceeds the control p95;
- R = reads / G, R_quiet = reads outside contended intervals / G. The main split uses
  R_quiet, the consistency split uses R; they must name the same dominant hypothesis.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
from typing import Any

GAP_FLOOR_SECONDS = 3.0
GAP_FLOOR_SHARE = 0.10
DOMINANT_AT = 0.6
DOMINANT_LEAD = 0.2
REFUTED_BELOW = 0.2
MIN_CONTROL_STARTS = 5
MIN_CONTROL_NO_READ_INTERVALS = 20
MIN_DECIDING = 3
CPU_FLOOR_SECONDS = 0.001
WINDOW = timedelta(hours=6)
SLOW_GAP_SECONDS = 15.0
CONTROL_GAP_SECONDS = 3.0
LOAD_BAND = 0.25
CONTROL_SPAN = timedelta(days=7)
FIELDS = ("utime", "stime", "minflt", "majflt", "inblock", "nvcsw", "nivcsw")


def load(path: Path) -> dict[str, Any]:
    """Parse one probe file; a torn last line (SIGKILL mid-write) is tolerated."""
    parsed: dict[str, Any] = {"install": None, "samples": [], "marks": {}, "problems": [], "torn": False,
                              "path": str(path)}
    try:
        lines = Path(path).read_text().splitlines()
    except FileNotFoundError:
        parsed["problems"].append("probe file missing")
        return parsed
    except (OSError, ValueError) as error:
        parsed["problems"].append(f"probe file unreadable: {type(error).__name__}")
        return parsed
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            if index == len(lines) - 1:
                parsed["torn"] = True
            else:
                parsed["problems"].append(f"line {index + 1} unparsable")
            continue
        if not isinstance(record, dict):
            parsed["problems"].append(f"line {index + 1} not an object")
            continue
        kind = record.get("kind")
        if kind == "install" and parsed["install"] is None:
            parsed["install"] = record
        elif kind == "sample" and _finite(record.get("mono")) and isinstance(record.get("usage"), dict):
            parsed["samples"].append(record)
        elif kind == "mark" and isinstance(record.get("name"), str):
            parsed["marks"].setdefault(record["name"], record.get("mono"))
    parsed["samples"].sort(key=lambda record: record["mono"])
    return parsed


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _cpu(usage: dict | None) -> float | None:
    if not isinstance(usage, dict) or not _finite(usage.get("utime")) or not _finite(usage.get("stime")):
        return None
    return usage["utime"] + usage["stime"]


def _window(probe: dict, end_mono: float | None) -> tuple[float, list[dict], str]:
    """(end, samples up to end, source of the end point)."""
    samples = probe["samples"]
    if _finite(end_mono):
        return float(end_mono), [s for s in samples if s["mono"] <= end_mono], "ready-mark"
    return (samples[-1]["mono"] if samples else probe["install"]["mono"]), samples, "last-sample"


def _reads(samples: list[dict], end: float) -> list[tuple[float, float, float]]:
    """Every read as (start, end, wait share), clipped to `end`.

    criteria-v3.3 §5: a read's time is its wait, wall minus the thread CPU spent
    inside it (that CPU is already in G's CPU); the share wait/wall spreads the wait
    evenly over the read's span. An in-flight read at the last sample has no CPU
    record, so it counts in full up to `end`."""
    spans = []
    for sample in samples:
        for read in sample.get("reads") or []:
            if isinstance(read, list) and len(read) >= 2 and _finite(read[0]) and _finite(read[1]):
                wall = read[1] - read[0]
                cpu = read[2] if len(read) > 2 and _finite(read[2]) else 0.0
                share = min(1.0, max(0.0, (wall - cpu) / wall)) if wall > 0 else 0.0
                spans.append((read[0], min(read[1], end), share))
    if samples:
        for read in samples[-1].get("inflight") or []:
            if isinstance(read, list) and read and _finite(read[0]):
                spans.append((read[0], end, 1.0))
    return [(a, b, w) for a, b, w in spans if b > a]


def summarize(probe: dict, end_mono: float | None) -> dict[str, Any]:
    """Observation fields for readiness.json; never raises."""
    summary: dict[str, Any] = {"installed": False, "note": None, "samples": 0, "reads": 0, "readSeconds": None,
                               "wall": None, "cpu": None, "G": None, "endSource": None,
                               "lastSampleToEndSeconds": None, "loadavg1": None, "loadavgSource": None, "hooks": None,
                               "readWallSeconds": None,
                               "torn": False}
    try:
        install = probe.get("install")
        if install is None or not _finite(install.get("mono")):
            summary["note"] = "; ".join(probe.get("problems") or []) or "probe not installed"
            return summary
        summary.update(installed=True, hooks=install.get("hooks"), torn=probe.get("torn", False))
        # criteria §3/§6: the first P2 sample's loadavg; the install record (same
        # moment, before any import) only when that sample lacks one.
        samples_all = probe.get("samples") or []
        first_load = (samples_all[0].get("loadavg") or [None])[0] if samples_all else None
        if _finite(first_load):
            summary.update(loadavg1=first_load, loadavgSource="first-sample")
        else:
            load1 = (install.get("loadavg") or [None])[0]
            summary.update(loadavg1=load1 if _finite(load1) else None,
                           loadavgSource="install" if _finite(load1) else None)
        end, samples, source = _window(probe, end_mono)
        spans = _reads(samples, end)
        summary.update(samples=len(samples), reads=len(spans),
                       readSeconds=round(sum((b - a) * w for a, b, w in spans), 6),
                       readWallSeconds=round(sum(b - a for a, b, _ in spans), 6),
                       endSource=source, wall=round(end - install["mono"], 6))
        start_cpu = _cpu(install.get("usage"))
        end_cpu = _cpu(samples[-1]["usage"]) if samples else None
        if start_cpu is not None and end_cpu is not None:
            summary["cpu"] = round(end_cpu - start_cpu, 6)
            summary["G"] = round(summary["wall"] - summary["cpu"], 6)
        if samples and source == "ready-mark":
            # Only meaningful when the end is an external reading; a timed-out start's
            # distance to the timeout moment is timeoutDeltaSeconds (observe_start).
            summary["lastSampleToEndSeconds"] = round(end - samples[-1]["mono"], 6)
        notes = list(probe.get("problems") or [])
        if not samples:
            notes.append("no sample before the end point: CPU and G unmeasured")
        if notes:
            summary["note"] = "; ".join(notes)
    except Exception as error:  # noqa: BLE001 - evidence only
        summary["note"] = f"summary failed: {type(error).__name__}: {error}"
    return summary


def _intervals(probe: dict, end: float, samples: list[dict]) -> list[dict]:
    """Consecutive-sample intervals with nivcsw rate and the read time falling in each."""
    points = [(probe["install"]["mono"], probe["install"].get("usage"))] + [(s["mono"], s["usage"]) for s in samples]
    spans = _reads(samples, end)
    out = []
    for (t0, u0), (t1, u1) in zip(points, points[1:]):
        if t1 <= t0 or not isinstance(u0, dict) or not isinstance(u1, dict):
            continue
        c0, c1 = _cpu(u0), _cpu(u1)
        if not (_finite(u0.get("nivcsw")) and _finite(u1.get("nivcsw"))) or c0 is None or c1 is None:
            continue
        overlap = sum(max(0.0, min(b, t1) - max(a, t0)) * w for a, b, w in spans)
        switches = u1["nivcsw"] - u0["nivcsw"]
        rate = switches / max(c1 - c0, CPU_FLOOR_SECONDS)
        out.append({"t0": t0, "t1": t1, "rate": rate, "read": overlap,
                    "floorRaised": c1 - c0 < CPU_FLOOR_SECONDS and switches >= 1})
    return out


def control_p95(controls: list[dict]) -> dict[str, Any]:
    """nivcsw-rate p95 over ALL control intervals; needs ≥5 starts AND ≥20 no-read intervals."""
    rates, no_read, starts = [], 0, 0
    for probe in controls:
        if probe.get("install") is None or not probe["samples"]:
            continue
        starts += 1
        end, samples, _ = _window(probe, None)
        for interval in _intervals(probe, end, samples):
            rates.append(interval["rate"])
            no_read += interval["read"] == 0
    ok = starts >= MIN_CONTROL_STARTS and no_read >= MIN_CONTROL_NO_READ_INTERVALS and bool(rates)
    p95 = None
    if rates:
        ordered = sorted(rates)
        p95 = ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)]
    return {"ok": ok, "p95": p95, "starts": starts, "intervals": len(rates), "noReadIntervals": no_read}


def dominant(s1: float, s_io: float) -> str:
    if s1 >= DOMINANT_AT and s1 - s_io >= DOMINANT_LEAD:
        return "H1"
    if s_io >= DOMINANT_AT and s_io - s1 >= DOMINANT_LEAD:
        return "H-IO"
    return "mixed"


def partition(probe: dict, end_mono: float | None, p95: float | None, elapsed: float | None) -> dict[str, Any]:
    """Exclusive split of G (criteria §5); unscored starts say why."""
    result: dict[str, Any] = {"scored": False, "reason": None, "label": None}
    summary = summarize(probe, end_mono)
    result["summary"] = summary
    gap = summary.get("G")
    if not summary["installed"] or gap is None:
        result["reason"] = "unmeasured"
        return result
    if gap < GAP_FLOOR_SECONDS or (_finite(elapsed) and gap < GAP_FLOOR_SHARE * elapsed):
        result["reason"] = "cpu-dominant"
        return result
    if not _finite(p95):
        result["reason"] = "no-control"
        return result
    end, samples, _ = _window(probe, end_mono)
    intervals = _intervals(probe, end, samples)
    contended = [interval["rate"] > p95 for interval in intervals]
    read_all = sum(interval["read"] for interval in intervals)
    read_quiet = sum(interval["read"] for interval, hot in zip(intervals, contended) if not hot)
    raw_r, raw_quiet = read_all / gap, read_quiet / gap
    result.update(R_raw=raw_r, R_quiet_raw=raw_quiet)
    if raw_r > 1 or raw_quiet > 1:
        result["reason"] = "probe-or-definition-anomaly"
        return result

    def share(keep: list[bool]) -> float:
        pool = [hot for hot, kept in zip(contended, keep) if kept]
        return sum(pool) / len(pool) if pool else 0.0

    # A read with zero wait (pure CPU) adds nothing to an interval: that interval counts
    # as holding no read (criteria-v3.3 §5, review f17da57c 4c).
    no_quiet_read = [not (interval["read"] > 0 and not hot) for interval, hot in zip(intervals, contended)]
    no_read = [interval["read"] == 0 for interval in intervals]
    r_quiet, r_all = min(1.0, raw_quiet), min(1.0, raw_r)
    s1 = (1 - r_quiet) * share(no_quiet_read)
    s1_r = (1 - r_all) * share(no_read)
    floor = [interval["floorRaised"] for interval in intervals]
    result.update(floorRaisedIntervals=sum(floor),
                  floorRaisedContended=sum(1 for raised, hot in zip(floor, contended) if raised and hot))
    result.update(scored=True, R=r_all, R_quiet=r_quiet, s_io=r_quiet, s1=s1, s_io_R=r_all, s1_R=s1_r,
                  residual=1 - s1 - r_quiet, residual_R=1 - s1_r - r_all,
                  dominant=dominant(s1, r_quiet), dominant_R=dominant(s1_r, r_all))
    result["label"] = result["dominant"] if result["dominant"] == result["dominant_R"] else "mixed"
    return result


def mechanism_verdict(samples: list[dict]) -> dict[str, Any]:
    """§7: one earliest start per (job, tree); decided needs ≥3 scored agreeing, no
    counterexample and not all inside one 6 h window; refuted needs ≥3 scored < 0.2."""
    first: dict[tuple, dict] = {}
    for sample in sorted(samples, key=lambda s: s["createdUtc"]):
        first.setdefault((sample["taskId"], sample.get("tree")), sample)
    counted = [s for s in first.values() if s.get("scored")]
    labels = [s["label"] for s in counted]
    refuted = [name for name, key in (("H1", "s1"), ("H-IO", "s_io"))
               if sum(1 for s in counted if s.get(key, 1) < REFUTED_BELOW) >= MIN_DECIDING]
    verdict: dict[str, Any] = {"counted": len(counted), "labels": labels, "refuted": refuted,
                               "mechanism": None, "outcome": "undecided"}
    for name in ("H1", "H-IO"):
        agreeing = [s for s in counted if s["label"] == name]
        opposite = [s for s in counted if s["label"] in ("H1", "H-IO") and s["label"] != name]
        if len(agreeing) >= MIN_DECIDING and not opposite:
            times = sorted(datetime.fromisoformat(s["createdUtc"]) for s in agreeing)
            verdict["mechanism"] = name
            verdict["outcome"] = "observational" if times[-1] - times[0] < WINDOW else "decided"
    return verdict


def read_mark(path: Path) -> dict[str, Any]:
    """The parent's <label>-ready-mark.json; anything unusable is {} (never raises)."""
    try:
        mark = json.loads(Path(path).read_text())
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(mark, dict) or not _finite(mark.get("monotonic")):
        return {}
    return {"status": mark.get("status"), "monotonic": float(mark["monotonic"])}


def observe_start(probe_path: Path, mark_path: Path) -> dict[str, Any]:
    """One start's observation under criteria §4.

    A ready start ends at the parent's ready-mark reading. A timed-out start ends at
    its last probe sample before SIGKILL: the mark then holds the timeout moment, and
    the child keeps sampling through the diagnostics that follow, so the mark is
    reported (timeoutMono, timeoutDeltaSeconds = last sample − timeout) but is not
    the end. Without a usable mark the last sample is the end as well."""
    mark = read_mark(mark_path)
    probe = load(probe_path)
    timed_out = mark.get("status") == "timeout"
    end = None if timed_out or not mark else mark["monotonic"]
    summary = summarize(probe, end)
    summary.update(markStatus=mark.get("status"), timeoutMono=None, timeoutDeltaSeconds=None)
    if timed_out:
        summary["timeoutMono"] = mark["monotonic"]
        if probe["samples"]:
            summary["timeoutDeltaSeconds"] = round(probe["samples"][-1]["mono"] - mark["monotonic"], 6)
    summary["_probe"], summary["_end"] = probe, end
    return summary


def _starts(job: dict[str, Any], notes: list[str]) -> list[dict[str, Any]]:
    """Every start of one job's evidence root, with its observation."""
    evidence = Path(job["evidence"])
    try:
        rows = json.loads((evidence / "readiness.json").read_text())["rows"]
    except Exception as error:  # noqa: BLE001
        notes.append(f"task {job.get('taskId')}: readiness.json unusable ({type(error).__name__})")
        return []
    starts = []
    for row in rows:
        try:
            key = row["key"]
            observation = observe_start(evidence / (key + "-probe.jsonl"), evidence / (key + "-ready-mark.json"))
            starts.append({"taskId": job["taskId"], "jobCreatedUtc": job.get("createdUtc"), "key": key,
                           "tree": row.get("tree"), "status": row.get("status"),
                           "createdUtc": row.get("startedUtc") or job.get("createdUtc"),
                           "elapsed": row.get("elapsedSeconds"), "observation": observation})
        except Exception as error:  # noqa: BLE001
            notes.append(f"task {job.get('taskId')} {row.get('key')}: {type(error).__name__}")
    return starts


def _when(value: str) -> datetime:
    return datetime.fromisoformat(value)


def mechanism_report(jobs: list[dict[str, Any]], probe_landed_utc: str) -> dict[str, Any]:
    """Criteria §5–§7 over a manifest of hosted jobs ({taskId, createdUtc, evidence}).

    Slow = old tree and (G ≥ 15 s or status timeout). Controls for one slow start =
    old-tree starts with G < 3 s, created at or after the probe landed, loadavg1 (the
    probe's first record) within ±25 % of the slow start's, within ±7 days; p95 needs
    ≥ 5 such starts and ≥ 20 no-read intervals. Then partition, then the §7 verdict
    (one earliest start per job and tree)."""
    notes: list[str] = []
    landed = _when(probe_landed_utc)
    starts = [start for job in jobs for start in _starts(job, notes)]
    old = [s for s in starts if s["tree"] == "old"]
    slow = [s for s in old if s["status"] == "timeout"
            or (_finite(s["observation"].get("G")) and s["observation"]["G"] >= SLOW_GAP_SECONDS)]
    pool = [s for s in old if s not in slow and _finite(s["observation"].get("G"))
            and s["observation"]["G"] < CONTROL_GAP_SECONDS and s["createdUtc"]
            and _when(s["createdUtc"]) >= landed and _finite(s["observation"].get("loadavg1"))]
    scored = []
    for sample in slow:
        load1 = sample["observation"].get("loadavg1")
        when = _when(sample["createdUtc"]) if sample["createdUtc"] else None
        controls = [c for c in pool if _finite(load1) and when is not None
                    and abs(c["observation"]["loadavg1"] - load1) <= LOAD_BAND * load1
                    and abs(_when(c["createdUtc"]) - when) <= CONTROL_SPAN]
        control = control_p95([c["observation"]["_probe"] for c in controls])
        control["tasks"] = sorted({c["taskId"] for c in controls})
        p95 = control["p95"] if control["ok"] else None
        result = partition(sample["observation"]["_probe"], sample["observation"]["_end"], p95, sample["elapsed"])
        result.pop("summary", None)
        sample["control"], sample["partition"] = control, result
        scored.append({"scored": result["scored"], "label": result["label"], "s1": result.get("s1"),
                       "s_io": result.get("s_io"), "createdUtc": sample["createdUtc"],
                       "taskId": sample["taskId"], "tree": sample["tree"]})
    verdict = mechanism_verdict(scored)
    for sample in starts:
        sample["observation"] = {k: v for k, v in sample["observation"].items() if not k.startswith("_")}
    return {"probeLandedUtc": probe_landed_utc, "counts": {"jobs": len(jobs), "starts": len(starts),
            "oldStarts": len(old), "slow": len(slow), "controlPool": len(pool)},
            "slow": sorted(slow, key=lambda s: (s["createdUtc"] or "", s["taskId"])),
            "verdict": verdict, "notes": notes}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AT04 card 6 mechanism report (criteria-v3.2 §5–§7)")
    parser.add_argument("--manifest", required=True, help="JSON list of {taskId, createdUtc, evidence}")
    parser.add_argument("--probe-landed", required=True, help="ISO time the probe landed on dev")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    jobs = json.loads(Path(args.manifest).read_text())
    report = mechanism_report(jobs, args.probe_landed)
    Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
