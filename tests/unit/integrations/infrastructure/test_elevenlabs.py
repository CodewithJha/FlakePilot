"""The ElevenLabs speech adapter over canned HTTP exchanges (goal 14).

An injected transport stands in for ElevenLabs. The adapter must validate
audio before trusting it, keep the key in its header only, map failures to
short codes without echoing provider text, and retry only what is retryable.
"""

import asyncio
import http.client
import json
import time
import urllib.request

import pytest

from flakepilot.integrations.application.ports import SpeechProviderError, SpeechRequest
from flakepilot.integrations.infrastructure.elevenlabs import (
    API_URL,
    PROVIDER,
    ElevenLabsSpeech,
)

KEY = "sk_eleven_secret_456"
VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"
BRIEF = "FlakePilot investigation brief. Verdict: 1 flaky."
MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" + b"\x00" * 64


class RecordedTransport:
    def __init__(self, *answers: tuple[int, str, bytes] | Exception) -> None:
        self.answers = list(answers)
        self.requests: list[urllib.request.Request] = []

    def __call__(
        self, request: urllib.request.Request, timeout: float
    ) -> tuple[int, str, bytes]:
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def _detail(code: str, message: str = "provider prose with secrets") -> bytes:
    return json.dumps({"detail": {"code": code, "message": message}}).encode()


def _adapter(
    transport: RecordedTransport, sleeps: Sleeps | None = None, **kw: object
) -> ElevenLabsSpeech:
    params: dict[str, object] = {
        "api_key": KEY,
        "voices": {"default": VOICE_ID},
        "transport": transport,
        "sleep": sleeps or Sleeps(),
    }
    params.update(kw)
    return ElevenLabsSpeech(**params)  # type: ignore[arg-type]


def _speak(adapter: ElevenLabsSpeech, voice: str = "default"):  # type: ignore[no-untyped-def]
    return asyncio.run(adapter.speak(SpeechRequest(BRIEF, voice=voice)))


def _refused(adapter: ElevenLabsSpeech) -> SpeechProviderError:
    with pytest.raises(SpeechProviderError) as caught:
        _speak(adapter)
    error = caught.value
    chain: BaseException | None = error
    while chain is not None:
        assert KEY not in str(chain) and "prose" not in str(chain)
        chain = chain.__cause__ or chain.__context__
    return error


def test_a_brief_becomes_validated_mp3_audio() -> None:
    transport = RecordedTransport((200, "audio/mpeg", MP3))
    audio = _speak(_adapter(transport))
    assert audio.audio == MP3 and audio.media_type == "audio/mpeg"
    assert audio.provider == PROVIDER and audio.model == "eleven_flash_v2_5"
    call = transport.requests[0]
    assert call.full_url == f"{API_URL}{VOICE_ID}?output_format=mp3_44100_64"
    assert call.get_method() == "POST" and call.get_header("Xi-api-key") == KEY
    sent = json.loads(call.data.decode())  # type: ignore[union-attr]
    assert sent == {"text": BRIEF, "model_id": "eleven_flash_v2_5"}
    assert KEY not in call.data.decode()  # type: ignore[union-attr]


def test_a_frame_sync_without_id3_is_audio_too() -> None:
    body = b"\xff\xf3\x64\xc4" + b"\x00" * 32
    audio = _speak(_adapter(RecordedTransport((200, "audio/mpeg; charset=x", body))))
    assert audio.audio == body


@pytest.mark.parametrize(
    "answer",
    [
        (200, "application/json", MP3),  # wrong content type
        (200, "audio/mpeg", b""),  # empty
        (200, "audio/mpeg", b"<html>not audio</html>"),  # not mp3 framing
        (200, "audio/mpeg", b"\xff"),  # truncated frame header
    ],
)
def test_unusable_audio_is_refused(answer: tuple[int, str, bytes]) -> None:
    assert _refused(_adapter(RecordedTransport(answer))).reason == "bad_response"


def test_audio_over_the_cap_is_refused() -> None:
    body = MP3 + b"\x00" * (2 * 1024 * 1024)
    error = _refused(_adapter(RecordedTransport((200, "audio/mpeg", body))))
    assert error.reason == "audio_too_large"


def test_an_invalid_key_is_unauthorized_without_retry() -> None:
    transport = RecordedTransport((401, "", _detail("invalid_api_key")))
    error = _refused(_adapter(transport))
    assert error.reason == "unauthorized" and not error.unavailable
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    "answer",
    [
        (401, "", json.dumps({"detail": {"status": "quota_exceeded"}}).encode()),
        (402, "", _detail("insufficient_credits")),
        (429, "", _detail("quota_exceeded")),
    ],
)
def test_quota_exhaustion_is_never_retried(answer: tuple[int, str, bytes]) -> None:
    transport = RecordedTransport(answer)
    assert _refused(_adapter(transport)).reason == "quota_exceeded"
    assert len(transport.requests) == 1


def test_rate_limits_retry_with_backoff_then_fail_unavailable() -> None:
    sleeps = Sleeps()
    transport = RecordedTransport(*[(429, "", _detail("system_busy"))] * 3)
    error = _refused(_adapter(transport, sleeps))
    assert error.reason == "rate_limited" and error.unavailable
    assert len(transport.requests) == 3 and sleeps.delays == [0.5, 1.0]


def test_a_transient_5xx_recovers_within_the_retry_bound() -> None:
    transport = RecordedTransport((503, "", b""), (200, "audio/mpeg", MP3))
    assert _speak(_adapter(transport)).audio == MP3
    assert len(transport.requests) == 2


def test_persistent_5xx_fails_after_the_bound() -> None:
    transport = RecordedTransport(*[(500, "", b"traceback")] * 3)
    error = _refused(_adapter(transport))
    assert error.reason == "provider_http_error" and error.unavailable
    assert len(transport.requests) == 3


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (403, "forbidden"),
        (404, "unknown_voice"),
        (422, "bad_request"),
        (400, "bad_request"),
    ],
)
def test_request_errors_are_refusals(status: int, reason: str) -> None:
    transport = RecordedTransport((status, "", b"{not json"))
    error = _refused(_adapter(transport))
    assert error.reason == reason and len(transport.requests) == 1


def test_a_timeout_or_network_failure_is_unreachable_without_the_key() -> None:
    for failure in (
        TimeoutError(),
        ConnectionError(f"refused {KEY}"),
        http.client.IncompleteRead(KEY.encode()),
    ):
        error = _refused(_adapter(RecordedTransport(failure)))
        assert error.reason == "provider_unreachable" and error.unavailable


def test_a_slow_provider_is_cut_off_by_the_timeout() -> None:
    def slow(request: urllib.request.Request, timeout: float) -> tuple[int, str, bytes]:
        time.sleep(1.5)
        return 200, "audio/mpeg", MP3

    adapter = ElevenLabsSpeech(
        api_key=KEY, voices={"default": VOICE_ID}, timeout_seconds=1, transport=slow
    )
    with pytest.raises(SpeechProviderError) as caught:
        _speak(adapter)
    assert caught.value.reason == "provider_unreachable"


def test_an_unmapped_voice_is_refused_without_contact() -> None:
    transport = RecordedTransport()
    with pytest.raises(SpeechProviderError) as caught:
        _speak(_adapter(transport), voice="narrator")
    assert caught.value.reason == "unknown_voice" and transport.requests == []


def test_constructor_refuses_unusable_configuration() -> None:
    base: dict[str, object] = {"api_key": KEY, "voices": {"default": VOICE_ID}}
    overrides: tuple[dict[str, object], ...] = (
        {"api_key": ""},
        {"voices": {}},
        {"voices": {"default": "../../admin"}},
        {"model": "eleven_v9"},
        {"output_format": "pcm_44100"},
        {"output_format": "mp3_44100_128&enable_logging=false"},
        {"timeout_seconds": 0},
        {"max_attempts": 9},
    )
    for override in overrides:
        with pytest.raises(ValueError):
            ElevenLabsSpeech(**{**base, **override})  # type: ignore[arg-type]
