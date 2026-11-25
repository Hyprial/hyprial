#!/usr/bin/env python3
"""Build and assemble the per-target native module set for wheel assembly.

``receipt`` runs on each target host after that host built and smoke-tested
its own wheel: it extracts ``hyprial/_core/actors.*`` from the wheel, checks
the payload's format and architecture, and writes the module beside a
``receipt.json``.

``assemble`` runs once on the wheel-assembly host: it verifies every receipt
against the module bytes, the shared source identity and the requested
target set, then writes ``native-prebuilt.json`` for
``HYPRIAL_NATIVE_PREBUILT_DIR``.  The build hook consumes that directory
without a cargo toolchain or a host-only target check, so one machine can
assemble all five wheels while every module was compiled and import-checked
on its own host.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import runpy
import stat
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_NATIVE = runpy.run_path(
    str(ROOT / "src/hyprial/shell/impl/packaging/native_engine.py")
)
_TAILCAT = runpy.run_path(
    str(ROOT / "src/hyprial/shell/impl/packaging/tailcat_wheel.py")
)
_NATIVE_ERROR = _NATIVE["NativeEngineError"]
RECEIPT_SCHEMA = "hyprial.native-prebuilt-receipt/v1"
WHEEL_NAME = re.compile(
    r"^hyprial-(?P<version>[^-]+)-(?P<tag>cp312-abi3-(?P<platform>.+))\.whl$"
)


class PrebuiltError(ValueError):
    """A native module, receipt or manifest violates the assembly contract."""


def fail(message: str) -> None:
    raise PrebuiltError(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        fail(f"cannot read {path}: {error}")
    except ValueError as error:
        fail(f"{path} is not valid JSON: {error}")
    if not isinstance(document, dict):
        fail(f"{path} must contain a JSON object")
    return document


def _module_name(target: str) -> str:
    return _NATIVE["wheel_module_name"](target)


def _platform_tag(target: str) -> str:
    try:
        return _TAILCAT["platform_tag"](target)
    except ValueError as error:
        fail(str(error))


def _source_sha256() -> str:
    return _NATIVE["source_digest"](ROOT)


def _regular_module(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        fail(f"{path} is not a regular module file")
    mode = path.stat().st_mode
    if not stat.S_ISREG(mode):
        fail(f"{path} is not a regular module file")
    return path.read_bytes()


def _verify_module(path: Path, target: str, sha256: str, size: int) -> None:
    data = _regular_module(path)
    if len(data) != size:
        fail(f"{path} is {len(data)} bytes, receipt records {size}")
    actual = hashlib.sha256(data).hexdigest()
    if actual != sha256:
        fail(f"{path} sha256 is {actual}, receipt records {sha256}")
    _NATIVE["module_architecture"](path, target)
    if target.startswith("linux/"):
        _NATIVE["audit_library"](path)


def receipt(args: argparse.Namespace) -> int:
    target = args.target
    platform_tag = _platform_tag(target)
    wheel = Path(args.wheel)
    if not wheel.is_file():
        fail(f"{wheel} is not a file")
    match = WHEEL_NAME.fullmatch(wheel.name)
    if match is None:
        fail(f"{wheel.name} is not a hyprial cp312-abi3 wheel")
    if match.group("platform") != platform_tag:
        fail(
            f"{wheel.name} carries platform {match.group('platform')!r}; "
            f"{target} needs {platform_tag!r}"
        )
    module_name = _module_name(target)
    expected_member = f"hyprial/_core/{module_name}"
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = [info.filename for info in archive.infolist() if not info.is_dir()]
            members = [name for name in names if name == expected_member]
            if len(members) != 1:
                fail(f"{wheel.name} must contain exactly one {expected_member}")
            hidden = [
                name
                for name in names
                if name.startswith("hyprial/_core/")
                and Path(name).name.startswith("actors.")
                and name != expected_member
            ]
            if hidden:
                fail(f"{wheel.name} hides another actor payload: {hidden!r}")
            data = archive.read(expected_member)
    except zipfile.BadZipFile as error:
        fail(f"{wheel} is not a readable wheel: {error}")
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    module = output / module_name
    module.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    _verify_module(module, target, digest, len(data))
    if args.smoke != "pass":
        fail("receipt requires --smoke pass from the host import check")
    if args.installed_module is None:
        fail("receipt requires --installed-module from the isolated install")
    if not args.python:
        fail("receipt requires --python for the isolated import check")
    installed = Path(args.installed_module)
    _verify_module(installed, target, digest, len(data))
    version = _NATIVE["probe_abi_version"](installed, args.python)
    if version != _NATIVE["ENGINE_ABI_VERSION"]:
        fail(
            f"{installed} reports ENGINE_ABI_VERSION {version!r}; "
            f"this tree requires {_NATIVE['ENGINE_ABI_VERSION']}"
        )
    document = {
        "schema": RECEIPT_SCHEMA,
        "target": target,
        "source_sha": args.source_sha,
        "source_sha256": _source_sha256(),
        "module": module_name,
        "sha256": digest,
        "size": len(data),
        "engine_abi_version": _NATIVE["ENGINE_ABI_VERSION"],
        "platform_tag": platform_tag,
        "wheel": wheel.name,
        "wheel_sha256": sha256_file(wheel),
        "smoke": "pass",
        "installed_module": installed.name,
        "python": args.python,
    }
    (output / "receipt.json").write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        f"receipt {target}: {module_name} sha256 {digest} wheel {wheel.name}",
        file=sys.stderr,
    )
    return 0


def assemble(args: argparse.Namespace) -> int:
    directory = Path(args.directory)
    if not directory.is_dir():
        fail(f"{directory} is not a directory")
    source_sha256 = _source_sha256()
    targets: dict[str, dict] = {}
    for target in dict.fromkeys(args.target):
        folder = directory / target.replace("/", "-")
        receipt_path = folder / "receipt.json"
        document = _read_json(receipt_path)
        if document.get("schema") != RECEIPT_SCHEMA:
            fail(f"{receipt_path} declares schema {document.get('schema')!r}")
        if document.get("target") != target:
            fail(f"{receipt_path} names target {document.get('target')!r}, expected {target!r}")
        if document.get("source_sha") != args.source_sha:
            fail(
                f"{receipt_path} was built from source {document.get('source_sha')!r}, "
                f"expected {args.source_sha!r}"
            )
        if document.get("source_sha256") != source_sha256:
            fail(f"{receipt_path} was built from another crates/ digest")
        module_name = _module_name(target)
        if document.get("module") != module_name:
            fail(f"{receipt_path} names module {document.get('module')!r}")
        if document.get("engine_abi_version") != _NATIVE["ENGINE_ABI_VERSION"]:
            fail(
                f"{receipt_path} claims ENGINE_ABI_VERSION "
                f"{document.get('engine_abi_version')!r}"
            )
        platform_tag = _platform_tag(target)
        if document.get("platform_tag") != platform_tag:
            fail(
                f"{receipt_path} names platform tag {document.get('platform_tag')!r}; "
                f"{target} needs {platform_tag!r}"
            )
        if document.get("smoke") != "pass":
            fail(f"{receipt_path} has no passing host smoke test")
        if not isinstance(document.get("sha256"), str) or not re.fullmatch(
            r"[0-9a-f]{64}", document["sha256"]
        ):
            fail(f"{receipt_path} has no valid module sha256")
        if not isinstance(document.get("size"), int):
            fail(f"{receipt_path} has no valid module size")
        module = folder / module_name
        _verify_module(module, target, document["sha256"], document["size"])
        if target in targets:
            continue
        targets[target] = {
            "module": module_name,
            "sha256": document["sha256"],
            "size": document["size"],
            "engine_abi_version": document["engine_abi_version"],
            "platform_tag": platform_tag,
        }
    if not targets:
        fail("no targets requested; refusing to assemble an empty module set")
    manifest = {
        "schema": _NATIVE["PREBUILT_SCHEMA"],
        "source_sha256": source_sha256,
        "targets": targets,
    }
    path = directory / _NATIVE["PREBUILT_MANIFEST"]
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"assembled {len(targets)} targets for source_sha256 {source_sha256}",
        file=sys.stderr,
    )
    for target in sorted(targets):
        print(f"  {target}: {targets[target]['sha256']}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    receipt_parser = sub.add_parser("receipt", help="write one target receipt from a wheel")
    receipt_parser.add_argument("--wheel", required=True)
    receipt_parser.add_argument("--target", required=True)
    receipt_parser.add_argument("--source-sha", required=True)
    receipt_parser.add_argument("--output", required=True)
    receipt_parser.add_argument("--installed-module", default=None)
    receipt_parser.add_argument("--python", default="")
    receipt_parser.add_argument("--smoke", default="fail")
    receipt_parser.set_defaults(handler=receipt)

    assemble_parser = sub.add_parser("assemble", help="write native-prebuilt.json")
    assemble_parser.add_argument("--directory", required=True)
    assemble_parser.add_argument("--source-sha", required=True)
    assemble_parser.add_argument("--target", action="append", required=True)
    assemble_parser.set_defaults(handler=assemble)

    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (PrebuiltError, _NATIVE_ERROR) as error:
        print(f"native-prebuilt: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
