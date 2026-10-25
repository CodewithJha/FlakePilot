"""`backfill-ci-history`: refused before any database or GitHub work when unconfigured."""

import pytest
from support import FAKE_DSN

from flakepilot.interface.composition_root import Secret, Settings
from flakepilot.interface.composition_root.operator import (
    _environment,
    run_backfill_ci_history,
)

ORG = "11111111-2222-4333-8444-555555555555"
REPO = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def test_without_the_github_app_and_artifact_dir_nothing_runs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = run_backfill_ci_history(
        Settings(Secret(FAKE_DSN)), ["--org-id", ORG, "--repo-id", REPO]
    )
    assert code == 1
    assert "FLAKEPILOT_GITHUB_APP_ID" in capsys.readouterr().err


def test_ids_must_be_canonical_uuids(capsys: pytest.CaptureFixture[str]) -> None:
    code = run_backfill_ci_history(
        Settings(Secret(FAKE_DSN)), ["--org-id", "acme", "--repo-id", REPO]
    )
    assert code == 1
    assert "--org-id must be a canonical UUID" in capsys.readouterr().err


@pytest.mark.parametrize("runs", ["0", "51", "ten"])
def test_the_run_bound_is_at_most_fifty(runs: str) -> None:
    with pytest.raises(SystemExit):
        run_backfill_ci_history(
            Settings(Secret(FAKE_DSN)),
            ["--org-id", ORG, "--repo-id", REPO, "--max-runs", runs],
        )


def test_artifact_names_become_environment_labels() -> None:
    assert _environment("junit (ubuntu, 3.12)") == "github-actions/junit--ubuntu--3.12-"
    assert len(_environment("x" * 300)) <= 100
