"""Retention selection. No duration is configured, so nothing is selected.

Activating deletion requires one approved product, legal, and security
decision: the retention duration, and confirmation that audit rows and outbox
rows are not artifact keys. This function does not choose that duration.
Audit rows and outbox rows are not inputs. This function cannot see them.
"""

__all__ = ["plan_deletion", "select_for_deletion"]


def select_for_deletion(
    *, policy: str, artifact_keys: tuple[str, ...]
) -> tuple[str, ...]:
    """Return the artifact keys a retention pass may delete.

    `disabled` selects nothing, including when keys were supplied. Any other
    policy is refused, because no duration is implemented.
    """
    if policy != "disabled" or not isinstance(artifact_keys, tuple):
        raise ValueError("retention_unconfigured")
    return ()


def plan_deletion(
    *,
    artifact_policy: str,
    artifact_keys: tuple[str, ...],
    audit_keys: tuple[str, ...],
    outbox_keys: tuple[str, ...],
) -> tuple[str, ...]:
    """Keys a deletion pass may remove. The result is empty.

    Artifact retention is separate from audit and outbox retention. Those
    keys are arguments so a caller cannot hide them inside `artifact_keys`
    and have this function treat them as eligible; they are never returned.
    A duration is refused, including one read from configuration. There is
    no deletion state, no partial deletion, and no retry record, because
    nothing is selected. A later call, including after a process restart,
    still selects nothing. Tenant ownership is required before any future
    implementation may return a key.
    """
    if artifact_policy != "disabled" or not all(
        isinstance(keys, tuple) for keys in (artifact_keys, audit_keys, outbox_keys)
    ):
        raise ValueError("retention_unconfigured")
    return ()
