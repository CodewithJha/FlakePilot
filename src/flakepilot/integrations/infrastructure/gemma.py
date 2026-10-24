"""Hosted Gemma through the Gemini API `generateContent` method (ADR-017, goal 1).

Implements `AiAssistPort` (ADR-009) against Google's hosted Gemma models
(`gemma-4-31b-it`, `gemma-4-26b-a4b-it`; see
https://ai.google.dev/gemma/docs/core/gemma_on_gemini_api). The API key rides
only in the `x-goog-api-key` header; it is never put in a URL, reason,
exception, or log line.

Gemma on this API does not reliably accept JSON mode (`responseMimeType`
answered 400 for Gemma 3), so structure is asked for in the instructions and
checked here: the answer must be one JSON object, optionally inside a single
Markdown code fence, which is removed. The hypothesis schema, citation links
and claim bounds stay the assist use case's rule set (ADR-014); the answer is
advisory and `ti-classification/1` stays authoritative.

Fail-safe contract (ADR-014, unchanged): every failure is an `unavailable`
`AiResponse` with a short reason code and `submit` never raises. 401/403 (and
the API's 400 `API_KEY_INVALID`) are `provider_auth_failed` and never retried;
408/429/5xx are retried with bounded backoff (`hosted_http.exchange`).
"""

import asyncio
import json
import re
import urllib.request

from flakepilot.integrations.application.ports import (
    AiCapabilities,
    AiRequest,
    AiResponse,
)
from flakepilot.integrations.infrastructure.hosted_http import (
    HostedError,
    Sleep,
    Transport,
    endpoint_ok,
    exchange,
    https_transport,
)
from flakepilot.integrations.infrastructure.model_endpoint import frame_evidence

PROVIDER = "gemini_api"
DEFAULT_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta"
MAX_REQUEST_BYTES = 512 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
_MAX_OUTPUT_TOKENS = 4096

_MODEL_ID = re.compile(r"gemma-[a-z0-9][a-z0-9.-]{0,62}")
_KEY = re.compile(r"[\x21-\x7e]{1,200}")
_FENCE = re.compile(r"\A```(?:json)?[ \t]*\n(.*?)\n?```\Z", re.DOTALL)


def model_ok(model_id: str) -> bool:
    return _MODEL_ID.fullmatch(model_id) is not None


def model_code(model_id: str) -> str:
    """`gemma-4-26b-a4b-it` -> `gemma_4_26b_a4b_it`: the short-code form the
    AiResponse contract records as provenance."""
    return re.sub(r"[^a-z0-9]+", "_", model_id)[:40].rstrip("_")


class _Bad(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class GemmaAssist:
    """Call one hosted Gemma model; never raise, never leak key or content.

    `endpoint` is the API base (default Google's v1beta); `model_id` is the
    API's own model name. Every response records `gemini_api` and the model the
    API reports running (`modelVersion`), else the configured one.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model_id: str,
        endpoint: str = DEFAULT_ENDPOINT,
        timeout_seconds: int = 60,
        transport: Transport = https_transport,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if _KEY.fullmatch(api_key) is None:
            raise ValueError("the Gemini API key is not a key")
        if _MODEL_ID.fullmatch(model_id) is None:
            raise ValueError("the model must be a Gemini API Gemma model name")
        if not endpoint_ok(endpoint):
            raise ValueError("the endpoint must be https (http only to loopback)")
        if not 1 <= int(timeout_seconds) <= 300:
            raise ValueError("the model timeout must be an integer from 1 to 300")
        self._key = api_key
        self._model_id = model_id
        self._url = f"{endpoint.rstrip('/')}/models/{model_id}:generateContent"
        self._timeout_seconds = int(timeout_seconds)
        self._transport = transport
        self._sleep = sleep

    def __repr__(self) -> str:
        return f"GemmaAssist(model_id={self._model_id!r})"

    def describe(self) -> AiCapabilities:
        return AiCapabilities(available=True, provider=PROVIDER)

    def _unavailable(self, reason: str) -> AiResponse:
        return AiResponse(
            available=False,
            output=None,
            reason=reason,
            provider=PROVIDER,
            model=model_code(self._model_id),
        )

    async def submit(self, request: AiRequest) -> AiResponse:
        encoded = json.dumps(
            {
                "systemInstruction": {"parts": [{"text": request.instructions}]},
                "contents": [
                    {"role": "user", "parts": [{"text": frame_evidence(request)}]}
                ],
                "generationConfig": {
                    "candidateCount": 1,
                    "maxOutputTokens": _MAX_OUTPUT_TOKENS,
                    "thinkingConfig": {"thinkingLevel": "minimal"},
                },
            }
        ).encode()
        if len(encoded) > MAX_REQUEST_BYTES:
            return self._unavailable("request_too_large")
        call = urllib.request.Request(
            self._url,
            data=encoded,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "FlakePilot",
                "x-goog-api-key": self._key,
            },
            method="POST",
        )
        try:
            status, body = await exchange(
                call,
                transport=self._transport,
                timeout_seconds=self._timeout_seconds,
                max_bytes=MAX_RESPONSE_BYTES,
                sleep=self._sleep,
            )
        except HostedError as error:
            return self._unavailable(error.reason)
        if status != 200:
            return self._unavailable(_status_reason(status, body))
        if len(body) > MAX_RESPONSE_BYTES:  # defense in depth beside the transport
            return self._unavailable("response_too_large")
        try:
            text, reported = _answer(body)
            output = _structured(text, request.max_output_bytes)
        except _Bad as error:
            return self._unavailable(error.reason)
        model = self._model_id
        if reported is not None and _MODEL_ID.fullmatch(reported):
            model = reported
        return AiResponse(
            available=True,
            output=output,
            reason=None,
            provider=PROVIDER,
            model=model_code(model),
        )


def _status_reason(status: int, body: bytes) -> str:
    if status in (401, 403) or (status == 400 and _invalid_key(body)):
        return "provider_auth_failed"
    if status == 402:
        return "provider_quota_exhausted"
    if status == 429:
        return "provider_rate_limited"
    if status == 408 or status >= 500:
        return "provider_http_error"
    return "provider_rejected"


def _invalid_key(body: bytes) -> bool:
    """Google's 400 for a bad key names `API_KEY_INVALID` in `error.details`.
    Only that enum is inspected; the message is never read."""
    try:
        details = json.loads(body)["error"]["details"]
        return any(
            isinstance(item, dict) and item.get("reason") == "API_KEY_INVALID"
            for item in details
        )
    except (ValueError, KeyError, TypeError):
        return False


def _answer(body: bytes) -> tuple[str, str | None]:
    """The text of the first candidate (thought parts dropped) and the
    reported model name."""
    try:
        parsed = json.loads(body)
        if not isinstance(parsed, dict):
            raise _Bad("bad_response")
        feedback = parsed.get("promptFeedback")
        if isinstance(feedback, dict) and feedback.get("blockReason"):
            raise _Bad("provider_blocked")
        candidate = parsed["candidates"][0]
        finish = candidate["finishReason"]
        if finish == "MAX_TOKENS":
            raise _Bad("output_truncated")
        if finish != "STOP":
            raise _Bad("provider_blocked")
        parts = candidate["content"]["parts"]
        texts = [
            part["text"]
            for part in parts
            if isinstance(part, dict)
            and not part.get("thought")
            and isinstance(part.get("text"), str)
        ]
        reported = parsed.get("modelVersion")
    except (ValueError, KeyError, IndexError, TypeError):
        raise _Bad("bad_response") from None
    text = "".join(texts)
    if not text:
        raise _Bad("bad_response")
    return text, reported if isinstance(reported, str) else None


def _structured(text: str, max_output_bytes: int) -> str:
    """One JSON object, fence removed, within the caller's byte cap."""
    if len(text.encode()) > max_output_bytes:
        raise _Bad("output_too_large")
    stripped = text.strip()
    fenced = _FENCE.fullmatch(stripped)
    if fenced is not None:
        stripped = fenced.group(1).strip()
    try:
        parsed = json.loads(stripped)
    except ValueError:
        raise _Bad("malformed_output") from None
    if not isinstance(parsed, dict):
        raise _Bad("malformed_output")
    return stripped
