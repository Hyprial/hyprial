"""Landing policies for suites that exercise mechanics, not attribution.

AT06 slice 2b makes the three landing entries demand a trusted signature: the
entry points take ``signature`` with no default, so every caller states its
intent.  The suites under ``tests/integration/transfer`` build bundles with
``export_bundle`` and never sign them (the export side is a later slice), so
they pass this policy and say so out loud.

Nothing in ``src/`` may use this: a production call site builds its policy with
``signature_gate.trust_policy_for(home)``, which cannot allow an unsigned
bundle.  ``test_transfer_signature_gate.py`` pins that.
"""

from __future__ import annotations

from hyprial.daemon.impl.transfer.landing.signature_gate import SignaturePolicy

UNSIGNED_REHEARSAL = SignaturePolicy(allow_unsigned=True)

__all__ = ["UNSIGNED_REHEARSAL"]
