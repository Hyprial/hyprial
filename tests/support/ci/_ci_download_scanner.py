from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
from dataclasses import replace
import shlex

import yaml

import tests.support.ci.ci_workflow_policy as workflows
from tests.support.ci_download.types import (
    Context,
    Diagnostic,
    Inventory,
    Site,
    condition,
    expand,
    families,
    matrix_jobs,
)
from tests.support.ci._ci_download_scanner_commands import CommandMethods
from tests.support.ci._ci_download_scanner_sources import SourceMethods

from tests.support.ci_download.runners import (
    CONTAINER_RUNNER_LABELS,
    HOST_RUNNER_LABELS as HOST_RUNNER_LABELS,
    runner_labels,
)


class Scanner(CommandMethods, SourceMethods):
    def __init__(self, root: Path, manifest: dict | None = None):
        self.root = root.resolve()
        self.manifest = manifest or {}
        self.result = Inventory([], [], {})
        self.active: list[str] = []
        self.ordinals: Counter = Counter()
        self.emitted: set[tuple] = set()
        self.visited: set[tuple] = set()
        self.used_edges: set[int] = set()
        self.generated: dict[str, tuple[str, str, int]] = {}
        self.action_stack: set[str] = set()

    def diagnostic(self, ctx: Context, file: str, line: int, message: str):
        item = Diagnostic(
            ctx.workflow, ctx.job, ctx.step, file, line, message, ctx.chain
        )
        if item not in self.result.diagnostics:
            self.result.diagnostics.append(item)

    def emit(
        self,
        argv: list[str],
        ctx: Context,
        file: str,
        line: int,
        function: str = "",
        force: str = "",
    ):
        command = shlex.join(argv).replace(str(self.root), "${WORKSPACE}")
        for family in [force] if force else families(argv, dict(ctx.env)):
            key = (
                ctx.workflow,
                ctx.job,
                ctx.step,
                file,
                line,
                function,
                command,
                family,
                ctx.chain,
            )
            if key in self.emitted:
                continue
            self.emitted.add(key)
            ordinal_key = (
                ctx.workflow,
                ctx.job,
                ctx.step,
                file,
                function,
                command,
                family,
            )
            ordinal = self.ordinals[ordinal_key]
            self.ordinals[ordinal_key] += 1
            identity = [
                *ordinal_key,
                ordinal,
                [c.replace(str(self.root), "${WORKSPACE}") for c in ctx.chain],
            ]
            fingerprint = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
            self.result.sites.append(
                Site(
                    ctx.workflow,
                    ctx.job,
                    ctx.step,
                    ctx.index,
                    file,
                    line,
                    function,
                    ordinal,
                    command,
                    family,
                    ctx.chain,
                    ctx.env,
                    ctx.condition,
                    file == ctx.workflow,
                    ctx.builder,
                    fingerprint,
                )
            )

    def scan(self, documents: dict[str, str] | None = None) -> Inventory:
        if documents is None:
            # Shared discovery, including future .yaml files; no filename list.
            if self.root == workflows.ROOT.resolve():
                paths = workflows.workflow_files()
            else:
                paths = sorted((self.root / ".forgejo/workflows").glob("*.y*ml"))
            documents = {str(p.relative_to(self.root)): p.read_text() for p in paths}
        for file, text in documents.items():
            self.workflow(file, text)
        for i, edge in enumerate(self.manifest.get("edges", [])):
            if i not in self.used_edges:
                ctx = Context("<manifest>", "", "", -1)
                self.diagnostic(
                    ctx, edge.get("from", "?"), 0, "dead explicit traversal edge"
                )
        return self.result

    def workflow(self, file: str, text: str, caller: Context | None = None):
        try:
            doc = yaml.safe_load(text) or {}
            node = yaml.compose(text)
        except yaml.YAMLError as exc:
            self.diagnostic(
                caller or Context(file, "", "", -1), file, 1, f"invalid YAML: {exc}"
            )
            return
        marks: dict[int, int] = {}

        def mark(n):
            marks[id(n)] = n.start_mark.line + 1
            if isinstance(n, yaml.MappingNode):
                for k, v in n.value:
                    mark(k)
                    mark(v)
            elif isinstance(n, yaml.SequenceNode):
                for v in n.value:
                    mark(v)

        mark(node)

        def mapping(n):
            return (
                {k.value: v for k, v in n.value}
                if isinstance(n, yaml.MappingNode)
                else {}
            )

        job_nodes = mapping(mapping(node).get("jobs"))
        for base_name, name, job in matrix_jobs(doc):
            if not isinstance(job, dict):
                continue
            ctx = Context(
                file, str(name), "<job>", -1, chain=caller.chain if caller else ()
            )
            env = {k: str(v) for k, v in (doc.get("env") or {}).items()}
            env.update({k: str(v) for k, v in (job.get("env") or {}).items()})
            env.update({"GITHUB_WORKSPACE": str(self.root)})
            cwd = (
                job.get("defaults", {})
                .get("run", {})
                .get(
                    "working-directory",
                    doc.get("defaults", {})
                    .get("run", {})
                    .get("working-directory", "."),
                )
            )
            ctx = replace(
                ctx,
                cwd=str(cwd),
                env=tuple(sorted(env.items())),
                condition=condition(job.get("if")),
            )
            jn = mapping(job_nodes.get(str(base_name)))
            if isinstance(job.get("strategy", {}).get("matrix"), str):
                self.diagnostic(
                    ctx,
                    file,
                    marks[id(job_nodes[str(base_name)])],
                    "unresolved dynamic matrix",
                )
            container = job.get("container")
            if container:
                self.emit(
                    [
                        "FROM",
                        str(
                            container.get("image")
                            if isinstance(container, dict)
                            else container
                        ),
                    ],
                    ctx,
                    file,
                    marks[id(jn["container"])],
                    force="IMAGE",
                )
            if "runs-on" in job:
                try:
                    labels = runner_labels(job["runs-on"])
                except ValueError as exc:
                    self.diagnostic(ctx, file, marks[id(jn["runs-on"])], str(exc))
                else:
                    for label in labels:
                        if label in CONTAINER_RUNNER_LABELS:
                            self.emit(
                                ["runner-image", label],
                                ctx,
                                file,
                                marks[id(jn["runs-on"])],
                                "implicit-image",
                                "IMAGE",
                            )
            for service, spec in (job.get("services") or {}).items():
                self.emit(
                    ["FROM", str(spec.get("image", ""))],
                    ctx,
                    file,
                    marks[id(jn["services"])],
                    f"service:{service}",
                    "IMAGE",
                )
            if "uses" in job:
                self.use(str(job["uses"]), ctx, file, marks[id(jn["uses"])])
            steps = job.get("steps") or []
            self.result.steps[(file, str(name))] = steps
            sn = jn.get("steps")
            for index, step in enumerate(steps):
                if not isinstance(step, dict):
                    continue
                sm = mapping(sn.value[index])
                se = {**env, **{k: str(v) for k, v in (step.get("env") or {}).items()}}
                sc = replace(
                    ctx,
                    step=str(step.get("id", step.get("name", f"step[{index}]"))),
                    index=index,
                    cwd=str(step.get("working-directory", cwd)),
                    env=tuple(sorted(se.items())),
                    condition=condition(step.get("if")) or ctx.condition,
                )
                if "uses" in step:
                    self.use(str(step["uses"]), sc, file, marks[id(sm["uses"])])
                if "run" in step:
                    n = sm["run"]
                    start = marks[id(n)] + (1 if n.style in {"|", ">"} else 0)
                    self.shell(str(step["run"]), sc, file, start)

    def use(self, target: str, ctx: Context, file: str, line: int):
        if not target.startswith("./"):
            self.emit(["uses", target], ctx, file, line, force="ACTION")
            return
        path = (self.root / target).resolve()
        if not path.is_relative_to(self.root) or target in self.action_stack:
            self.diagnostic(
                ctx, file, line, f"unresolved or recursive local action: {target}"
            )
            return
        if path.is_file() and path.suffix in {".yaml", ".yml"}:
            self.action_stack.add(target)
            self.workflow(
                target,
                path.read_text(),
                replace(ctx, chain=ctx.chain + (f"{file}:{ctx.step}:uses {target}",)),
            )
            self.action_stack.remove(target)
            return
        action = next(
            (path / n for n in ("action.yml", "action.yaml") if (path / n).is_file()),
            None,
        )
        if action is None:
            self.diagnostic(ctx, file, line, f"missing local action: {target}")
            return
        data = yaml.safe_load(action.read_text())
        runs = data.get("runs", {})
        if runs.get("using") != "composite":
            self.diagnostic(ctx, file, line, f"unsupported local action: {target}")
            return
        self.action_stack.add(target)
        action_file = str(action.relative_to(self.root))
        env = dict(ctx.env)
        env["GITHUB_ACTION_PATH"] = str(path)
        for step in runs.get("steps", []):
            child = replace(
                ctx,
                env=tuple(sorted({**env, **step.get("env", {})}.items())),
                chain=ctx.chain + (target,),
                condition=condition(step.get("if")) or ctx.condition,
            )
            if "run" in step:
                self.shell(step["run"], child, action_file, 1)
            elif "uses" in step:
                self.use(step["uses"], child, action_file, 1)
            else:
                self.diagnostic(child, action_file, 1, "unsupported composite step")
        self.action_stack.remove(target)

    def follow(self, target: str, ctx: Context, file: str, line: int):
        target = expand(target, dict(ctx.env))
        path = Path(target)
        if not path.is_absolute():
            path = self.root / ctx.cwd / path
        path = path.resolve()
        if not path.is_relative_to(self.root) or any(
            c in str(path) for c in ("$", "<", "*", "?")
        ):
            self.diagnostic(
                ctx, file, line, f"unresolved local script/build target: {target}"
            )
            return
        label = str(path.relative_to(self.root))
        if not path.is_file() and label not in self.generated:
            self.diagnostic(
                ctx, file, line, f"missing local script/build target: {label}"
            )
            return
        if label in self.active:
            self.diagnostic(ctx, file, line, f"recursive traversal: {label}")
            return
        chain = ctx.chain + (f"{file}:{ctx.step} -> {label}",)
        child = replace(ctx, chain=chain)
        key = (label, ctx.workflow, ctx.job, ctx.step, ctx.env, ctx.cwd, ctx.builder)
        if key in self.visited:
            return
        self.visited.add(key)
        self.active.append(label)
        text = (
            self.generated[label][0]
            if label in self.generated
            else path.read_text(errors="replace")
        )
        result_env = None
        if path.suffix == ".py":
            self.python(text, child, label)
        elif path.suffix in {".js", ".mjs", ".cjs", ".ts"}:
            self.javascript(text, child, label)
        elif "Dockerfile" in path.name:
            self.dockerfile(text, replace(child, builder=True, env=()), label)
        elif path.suffix in {".sh", ".bash"} or text.startswith("#!"):
            result_env = self.shell(text, child, label)
        else:
            self.diagnostic(ctx, file, line, f"unsupported executable source: {label}")
        for i, edge in enumerate(self.manifest.get("edges", [])):
            if edge.get("from") == label:
                self.used_edges.add(i)
                if not edge.get("reason", "").strip():
                    self.diagnostic(child, label, 1, "explicit edge has no reason")
                else:
                    self.follow(edge["to"], replace(child, cwd="."), label, 1)
        self.active.pop()
        return result_env
