"""ElevenLabs text-to-speech adapter for the `SpeechProvider` port (goal 14).

Speaks `POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}` with the
`xi-api-key` header, a fixed model and an MP3 `output_format`. The text that
arrives is already a summarized brief without secrets (the port's contract);
it is the only content sent. The key rides only in its header.

What comes back is checked before it is trusted as audio: `audio/mpeg`,
non-empty, inside the port's 2 MiB cap, and starting with an ID3 tag or an
MPEG frame sync. Failures are short codes. An error body is read only up to
4 KiB and only to pick ElevenLabs' own error code out of a fixed set — its
message is never echoed. Rate limits and 5xx get a bounded retry with
backoff; quota, auth, and request errors do not.
"""

import asyncio
import http.client
import json
import re
import ssl
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from flakepilot.integrations.application.ports import (
    AiCapabilities,
    SpeechAudio,
    SpeechProviderError,
    SpeechRequest,
)

PROVIDER = "elevenlabs"
API_URL = "https://api.elevenlabs.io/v1/text-to-speech/"
MODELS = frozenset({"eleven_flash_v2_5", "eleven_multilingual_v2"})
MAX_AUDIO_BYTES = 2 * 1024 * 1024
MAX_ERROR_BYTES = 4 * 1024

_VOICE_ID = re.compile(r"[A-Za-z0-9]{8,64}")
_MP3_FORMAT = re.compile(r"mp3_(22050|24000|44100)_(32|48|64|96|128|192)")
_QUOTA_CODES = frozenset({"quota_exceeded", "insufficient_credits"})

Transport = Callable[[urllib.request.Request, float], tuple[int, str, bytes]]
Sleep = Callable[[float], Awaitable[None]]


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def urllib_transport(
    request: urllib.request.Request, timeout: float
) -> tuple[int, str, bytes]:
    """Blocking call: (status, content type, body). Audio is read to one byte
    past the cap; an error body only to `MAX_ERROR_BYTES`."""
    opener = urllib.request.build_opener(
        _NoRedirects, urllib.request.HTTPSHandler(context=ssl.create_default_context())
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            return (
                response.status,
                response.headers.get("Content-Type", ""),
                response.read(MAX_AUDIO_BYTES + 1),
            )
    except urllib.error.HTTPError as error:
        with error:
            return error.code, "", error.read(MAX_ERROR_BYTES)


def _error_code(body: bytes) -> str | None:
    """ElevenLabs' `detail.code` (or legacy `detail.status`); nothing else."""
    try:
        detail = json.loads(body[:MAX_ERROR_BYTES]).get("detail")
    except (ValueError, AttributeError):
        return None
    if not isinstance(detail, dict):
        return None
    code = detail.get("code") or detail.get("status")
    return code if isinstance(code, str) else None


def _refusal(status: int, body: bytes) -> tuple[str, bool, bool]:
    """(reason, unavailable, retryable) for a non-200 answer."""
    code = _error_code(body)
    if code in _QUOTA_CODES or status == 402:
        return "quota_exceeded", False, False
    if status == 401:
        return "unauthorized", False, False
    if status == 403:
        return "forbidden", False, False
    if status == 404:
        return "unknown_voice", False, False
    if status in (400, 422):
        return "bad_request", False, False
    if status == 429:
        return "rate_limited", True, True
    if 500 <= status <= 599:
        return "provider_http_error", True, True
    return "provider_http_error", True, False


def voice_ok(voice_id: str) -> bool:
    return _VOICE_ID.fullmatch(voice_id) is not None


def _is_mp3(audio: bytes) -> bool:
    return audio.startswith(b"ID3") or (
        len(audio) >= 2 and audio[0] == 0xFF and audio[1] & 0xE0 == 0xE0
    )


class ElevenLabsSpeech:
    """Live speech. `voices` maps the port's short voice codes to ElevenLabs
    voice IDs chosen by the operator; an unmapped code is refused locally."""

    def __init__(
        self,
        *,
        api_key: str,
        voices: Mapping[str, str],
        model: str = "eleven_flash_v2_5",
        output_format: str = "mp3_44100_64",
        timeout_seconds: int = 30,
        max_attempts: int = 3,
        transport: Transport = urllib_transport,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if not api_key or not api_key.isprintable() or len(api_key) > 200:
            raise ValueError("the ElevenLabs API key is 1-200 printable characters")
        if not voices or not all(_VOICE_ID.fullmatch(v) for v in voices.values()):
            raise ValueError("voices map short codes to ElevenLabs voice IDs")
        if model not in MODELS:
            raise ValueError(f"the model is one of {sorted(MODELS)}")
        if _MP3_FORMAT.fullmatch(output_format) is None:
            raise ValueError("the output format is an ElevenLabs mp3_* format")
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("the speech timeout is 1-120 seconds")
        if not 1 <= max_attempts <= 3:
            raise ValueError("speech makes 1-3 attempts")
        self._key = api_key
        self._voices = dict(voices)
        self._model = model
        self._format = output_format
        self._timeout = timeout_seconds
        self._attempts = max_attempts
        self._transport = transport
        self._sleep = sleep

    def describe(self) -> AiCapabilities:
        return AiCapabilities(available=True, provider=PROVIDER)

    async def speak(self, request: SpeechRequest) -> SpeechAudio:
        voice_id = self._voices.get(request.voice)
        if voice_id is None:
            raise SpeechProviderError("unknown_voice")
        call = urllib.request.Request(
            f"{API_URL}{voice_id}?output_format={self._format}",
            data=json.dumps({"text": request.text, "model_id": self._model}).encode(),
            headers={
                "Accept": "audio/mpeg",
                "Content-Type": "application/json",
                "User-Agent": "FlakePilot",
                "xi-api-key": self._key,
            },
            method="POST",
        )
        for attempt in range(1, self._attempts + 1):
            try:
                status, content_type, body = await asyncio.wait_for(
                    asyncio.to_thread(self._transport, call, self._timeout),
                    timeout=self._timeout,
                )
            # OSError includes TimeoutError; HTTPException is a broken answer.
            except (OSError, http.client.HTTPException):
                status, content_type, body = 0, "", b""
            if status == 0:
                # Raised outside the handler so no transport text is chained.
                raise SpeechProviderError("provider_unreachable", unavailable=True)
            if status == 200:
                return self._audio(content_type, body)
            reason, unavailable, retryable = _refusal(status, body)
            if not retryable or attempt == self._attempts:
                raise SpeechProviderError(reason, unavailable=unavailable)
            await self._sleep(0.5 * 2 ** (attempt - 1))
        raise AssertionError("unreachable")  # pragma: no cover

    def _audio(self, content_type: str, body: bytes) -> SpeechAudio:
        if content_type.split(";")[0].strip().lower() != "audio/mpeg":
            raise SpeechProviderError("bad_response", unavailable=True)
        if len(body) > MAX_AUDIO_BYTES:
            raise SpeechProviderError("audio_too_large", unavailable=True)
        if not _is_mp3(body):
            raise SpeechProviderError("bad_response", unavailable=True)
        return SpeechAudio(
            audio=body, media_type="audio/mpeg", provider=PROVIDER, model=self._model
        )
