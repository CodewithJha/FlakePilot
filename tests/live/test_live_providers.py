"""One real request per hosted provider (goals 1 and 8). Owner-run only.

Each test is skipped unless its key is in the environment, so the default
suite and CI never contact a provider. A skip is not verification. Running
these spends real quota:

    FLAKEPILOT_LIVE_GEMINI_API_KEY=... [FLAKEPILOT_LIVE_GEMMA_MODEL=gemma-4-31b-it] \\
        uv run pytest tests/live -k gemma
    FLAKEPILOT_LIVE_SERPAPI_API_KEY=... uv run pytest tests/live -k serpapi
"""

import asyncio
import os

import pytest

from flakepilot.diagnosis.domain.output import validate_output
from flakepilot.integrations.application.ports import (
    AiDataPart,
    AiRequest,
    ResearchQuery,
)
from flakepilot.integrations.infrastructure.gemma import GemmaAssist
from flakepilot.integrations.infrastructure.serpapi import SerpApiResearch

GEMINI_KEY = os.environ.get("FLAKEPILOT_LIVE_GEMINI_API_KEY", "")
GEMMA_MODEL = os.environ.get("FLAKEPILOT_LIVE_GEMMA_MODEL") or "gemma-4-26b-a4b-it"
SERPAPI_KEY = os.environ.get("FLAKEPILOT_LIVE_SERPAPI_API_KEY", "")


@pytest.mark.skipif(not GEMINI_KEY, reason="FLAKEPILOT_LIVE_GEMINI_API_KEY is not set")
def test_live_gemma_returns_a_schema_valid_answer() -> None:
    adapter = GemmaAssist(api_key=GEMINI_KEY, model_id=GEMMA_MODEL)
    response = asyncio.run(
        adapter.submit(
            AiRequest(
                instructions=(
                    "You propose causes of a failing test. Answer with one JSON "
                    'object only, shaped {"hypotheses": [{"summary": string, '
                    '"cause": lowercase_snake_case, "citations": []}]}, at most '
                    "two hypotheses, no prose and no Markdown."
                ),
                data_parts=(
                    AiDataPart(
                        name="failure",
                        text="test_checkout timed out after 30s waiting for a "
                        "network response; it passed on rerun.",
                    ),
                ),
                max_output_bytes=65_536,
            )
        )
    )
    assert response.available, response.reason
    assert response.provider == "gemini_api"
    assert response.model is not None and response.model.startswith("gemma_")
    assert response.output is not None
    assert validate_output(response.output).ok


@pytest.mark.skipif(
    not SERPAPI_KEY, reason="FLAKEPILOT_LIVE_SERPAPI_API_KEY is not set"
)
def test_live_serpapi_returns_bounded_https_sources() -> None:
    provider = SerpApiResearch(api_key=SERPAPI_KEY)
    sources = asyncio.run(
        provider.search(ResearchQuery("pytest flaky test rerun", max_sources=3))
    )
    assert 1 <= len(sources) <= 3
    assert len({source.url for source in sources}) == len(sources)
    for source in sources:
        assert source.url.startswith("https://")
        assert source.retrieved_at.tzinfo is not None
