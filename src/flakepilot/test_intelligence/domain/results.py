"""Test results, test identity and upload metadata (PRODUCT-SCOPE Workflow B). Pure rules.

Test identity (ADR-013): within one (org, repo), the pair (classname, name) as reported,
with surrounding whitespace removed; case-sensitive; the file and suite are not part of it.
A renamed or moved test is a new identity (PRODUCT-SCOPE Q5 default).
"""

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

MAX_ATTEMPTS_PER_TEST = 100  # per test identity in one upload, after merging duplicates


class Outcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERRORED = "errored"
    SKIPPED = "skipped"

    @property
    def failing(self) -> bool:
        return self in (Outcome.FAILED, Outcome.ERRORED)


class RunSource(StrEnum):
    """Where a run's results come from. Only CI uploads are classification evidence; the
    sandbox source stands for FlakePilot's own runs, whose `execution_failure` is never
    evidence (D5)."""

    CI_UPLOAD = "ci_upload"
    SANDBOX = "sandbox"


@dataclass(frozen=True)
class TestIdentity:
    __test__ = False  # not a pytest test class

    classname: str
    name: str

    @property
    def key(self) -> bytes:
        return hashlib.sha256(json.dumps([self.classname, self.name]).encode()).digest()


@dataclass(frozen=True)
class TestResult:
    __test__ = False  # not a pytest test class

    identity: TestIdentity
    attempts: tuple[Outcome, ...]

    @property
    def final(self) -> Outcome:
        return self.attempts[-1]


class TooManyAttempts(ValueError):
    pass


def merge_results(cases: Iterable[tuple[str, str, Iterable[str]]]) -> list[TestResult]:
    """One result per identity, in first-seen order. A test case reported more than once
    in one report (as some reporters do for retries) contributes its attempts in document
    order."""
    merged: dict[TestIdentity, list[Outcome]] = {}
    for classname, name, attempts in cases:
        merged.setdefault(TestIdentity(classname, name), []).extend(
            Outcome(a) for a in attempts
        )
    if any(len(a) > MAX_ATTEMPTS_PER_TEST for a in merged.values()):
        raise TooManyAttempts
    return [TestResult(identity, tuple(a)) for identity, a in merged.items()]


# Header names are part of the upload contract (README).
HEADERS = {
    "ci_source": "x-flakepilot-ci-source",
    "ci_run_id": "x-flakepilot-ci-run-id",
    "ci_attempt": "x-flakepilot-ci-attempt",
    "job_name": "x-flakepilot-job-name",
    "commit_sha": "x-flakepilot-commit-sha",
    "ref": "x-flakepilot-ref",
    "environment": "x-flakepilot-environment",
    "started_at": "x-flakepilot-started-at",
    "finished_at": "x-flakepilot-finished-at",
}
# The operator backfill's `ci_source`; a CI upload may not claim it.
BACKFILL_CI_SOURCE = "github_actions_backfill"
SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_FORMATS = {
    "ci_source": re.compile(r"[a-z][a-z0-9_-]{0,31}"),
    "ci_run_id": re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"),
    "ci_attempt": re.compile(r"[1-9][0-9]{0,3}"),
    "job_name": re.compile(r"[\x21-\x7e](?:[\x20-\x7e]{0,198}[\x21-\x7e])?"),
    "commit_sha": SHA,
    "ref": re.compile(r"(?!.*\.\.)[A-Za-z0-9._/+-]{1,255}"),
    "environment": re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,99}"),
}


class InvalidMetadata(ValueError):
    """`header` names the offending header; the value is never included."""

    def __init__(self, header: str) -> None:
        super().__init__(header)
        self.header = header


@dataclass(frozen=True)
class UploadMetadata:
    ci_source: str
    ci_run_id: str
    ci_attempt: int
    job_name: str
    commit_sha: str
    ref: str
    environment: str
    started_at: datetime
    finished_at: datetime


def parse_metadata(headers: Mapping[str, str]) -> UploadMetadata:
    """Strict: every field required, fixed character sets and lengths."""
    values: dict[str, str] = {}
    for field, header in HEADERS.items():
        value = headers.get(header)
        pattern = _FORMATS.get(field)
        if value is None or (pattern is not None and not pattern.fullmatch(value)):
            raise InvalidMetadata(header)
        values[field] = value
    started = _timestamp(values["started_at"], HEADERS["started_at"])
    finished = _timestamp(values["finished_at"], HEADERS["finished_at"])
    if finished < started:
        raise InvalidMetadata(HEADERS["finished_at"])
    return UploadMetadata(
        ci_source=values["ci_source"],
        ci_run_id=values["ci_run_id"],
        ci_attempt=int(values["ci_attempt"]),
        job_name=values["job_name"],
        commit_sha=values["commit_sha"],
        ref=values["ref"],
        environment=values["environment"],
        started_at=started,
        finished_at=finished,
    )


def _timestamp(value: str, header: str) -> datetime:
    """ISO 8601 with an explicit offset. Recorded only; never used to order anything."""
    if len(value) > 40:
        raise InvalidMetadata(header)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise InvalidMetadata(header) from None
    if parsed.tzinfo is None:
        raise InvalidMetadata(header)
    return parsed
