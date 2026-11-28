# Decision record (S3a red tests R2; no inline comments by repository rule):
# - receivers.json stops being a protected section: protected_section_for_path
#   no longer names it and the identity vocabulary has no Receivers record or
#   receivers_path.  Checks go through getattr so a missing symbol is an
#   assertion about the surface, not an ImportError.
# - Package S (2026-11-25): the tests that replicated or reopened a journal
#   store holding an older build's receivers document were deleted with the
#   journal store; the state store refuses a journal space as a whole
#   (``ORGFS_LAYOUT_UNSUPPORTED``).
"""receivers.json is an ordinary OrgFS path."""

from __future__ import annotations


from hyprial.identity import OrgMembership
from hyprial.identity.impl.org_directory.models import protected_section_for_path

OWNER = "owner@example"


def test_the_path_has_no_protected_section() -> None:
    assert protected_section_for_path("receivers.json") is None
    assert protected_section_for_path("receivers.json", space_owner=OWNER) is None


def test_the_identity_vocabulary_has_no_receivers_record() -> None:
    assert getattr(OrgMembership, "Receivers", None) is None
    assert getattr(OrgMembership, "receivers_path", None) is None


