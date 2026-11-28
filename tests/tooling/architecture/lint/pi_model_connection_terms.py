"""Exact Pi SDK model-registration terms for the connection adapter.

Provider here is the external SDK registration/model vendor identifier or the
existing model-provider launch field. It never names a Hyprial harness. The
term lint applies exact path/line matching and negative mutation tests to each
entry; this is not an allowance for the directory or future source lines.
"""

PI_MODEL_CONNECTION_LINES = (
    ('daemon/impl/harnesses/pi/bindings/__init__.py', 'bindings. No binding is inferred from legacy provider arguments or environment.'),
    ('daemon/impl/harnesses/pi/bindings/__init__.py', '    def provider_id(self) -> str:'),
    ('daemon/impl/harnesses/pi/bindings/__init__.py', '        return {self.provider_id: {'),
    ('daemon/impl/harnesses/pi/bindings/errors.py', '        return PiStartPreconditionError("configured Pi model is unavailable; check the selected provider and model")'),
    ('daemon/impl/harnesses/pi/bindings/launch.py', '                  provider_environment, trust_receipt, extension_path,'),
    ('daemon/impl/harnesses/pi/bindings/launch.py', '        command_name=spec.name, command_model_provider=spec.model_provider,'),
    ('daemon/impl/harnesses/pi/bindings/launch.py', '        model_provider=spec.model_provider, model=spec.model,'),
    ('daemon/impl/harnesses/pi/bindings/launch.py', '        model_environment={**environment, **provider_environment},'),
    ('daemon/impl/harnesses/pi/bindings/launch.py', '    payload.update(modelBinding=binding.snapshot(), modelProvider=binding.provider_id,'),
    ('daemon/impl/harnesses/pi/bindings/launch.py', '    if selection not in ((None, None), (binding.provider_id, binding.model)):'),
    ('daemon/impl/harnesses/pi/resources/pi_model_binding.mjs', '      const provider = "hyprial-connection-" + createHash("sha256").update(JSON.stringify([binding.connectionRef, binding.configRevision])).digest("hex");'),
    ('daemon/impl/harnesses/pi/resources/pi_model_binding.mjs', '      if (options.modelProvider !== provider || options.model !== binding.model) refuse();'),
    ('daemon/impl/harnesses/pi/resources/pi_model_binding.mjs', '    if (model.provider !== selected.provider || model.id !== selected.id) refuse();'),
    ('daemon/impl/harnesses/pi/resources/pi_model_binding.mjs', 'export function boundModelRuntime(runtime, provider, modelId, definition) {'),
    ('daemon/impl/harnesses/pi/resources/pi_model_binding.mjs', '      if (model.provider !== provider || model.id !== modelId || model.baseUrl !== definition.baseUrl ||'),
)
