"""Who may approve a patch: the authority and separation-of-duties part of the
Workflow H preconditions (PRODUCT-SCOPE D2). Patch, verification and repo-state checks
are separate. Inputs are plain values: the grant decision comes from IAM, code-host write
from REPO, the creator from ORCH's approval request, the human author from PATCH, and the
member count from IAM (SYSTEM-ARCHITECTURE §7.2).
"""


def can_approve(
    *,
    approver_id: str,
    holds_approve_grant: bool,
    has_code_host_write: bool,
    investigation_creator_id: str,
    patch_human_author_id: str | None,
    org_member_count: int,
) -> bool:
    """Requires `repo.approve` and code-host write. In an org with more than one member,
    the approver must be neither the investigation creator nor the patch's human author."""
    if not (holds_approve_grant and has_code_host_write):
        return False
    is_self = approver_id in (investigation_creator_id, patch_human_author_id)
    return not is_self or org_member_count == 1
