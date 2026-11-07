"""Shared builders for the refactor toolchain's synthetic fixture repos."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

GIT_ENV = {**os.environ, "PATH": "/opt/homebrew/bin:" + os.environ.get("PATH", "")}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class RepoFixture(unittest.TestCase):
    """A synthetic git repository + matching helpers per test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="refactor-test-")
        self.root = Path(self._tmp.name) / "repo"
        self.root.mkdir(parents=True)
        self.outside = Path(self._tmp.name) / "outside"
        self.outside.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- repository -------------------------------------------------------
    def write(self, rel: str, content: str | bytes) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def commit_all(self, message: str = "fixture") -> str:
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True, env=GIT_ENV)
        subprocess.run(["git", "add", "."], cwd=self.root, check=True, env=GIT_ENV)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Refactor",
                "-c",
                "user.email=refactor@example.invalid",
                "commit",
                "-qm",
                message,
            ],
            cwd=self.root,
            check=True,
            env=GIT_ENV,
        )
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
            env=GIT_ENV,
        ).stdout.strip()

    def git_add(self, *paths: str) -> None:
        subprocess.run(["git", "add", *paths], cwd=self.root, check=True, env=GIT_ENV)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Refactor",
                "-c",
                "user.email=refactor@example.invalid",
                "commit",
                "-qm",
                "fixture-update",
            ],
            cwd=self.root,
            check=True,
            env=GIT_ENV,
        )

    # -- manifests ---------------------------------------------------------
    def rule(self, **kwargs) -> dict:
        base = {
            "rule_id": "rule",
            "kind": "import_symbol",
            "stage": "S1",
            "status": "approved",
            "expected_matches": 1,
            "reason": "synthetic fixture rule",
        }
        base.update(kwargs)
        return base

    def manifest(self, *rules: dict, **extra) -> dict:
        manifest = {
            "schema_version": 1,
            "baseline_commit": extra.get("baseline_commit", "0" * 40),
            "rules": list(rules),
        }
        for key, value in extra.items():
            if key != "baseline_commit":
                manifest[key] = value
        return manifest

    def write_manifest(self, manifest: dict) -> Path:
        path = self.outside / f"manifest-{sha(json.dumps(manifest).encode())[:8]}.json"
        path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
        return path

    def pinned_source(self, rule: dict, rel: str) -> dict:
        rule = dict(rule)
        rule["source_path"] = rel
        rule["source_sha256"] = sha((self.root / rel).read_bytes())
        return rule

    def build_workspace(self):
        from scripts.refactor.transforms.common import Workspace
        from scripts.refactor.transforms.parsing import TransformContext

        files = sorted(
            p.relative_to(self.root).as_posix()
            for p in self.root.rglob("*")
            if p.is_file() and ".git" not in p.parts
        )
        ws = Workspace(self.root, files)
        return ws, TransformContext(ws)

    def run_outcome(self, engine_module, rule: dict, ws, ctx):
        from scripts.refactor.transforms.common import Rule

        return engine_module.apply_rule(Rule(rule), ctx)

    # -- assertions ---------------------------------------------------------
    def assertRefused(self, outcome, code: str | None = None) -> None:
        self.assertEqual(outcome.status, "refused", outcome.diagnostics)
        codes = [d.code for d in outcome.diagnostics]
        if code is not None:
            self.assertIn(code, codes, f"expected {code} in {codes}")

    def assertRead(self, rel: str) -> str:
        return (self.root / rel).read_text(encoding="utf-8")


def standard_repo(root: Path) -> None:
    """A small product-like tree used across transform tests."""
    (root / "src/hyprial/kernel").mkdir(parents=True, exist_ok=True)
    (root / "src/hyprial/agents").mkdir(parents=True, exist_ok=True)
    (root / "src/hyprial/daemon").mkdir(parents=True, exist_ok=True)
    (root / "src/hyprial/kernel/__init__.py").write_text("")
    (root / "src/hyprial/agents/__init__.py").write_text("")
    (root / "src/hyprial/daemon/__init__.py").write_text("")
    (root / "src/hyprial/__init__.py").write_text("")
