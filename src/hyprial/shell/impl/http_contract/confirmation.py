"""HTTP projection of daemon-owned preview confirmation declarations.

Transport retains the preview metadata and admits only a closed operationId
execute body. Authorization, session binding, atomic resource-version checks
and execution ownership remain in the daemon's domain handler. No transport
record, automatic confirmation or write retry is introduced here.
"""

from collections.abc import Mapping
from typing import Any

from .envelope import thaw_json


def confirmation_fields(
    confirmation: str, mutates: bool,
    params_schema: Mapping[str, Any], result_schema: Mapping[str, Any],
    preview: Mapping[str, Any] | None = None, preview_of: str | None = None,
) -> dict[str, Any]:
    """Validate the supported HTTP confirmation profile and detach metadata."""

    if confirmation == "none":
        if preview is not None or preview_of is not None:
            raise ValueError("confirmation none cannot carry preview metadata")
        return {}
    if confirmation == "execute":
        properties = params_schema.get("properties") if isinstance(params_schema, Mapping) else None
        if (
            mutates is not True or preview is not None
            or not isinstance(preview_of, str) or not preview_of.strip()
            or not isinstance(params_schema, Mapping)
            or params_schema.get("type") != "object"
            or params_schema.get("additionalProperties") is not False
            or not isinstance(properties, Mapping) or set(properties) != {"operationId"}
            or params_schema.get("required") not in (["operationId"], ("operationId",))
        ):
            raise ValueError("execute requires a preview pair and only operationId")
        operation = params_schema["properties"]["operationId"]
        if (
            not isinstance(operation, Mapping) or operation.get("type") != "string"
            or operation.get("x-hyprial-http-forbidden") is True
        ):
            raise ValueError("execute operationId must be an HTTP-visible string")
        return {"previewOf": preview_of}
    if (
        confirmation != "preview" or mutates is not False
        or preview_of is not None or not isinstance(preview, Mapping)
        or not isinstance(result_schema, Mapping)
    ):
        raise ValueError("unsupported HTTP preview declaration")
    ttl = preview.get("ttlSeconds")
    binds = preview.get("binds")
    if (
        type(ttl) is not int or not 1 <= ttl <= 3600
        or not isinstance(binds, (list, tuple))
        or not all(isinstance(item, str) for item in binds)
        or set(binds) != {"caller", "method", "paramsDigest", "expiry"}
        or preview.get("operationField") != "operationId"
    ):
        raise ValueError("preview requires bounded expiry and declared bindings")
    properties = result_schema.get("properties")
    required = result_schema.get("required", ())
    if not isinstance(properties, Mapping) or not isinstance(required, (list, tuple)):
        raise ValueError("preview result must declare its confirmation fields")
    for key in ("operationField", "expiresField", "summaryField"):
        field = preview.get(key)
        if not isinstance(field, str) or not field or field not in properties:
            raise ValueError("preview result must declare its confirmation fields")
        if key != "summaryField" and field not in required:
            raise ValueError("preview result must require operation and expiry")
    return {"preview": thaw_json(preview)}


def missing_preview_pairs(methods: Mapping[str, Any]) -> tuple[str, ...]:
    """Execute may reference only another admitted preview in this surface."""

    return tuple(
        name for name, method in methods.items()
        if method.confirmation == "execute" and (
            method.preview_of not in methods
            or methods[method.preview_of].confirmation != "preview"
        )
    )
