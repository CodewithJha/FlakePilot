"""Structured AI output: schema reasons, citation status, no contradicted claims."""

import json

import pytest

from flakepilot.diagnosis.domain.output import (
    MAX_OUTPUT_BYTES,
    Citation,
    Claim,
    ClaimStatus,
    DataPolicy,
    DiagnosisOutcome,
    Hypothesis,
    check_citations,
    validate_output,
)

KNOWN = frozenset({"11111111-1111-1111-1111-111111111111"})


def _hypothesis(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "summary": "a race on the shared clock",
        "cause": "unknown",
        "citations": [{"evidence_id": "11111111-1111-1111-1111-111111111111"}],
    }
    body.update(overrides)
    return body


def _raw(hypotheses: object, **extra: object) -> str:
    body: dict[str, object] = {"hypotheses": hypotheses}
    body.update(extra)
    return json.dumps(body)


def test_policy_and_outcome_values() -> None:
    assert DataPolicy.DISABLED.value == "disabled"
    assert DataPolicy.EVIDENCE_ONLY.value == "evidence_only"
    assert DataPolicy.EVIDENCE_AND_SOURCE.value == "evidence_and_source"
    assert {item.value for item in DiagnosisOutcome} == {
        "produced",
        "no_hypothesis",
        "rejected",
        "unavailable",
    }
    assert ClaimStatus.CONTRADICTED.value == "contradicted"


def test_valid_output_keeps_citations_and_drops_confidence() -> None:
    raw = _raw(
        [
            _hypothesis(confidence="high"),
            _hypothesis(
                summary="no linked evidence",
                citations=[],
                confidence=0.2,
            ),
        ]
    )
    result = validate_output(raw)
    assert result.ok is True
    assert result.reason is None
    assert result.hypotheses == (
        Hypothesis(
            summary="a race on the shared clock",
            cause="unknown",
            citations=(
                Citation(
                    evidence_id="11111111-1111-1111-1111-111111111111",
                    path=None,
                    line=None,
                ),
            ),
        ),
        Hypothesis(summary="no linked evidence", cause="unknown", citations=()),
    )


def test_empty_hypothesis_list_is_valid() -> None:
    result = validate_output(_raw([]))
    assert result.ok is True
    assert result.hypotheses == ()


def test_bounds_and_optional_citation_fields() -> None:
    wide = validate_output(_raw([_hypothesis()] * 10))
    deep = validate_output(
        _raw(
            [
                _hypothesis(
                    summary="s" * 500,
                    cause="a" + "b" * 39,
                    citations=[
                        {
                            "evidence_id": "e" * 100,
                            "path": "p" * 200,
                            "line": 1_000_000,
                        }
                    ]
                    * 20,
                )
            ]
        )
    )
    assert wide.ok is True and len(wide.hypotheses) == 10
    assert deep.ok is True
    assert len(deep.hypotheses[0].citations) == 20
    assert deep.hypotheses[0].citations[0].line == 1_000_000


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("{", "malformed_json"),
        ("[", "malformed_json"),
        ("null", "not_an_object"),
        ("[]", "not_an_object"),
        ('"x"', "not_an_object"),
        ("{}", "missing_hypotheses"),
        (_raw([_hypothesis()] * 11), "too_many_hypotheses"),
        ("x" * (MAX_OUTPUT_BYTES + 1), "output_too_large"),
        ("{" * MAX_OUTPUT_BYTES, "malformed_json"),
    ],
)
def test_recorded_rejection_reasons(raw: str, reason: str) -> None:
    result = validate_output(raw)
    assert result.ok is False
    assert result.reason == reason
    assert result.hypotheses == ()


@pytest.mark.parametrize(
    "hypothesis",
    [
        _hypothesis(summary=""),
        _hypothesis(summary="s" * 501),
        _hypothesis(summary="line\nbreak"),
        _hypothesis(cause="Bad"),
        _hypothesis(cause=""),
        _hypothesis(extra="nope"),
        _hypothesis(citations="none"),
        _hypothesis(citations=[{"evidence_id": "e"}] * 21),
        _hypothesis(citations=[{"evidence_id": ""}]),
        _hypothesis(citations=[{"evidence_id": "e" * 101}]),
        _hypothesis(citations=[{"evidence_id": 1}]),
        _hypothesis(citations=[{"evidence_id": "e", "path": "a/b\n"}]),
        _hypothesis(citations=[{"evidence_id": "e", "path": "p" * 201}]),
        _hypothesis(citations=[{"evidence_id": "e", "line": 0}]),
        _hypothesis(citations=[{"evidence_id": "e", "line": True}]),
        _hypothesis(citations=[{"evidence_id": "e", "line": 1.0}]),
        _hypothesis(citations=[{"evidence_id": "e", "note": "x"}]),
        _hypothesis(confidence=True),
        _hypothesis(confidence="c" * 41),
        "not-an-object",
    ],
)
def test_invalid_hypothesis_is_one_reason(hypothesis: object) -> None:
    result = validate_output(_raw([hypothesis]))
    assert result.reason == "invalid_hypothesis"
    assert result.hypotheses == ()


def test_unknown_top_level_key_is_rejected() -> None:
    result = validate_output(_raw([_hypothesis()], note="x"))
    assert result.reason == "invalid_hypothesis"


def test_citations_link_only_exact_known_ids() -> None:
    parsed = validate_output(
        _raw(
            [
                _hypothesis(
                    citations=[
                        {"evidence_id": "11111111-1111-1111-1111-111111111111"},
                        {"evidence_id": "not-a-uuid"},
                        {"evidence_id": "11111111-1111-1111-1111-111111111111 "},
                    ]
                ),
                _hypothesis(summary="uncited", citations=[]),
            ]
        )
    )
    assert check_citations(parsed.hypotheses, KNOWN) == (
        Claim(ClaimStatus.EVIDENCE_LINKED, "11111111-1111-1111-1111-111111111111"),
        Claim(ClaimStatus.UNVERIFIED, "not-a-uuid"),
        Claim(ClaimStatus.UNVERIFIED, "11111111-1111-1111-1111-111111111111 "),
        Claim(ClaimStatus.UNVERIFIED, None),
    )


def test_confidence_and_absence_never_produce_contradicted() -> None:
    low = validate_output(_raw([_hypothesis(confidence=0)]))
    high = validate_output(_raw([_hypothesis(confidence="certain")]))
    assert low.hypotheses == high.hypotheses
    claims = check_citations(low.hypotheses, frozenset())
    assert claims == (
        Claim(ClaimStatus.UNVERIFIED, "11111111-1111-1111-1111-111111111111"),
    )
    assert ClaimStatus.CONTRADICTED not in {claim.status for claim in claims}
    assert all(claim.status is not ClaimStatus.CONTRADICTED for claim in claims)
