"""Keep the corrected login design tag."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]


def test_design_doc_tag_corrected() -> None:
    design = (ROOT / "docs" / "design" / "hyprial-login-design.md").read_text(
        encoding="utf-8"
    )
    assert "tag:hyprial-ci" in design
    assert "2026-09-05 更正" in design
