#!/usr/bin/env python3
"""Read-only baseline scanner for the five-module refactor proposal."""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tokenize
from collections import Counter, defaultdict

if __package__ in (None, ""):
    from c0.references import (
        MAX_SNIPPET,
        REPO_SCOPES,
        SRC_PREFIX,
        annotate_parents,
        dotted_name,
        evidence_line,
        git,
        in_type_checking,
        qualified_name,
        resolve_from,
        resource_only_directories,
        scan_references,
        scope_name,
        snippet,
        strongly_connected,
        type_checking_names,
        _package_for_unindexed,
    )
else:
    from .c0.references import (
        MAX_SNIPPET,
        REPO_SCOPES,
        SRC_PREFIX,
        annotate_parents,
        dotted_name,
        evidence_line,
        git,
        in_type_checking,
        qualified_name,
        resolve_from,
        resource_only_directories,
        scan_references,
        scope_name,
        snippet,
        strongly_connected,
        type_checking_names,
        _package_for_unindexed,
    )

__all__ = [
    "MAX_SNIPPET",
    "REPO_SCOPES",
    "SRC_PREFIX",
    "annotate_parents",
    "dotted_name",
    "evidence_line",
    "git",
    "in_type_checking",
    "qualified_name",
    "resolve_from",
    "resource_only_directories",
    "scan_references",
    "scope_name",
    "snippet",
    "strongly_connected",
    "type_checking_names",
    "_package_for_unindexed",
    "digest",
    "stable_write",
    "tracked_files",
    "reject_output",
    "load_mapping",
    "module_index",
    "original_class",
    "scan",
    "make_summary",
    "main",
]

EXEMPTIONS = (
    "tests/actor_boundary_exemptions.json",
    "tests/supervision_exemptions.json",
    "tests/inbox_authority_exemptions.json",
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stable_write(path: Path, data: object) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def tracked_files(root: Path) -> list[str]:
    raw = subprocess.check_output(
        ["git", "-C", str(root), "ls-files", "-z"], stderr=subprocess.PIPE
    )
    return sorted(p.decode("utf-8", "surrogateescape") for p in raw.split(b"\0") if p)


def reject_output(root: Path, output: Path) -> None:
    resolved = output.resolve()
    root_resolved = root.resolve()
    git_dir = (root_resolved / ".git").resolve()
    if resolved == root_resolved or resolved == git_dir or git_dir in resolved.parents:
        raise ValueError(
            f"output directory must be outside repository and .git: {resolved}"
        )
    if resolved.exists() and (not resolved.is_dir() or any(resolved.iterdir())):
        raise ValueError(f"output must be a new or empty directory: {resolved}")
    if resolved.is_relative_to(root_resolved) and not resolved.is_relative_to(
        root_resolved / "docs" / "evidence"
    ):
        raise ValueError("repository output is restricted to docs/evidence")
    for rel in REPO_SCOPES:
        base = (root / rel).resolve()
        if resolved == base or base in resolved.parents:
            raise ValueError(
                f"output directory must be outside scanned source folder {rel}: {resolved}"
            )


def load_mapping(path: Path) -> tuple[dict, str]:
    raw = path.read_bytes()
    mapping = json.loads(raw)
    if mapping.get("source_root") != "src/hyprial" or set(
        mapping.get("modules", {})
    ) != {"kernel", "identity", "daemon", "biz", "shell"}:
        raise ValueError(
            "mapping must cover the five original modules under src/hyprial"
        )
    seen: set[str] = set()
    for spec in mapping["modules"].values():
        for key in ("packages", "files"):
            for name in spec.get(key, []):
                if not isinstance(name, str) or "/" in name or name in seen:
                    raise ValueError(f"invalid or ambiguous mapping key: {name}")
                seen.add(name)
    if any(
        row.get("owner") not in mapping["modules"]
        for row in mapping.get("retained_paths", {}).values()
    ):
        raise ValueError("retained path has an unknown owner")
    return mapping, digest(raw)


def module_index(root: Path, files: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    modules: dict[str, str] = {}
    package_of: dict[str, str] = {}
    for rel in files:
        if not rel.startswith(SRC_PREFIX) or not rel.endswith(".py"):
            continue
        local = rel[len(SRC_PREFIX) : -3]
        if rel == SRC_PREFIX + "__init__.py":
            mod = "hyprial"
        else:
            mod = "hyprial" + (
                "." + local[: -len("/__init__")].replace("/", ".")
                if local.endswith("/__init__")
                else "." + local.replace("/", ".")
            )
        modules[mod] = rel
        package_of[rel] = (
            mod if rel.endswith("/__init__.py") else mod.rpartition(".")[0]
        )
    return modules, package_of


def original_class(rel: str, mapping: dict) -> tuple[str | None, str]:
    if rel in mapping.get("retained_paths", {}):
        return mapping["retained_paths"][rel]["owner"], "retained-entry"
    if not rel.startswith(SRC_PREFIX):
        return None, "out-of-source-map"
    local = rel[len(SRC_PREFIX) :]
    parts = local.split("/")
    top = parts[0].removesuffix(".py")
    for module, spec in mapping["modules"].items():
        if top in spec.get("packages", []):
            return module, "package"
        if len(parts) == 1 and local in spec.get("files", []):
            return module, "loose-file"
    return None, "unmapped-source"


def scan(root: Path, output: Path, mapping_path: Path) -> dict:
    reject_output(root, output)
    mapping, mapping_hash = load_mapping(mapping_path)
    files = tracked_files(root)
    head = git(root, "rev-parse", "HEAD")
    status = git(root, "status", "--porcelain=v1", "--untracked-files=no")
    modules, packages = module_index(root, files)
    outside_modules = {}
    for rel in files:
        if rel.endswith(".py") and not rel.startswith(SRC_PREFIX):
            local = rel[:-3]
            outside_modules[local.removesuffix("/__init__").replace("/", ".")] = rel
    known_modules = {**outside_modules, **modules}
    inv, py_records, parse_errors, binaries = [], {}, [], []
    source_scope_counts = Counter()
    missing_files = []
    for rel in files:
        path = root / rel
        try:
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                raise ValueError("symlink or outside-checkout input is not scanned")
            data = path.read_bytes()
        except (OSError, ValueError) as exc:
            missing_files.append({"path": rel, "error": str(exc)})
            continue
        ext = Path(rel).suffix.lower()
        is_binary = b"\0" in data[:8192]
        rec = {
            "path": rel,
            "sha256": digest(data),
            "bytes": len(data),
            "binary": is_binary,
        }
        rec["scope"] = (
            "hyprial-source"
            if rel.startswith(SRC_PREFIX)
            else next((s for s in REPO_SCOPES if rel.startswith(s + "/")), "other")
        )
        if rel.startswith(SRC_PREFIX) and ext == ".py":
            rec["module"] = next(m for m, p in modules.items() if p == rel)
            rec["mapped_owner"], rec["classification"] = original_class(rel, mapping)
        if ext == ".py":
            rec["lines"] = data.count(b"\n") + (
                1 if data and not data.endswith(b"\n") else 0
            )
            try:
                tree = ast.parse(data, filename=rel)
                annotate_parents(tree)
                rec["scope"] = (
                    "hyprial-source"
                    if rel.startswith(SRC_PREFIX)
                    else next(
                        (s for s in REPO_SCOPES if rel.startswith(s + "/")), "other"
                    )
                )
                encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
                py_records[rel] = (data.decode(encoding), tree)
                if rel.startswith(SRC_PREFIX):
                    mod, why = original_class(rel, mapping)
                    source_scope_counts[mod or why] += 1
            except (SyntaxError, UnicodeDecodeError) as exc:
                scope = (
                    "hyprial-source"
                    if rel.startswith(SRC_PREFIX)
                    else next(
                        (s for s in REPO_SCOPES if rel.startswith(s + "/")), "other"
                    )
                )
                parse_errors.append({"path": rel, "scope": scope, "error": str(exc)})
        elif is_binary:
            binaries.append(rel)
        inv.append(rec)

    imports, edge_sites, unresolved = [], defaultdict(set), []
    package_name_for = {rel: packages.get(rel, "") for rel in py_records}
    for rel, (source, tree) in py_records.items():
        source_lines = source.splitlines()
        tc_names, tc_modules = type_checking_names(tree)
        current = next((mod for mod, path in modules.items() if path == rel), None)
        package = package_name_for.get(rel, "") or _package_for_unindexed(rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                targets = [(a.name, a.asname, a.name, None) for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                base, found = resolve_from(node, current or "", package, known_modules)
                targets = []
                for alias in node.names:
                    candidate = (
                        base + "." + alias.name if base and alias.name != "*" else ""
                    )
                    package_module = bool(
                        base in known_modules
                        and known_modules[base].endswith("__init__.py")
                    ) or bool(
                        base not in known_modules
                        and base
                        and any(name.startswith(base + ".") for name in known_modules)
                    )
                    is_child = candidate in known_modules and package_module
                    target = candidate if is_child else (base or "")
                    symbol = alias.name if target == (base or "") else None
                    targets.append((target, alias.asname, base, symbol))
            else:
                continue
            site_id = f"{rel}:{node.lineno}:{node.col_offset}:{type(node).__name__}"
            site = {
                "site": site_id,
                "path": rel,
                "line": node.lineno,
                "kind": type(node).__name__,
                "scope": scope_name(node),
                "type_checking": in_type_checking(node, tc_names, tc_modules),
                "relative_level": node.level if isinstance(node, ast.ImportFrom) else 0,
                "source": evidence_line(source, node.lineno, source_lines),
            }
            for target, alias, base, symbol in targets:
                known = target in modules
                internal_prefix = (
                    target == "hyprial"
                    or target.startswith("hyprial.")
                    or (isinstance(node, ast.ImportFrom) and node.level > 0)
                )
                record = {
                    **site,
                    "target": target,
                    "alias": alias,
                    "resolved": known,
                    "resolved_tracked_outside_product": target in outside_modules,
                    "symbol": symbol,
                }
                imports.append(record)
                if known and current and target != current:
                    edge_sites[(current, target)].add(site_id)
                elif internal_prefix and not known and target not in outside_modules:
                    unresolved.append(
                        {
                            "site": site_id,
                            "target": target,
                            "base": base,
                            "symbol": symbol,
                            "classification": "unresolved-product-import"
                            if rel.startswith(SRC_PREFIX)
                            or target == "hyprial"
                            or target.startswith("hyprial.")
                            else "outside-product-package-relative-import",
                            "source": site["source"],
                        }
                    )

    source_mods = {m: p for m, p in modules.items() if p.startswith(SRC_PREFIX)}
    mapping_for_mod = {m: original_class(p, mapping)[0] for m, p in source_mods.items()}
    graph = defaultdict(set)
    for a, b in edge_sites:
        ma, mb = mapping_for_mod.get(a), mapping_for_mod.get(b)
        if ma and mb and ma != mb:
            graph[ma].add(mb)
    for mod in mapping["modules"]:
        graph.setdefault(mod, set())
    components = strongly_connected(graph)
    order = ["kernel", "identity", "daemon", "biz", "shell"]
    rank = {m: i for i, m in enumerate(order)}
    forbidden_sites = {
        site
        for (a, b), sites in edge_sites.items()
        if mapping_for_mod.get(a) in rank
        and mapping_for_mod.get(b) in rank
        and rank[mapping_for_mod[a]] < rank[mapping_for_mod[b]]
        for site in sites
    }
    edge_detail = []
    for (a, b), site_ids in sorted(edge_sites.items()):
        ma, mb = mapping_for_mod.get(a), mapping_for_mod.get(b)
        if ma and mb and ma != mb:
            sites = [
                i for i in imports if i["target"] == b and i["path"] == source_mods[a]
            ]
            edge_detail.append(
                {
                    "from_module": ma,
                    "to_module": mb,
                    "from": a,
                    "to": b,
                    "sites": len(site_ids),
                    "forbidden": rank[ma] < rank[mb],
                    "evidence": [
                        {"path": s["path"], "line": s["line"], "source": s["source"]}
                        for s in sites[:20]
                    ],
                }
            )

    output.mkdir(parents=True, exist_ok=True)
    stable_write(
        output / "inventory.json",
        {
            "head": head,
            "status": status,
            "python": sys.version,
            "mapping_sha256": mapping_hash,
            "tracked_files": inv,
            "missing_tracked_files": missing_files,
            "python_files": sum(p.endswith(".py") for p in files),
        },
    )
    site_rows = {}
    for record in imports:
        site_rows.setdefault(
            record["site"],
            {
                k: record[k]
                for k in (
                    "site",
                    "path",
                    "line",
                    "kind",
                    "scope",
                    "type_checking",
                    "relative_level",
                    "source",
                )
            },
        )
    stable_write(
        output / "imports.json",
        {
            "import_sites": [site_rows[k] for k in sorted(site_rows)],
            "target_references": imports,
            "unresolved": unresolved,
            "site_count": len(site_rows),
            "target_reference_count": len(imports),
            "module_edges": [
                {"from": a, "to": b, "sites": len(c)}
                for (a, b), c in sorted(edge_sites.items())
            ],
        },
    )
    stable_write(
        output / "boundaries.json",
        {
            "module_order": order,
            "mapped_module_edges": edge_detail,
            "forbidden_import_sites": len(forbidden_sites),
            "forbidden_target_edges_site_sum": sum(
                x["sites"] for x in edge_detail if x["forbidden"]
            ),
            "sccs": components,
        },
    )
    refs, textrefs, exports, extraction = scan_references(
        root, [r["path"] for r in inv], py_records, modules, mapping_for_mod, imports
    )
    stable_write(
        output / "references.json",
        {"python_references": refs, "tracked_text_path_references": textrefs},
    )
    stable_write(output / "exports.json", exports)
    stable_write(output / "extraction.json", extraction)
    ex = []
    for rel in EXEMPTIONS:
        p = root / rel
        if rel in {r["path"] for r in inv}:
            value = json.loads(p.read_text(encoding="utf-8"))
            entries = (
                value
                if isinstance(value, list)
                else value.get("exemptions", value.get("entries", []))
                if isinstance(value, dict)
                else []
            )
            ex.append({"path": rel, "entry_count": len(entries), "entries": entries})
        else:
            ex.append({"path": rel, "entry_count": 0, "entries": [], "missing": True})
    stable_write(
        output / "exemptions.json",
        {"manifests": ex, "total_entries": sum(x["entry_count"] for x in ex)},
    )
    unmapped = [
        {"path": p, "classification": original_class(p, mapping)[1]}
        for p in py_records
        if p.startswith(SRC_PREFIX)
        and original_class(p, mapping)[0] is None
        and original_class(p, mapping)[1] != "retained-entry"
    ]
    skipped = [
        p
        for p in files
        if Path(p).suffix.lower() == ".py"
        and p not in py_records
        and not any(e["path"] == p for e in parse_errors)
    ]
    source_parse_errors = [e for e in parse_errors if e["path"].startswith(SRC_PREFIX)]
    drifted = []
    for row in inv:
        path = root / row["path"]
        if (
            not path.is_file()
            or path.is_symlink()
            or digest(path.read_bytes()) != row["sha256"]
        ):
            drifted.append(row["path"])
    stable_inputs = (
        not drifted
        and files == tracked_files(root)
        and head == git(root, "rev-parse", "HEAD")
        and status == git(root, "status", "--porcelain=v1", "--untracked-files=no")
    )
    product_unresolved = [
        r
        for r in unresolved
        if r["target"] == "hyprial"
        or r["target"].startswith("hyprial.")
        or r["classification"] == "unresolved-product-import"
    ]
    coverage = {
        "tracked_files": len(files),
        "python_files_parsed": len(py_records),
        "parse_errors": parse_errors,
        "binary_files": binaries,
        "missing_tracked_files": missing_files,
        "unmapped_source_modules": unmapped,
        "unresolved_internal_imports": unresolved,
        "skipped_python_files": skipped,
        "resource_only_directories": resource_only_directories(files),
        "input_hashes_unchanged": stable_inputs,
        "drifted_input_paths": drifted,
        "product_scan_complete": not source_parse_errors
        and not unmapped
        and not missing_files
        and not product_unresolved
        and stable_inputs,
        "refactor_authorized_by_scan": False,
        "known_blind_spots": [
            "Static AST scan cannot establish runtime import execution or dynamic target values.",
            "Text reference classification is heuristic and preserves evidence for review.",
            "SCCs below are mapped static import SCCs; they do not establish runtime cycles.",
            "TYPE_CHECKING tags recognize direct and aliased positive guards, not rebinding or arbitrary boolean conditions.",
        ],
    }
    stable_write(output / "coverage.json", coverage)
    summary = make_summary(
        files,
        inv,
        py_records,
        imports,
        edge_detail,
        unresolved,
        parse_errors,
        unmapped,
        ex,
        components,
        textrefs,
    )
    (output / "summary.md").write_text(summary, encoding="utf-8")
    return {
        "files": len(files),
        "py": len(py_records),
        "import_sites": len({i["site"] for i in imports}),
        "target_references": len(imports),
        "unresolved": len(unresolved),
        "forbidden": len(forbidden_sites),
        "parse_errors": len(parse_errors),
        "unmapped": len(unmapped),
        "c0_ready": coverage["product_scan_complete"],
        "output": str(output),
    }


def make_summary(
    files,
    inventory,
    parsed,
    imports,
    edges,
    unresolved,
    errors,
    unmapped,
    exemptions,
    sccs,
    textrefs,
) -> str:
    srcpy = [
        p
        for p in inventory
        if p["path"].startswith(SRC_PREFIX) and p["path"].endswith(".py")
    ]
    lines = sum(p.get("lines", 0) for p in srcpy)
    forbidden = [e for e in edges if e["forbidden"]]
    return "\n".join(
        [
            "# C0 read-only refactor scan",
            "",
            f"- Tracked files: {len(files)}; parsed Python files: {len(parsed)}.",
            f"- `src/hyprial` Python files: {len(srcpy)}; source lines: {lines}.",
            f"- Import sites: {len({i['site'] for i in imports})}; target/symbol references: {len(imports)}; unresolved internal imports: {len(unresolved)}.",
            f"- Forbidden target-edge site sum: {sum(e['sites'] for e in forbidden)} across {len(forbidden)} source-module edges / {len({(e['from_module'], e['to_module']) for e in forbidden})} domain pairs; distinct statement count is in boundaries.json.",
            f"- Static mapped SCCs: {sum(len(c) > 1 for c in sccs)} cyclic components (runtime cycles are not established).",
            f"- Source parse errors: {sum(e['path'].startswith(SRC_PREFIX) for e in errors)}; unmapped source Python files: {len(unmapped)}.",
            f"- Text path-reference observations: {len(textrefs)}; exemption entries: {sum(x['entry_count'] for x in exemptions)}.",
            "",
            "## Reading the results",
            "",
            "These are candidate observations from tracked source at the captured HEAD. They are not approved rewrite rules. Dynamic imports, execution conditions, package semantics, and resource behavior need review before migration. Import-site counts count AST statements; target rows and edges preserve per imported target/symbol detail. SCCs use the proposed five-module mapping and static source imports only.",
            "",
            "Output includes bounded source evidence for import/path observations and does not store arbitrary source files.",
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--mapping",
        type=Path,
        default=Path(__file__).with_name("module-map.original.json"),
    )
    args = parser.parse_args()
    try:
        result = scan(
            args.root.resolve(), args.output.resolve(), args.mapping.resolve()
        )
    except (
        OSError,
        ValueError,
        subprocess.CalledProcessError,
        json.JSONDecodeError,
    ) as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True))
    return int(not result["c0_ready"])


if __name__ == "__main__":
    raise SystemExit(main())
