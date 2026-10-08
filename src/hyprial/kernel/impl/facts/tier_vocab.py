"""Tier vocabulary: TIERS / tier_for_model / Candidate (kernel-owned, 任务书 §2.5).

依据 PY-BATCH-2026-10-01，由执行模型落实：自 dispatch/matrix.py 下沉的纯映射与常量；
engine 提取 + 顺序修正（Candidate/_candidate 须先于 TIERS 的模块级求值）。
"""

from typing import NamedTuple


def build_probe_command(
    harness: str, model: str, provider: str | None = None
) -> tuple[str, ...]:
    """Format the exact minimal invocation; execution belongs to the caller."""
    if harness == "pi":
        if not provider:
            raise ValueError(
                "pi probes require an explicit provider; without --provider the "
                "model resolves ambiguously"
            )
        return (
            "pi", "-p", "--provider", provider, "--model", model,
            "--thinking", "high", "hello",
        )
    if harness == "claude":
        return ("claude", "-p", "--model", model, "hello")
    if harness == "codex":
        return ("codex", "exec", "--skip-git-repo-check", "--model", model, "hello")
    raise ValueError(f"unsupported harness {harness!r}")


class Candidate(NamedTuple):
    harness: str
    provider: str | None
    model: str

    @property
    def probe_cmd(self) -> tuple[str, ...]:
        return build_probe_command(self.harness, self.model, self.provider)


def _candidate(harness: str, provider: str | None, model: str) -> Candidate:
    return Candidate(harness, provider, model)


TIERS: dict[str, tuple[Candidate, ...]] = {
    "fast": (
        _candidate("codex", None, "gpt-5.6-luna"),
        _candidate("claude", None, "sonnet"),
        _candidate("pi", "deepseek", "deepseek-flash"),
        _candidate("pi", "zai-coding-cn", "glm-5.3-flash"),
    ),
    "strong": (
        _candidate("codex", None, "gpt-5.6-sol"),
        _candidate("pi", "openai-codex", "gpt-5.6-sol"),
        _candidate("claude", None, "fable"),
        _candidate("pi", "openai-codex", "gpt-6-astra"),
        _candidate("claude", None, "opus"),
        _candidate("pi", "kimi-coding", "k3"),
    ),
    "super": (
        _candidate("claude", None, "fable"),
        _candidate("pi", "openai-codex", "gpt-6-astra"),
    ),
}


def tier_for_model(model: str | None) -> str | None:
    """Highest tier for an exact model id; never guess unknown models."""
    return next(
        (tier for tier in reversed(TIERS) if any(c.model == model for c in TIERS[tier])),
        None,
    )
