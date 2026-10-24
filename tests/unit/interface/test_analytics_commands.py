"""Operator analytics commands: the paths that need no database."""

import json

import pytest
from support import FAKE_DSN

from flakepilot.interface.composition_root import Secret, Settings, analytics

ORG = "11111111-2222-4333-8444-555555555555"
REPO = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def test_provider_status_reports_none_without_settings(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert analytics.run_provider_status(Settings(Secret(FAKE_DSN)), []) == 0
    assert set(json.loads(capsys.readouterr().out).values()) == {"none"}


def test_provider_status_prints_codes_never_keys(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = Settings.from_environ(
        {
            "FLAKEPILOT_DATABASE_URL": FAKE_DSN,
            "FLAKEPILOT_SERPAPI_API_KEY": "serp-secret-BBBB2222",
        }
    )
    assert analytics.run_provider_status(settings, []) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["research"] == "serpapi"
    assert "serp-secret" not in out


def test_tabpfn_needs_the_key_and_is_refused_before_any_database_work(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = analytics.run_evaluate_prioritization(
        Settings(Secret(FAKE_DSN)),
        [
            "--org-id",
            ORG,
            "--repo-id",
            REPO,
            "--train-cutoff",
            "2026-10-01T00:00:00+00:00",
            "--horizon-days",
            "7",
            "--with-tabpfn",
        ],
    )
    assert code == 1
    assert "FLAKEPILOT_TABPFN_API_KEY" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["--org-id", "not-a-uuid", "--repo-id", REPO],
        ["--org-id", ORG, "--repo-id", REPO, "--days", "0"],
        ["--org-id", ORG, "--repo-id", REPO, "--days", "367"],
        ["--org-id", ORG, "--repo-id", REPO, "--bucket", "month"],
    ],
)
def test_reliability_trend_refuses_bad_arguments(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exit_:
        analytics.run_reliability_trend(Settings(Secret(FAKE_DSN)), argv)
    assert exit_.value.code == 2


def test_a_naive_train_cutoff_is_refused() -> None:
    with pytest.raises(SystemExit):
        analytics.run_evaluate_prioritization(
            Settings(Secret(FAKE_DSN)),
            [
                "--org-id",
                ORG,
                "--repo-id",
                REPO,
                "--train-cutoff",
                "2026-10-01T00:00:00",
                "--horizon-days",
                "7",
            ],
        )
