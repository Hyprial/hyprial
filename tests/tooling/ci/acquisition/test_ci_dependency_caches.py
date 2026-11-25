"""D1 policy controls enforce fixtures; the real repository remains AUDIT.

These tests never execute scanned commands, contact a runner, or edit workflows.
Synthetic runner contracts prove matching semantics, not operational persistence.
"""

from copy import deepcopy
import json
import os
from pathlib import Path
import warnings

import pytest
import yaml

import tests.support.ci.ci_download_policy as policy
from tests.support.ci_download import release_images as release_deltas
from tests.support.ci_download.release_images import (
    INTRODUCED_CI_LIGHT_MOVE_IMAGE_CONTEXTS,
    INTRODUCED_RELEASE_RUNNER_IMAGE_CONTEXTS,
    RETIRED_CI_LIGHT_MOVE_IMAGE_CONTEXTS,
    RETIRED_RELEASE_RUNNER_IMAGE_CONTEXTS,
)

WF = ".github/workflows/fixture.yml"

# Exact HEAD 1ea20ee0 pre-refactor image identities; the original >=20 floor
# remains historical evidence, not a minimum number of unnecessary downloads.
# The independent GUI intentionally retired only these four real old jobs.
ORIGINAL_IMAGE_CONTEXTS = {
    (".github/workflows/contract-impact.yml","impact","runner-image ubuntu-latest"),
    (".github/workflows/desktop-check.yml","desktop-unit","runner-image ubuntu-latest"),
    (".github/workflows/desktop-release.yml","publish","runner-image ubuntu-latest"),
    (".github/workflows/github-publish.yml","publish","runner-image ubuntu-latest"),
    (".github/workflows/gui.yml","gui-unit","runner-image ubuntu-latest"),
    (".github/workflows/gui.yml","gui-browser","runner-image ubuntu-latest"),
    (".github/workflows/gui.yml","codex-compatibility","runner-image ubuntu-latest"),
    (".github/workflows/gui.yml","dsh-latest","runner-image ubuntu-latest"),
    (".github/workflows/gui.yml","dsh-release","runner-image ubuntu-latest"),
    (".github/workflows/gui.yml","release-package","runner-image ubuntu-latest"),
    (".github/workflows/nightly-promote.yml","promote","runner-image ubuntu-latest"),
    (".github/workflows/release-tag.yml","tag","runner-image ubuntu-latest"),
    (".github/workflows/sidecar.yml","test","runner-image ubuntu-latest"),
    (".github/workflows/sidecar.yml","build","runner-image ubuntu-latest"),
    (".github/workflows/sidecar.yml","e2e-official","runner-image ubuntu-latest"),
    (".github/workflows/sidecar.yml","e2e-headscale","runner-image ubuntu-latest"),
    (".github/workflows/skill-declaration.yml","declaration","runner-image ubuntu-latest"),
    (".github/workflows/tests.yml","cli-help-inventory","runner-image ubuntu-latest"),
    (".github/workflows/tests.yml","unit","runner-image ubuntu-latest"),
    (".github/workflows/tests.yml","stable-pointer","runner-image ubuntu-latest"),
    (".github/workflows/wheel-release.yml","build-publish","runner-image ubuntu-latest"),
}
# Allen 2026-10-26: formal publication replaces the old main-push mover.
RETIRED_TRACK_IMAGE_CONTEXTS = {
    (".github/workflows/tests.yml", "stable-pointer", "runner-image ubuntu-latest"),
}
RETIRED_GUI_IMAGE_CONTEXTS = {
    (".github/workflows/gui.yml", job, "runner-image ubuntu-latest")
    for job in ("gui-browser", "codex-compatibility", "dsh-latest", "dsh-release")
}

# Keep the old HEAD inventory frozen. The Tauri integration renames the
# explicitly reviewed legacy publisher and adds one Ubuntu Rust job. The
# macos-host native build has no pre-job runner-image acquisition context.
RETIRED_TAURI_IMAGE_CONTEXTS = {
    (".github/workflows/desktop-release.yml", "publish", "runner-image ubuntu-latest"),
}
INTRODUCED_TAURI_IMAGE_CONTEXTS = {
    (".github/workflows/desktop-release.yml", "publish-reviewed-legacy", "runner-image ubuntu-latest"),
    (".github/workflows/desktop-check.yml", "desktop-rust", "runner-image ubuntu-latest"),
}

# The 2026-10-27 GUI retirement removed app and src/gui. Retire their
# unconditionally scheduled check jobs; keep the original inventory frozen.
RETIRED_DESKTOP_CHECK_IMAGE_CONTEXTS = {
    (".github/workflows/desktop-check.yml", job, "runner-image ubuntu-latest")
    for job in ("desktop-unit", "desktop-rust")
}

# CI speedup (2026-10-04): tests.yml's single `unit` gate became `checks` +
# a four-shard `pytest-shard` matrix (the `unit` name stays, as the required
# aggregator), and hosted-agent-e2e moved from the macos-host to a Linux
# container. Each is a new runner-image context, never a renamed original.
INTRODUCED_CI_SPEEDUP_IMAGE_CONTEXTS = {
    (".github/workflows/tests.yml", "checks", "runner-image ubuntu-latest"),
    *((".github/workflows/tests.yml", f"pytest-shard[shard={index}]", "runner-image ubuntu-latest")
      for index in range(1, 5)),
    (".github/workflows/hosted-agent-e2e.yml", "hosted", "runner-image ubuntu-latest"),
}

# The Tailcat Go gate (2026-10-07): sidecar.yml's Go tests left with TSNet
# and nothing replaced them; tests.yml's `tailcat` job is a new context,
# not the retired sidecar.yml `test` job renamed.
INTRODUCED_TAILCAT_GATE_IMAGE_CONTEXTS = {
    (".github/workflows/tests.yml", "tailcat", "runner-image ubuntu-latest"),
}

# The PR-hygiene fan-in (2026-10-09): a new pull_request-only job in tests.yml
# that is a `needs:` of the required `unit` aggregator (base freshness, merge
# tree, registry union, Submitted-by). A new context, not a renamed job.
INTRODUCED_PR_HYGIENE_IMAGE_CONTEXTS = {
    (".github/workflows/tests.yml", "pr-hygiene", "runner-image ubuntu-latest"),
}

# The Homepage changelog sync (2026-10-17): a new workflow_dispatch-only job
# in its own workflow adds one Ubuntu runner-image context.
INTRODUCED_CHANGELOG_SYNC_IMAGE_CONTEXTS = {
    (".github/workflows/homepage-changelog-sync.yml", "sync", "runner-image ubuntu-latest"),
}

# Main push listener requests release-tag; it is not a second track writer.
INTRODUCED_MAIN_RELEASE_IMAGE_CONTEXTS = {
    (".github/workflows/main-release.yml", "dispatch", "runner-image ubuntu-latest"),
}

# Direct R2 installer publication and its independent anonymous drift check.
INTRODUCED_INSTALLER_IMAGE_CONTEXTS = {
    (".github/workflows/install-scripts.yml", job, "runner-image ubuntu-latest")
    for job in ("publish", "drift")
}

# github-publish moved to the proxy-enabled publish-github runner (2026-10-26,
# Allen; CI runners reach github.com only intermittently): the same job keeps
# an implicit job image, now on the new label.
RETIRED_PUBLISH_RUNNER_IMAGE_CONTEXTS = {
    (".github/workflows/github-publish.yml", "publish", "runner-image ubuntu-latest"),
}
INTRODUCED_PUBLISH_RUNNER_IMAGE_CONTEXTS = {
    (".github/workflows/github-publish.yml", "publish", "runner-image publish-github"),
}

# The release-branch changelog gate (Allen 2026-10-29): a separate pull_request
# workflow the runner reaches without uv, because tests.yml does not trigger on
# release/** and its install steps do not run on a lightweight/docs-only plan.
INTRODUCED_RELEASE_CHANGELOG_IMAGE_CONTEXTS = {
    (".github/workflows/release-changelog.yml", "changelog", "runner-image ubuntu-latest"),
}

# The tsnet sidecar workflow retired with the sidecar itself (Tailcat
# cutover): the whole file is gone, so its four job contexts are retired,
# never reintroduced.
RETIRED_SIDECAR_IMAGE_CONTEXTS = {
    (".github/workflows/sidecar.yml", job, "runner-image ubuntu-latest")
    for job in ("test", "build", "e2e-official", "e2e-headscale")
}



class CiDownloadAuditSummary(UserWarning):
    """Carries the one-line audit summary into pytest's warnings summary (see below)."""


def manifest():
    return {
        "version": 1,
        "coverage": [],
        "exceptions": [],
        "edges": [],
        "runner_contracts": [],
    }


def identity(site):
    return {
        key: getattr(site, key)
        for key in ("fingerprint", "workflow", "job", "file", "function", "ordinal")
    }


def exception(site):
    return {
        **identity(site),
        "reason": "Fresh remote verification is the purpose of this exact fixture call.",
        "purpose": "LIVE verification control",
        "owner": "CI policy fixture",
        "review": "Review on command or caller changes",
    }


def scan(
    tmp_path,
    command="uv sync --locked",
    *,
    steps=None,
    env=None,
    files=None,
    jobs=None,
    data=None,
):
    for name, text in (files or {}).items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    steps = steps if steps is not None else [{"id": "install", "run": command}]
    doc = {
        "jobs": jobs
        or {
            "build": {
                "runs-on": "macos-host",
                "env": env or {"UV_PYTHON_DOWNLOADS": "never"},
                "steps": steps,
            }
        }
    }
    return policy.Scanner(tmp_path, data).scan(
        {WF: yaml.safe_dump(doc, sort_keys=False)}
    )


def cover(inventory, data, family="UV", path="/cache/uv", key="uv-linux-py312-lockSHA"):
    data["runner_contracts"].append(
        {
            "id": f"fixture-{family}",
            "verified": True,
            "evidence": "synthetic-fixture://verified-mount",
            "workflow": WF,
            "job": "build",
            "paths": [path],
            "mechanism": "persistent",
            "prepare_command": "prepare-cache",
            "key": key,
        }
    )
    for site in inventory.sites:
        if site.family == family:
            data["coverage"].append(
                {
                    **identity(site),
                    "family": family,
                    "path": path,
                    "key": key,
                    "prepare_step": "cache",
                    "mechanism": "persistent",
                    "runner_contract": f"fixture-{family}",
                }
            )


def cached(
    tmp_path, *, command="uv sync --locked", family="UV", path="/cache/uv", files=None
):
    data = manifest()
    env = {policy.CACHE_ENV[family]: path, "UV_PYTHON_DOWNLOADS": "never"}
    steps = [{"id": "cache", "run": "prepare-cache"}, {"id": "install", "run": command}]
    inventory = scan(tmp_path, steps=steps, env=env, files=files)
    cover(inventory, data, family, path)
    return inventory, data


def test_url_free_uv_sync_is_uncovered(tmp_path):
    inventory = scan(tmp_path)
    errors = policy.violations(inventory, manifest())
    assert len(inventory.sites) == 1
    assert inventory.sites[0].family == "UV"
    assert any(f"{WF}::build::install {WF}:" in e and "UV" in e for e in errors)
    assert "http" not in inventory.sites[0].command


def test_preceding_matching_cache_with_key_passes(tmp_path):
    inventory, data = cached(tmp_path)
    assert not policy.violations(inventory, data)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-key",
        "empty-key",
        "empty-hash",
        "wrong-family",
        "wrong-path",
        "other-job",
        "late",
        "false",
        "condition",
        "unverified",
        "no-evidence",
        "no-prepare",
        "relaxed",
        "run-only-key",
    ],
)
def test_invalid_cache_controls_fail(tmp_path, mutation):
    inventory, data = cached(tmp_path)
    entry = data["coverage"][0]
    steps = inventory.steps[(WF, "build")]
    if mutation == "missing-key":
        del entry["key"]
    elif mutation == "empty-key":
        entry["key"] = ""
    elif mutation == "empty-hash":
        entry["key"] = "uv-${{ hashFiles('') }}"
    elif mutation == "wrong-family":
        entry["family"] = "NPM"
    elif mutation == "wrong-path":
        entry["path"] = "/unrelated"
    elif mutation == "other-job":
        inventory.steps[(WF, "other")] = [steps[0]]
        steps[0] = {"run": "echo no cache"}
    elif mutation == "late":
        steps.reverse()
    elif mutation == "false":
        steps[0]["if"] = False
    elif mutation == "condition":
        steps[0]["if"] = "github.ref == 'refs/heads/main'"
    elif mutation == "unverified":
        data["runner_contracts"][0]["verified"] = False
    elif mutation == "no-evidence":
        data["runner_contracts"][0].pop("evidence")
    elif mutation == "no-prepare":
        steps[0]["run"] = ""
    elif mutation == "relaxed":
        steps[0]["continue-on-error"] = True
    elif mutation == "run-only-key":
        entry["key"] = "${{ github.run_id }}"
    assert policy.violations(inventory, data)


def test_managed_python_needs_separate_coverage(tmp_path):
    inventory = scan(
        tmp_path,
        env={"UV_CACHE_DIR": "/cache/uv"},
        steps=[
            {"id": "cache", "run": "prepare-cache"},
            {"run": "uv sync --python 3.12"},
        ],
    )
    data = manifest()
    cover(inventory, data)
    assert {s.family for s in inventory.sites} == {"UV", "PY"}
    assert any("PY:" in e for e in policy.violations(inventory, data))


@pytest.mark.parametrize(
    "command",
    [
        "npm --prefix src/gui ci --ignore-scripts",
        "npm --prefix=src/gui ci --ignore-scripts",
    ],
)
def test_npm_prefix_does_not_hide_locked_gui_download(tmp_path, command):
    inventory, data = cached(
        tmp_path, command=command, family="NPM", path="/cache/npm"
    )
    assert {site.family for site in inventory.sites} == {"NPM"}
    assert policy.violations(inventory, manifest())
    assert not policy.violations(inventory, data)


@pytest.mark.parametrize("prefix", ["--prefix ../packages/gui", "--prefix=../packages/gui"])
def test_npm_prefix_resolves_from_actual_workflow_cwd(tmp_path, prefix):
    inventory = scan(
        tmp_path,
        files={
            "package.json": json.dumps({"scripts": {"setup": "curl ROOT_NOT_SELECTED"}}),
            "packages/gui/package.json": json.dumps({"scripts": {"setup": "node acquire.mjs"}}),
            "packages/gui/acquire.mjs": "spawnSync('npm', ['ci']);\n",
        },
        jobs={
            "build": {
                "runs-on": "macos-host",
                "defaults": {"run": {"working-directory": "work"}},
                "steps": [{"run": f"npm {prefix} run setup"}],
            }
        },
    )
    assert any(
        site.file == "packages/gui/acquire.mjs" and site.family == "NPM"
        and site.call_chain
        for site in inventory.sites
    )
    assert not any(site.family == "HTTP" for site in inventory.sites)
    assert not inventory.diagnostics
    assert policy.violations(inventory, manifest())


def test_shell_to_python_argv_go_download(tmp_path):
    files = {
        "helper.sh": "python tools/download.py\n",
        "tools/download.py": "import subprocess\ndef bounded(argv):\n    return subprocess.run(argv)\nbounded(['go', 'mod', 'download'])\n",
    }
    inventory, data = cached(
        tmp_path,
        command="bash helper.sh",
        family="GOMOD",
        path="/cache/go",
        files=files,
    )
    sites = [s for s in inventory.sites if s.family == "GOMOD"]
    assert len(sites) == 1
    assert sites[0].file == "tools/download.py"
    assert len(sites[0].call_chain) == 2
    assert policy.violations(inventory, manifest())
    # The forwarded definition is symbolic, but the call site is resolved.
    assert not policy.violations(inventory, data)


def test_npm_package_script_to_js_pnpm_needs_own_store(tmp_path):
    files = {
        "package.json": json.dumps({"scripts": {"build": "node helper.mjs"}}),
        "helper.mjs": "import {spawnSync} from 'node:child_process';\nspawnSync('pnpm', ['install']);\n",
    }
    inventory, data = cached(
        tmp_path, command="npm run build", family="NPM", path="/cache/npm", files=files
    )
    assert any(s.family == "PNPM" and s.call_chain for s in inventory.sites)
    assert policy.violations(inventory, data)
    # Only an explicitly matching effective pnpm store may cover the child.
    inventory = scan(
        tmp_path,
        steps=[{"id": "cache", "run": "prepare-cache"}, {"run": "npm run build"}],
        env={"PNPM_STORE_DIR": "/cache/pnpm"},
        files=files,
    )
    data = manifest()
    cover(inventory, data, "PNPM", "/cache/pnpm")
    assert not policy.violations(inventory, data)


@pytest.mark.parametrize(
    "file,body,command,family,path",
    [
        (
            "helper.sh",
            "export UV_CACHE_DIR=/tmp/disposable\nuv sync\n",
            "bash helper.sh",
            "UV",
            "/cache/uv",
        ),
        (
            "helper.py",
            "import os, subprocess\nos.environ['UV_CACHE_DIR']='/tmp/disposable'\nsubprocess.run(['uv','sync'])\n",
            "python helper.py",
            "UV",
            "/cache/uv",
        ),
        (
            "helper.mjs",
            "const env = {npm_config_cache: '/tmp/disposable'};\nspawnSync('npm', ['ci'], {env});\n",
            "node helper.mjs",
            "NPM",
            "/cache/npm",
        ),
    ],
)
def test_helper_cache_override_fails(tmp_path, file, body, command, family, path):
    inventory, data = cached(
        tmp_path, command=command, family=family, path=path, files={file: body}
    )
    assert inventory.sites
    assert policy.violations(inventory, data)
    assert all(dict(s.env)[policy.CACHE_ENV[family]] != path for s in inventory.sites)


def test_dockerfile_and_prejob_images_are_distinct(tmp_path):
    inventory = scan(
        tmp_path,
        files={
            "Dockerfile": "FROM python:3.12-slim\nRUN pip install uv\nRUN uv sync\n"
        },
        jobs={
            "build": {
                "runs-on": "ubuntu-latest",
                "container": {"image": "node:22"},
                "services": {"db": {"image": "postgres:17"}},
                "steps": [{"run": "docker build ."}],
            },
            "implicit": {"runs-on": "ubuntu-latest", "steps": []},
        },
    )
    assert {s.family for s in inventory.sites if s.file == "Dockerfile"} == {
        "IMAGE",
        "PIP",
        "UV",
        "PY",
    }
    assert sum(s.function == "implicit-image" for s in inventory.sites) == 2
    assert sum(s.family == "IMAGE" for s in inventory.sites) == 6
    assert policy.violations(inventory, manifest())


FAMILIES = [
    ("uv sync", "UV"),
    ("uv run --with foo python -V", "UV"),
    ("uv tool install foo", "UV"),
    ("uv tool upgrade foo", "UV"),
    ("uv pip install foo", "UV"),
    ("uv pip sync requirements.txt", "UV"),
    ("uv build", "UV"),
    ("uv lock", "UV"),
    ("uv python install 3.12", "PY"),
    ("uv venv --python 3.12", "PY"),
    ("pip install foo", "PIP"),
    ("pip3 download foo", "PIP"),
    ("python -m pip wheel foo", "PIP"),
    ("npm ci", "NPM"),
    ("npm install --package-lock-only", "NPM"),
    ("npm i foo", "NPM"),
    ("npm view foo", "NPM"),
    ("npm exec foo", "NPM"),
    ("pnpm install", "PNPM"),
    ("pnpm add foo", "PNPM"),
    ("pnpm fetch", "PNPM"),
    ("pnpm dlx foo", "PNPM"),
    ("npx foo", "NPM"),
    ("go build ./...", "GOMOD"),
    ("go test ./...", "GOMOD"),
    ("go vet ./...", "GOMOD"),
    ("go run main.go", "GOMOD"),
    ("go install tool", "GOMOD"),
    ("go mod download", "GOMOD"),
    ("go get foo", "GOMOD"),
    ("cargo build", "CARGO"),
    ("cargo test", "CARGO"),
    ("cargo check", "CARGO"),
    ("cargo fetch", "CARGO"),
    ("cargo install foo", "CARGO"),
    ("rustup toolchain install stable", "RUSTUP"),
    ("playwright install", "PW"),
    ("playwright install-deps", "APT"),
    ("node node_modules/playwright/cli.js install", "PW"),
    ("npx playwright install-deps chromium", "APT"),
    ("docker pull image", "IMAGE"),
    ("docker run image", "IMAGE"),
    ("apt-get update", "APT"),
    ("apt install foo", "APT"),
    ("apt-get download foo", "APT"),
    ("apk add foo", "APK"),
    ("apk update", "APK"),
    ("brew install foo", "BREW"),
    ("brew upgrade foo", "BREW"),
    ("brew fetch foo", "BREW"),
    ("git clone remote", "GIT"),
    ("git fetch origin", "GIT"),
    ("git submodule update --init", "GIT"),
    ('curl "$DOWNLOAD_URL"', "HTTP"),
    ('wget "$DOWNLOAD_URL"', "HTTP"),
]


@pytest.mark.parametrize("command,family", FAMILIES)
def test_each_family_uncovered_then_exact_exception(tmp_path, command, family):
    inventory = scan(tmp_path, command, env={"UV_PYTHON_DOWNLOADS": "auto"})
    assert family in {s.family for s in inventory.sites}
    assert policy.audit(inventory, manifest())["uncovered"]
    data = manifest()
    data["exceptions"] = [exception(s) for s in inventory.sites]
    assert not policy.violations(inventory, data)
    assert not policy.validate_manifest(data, inventory)


def test_new_workflow_and_called_script_discovered(tmp_path, monkeypatch):
    directory = tmp_path / ".github/workflows"
    directory.mkdir(parents=True)
    (directory / "new.yml").write_text(
        "jobs:\n  new:\n    runs-on: macos\n    steps:\n      - run: uv run --with foo python helper.py\n"
    )
    (tmp_path / "helper.py").write_text(
        "import subprocess\nsubprocess.run(['curl', url])\n"
    )
    monkeypatch.setattr(policy.workflows, "ROOT", tmp_path)
    monkeypatch.setattr(policy.workflows, "WORKFLOW_DIR", directory)
    inventory = policy.Scanner(tmp_path).scan()
    assert {s.family for s in inventory.sites} >= {"UV", "HTTP"}
    assert policy.violations(inventory, manifest())


@pytest.mark.parametrize(
    "body",
    [
        "import urllib.request\nurllib.request.urlopen(url)\n",
        "import requests\nrequests.get(url)\n",
        "import httpx\nhttpx.get(url)\n",
    ],
)
def test_variable_http_api_exact_exception(tmp_path, body):
    inventory = scan(tmp_path, "python helper.py", files={"helper.py": body})
    assert [s.family for s in inventory.sites] == ["HTTP"]
    data = manifest()
    data["exceptions"] = [exception(inventory.sites[0])]
    assert not policy.violations(inventory, data)
    for reason in ("", " ", "already installed", "*"):
        broken = deepcopy(data)
        broken["exceptions"][0]["reason"] = reason
        assert policy.violations(inventory, broken)
    broken = deepcopy(data)
    broken["exceptions"][0]["fingerprint"] = "*"
    assert policy.violations(inventory, broken)


def test_curl_array_readback(tmp_path):
    inventory = scan(
        tmp_path,
        "bash helper.sh",
        files={
            "helper.sh": 'curl_args=(curl --fail)\n"${curl_args[@]}" "$ASSET_URL"\n'
        },
    )
    assert len(inventory.sites) == 1
    assert inventory.sites[0].family == "HTTP"


def test_comments_echo_assertions_pack_not_sites(tmp_path):
    inventory = scan(
        tmp_path,
        "echo 'uv sync'\n# curl $URL\nnpm pack --ignore-scripts\npython helper.py\nnode helper.mjs",
        files={
            "helper.py": 'assert "uv sync" in text\nmessage = "curl URL"\n',
            "helper.mjs": '// spawnSync("npm", ["ci"]);\nassert.equal(text, "uv sync");\nconst message = "curl URL";\n',
        },
    )
    assert inventory.sites == []
    assert not inventory.diagnostics


def test_upload_is_live_not_dependency_download(tmp_path):
    inventory = scan(tmp_path, 'curl -X POST --data-binary @report.json "$API_URL"')
    assert [s.family for s in inventory.sites] == ["LIVE"]


@pytest.mark.parametrize(
    "command,files",
    [
        ("bash missing.sh", {}),
        ("python $TARGET", {}),
        ("node helper.mjs", {"helper.mjs": "spawnSync(command, args);\n"}),
        ("bash loop.sh", {"loop.sh": "bash loop.sh\n"}),
        ("make download", {}),
    ],
)
def test_unresolved_execution_is_explicit_diagnostic(tmp_path, command, files):
    inventory = scan(tmp_path, command, files=files)
    assert inventory.diagnostics
    assert policy.violations(inventory, manifest())


def test_unsupported_composite_and_stale_exception_fail(tmp_path):
    inventory = scan(
        tmp_path,
        steps=[{"uses": "./local"}],
        files={"local/action.yml": "runs:\n  using: node20\n  main: index.js\n"},
    )
    assert inventory.diagnostics
    data = manifest()
    data["exceptions"] = [{"fingerprint": "a" * 64, "reason": "Fresh refs"}]
    assert any("dead entry" in e for e in policy.validate_manifest(data, inventory))


def test_new_caller_not_excepted_and_line_moves_stable(tmp_path):
    inventory = scan(tmp_path, "git fetch origin")
    data = manifest()
    data["exceptions"] = [exception(inventory.sites[0])]
    moved = scan(tmp_path, "\n\ngit fetch origin")
    assert inventory.sites[0].fingerprint == moved.sites[0].fingerprint
    assert not policy.violations(moved, data)
    other = scan(
        tmp_path,
        jobs={
            "other": {
                "runs-on": "macos",
                "steps": [{"id": "install", "run": "git fetch origin"}],
            }
        },
    )
    assert policy.violations(other, data)


def test_actions_cache_and_restore_cjs_contracts(tmp_path):
    inventory, data = cached(tmp_path)
    for mechanism, step in [
        (
            "actions/cache",
            {
                "id": "cache",
                "uses": "actions/cache@v4",
                "with": {"key": "uv-linux-py312-lockSHA", "path": "/cache/uv"},
            },
        ),
        (
            "restore.cjs",
            {
                "id": "cache",
                "run": "node /tools/restore.cjs",
                "env": {
                    "INPUT_KEY": "uv-linux-py312-lockSHA",
                    "INPUT_PATH": "/cache/uv",
                },
            },
        ),
    ]:
        inventory.steps[(WF, "build")][0] = step
        data["coverage"][0]["mechanism"] = mechanism
        data["runner_contracts"][0]["mechanism"] = mechanism
        assert not policy.violations(inventory, data)
        data["runner_contracts"][0]["verified"] = False
        assert policy.violations(inventory, data)
        data["runner_contracts"][0]["verified"] = True


@pytest.fixture(scope="module")
def real_inventory():
    root = Path(__file__).resolve().parents[4]
    data = policy.load_manifest(root)
    return policy.Scanner(root, data).scan(), data


def test_real_tree_denominator_and_family_witnesses(real_inventory):
    inventory, _ = real_inventory
    assert len(inventory.lexical(True)) >= 113
    # Transitive-site denominator; dropped from 25 to 18 when the retired
    # tsnet sidecar's e2e download scripts left the tree (Tailcat cutover).
    assert len(inventory.lexical(False)) >= 18
    assert len(ORIGINAL_IMAGE_CONTEXTS) >= 20
    assert RETIRED_TRACK_IMAGE_CONTEXTS <= ORIGINAL_IMAGE_CONTEXTS
    assert RETIRED_TAURI_IMAGE_CONTEXTS <= ORIGINAL_IMAGE_CONTEXTS
    assert RETIRED_SIDECAR_IMAGE_CONTEXTS <= ORIGINAL_IMAGE_CONTEXTS
    assert RETIRED_PUBLISH_RUNNER_IMAGE_CONTEXTS <= ORIGINAL_IMAGE_CONTEXTS
    assert not INTRODUCED_TAURI_IMAGE_CONTEXTS.intersection(ORIGINAL_IMAGE_CONTEXTS)
    assert not INTRODUCED_CI_SPEEDUP_IMAGE_CONTEXTS.intersection(ORIGINAL_IMAGE_CONTEXTS)
    assert not INTRODUCED_TAILCAT_GATE_IMAGE_CONTEXTS.intersection(ORIGINAL_IMAGE_CONTEXTS)
    assert not INTRODUCED_PR_HYGIENE_IMAGE_CONTEXTS.intersection(ORIGINAL_IMAGE_CONTEXTS)
    assert not INTRODUCED_CHANGELOG_SYNC_IMAGE_CONTEXTS.intersection(ORIGINAL_IMAGE_CONTEXTS)
    assert not INTRODUCED_INSTALLER_IMAGE_CONTEXTS.intersection(ORIGINAL_IMAGE_CONTEXTS)
    assert not INTRODUCED_PUBLISH_RUNNER_IMAGE_CONTEXTS.intersection(ORIGINAL_IMAGE_CONTEXTS)
    assert not (INTRODUCED_RELEASE_CHANGELOG_IMAGE_CONTEXTS | release_deltas.INTRODUCED_NATIVE_CONFORMANCE_IMAGE_CONTEXTS | release_deltas.INTRODUCED_KP027_CANDIDATE_IMAGE_CONTEXTS | release_deltas.INTRODUCED_ISOLATED_LANE_IMAGE_CONTEXTS | release_deltas.INTRODUCED_WIP_PLAN_IMAGE_CONTEXTS | release_deltas.INTRODUCED_NATIVE_SHELL_IMAGE_CONTEXTS | release_deltas.INTRODUCED_GUI_ACCEPTANCE_IMAGE_CONTEXTS).intersection(ORIGINAL_IMAGE_CONTEXTS)
    assert RETIRED_RELEASE_RUNNER_IMAGE_CONTEXTS <= ORIGINAL_IMAGE_CONTEXTS | INTRODUCED_TAURI_IMAGE_CONTEXTS
    assert not INTRODUCED_RELEASE_RUNNER_IMAGE_CONTEXTS.intersection(ORIGINAL_IMAGE_CONTEXTS)
    assert not policy.image_disposition_errors(
        inventory,
        ORIGINAL_IMAGE_CONTEXTS | INTRODUCED_TAURI_IMAGE_CONTEXTS | INTRODUCED_CI_SPEEDUP_IMAGE_CONTEXTS
        | INTRODUCED_RELEASE_RUNNER_IMAGE_CONTEXTS
        | INTRODUCED_TAILCAT_GATE_IMAGE_CONTEXTS | INTRODUCED_PR_HYGIENE_IMAGE_CONTEXTS
        | INTRODUCED_CHANGELOG_SYNC_IMAGE_CONTEXTS | INTRODUCED_INSTALLER_IMAGE_CONTEXTS
        | INTRODUCED_PUBLISH_RUNNER_IMAGE_CONTEXTS | INTRODUCED_RELEASE_CHANGELOG_IMAGE_CONTEXTS
        | INTRODUCED_MAIN_RELEASE_IMAGE_CONTEXTS | INTRODUCED_CI_LIGHT_MOVE_IMAGE_CONTEXTS | release_deltas.INTRODUCED_CANCEL_ON_PR_CLOSE_IMAGE_CONTEXTS | release_deltas.INTRODUCED_HOSTED_RENAME_IMAGE_CONTEXTS | release_deltas.INTRODUCED_GATE_RENAME_IMAGE_CONTEXTS | release_deltas.INTRODUCED_NATIVE_CONFORMANCE_IMAGE_CONTEXTS | release_deltas.INTRODUCED_KP027_CANDIDATE_IMAGE_CONTEXTS | release_deltas.INTRODUCED_ISOLATED_LANE_IMAGE_CONTEXTS | release_deltas.INTRODUCED_WIP_PLAN_IMAGE_CONTEXTS | release_deltas.INTRODUCED_NATIVE_SHELL_IMAGE_CONTEXTS | release_deltas.INTRODUCED_GUI_ACCEPTANCE_IMAGE_CONTEXTS,
        RETIRED_GUI_IMAGE_CONTEXTS | RETIRED_TAURI_IMAGE_CONTEXTS | RETIRED_SIDECAR_IMAGE_CONTEXTS
        | RETIRED_TRACK_IMAGE_CONTEXTS | RETIRED_PUBLISH_RUNNER_IMAGE_CONTEXTS
        | RETIRED_DESKTOP_CHECK_IMAGE_CONTEXTS | RETIRED_RELEASE_RUNNER_IMAGE_CONTEXTS
        | RETIRED_CI_LIGHT_MOVE_IMAGE_CONTEXTS | release_deltas.RETIRED_DESKTOP_RELEASE_IMAGE_CONTEXTS | release_deltas.RETIRED_HOSTED_RENAME_IMAGE_CONTEXTS | release_deltas.RETIRED_GATE_RENAME_IMAGE_CONTEXTS,
    )
    root = Path(__file__).resolve().parents[4]
    gui_jobs = yaml.safe_load((root / ".github/workflows/gate-gui.yml").read_text())["jobs"]
    assert not {job for _, job, _ in RETIRED_GUI_IMAGE_CONTEXTS}.intersection(gui_jobs)
    for file, family, command in [
        ("contract/e2e/docker/Dockerfile", "PIP", "pip install"),
        (".github/workflows/gate-gui.yml", "NPM", "npm --prefix gui ci --ignore-scripts"),
        ("src/hyprial/daemon/impl/harnesses/agent_sdk/__init__.py", "UV", "--with"),
    ]:
        assert any(
            s.file == file and s.family == family and command in s.command
            for s in inventory.sites
        ), (file, family, command)


@pytest.mark.parametrize("mutation", ["omit", "inflate", "new-job", "invent-retirement"])
def test_image_dispositions_reject_missing_inflated_or_new_contexts(tmp_path, mutation):
    jobs = {
        "kept": {"runs-on": "ubuntu-latest", "steps": []},
    }
    if mutation == "new-job":
        jobs["new"] = {"runs-on": "ubuntu-latest", "steps": []}
    inventory = scan(tmp_path, jobs=jobs)
    original = {
        (WF, "kept", "runner-image ubuntu-latest"),
        (WF, "retired", "runner-image ubuntu-latest"),
    }
    retired = {(WF, "retired", "runner-image ubuntu-latest")}
    if mutation == "omit":
        inventory.sites.clear()
    elif mutation == "inflate":
        inventory.sites.append(inventory.sites[0])
    elif mutation == "invent-retirement":
        retired.add((WF, "not-original", "runner-image ubuntu-latest"))
    assert policy.image_disposition_errors(inventory, original, retired)
    if mutation == "new-job":
        assert {site.job for site in inventory.sites} == {"kept", "new"}


def test_image_dispositions_account_retained_and_retired_without_padding(tmp_path):
    inventory = scan(
        tmp_path, jobs={"kept": {"runs-on": "ubuntu-latest", "steps": []}}
    )
    kept = (WF, "kept", "runner-image ubuntu-latest")
    retired = (WF, "retired", "runner-image ubuntu-latest")
    assert not policy.image_disposition_errors(inventory, {kept, retired}, {retired})
    assert len(inventory.sites) == 1


def test_real_tree_download_coverage_audit(real_inventory, tmp_path):
    """Expected uncovered sites pass in D1; D5 flips MODE to enforce."""
    inventory, data = real_inventory
    report = policy.audit(inventory, data)
    destination = tmp_path / "ci-download-audit.json"
    destination.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {k: v for k, v in report.items() if k not in {"uncovered", "diagnostics"}},
            sort_keys=True,
        )
    )
    print(
        f"CI download {policy.MODE}: {destination}; review diagnostics={len(inventory.diagnostics)}"
    )
    # A passing test's stdout is captured, so the prints above never reach the CI job log:
    # in audit mode the coverage gap would be visible only to someone running this locally.
    # Two channels that survive capture on a green run:
    #   1. the full report next to the junit XML, which gate-python.yml uploads with if: always();
    #   2. one stable, greppable summary line in pytest's warnings summary.
    results = os.environ.get("RUNNER_TEMP")
    if results:
        kept = Path(results) / "pytest-results" / "ci-download-audit.json"
        kept.parent.mkdir(parents=True, exist_ok=True)
        kept.write_text(json.dumps(report, indent=2) + "\n")
    warnings.warn(
        CiDownloadAuditSummary(
            f"CI_DOWNLOADS_AUDIT=mode={policy.MODE}"
            f" direct={report['direct_sites']}"
            f" transitive={report['transitive_sites']}"
            f" uncovered={report['uncovered_count']}"
            f" diagnostics={len(inventory.diagnostics)}"
        ),
        stacklevel=1,
    )
    assert not report["manifest_errors"]
    assert policy.MODE in {"audit", "enforce"}
    if policy.MODE == "enforce":
        assert not policy.violations(inventory, data)


def test_matrix_callers_have_distinct_identity(tmp_path):
    inventory = scan(
        tmp_path,
        jobs={
            "build": {
                "runs-on": "macos",
                "strategy": {"matrix": {"tool": ["uv", "npm"]}},
                "steps": [{"run": "${{ matrix.tool }} install foo"}],
            }
        },
    )
    # uv install is not a valid uv subcommand; npm is an acquisition.
    assert any(
        s.job == "build[tool=npm]" and s.family == "NPM" for s in inventory.sites
    )
    assert len(inventory.steps) == 2


def test_local_composite_and_generated_script(tmp_path):
    inventory = scan(
        tmp_path,
        steps=[{"uses": "./action"}],
        files={
            "action/action.yml": "runs:\n  using: composite\n  steps:\n    - shell: bash\n      run: |\n        cat > generated.sh <<'EOF'\n        uv sync\n        EOF\n        bash generated.sh\n",
        },
    )
    assert any(s.family == "UV" and s.file == "generated.sh" for s in inventory.sites)
    assert not inventory.diagnostics


def test_recursive_composite_is_diagnostic(tmp_path):
    inventory = scan(
        tmp_path,
        steps=[{"uses": "./action"}],
        files={
            "action/action.yml": "runs:\n  using: composite\n  steps:\n    - uses: ./action\n"
        },
    )
    assert any("recursive" in d.message for d in inventory.diagnostics)


def test_substitutions_and_multiline_line_numbers(tmp_path):
    inventory = scan(tmp_path, 'echo "$(curl $URL)"\nvalue="$(curl -X POST $API)"\n')
    assert {s.family for s in inventory.sites} == {"HTTP", "LIVE"}
    assert len({s.line for s in inventory.sites}) == 2


def test_literal_make_recipe_is_followed(tmp_path):
    inventory = scan(
        tmp_path, "make setup", files={"Makefile": "setup:\n\tcargo fetch\n"}
    )
    assert any(s.family == "CARGO" and s.file == "Makefile" for s in inventory.sites)


def test_js_constant_argv_and_python_constant_argv(tmp_path):
    inventory = scan(
        tmp_path,
        "node helper.mjs\npython helper.py",
        files={
            "helper.mjs": "const args = ['install', '--package-lock-only'];\nspawnSync('npm', args);\n",
            "helper.py": "import subprocess\nARGS=['go','mod'] + ['download']\nsubprocess.run(ARGS)\n",
        },
    )
    assert {s.family for s in inventory.sites} == {"NPM", "GOMOD"}
    assert not inventory.diagnostics


def test_sourced_cache_override_propagates_to_parent(tmp_path):
    inventory, data = cached(
        tmp_path,
        command="source settings.sh\nuv sync",
        files={"settings.sh": "export UV_CACHE_DIR=/tmp/wrong\n"},
    )
    assert policy.violations(inventory, data)
    assert dict(inventory.sites[0].env)["UV_CACHE_DIR"] == "/tmp/wrong"


@pytest.mark.parametrize(
    "command",
    [
        "env -u PYTHONPATH uv sync",
        "sudo -u builder uv sync",
        "timeout --signal TERM 60s uv sync",
        "command uv sync",
        "sh -c 'uv sync'",
    ],
)
def test_command_wrappers_do_not_hide_family(tmp_path, command):
    inventory = scan(tmp_path, command)
    assert any(s.family == "UV" for s in inventory.sites)


def test_asyncio_variadic_and_qualified_js_spawn(tmp_path):
    inventory = scan(
        tmp_path,
        "python helper.py\nnode helper.mjs",
        files={
            "helper.py": "import asyncio\nasyncio.create_subprocess_exec('go', 'mod', 'download')\n",
            "helper.mjs": "child_process.spawnSync('cargo', ['fetch']);\n",
        },
    )
    assert {s.family for s in inventory.sites} == {"GOMOD", "CARGO"}


def test_github_env_cache_override_is_review_diagnostic(tmp_path):
    inventory = scan(tmp_path, 'echo UV_CACHE_DIR=/wrong >> "$GITHUB_ENV"')
    assert inventory.diagnostics
    assert not inventory.sites


def test_js_variadic_git_wrapper(tmp_path):
    inventory = scan(
        tmp_path,
        "node helper.mjs",
        files={
            "helper.mjs": "function git(root, ...args) { return execFileSync('git', args); }\ngit(root, 'fetch', 'origin');\n"
        },
    )
    assert any(s.family == "GIT" and "fetch" in s.command for s in inventory.sites)


def test_multiline_inline_js_is_not_shell_text(tmp_path):
    inventory = scan(
        tmp_path,
        'node -e \'\nconst instruction = "uv sync";\nspawnSync("npm", ["ci"]);\n\'',
    )
    assert [s.family for s in inventory.sites] == ["NPM"]
    assert not inventory.diagnostics


def test_seeded_copy_requires_real_preceding_copy(tmp_path):
    steps = [
        {"id": "cache", "run": "prepare-cache"},
        {"id": "seed", "run": "cp -a /cache/uv/. /tmp/seed"},
        {"id": "install", "run": "bash helper.sh"},
    ]
    inventory = scan(
        tmp_path,
        steps=steps,
        env={"UV_CACHE_DIR": "/cache/uv", "UV_PYTHON_DOWNLOADS": "never"},
        files={"helper.sh": "UV_CACHE_DIR=/tmp/seed uv sync\n"},
    )
    data = manifest()
    cover(inventory, data)
    assert policy.violations(inventory, data)
    data["coverage"][0]["seed_copy"] = {"step": "seed", "path": "/tmp/seed"}
    assert not policy.violations(inventory, data)
    inventory.steps[(WF, "build")][1]["run"] = "echo pretend copy"
    assert policy.violations(inventory, data)


def test_python_import_aliases_and_keyword_argv(tmp_path):
    inventory = scan(
        tmp_path,
        "python helper.py",
        files={
            "helper.py": "from subprocess import run as launch\nfrom urllib.request import urlopen as fetch\nlaunch(args=['go','mod','download'])\nfetch(url)\n"
        },
    )
    assert {s.family for s in inventory.sites} == {"GOMOD", "HTTP"}
    assert not inventory.diagnostics


def test_multiline_substitution_preserves_upload_flags(tmp_path):
    inventory = scan(
        tmp_path,
        'code="$(curl --fail \\\n  -H "Content-Type: application/json" \\\n  -X POST --data "$BODY" "$API_URL")"\n',
    )
    assert [s.family for s in inventory.sites] == ["LIVE"]
    assert "POST" in inventory.sites[0].command

