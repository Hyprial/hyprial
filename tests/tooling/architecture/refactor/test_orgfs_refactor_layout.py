"""The owned orgfs checkpoint must satisfy the physical source limits."""

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[4] / 'src/hyprial/daemon/impl'
OWNED = ("org", "orgfs", "transfer", "squire")


def test_owned_product_layout_limits():
    violations = []
    for package in OWNED:
        for directory in (ROOT / package, *(ROOT / package).rglob("*")):
            if not directory.is_dir() or directory.name == "__pycache__":
                continue
            files = [p for p in directory.iterdir() if p.is_file()]
            if not any(p.suffix == ".py" for p in files):
                continue
            if len(files) > 7:
                violations.append(f"{directory.relative_to(ROOT)}: {len(files)} direct files")
            for source in files:
                if source.suffix == ".py":
                    lines = len(source.read_text().splitlines())
                    if lines > 1000:
                        violations.append(f"{source.relative_to(ROOT)}: {lines} lines")
    assert not violations, "\n".join(violations)


@pytest.mark.parametrize("package", ("orgfs", "transfer", "squire"))
def test_owned_product_has_no_numbered_partition_modules(package):
    assert not [p for p in (ROOT / package).rglob("*.py") if p.stem.startswith("part0")]
