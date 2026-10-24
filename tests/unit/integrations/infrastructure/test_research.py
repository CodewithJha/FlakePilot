"""ResearchProvider port contract over the recorded fixture (goal 8).

Local truth is a canned corpus; every test exercises the real adapter path:
budget honoring, URL deduplication, provenance validation, fail-closed
behavior on malformed entries, and the untrusted-content rule (an excerpt's
shaped like instructions must still be data — a property the framing in the
assist pipeline enforces downstream, verified here only in that the adapter
never rewrites or interprets it).
"""

import asyncio
from datetime import UTC, datetime

import pytest

from flakepilot.integrations.application.ports import (
    ResearchProviderError,
    ResearchQuery,
    ResearchSource,
)
from flakepilot.integrations.infrastructure.research import (
    PROVIDER,
    RecordedResearchProvider,
)

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def _source(url: str, *, title: str = "t", topic: str = "playwright") -> dict[str, str]:
    return {
        "url": url,
        "title": title,
        "retrieved_at": T0.isoformat(),
        "excerpt": f"discussion of {topic}",
    }


def _corpus() -> dict[str, tuple[dict[str, str], ...]]:
    return {
        "flaky": (
            _source("https://example.test/a", title="first flaky"),
            _source("https://example.test/b", title="second flaky"),
        ),
        "playwright": (
            _source("https://example.test/b", title="same page again"),
            _source("https://example.test/c", title="pw docs"),
        ),
    }


def _provider() -> RecordedResearchProvider:
    return RecordedResearchProvider(_corpus())


def test_describe_announces_the_fixture_provider() -> None:
    assert _provider().describe().provider == PROVIDER


def test_a_query_yields_valid_sources_with_provenance() -> None:
    sources = asyncio.run(_provider().search(ResearchQuery("flaky playwright")))
    assert all(isinstance(s, ResearchSource) for s in sources)
    assert all(s.retrieved_at == T0 for s in sources)
    assert all(s.url.startswith("https://") for s in sources)


def test_the_budget_bounds_the_answer() -> None:
    sources = asyncio.run(_provider().search(ResearchQuery("flaky", max_sources=1)))
    assert len(sources) == 1


def test_a_url_retrieved_twice_appears_once() -> None:
    sources = asyncio.run(_provider().search(ResearchQuery("flaky playwright")))
    urls = [source.url for source in sources]
    assert urls == [
        "https://example.test/a",
        "https://example.test/b",
        "https://example.test/c",
    ]
    assert len(set(urls)) == len(urls)


def test_round_robin_keeps_one_topic_from_evicting_the_others() -> None:
    corpus: dict[str, tuple[dict[str, str], ...]] = {
        "crowd": tuple(_source(f"https://example.test/{index}") for index in range(9)),
        "rare": (_source("https://example.test/rare"),),
    }
    sources = asyncio.run(
        RecordedResearchProvider(corpus).search(
            ResearchQuery("crowd rare", max_sources=2)
        )
    )
    assert [source.url for source in sources] == [
        "https://example.test/0",
        "https://example.test/rare",
    ]


def test_an_unknown_query_is_an_empty_answer_not_an_error() -> None:
    assert asyncio.run(_provider().search(ResearchQuery("esoteric-nonsense"))) == ()


def test_a_malformed_entry_fails_closed() -> None:
    corpus: dict[str, tuple[dict[str, str], ...]] = {
        "bad": (
            {
                "url": "not-a-url",
                "title": "t",
                "retrieved_at": T0.isoformat(),
                "excerpt": "x",
            },
        )
    }
    with pytest.raises(ResearchProviderError) as error:
        asyncio.run(RecordedResearchProvider(corpus).search(ResearchQuery("bad")))
    assert error.value.reason == "bad_source"


def test_a_malformed_timestamp_fails_closed() -> None:
    corpus: dict[str, tuple[dict[str, str], ...]] = {
        "bad": (
            {
                "url": "https://x.test/a",
                "title": "t",
                "retrieved_at": "not-a-time",
                "excerpt": "x",
            },
        )
    }
    with pytest.raises(ResearchProviderError) as error:
        asyncio.run(RecordedResearchProvider(corpus).search(ResearchQuery("bad")))
    assert error.value.reason == "bad_source"


def test_query_shapes_are_refused_upstream() -> None:
    with pytest.raises(ValueError):
        ResearchQuery("")
    with pytest.raises(ValueError):
        ResearchQuery("x", max_sources=11)


def test_excerpts_cannot_beyond_the_cap() -> None:
    with pytest.raises(ValueError):
        ResearchSource(
            url="https://x.test/a",
            title="t",
            retrieved_at=T0,
            excerpt="x" * 9_000,
        )


def test_the_same_query_is_always_the_same_answer() -> None:
    first = asyncio.run(_provider().search(ResearchQuery("flaky")))
    second = asyncio.run(_provider().search(ResearchQuery("flaky")))
    assert first == second
