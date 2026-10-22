"""Retention selection. No duration is configured, so nothing is selected.

Audit rows and outbox rows are not inputs. This function cannot see them.
"""

__all__ = ["select_for_deletion"]


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
