from datetime import UTC, datetime

import pytest

from flakepilot.test_intelligence.domain.results import (
    HEADERS,
    MAX_ATTEMPTS_PER_TEST,
    InvalidMetadata,
    Outcome,
    TestIdentity,
    TooManyAttempts,
    merge_results,
    parse_metadata,
)

SENTINEL = "SENTINEL-meta-77"
VALID = {
    HEADERS["ci_source"]: "github_actions",
    HEADERS["ci_run_id"]: "12345678.9",
    HEADERS["ci_attempt"]: "2",
    HEADERS["job_name"]: "unit tests (py3.12)",
    HEADERS["commit_sha"]: "a" * 40,
    HEADERS["ref"]: "refs/heads/main",
    HEADERS["environment"]: "ubuntu-24.04/py3.12",
    HEADERS["started_at"]: "2026-10-21T10:00:00+00:00",
    HEADERS["finished_at"]: "2026-10-21T10:05:00Z",
}


def test_valid_metadata_is_parsed() -> None:
    metadata = parse_metadata(VALID)
    assert (metadata.ci_attempt, metadata.commit_sha) == (2, "a" * 40)
    assert metadata.finished_at == datetime(2026, 10, 21, 10, 5, tzinfo=UTC)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ci_source", "GitHub Actions"),
        ("ci_run_id", ""),
        ("ci_run_id", "x" * 129),
        ("ci_attempt", "0"),
        ("ci_attempt", "-1"),
        ("ci_attempt", "12345"),
        ("job_name", " padded"),
        ("job_name", "tab\there"),
        ("commit_sha", "A" * 40),
        ("commit_sha", "a" * 39),
        ("ref", "refs/../../etc"),
        ("ref", "has space"),
        ("environment", "-leading"),
        ("started_at", "2026-10-21T10:00:00"),
        ("started_at", "yesterday"),
        ("finished_at", "2026-10-21T09:00:00+00:00"),
    ],
)
def test_invalid_metadata_names_the_header_but_never_the_value(
    field: str, value: str
) -> None:
    headers = {**VALID, HEADERS[field]: value + SENTINEL if value else value}
    with pytest.raises(InvalidMetadata) as excinfo:
        parse_metadata(headers)
    assert excinfo.value.header == HEADERS[field]
    assert SENTINEL not in str(excinfo.value)


@pytest.mark.parametrize("field", list(HEADERS))
def test_every_header_is_required(field: str) -> None:
    headers = {k: v for k, v in VALID.items() if k != HEADERS[field]}
    with pytest.raises(InvalidMetadata) as excinfo:
        parse_metadata(headers)
    assert excinfo.value.header == HEADERS[field]


def test_duplicate_cases_in_one_report_merge_as_successive_attempts() -> None:
    results = merge_results(
        [
            ("c", "t", ["failed"]),
            ("c", "other", ["passed"]),
            ("c", "t", ["passed"]),
        ]
    )
    assert [(r.identity, r.attempts) for r in results] == [
        (TestIdentity("c", "t"), (Outcome.FAILED, Outcome.PASSED)),
        (TestIdentity("c", "other"), (Outcome.PASSED,)),
    ]
    assert results[0].final is Outcome.PASSED


def test_merged_attempts_are_capped() -> None:
    with pytest.raises(TooManyAttempts):
        merge_results([("c", "t", ["failed"])] * (MAX_ATTEMPTS_PER_TEST + 1))


def test_identity_keys_are_case_sensitive_and_unambiguous() -> None:
    keys = {
        TestIdentity("a", "b").key,
        TestIdentity("A", "b").key,
        TestIdentity("a.b", "").key,
        TestIdentity("a", ".b").key,
    }
    assert len(keys) == 4
