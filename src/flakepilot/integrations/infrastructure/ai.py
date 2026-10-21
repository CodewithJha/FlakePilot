"""Assist adapters with no network (ADR-009).

`NullAiAssist` is the only adapter the composition root wires. It reports that no
provider is configured and does not read the request text back.

`FakeAiAssist` is deterministic and exists for tests and the offline evaluation
harness. A script maps exact instructions to a raw string so tests can feed
malformed output. It is not a production stand-in for a disabled policy.
"""

import hashlib
import json
from collections.abc import Mapping

from flakepilot.integrations.application.ports import (
    AiCapabilities,
    AiRequest,
    AiResponse,
)


def canonical_request(request: AiRequest) -> bytes:
    """Stable bytes for a request. Field order does not change the digest."""
    payload = {
        "data_parts": [
            {"name": part.name, "text": part.text} for part in request.data_parts
        ],
        "instructions": request.instructions,
        "max_output_bytes": request.max_output_bytes,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


class NullAiAssist:
    """No provider. `submit` ignores the request body and performs no I/O."""

    def describe(self) -> AiCapabilities:
        return AiCapabilities(available=False, provider="none")

    async def submit(self, request: AiRequest) -> AiResponse:
        del request
        return AiResponse(
            available=False,
            output=None,
            reason="no_provider",
            provider="none",
            model=None,
        )


class FakeAiAssist:
    """Deterministic output from the request digest, or a scripted string."""

    def __init__(self, scripts: Mapping[str, str] | None = None) -> None:
        self._scripts = dict(scripts or {})

    def describe(self) -> AiCapabilities:
        return AiCapabilities(available=True, provider="fake")

    async def submit(self, request: AiRequest) -> AiResponse:
        scripted = self._scripts.get(request.instructions)
        if scripted is None:
            digest = hashlib.sha256(canonical_request(request)).hexdigest()[:16]
            scripted = json.dumps(
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
        return AiResponse(
            available=True,
            output=scripted,
            reason=None,
            provider="fake",
            model="deterministic",
        )
