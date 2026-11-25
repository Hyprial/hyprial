"""Validation of every Contract Mock example that only Python can run.

Decision record (1e-2, 2026-11-15)
- These checks used to run when the Python mock server started.  The served mock
  is JavaScript now, so the startup guard became an explicit function.  Only the
  tests under ``tests/daemon/contract_mock`` call it (the real-projection test
  runs it over the committed contract in the pytest shards); no script does.
  It is not wired into ``scripts/verify/check_agent_contract_examples.py``
  because that verifier is a five-method completeness gate with its own report,
  while this function is a whole-contract defect check that raises on the first
  failure; folding one into the other would change the verifier's verdicts.
  What it checks: the sensitive-material scan of every example and the
  validation of every example, as the envelope the mock would serve, against the
  pinned HTTP envelope schema with jsonschema.
- Load-time validation of examples against the projected params, result and
  error schemas stays in ``load_contract``; this module adds what a served reply
  would otherwise carry to a browser.
"""

from __future__ import annotations

from .contract import Contract
from .envelope import Envelope
from .sensitive import assert_no_sensitive_material

_STARTUP_ID = "mock-startup"


def check_contract(contract: Contract) -> None:
    assert_no_sensitive_material(
        {name: list(method.examples) for name, method in contract.methods.items()}
    )
    envelope = Envelope(contract)
    for name, method in contract.methods.items():
        for case in method.examples:
            if case["kind"] in ("domain-error", "semantic-param-error", "invalid-params"):
                error = case["error"]
                dispatch = "not-dispatched" if case["kind"] == "invalid-params" else "dispatched"
                envelope.error(
                    _STARTUP_ID,
                    error["code"],
                    error["message"],
                    dispatch,
                    method=name,
                    data=error.get("data"),
                )
            else:
                envelope.success(name, _STARTUP_ID, case["result"])
