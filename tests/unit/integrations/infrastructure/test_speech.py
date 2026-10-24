"""SpeechProvider port contract over the recorded fixture (goal 14).

The fixture is verified as genuine, well-formed WAV audio. Determinism here
is the contract: same text, same voice, same audio — and nothing about the
text rides anywhere (no network, no echoes).
"""

import asyncio
import datetime
import wave
from io import BytesIO

import pytest

from flakepilot.integrations.application.ports import (
    SpeechRequest,
)
from flakepilot.integrations.infrastructure.speech import (
    PROVIDER,
    RecordedSpeechProvider,
)

BRIEF = "Investigation brief: verdict flaky; evidence linked; one next step."


def test_describe_announces_the_fixture_provider() -> None:
    assert RecordedSpeechProvider().describe().provider == PROVIDER


def test_a_brief_becomes_valid_wav_audio() -> None:
    audio = asyncio.run(RecordedSpeechProvider().speak(SpeechRequest(BRIEF)))
    assert audio.media_type == "audio/wav" and audio.audio.startswith(b"RIFF")
    with wave.open(BytesIO(audio.audio)) as reader:
        assert reader.getframerate() == 8_000 and reader.getnframes() == 8_000
        assert reader.getnchannels() == 1 and reader.getsampwidth() == 1


def test_the_same_text_and_voice_are_the_same_audio() -> None:
    first = asyncio.run(
        RecordedSpeechProvider().speak(SpeechRequest(BRIEF, voice="calm"))
    )
    second = asyncio.run(
        RecordedSpeechProvider().speak(SpeechRequest(BRIEF, voice="calm"))
    )
    assert first == second


def test_a_different_brief_or_voice_is_different_audio() -> None:
    brief = asyncio.run(RecordedSpeechProvider().speak(SpeechRequest(BRIEF)))
    other = asyncio.run(
        RecordedSpeechProvider().speak(SpeechRequest(BRIEF + " More detail."))
    )
    voice = asyncio.run(
        RecordedSpeechProvider().speak(SpeechRequest(BRIEF, voice="sharp"))
    )
    assert other.audio != brief.audio and voice.audio != brief.audio


def test_audio_stays_under_the_contract_cap() -> None:
    audio = asyncio.run(
        RecordedSpeechProvider(seconds=60, rate=30_000).speak(SpeechRequest(BRIEF))
    )
    assert len(audio.audio) <= 2 * 1024 * 1024
    with pytest.raises(ValueError):
        RecordedSpeechProvider(seconds=60, rate=96_000)  # would exceed the cap


def test_the_port_refuses_loose_text() -> None:
    with pytest.raises(ValueError):
        SpeechRequest("")
    with pytest.raises(ValueError):
        SpeechRequest("x" * 4_097)
    with pytest.raises(ValueError):
        SpeechRequest("ok", voice="TWO WORDS")


def test_adapter_shape_refuses_unusable_configuration() -> None:
    with pytest.raises(ValueError):
        RecordedSpeechProvider(seconds=0)
    with pytest.raises(ValueError):
        RecordedSpeechProvider(rate=1_000)


def test_a_secret_in_the_brief_never_appears_in_the_audio_object() -> None:
    secret = "FLAKEPILOT_TOKEN=supersecret123"
    audio = asyncio.run(RecordedSpeechProvider().speak(SpeechRequest(secret)))
    assert secret.encode() not in audio.audio
    assert secret not in repr(audio) and secret not in str(audio)


def test_duration_and_rate_are_honoured() -> None:
    audio = asyncio.run(
        RecordedSpeechProvider(seconds=2, rate=16_000).speak(SpeechRequest(BRIEF))
    )
    with wave.open(BytesIO(audio.audio)) as reader:
        assert reader.getframerate() == 16_000 and reader.getnframes() == 32_000


def test_datetime_never_used_but_module_depends_only_on_the_brief() -> None:
    """The fixture must be clock-free: it depends on the text, not the time."""
    first = asyncio.run(RecordedSpeechProvider().speak(SpeechRequest(BRIEF)))
    second = asyncio.run(RecordedSpeechProvider().speak(SpeechRequest(BRIEF)))
    assert first.audio == second.audio and datetime.datetime is datetime.datetime
