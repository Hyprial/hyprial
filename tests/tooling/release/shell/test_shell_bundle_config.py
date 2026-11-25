"""shell-6.md §D.4: the runtime enters the bundle only through the overlay.

This file was ``test_shell_workflow.py``, which also pinned the manual
candidate workflow ``shell-macos-candidate.yml``. Owner decision 2026-11-20:
packaging leaves CI entirely and both packaging workflows are deleted (no
workflow packages, ``signing/test_signing_only_by_switch.py``); the package
is built by running ``scripts/build/package-macos-shell.sh`` by hand
(``packaging/clients/README.md`` "Package by hand"). What stays here are the
bundle configuration checks the script relies on; the properties of the
build itself (pins, Node 24, order, collect) are asserted on the script in
``candidate/test_package_script.py``.

The "Package by hand" commands are pinned here too (PR #2980 re-review,
items 4, 5 and 7): both command blocks check that the source commit is on
the mainline before ``git checkout`` (a documented operator step, the script
never contacts the remote), name the ``origin/main`` variant for a release,
pass no ``--notary-timeout-minutes`` (sign_bundle's default applies) and say
which output counts.
"""

from __future__ import annotations

import json
import re

from tests.tooling.release.shell.support import PACKAGING, ROOT

TAURI_CONF = ROOT / "clients" / "native" / "src-tauri" / "tauri.conf.json"


def test_runtime_enters_the_bundle_only_through_the_overlay() -> None:
    overlay = json.loads((PACKAGING / "tauri.bundle.json").read_text(encoding="utf-8"))
    assert overlay == {"bundle": {"resources": {"runtime/": "runtime/"}}}
    config = json.loads(TAURI_CONF.read_text(encoding="utf-8"))
    assert "resources" not in config["bundle"]
    assert config["bundle"]["macOS"]["signingIdentity"] == "-"
    assert config["bundle"]["macOS"]["minimumSystemVersion"] == "13.0"


def test_the_minimum_system_version_matches_the_wheel_and_the_readme() -> None:
    lock = json.loads((PACKAGING / "runtime-lock.json").read_text(encoding="utf-8"))
    assert lock["targets"]["aarch64-apple-darwin"]["wheelPlatform"] == "macosx_13_0_arm64"
    readme = (PACKAGING / "README.md").read_text(encoding="utf-8")
    assert "minimumSystemVersion" in readme and "13.0" in readme


ANCESTRY = "git fetch origin && git merge-base --is-ancestor <source-sha> origin/dev || exit 1"


def _package_by_hand() -> str:
    readme = (PACKAGING / "README.md").read_text(encoding="utf-8")
    return readme.split("\n## Package by hand\n", 1)[1].split("\n## ", 1)[0]


def test_both_manual_commands_check_the_mainline_before_the_checkout_and_use_the_default_notary_wait() -> None:
    section = _package_by_hand()
    blocks = [block for block in re.findall(r"```sh\n(.*?)```", section, re.S) if "package-macos-shell.sh" in block]
    assert len(blocks) == 2
    for block in blocks:
        lines = block.splitlines()
        assert lines[0] == ANCESTRY and lines[1] == "git checkout --detach <source-sha>", block
        assert "--notary-timeout-minutes" not in block
    assert "git merge-base --is-ancestor <source-sha> origin/main || exit 1" in section
    assert "default 30 minutes" in section and "62 minutes" in section
    assert "only an `--out` without `NOT-SIGNED.txt` or `NOT-BUILT.txt` counts" in section
