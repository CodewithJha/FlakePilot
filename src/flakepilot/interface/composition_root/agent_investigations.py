"""The agent investigation: DIA's bounded workflow behind a verified Auth0 token (goal 9).

`AgentInvestigations` is what `interface.http.agent_investigations` calls, and
it exists only when `FLAKEPILOT_AUTH0_DOMAIN`/`_AUDIENCE` are set. The chain,
in order, each step failing closed:

1. `Auth0TokenVerifier.verify` — any refusal is `Unauthenticated` (401), with
   no reason.
2. The active binding for (org, token subject) and the delegating admin's
   current membership, from IAM — none is `ResourceNotFound` (404).
3. The run's canonical repository, which must be the binding's — otherwise
   the same 404, before any code-host call.
4. The delegator's code-host standing, verified by REPO's verifier for that
   repository (the session route's own step), then `bound_access`: delegation
   from live grants, visibility, `narrow`, `authorize_agent`.
5. `Investigation.investigate` with the resulting `AgentAccess`, which
   authorizes again and records provenance as the delegating admin with the
   agent code alongside (`via_agent`).

The agent can investigate one bound repository and nothing else: no route
here approves, configures, grants, or produces audio.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncEngine

from flakepilot.diagnosis.application.investigation import (
    Investigation,
    InvestigationReport,
)
from flakepilot.identity.application.agent_bindings import (
    AgentBindingStore,
    bound_access,
    subject_digest,
)
from flakepilot.identity.application.agents import AgentExpired
from flakepilot.identity.application.authorization import ResourceNotFound
from flakepilot.identity.application.iam import SessionPrincipal
from flakepilot.identity.application.verification import (
    AccessVerification,
    CodeHostAccessVerifier,
)
from flakepilot.identity.infrastructure.store import SqlIamStore
from flakepilot.integrations.infrastructure.auth0 import (
    Auth0TokenVerifier,
    TokenRejected,
)
from flakepilot.interface.composition_root.database import transaction
from flakepilot.interface.http.errors import Unauthenticated
from flakepilot.shared_kernel.events import Origin
from flakepilot.test_intelligence.application.store import UnitOfWork as TiUnitOfWork

BindingUnitOfWork = Callable[[], AbstractAsyncContextManager[AgentBindingStore]]
LoginReader = Callable[[SessionPrincipal], Awaitable[str | None]]


def binding_unit_of_work(engine: AsyncEngine) -> BindingUnitOfWork:
    @asynccontextmanager
    async def unit_of_work() -> AsyncIterator[AgentBindingStore]:
        async with transaction(engine) as session:
            yield SqlIamStore(session)

    return unit_of_work


class AgentInvestigations:
    def __init__(
        self,
        *,
        tokens: Auth0TokenVerifier,
        bindings: BindingUnitOfWork,
        workflow: Investigation,
        runs: TiUnitOfWork,
        verifier: CodeHostAccessVerifier,
        login_reader: LoginReader,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._tokens = tokens
        self._bindings = bindings
        self._workflow = workflow
        self._runs = runs
        self._verifier = verifier
        self._login_reader = login_reader
        self._clock = clock

    async def investigate(
        self, *, bearer: str, org_id: str, run_id: str, origin: Origin
    ) -> InvestigationReport:
        try:
            token = await self._tokens.verify(bearer)
        except TokenRejected:
            raise Unauthenticated from None
        subject_hash = subject_digest(token.subject)
        if subject_hash is None:
            raise ResourceNotFound
        async with self._bindings() as store:
            binding = await store.active_agent_binding(
                org_id=org_id, subject_hash=subject_hash
            )
            delegator = (
                await store.org_access(user_id=binding.delegator_id, org_id=org_id)
                if binding is not None
                else None
            )
        if binding is None or delegator is None:
            raise ResourceNotFound
        async with self._runs() as runs:
            repo_id = await runs.run_repo_id(org_id=org_id, run_id=run_id)
        if repo_id is None or repo_id != binding.repo_id:
            raise ResourceNotFound
        login = await self._login_reader(SessionPrincipal(binding.delegator_id))
        verified = (
            AccessVerification.INDETERMINATE
            if login is None
            else await self._verifier.verify(
                login=login, org_id=org_id, repo_id=repo_id
            )
        )
        now = self._clock()
        try:
            access = bound_access(
                binding,
                delegator,
                repo_id=repo_id,
                code_host=verified.standing,
                token_scopes=token.scopes,
                token_expires_at=token.expires_at,
                now=now,
            )
        except AgentExpired:
            raise ResourceNotFound from None
        return await self._workflow.investigate(
            access,
            org_id=org_id,
            repo_id=repo_id,
            run_id=run_id,
            code_host=verified.standing,
            cutoff=now,
            origin=origin,
        )
