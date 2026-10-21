"""A repository binding is not a live code-host ACL."""

import asyncio
from datetime import UTC, datetime

from flakepilot.execution.application.reproduction import (
    CallerAccess,
    Execution,
    schedule_registered,
)
from flakepilot.execution.domain.reproduction import Decision
from flakepilot.identity.application.authorization import OrgAccess
from flakepilot.repository.application.source import RepoBinding
from flakepilot.shared_kernel.events import Actor, ActorType, Channel, Origin

ORG = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
REPO = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
USER = "cccccccc-cccc-cccc-cccc-cccccccccccc"
SHA = "ab" * 20
SELECTOR = "tests.login.TestLogin.test_acl"
NOW = datetime(2026, 10, 21, tzinfo=UTC)


class _Directory:
    def __init__(self, binding: RepoBinding) -> None:
        self._binding = binding

    async def get(self, repo_id: str) -> RepoBinding | None:
        if repo_id == self._binding.repo_id:
            return self._binding
        return None


class _Host:
    def __init__(self) -> None:
        self.calls = 0

    async def confirm_commit(self, *, owner: str, name: str, claimed_sha: str) -> str:
        del owner, name
        self.calls += 1
        return claimed_sha

    async def fetch_archive(self, *, owner: str, name: str, sha: str) -> bytes:
        del owner, name, sha
        raise AssertionError("archive")


class _Records:
    def __init__(self) -> None:
        self.rows: list[Execution] = []

    async def find_active(self, **kwargs: object) -> Execution | None:
        del kwargs
        return None

    async def find_closed(self, **kwargs: object) -> Execution | None:
        del kwargs
        return None

    async def find_unverified(
        self, *, org_id: str, repo_id: str, selector: str, sha: str
    ) -> Execution | None:
        for row in self.rows:
            if (
                row.org_id == org_id
                and row.repo_id == repo_id
                and row.selector == selector
                and row.sha == sha
                and row.reason == "source_unverified"
            ):
                return row
        return None

    async def insert(
        self,
        *,
        org_id: str,
        repo_id: str,
        selector: str,
        sha: str,
        decision: Decision,
        actor: Actor,
        origin: Origin,
        now: datetime,
    ) -> Execution:
        del actor, now
        row = Execution(
            {
                "id": f"id-{len(self.rows)}",
                "org_id": org_id,
                "repo_id": repo_id,
                "selector": selector,
                "sha": sha,
                "state": decision.state,
                "outcome": decision.outcome,
                "reason": decision.reason,
                "generation": 1,
                "job_id": None,
                "evidence_partial": False,
                "cancel_requested": False,
                "lease_owner": None,
                "lease_expires_at": None,
                "correlation_id": origin.correlation_id,
            }
        )
        self.rows.append(row)
        return row


class _Confirmed:
    async def confirmed_read(self, owner: str, name: str) -> bool:
        del owner, name
        return True


def _schedule(
    host: _Host, records: _Records, caller: CallerAccess | None = None
) -> Execution:
    binding = RepoBinding(ORG, REPO, "acme", "web")
    return asyncio.run(
        schedule_registered(
            _Directory(binding),
            host,
            records,
            access=OrgAccess(USER, ORG, org_admin=True),
            org_id=ORG,
            repo_id=REPO,
            selector=SELECTOR,
            claimed_sha=SHA,
            actor=Actor(ActorType.USER, USER),
            origin=Origin(Channel.HTTP, "corr-acl"),
            now=NOW,
            caller=caller,
        )
    )


def test_the_default_caller_does_not_touch_the_code_host() -> None:
    host = _Host()
    records = _Records()
    first = _schedule(host, records)
    second = _schedule(host, records)
    assert host.calls == 0
    assert first.reason == "source_unverified"
    assert first.state == "completed"
    assert second.id == first.id


def test_a_confirming_caller_pins_the_commit() -> None:
    host = _Host()
    records = _Records()
    record = _schedule(host, records, _Confirmed())
    assert host.calls == 1
    assert record.state == "scheduled"
    assert record.reason is None
