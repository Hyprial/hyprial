from __future__ import annotations

import ast
from pathlib import Path
import re
from typing import Any
from dataclasses import replace

from tests.support.ci_download.types import (
    CACHE_ENV,
    Context,
    families,
    js_close,
    js_split,
    js_tokens,
)


class SourceMethods:
    def python(self, text: str, ctx: Context, file: str, offset: int = 0):
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            self.diagnostic(
                ctx, file, (exc.lineno or 1) + offset, "unsupported Python syntax"
            )
            return
        values: dict[str, Any] = {
            "__file__": str(self.root / file),
            "sys.executable": "python",
            "ROOT": str(self.root),
            "REPO_ROOT": str(self.root),
        }

        def name(n):
            return ast.unparse(n) if n is not None else ""

        def value(n):
            if isinstance(n, ast.Constant):
                return n.value
            if isinstance(n, (ast.List, ast.Tuple)):
                out = []
                for a in n.elts:
                    if isinstance(a, ast.Starred):
                        v = value(a.value)
                        out.extend(v if isinstance(v, list) else [v])
                    else:
                        out.append(value(a))
                return out
            if isinstance(n, ast.Name):
                return values.get(n.id, f"<{n.id}>")
            if name(n) in values:
                return values[name(n)]
            if isinstance(n, ast.JoinedStr):
                return "".join(
                    str(value(v.value))
                    if isinstance(v, ast.FormattedValue)
                    else str(value(v))
                    for v in n.values
                )
            if isinstance(n, ast.BinOp):
                a, b = value(n.left), value(n.right)
                if isinstance(n.op, ast.Add):
                    if isinstance(a, list) and isinstance(b, list):
                        return a + b
                    return str(a) + str(b)
                if isinstance(n.op, ast.Div):
                    return str(Path(str(a)) / str(b))
            if isinstance(n, ast.Attribute) and n.attr == "parent":
                return str(Path(str(value(n.value))).parent)
            if (
                isinstance(n, ast.Subscript)
                and isinstance(n.value, ast.Attribute)
                and n.value.attr == "parents"
            ):
                index = value(n.slice)
                if isinstance(index, int):
                    parents = Path(str(value(n.value.value))).parents
                    if index < len(parents):
                        return str(parents[index])
            if isinstance(n, ast.IfExp):
                return value(n.body)
            if isinstance(n, ast.Call):
                fn = name(n.func)
                if fn in {"str", "Path", "pathlib.Path", "list", "tuple"} and n.args:
                    return value(n.args[0])
                if fn in {"shutil.which"} and n.args:
                    return value(n.args[0])
                if isinstance(n.func, ast.Attribute):
                    if n.func.attr == "resolve":
                        return value(n.func.value)
                    if n.func.attr == "with_name" and n.args:
                        return str(
                            Path(str(value(n.func.value))).with_name(
                                str(value(n.args[0]))
                            )
                        )
            if isinstance(n, ast.BoolOp):
                for v in n.values:
                    resolved = value(v)
                    if not str(resolved).startswith("<"):
                        return resolved
            return f"<{name(n)}>"

        aliases = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    aliases[alias.asname or alias.name] = alias.name
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"

        def qualified(n):
            raw = name(n)
            head, dot, tail = raw.partition(".")
            return aliases.get(head, head) + (dot + tail if dot else "")

        parents: dict[ast.AST, ast.AST] = {}
        for n in ast.walk(tree):
            for child in ast.iter_child_nodes(n):
                parents[child] = n

        def function(n):
            while n in parents:
                n = parents[n]
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    return n.name
            return "<module>"

        # Resolve constants before inspecting sinks, without importing code.
        for _ in range(3):
            for n in ast.walk(tree):
                if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None:
                    targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                    v = value(n.value)
                    for target in targets:
                        if isinstance(target, ast.Name):
                            values[target.id] = v
        # Cache overrides are conservatively applied throughout the helper;
        # branch-sensitive inheritance cannot establish safe coverage.
        env = dict(ctx.env)
        for n in ast.walk(tree):
            if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None:
                targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                for target in targets:
                    if (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.slice, ast.Constant)
                        and target.slice.value in CACHE_ENV.values()
                    ):
                        env[target.slice.value] = str(value(n.value))
            if isinstance(n, ast.Dict):
                for k, v in zip(n.keys, n.values):
                    if isinstance(k, ast.Constant) and k.value in CACHE_ENV.values():
                        env[k.value] = str(value(v))
        ctx = replace(ctx, env=tuple(sorted(env.items())))
        sinks = {
            "run",
            "Popen",
            "check_call",
            "check_output",
            "call",
            "create_subprocess_exec",
            "create_subprocess_shell",
            "system",
        }
        wrappers: set[str] = set()
        prefixes: dict[str, str] = {}
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for call in ast.walk(n):
                    if (
                        isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and call.func.attr in sinks
                        and name(call.func) != "asyncio.run"
                    ):
                        wrappers.add(n.name)
                        if call.args:
                            v = value(call.args[0])
                            if (
                                isinstance(v, list)
                                and v
                                and str(v[0]) in {"docker", "git", "uv", "npm", "go"}
                            ):
                                prefixes[n.name] = str(v[0])
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                fn = qualified(n.func)
                short = fn.rsplit(".", 1)[-1]
                is_sink = (
                    short in sinks
                    and (
                        (
                            fn.startswith("subprocess.")
                            or fn
                            in {
                                "os.system",
                                "asyncio.create_subprocess_exec",
                                "asyncio.create_subprocess_shell",
                            }
                        )
                        or fn in wrappers
                    )
                ) or fn in wrappers
                # Named command wrappers carry argv even when defined in an imported helper.
                is_sink = is_sink or fn in {
                    "run_bounded",
                    "docker",
                    "git",
                    "_git",
                    "run_cmd",
                    "run_command",
                }
                call_args = n.args or [
                    kw.value for kw in n.keywords if kw.arg == "args"
                ]
                if is_sink and call_args:
                    argv = value(call_args[0])
                    if fn == "asyncio.create_subprocess_exec":
                        argv = [value(a) for a in call_args]
                        if len(argv) == 1 and isinstance(call_args[0], ast.Starred):
                            argv = value(call_args[0].value)
                    if (
                        fn in prefixes
                        and isinstance(argv, list)
                        and argv
                        and argv[0] != prefixes[fn]
                    ):
                        argv = [prefixes[fn], *argv]
                    elif fn in {"docker", "git", "_git"} and isinstance(argv, list):
                        argv = [fn.lstrip("_"), *argv]
                    if isinstance(argv, list) and argv:
                        self.command(
                            [str(a) for a in argv],
                            ctx,
                            file,
                            n.lineno + offset,
                            function(n),
                        )
                    elif isinstance(argv, str) and not argv.startswith("<"):
                        self.shell(argv, ctx, file, n.lineno + offset)
                    else:
                        owner = function(n)
                        callers = [
                            c
                            for c in ast.walk(tree)
                            if isinstance(c, ast.Call)
                            and name(c.func) == owner
                            and c.args
                        ]
                        resolved_callers = callers and all(
                            isinstance(value(c.args[0]), list) for c in callers
                        )
                        if not resolved_callers:
                            self.diagnostic(
                                ctx,
                                file,
                                n.lineno + offset,
                                f"unresolved Python subprocess argv: {fn}",
                            )
                if (
                    fn.startswith(("urllib.request.", "requests.", "httpx."))
                    and short
                    in {
                        "urlopen",
                        "urlretrieve",
                        "get",
                        "post",
                        "put",
                        "request",
                        "stream",
                    }
                ) or fn == "urlopen":
                    family = "LIVE" if short in {"post", "put"} else "HTTP"
                    self.emit(
                        [fn, *(str(value(a)) for a in n.args)],
                        ctx,
                        file,
                        n.lineno + offset,
                        function(n),
                        family,
                    )
            if isinstance(n, ast.Return) and isinstance(n.value, (ast.Tuple, ast.List)):
                argv = value(n.value)
                if argv and families([str(a) for a in argv], env):
                    self.command(
                        [str(a) for a in argv],
                        ctx,
                        file,
                        n.lineno + offset,
                        function(n),
                    )
            if isinstance(n, ast.ImportFrom) and n.module:
                sibling = (
                    self.root / Path(file).parent / (n.module.replace(".", "/") + ".py")
                )
                if sibling.is_file():
                    self.follow(str(sibling), ctx, file, n.lineno + offset)
        # Static dispatch tables: follow script members only in a named scenario
        # table, not arbitrary strings, docstrings, assertions or fixture text.
        for n in ast.walk(tree):
            if isinstance(n, (ast.Assign, ast.AnnAssign)):
                targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                if any(
                    isinstance(t, ast.Name) and t.id == "SCENARIOS" for t in targets
                ):
                    for item in ast.walk(n.value):
                        if (
                            isinstance(item, ast.Constant)
                            and isinstance(item.value, str)
                            and item.value.endswith(".py")
                        ):
                            target = str(Path(file).parent / item.value)
                            self.follow(
                                target,
                                replace(ctx, cwd="."),
                                file,
                                item.lineno + offset,
                            )

    def javascript(self, text: str, ctx: Context, file: str, offset: int = 0):
        tokens = js_tokens(text)
        values: dict[str, list[str]] = {"process.execPath": ["node"]}
        env = dict(ctx.env)
        # Cache path replacement, including objects built with filtered env.
        for i, t in enumerate(tokens[:-2]):
            if t[0] in CACHE_ENV.values() and tokens[i + 1][0] in {":", "="}:
                env[t[0]] = (
                    tokens[i + 2][0]
                    if tokens[i + 2][1] == "string"
                    else "<helper-cache-override>"
                )
        ctx = replace(ctx, env=tuple(sorted(env.items())))

        def evaluate(ts):
            if not ts:
                return []
            if ts[0][0] == "[" and ts[-1][0] == "]":
                return [a for part in js_split(ts[1:-1]) for a in evaluate(part)]
            if len(ts) == 1 and ts[0][1] == "string":
                return [ts[0][0]]
            raw = "".join(t[0] for t in ts)
            if raw in values:
                return values[raw]
            # URL/path construction to a static local script, not a string grep.
            strings = [t[0] for t in ts if t[1] == "string"]
            if raw.startswith(("fileURLToPath(", "newURL(", "join(", "resolve(")):
                path = next(
                    (s for s in strings if s.endswith((".mjs", ".js", ".sh", ".py"))),
                    None,
                )
                if path:
                    if raw.startswith(("fileURLToPath(", "newURL(")):
                        path = str(Path(file).parent / path)
                        return [str(self.root / path)]
                    return [path]
            return [f"<{raw}>"]

        for i, t in enumerate(tokens[:-2]):
            if t[0] in {"const", "let", "var"} and tokens[i + 2][0] == "=":
                end = i + 3
                depth = 0
                while end < len(tokens):
                    v = tokens[end][0]
                    if depth == 0 and v in {";", ","}:
                        break
                    depth += (v in {"[", "(", "{"}) - (v in {"]", ")", "}"})
                    end += 1
                values[tokens[i + 1][0]] = evaluate(tokens[i + 3 : end])
        variadic = {
            m[1]: len(m[2].split(",")) if m[2].strip() else 0
            for m in re.finditer(r"function\s+(\w+)\s*\((.*?)\.\.\.\w+\s*\)", text)
        }
        variadic = {k: max(0, v - 1) for k, v in variadic.items()}
        wrapper_names = {
            "spawn",
            "spawnSync",
            "exec",
            "execSync",
            "execFile",
            "execFileSync",
            "run",
            "runCommand",
            "npm",
            "git",
            "docker",
        }
        # Function wrappers whose bodies invoke process primitives.
        for m in re.finditer(r"function\s+(\w+)\s*\([^)]*\)\s*\{", text):
            if re.search(
                r"\b(?:spawn|spawnSync|execFileSync)\s*\(",
                text[m.end() : text.find("\n}", m.end())],
            ):
                wrapper_names.add(m[1])
        for i, t in enumerate(tokens):
            v, kind, line = t
            if v in {"import", "from"}:
                # Static imports and import('./module.mjs'); do not follow bare dependencies.
                end = next(
                    (j for j in range(i + 1, len(tokens)) if tokens[j][0] == ";"),
                    min(i + 30, len(tokens)),
                )
                for item in tokens[i + 1 : end]:
                    if (
                        item[1] == "string"
                        and item[0].startswith(".")
                        and item[0].endswith((".js", ".mjs"))
                    ):
                        target = str(self.root / Path(file).parent / item[0])
                        self.follow(target, ctx, file, line + offset)
            if kind != "word" or i + 1 >= len(tokens) or tokens[i + 1][0] != "(":
                continue
            if i and (
                tokens[i - 1][0] == "function"
                or (
                    tokens[i - 1][0] == "."
                    and v
                    not in {
                        "spawn",
                        "spawnSync",
                        "exec",
                        "execSync",
                        "execFile",
                        "execFileSync",
                    }
                )
            ):
                continue
            end = js_close(tokens, i + 1)
            if end is None:
                self.diagnostic(ctx, file, line + offset, "unbalanced JS call")
                continue
            args = js_split(tokens[i + 2 : end])
            if v in wrapper_names and args:
                first = evaluate(args[0])
                if v in {"npm", "git", "docker"}:
                    if v in variadic and len(args) > 1:
                        argv = [
                            v,
                            *(
                                a
                                for part in args[variadic[v] :]
                                for a in evaluate(part)
                            ),
                        ]
                    else:
                        argv = [v, *first]
                elif args[0] and args[0][0][0] == "[":
                    argv = first
                elif (
                    len(args) > 1
                    and args[1]
                    and (
                        args[1][0][0] == "[" or "".join(t[0] for t in args[1]) in values
                    )
                ):
                    argv = first + evaluate(args[1])
                elif (
                    v in {"exec", "execSync"} and first and not first[0].startswith("<")
                ):
                    self.shell(first[0], ctx, file, line + offset)
                    continue
                else:
                    self.diagnostic(
                        ctx, file, line + offset, f"unresolved JS command wrapper: {v}"
                    )
                    continue
                self.command(argv, ctx, file, line + offset, v)
            elif v == "fetch":
                self.emit(
                    ["fetch", *(evaluate(args[0]) if args else [])],
                    ctx,
                    file,
                    line + offset,
                    "fetch",
                    "HTTP",
                )
