"""The authenticated run view: one use case behind the session principal.

`RunViews` is what `interface.http.runs` calls. The `OrgAccess` arrives from
the `org_access` dependency (IAM's records for a verified session cookie); it
is never built from request input here. Before the view is served, the code
host standing REPO's `CodeHostAccessVerifier` implementation reports for this
principal's account handle on this run's canonical repository is resolved here
(provider call outside any database transaction; every unverified outcome and
a verified lack of code-host read both deny without disclosure). This is a
read-only view of already-stored uploads: it never fetches source, starts a
runtime, or writes, and closed-mode execution safety is untouched.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine

from flakepilot.identity.application.authorization import OrgAccess, ResourceNotFound
from flakepilot.identity.application.iam import SessionPrincipal
from flakepilot.identity.application.verification import (
    AccessVerification,
    CodeHostAccessVerifier,
)
from flakepilot.integrations.infrastructure.code_host import GitHubCodeHost
from flakepilot.integrations.infrastructure.installation import InstallationMinter
from flakepilot.interface.composition_root.database import transaction
from flakepilot.interface.composition_root.settings import Settings
from flakepilot.repository.application.verification import (
    BoundCodeAccessVerifier,
    RepoDirectoryLike,
)
from flakepilot.repository.infrastructure.store import SqlRepoDirectory
from flakepilot.test_intelligence.application.run_view import (
    ClassifiedRunReport,
    RunViewer,
)
from flakepilot.test_intelligence.application.store import UnitOfWork

__all__ = [
    "BoundCodeAccessVerifier",
    "ClassifiedRunReport",
    "RunViews",
    "code_access_verifier",
]

LoginReader = Callable[[SessionPrincipal], Awaitable[str | None]]


def code_access_verifier(
    engine: AsyncEngine, settings: Settings
) -> CodeHostAccessVerifier:
    """REPO's verifier over this engine and provider.

    The permission lookup runs as FlakePilot's GitHub App installation for the
    canonical repository (ADR-009): reading one account's repository permission
    needs a metadata read, which the installation token the worker already
    mints for source pinning also grants. For a deployment without installation
    credentials the provider can mint no token, so the lookup never succeeds
    and every call answers `unavailable`: fail closed, without contacting the
    host, and never logged with the token or the login.
    """
    creds = settings.github_installation
    if creds is None:
        provider = GitHubCodeHost(None)
    else:
        minter = InstallationMinter(
            app_id=creds.app_id, private_key_pem=creds.private_key.reveal()
        )
        provider = GitHubCodeHost(None, tokens=minter)

    @asynccontextmanager
    async def directories() -> AsyncIterator[RepoDirectoryLike]:
        async with transaction(engine) as session:
            yield SqlRepoDirectory(session)

    return BoundCodeAccessVerifier(directories, provider)


class RunViews:
    """Server-rendered run views for signed-in users."""

    def __init__(
        self,
        unit_of_work: UnitOfWork,
        verifier: CodeHostAccessVerifier,
        login_reader: LoginReader,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._verifier = verifier
        self._login_reader = login_reader

    async def classified(
        self, *, access: OrgAccess, org_id: str, run_id: str
    ) -> ClassifiedRunReport:
        """The run report plus per-test verdicts, authorization-checked.

        The org in the URL is only a lookup key for IAM's records. The
        grantee's code-host standing is verified against this run's canonical
        repository before the use case runs; only a verified standing reaches
        the intersection rule, so an unavailable, indeterminate, or unbound
        lookup denies in the same non-disclosing way as "no grant".
        """
        async with self._unit_of_work() as store:
            repo_id = await store.run_repo_id(org_id=org_id, run_id=run_id)
        if repo_id is None:
            raise ResourceNotFound
        login = await self._login_reader(SessionPrincipal(access.principal_id))
        if login is None:
            # No handle to verify: not evidence, deny like every unverified answer.
            verified = AccessVerification.INDETERMINATE
        else:
            verified = await self._verifier.verify(
                login=login, org_id=org_id, repo_id=repo_id
            )
        return await RunViewer(self._unit_of_work).classified_report_for(
            access=access,
            org_id=org_id,
            repo_id=repo_id,
            run_id=run_id,
            code_host=verified.standing,
        )
