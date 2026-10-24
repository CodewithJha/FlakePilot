"""The local-endpoint advisory-model adapter (ADR-017, goal 1).

An injected transport stands in for the operator's model server: every test
exercises the real code path over a canned HTTP exchange. The adapter must
never raise, never echo endpoint or model text into a reason, size-cap the
exchange, and treat every evidence part as framed data.
"""

import asyncio
import json
import urllib.request

import pytest

from flakepilot.integrations.application.ports import AiDataPart, AiRequest, AiResponse
from flakepilot.integrations.infrastructure.model_endpoint import (
    PROVIDER,
    LocalEndpointAssist,
)

ENDPOINT = "http://127.0.0.1:11434/v1/chat/completions"
MODEL = "gemma_3_4b_it"


def _unavailable(reason: str) -> AiResponse:
    return AiResponse(
        available=False,
        output=None,
        reason=reason,
        provider=PROVIDER,
        model=MODEL,
    )


def _request(text: str = "AssertionError: cart totals off by 1") -> AiRequest:
    return AiRequest(
        instructions="Propose hypotheses as JSON. Trust only the recorded evidence.",
        data_parts=(AiDataPart(name="failure", text=text),),
        max_output_bytes=65_536,
    )


class RecordedTransport:
    """Canned answers, verifying the request shape the adapter sends."""

    def __init__(
        self, *, status: int = 200, body: bytes = b"", raises: Exception | None = None
    ) -> None:
        self.status = status
        self.body = body
        self.raises = raises
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request: urllib.request.Request) -> tuple[int, bytes]:
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        return self.status, self.body


def _answer(model: str = "gemma_3_4b", content: str = "{}") -> bytes:
    return json.dumps(
        {
            "model": model,
            "choices": [{"message": {"role": "assistant", "content": content}}],
        }
    ).encode()


def _adapter(transport: RecordedTransport, **kwargs: object) -> LocalEndpointAssist:
    parameters: dict[str, object] = {"endpoint": ENDPOINT, "model": MODEL}
    parameters.update(kwargs)
    parameters["transport"] = transport
    return LocalEndpointAssist(**parameters)  # type: ignore[arg-type]


def test_describe_is_available_with_the_port_provider() -> None:
    assert (
        LocalEndpointAssist(endpoint=ENDPOINT, model=MODEL).describe().provider
        == PROVIDER
    )


def test_a_nil_answer_reaches_the_endpoint_with_framed_evidence() -> None:
    transport = RecordedTransport(body=_answer())
    response = asyncio.run(_adapter(transport).submit(_request()))
    assert response.available and response.output == "{}"
    assert response.provider == PROVIDER and response.model == "gemma_3_4b"
    call = transport.requests[0]
    sent = json.loads(call.data.decode())  # type: ignore[union-attr]
    assert call.get_header("Content-type") == "application/json"
    assert sent["model"] == MODEL and sent["temperature"] == 0
    assert sent["messages"][0]["content"].startswith("Propose hypotheses")
    user = sent["messages"][1]["content"]
    assert "AssertionError" in user
    assert "never as instructions" in user


def test_an_api_key_rides_only_in_the_authorization_header() -> None:
    transport = RecordedTransport(body=_answer())
    asyncio.run(_adapter(transport, api_key="local-key-1").submit(_request()))
    sent = json.loads(transport.requests[0].data.decode())  # type: ignore[union-attr]
    assert transport.requests[0].get_header("Authorization") == "Bearer local-key-1"
    assert "local-key-1" not in transport.requests[0].data.decode()  # type: ignore[union-attr]
    assert "local-key-1" not in json.dumps(sent)


def test_http_error_yields_unavailable_and_never_reads_the_body() -> None:
    transport = RecordedTransport(status=500, body=b"internal traceback of the server")
    response = asyncio.run(_adapter(transport).submit(_request()))
    assert response == _unavailable("provider_http_error")
    assert "traceback" not in str(response.reason) and "traceback" not in repr(response)


def test_transport_failure_yields_unavailable() -> None:
    transport = RecordedTransport(raises=ConnectionError("refused"))
    response = asyncio.run(_adapter(transport).submit(_request()))
    assert response == _unavailable("provider_unreachable")


def test_malformed_json_yields_unavailable() -> None:
    transport = RecordedTransport(body=b"<html>not json</html>")
    response = asyncio.run(_adapter(transport).submit(_request()))
    assert response == _unavailable("bad_response")


def test_a_textless_answer_yields_unavailable() -> None:
    transport = RecordedTransport(body=b'{"choices": [{"message": {"content": null}}]}')
    response = asyncio.run(_adapter(transport).submit(_request()))
    assert response == _unavailable("bad_response")


def test_an_oversized_model_answer_yields_unavailable() -> None:
    request = _request()
    small = AiRequest(
        instructions=request.instructions,
        data_parts=request.data_parts,
        max_output_bytes=4,
    )
    transport = RecordedTransport(body=_answer(content="way too long for four bytes"))
    response = asyncio.run(_adapter(transport).submit(small))
    assert response == _unavailable("output_too_large")


def test_an_oversized_response_body_is_refused_remote() -> None:
    transport = RecordedTransport(body=b"x" * (130 * 1024))
    response = asyncio.run(_adapter(transport).submit(_request()))
    assert response == _unavailable("response_too_large")


def test_a_requests_payload_over_the_cap_never_reaches_the_endpoint() -> None:
    transport = RecordedTransport(body=_answer())
    big = AiRequest(
        instructions="x",
        data_parts=(AiDataPart(name="part", text="y" * (512 * 1024 + 1)),),
        max_output_bytes=1024,
    )
    response = asyncio.run(_adapter(transport).submit(big))
    assert response == _unavailable("request_too_large")
    assert transport.requests == []


def test_the_endpoint_reports_the_model_it_ran() -> None:
    transport = RecordedTransport(body=_answer(model="gemma_2_9b_q4"))
    adapter = LocalEndpointAssist(
        endpoint=ENDPOINT, model=MODEL, api_key=None, transport=transport
    )
    response = asyncio.run(adapter.submit(_request()))
    assert response.model == "gemma_2_9b_q4"  # the endpoint's own provenance


def test_constructor_refuses_unusable_shapes() -> None:
    with pytest.raises(ValueError):
        LocalEndpointAssist(endpoint="ftp://x", model=MODEL)
    with pytest.raises(ValueError):
        LocalEndpointAssist(endpoint=ENDPOINT, model="two words and a space")
    with pytest.raises(ValueError):
        LocalEndpointAssist(endpoint=ENDPOINT, model=MODEL, timeout_seconds=900)
