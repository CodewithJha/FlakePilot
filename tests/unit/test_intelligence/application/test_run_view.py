"""RunViewer (operator-demo reporting): pass-through reads, fail-closed by default."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

from flakepilot.identity.application.authorization import (
    CodeHostAccess,
    Grant,
    OrgAccess,
    ResourceNotFound,
)

READ = CodeHostAccess.READ
from flakepilot.shared_kernel.events import Event
from flakepilot.test_intelligence.application.run_view import (
    MAX_INDEX_LIMIT,
    ClassifiedRunReport,
    RunViewer,
)
from flakepilot.test_intelligence.application.store import (
    ReportedCase,
    RunIndexEntry,
    RunReport,
    RunSummary,
    TiStore,
)
from flakepilot.test_intelligence.domain.classification import (
    FailureClass,
    Observation,
    Reason,
)
from flakepilot.test_intelligence.domain.results import RunSource

ORG = "11111111-2222-4333-8444-555555555555"
REPO = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
RUN = "cccccccc-dddd-4eee-8fff-000000000001"
TEST_ID = "dddddddd-eeee-4fff-8aaa-000000000003"
SHA = "0" * 40
RECEIVED = datetime(2026, 10, 23, tzinfo=UTC)

ORG_B = "99999999-8888-4777-8666-555555555555"

REPORT = RunReport(
    run_id=RUN,
    org_id=ORG,
    repo_id=REPO,
    upload_id="cccccccc-dddd-4eee-8fff-000000000002",
    source="ci_upload",
    commit_sha="0" * 40,
    environment="demo.local",
    revision_status="unchecked",
    received_at=datetime(2026, 10, 23, tzinfo=UTC),
    summary=RunSummary(tests=3, attempts=5, passed=1, failed=1, errored=0, skipped=1),
    metadata=None,
    content_sha256=None,
    size_bytes=None,
    artifact_key=None,
    results=(
        ReportedCase(
            classname="demo.auth",
            name="test_login_succeeds",
            attempts=("failed", "passed"),
            test_id=TEST_ID,
        ),
    ),
)


class FakeTiStore:
    """The protocol methods this read-only use case never calls are inert."""

    def __init__(self, stored: RunReport | None = None) -> None:
        self.stored = stored
        self.report_calls: list[tuple[str, str, str]] = []
        self.appended: list[str] = []
        self.observations_by_test: dict[str, list[Observation] | None] = {}
        self.observed: list[str] = []
        self.repo_lookups: list[tuple[str, str]] = []
        self.index_entries: tuple[RunIndexEntry, ...] = ()
        self.index_calls: list[tuple[str, str, int]] = []
        self.history_responses: dict[int, tuple[Any, ...]] = {}

    def knows(self, test_id: str, history: list[Observation] | None) -> None:
        self.observations_by_test[test_id] = history

    async def append_event(self, event: Event) -> None:
        self.appended.append(event.name)

    async def claim_upload(self, upload: Any) -> bool:
        del upload
        return False

    async def processed_upload(self, **kwargs: Any) -> None:
        del kwargs

    async def record_rejection(self, upload: Any) -> None:
        del upload

    async def processed_count_since(
        self, *, org_id: str, repo_id: str, token_id: str, since: object
    ) -> int:
        del org_id, repo_id, token_id, since
        return 0

    async def ensure_identities(self, **kwargs: Any) -> dict[Any, Any]:
        del kwargs
        return {}

    async def create_run(self, **kwargs: Any) -> None:
        del kwargs

    async def add_results(self, **kwargs: Any) -> None:
        del kwargs

    async def run_report(
        self, *, org_id: str, repo_id: str, run_id: str
    ) -> RunReport | None:
        self.report_calls.append((org_id, repo_id, run_id))
        if self.stored is not None and (org_id, repo_id, run_id) == (
            ORG,
            REPO,
            self.stored.run_id,
        ):
            return self.stored
        return None

    async def run_repo_id(self, *, org_id: str, run_id: str) -> str | None:
        self.repo_lookups.append((org_id, run_id))
        if self.stored is not None and (org_id, run_id) == (
            self.stored.org_id,
            self.stored.run_id,
        ):
            return self.stored.repo_id
        return None

    async def observations(
        self, *, org_id: str, repo_id: str, test_id: str
    ) -> list[Observation] | None:
        assert (org_id, repo_id) == (ORG, REPO)
        self.observed.append(test_id)
        return self.observations_by_test.get(test_id)

    async def repo_history(
        self,
        *,
        org_id: str,
        repo_id: str,
        cutoff: Any,
        max_tests: int = 100,
    ) -> tuple[Any, ...]:
        assert (org_id, repo_id) == (ORG, REPO)
        return self.history_responses.get(max_tests, ())

    async def failure_trend(
        self,
        *,
        org_id: str,
        repo_id: str,
        start: Any,
        end: Any,
        bucket: str,
        limit: int,
    ) -> tuple[Any, ...]:
        raise AssertionError("the run viewer never reads trends")

    def page(self, entries: tuple[RunIndexEntry, ...]) -> None:
        self.index_entries = entries

    async def runs_index(
        self, *, org_id: str, repo_id: str, limit: int
    ) -> tuple[RunIndexEntry, ...]:
        self.index_calls.append((org_id, repo_id, limit))
        return self.index_entries


def viewer(store: FakeTiStore) -> RunViewer:
    @asynccontextmanager
    async def unit_of_work() -> AsyncIterator[TiStore]:
        yield store

    return RunViewer(unit_of_work)


def test_the_report_passes_the_stored_row_through() -> None:
    store = FakeTiStore(REPORT)
    result = asyncio.run(viewer(store).report(org_id=ORG, repo_id=REPO, run_id=RUN))
    assert result is REPORT  # one unit of work, no copying
    assert store.report_calls == [(ORG, REPO, RUN)]
    [case] = result.results
    assert case.attempts == ("failed", "passed")
    assert store.appended == []  # a read appends no events


def test_an_unknown_run_is_not_found() -> None:
    store = FakeTiStore(None)
    with pytest.raises(ResourceNotFound):
        asyncio.run(viewer(store).report(org_id=ORG, repo_id=REPO, run_id=RUN))


def test_non_canonical_ids_are_not_found_without_a_query() -> None:
    store = FakeTiStore(REPORT)
    v = viewer(store)
    for org_id, repo_id, run_id in (
        ("ORG", REPO, RUN),
        (ORG, REPO.upper(), RUN),
        ("../" + ORG, REPO, RUN),
        (ORG, REPO, "0" * 41),
    ):
        with pytest.raises(ResourceNotFound):
            asyncio.run(v.report(org_id=org_id, repo_id=repo_id, run_id=run_id))
    assert store.report_calls == []
    # A canonical but unknown ID is looked up once and reported as not found.
    unknown_run = RUN[:-1] + "2"
    with pytest.raises(ResourceNotFound):
        asyncio.run(v.report(org_id=ORG, repo_id=REPO, run_id=unknown_run))
    assert store.report_calls == [(ORG, REPO, unknown_run)]


def observation(
    run_id: str,
    attempts: tuple[str, ...] = (),
    *,
    sha: str = SHA,
    source: RunSource = RunSource.CI_UPLOAD,
) -> Observation:
    from flakepilot.test_intelligence.domain.results import Outcome

    return Observation(
        run_id=run_id,
        commit_sha=sha,
        environment="demo.local",
        received_at=RECEIVED,
        source=source,
        attempts=tuple(Outcome(a) for a in attempts),
    )


def test_classification_uses_the_stored_evidence_over_the_identity_history() -> None:
    store = FakeTiStore(REPORT)
    store.knows(TEST_ID, [observation("run-x", ("failed", "passed"))])
    result = asyncio.run(
        viewer(store).classified_report(org_id=ORG, repo_id=REPO, run_id=RUN)
    )
    assert isinstance(result, ClassifiedRunReport)
    assert result.run is REPORT
    assert store.observed == [TEST_ID]  # one evidence read per case
    [entry] = result.cases
    assert entry.case is REPORT.results[0]
    verdict = entry.classification
    assert verdict is not None
    assert verdict.failure_class is FailureClass.FLAKY
    assert verdict.reason is None
    assert verdict.target_sha == SHA
    assert verdict.rule_version == "ti-classification/1"
    assert verdict.evidence["environment"] == "demo.local"
    assert verdict.evidence["target_runs"] == ["run-x"]
    assert verdict.evidence["failed"] == 1 and verdict.evidence["passed"] == 1


def test_insufficient_evidence_is_reported_as_the_rule_set_states_it() -> None:
    store = FakeTiStore(REPORT)
    store.knows(TEST_ID, [observation("run-x", ("passed",))])
    [entry] = asyncio.run(
        viewer(store).classified_report(org_id=ORG, repo_id=REPO, run_id=RUN)
    ).cases
    verdict = entry.classification
    assert verdict is not None
    assert (verdict.failure_class, verdict.reason) == (
        FailureClass.INCONCLUSIVE,
        Reason.INSUFFICIENT_HISTORY,
    )


def test_non_ci_runs_are_excluded_evidence_and_listed() -> None:
    store = FakeTiStore(REPORT)
    store.knows(
        TEST_ID, [observation("sandbox-x", ("failed",), source=RunSource.SANDBOX)]
    )
    [entry] = asyncio.run(
        viewer(store).classified_report(org_id=ORG, repo_id=REPO, run_id=RUN)
    ).cases
    verdict = entry.classification
    assert verdict is not None
    assert verdict.failure_class is FailureClass.INCONCLUSIVE
    assert verdict.evidence["excluded_runs"] == ["sandbox-x"]


def test_an_identity_without_a_verdict_is_rendered_as_unavailable_not_guessed() -> None:
    store = FakeTiStore(REPORT)
    store.knows(TEST_ID, None)  # the identity is not in this (org, repo) pair
    [entry] = asyncio.run(
        viewer(store).classified_report(org_id=ORG, repo_id=REPO, run_id=RUN)
    ).cases
    assert entry.classification is None
    assert entry.case.attempts == ("failed", "passed")  # the chain is still shown


def test_classified_report_is_fail_closed_like_the_plain_read() -> None:
    store = FakeTiStore(None)
    v = viewer(store)
    for org_id, repo_id, run_id in (
        ("ORG", REPO, RUN),
        (ORG, REPO.upper(), RUN),
        (ORG, REPO, "x" * 36),
    ):
        with pytest.raises(ResourceNotFound):
            asyncio.run(
                v.classified_report(org_id=org_id, repo_id=repo_id, run_id=run_id)
            )
    assert store.report_calls == [] and store.observed == []
    # A canonical but unknown run is looked up once (no evidence read) and not found.
    unknown_run = RUN[:-1] + "3"
    with pytest.raises(ResourceNotFound):
        asyncio.run(v.classified_report(org_id=ORG, repo_id=REPO, run_id=unknown_run))
    assert store.report_calls == [(ORG, REPO, unknown_run)] and store.observed == []


ADMIN = OrgAccess("user-a", ORG, org_admin=True)
OUTSIDER = OrgAccess("user-b", ORG_B, org_admin=False)


def test_the_authorization_and_the_read_share_one_unit_of_work() -> None:
    """The decision and the read happen inside one store unit; the caller resolved
    the run's repo inside the org (and verified code-host access) beforehand."""
    store = FakeTiStore(REPORT)
    store.knows(TEST_ID, None)
    asyncio.run(
        viewer(store).classified_report_for(
            ADMIN, org_id=ORG, repo_id=REPO, run_id=RUN, code_host=READ
        )
    )
    assert store.repo_lookups == []
    assert store.report_calls == [(ORG, REPO, RUN)]
    assert store.observed == [TEST_ID]


def test_non_canonical_identifiers_are_not_sent_to_the_store() -> None:
    store = FakeTiStore(REPORT)
    viewer_instance = viewer(store)
    for org_id, repo_id, run_id in (
        ("ORG", REPO, RUN),
        (ORG, REPO, "x" * 36),
        (ORG, "x" * 36, RUN),
    ):
        with pytest.raises(ResourceNotFound):
            asyncio.run(
                viewer_instance.classified_report_for(
                    ADMIN, org_id=org_id, repo_id=repo_id, run_id=run_id, code_host=READ
                )
            )
    assert store.report_calls == [] and store.observed == []


def test_view_without_grant_is_denied_as_not_found() -> None:
    """A member with no repo.view grant learns nothing about the run.

    `decide` answers NOT_FOUND for a missing VIEW (not FORBIDDEN), so the
    denial cannot even reveal that the run exists.
    """
    store = FakeTiStore(REPORT)
    member = OrgAccess("member", ORG, org_admin=False)  # no grants on the repo
    with pytest.raises(ResourceNotFound):
        asyncio.run(
            viewer(store).classified_report_for(
                member, org_id=ORG, repo_id=REPO, run_id=RUN, code_host=READ
            )
        )
    assert store.report_calls == [] and store.observed == []


def test_a_viewer_from_another_org_is_denied_at_the_decision_itself() -> None:
    store = FakeTiStore(REPORT)
    with pytest.raises(ResourceNotFound):
        asyncio.run(
            viewer(store).classified_report_for(
                OUTSIDER, org_id=ORG, repo_id=REPO, run_id=RUN, code_host=READ
            )
        )
    assert store.report_calls == [] and store.observed == []


def test_an_authorized_member_sees_the_verdict() -> None:
    """`repo.view` explicitly granted decides ALLOWED through the real rules."""
    store = FakeTiStore(REPORT)
    store.knows(TEST_ID, [observation("run-x", ("failed", "passed"))])
    member = OrgAccess(
        "member", ORG, org_admin=False, grants=frozenset({(REPO, Grant.VIEW)})
    )
    result = asyncio.run(
        viewer(store).classified_report_for(
            member, org_id=ORG, repo_id=REPO, run_id=RUN, code_host=READ
        )
    )
    [entry] = result.cases
    assert entry.classification is not None
    assert entry.classification.failure_class is FailureClass.FLAKY


# --- the runs index: the same fail-closed reads, one (org, repo) pair wide ---


def index_entry(run: str = RUN) -> RunIndexEntry:
    return RunIndexEntry(
        run_id=run,
        source="ci_upload",
        commit_sha=SHA,
        environment="demo.local",
        revision_status="unchecked",
        received_at=RECEIVED,
        summary=RunSummary(
            tests=1, attempts=1, passed=0, failed=1, errored=0, skipped=0
        ),
    )


def test_the_index_lists_the_pair_newest_first_and_clamps_the_limit() -> None:
    store = FakeTiStore()
    store.page((index_entry(),))
    index = asyncio.run(
        viewer(store).runs_index(org_id=ORG, repo_id=REPO, limit=10_000)
    )
    assert index.entries == (index_entry(),)
    # The use case clamps the limit: one honest page, not an unbounded query.
    assert store.index_calls == [(ORG, REPO, MAX_INDEX_LIMIT)]
    # And a nonsense-or-noncanonical pair is refused before any store call.
    for org_id, repo_id in (("ORG", REPO), (ORG, REPO.upper()), (ORG, "x" * 36)):
        with pytest.raises(ResourceNotFound):
            asyncio.run(viewer(store).runs_index(org_id=org_id, repo_id=repo_id))
    assert len(store.index_calls) == 1


def test_the_operator_index_of_a_pair_without_runs_is_empty_not_an_error() -> None:
    store = FakeTiStore()
    index = asyncio.run(viewer(store).runs_index(org_id=ORG, repo_id=REPO))
    assert index.entries == ()
    assert store.index_calls == [(ORG, REPO, 50)]


def test_the_member_index_needs_the_verified_standing_and_the_grant() -> None:
    """Same intersection rule as every session read, before any listing query."""
    member = OrgAccess(
        "member", ORG, org_admin=False, grants=frozenset({(REPO, Grant.VIEW)})
    )
    grantless = OrgAccess("stranger", ORG, org_admin=False)

    # No grant, or a code-host standing that verified no access: deny, no read.
    for access, standing in ((grantless, READ), (member, CodeHostAccess.NONE)):
        store = FakeTiStore()
        store.page((index_entry(),))
        with pytest.raises(ResourceNotFound):
            asyncio.run(
                viewer(store).runs_index_for(
                    access, org_id=ORG, repo_id=REPO, code_host=standing
                )
            )
        assert store.index_calls == []
        assert store.report_calls == []

    # Verified access plus the grant: the listing goes through.
    store = FakeTiStore()
    store.page((index_entry(),))
    index = asyncio.run(
        viewer(store).runs_index_for(
            member, org_id=ORG, repo_id=REPO, code_host=READ, limit=3
        )
    )
    assert index.org_id == ORG and index.repo_id == REPO
    assert [entry.run_id for entry in index.entries] == [RUN]
    assert store.index_calls == [(ORG, REPO, 3)]
