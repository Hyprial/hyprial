"""Hatch adapter for the tailcat sidecar payload.

The legacy GUI bundle staging was retired with ``src/gui`` and the ``app``
shell.  Wheels still carry the Go sidecar under
``hyprial/_bin/hyprial-tailcat[.exe]``; sdists carry the sidecar *sources*
(``sidecar/hyprial-tailcat``), so a wheel built from an sdist needs a local Go
toolchain rather than a download.
"""
from pathlib import Path
import os
import runpy
import tempfile

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

tailcat_tools = runpy.run_path(
    str(Path(__file__).parent / "shell/impl/packaging/tailcat_wheel.py")
)
build_tailcat = tailcat_tools["build_tailcat"]
TailcatWheelError = tailcat_tools["TailcatWheelError"]

WHEEL_BIN_DIR = "hyprial/_bin"
ALLOW_MISSING_ENV = "HYPRIAL_TAILCAT_ALLOW_MISSING"
WORKER_WHEEL_ENV = "HYPRIAL_BUILD_WORKER_WHEEL"


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        if version == "editable":
            return  # Source development is not a product distribution.
        root = Path(self.root)
        self._stages: list[tempfile.TemporaryDirectory] = []
        if os.environ.get(WORKER_WHEEL_ENV) == "1":
            # Thin worker wheel (containerized-transfer image): no tailcat
            # sidecar.  The image's builder stage compiles the sidecar once
            # and COPY --from=builder into /opt/hyprial/_bin, so the wheel
            # needs neither a .git context nor a Go toolchain.
            return
        self._stage_tailcat(root, build_data)

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
