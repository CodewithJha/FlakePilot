"""The authenticated investigation: DIA's bounded workflow behind the session principal.

`Investigations` is what `interface.http.investigations` calls. The access
chain is the run view's own (`run_views.RunViews`): the run's canonical repo
from the org-scoped store, the principal's code-host standing verified for that
repo, then `Investigation.investigate`, which authorizes `repo.investigate`
itself, records the refusal or the outcome as provenance, and runs only the
providers the composition root wired. Every unverified outcome denies without
disclosure. The spoken brief is a second, explicit request: nothing here
produces audio unless that method is called, and the audio is returned to the
caller, never stored.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from flakepilot.diagnosis.application.brief import speak_brief
from flakepilot.diagnosis.application.investigation import (
    Investigation,
    InvestigationReport,
)
from flakepilot.identity.application.authorization import OrgAccess, ResourceNotFound
from flakepilot.identity.application.iam import SessionPrincipal
from flakepilot.identity.application.verification import (
    AccessVerification,
    CodeHostAccessVerifier,
)
from flakepilot.integrations.infrastructure.elevenlabs import ElevenLabsSpeech
from flakepilot.interface.http.errors import SpeechNotConfigured
from flakepilot.shared_kernel.events import Origin
from flakepilot.test_intelligence.application.store import UnitOfWork

LoginReader = Callable[[SessionPrincipal], Awaitable[str | None]]


class Investigations:
    def __init__(
        self,
        workflow: Investigation,
        unit_of_work: UnitOfWork,
        verifier: CodeHostAccessVerifier,
        login_reader: LoginReader,
        speech: ElevenLabsSpeech | None,
    ) -> None:
        self._workflow = workflow
        self._unit_of_work = unit_of_work
        self._verifier = verifier
        self._login_reader = login_reader
        self._speech = speech

    async def _verified(
        self, access: OrgAccess, org_id: str, run_id: str
    ) -> tuple[str, AccessVerification]:
        async with self._unit_of_work() as store:
            repo_id = await store.run_repo_id(org_id=org_id, run_id=run_id)
        if repo_id is None:
            raise ResourceNotFound
        login = await self._login_reader(SessionPrincipal(access.principal_id))
        if login is None:
            return repo_id, AccessVerification.INDETERMINATE
        return repo_id, await self._verifier.verify(
            login=login, org_id=org_id, repo_id=repo_id
        )

    async def investigate(
        self, *, access: OrgAccess, org_id: str, run_id: str, origin: Origin
    ) -> InvestigationReport:
        repo_id, verified = await self._verified(access, org_id, run_id)
        return await self._workflow.investigate(
            access,
            org_id=org_id,
            repo_id=repo_id,
            run_id=run_id,
            code_host=verified.standing,
            cutoff=datetime.now(UTC),
            origin=origin,
        )

    async def brief_audio(
        self,
        *,
        access: OrgAccess,
        org_id: str,
        run_id: str,
        origin: Origin,
        report_url: str,
    ) -> tuple[bytes, str]:
        """(audio, media type). Refused before any work when no speech
        provider is configured; provider failures raise
        `SpokenBriefUnavailable` with a short code."""
        if self._speech is None:
            raise SpeechNotConfigured
        report = await self.investigate(
            access=access, org_id=org_id, run_id=run_id, origin=origin
        )
        audio = await speak_brief(report, speech=self._speech, report_url=report_url)
        return audio.audio, audio.media_type
