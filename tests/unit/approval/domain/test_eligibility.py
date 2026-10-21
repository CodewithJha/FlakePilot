import pytest

from flakepilot.approval.domain.eligibility import can_approve

CREATOR, AUTHOR, OTHER = "creator", "author", "other"


def approve(
    approver: str, *, members: int, grant: bool = True, write: bool = True
) -> bool:
    return can_approve(
        approver_id=approver,
        holds_approve_grant=grant,
        has_code_host_write=write,
        investigation_creator_id=CREATOR,
        patch_human_author_id=AUTHOR,
        org_member_count=members,
    )


@pytest.mark.parametrize("members", [2, 5])
def test_multi_member_org_forbids_self_approval(members: int) -> None:
    assert not approve(CREATOR, members=members)
    assert not approve(AUTHOR, members=members)
    assert approve(OTHER, members=members)


def test_single_member_org_allows_self_approval() -> None:
    assert approve(CREATOR, members=1)
    assert approve(AUTHOR, members=1)


@pytest.mark.parametrize("members", [1, 2])
@pytest.mark.parametrize("approver", [CREATOR, AUTHOR, OTHER])
def test_never_without_the_grant_or_code_host_write(
    approver: str, members: int
) -> None:
    assert not approve(approver, members=members, grant=False)
    assert not approve(approver, members=members, write=False)


def test_ai_patch_without_human_author_only_checks_the_creator() -> None:
    def ai_patch(approver: str) -> bool:
        return can_approve(
            approver_id=approver,
            holds_approve_grant=True,
            has_code_host_write=True,
            investigation_creator_id=CREATOR,
            patch_human_author_id=None,
            org_member_count=3,
        )

    assert not ai_patch(CREATOR)
    assert ai_patch(AUTHOR)
