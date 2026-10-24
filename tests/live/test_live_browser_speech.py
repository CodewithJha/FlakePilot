"""One real request per browser and speech provider (goals 3 and 14). Owner-run only.

Each test is skipped unless its key is in the environment, so the default
suite and CI never contact a provider. A skip is not verification. Running
these spends real quota (one Fetch call; about fifty characters of speech):

    FLAKEPILOT_LIVE_BROWSERBASE_API_KEY=... uv run pytest tests/live -k browserbase
    FLAKEPILOT_LIVE_ELEVENLABS_API_KEY=... FLAKEPILOT_LIVE_ELEVENLABS_VOICE_ID=... \\
        uv run pytest tests/live -k elevenlabs
"""

import asyncio
import os

import pytest

from flakepilot.integrations.application.browser import BrowserCaptureRequest
from flakepilot.integrations.application.ports import SpeechRequest
from flakepilot.integrations.infrastructure.browserbase import BrowserbaseEvidence
from flakepilot.integrations.infrastructure.elevenlabs import ElevenLabsSpeech

BROWSERBASE_KEY = os.environ.get("FLAKEPILOT_LIVE_BROWSERBASE_API_KEY", "")
ELEVENLABS_KEY = os.environ.get("FLAKEPILOT_LIVE_ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE = os.environ.get("FLAKEPILOT_LIVE_ELEVENLABS_VOICE_ID", "")


@pytest.mark.skipif(
    not BROWSERBASE_KEY, reason="FLAKEPILOT_LIVE_BROWSERBASE_API_KEY is not set"
)
def test_live_browserbase_fetch_captures_one_allowlisted_page() -> None:
    adapter = BrowserbaseEvidence(
        api_key=BROWSERBASE_KEY,
        allowed_hosts={"docs.python.org"},
    )
    capture = asyncio.run(
        adapter.capture(
            BrowserCaptureRequest(("https://docs.python.org/3/",), timeout_seconds=60)
        )
    )
    assert capture.refused == ()
    (evidence,) = capture.evidence
    assert evidence.size > 0 and len(evidence.sha256) == 64
    assert evidence.capture_id


@pytest.mark.skipif(
    not (ELEVENLABS_KEY and ELEVENLABS_VOICE),
    reason="FLAKEPILOT_LIVE_ELEVENLABS_API_KEY or _VOICE_ID is not set",
)
def test_live_elevenlabs_speaks_a_short_brief_as_mp3() -> None:
    adapter = ElevenLabsSpeech(
        api_key=ELEVENLABS_KEY, voices={"default": ELEVENLABS_VOICE}
    )
    audio = asyncio.run(
        adapter.speak(SpeechRequest("FlakePilot investigation brief. One flaky test."))
    )
    assert audio.media_type == "audio/mpeg" and len(audio.audio) > 1_000
