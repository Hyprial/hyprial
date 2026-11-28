"""Project a trusted P2 connection into a private local Pi launch.

Only the runtime-context builder may supply the managed home/root. Public
projections and legacy launch selection do not acquire a model binding here.
"""

import json
import stat
from pathlib import Path

from hyprial.error import codes
from hyprial.identity import AgentRuntimeContext
from hyprial.kernel import manysys
from . import KEY_ENV, PiModelBinding
from .errors import PiModelBindingError as PiModelBindingError, start_failure as start_failure


def p2_rpc_launch(*, context, spec, session_id, command, environment,
                  provider_environment, trust_receipt, extension_path,
                  model_binding, require_existing_session):
    from ..loader import find_pi_package_root, pi_sdk_launch_from_runtime_context, resolve_approved_pi_project

    trust = trust_receipt or resolve_approved_pi_project(
        cwd=spec.cwd, runtime_args=spec.args, own_workspace=context.roots.workspace_root,
        command_name=spec.name, command_model_provider=spec.model_provider,
        command_model=spec.model, command_session_ref=spec.session_ref,
    )
    return pi_sdk_launch_from_runtime_context(
        context, trust=trust, mode="rpc", session_id=session_id,
        require_existing_session=require_existing_session,
        pi_package_root=find_pi_package_root(command, environment),
        model_provider=spec.model_provider, model=spec.model,
        model_environment={**environment, **provider_environment},
        additional_extension_paths=(extension_path,), model_binding=model_binding,
    )


def bind_launch(payload: dict[str, object], environment: dict[str, str], *,
                binding: PiModelBinding, agent_home: Path, mode: str,
                session_root: object = None) -> None:
    from ..loader import pi_version_supported

    if mode != "rpc":
        raise PiModelBindingError(codes.MODEL_COMBINATION_UNSUPPORTED, "admission", "mode")
    try:
        manifest = json.loads((Path(str(payload["piPackageRoot"])) / "package.json").read_text())
        version = manifest["version"]
        proven = version in {"0.85.1", "1.2.0", "1.3.0"}
        if not proven or not pi_version_supported(version):
            raise ValueError
    except (OSError, KeyError, TypeError, ValueError):
        raise PiModelBindingError(codes.MODEL_COMBINATION_UNSUPPORTED, "admission", "sdk-version") from None
    try:
        home_path = agent_home.absolute()
        path = Path(str(session_root if session_root is not None else payload["sessionRoot"])).absolute()
        if path == home_path or not path.is_relative_to(home_path):
            raise ValueError
        while path != home_path:
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or manysys.path_is_reparse(metadata):
                raise ValueError
            path = path.parent
        home = home_path.resolve(strict=True)
        root = Path(str(payload["sessionRoot"])).resolve(strict=True)
        expected = AgentRuntimeContext.session_root_for(home, "pi").resolve(strict=True)
        if root != expected or not root.is_relative_to(home) or not root.is_dir():
            raise ValueError
        path = root
        while path != home:
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or manysys.path_is_reparse(metadata):
                raise ValueError
            path = path.parent
        if not manysys.local_filesystem(root):
            raise ValueError
    except (OSError, ValueError):
        raise PiModelBindingError(codes.MODEL_COMBINATION_UNSUPPORTED, "admission", "session-storage") from None
    selection = (payload.get("modelProvider"), payload.get("model"))
    if selection not in ((None, None), (binding.provider_id, binding.model)):
        raise PiModelBindingError(codes.MODEL_COMBINATION_UNSUPPORTED, "admission", "runtime-option")
    payload.update(modelBinding=binding.snapshot(), modelProvider=binding.provider_id,
                   model=binding.model, modelDefinitions=binding.definitions(),
                   managedSessionRoot=str(root), agentHome=str(home))
    environment[KEY_ENV] = binding.key


