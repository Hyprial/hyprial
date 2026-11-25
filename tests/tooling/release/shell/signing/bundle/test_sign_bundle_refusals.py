"""sign_bundle.py refusals (exit 2) before anything is copied or signed.

Runs on every platform with the fake tools of ``bundle_fakes``. A refusal
leaves no output and no ``.unfinished`` directory, makes no ``codesign
--sign`` and no ``notarytool`` call, and still writes evidence with the
verdict REFUSED. Covered: not macOS arm64, a missing app, a malformed or
absent identity, an explicit identity that is not a valid Developer ID
Application identity, discovery that finds none or more than one, a runtime
that does not match its manifest, an output or ``.unfinished`` directory
that already exists, a malformed notary profile name. Path refusals are
usage errors (exit 2) that write nothing at all, evidence included: an
output, ``.unfinished``, evidence or launch-check path inside the input app
(after resolving symlinks), the app inside one of them, an evidence file that
already exists. A refusal's evidence and output carry no home path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.tooling.release.shell.signing.bundle.bundle_fakes import APPLE_DEV, DEVID, LEAF, OTHER_LEAF, OTHER_SHA1, SHA1, Bench, tree
from tests.tooling.release.shell.support import load

sign_bundle = load("sign_bundle")


@pytest.fixture
def bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Bench:
    return Bench(tmp_path).install(monkeypatch, sign_bundle)


def _assert_refused(bench: Bench, code: int, needle: str) -> None:
    assert code == 2
    assert "codesign-sign" not in bench.names()
    assert not any(name.startswith("notarytool") for name in bench.names())
    assert not bench.out.exists() and not bench.unfinished.exists()
    evidence = bench.evidence_json()
    assert evidence["verdict"] == "REFUSED"
    assert any(needle in problem for problem in evidence["problems"]), evidence["problems"]


@pytest.mark.parametrize(("system", "machine"), [("Linux", "x86_64"), ("Darwin", "x86_64"), ("Linux", "arm64")])
def test_refuses_off_macos_arm64(bench: Bench, system: str, machine: str) -> None:
    bench.knob(FAKE_UNAME_S=system, FAKE_UNAME_M=machine)
    _assert_refused(bench, sign_bundle.main(bench.argv()), "macOS arm64")


def test_refuses_a_missing_app_without_printing_the_home_path(bench: Bench, capsys: pytest.CaptureFixture[str]) -> None:
    argv = bench.argv()
    argv[argv.index("--app") + 1] = str(bench.tmp / "nowhere" / "HyprialOS.app")
    _assert_refused(bench, sign_bundle.main(argv), "~/nowhere/HyprialOS.app")
    printed = capsys.readouterr()
    assert "~/nowhere" in printed.out
    for text in (printed.out, printed.err, bench.evidence.read_text(encoding="utf-8")):
        assert str(bench.tmp) not in text


def _replace(argv: list[str], option: str, value: Path) -> list[str]:
    argv[argv.index(option) + 1] = str(value)
    return argv


@pytest.mark.parametrize("case", ["out", "out-through-symlink", "evidence", "root-parent", "app-inside-out", "app-inside-evidence"])
def test_refuses_paths_that_overlap_the_input_app_and_writes_nothing(bench: Bench, case: str, capsys: pytest.CaptureFixture[str]) -> None:
    inside = bench.app / "Contents" / "Resources"
    link = bench.tmp / "into-the-app"
    link.symlink_to(inside)
    argv = bench.argv()
    if case == "out":
        _replace(argv, "--out", inside / "signed")
    elif case == "out-through-symlink":
        _replace(argv, "--out", link / "signed")
    elif case == "evidence":
        _replace(argv, "--evidence", inside / "sign.json")
    elif case == "root-parent":
        _replace(argv, "--root-parent", link)
    elif case == "app-inside-out":
        _replace(argv, "--out", bench.app.parent)
    else:
        _replace(argv, "--evidence", bench.app.parent)
    before = tree(bench.app)
    with pytest.raises(SystemExit) as raised:
        sign_bundle.main(argv)
    assert raised.value.code == 2
    assert "input app" in capsys.readouterr().err
    assert bench.calls() == []
    assert tree(bench.app) == before
    assert not bench.evidence.exists() and not bench.out.exists() and not bench.unfinished.exists()


def test_refuses_an_evidence_file_that_already_exists_and_leaves_it_alone(bench: Bench) -> None:
    bench.evidence.parent.mkdir(parents=True)
    bench.evidence.write_text("an earlier run\n", encoding="utf-8")
    with pytest.raises(SystemExit) as raised:
        sign_bundle.main(bench.argv())
    assert raised.value.code == 2
    assert bench.evidence.read_text(encoding="utf-8") == "an earlier run\n"
    assert bench.calls() == []


def test_the_system_tool_path_is_used_unless_the_test_variable_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(sign_bundle.TOOL_PATH_ENV, raising=False)
    tools = sign_bundle.Tools()
    assert (tools.path, tools.overridden) == (sign_bundle.SYSTEM_PATH, False)
    monkeypatch.setenv(sign_bundle.TOOL_PATH_ENV, "/somewhere")
    assert sign_bundle.Tools().overridden is True


@pytest.mark.parametrize("identity", ["Developer ID Application: Example", "ABC", "g" * 40, "-"])
def test_refuses_an_identity_that_is_not_a_sha1(bench: Bench, identity: str) -> None:
    _assert_refused(bench, sign_bundle.main(bench.argv(identity=identity)), "SHA-1")


def test_refuses_an_identity_that_is_not_a_valid_developer_id_application(bench: Bench) -> None:
    _assert_refused(bench, sign_bundle.main(bench.argv(identity=OTHER_SHA1)), "Developer ID Application")
    bench.reset()
    _assert_refused(bench, sign_bundle.main(bench.argv(identity="C" * 40)), "Developer ID Application")


def test_requires_exactly_one_of_identity_or_discovery(bench: Bench, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        sign_bundle.main(bench.argv(identity=None))
    assert raised.value.code == 2
    with pytest.raises(SystemExit) as raised:
        sign_bundle.main(bench.argv("--discover-identity"))
    assert raised.value.code == 2


def test_discovery_picks_the_single_developer_id_application_identity(bench: Bench) -> None:
    assert sign_bundle.discover_identity(sign_bundle.Tools()) == SHA1
    assert sign_bundle.main(bench.argv("--discover-identity", identity=None)) == 0
    signs = [call["argv"] for call in bench.of("codesign-sign")]
    assert signs and all(argv[argv.index("--sign") + 1] == SHA1 for argv in signs)


@pytest.mark.parametrize("identities", [[], [[SHA1, DEVID, LEAF], [OTHER_SHA1, DEVID.replace("Example", "Other"), OTHER_LEAF]]])
def test_discovery_refuses_none_or_several(bench: Bench, identities: list) -> None:
    bench.knob(FAKE_IDENTITIES=json.dumps(identities))
    with pytest.raises(sign_bundle.Refused, match="Developer ID Application"):
        sign_bundle.discover_identity(sign_bundle.Tools())
    _assert_refused(bench, sign_bundle.main(bench.argv("--discover-identity", identity=None)), "Developer ID Application")
    assert APPLE_DEV not in json.dumps(bench.evidence_json())


def test_refuses_a_runtime_that_does_not_match_its_manifest(bench: Bench) -> None:
    init = bench.app / "Contents" / "Resources" / "runtime" / "lib" / "python3.12" / "site-packages" / "pkg" / "__init__.py"
    init.write_text("VALUE = 2\n", encoding="utf-8")
    _assert_refused(bench, sign_bundle.main(bench.argv()), "manifest")


@pytest.mark.parametrize("which", ["out", "unfinished"])
def test_refuses_an_existing_output_or_unfinished_directory(bench: Bench, which: str) -> None:
    target = bench.out if which == "out" else bench.unfinished
    target.mkdir()
    code = sign_bundle.main(bench.argv())
    assert code == 2
    assert "codesign-sign" not in bench.names()
    assert target.is_dir() and not list(target.iterdir())
    assert bench.evidence_json()["verdict"] == "REFUSED"


@pytest.mark.parametrize("profile", ["", "has space", "x;rm", "a/b"])
def test_refuses_a_malformed_notary_profile(bench: Bench, profile: str) -> None:
    argv = bench.argv()
    argv[argv.index("--notary-profile") + 1] = profile
    _assert_refused(bench, sign_bundle.main(argv), "profile")
