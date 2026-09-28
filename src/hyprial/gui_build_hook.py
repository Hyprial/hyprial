"""Hatch adapter for the first-party GUI bundle."""
from pathlib import Path
import runpy
import tempfile

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

bundle_tools = runpy.run_path(str(Path(__file__).with_name("gui_bundle.py")))
build_gui = bundle_tools["build_gui"]


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        if version == "editable":
            return  # Source development is not a product distribution.
        root = Path(self.root)
        existing = root / "src/hyprial/_gui"
        if not (root / ".git").exists() and (existing / "release.json").is_file():
            import json
            if json.loads((existing / "release.json").read_text())["productVersion"] != self.metadata.version:
                raise ValueError("Bundled GUI belongs to another product version")
            # Already under the normal package tree in an sdist. Force-including
            # it again would add duplicate ZIP members to the wheel.
            return
        else:
            self._stage = tempfile.TemporaryDirectory(prefix="hyprial-product-gui-")
            bundle = Path(self._stage.name)
            commit, timestamp = bundle_tools["source_revision"](root)
            separator = "." if "+" in self.metadata.version else "+"
            component_version = f"{self.metadata.version}{separator}gui.{timestamp}.{commit[:12]}"
            build_gui(root, bundle, component_version, product_version=self.metadata.version)
        destination = "hyprial/_gui" if self.target_name == "wheel" else "src/hyprial/_gui"
        build_data["force_include"][str(bundle)] = destination

    def finalize(self, version, build_data, artifact_path):
        if hasattr(self, "_stage"):
            self._stage.cleanup()
