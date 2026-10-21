"""Validate model output and link citations to caller-supplied evidence IDs.

Confidence is accepted only so a malformed value can be rejected, then discarded.
It never changes claim status. `contradicted` is a status this checker does not
produce: citation text is not compared with evidence content. An unknown id is
`unverified`, including ids that are not UUIDs.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

MAX_OUTPUT_BYTES = 65_536
MAX_HYPOTHESES = 10
MAX_CITATIONS = 20
MAX_SUMMARY = 500
MAX_EVIDENCE_ID = 100
MAX_PATH = 200
MAX_LINE = 1_000_000
MAX_CONFIDENCE_TEXT = 40

_CAUSE = re.compile(r"[a-z][a-z0-9_]{0,39}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_HYPOTHESIS_KEYS = frozenset({"summary", "cause", "citations", "confidence"})
_CITATION_KEYS = frozenset({"evidence_id", "path", "line"})


class DataPolicy(StrEnum):
    DISABLED = "disabled"
    EVIDENCE_ONLY = "evidence_only"
    EVIDENCE_AND_SOURCE = "evidence_and_source"


class DiagnosisOutcome(StrEnum):
    PRODUCED = "produced"
    NO_HYPOTHESIS = "no_hypothesis"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"


class ClaimStatus(StrEnum):
    EVIDENCE_LINKED = "evidence_linked"
    UNVERIFIED = "unverified"
    CONTRADICTED = "contradicted"


@dataclass(frozen=True)
class Citation:
    evidence_id: str
    path: str | None
    line: int | None


@dataclass(frozen=True)
class Hypothesis:
    summary: str
    cause: str
    citations: tuple[Citation, ...]


@dataclass(frozen=True)
class Claim:
    status: ClaimStatus
    evidence_id: str | None


@dataclass(frozen=True)
class Validation:
    ok: bool
    reason: str | None
    hypotheses: tuple[Hypothesis, ...]

    def __post_init__(self) -> None:
        if self.ok is not (self.reason is None):
            raise ValueError("a valid result has no reason")
        if not self.ok and self.hypotheses:
            raise ValueError("a rejected result has no hypotheses")


def validate_output(raw: str) -> Validation:
    """Parse one model string. Failure keeps a reason and no hypotheses."""
    if len(raw.encode()) > MAX_OUTPUT_BYTES:
        return _reject("output_too_large")
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError:
        return _reject("malformed_json")
    if not isinstance(parsed, dict):
        return _reject("not_an_object")
    if set(parsed) - {"hypotheses"}:
        return _reject("invalid_hypothesis")
    if "hypotheses" not in parsed:
        return _reject("missing_hypotheses")
    hypotheses = parsed["hypotheses"]
    if not isinstance(hypotheses, list) or len(hypotheses) > MAX_HYPOTHESES:
        return (
            _reject("too_many_hypotheses")
            if isinstance(hypotheses, list)
            else _reject("invalid_hypothesis")
        )
    built: list[Hypothesis] = []
    for item in hypotheses:
        hypothesis = _hypothesis(item)
        if hypothesis is None:
            return _reject("invalid_hypothesis")
        built.append(hypothesis)
    return Validation(True, None, tuple(built))


def check_citations(
    hypotheses: Sequence[Hypothesis], known: frozenset[str]
) -> tuple[Claim, ...]:
    """Exact id match. A hypothesis with no citations is one unverified claim."""
    claims: list[Claim] = []
    for hypothesis in hypotheses:
        if not hypothesis.citations:
            claims.append(Claim(ClaimStatus.UNVERIFIED, None))
            continue
        for citation in hypothesis.citations:
            linked = citation.evidence_id in known
            status = ClaimStatus.EVIDENCE_LINKED if linked else ClaimStatus.UNVERIFIED
            claims.append(Claim(status, citation.evidence_id))
    return tuple(claims)


def _reject(reason: str) -> Validation:
    return Validation(False, reason, ())


def _hypothesis(item: object) -> Hypothesis | None:
    if not isinstance(item, dict) or set(item) - _HYPOTHESIS_KEYS:
        return None
    summary = item.get("summary")
    cause = item.get("cause")
    citations = item.get("citations")
    if not isinstance(summary, str) or not 1 <= len(summary) <= MAX_SUMMARY:
        return None
    if _CONTROL.search(summary) or not isinstance(cause, str):
        return None
    if _CAUSE.fullmatch(cause) is None or not isinstance(citations, list):
        return None
    if len(citations) > MAX_CITATIONS or not _confidence_ok(item):
        return None
    built: list[Citation] = []
    for citation in citations:
        parsed = _citation(citation)
        if parsed is None:
            return None
        built.append(parsed)
    return Hypothesis(summary, cause, tuple(built))


def _confidence_ok(item: dict[str, object]) -> bool:
    if "confidence" not in item:
        return True
    value = item["confidence"]
    if isinstance(value, str):
        return len(value) <= MAX_CONFIDENCE_TEXT and _CONTROL.search(value) is None
    if isinstance(value, bool):
        return False
    return isinstance(value, (int, float))


def _citation(item: object) -> Citation | None:
    if (
        not isinstance(item, dict)
        or set(item) - _CITATION_KEYS
        or "evidence_id" not in item
    ):
        return None
    evidence_id = item["evidence_id"]
    if not isinstance(evidence_id, str) or not 1 <= len(evidence_id) <= MAX_EVIDENCE_ID:
        return None
    path: str | None = None
    if "path" in item:
        raw_path = item["path"]
        if (
            not isinstance(raw_path, str)
            or len(raw_path) > MAX_PATH
            or _CONTROL.search(raw_path)
        ):
            return None
        path = raw_path
    line: int | None = None
    if "line" in item:
        raw_line = item["line"]
        if (
            isinstance(raw_line, bool)
            or not isinstance(raw_line, int)
            or not 1 <= raw_line <= MAX_LINE
        ):
            return None
        line = raw_line
    return Citation(evidence_id, path, line)
