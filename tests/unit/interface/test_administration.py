"""The administration CLI refuses unusable input before touching the database.

Identities and IDs are validated by the use cases (canonical UUIDs, GitHub subject and
login rules, supported grant names) and binding input by `validate_binding`; every case
below is refused before the first query reaches the (unreachable) database.
"""

import pytest
from support import SETTINGS

from flakepilot.interface.composition_root.administration import run_administration

ORG = "00000000-0000-0000-0000-000000000001"
REPO = "00000000-0000-0000-0000-000000000002"

ARGUMENT_CASES = [
    (
        "register-repo",
        ["--org-id", "x", "--repo-id", REPO, "--owner", "octo", "--name", "acme"],
        "invalid_org_id",
    ),
    (
        "register-repo",
        ["--org-id", ORG, "--repo-id", "z", "--owner", "octo", "--name", "acme"],
        "invalid_repo_id",
    ),
    (
        "register-repo",
        ["--org-id", ORG, "--repo-id", REPO, "--owner", "bad owner", "--name", "acme"],
        "invalid_owner",
    ),
    (
        "register-repo",
        ["--org-id", ORG, "--repo-id", REPO, "--owner", "octo", "--name", "..bad"],
        "invalid_name",
    ),
    ("add-member", ["--org-id", "x", "--member-github-id", "1"], "--org-id"),
    ("add-member", ["--org-id", ORG, "--member-github-id", "0"], "--member-github-id"),
    (
        "add-member",
        ["--org-id", ORG, "--member-github-id", "1", "--member-login", "a b"],
        "--member-login",
    ),
    (
        "grant",
        [
            "--org-id",
            "x",
            "--member-github-id",
            "1",
            "--repo-id",
            REPO,
            "--grant",
            "repo.view",
        ],
        "--org-id",
    ),
    (
        "grant",
        [
            "--org-id",
            ORG,
            "--member-github-id",
            "1",
            "--repo-id",
            "z",
            "--grant",
            "repo.view",
        ],
        "--repo-id",
    ),
    (
        "revoke",
        [
            "--org-id",
            ORG,
            "--member-github-id",
            "1",
            "--repo-id",
            "z",
            "--grant",
            "repo.view",
        ],
        "--repo-id",
    ),
]


@pytest.mark.parametrize(
    "command",
    ["register-repo", "add-member", "grant", "revoke", "show-access"],
)
def test_missing_required_arguments_use_argparse(command: str) -> None:
    with pytest.raises(SystemExit) as excinfo:
        run_administration(SETTINGS, [command])
    assert excinfo.value.code == 2


@pytest.mark.parametrize(
    ("command", "argv", "message"),
    ARGUMENT_CASES,
    ids=[f"{command}:{message}" for command, _argv, message in ARGUMENT_CASES],
)
def test_invalid_arguments_are_refused_before_any_database_access(
    command: str, argv: list[str], message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_administration(SETTINGS, [command, *argv]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith(f"{command}: {message}")
    assert "Traceback" not in captured.err
