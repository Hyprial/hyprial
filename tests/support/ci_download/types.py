from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from pathlib import Path
import re
from typing import Any

CACHE_ENV = {
    "UV": "UV_CACHE_DIR",
    "PY": "UV_PYTHON_INSTALL_DIR",
    "PIP": "PIP_CACHE_DIR",
    "NPM": "npm_config_cache",
    "PNPM": "PNPM_STORE_DIR",
    "GOMOD": "GOMODCACHE",
    "CARGO": "CARGO_HOME",
    "RUSTUP": "RUSTUP_HOME",
    "PW": "PLAYWRIGHT_BROWSERS_PATH",
    "APT": "APT_CACHE_DIR",
    "APK": "APK_CACHE_DIR",
    "BREW": "HOMEBREW_CACHE",
    "HTTP": "CI_ARTIFACT_CACHE",
    "IMAGE": "CI_IMAGE_CACHE",
}


@dataclass(frozen=True)
class Context:
    workflow: str
    job: str
    step: str
    index: int
    cwd: str = "."
    env: tuple[tuple[str, str], ...] = ()
    condition: str = ""
    chain: tuple[str, ...] = ()
    builder: bool = False


@dataclass(frozen=True)
class Site:
    workflow: str
    job: str
    step: str
    step_index: int
    file: str
    line: int
    function: str
    ordinal: int
    command: str
    family: str
    call_chain: tuple[str, ...]
    env: tuple[tuple[str, str], ...]
    condition: str
    direct: bool
    builder: bool
    fingerprint: str


@dataclass(frozen=True)
class Diagnostic:
    workflow: str
    job: str
    step: str
    file: str
    line: int
    message: str
    call_chain: tuple[str, ...]


@dataclass
class Inventory:
    sites: list[Site]
    diagnostics: list[Diagnostic]
    steps: dict[tuple[str, str], list[dict]]

    def lexical(self, direct: bool) -> set[tuple]:
        # Family expansion (UV/PY, npx/PW/APT) and multiple callers are not
        # additional lexical command sites. Implicit images are separate.
        return {
            (s.file, s.line, s.function, s.command)
            for s in self.sites
            if s.direct == direct and s.function != "implicit-image"
        }


def families(argv: list[str], env: dict[str, str]) -> list[str]:
    if not argv:
        return []
    exe = Path(argv[0]).name.removesuffix(".exe")
    args = argv[1:]
    sub = next((a for a in args if not a.startswith("-")), "")
    if exe in {"npm", "pnpm"}:
        options = iter(args)
        for argument in options:
            if argument == "--prefix":
                next(options, None)
            elif not argument.startswith("-"):
                sub = argument
                break
    result: list[str] = []
    if exe == "uv":
        if sub in {"sync", "run", "build", "lock"} or (
            sub in {"tool", "pip"}
            and any(a in args for a in ("install", "upgrade", "sync"))
        ):
            result.append("UV")
        if sub == "python" and "install" in args:
            result.append("PY")
        elif (result or (sub == "venv" and "--python" in args)) and env.get(
            "UV_PYTHON_DOWNLOADS"
        ) != "never":
            result.append("PY")
        if sub == "publish":
            result.append("LIVE")
    elif exe in {"pip", "pip3"} and sub in {"install", "download", "wheel"}:
        result.append("PIP")
    elif exe == "npm" and sub in {"ci", "install", "i", "view", "exec"}:
        result.append("NPM")
    elif exe == "pnpm" and sub in {"install", "i", "add", "fetch", "dlx"}:
        result.append("PNPM")
    elif exe == "npx":
        result.append("NPM")
    elif exe == "go" and (
        sub in {"build", "test", "vet", "run", "install", "get"}
        or (sub == "mod" and "download" in args)
    ):
        result.append("GOMOD")
    elif exe == "cargo" and sub in {
        "build",
        "test",
        "check",
        "fetch",
        "install",
        "update",
    }:
        result.append("CARGO")
    elif exe == "rustup" and sub in {
        "install",
        "update",
        "default",
        "toolchain",
        "target",
        "component",
    }:
        result.append("RUSTUP")
    elif exe in {"apt", "apt-get"} and sub in {"update", "install", "download"}:
        result.append("APT")
    elif exe == "apk" and sub in {"add", "update", "upgrade", "fetch"}:
        result.append("APK")
    elif exe == "brew" and sub in {"install", "upgrade", "fetch", "update"}:
        result.append("BREW")
    elif exe == "git" and any(a in args for a in ("clone", "fetch", "ls-remote")):
        result.append("GIT")
    elif exe == "git" and "submodule" in args and "update" in args:
        result.append("GIT")
    elif exe in {"docker", "podman"} and any(
        a in args for a in ("pull", "build", "run", "up", "create")
    ):
        result.append("IMAGE")
    elif exe in {"curl", "wget"}:
        upload = any(
            a in args
            for a in (
                "POST",
                "PUT",
                "PATCH",
                "DELETE",
                "--upload-file",
                "-T",
                "--data",
                "--data-binary",
                "-d",
            )
        )
        result.append("LIVE" if upload else "HTTP")
    elif exe == "FROM":
        result.append("IMAGE")
    elif exe == "ADD" and any(re.match(r"(?:https?|git)://", a) for a in args):
        result.append("HTTP")
    if any("playwright" in a for a in argv):
        if "install-deps" in args or "--with-deps" in args:
            result.append("APT")
        if "install" in args:
            result.append("PW")
    return list(dict.fromkeys(result))


def condition(value: Any) -> str:
    return (
        str(value).strip().removeprefix("${{").removesuffix("}}").strip().lower()
        if value is not None
        else ""
    )


def expand(value: str, env: dict[str, str]) -> str:
    value = re.sub(
        r"\$\{([A-Za-z_]\w*):-([^{}]+)\}", lambda m: env.get(m[1], m[2]), value
    )
    value = value.replace(
        "${{ github.workspace }}",
        env.get("GITHUB_WORKSPACE", "${{ github.workspace }}"),
    )
    value = value.replace(
        "${{ github.action_path }}",
        env.get("GITHUB_ACTION_PATH", "${{ github.action_path }}"),
    )
    return re.sub(
        r"\$\{([A-Za-z_]\w*)\}|\$([A-Za-z_]\w*)",
        lambda m: env.get(m[1] or m[2], m[0]),
        value,
    )


def matrix_jobs(document: dict):
    """Expand static matrix rows; unknown expressions remain reviewable."""
    for name, job in (document.get("jobs") or {}).items():
        matrix = (
            job.get("strategy", {}).get("matrix", {}) if isinstance(job, dict) else {}
        )
        if not isinstance(matrix, dict) or not matrix:
            yield name, name, job
            continue
        axes = {
            k: v
            for k, v in matrix.items()
            if k not in {"include", "exclude"} and isinstance(v, list)
        }
        rows = (
            [dict(zip(axes, values)) for values in product(*axes.values())]
            if axes
            else []
        )
        rows = [
            r
            for r in rows
            if not any(
                all(r.get(k) == v for k, v in e.items())
                for e in matrix.get("exclude", [])
            )
        ]
        for include in matrix.get("include", []):
            matching = [
                r
                for r in rows
                if all(r.get(k) == v for k, v in include.items() if k in axes)
            ]
            if matching:
                for row in matching:
                    row.update(include)
            else:
                rows.append(include)

        def substitute(obj, row):
            if isinstance(obj, str):
                return re.sub(
                    r"\$\{\{\s*matrix\.(\w+)\s*}}",
                    lambda m: str(row.get(m[1], m[0])),
                    obj,
                )
            if isinstance(obj, list):
                return [substitute(v, row) for v in obj]
            if isinstance(obj, dict):
                return {k: substitute(v, row) for k, v in obj.items()}
            return obj

        for row in rows:
            label = (
                str(name)
                + "["
                + ",".join(f"{k}={v}" for k, v in sorted(row.items()))
                + "]"
            )
            yield name, label, substitute(job, row)


def js_tokens(text: str) -> list[tuple[str, str, int]]:
    """Small lexical reader: strings/comments are opaque, nesting is preserved.

    Regex literals are consumed as opaque too; strings inside assertions never
    become calls. Unsupported expressions remain symbolic review diagnostics.
    """
    pattern = re.compile(
        r"""(?P<space>\s+)|(?P<comment>//[^\n]*|/\*[\s\S]*?\*/)|(?P<string>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)|(?P<regex>/(?![/*])(?:\\.|\[(?:\\.|[^\]\\])*\]|[^/\n\\])+/[gimsuy]*)|(?P<word>[A-Za-z_$][\w$]*)|(?P<punct>.)"""
    )
    result = []
    line = 1
    for m in pattern.finditer(text):
        kind = m.lastgroup
        raw = m[0]
        if kind not in {"space", "comment", "regex"}:
            result.append((raw[1:-1] if kind == "string" else raw, kind, line))
        line += raw.count("\n")
    return result


def js_close(tokens, start):
    depth = 0
    for i in range(start, len(tokens)):
        if tokens[i][1] == "string":
            continue
        depth += (tokens[i][0] in {"(", "[", "{"}) - (tokens[i][0] in {")", "]", "}"})
        if depth == 0:
            return i
    return None


def js_split(tokens):
    parts = [[]]
    depth = 0
    for token in tokens:
        v, kind, _ = token
        if kind != "string":
            depth += (v in {"(", "[", "{"}) - (v in {")", "]", "}"})
            if v == "," and depth == 0:
                parts.append([])
                continue
        parts[-1].append(token)
    return parts
