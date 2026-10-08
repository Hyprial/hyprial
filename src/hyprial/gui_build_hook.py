"""Hatch adapter for the first-party GUI bundle and the tailcat sidecar binary.

Wheels carry both payloads: the GUI under ``hyprial/_gui`` and the Go sidecar
under ``hyprial/_bin/hyprial-tailcat[.exe]``.  sdists carry the GUI bundle plus
the sidecar *sources* (``sidecar/hyprial-tailcat``) -- building a wheel from an
sdist then needs a local Go toolchain, never a download.
"""
from pathlib import Path
import os
import runpy
import tempfile

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

bundle_tools = runpy.run_path(str(Path(__file__).parent / "shell/impl/gui/bundle.py"))
build_gui = bundle_tools["build_gui"]

tailcat_tools = runpy.run_path(
    str(Path(__file__).parent / "shell/impl/packaging/tailcat_wheel.py")
)
build_tailcat = tailcat_tools["build_tailcat"]
TailcatWheelError = tailcat_tools["TailcatWheelError"]

WHEEL_BIN_DIR = "hyprial/_bin"
ALLOW_MISSING_ENV = "HYPRIAL_TAILCAT_ALLOW_MISSING"


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        if version == "editable":
            return  # Source development is not a product distribution.
        root = Path(self.root)
        self._stages: list[tempfile.TemporaryDirectory] = []
        self._stage_gui(root, build_data)
        self._stage_tailcat(root, build_data)

    def _stage_gui(self, root: Path, build_data: dict) -> None:
        existing = root / "src/hyprial/_gui"
        if not (root / ".git").exists() and (existing / "release.json").is_file():
            import json
            if json.loads((existing / "release.json").read_text())["productVersion"] != self.metadata.version:
                raise ValueError("Bundled GUI belongs to another product version")
            # Already under the normal package tree in an sdist. Force-including
            # it again would add duplicate ZIP members to the wheel.
            return
        stage = tempfile.TemporaryDirectory(prefix="hyprial-product-gui-")
        self._stages.append(stage)
        bundle = Path(stage.name)
        commit, timestamp = bundle_tools["source_revision"](root)
        separator = "." if "+" in self.metadata.version else "+"
        component_version = f"{self.metadata.version}{separator}gui.{timestamp}.{commit[:12]}"
        build_gui(root, bundle, component_version, product_version=self.metadata.version)
        destination = "hyprial/_gui" if self.target_name == "wheel" else "src/hyprial/_gui"
        build_data["force_include"][str(bundle)] = destination

    def _stage_tailcat(self, root: Path, build_data: dict) -> None:
        tools = tailcat_tools
        if self.target_name != "wheel":
            # LAX(tailnet-cutover): sdists ship the Go sources instead of a
            # binary; the formal arrangement is a from-source wheel build in CI
            # with the pinned toolchain, which is exactly what these sources
            # feed once the sdist is unpacked.
            source = root / tools["SIDECAR_SOURCE_DIR"]
            build_data["force_include"][str(source)] = tools["SIDECAR_SOURCE_DIR"]
            return
        target = tools["resolve_target"]()
        stage = None
        try:
            stage = tempfile.TemporaryDirectory(prefix="hyprial-tailcat-")
            binary = build_tailcat(root, target, output=Path(stage.name))
        except TailcatWheelError as error:
            if stage is not None:
                stage.cleanup()
            if os.environ.get(ALLOW_MISSING_ENV) == "1":
                # LAX(tailnet-cutover): a Python-only development build may
                # have neither a Go toolchain nor a prebuilt sidecar; the
                # formal rule is that every published wheel carries one, so
                # release pipelines must not set this variable.
                print(
                    f"warning: {ALLOW_MISSING_ENV}=1 -- building a pure-Python "
                    f"wheel without the tailcat sidecar: {error}"
                )
                return
            raise
        self._stages.append(stage)
        build_data["force_include"][str(binary)] = (
            f"{WHEEL_BIN_DIR}/{tools['binary_name'](target)}"
        )
        build_data["pure_python"] = False
        build_data["tag"] = f"py3-none-{tools['platform_tag'](target)}"

    def finalize(self, version, build_data, artifact_path):
        for stage in getattr(self, "_stages", []):
            stage.cleanup()
