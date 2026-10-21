"""The execution handoff carries no control-plane secret and does not run."""

import asyncio

import pytest

from flakepilot.execution.application.plane import (
    UnavailableExecutionPlane,
    UnavailableRuntimeReaper,
)
from flakepilot.execution.domain.plane import (
    PLANE_FIELDS,
    PlaneRejected,
    PlaneRequest,
    plane_request,
)

ORG = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
REPO = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
EXECUTION = "cccccccc-cccc-cccc-cccc-cccccccccccc"
SHA = "ab" * 20
DIGEST = "cd" * 32
SELECTOR = "tests.login.TestLogin.test_ok"


def _request() -> PlaneRequest:
    return plane_request(
        execution_id=EXECUTION,
        org_id=ORG,
        repo_id=REPO,
        sha=SHA,
        selector=SELECTOR,
        wall_seconds=30,
        memory_mb=512,
        pids=64,
        source_sha256=DIGEST,
    )


def test_the_field_set_has_no_secret_slot() -> None:
    assert set(PlaneRequest.__dataclass_fields__) == PLANE_FIELDS
    banned = {"database_url", "token", "private_key", "secret", "password"}
    assert banned.isdisjoint(PLANE_FIELDS)


def test_a_valid_request_is_idempotent_and_runs_nothing() -> None:
    request = _request()
    plane = UnavailableExecutionPlane()

    async def scenario() -> tuple[str, str]:
        first = await plane.submit(request)
        second = await plane.submit(request)
        return first.reason, second.reason

    first, second = asyncio.run(scenario())
    assert first == second == "execution_host_unavailable"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("wall_seconds", 4),
        ("memory_mb", 2049),
        ("pids", 7),
        ("sha", "AB" * 20),
        ("source_sha256", "not-a-digest"),
        ("selector", "../etc/passwd"),
        ("org_id", "not-a-uuid"),
    ],
)
def test_quotas_and_identifiers_are_rejected_before_a_handoff(
    name: str, value: object
) -> None:
    kwargs = {
        "execution_id": EXECUTION,
        "org_id": ORG,
        "repo_id": REPO,
        "sha": SHA,
        "selector": SELECTOR,
        "wall_seconds": 30,
        "memory_mb": 512,
        "pids": 64,
        "source_sha256": DIGEST,
    }
    kwargs[name] = value
    with pytest.raises(PlaneRejected):
        plane_request(**kwargs)  # type: ignore[arg-type]


def test_a_secret_field_cannot_be_added() -> None:
    with pytest.raises(TypeError):
        PlaneRequest(
            execution_id=EXECUTION,
            org_id=ORG,
            repo_id=REPO,
            sha=SHA,
            selector=SELECTOR,
            wall_seconds=30,
            memory_mb=512,
            pids=64,
            source_sha256=DIGEST,
            database_url="postgresql://db",  # type: ignore[call-arg]
        )


def test_the_reaper_removes_nothing_and_says_so() -> None:
    report = UnavailableRuntimeReaper().reclaim()
    assert (report.reclaimed, report.reason) == (0, "runtime_reaper_unavailable")
    with pytest.raises(TypeError):
        asyncio.run(UnavailableExecutionPlane().submit(object()))  # type: ignore[arg-type]
