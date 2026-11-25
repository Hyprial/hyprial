"""Same-source examples verified against the thirteen actual OrgFS write producers.

Export/import retain the trusted caller field in full MethodSpec examples;
the existing exporter removes HTTP-forbidden fields only after validation.
Empty and refusal cases describe the method's actual behavior, not a generic
write-only exemption. Fixed synthetic digests have publicly reproducible inputs.
"""

from __future__ import annotations

from hyprial.kernel import MethodExample, MethodExampleError, MethodExampleSet, ipc_errors

from .values import WRITE_OBSERVATIONS

__all__ = ["ORGFS_WRITE_EXAMPLES"]

_EMPTY_GAPS = {
    "orgfs.create": "A successful create returns the complete new SpaceInfo; a blank name is refused.",
    "orgfs.import": "The file reader refuses an empty local file; admitted bytes return a blob NodeInfo.",
    "orgfs.mkdir": "Even an empty new directory returns its NodeInfo; absence is not a successful result.",
    "orgfs.remove": "A successful removal returns ok=true; missing targets and the root are refused.",
    "orgfs.invite": "Inviting or updating a member returns MemberInfo; there is no absent-member success.",
    "orgfs.remove_member": "Removing a member returns ok=true; absent members and the owner are refused.",
    "orgfs.join": "A local/remote join returns SpaceInfo; an unknown space is a typed error, not an empty success.",
}
_REASONS = {
    "empty-document": "A zero-byte text document remains legal when moved or restored; NodeInfo reports size=0.",
    "empty-text": "A zero-byte text document is legal and returns size=0 with a document identity.",
    "empty-export": "Exporting an existing zero-byte document writes an empty file and returns its NodeInfo.",
    "missing-space": "Purging a nonexistent safe local space is a legal no-op with purged=false.",
    "disabled": "Disabling an unconfigured checkout returns enabled=false and its deterministic checkout path.",
}
_DESCRIPTIONS = {
    "empty-document": "Operate on a legal zero-byte text document.",
    "producer-success": "The isolated local producer completes the operation with valid preconditions.",
    "missing-space": "The producer handles a nonexistent local space according to its actual lookup path.",
    "blank-name": "Whitespace passes minLength but create_space rejects a blank logical name.",
    "bad-base64": "The bridge rejects schema-valid text that is not valid base64 content.",
    "empty-text": "Write a legal empty text document.",
    "binary-write": "Write the public bytes example-not-a-credential as a blob.",
    "relative-destination": "The real local-file path policy refuses a relative export destination.",
    "relative-source": "The real local-file path policy refuses a relative import source.",
    "absolute-path": "The OrgFS path resolver refuses absolute filesystem-style paths.",
    "root-refused": "Removing the OrgFS root is refused even though the request is schema-valid.",
    "permanent": "A permanent removal deletes the node instead of moving it to the trash.",
    "unsafe-space": "Local space purging refuses an unsafe path component before filesystem access.",
    "other-members": "Local space purging refuses a space with another live member.",
    "disabled": "An unconfigured checkout can be explicitly disabled.",
    "owner-refused": "The owner cannot be removed from its own membership list.",
    "empty-export": "Export a zero-byte document to a real isolated absolute path.",
    "empty-file-refused": "The local-file import reader refuses an empty source file.",
}


def _example(row):
    error = row.get("error")
    return MethodExample(
        name=row["name"], kind=row["kind"], description=_DESCRIPTIONS[row["name"]],
        params=row["params"], result=row.get("result"),
        error=MethodExampleError(**error) if error else None,
        reason=_REASONS[row["name"]] if row["kind"] in ("empty", "unconfigured") else "",
    )


def _set(example_method):
    cases = tuple(_example(row) for row in WRITE_OBSERVATIONS if row["method"] == example_method)
    invalid = MethodExample(
        name="schema-invalid", kind="invalid-params",
        description="Required business parameters are absent; validation rejects before dispatch.",
        params={}, error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "params do not match the method schema"),
    )
    return MethodExampleSet(method=example_method, cases=(*cases, invalid), empty_not_applicable=_EMPTY_GAPS.get(example_method, ""))


ORGFS_WRITE_EXAMPLES = tuple(_set(method) for method in dict.fromkeys(row["method"] for row in WRITE_OBSERVATIONS))
