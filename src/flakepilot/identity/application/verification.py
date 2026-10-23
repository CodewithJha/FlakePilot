"""IAM's `CodeHostAccessVerifier` port, defined here and implemented by REPO
(DEPENDENCY-RULES §4). The compile-time edge is REPO -> IAM; at runtime IAM's consumers
call the REPO implementation through the composition root.

The result separates four kinds of answer, and only the first is evidence:

- a **verified** standing (`CodeHostAccess`)
- a provider that **could not answer** (network, credentials, rate limit): never evidence
- an **indeterminate** answer (refused lookup, unusable or ambiguous response): never evidence
- a repository **unknown to the deployment**: no canonical repo identity to verify against

Fail closed: an unexplained outcome never stands in for a verified denial. Consumers that
need a `CodeHostAccess` value for the pure policy functions use `standing()`, which maps
everything unverified to `CodeHostAccess.NONE`, so "cannot say" and "verified none" both
deny under PRODUCT-SCOPE §3.1's intersection rule. The distinction is preserved in the
result itself for callers that may want to report why.
"""

import enum
from typing import Protocol

from flakepilot.identity.domain.access import CodeHostAccess


class AccessVerification(enum.Enum):
    """One grantee-and-repo code-host lookup, as REPO reports it.

    A verified standing is a fact about the grantee's permission at the code host right
    now. Everything else withholds the fact without asserting a falsehood: the deployment
    cannot say whether the grantee has access.
    """

    # Verified: the code host answered with the account's permission level.
    NONE = "verified_none"
    READ = "verified_read"
    WRITE = "verified_write"

    # Not verified: no evidence either way. Never satisfies a requirement.
    UNAVAILABLE = "unavailable"
    INDETERMINATE = "indeterminate"
    UNBOUND = "unbound"

    @property
    def is_verified(self) -> bool:
        """True only when the standing is a verified code-host fact."""
        return self in (
            AccessVerification.NONE,
            AccessVerification.READ,
            AccessVerification.WRITE,
        )

    @property
    def standing(self) -> CodeHostAccess:
        """The standing for the pure policy functions: unverified outcomes deny."""
        return {
            AccessVerification.NONE: CodeHostAccess.NONE,
            AccessVerification.READ: CodeHostAccess.READ,
            AccessVerification.WRITE: CodeHostAccess.WRITE,
        }.get(self, CodeHostAccess.NONE)


class CodeHostAccessVerifier(Protocol):
    """The permission lookup IAM's consumers use. Implemented by REPO.

    Inputs must be canonical: `login` is the grantee's code-host handle from IAM's own
    user record, and (org_id, repo_id) is the deployment's repository identity — a
    binding, not a claim from this caller. Implementations must not call IAM
    authorization (no re-entry), must never log the login or any token, and report every
    ambiguous provider answer as not verified.
    """

    async def verify(
        self, *, login: str, org_id: str, repo_id: str
    ) -> AccessVerification: ...
