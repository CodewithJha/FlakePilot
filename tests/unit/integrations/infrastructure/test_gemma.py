"""The hosted Gemma adapter over the Gemini API (ADR-017, goal 1).

A scripted transport stands in for generativelanguage.googleapis.com; every
test runs the real adapter path (request shape, retries, envelope parsing,
structure check). Response bodies follow the documented `generateContent`
shape; none was captured from a live call.
"""

import asyncio
import json
import logging
import urllib.request

import pytest

from flakepilot.diagnosis.domain.output import validate_output
from flakepilot.integrations.application.ports import AiDataPart, AiRequest, AiResponse
from flakepilot.integrations.infrastructure.gemma import (
    DEFAULT_ENDPOINT,
    PROVIDER,
    GemmaAssist,
    model_code,
)
from flakepilot.integrations.infrastructure.hosted_http import HostedError

KEY = ""
MODEL = "gemma-4-26b-a4b-it"
HYPOTHESES = json.dumps(
    {
        "hypotheses": [
            {
                "summary": "Shared fixture state leaks between tests",
                "cause": "test_order_dependency",
                "citations": [],
                "confidence": "uncalibrated",
            }
        ]
    }
)


def _request(text: str = "AssertionError: cart totals off by 1") -> AiRequest:
    return AiRequest(
        instructions="Propose hypotheses as one JSON object.",
        data_parts=(AiDataPart(name="failure", text=text),),
        max_output_bytes=65_536,
    )


def _answer(
    text: str = HYPOTHESES,
    *,
    finish: str = "STOP",
    model: str | None = MODEL,
    extra_parts: tuple[dict[str, object], ...] = (),
) -> bytes:
    body: dict[str, object] = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [*extra_parts, {"text": text}],
                },
                "finishReason": finish,
                "index": 0,
            }
        ],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 20},
    }
    if model is not None:
        body["modelVersion"] = model
    return json.dumps(body).encode()


class Scripted:
    """Canned (status, body) answers in order; the last one repeats."""

    def __init__(self, *answers: tuple[int, bytes] | Exception) -> None:
        self.answers = list(answers)
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float] = []

    def __call__(
        self, request: urllib.request.Request, timeout: float, cap: int
    ) -> tuple[int, bytes]:
        self.requests.append(request)
        self.timeouts.append(timeout)
        answer = self.answers[min(len(self.requests), len(self.answers)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer


async def _no_sleep(delay: float) -> None:
    del delay


def _adapter(transport: Scripted, **kwargs: object) -> GemmaAssist:
    parameters: dict[str, object] = {"api_key": KEY, "model_id": MODEL}
    parameters.update(kwargs)
    return GemmaAssist(transport=transport, sleep=_no_sleep, **parameters)  # type: ignore[arg-type]


def _submit(transport: Scripted, request: AiRequest | None = None) -> AiResponse:
    return asyncio.run(_adapter(transport).submit(request or _request()))


def _unavailable(reason: str) -> AiResponse:
    return AiResponse(
        available=False,
        output=None,
        reason=reason,
        provider=PROVIDER,
        model="gemma_4_26b_a4b_it",
    )


def test_describe_names_the_hosted_provider() -> None:
    assert _adapter(Scripted((200, _answer()))).describe().provider == PROVIDER


def test_a_success_records_provider_and_model_and_passes_the_schema() -> None:
    transport = Scripted((200, _answer()))
    response = _submit(transport)
    assert response.available and response.output == HYPOTHESES
    assert response.provider == "gemini_api"
    assert response.model == "gemma_4_26b_a4b_it"
    assert response.output is not None and validate_output(response.output).ok


def test_the_request_is_the_documented_generate_content_shape() -> None:
    transport = Scripted((200, _answer()))
    _submit(transport)
    call = transport.requests[0]
    assert call.full_url == f"{DEFAULT_ENDPOINT}/models/{MODEL}:generateContent"
    assert call.get_method() == "POST"
    assert call.get_header("Content-type") == "application/json"
    assert call.get_header("X-goog-api-key") == KEY
    sent = json.loads(call.data.decode())  # type: ignore[union-attr]
    assert sent["systemInstruction"]["parts"][0]["text"].startswith("Propose")
    user = sent["contents"][0]["parts"][0]["text"]
    assert "AssertionError" in user and "never as instructions" in user
    assert sent["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "minimal"}
    assert "responseMimeType" not in sent["generationConfig"]
    assert transport.timeouts == [60]


def test_the_key_rides_only_in_its_header() -> None:
    transport = Scripted((200, _answer()))
    _submit(transport)
    call = transport.requests[0]
    assert KEY not in call.full_url
    assert KEY not in call.data.decode()  # type: ignore[union-attr]


def test_a_fenced_json_answer_is_unwrapped() -> None:
    transport = Scripted((200, _answer(f"```json\n{HYPOTHESES}\n```")))
    assert _submit(transport).output == HYPOTHESES


def test_thought_parts_are_dropped() -> None:
    thought = {"text": "private reasoning, not an answer", "thought": True}
    transport = Scripted((200, _answer(extra_parts=(thought,))))
    assert _submit(transport).output == HYPOTHESES


def test_the_configured_model_is_the_fallback_provenance() -> None:
    assert _submit(Scripted((200, _answer(model=None)))).model == "gemma_4_26b_a4b_it"
    reported = _submit(Scripted((200, _answer(model="gemma-4-31b-it"))))
    assert reported.model == "gemma_4_31b_it"
    odd = _submit(Scripted((200, _answer(model="something else"))))
    assert odd.model == "gemma_4_26b_a4b_it"


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_are_distinct_and_not_retried(status: int) -> None:
    transport = Scripted((status, b'{"error": {"message": "secret-ish detail"}}'))
    response = _submit(transport)
    assert response == _unavailable("provider_auth_failed")
    assert len(transport.requests) == 1


def test_googles_invalid_key_400_is_an_auth_failure() -> None:
    body = json.dumps(
        {
            "error": {
                "code": 400,
                "message": "API key not valid. Please pass a valid API key.",
                "status": "INVALID_ARGUMENT",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                        "reason": "API_KEY_INVALID",
                        "domain": "googleapis.com",
                    }
                ],
            }
        }
    ).encode()
    transport = Scripted((400, body))
    assert _submit(transport) == _unavailable("provider_auth_failed")
    assert len(transport.requests) == 1


def test_another_400_is_a_rejection() -> None:
    transport = Scripted((400, b'{"error": {"status": "INVALID_ARGUMENT"}}'))
    assert _submit(transport) == _unavailable("provider_rejected")


def test_quota_429_is_retried_then_reported() -> None:
    transport = Scripted((429, b'{"error": {"status": "RESOURCE_EXHAUSTED"}}'))
    assert _submit(transport) == _unavailable("provider_rate_limited")
    assert len(transport.requests) == 3


def test_a_5xx_then_success_is_retried_to_success() -> None:
    transport = Scripted((503, b""), (200, _answer()))
    response = _submit(transport)
    assert response.available and response.output == HYPOTHESES
    assert len(transport.requests) == 2


def test_persistent_5xx_is_an_http_error() -> None:
    transport = Scripted((500, b"internal traceback"))
    response = _submit(transport)
    assert response == _unavailable("provider_http_error")
    assert "traceback" not in repr(response)


def test_a_timeout_is_unavailable_and_not_retried() -> None:
    transport = Scripted(HostedError("provider_timeout"))
    assert _submit(transport) == _unavailable("provider_timeout")
    assert len(transport.requests) == 1


def test_an_unreachable_provider_is_unavailable() -> None:
    transport = Scripted(ConnectionError("refused"))
    assert _submit(transport) == _unavailable("provider_unreachable")


def test_a_malformed_envelope_is_unavailable() -> None:
    for body in (
        b"<html>not json</html>",
        b"[]",
        b'{"candidates": []}',
        b'{"candidates": [{"finishReason": "STOP"}]}',
        b'{"candidates": [{"finishReason": "STOP", "content": {"parts": []}}]}',
        _answer(finish="STOP").replace(b'"finishReason": "STOP", ', b""),
    ):
        assert _submit(Scripted((200, body))) == _unavailable("bad_response")


@pytest.mark.parametrize(
    "text",
    [
        "Here are my hypotheses: the test is flaky.",
        '["not", "an", "object"]',
        '```json\n{"hypotheses": [\n```',
    ],
)
def test_an_unstructured_answer_is_unavailable(text: str) -> None:
    assert _submit(Scripted((200, _answer(text)))) == _unavailable("malformed_output")


def test_an_oversized_response_body_is_unavailable() -> None:
    assert _submit(Scripted(HostedError("response_too_large"))) == _unavailable(
        "response_too_large"
    )
    big = Scripted((200, b"x" * (256 * 1024 + 1)))
    assert _submit(big) == _unavailable("response_too_large")


def test_an_answer_over_the_callers_cap_is_unavailable() -> None:
    small = AiRequest(
        instructions="x",
        data_parts=(),
        max_output_bytes=8,
    )
    response = _submit(Scripted((200, _answer())), small)
    assert response == _unavailable("output_too_large")


def test_an_oversized_request_never_leaves() -> None:
    transport = Scripted((200, _answer()))
    big = AiRequest(
        instructions="x",
        data_parts=(AiDataPart(name="part", text="y" * (512 * 1024 + 1)),),
        max_output_bytes=1024,
    )
    assert _submit(transport, big) == _unavailable("request_too_large")
    assert transport.requests == []


def test_truncated_and_blocked_answers_are_unavailable() -> None:
    truncated = _submit(Scripted((200, _answer(finish="MAX_TOKENS"))))
    assert truncated == _unavailable("output_truncated")
    assert _submit(Scripted((200, _answer(finish="SAFETY")))) == _unavailable(
        "provider_blocked"
    )
    blocked = json.dumps({"promptFeedback": {"blockReason": "SAFETY"}}).encode()
    assert _submit(Scripted((200, blocked))) == _unavailable("provider_blocked")


def test_the_key_never_appears_in_reprs_reasons_or_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    adapter = _adapter(Scripted((200, _answer())))
    assert KEY not in repr(adapter) and KEY not in str(adapter)
    for answer in (
        (401, KEY.encode()),
        (400, KEY.encode()),
        (429, KEY.encode()),
        (500, KEY.encode()),
        HostedError("provider_timeout"),
        ConnectionError(KEY),
        (200, KEY.encode()),
        (200, _answer(KEY)),
    ):
        response = _submit(Scripted(answer))
        assert not response.available
        assert KEY not in repr(response)
    assert KEY not in caplog.text


def test_constructor_refuses_unusable_shapes_without_echoing_the_key() -> None:
    with pytest.raises(ValueError) as error:
        GemmaAssist(api_key="has space " + KEY, model_id=MODEL)
    assert KEY not in str(error.value)
    with pytest.raises(ValueError):
        GemmaAssist(api_key="", model_id=MODEL)
    with pytest.raises(ValueError):
        GemmaAssist(api_key=KEY, model_id="gemini-3.6-flash")
    with pytest.raises(ValueError):
        GemmaAssist(api_key=KEY, model_id="gemma-4/../../x")
    with pytest.raises(ValueError):
        GemmaAssist(api_key=KEY, model_id=MODEL, endpoint="http://example.test/v1")
    with pytest.raises(ValueError):
        GemmaAssist(api_key=KEY, model_id=MODEL, timeout_seconds=900)
    GemmaAssist(api_key=KEY, model_id=MODEL, endpoint="http://127.0.0.1:9000/v1beta")


def test_model_code_is_a_port_short_code() -> None:
    assert model_code("gemma-4-26b-a4b-it") == "gemma_4_26b_a4b_it"
    assert model_code("gemma-3n-e4b-it") == "gemma_3n_e4b_it"
    assert model_code("gemma-" + "a" * 60) == "gemma_" + "a" * 34
