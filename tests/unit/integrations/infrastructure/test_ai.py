"""AiAssistPort adapters: no network, null in production, fake only in tests."""

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from flakepilot.integrations.application.ports import (
    AiCapabilities,
    AiDataPart,
    AiRequest,
    AiResponse,
)
from flakepilot.integrations.infrastructure.ai import FakeAiAssist, NullAiAssist

_SOURCE = Path("src/flakepilot/integrations/infrastructure/ai.py")


def _request(instructions: str = "summarize", text: str = "fail") -> AiRequest:
    return AiRequest(
        instructions=instructions,
        data_parts=(AiDataPart(name="log", text=text),),
        max_output_bytes=1024,
    )


def _canonical(request: AiRequest) -> bytes:
    payload = {
        "data_parts": [
            {"name": part.name, "text": part.text} for part in request.data_parts
        ],
        "instructions": request.instructions,
        "max_output_bytes": request.max_output_bytes,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def test_null_describes_itself_as_unavailable() -> None:
    assert NullAiAssist().describe() == AiCapabilities(available=False, provider="none")


def test_null_submit_is_unavailable_and_does_not_echo_the_request() -> None:
    request = _request(instructions="secret-instructions", text="secret-log")
    response = asyncio.run(NullAiAssist().submit(request))
    assert response == AiResponse(
        available=False,
        output=None,
        reason="no_provider",
        provider="none",
        model=None,
    )
    rendered = repr(response)
    assert "secret-instructions" not in rendered
    assert "secret-log" not in rendered


def test_fake_is_a_pure_function_of_the_request() -> None:
    request = _request()
    first = asyncio.run(FakeAiAssist().submit(request))
    second = asyncio.run(FakeAiAssist().submit(request))
    digest = hashlib.sha256(_canonical(request)).hexdigest()[:16]
    expected = json.dumps(
        {
            "hypotheses": [
                {
                    "summary": f"deterministic {digest}",
                    "cause": "unknown",
                    "citations": [],
                    "confidence": "uncalibrated",
                }
            ]
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    assert first == second
    assert first == AiResponse(
        available=True,
        output=expected,
        reason=None,
        provider="fake",
        model="deterministic",
    )
    assert FakeAiAssist().describe() == AiCapabilities(available=True, provider="fake")


def test_fake_changes_when_any_request_field_changes() -> None:
    baseline = asyncio.run(FakeAiAssist().submit(_request()))
    changed = asyncio.run(FakeAiAssist().submit(_request(text="other")))
    assert baseline.output != changed.output


def test_fake_script_replaces_the_output_for_exact_instructions() -> None:
    fake = FakeAiAssist(scripts={"broken": "not-json"})
    scripted = asyncio.run(fake.submit(_request(instructions="broken")))
    default = asyncio.run(fake.submit(_request(instructions="other")))
    assert scripted.available is True
    assert scripted.output == "not-json"
    assert default.output != "not-json"
    assert default.output is not None
    assert default.output.startswith('{"hypotheses":')


def test_adapter_source_imports_no_network_or_vendor_sdk() -> None:
    source = _SOURCE.read_text()
    for forbidden in (
        "urllib",
        "socket",
        "httpx",
        "http.client",
        "openai",
        "anthropic",
    ):
        assert forbidden not in source


def test_response_invariant() -> None:
    with pytest.raises(ValueError):
        AiResponse(
            available=True, output=None, reason=None, provider="fake", model=None
        )
    with pytest.raises(ValueError):
        AiResponse(
            available=True, output="{}", reason="extra", provider="fake", model=None
        )
    with pytest.raises(ValueError):
        AiResponse(
            available=False,
            output="{}",
            reason="no_provider",
            provider="none",
            model=None,
        )
    with pytest.raises(ValueError):
        AiResponse(
            available=False, output=None, reason=None, provider="none", model=None
        )
    with pytest.raises(ValueError):
        AiResponse(
            available=False,
            output=None,
            reason="Not a code",
            provider="none",
            model=None,
        )
