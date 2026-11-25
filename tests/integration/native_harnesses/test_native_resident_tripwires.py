"""T11's exact native ownership inventory: the union of the ``resident_metadata`` parts.

Decision record: the rows live one per line in ``resident_metadata/native_origins_*.py`` so no file
nears the structural line limit; each part states its own partition rule. This module only merges
the parts (sorted by file name, rows in file order) and fails loudly on a key present in two
parts. Parts are never imported: each part's ``.py`` source is read from disk and its single
``NATIVE_ORIGINS_PART`` dict literal is extracted with ``ast``, the exact bytes the audit reads, so
a package directory, extension module or planted bytecode cannot make the runtime inventory differ
from the audited one; a directory, symlink or stray file named like a part fails loudly.
``scripts/ci/audit_actor_rustify_ci.py`` audits the parts as one append-only inventory and
freezes this file, so a new part needs no edit here: it is discovered by its file name.
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

from .resident_metadata import HISTORICAL_ROWS as HISTORICAL_ROWS

_PART_NAME = re.compile(r"native_origins_([a-z])(?:_([a-z]))?\Z")
_PART_FILE = re.compile(r"native_origins_[a-z](?:_[a-z])?\.py\Z")
_PART_SYMBOL = "NATIVE_ORIGINS_PART"
_METADATA_DIR = Path(__file__).resolve().parent / "resident_metadata"


def _part_files(directory: Path) -> list[Path]:
    found: list[Path] = []
    for entry in sorted(os.scandir(directory), key=lambda item: item.name):
        if not entry.name.startswith("native_origins"):
            continue
        if not _PART_FILE.match(entry.name):
            raise ValueError(f"{entry.path}: not a native origins part file name")
        if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
            raise ValueError(f"{entry.path}: a part must be a regular file, not a directory or symlink")
        found.append(Path(entry.path))
    return found


def _part_literal(path: Path) -> dict:
    body = ast.parse(path.read_bytes(), filename=str(path)).body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    if not (
        len(body) == 1
        and isinstance(body[0], ast.Assign)
        and len(body[0].targets) == 1
        and isinstance(body[0].targets[0], ast.Name)
        and body[0].targets[0].id == _PART_SYMBOL
        and isinstance(body[0].value, ast.Dict)
    ):
        raise ValueError(f"{path}: a part must be a docstring plus one {_PART_SYMBOL} dict literal and nothing else")
    return ast.literal_eval(body[0].value)


def merge_parts(parts: dict[str, dict]) -> dict:
    merged: dict = {}
    owner: dict[str, str] = {}
    for name in sorted(parts):
        for key, value in parts[name].items():
            if key in merged:
                raise ValueError(f"native origin {key!r} is declared in both {owner[key]} and {name}")
            merged[key] = value
            owner[key] = name
    return merged


def load_parts(directory: Path) -> dict[str, dict]:
    return {path.stem: _part_literal(path) for path in _part_files(directory)}


def _load_parts() -> dict[str, dict]:
    return load_parts(_METADATA_DIR)


NATIVE_ORIGINS = merge_parts(_load_parts())


def partition_letter(key: str) -> str:
    segments = key.split("/")
    segment = segments[2] if key.startswith("daemon/impl/") and len(segments) > 2 else segments[0]
    return segment.lstrip("_")[:1]


def test_parts_are_discovered_and_merge_into_the_union():
    parts = _load_parts()
    assert len(parts) >= 2
    assert NATIVE_ORIGINS == merge_parts(parts)
    assert len(NATIVE_ORIGINS) == sum(len(rows) for rows in parts.values())
    assert list(NATIVE_ORIGINS) == [key for name in sorted(parts) for key in parts[name]]


def test_a_key_declared_in_two_parts_fails_loudly():
    row = ("resident", ("origin",))
    with pytest.raises(ValueError, match=r"'daemon/impl/a/x.py'.*native_origins_a.*native_origins_b_h"):
        merge_parts({"native_origins_a": {"daemon/impl/a/x.py": row}, "native_origins_b_h": {"daemon/impl/a/x.py": row}})
    assert merge_parts({"one": {"a": row}, "two": {"b": row}}) == {"a": row, "b": row}


def test_every_row_sits_in_the_part_its_partition_rule_names():
    ranges = []
    for name, rows in _load_parts().items():
        match = _PART_NAME.match(name)
        low, high = match.group(1), match.group(2) or match.group(1)
        ranges.append((low, high))
        misplaced = [key for key in rows if not low <= partition_letter(key) <= high]
        assert not misplaced, f"{name} holds rows outside {low}-{high}: {misplaced[:3]}"
    ranges.sort()
    assert ranges[0][0] == "a" and ranges[-1][1] == "z"
    for (_, high), (low, _) in zip(ranges, ranges[1:]):
        assert ord(low) == ord(high) + 1, f"parts leave a gap or overlap between {high} and {low}"


def test_every_part_file_is_a_part_module():
    assert [path.name for path in _part_files(_METADATA_DIR)] == sorted(f"{name}.py" for name in _load_parts())


def _write_part(directory: Path, name: str, rows: str, tail: str = "") -> Path:
    path = directory / f"{name}.py"
    path.write_text(f'"""Part."""\nNATIVE_ORIGINS_PART = {{{rows}}}\n{tail}', encoding="utf-8")
    return path


def test_a_same_named_package_directory_fails_loudly(tmp_path):
    _write_part(tmp_path, "native_origins_a", '"a/x.py": ("resident", ("origin",))')
    shadow = tmp_path / "native_origins_s_z"
    shadow.mkdir()
    (shadow / "__init__.py").write_text("NATIVE_ORIGINS_PART = {}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="native_origins_s_z: not a native origins part file name"):
        load_parts(tmp_path)
    shadow.rename(tmp_path / "native_origins_s_z.py")
    with pytest.raises(ValueError, match="native_origins_s_z.py: a part must be a regular file"):
        load_parts(tmp_path)


def test_a_planted_bytecode_cannot_change_what_is_read(tmp_path):
    import py_compile

    _write_part(tmp_path, "native_origins_a", '"a/x.py": ("resident", ("origin",))')
    other = tmp_path / "other.py"
    other.write_text('NATIVE_ORIGINS_PART = {"evil/y.py": ("resident", ("origin",))}\n', encoding="utf-8")
    pycache = tmp_path / "__pycache__"
    pycache.mkdir()
    py_compile.compile(
        str(other),
        cfile=str(pycache / "native_origins_a.cpython-312.pyc"),
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
    )
    assert load_parts(tmp_path) == {"native_origins_a": {"a/x.py": ("resident", ("origin",))}}


def test_a_planted_extension_or_stub_next_to_a_part_fails_loudly(tmp_path):
    _write_part(tmp_path, "native_origins_a", '"a/x.py": ("resident", ("origin",))')
    (tmp_path / "native_origins_a.cpython-312-x86_64-linux-gnu.so").write_bytes(b"")
    with pytest.raises(ValueError, match="not a native origins part file name"):
        load_parts(tmp_path)


def test_a_symlinked_part_fails_loudly(tmp_path):
    real = tmp_path / "elsewhere"
    real.mkdir()
    target = _write_part(real, "native_origins_a", '"a/x.py": ("resident", ("origin",))')
    directory = tmp_path / "parts"
    directory.mkdir()
    (directory / "native_origins_a.py").symlink_to(target)
    with pytest.raises(ValueError, match="regular file, not a directory or symlink"):
        load_parts(directory)


@pytest.mark.parametrize(
    "tail",
    ["import os\n", "X = 1\n", "NATIVE_ORIGINS_PART = {}\n", "assert True\n"],
)
def test_a_part_with_extra_code_fails_loudly(tmp_path, tail):
    _write_part(tmp_path, "native_origins_a", '"a/x.py": ("resident", ("origin",))', tail)
    with pytest.raises(ValueError, match="docstring plus one NATIVE_ORIGINS_PART dict literal"):
        load_parts(tmp_path)


def test_a_part_that_is_not_a_dict_literal_fails_loudly(tmp_path):
    (tmp_path / "native_origins_a.py").write_text('NATIVE_ORIGINS_PART = dict(a=1)\n', encoding="utf-8")
    with pytest.raises(ValueError, match="dict literal"):
        load_parts(tmp_path)


def test_a_valid_directory_loads_and_a_duplicate_key_across_files_raises(tmp_path):
    row = '"a/x.py": ("resident", ("origin",))'
    _write_part(tmp_path, "native_origins_a", row)
    _write_part(tmp_path, "native_origins_b_h", row)
    parts = load_parts(tmp_path)
    assert list(parts) == ["native_origins_a", "native_origins_b_h"]
    with pytest.raises(ValueError, match="declared in both"):
        merge_parts(parts)
