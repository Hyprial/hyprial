"""``--mark-unfinished``: the signed copies are marked before they appear under their final names (PR #2980 review 38872).

The package script used to write ``UNFINISHED.txt`` into the signed output
after ``sign_bundle.py`` returned, so between the rename of
``<out>.unfinished`` to ``<out>`` and that write a notarized dmg sat there
unmarked, and the app copied out of the final dmg (``--extract-to``) was
never marked. With ``--mark-unfinished`` the tool itself writes the marker:
into ``<out>.unfinished`` before the rename (so ``<out>`` never exists
without it) and into the ``--extract-to`` directory before the app is copied
there. The rules on ``--out`` (must not exist) and ``--extract-to`` (a new
directory) are unchanged; the option is refused where the tool creates no
signed copy (``--check-only``, ``--verify-only`` without ``--extract-to``).
Without the option nothing is written (the tool used on its own).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.tooling.release.shell.signing.bundle.bundle_fakes import PROFILE, SHA1, Bench
from tests.tooling.release.shell.support import load

sign_bundle = load("sign_bundle")
MARKER = "UNFINISHED.txt"


@pytest.fixture
def bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Bench:
    return Bench(tmp_path).install(monkeypatch, sign_bundle)


def _verify(bench: Bench, target: Path, *extra: str) -> int:
    return sign_bundle.main(["--verify-only", "--identity", SHA1, "--out", str(bench.out), "--evidence", str(bench.evidence), "--root-parent", str(bench.tmp), "--extract-to", str(target), *extra])


def test_the_signed_output_is_marked_before_it_gets_its_final_name(bench: Bench, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[bool] = []
    rename = Path.rename

    def watched(self: Path, target: Path) -> Path:
        if self == bench.unfinished:
            seen.append((self / MARKER).is_file())
        return rename(self, target)

    monkeypatch.setattr(Path, "rename", watched)
    assert sign_bundle.main(bench.argv("--mark-unfinished")) == 0, bench.evidence_json()["problems"]
    assert seen == [True]
    assert (bench.out / MARKER).read_text(encoding="utf-8").startswith("NOT a release")
    assert MARKER not in (bench.out / "SHA256SUMS").read_text(encoding="utf-8")


def test_without_the_option_the_signed_output_is_not_marked(bench: Bench) -> None:
    assert sign_bundle.main(bench.argv()) == 0
    assert not (bench.out / MARKER).exists()


def test_the_final_copy_directory_is_marked_before_the_app_is_copied_into_it(bench: Bench) -> None:
    assert sign_bundle.main(bench.argv()) == 0
    bench.evidence.unlink()
    target = bench.tmp / "final"
    bench.knob(FAKE_FAIL="ditto")
    assert _verify(bench, target, "--mark-unfinished") == 1
    assert (target / MARKER).read_text(encoding="utf-8").startswith("NOT a release")
    bench.evidence.unlink()
    bench.knob(FAKE_FAIL="")
    other = bench.tmp / "final2"
    assert _verify(bench, other, "--mark-unfinished") == 0, bench.evidence_json()["problems"]
    assert (other / MARKER).is_file() and (other / "app" / "HyprialOS.app").is_dir()


@pytest.mark.parametrize("argv", [
    ["--check-only", "--identity", SHA1, "--notary-profile", PROFILE, "--evidence", "e.json", "--mark-unfinished"],
    ["--verify-only", "--identity", SHA1, "--out", "o", "--evidence", "e.json", "--mark-unfinished"],
])
def test_the_option_is_refused_where_no_signed_copy_is_made(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        sign_bundle.main(argv)
    assert raised.value.code == 2
