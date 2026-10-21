import uuid
from collections.abc import Callable

import pytest

from flakepilot.shared_kernel.events import (
    SCHEMA_VERSION,
    Actor,
    ActorType,
    Channel,
    Event,
    Origin,
    Subject,
)

SYSTEM = Actor(ActorType.SYSTEM)
CLI = Origin(Channel.CLI, "run-1")
SUBJECT = Subject("org", "org-1")


def test_envelopes_get_a_fresh_event_id_and_the_schema_version() -> None:
    first = Event("iam.org.bootstrapped", SYSTEM, CLI, SUBJECT)
    second = Event("iam.org.bootstrapped", SYSTEM, CLI, SUBJECT)
    assert first.event_id != second.event_id
    assert str(uuid.UUID(first.event_id)) == first.event_id
    assert first.schema_version == SCHEMA_VERSION == 1


@pytest.mark.parametrize(
    "build",
    [
        lambda: Event("iam.session", SYSTEM, CLI, SUBJECT),
        lambda: Event("IAM.Session.Created", SYSTEM, CLI, SUBJECT),
        lambda: Actor(ActorType.USER),
        lambda: Actor(ActorType.CI_TOKEN),
        lambda: Actor(ActorType.OPERATOR_CLI, "someone"),
        lambda: Event("iam.a.b", SYSTEM, CLI, SUBJECT, payload={"Bad": 1}),
        lambda: Event("iam.a.b", SYSTEM, CLI, SUBJECT, payload={"k": "x" * 101}),
        lambda: Event("iam.a.b", SYSTEM, CLI, SUBJECT, payload={"k": 1.5}),  # type: ignore[dict-item]
        lambda: Event("iam.a.b", SYSTEM, CLI, SUBJECT, payload={"k": b"raw"}),  # type: ignore[dict-item]
    ],
)
def test_envelopes_reject_malformed_names_actors_and_payloads(
    build: Callable[[], object],
) -> None:
    with pytest.raises(ValueError):
        build()
