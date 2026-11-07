#!/usr/bin/env python3
"""Regenerate and verify the survey's prose metrics and E1-E3 appendices."""

from __future__ import annotations

import argparse
import contextlib
import io

import kernel_isolation_survey as survey

DOC = (
    survey.ROOT
    / "docs"
    / "design"
    / "kernel-isolation-and-rust-core-survey-2026-10-23.md"
)
E1_HEADING = "### E1. Execution-site scanner output"


def capture(function: object, /, **kwargs: object) -> str:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        function(**kwargs)  # type: ignore[operator]
    return output.getvalue().rstrip()


def _replace_block(text: str, name: str, content: str) -> str:
    start = f"<!-- generated:{name}:start -->"
    end = f"<!-- generated:{name}:end -->"
    if text.count(start) != 1 or text.count(end) != 1:
        raise SystemExit(f"expected one generated block: {name}")
    prefix, remainder = text.split(start, 1)
    _old, suffix = remainder.split(end, 1)
    return f"{prefix}{start}\n{content.rstrip()}\n{end}{suffix}"


def _metric_blocks() -> dict[str, str]:
    metrics = survey.retry_metrics()
    if metrics.undisposed:
        raise SystemExit(f"undisposed retry candidates: {metrics.undisposed}")
    qualification = (
        "inventory complete with respect to the detected shapes listed in Method "
        "coverage; shapes listed as blind spots are flagged, not enumerated "
        "exhaustively"
    )
    return {
        "conclusion-retry": (
            f"当前源码有 {metrics.families} 个已分类的重试、重连、重启、重放或竞争等待机制。"
            "机械扫描保留旧候选，并按操作重做语义识别 Method coverage 中的形态。"
            f"扫描列出 {metrics.candidates} 个候选函数，其中 {metrics.operation_repeat} 个满足 "
            "operation-repeat 形态。复核处置为 "
            f"classified={metrics.classified}、limit-class={metrics.limit_class}、"
            f"dismissed={metrics.dismissed}、undisposed={metrics.undisposed}。"
            f"E2 是 METHOD-BOUNDED：{qualification}。纯状态读取即使循环轮询也属于 "
            "observation wait，不属于 retry。依赖内部重试总数为 **UNKNOWN**。"
            f"{metrics.families} 个机制没有共享的总预算。固定 10 ms 且等待 ACK 的 "
            "`EffectLane` 重投尤其危险：每条 lane 有容量，但 daemon 没有统一的唤醒率、CPU 或日志预算。"
        ),
        "a2-intro": (
            f"附录 E2 给出 {metrics.families} 个已分类机制，并按代码记录 schedule、cap、"
            "预算范围和日志行为。分类规则不看函数名：循环在 delay、wait 或 backoff 后重做操作，"
            "或在 failure、overload 后重做操作，就是 retry；循环若只读取状态，则是 observation wait。"
            "表中的 caller deadline 只限制等待；除非该行明确说明取消，否则 caller deadline 不撤销"
            "已接受的 custody。以下缺口直接解释当前故障模式："
        ),
        "a2-summary": (
            f"E2 对全部 {metrics.candidates} 个机械候选给出处置，其中 "
            f"{metrics.operation_repeat} 个满足 operation-repeat 形态。"
            f"处置计数为 classified={metrics.classified}、limit-class={metrics.limit_class}、"
            f"dismissed={metrics.dismissed}、undisposed={metrics.undisposed}。"
            f"E2 是 METHOD-BOUNDED：{qualification}。检测到的 periodic re-entry 等盲区实例"
            "按 `limit-class:<shape>` 和 `file:line` 记录，不作 dismissal。依赖内部循环仍为 **UNKNOWN**。"
        ),
        "r3": (
            f"| R3 | Rust timer 和 `RetryEngine` 成为 authority，actor 仍由 Pykka 执行 | "
            f"{metrics.families} 个 retry fixture 的 schedule/cap/durable outcome 相等；"
            f"{metrics.candidates} 个候选处置保持 classified={metrics.classified}、"
            f"limit-class={metrics.limit_class}、dismissed={metrics.dismissed}、"
            f"undisposed={metrics.undisposed}；storm 下本地读取达到 B2 | "
            "显式回退到 Python timer；不自动双写 |"
        ),
        "g1": (
            f"| G1 重试清单 | 对 {metrics.candidates} 个机械候选运行分类门 | "
            f"classified={metrics.classified}、limit-class={metrics.limit_class}、"
            f"dismissed={metrics.dismissed}、undisposed={metrics.undisposed}；"
            f"{metrics.families} 个已知 family 都绑定 `policy_id`；发现的盲区实例均有 "
            "`limit-class:<shape>` 和 `file:line`；非 durable policy 同时缺少 attempt cap 和 elapsed cap 时失败 |"
        ),
    }


def render(original: str) -> str:
    rendered = original
    for name, content in _metric_blocks().items():
        rendered = _replace_block(rendered, name, content)
    prefix, marker, _generated = rendered.partition(E1_HEADING)
    if not marker:
        raise SystemExit(f"missing generated appendix marker: {E1_HEADING}")
    execution = capture(survey.print_execution)
    retries = capture(survey.print_retries, all_candidates=True)
    kernel = capture(survey.print_kernel)
    return (
        f"{prefix}{E1_HEADING}\n\n{execution}\n\n"
        f"### E2. Retry scanner output\n\n{retries}\n\n"
        f"### E3. Kernel-interface scanner output\n\n{kernel}\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if committed prose metrics or appendices differ from generated evidence",
    )
    args = parser.parse_args()
    original = DOC.read_text(encoding="utf-8")
    rendered = render(original)
    if args.check:
        if rendered != original:
            raise SystemExit("survey evidence is stale; run update_kernel_isolation_survey_evidence.py")
        print("survey evidence: PASS; prose metrics and E1-E3 are current")
        return
    DOC.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
