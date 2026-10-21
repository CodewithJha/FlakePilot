"""Org-gated assist. AI is disabled unless the org policy says otherwise.

The composition root wires `NullAiAssist` only. This use case does not check an AI
spend quota, and it does not emit `quota.denied`: IAM has no quota store (ADR-014).
`NullAiAssist` and `FakeAiAssist` have no network side effect, so the request row and
its outbox events are written in one transaction after the in-process call. A future
network adapter must record intent before the call. There is no HTTP route.
"""

import hashlib
import logging
import re
import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

from flakepilot.diagnosis.application.store import AiPolicy, AiRequestRecord, DiagStore
from flakepilot.diagnosis.domain.output import (
    MAX_OUTPUT_BYTES,
    Claim,
    ClaimStatus,
    DataPolicy,
    DiagnosisOutcome,
    check_citations,
    validate_output,
)
from flakepilot.identity.application.authorization import (
    CodeHostAccess,
    Grant,
    OrgAccess,
    PermissionDenied,
    ResourceNotFound,
    authorize,
)
from flakepilot.integrations.application.ports import (
    AiAssistPort,
    AiDataPart,
    AiRequest,
    AiResponse,
)
from flakepilot.shared_kernel.events import Actor, ActorType, Event, Origin, Subject

MAX_INSTRUCTIONS = 4_000
MAX_PARTS = 20
MAX_PART_CHARS = 8_000
MAX_PAYLOAD_BYTES = 65_536
_CODE = re.compile(r"[a-z][a-z0-9_]{0,39}")
_logger = logging.getLogger("flakepilot.diagnosis")

UnitOfWork = Callable[[], AbstractAsyncContextManager[DiagStore]]


class AssistRejected(Exception):
    """The request was refused before any row was written. `reason` is a short code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class UntrustedPart:
    """Caller-supplied evidence text. It is delimited and never treated as instructions."""

    name: str
    text: str


@dataclass(frozen=True)
class AssistResult:
    """Counts only. Raw model text is not returned."""

    request_id: str
    outcome: DiagnosisOutcome
    reason: str | None
    linked: int
    unverified: int
    contradicted: int


class Assist:
    def __init__(self, unit_of_work: UnitOfWork, port: AiAssistPort) -> None:
        self._unit_of_work = unit_of_work
        self._port = port

    async def set_policy(
        self, access: OrgAccess, *, org_id: str, policy: AiPolicy, origin: Origin
    ) -> None:
        """Org admins only. A missing row stays disabled until this runs."""
        if access.org_id != org_id:
            raise ResourceNotFound
        if not access.org_admin:
            raise PermissionDenied
        async with self._unit_of_work() as store:
            previous = await store.policy(org_id)
            await store.set_policy(org_id, policy)
            await store.append_event(
                _event(
                    "diag.ai_policy.updated",
                    access,
                    origin,
                    Subject("ai_policy", org_id),
                    org_id,
                    None,
                    before=previous.level.value,
                    after=policy.level.value,
                    payload={"retain_raw": policy.retain_raw},
                )
            )

    async def assist(
        self,
        access: OrgAccess,
        *,
        org_id: str,
        repo_id: str,
        code_host: CodeHostAccess,
        instructions: str,
        parts: tuple[UntrustedPart, ...],
        evidence_ids: frozenset[str],
        origin: Origin,
    ) -> AssistResult:
        """Needs `repo.investigate`. A disabled policy does not call the port.

        AI spend quota is not checked, and `quota.denied` is not emitted.
        """
        authorize(
            access,
            Grant.INVESTIGATE,
            org_id=org_id,
            repo_id=repo_id,
            code_host=code_host,
        )
        payload = _payload(instructions, parts, evidence_ids)
        digest = hashlib.sha256(payload)
        async with self._unit_of_work() as store:
            policy = await store.policy(org_id)
        response: AiResponse | None = None
        if policy.level is not DataPolicy.DISABLED:
            response = await self._port.submit(
                AiRequest(
                    instructions=instructions,
                    data_parts=tuple(
                        AiDataPart(part.name, part.text) for part in parts
                    ),
                    max_output_bytes=MAX_OUTPUT_BYTES,
                )
            )
        result, record, events = _decide(
            access,
            origin,
            org_id,
            repo_id,
            policy,
            payload,
            digest.digest(),
            digest.hexdigest(),
            evidence_ids,
            response,
        )
        async with self._unit_of_work() as store:
            await store.record_request(record)
            for event in events:
                await store.append_event(event)
        _logger.info("diagnosis %s %s", result.outcome.value, result.reason or "-")
        return result


def _payload(
    instructions: str, parts: tuple[UntrustedPart, ...], evidence_ids: frozenset[str]
) -> bytes:
    if len(instructions) > MAX_INSTRUCTIONS:
        raise AssistRejected("instructions_too_long")
    if len(parts) > MAX_PARTS:
        raise AssistRejected("too_many_parts")
    for part in parts:
        if _CODE.fullmatch(part.name) is None:
            raise AssistRejected("invalid_part")
        if len(part.text) > MAX_PART_CHARS:
            raise AssistRejected("part_too_large")
    for evidence_id in evidence_ids:
        if not _canonical_uuid(evidence_id):
            raise AssistRejected("invalid_evidence_id")
    encoded = _canonical(instructions, parts)
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise AssistRejected("payload_too_large")
    return encoded


def _canonical(instructions: str, parts: tuple[UntrustedPart, ...]) -> bytes:
    chunks = ["INSTRUCTIONS\n", instructions, "\nUNTRUSTED DATA\n"]
    for part in parts:
        chunks.extend((part.name, "\n", part.text, "\n"))
    chunks.append("END UNTRUSTED DATA")
    return "".join(chunks).encode()


def _canonical_uuid(value: str) -> bool:
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return str(parsed) == value


def _decide(
    access: OrgAccess,
    origin: Origin,
    org_id: str,
    repo_id: str,
    policy: AiPolicy,
    payload: bytes,
    digest: bytes,
    digest_hex: str,
    evidence_ids: frozenset[str],
    response: AiResponse | None,
) -> tuple[AssistResult, AiRequestRecord, tuple[Event, ...]]:
    request_id = str(uuid.uuid4())
    linked = unverified = contradicted = 0
    raw: str | None = None
    model: str | None = None
    if response is None:
        outcome = DiagnosisOutcome.UNAVAILABLE
        reason: str | None = "policy_disabled"
        provider = "none"
        sent = False
    elif not response.available or response.output is None:
        outcome = DiagnosisOutcome.UNAVAILABLE
        reason = _code(response.reason)
        provider = response.provider
        model = response.model
        sent = True
    else:
        sent = True
        provider = response.provider
        model = response.model
        output = response.output
        if policy.retain_raw and len(output.encode()) <= MAX_OUTPUT_BYTES:
            raw = output
        validation = validate_output(output)
        if not validation.ok:
            outcome = DiagnosisOutcome.REJECTED
            reason = validation.reason or "invalid_hypothesis"
            if reason == "output_too_large":
                raw = None
        else:
            linked, unverified, contradicted = _counts(
                check_citations(validation.hypotheses, evidence_ids)
            )
            outcome = (
                DiagnosisOutcome.PRODUCED
                if validation.hypotheses
                else DiagnosisOutcome.NO_HYPOTHESIS
            )
            reason = None
    record = AiRequestRecord(
        id=request_id,
        org_id=org_id,
        repo_id=repo_id,
        actor_id=access.principal_id,
        policy_level=policy.level,
        retain_raw=policy.retain_raw,
        provider=provider,
        model=model,
        payload_sha256=digest,
        payload_size=len(payload),
        outcome=outcome,
        validation_reason=reason,
        linked_count=linked,
        unverified_count=unverified,
        contradicted_count=contradicted,
        raw_output=raw,
    )
    body: dict[str, str | int | bool] = {
        "policy": policy.level.value,
        "retain_raw": policy.retain_raw,
        "provider": provider,
        "size_bytes": len(payload),
        "content_sha256": digest_hex,
        "linked": linked,
        "unverified": unverified,
        "contradicted": contradicted,
        "raw_retained": raw is not None,
    }
    subject = Subject("ai_request", request_id)
    events: list[Event] = []
    if sent:
        events.append(
            _event(
                "diag.ai_request.sent",
                access,
                origin,
                subject,
                org_id,
                repo_id,
                payload=body,
            )
        )
    events.append(
        _event(
            "diag.diagnosis.recorded",
            access,
            origin,
            subject,
            org_id,
            repo_id,
            after=outcome.value,
            reason=reason,
            payload=body,
        )
    )
    result = AssistResult(request_id, outcome, reason, linked, unverified, contradicted)
    return result, record, tuple(events)


def _counts(claims: Sequence[Claim]) -> tuple[int, int, int]:
    linked = unverified = contradicted = 0
    for claim in claims:
        if claim.status is ClaimStatus.EVIDENCE_LINKED:
            linked += 1
        elif claim.status is ClaimStatus.UNVERIFIED:
            unverified += 1
        elif claim.status is ClaimStatus.CONTRADICTED:
            contradicted += 1
    return linked, unverified, contradicted


def _code(value: str | None) -> str:
    if value is not None and _CODE.fullmatch(value):
        return value
    return "unavailable"


def _event(
    name: str,
    access: OrgAccess,
    origin: Origin,
    subject: Subject,
    org_id: str,
    repo_id: str | None,
    *,
    before: str | None = None,
    after: str | None = None,
    reason: str | None = None,
    payload: dict[str, str | int | bool] | None = None,
) -> Event:
    return Event(
        name=name,
        actor=Actor(ActorType.USER, access.principal_id),
        origin=origin,
        subject=subject,
        org_id=org_id,
        repo_id=repo_id,
        before=before,
        after=after,
        reason=reason,
        payload=payload or {},
    )
