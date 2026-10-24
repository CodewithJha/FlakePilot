"""Speech provider adapters: a deterministic recorded fixture for local truth.

The fixture synthesizes a small, validity-shaped audio object — fixed WAV
framing plus a content-bound payload — so the opt-in speech path (ADR-017;
goal 14) is testable without credentials. A live ElevenLabs transport adapter
enters only after owner-approved credentials and a verified field list; the
text arriving here must already be a summarized brief without secrets, and
this module treats it as exactly that: opaque bytes to frame.
"""

import hashlib

from flakepilot.integrations.application.ports import (
    AiCapabilities,
    SpeechAudio,
    SpeechProviderError,
    SpeechRequest,
)

PROVIDER = "recorded_speech"
MODEL = "recorded_tone"


class RecordedSpeechProvider:
    """Deterministic, credential-free speech. The "audio" is a minimal, well-
    formed WAV (44-byte canonical header) whose samples derive from the brief
    text's SHA-256: same text, same voice, same audio, always — a spoken
    verification token, not a plausible voice model."""

    def __init__(self, seconds: int = 1, rate: int = 8_000) -> None:
        if not 1 <= seconds <= 60 or not 8_000 <= rate <= 96_000:
            raise ValueError("fixture speech is 1-60 seconds at 8k-96k Hz")
        if seconds * rate > 2_000_000:
            # The port's 2 MiB cap is a promise: no configuration may exceed it.
            raise ValueError("fixture speech audio must stay under the 2 MiB cap")
        self._seconds = seconds
        self._rate = rate

    def describe(self) -> AiCapabilities:
        return AiCapabilities(available=True, provider=PROVIDER)

    async def speak(self, request: SpeechRequest) -> SpeechAudio:
        # The port contract validated the text; here it is only hashed, sent
        # nowhere, and never echoed in errors.
        try:
            digest = hashlib.sha256(request.text.encode("utf-8", "ignore")).digest()
            voice_seed = hashlib.sha256(
                request.voice.encode("utf-8", "ignore")
            ).digest()
            audio = _wav(
                _samples(digest, voice_seed, self._seconds, self._rate), self._rate
            )
        except (ValueError, TypeError) as error:
            raise SpeechProviderError("bad_request", unavailable=False) from error
        return SpeechAudio(
            audio=audio,
            media_type="audio/wav",
            provider=PROVIDER,
            model=MODEL,
        )


def _samples(seed: bytes, voice: bytes, seconds: int, rate: int) -> bytes:
    """Frame-alternating sawtooth deterministically derived from the seeds.
    Deterministic everywhere: pure math over given bytes, in integer PWM
    offsets — no clock, no host randomness."""
    combined = (int.from_bytes(seed, "big") + int.from_bytes(voice, "big")) % 256 | 1
    total = seconds * rate
    pitch = 64
    return bytes(
        128 + ((offset * combined) // 96 % pitch) - pitch // 2
        for offset in range(total)
    )


def _wav(samples: bytes, rate: int) -> bytes:
    """A canonical 44-byte RIFF/WAVE header for signed 8-bit mono PCM."""
    channel_count, depth = 1, 1
    byte_rate = rate * channel_count * depth
    return (
        b"RIFF"
        + (36 + len(samples)).to_bytes(4, "little")
        + b"WAVE"
        + b"fmt "
        + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little")  # PCM
        + channel_count.to_bytes(2, "little")
        + rate.to_bytes(4, "little")
        + byte_rate.to_bytes(4, "little")
        + (channel_count * depth).to_bytes(2, "little")
        + (depth * 8).to_bytes(2, "little")
        + b"data"
        + len(samples).to_bytes(4, "little")
        + samples
    )
