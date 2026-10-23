"""Registration use case: outcomes, conflicts, validation, audit events."""

import asyncio
import uuid
from datetime import UTC, datetime

import pytest

from flakepilot.repository.application.registration import (
    OUTCOME_ALREADY_REGISTERED,
    OUTCOME_REGISTERED,
    RegistrationConflict,
    register_repository,
    validate_binding,
)
from flakepilot.repository.application.source import RepoBinding, SourceError
from flakepilot.shared_kernel.events import Channel, Event, Origin

CLI = Origin(Channel.CLI, "run-1")
ORG = str(uuid.uuid4())
OTHER_ORG = str(uuid.uuid4())
REPO = str(uuid.uuid4())
NO_UUID = "not-a-uuid"


class FakeRegistrationStore:
    """Mirrors the real store's rule: consult the ID, then the coordinates."""

    def __init__(self) -> None:
        self.by_id: dict[str, RepoBinding] = {}
        self.by_coordinates: dict[tuple[str, str, str], RepoBinding] = {}
        self.events: list[Event] = []

    async def register(self, binding: RepoBinding, *, now: datetime) -> str:
        if now.tzinfo is None:
            raise ValueError("invalid_request")
        found = self.by_id.get(binding.repo_id)
        if found is not None:
            if found == binding:
                return OUTCOME_ALREADY_REGISTERED
            raise RegistrationConflict("the repository ID is registered otherwise")
        occupied = self.by_coordinates.get(("github", binding.owner, binding.name))
        if occupied is not None:
            raise RegistrationConflict("already registered under a different ID")
        self.by_id[binding.repo_id] = binding
        self.by_coordinates[("github", binding.owner, binding.name)] = binding
        return OUTCOME_REGISTERED

    async def append_event(self, event: Event) -> None:
        self.events.append(event)


def register_now(
    store: FakeRegistrationStore,
    *,
    org_id: str = ORG,
    repo_id: str = REPO,
    owner: str = "octo",
    name: str = "acme",
) -> str:
    return asyncio.run(
        register_repository(
            store,
            org_id=org_id,
            repo_id=repo_id,
            owner=owner,
            name=name,
            origin=CLI,
            now=datetime.now(UTC),
        )
    )


def test_a_fresh_binding_registers_and_appends_one_event() -> None:
    fake = FakeRegistrationStore()
    assert register_now(fake) == OUTCOME_REGISTERED
    assert fake.by_id[REPO] == RepoBinding(ORG, REPO, "octo", "acme")
    assert len(fake.events) == 1
    event = fake.events[0]
    assert event.name == "repo.repository.registered"
    assert event.subject.type == "repository" and event.subject.id == REPO
    assert event.org_id == ORG and event.repo_id == REPO
    assert event.after == "registered"
    assert event.payload == {"owner": "octo", "name": "acme"}
    assert (event.actor.type.value, event.actor.id) == ("operator_cli", None)


def test_the_exact_repeat_changes_nothing_and_appends_nothing() -> None:
    fake = FakeRegistrationStore()
    register_now(fake)
    assert register_now(fake) == OUTCOME_ALREADY_REGISTERED
    assert len(fake.events) == 1


def test_the_same_id_with_different_coordinates_conflicts() -> None:
    fake = FakeRegistrationStore()
    register_now(fake)
    with pytest.raises(RegistrationConflict):
        register_now(fake, owner="octo", name="other")
    assert len(fake.events) == 1


def test_the_same_coordinates_under_a_different_id_conflict() -> None:
    fake = FakeRegistrationStore()
    register_now(fake)
    with pytest.raises(RegistrationConflict):
        register_now(fake, repo_id=str(uuid.uuid4()))
    assert len(fake.events) == 1


def test_a_foreign_org_is_a_conflict_on_the_coordinates() -> None:
    # Coordinates are global: the same owner/name under another org is not creatable.
    fake = FakeRegistrationStore()
    register_now(fake)
    with pytest.raises(RegistrationConflict):
        register_now(fake, org_id=OTHER_ORG, repo_id=str(uuid.uuid4()))


@pytest.mark.parametrize(
    ("org_id", "repo_id", "owner", "name"),
    [
        (NO_UUID, REPO, "octo", "acme"),
        (ORG, NO_UUID, "octo", "acme"),
        (ORG, REPO, "bad owner", "acme"),
        (ORG, REPO, "octo", ".dotfile"),
        (ORG, REPO, "octo", "a..b"),
    ],
)
def test_unusable_input_is_refused(
    org_id: str, repo_id: str, owner: str, name: str
) -> None:
    fake = FakeRegistrationStore()
    with pytest.raises(SourceError):
        register_now(fake, org_id=org_id, repo_id=repo_id, owner=owner, name=name)
    assert fake.events == [] and fake.by_id == {}


def test_validate_binding_names_the_field() -> None:
    for match, arguments in [
        ("invalid_org_id", (NO_UUID, REPO, "octo", "acme")),
        ("invalid_repo_id", (ORG, NO_UUID, "octo", "acme")),
        ("invalid_owner", (ORG, REPO, "bad owner", "acme")),
        ("invalid_name", (ORG, REPO, "octo", "..bad")),
    ]:
        with pytest.raises(SourceError, match=match) as info:
            validate_binding(*arguments)
        assert info.value.reason == match
