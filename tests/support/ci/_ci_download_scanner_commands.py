from __future__ import annotations

import json
from pathlib import Path
import re
import shlex
from dataclasses import replace

from tests.support.ci_download.types import CACHE_ENV, Context, expand


class CommandMethods:
    def command(
        self, argv: list[str], ctx: Context, file: str, line: int, function: str = ""
    ):
        argv = [str(a) for a in argv]
        if not argv:
            return
        env = dict(ctx.env)
        while argv and (
            "=" in argv[0]
            and re.match(r"^[A-Za-z_]\w*=", argv[0])
            or argv[0] in {"env", "sudo", "command", "exec", "timeout"}
        ):
            item = argv.pop(0)
            if "=" in item:
                k, v = item.split("=", 1)
                env[k] = expand(v, env)
            elif item in {"env", "sudo", "timeout"}:
                while argv and (
                    argv[0].startswith("-") or re.fullmatch(r"\d+[smh]?", argv[0])
                ):
                    option = argv.pop(0)
                    if (
                        option
                        in {
                            "-u",
                            "--unset",
                            "-C",
                            "--chdir",
                            "--user",
                            "-g",
                            "--group",
                            "-s",
                            "--signal",
                            "-k",
                            "--kill-after",
                        }
                        and argv
                    ):
                        argv.pop(0)
        if not argv:
            return
        argv = [expand(a, env) for a in argv]
        ctx = replace(ctx, env=tuple(sorted(env.items())))
        exe = Path(argv[0]).name.removesuffix(".exe")
        if exe in {"echo", "printf", "assert", "test", "[", "[["}:
            return
        if exe in {"python", "python3", "python3.12"} and argv[1:3] == ["-m", "pip"]:
            self.emit(["pip", *argv[3:]], ctx, file, line, function)
            return
        self.emit(argv, ctx, file, line, function)
        if exe == "uv" and "run" in argv:
            for i, a in enumerate(argv[2:], 2):
                if a in {"python", "python3", "bash", "sh", "node"} or a.endswith(
                    (".py", ".sh", ".mjs")
                ):
                    self.command(argv[i:], ctx, file, line, function)
                    break
        elif exe in {
            "bash",
            "sh",
            "python",
            "python3",
            "python3.12",
            "node",
            ".",
            "source",
        }:
            args = argv[1:]
            if "-c" in args or "-e" in args:
                pos = args.index("-c" if "-c" in args else "-e")
                if pos + 1 < len(args):
                    code = args[pos + 1]
                    if exe in {"bash", "sh"}:
                        self.shell(code, ctx, file, line)
                    elif exe.startswith("python"):
                        self.python(code, ctx, file, line - 1)
                    else:
                        self.javascript(code, ctx, file, line - 1)
            elif "-m" not in args and "--test" not in args and "-" not in args:
                target = next((a for a in args if not a.startswith("-")), "")
                if (
                    target
                    and "playwright" not in target
                    and "restore.cjs" not in target
                    and "save.cjs" not in target
                ):
                    inherited = self.follow(target, ctx, file, line)
                    if exe in {".", "source"}:
                        return inherited
                    # Statically proven argv-slice forwarding (machine-lock,
                    # timeout and similar Python launchers). The acquisition
                    # command belongs to the caller's lexical argv, with the
                    # forwarding definition in its call chain.
                    script = self.root / ctx.cwd / target
                    if script.is_file() and script.suffix == ".py":
                        source = script.read_text()
                        forwarding = re.search(
                            r"(\w+)\s*=\s*(?:sys\.)?argv\[(\d+):\]", source
                        )
                        if forwarding and re.search(
                            r"subprocess\.(?:run|Popen)\(" + forwarding[1] + r"\b",
                            source,
                        ):
                            tail = args[int(forwarding[2]) :]
                            if tail:
                                self.command(
                                    tail,
                                    replace(
                                        ctx,
                                        chain=ctx.chain
                                        + (f"argv forwarded by {target}",),
                                    ),
                                    file,
                                    line + 1,
                                    function,
                                )
        elif exe in {"npm", "pnpm"} and "run" in argv:
            pos = argv.index("run")
            if pos + 1 < len(argv):
                cwd = ctx.cwd
                prefix = next(
                    (a.split("=", 1)[1] for a in argv if a.startswith("--prefix=")),
                    None,
                )
                if "--prefix" in argv and argv.index("--prefix") + 1 < len(argv):
                    prefix = argv[argv.index("--prefix") + 1]
                if prefix is not None:
                    cwd = str(Path(ctx.cwd) / prefix)
                package = (self.root / cwd / "package.json").resolve()
                if not package.is_relative_to(self.root):
                    self.diagnostic(ctx, file, line, "package script outside checkout")
                    return
                if package.is_file():
                    script = (
                        json.loads(package.read_text())
                        .get("scripts", {})
                        .get(argv[pos + 1])
                    )
                    if script:
                        self.shell(
                            script,
                            replace(
                                ctx,
                                cwd=cwd,
                                chain=ctx.chain + (f"{file}:package:{argv[pos + 1]}",),
                            ),
                            str(package.relative_to(self.root)),
                        )
                    else:
                        self.diagnostic(
                            ctx, file, line, "missing package script " + argv[pos + 1]
                        )
                else:
                    self.diagnostic(ctx, file, line, f"missing package.json in {cwd}")
        elif exe in {"docker", "podman"} and "build" in argv:
            target = (
                argv[argv.index("-f") + 1]
                if "-f" in argv
                else str(Path(argv[-1]) / "Dockerfile")
            )
            self.follow(target, replace(ctx, builder=True), file, line)
        elif exe in {"eval", "xargs"}:
            self.diagnostic(
                ctx, file, line, f"{exe} execution requires a reviewed traversal edge"
            )
        elif exe == "make":
            makefile = self.root / ctx.cwd / "Makefile"
            if not makefile.is_file():
                self.diagnostic(ctx, file, line, "missing Makefile for target")
            else:
                target = next(
                    (a for a in argv[1:] if not a.startswith("-") and "=" not in a),
                    None,
                )
                active = False
                found = False
                for number, raw in enumerate(makefile.read_text().splitlines(), 1):
                    rule = re.match(r"^([\w.-]+):([^=]*)$", raw)
                    if rule:
                        target = target or rule[1]
                        active = rule[1] == target
                        found |= active
                        if active and rule[2].strip():
                            self.diagnostic(
                                ctx,
                                str(makefile.relative_to(self.root)),
                                number,
                                "make prerequisites require review",
                            )
                    elif active and raw.startswith("\t"):
                        self.shell(
                            raw.lstrip("\t@-+"),
                            replace(ctx, chain=ctx.chain + (f"make:{target}",)),
                            str(makefile.relative_to(self.root)),
                            number,
                        )
                if not found:
                    self.diagnostic(
                        ctx, file, line, f"unresolved make target: {target}"
                    )
        elif argv[0].endswith((".sh", ".py", ".mjs", ".js")):
            self.follow(argv[0], ctx, file, line)
        elif "$" in argv[0] or argv[0].startswith("<"):
            self.diagnostic(ctx, file, line, f"dynamic executable: {argv[0]}")

    def shell(self, text: str, ctx: Context, file: str, start: int = 1):
        # Preserve logical start lines; heredoc bodies are parsed as the invoked
        # language, never grepped as shell (cat <<EOF is inert generated text).
        lines = text.splitlines()
        env = dict(ctx.env)
        arrays: dict[str, list[str]] = {}
        i = 0
        function = ""
        while i < len(lines):
            line = start + i
            raw = lines[i]
            i += 1
            if raw.lstrip().startswith("#"):
                continue
            while raw.endswith("\\") and i < len(lines):
                raw = raw + "\n" + lines[i].lstrip()
                i += 1
            if "<<" not in raw:
                while i < len(lines):
                    try:
                        shlex.split(raw, comments=True)
                        break
                    except ValueError as exc:
                        if "No closing quotation" not in str(exc):
                            break
                        raw += "\n" + lines[i]
                        i += 1
            # Resolve the standard script-relative dirname expression without
            # executing shell. Leave all other substitutions symbolic.
            raw = re.sub(
                r'\$\(dirname\s+["\']?\$\{BASH_SOURCE\[0\]\}["\']?\)',
                str(self.root / Path(file).parent),
                raw,
            )
            raw = self.substitutions(
                raw, replace(ctx, env=tuple(sorted(env.items()))), file, line
            )
            if (
                "GITHUB_ENV" in raw
                and ">" in raw
                and any(v in raw for v in CACHE_ENV.values())
            ):
                self.diagnostic(
                    ctx,
                    file,
                    line,
                    "cache environment written through GITHUB_ENV requires review",
                )
            hd = re.search(r"<<-?\s*['\"]?(\w+)['\"]?", raw)
            if hd:
                body = []
                while i < len(lines) and lines[i].strip() != hd[1]:
                    body.append(lines[i])
                    i += 1
                i += 1
                if re.search(r"\bpython[\d.]*\b", raw):
                    self.python(
                        "\n".join(body),
                        replace(ctx, env=tuple(sorted(env.items()))),
                        file,
                        line,
                    )
                elif re.match(r"\s*(?:bash|sh)\b", raw):
                    self.shell("\n".join(body), ctx, file, line + 1)
                elif re.match(r"\s*cat\b", raw):
                    output = re.search(r">\s*[\"']?([^\s\"'<>]+)", raw)
                    if output:
                        target = (
                            self.root / ctx.cwd / expand(output[1], env)
                        ).resolve()
                        if target.is_relative_to(self.root):
                            self.generated[str(target.relative_to(self.root))] = (
                                "\n".join(body),
                                file,
                                line + 1,
                            )
                else:
                    self.diagnostic(ctx, file, line, "unsupported heredoc execution")
                raw = raw[: hd.start()]
            match = re.match(r"\s*(?:function\s+)?(\w+)\s*\(\)\s*\{", raw)
            if match:
                function = match[1]
                raw = raw[match.end() :]
            if raw.strip() == "}":
                function = ""
                continue
            array = re.match(r"\s*(\w+)=\((.*)\)\s*$", raw)
            if array:
                try:
                    arrays[array[1]] = shlex.split(array[2], comments=True)
                except ValueError:
                    self.diagnostic(ctx, file, line, "unresolved shell argv array")
                continue
            try:
                lex = shlex.shlex(
                    raw.replace("\\\n", " "), posix=True, punctuation_chars=";&|()"
                )
                lex.whitespace_split = True
                lex.commenters = ""
                tokens = list(lex)
            except ValueError:
                self.diagnostic(ctx, file, line, "unsupported shell quoting")
                continue
            statements: list[list[str]] = [[]]
            for token in tokens:
                if token.startswith("#"):
                    break
                if token and all(c in ";&|()" for c in token):
                    statements.append([])
                else:
                    statements[-1].append(token)
            for argv in statements:
                while argv and argv[0] in {
                    "if",
                    "then",
                    "elif",
                    "else",
                    "do",
                    "!",
                    "{",
                }:
                    argv.pop(0)
                if not argv:
                    continue
                if argv[0] in {"fi", "done", "for", "while", "case", "esac"}:
                    continue
                if argv[0] in {"export", "local", "readonly"}:
                    argv = argv[1:]
                if argv and all(re.match(r"^\w+=", a) for a in argv):
                    for item in argv:
                        k, v = item.split("=", 1)
                        env[k] = expand(v, env)
                    continue
                argv = [expand(a, env) for a in argv]
                if argv and argv[0] == "cd" and len(argv) > 1:
                    target = Path(ctx.cwd) / argv[1]
                    if target.is_absolute() and target.is_relative_to(self.root):
                        target = target.relative_to(self.root)
                    ctx = replace(ctx, cwd=str(target))
                    continue
                if argv:
                    arr = re.fullmatch(r"\$\{(\w+)\[@\]\}", argv[0])
                    if arr and arr[1] in arrays:
                        argv = arrays[arr[1]] + argv[1:]
                    found = raw.find(argv[0])
                    command_line = line + raw[: max(0, found)].count("\n")
                    updated = self.command(
                        argv,
                        replace(ctx, env=tuple(sorted(env.items()))),
                        file,
                        command_line,
                        function,
                    )
                    if updated:
                        env.update(updated)
        return env

    def substitutions(self, raw: str, ctx: Context, file: str, line: int) -> str:
        # Nested $() executes even inside double quotes, but never in a single
        # quoted instruction or comment. Return a placeholder after inventory.
        out = ""
        i = 0
        quote = ""
        while i < len(raw):
            c = raw[i]
            if c == "\\" and i + 1 < len(raw):
                out += raw[i : i + 2]
                i += 2
                continue
            if c in {"'", '"'}:
                if not quote:
                    quote = c
                elif quote == c:
                    quote = ""
            if c == "#" and not quote and (i == 0 or raw[i - 1].isspace()):
                break
            if raw[i : i + 2] == "$(" and quote != "'":
                depth, j, inner_quote = 1, i + 2, ""
                while j < len(raw) and depth:
                    v = raw[j]
                    if v == "\\":
                        j += 2
                        continue
                    if v in {"'", '"'}:
                        inner_quote = (
                            ""
                            if inner_quote == v
                            else v
                            if not inner_quote
                            else inner_quote
                        )
                    if not inner_quote:
                        depth += (v == "(") - (v == ")")
                    j += 1
                if depth:
                    self.diagnostic(ctx, file, line, "unclosed shell substitution")
                    return raw
                self.shell(raw[i + 2 : j - 1], ctx, file, line + raw[:i].count("\n"))
                out += "<substitution>"
                i = j
                continue
            out += c
            i += 1
        return out

    def dockerfile(self, text: str, ctx: Context, file: str):
        lines = text.splitlines()
        i = 0
        while i < len(lines):
            start = i + 1
            raw = lines[i]
            i += 1
            while raw.endswith("\\") and i < len(lines):
                raw = raw[:-1] + "\n" + lines[i]
                i += 1
            parts = raw.strip().split(None, 1)
            if not parts or raw.lstrip().startswith("#"):
                continue
            op = parts[0].upper()
            body = parts[1] if len(parts) > 1 else ""
            if op in {"FROM", "ADD"}:
                self.emit([op, *shlex.split(body)], ctx, file, start)
            elif op == "RUN":
                if body.startswith("["):
                    self.command(json.loads(body), ctx, file, start)
                else:
                    self.shell(body, ctx, file, start)
            elif op == "ENV":
                env = dict(ctx.env)
                for item in shlex.split(body):
                    if "=" in item:
                        k, v = item.split("=", 1)
                        env[k] = v
                ctx = replace(ctx, env=tuple(sorted(env.items())))
